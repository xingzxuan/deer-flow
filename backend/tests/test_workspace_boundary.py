"""Boundary check: only allowlisted modules may directly import LangGraph
checkpoint/saver clients.

Direct use of the LangGraph checkpoint API anywhere outside the gateway thread
plumbing and the harness checkpointer factory is a workspace-isolation hazard:
arbitrary code paths could otherwise reach across threads/workspaces by
constructing their own savers. PR7 enforces this with an AST static scan.

Imports inside ``if TYPE_CHECKING:`` blocks are intentionally ignored — they
never execute at runtime and therefore cannot bypass the boundary.

Allowlist lives in ``tests/boundary_allowlist.toml`` as a plain list of paths
relative to ``backend/``. Adding a new legitimate importer means appending a
line there in the same PR that introduces the import.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

BACKEND_ROOT = Path(__file__).parent.parent  # backend/
ALLOWLIST_FILE = Path(__file__).parent / "boundary_allowlist.toml"

# Match any submodule of these top-level packages.
TARGET_MODULE_PREFIXES: tuple[str, ...] = (
    "langgraph.checkpoint",
    "langgraph_checkpoint_postgres",
    "langgraph_checkpoint_sqlite",
)

# Directories under backend/ that are not part of the running app.
EXCLUDED_TOP_LEVEL = ("tests", ".venv", "build", "dist", "docs", ".pytest_cache", "node_modules")


def _matches_target(name: str) -> bool:
    return any(name == prefix or name.startswith(prefix + ".") for prefix in TARGET_MODULE_PREFIXES)


def _build_parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents


def _is_type_checking_test(test: ast.expr) -> bool:
    """Return True for ``TYPE_CHECKING`` or ``typing.TYPE_CHECKING`` conditions."""
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
        return True
    return False


def _inside_type_checking(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    current = parents.get(id(node))
    while current is not None:
        if isinstance(current, ast.If) and _is_type_checking_test(current.test):
            return True
        current = parents.get(id(current))
    return False


def collect_runtime_checkpoint_imports(filepath: Path) -> list[tuple[int, str]]:
    """Return ``(lineno, module_path)`` for every runtime import that targets a banned module."""
    source = filepath.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return []

    parents = _build_parent_map(tree)
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _matches_target(module) and not _inside_type_checking(node, parents):
                hits.append((node.lineno, module))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if _matches_target(alias.name) and not _inside_type_checking(node, parents):
                    hits.append((node.lineno, alias.name))
    return hits


def _iter_backend_py_files() -> list[Path]:
    candidates: list[Path] = []
    for path in sorted(BACKEND_ROOT.rglob("*.py")):
        rel_parts = path.relative_to(BACKEND_ROOT).parts
        if rel_parts and rel_parts[0] in EXCLUDED_TOP_LEVEL:
            continue
        if "__pycache__" in rel_parts:
            continue
        candidates.append(path)
    return candidates


def _load_allowlist() -> set[str]:
    data = tomllib.loads(ALLOWLIST_FILE.read_text(encoding="utf-8"))
    raw = data.get("langgraph_checkpoint_importers", [])
    return set(raw)


def scan_violations(allowlist: set[str]) -> list[str]:
    """Return formatted violation lines (one per banned import outside the allowlist)."""
    violations: list[str] = []
    for py in _iter_backend_py_files():
        rel = py.relative_to(BACKEND_ROOT).as_posix()
        for lineno, module in collect_runtime_checkpoint_imports(py):
            if rel in allowlist:
                continue
            violations.append(f"  {rel}:{lineno}  imports {module}")
    return violations


def test_only_allowlisted_modules_import_langgraph_checkpoint() -> None:
    allowlist = _load_allowlist()
    violations = scan_violations(allowlist)
    assert not violations, (
        "Unauthorized direct imports of langgraph.checkpoint.* detected. "
        "Either route the access through `app.gateway.deps.get_checkpointer` "
        "or, if this is a legitimate new importer, add the path to "
        "tests/boundary_allowlist.toml in the same PR.\n" + "\n".join(violations)
    )
