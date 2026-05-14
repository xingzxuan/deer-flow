"""Self-tests for the boundary scanner in ``test_workspace_boundary``.

These tests guard against the scanner silently passing because it cannot
detect anything. They feed synthetic ``.py`` snippets to the per-file
collector and assert it produces (or correctly suppresses) the expected
hits — so we know the integration test in ``test_workspace_boundary.py``
would actually fail if a real violation appeared.
"""

from __future__ import annotations

from pathlib import Path

from test_workspace_boundary import collect_runtime_checkpoint_imports


def _write(tmp_path: Path, body: str) -> Path:
    target = tmp_path / "sample.py"
    target.write_text(body, encoding="utf-8")
    return target


def test_flags_direct_from_import(tmp_path: Path) -> None:
    target = _write(
        tmp_path,
        "from langgraph.checkpoint.postgres import AsyncPostgresSaver\n",
    )
    hits = collect_runtime_checkpoint_imports(target)
    assert hits == [(1, "langgraph.checkpoint.postgres")]


def test_flags_bare_module_import(tmp_path: Path) -> None:
    target = _write(
        tmp_path,
        "import langgraph.checkpoint.memory  # noqa: F401\n",
    )
    hits = collect_runtime_checkpoint_imports(target)
    assert hits == [(1, "langgraph.checkpoint.memory")]


def test_flags_third_party_checkpoint_package(tmp_path: Path) -> None:
    target = _write(
        tmp_path,
        "from langgraph_checkpoint_postgres import PostgresSaver\nfrom langgraph_checkpoint_sqlite import SqliteSaver\n",
    )
    hits = collect_runtime_checkpoint_imports(target)
    assert (1, "langgraph_checkpoint_postgres") in hits
    assert (2, "langgraph_checkpoint_sqlite") in hits


def test_skips_type_checking_block_name_form(tmp_path: Path) -> None:
    target = _write(
        tmp_path,
        "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from langgraph.checkpoint.base import BaseCheckpointSaver  # noqa: F401\n",
    )
    assert collect_runtime_checkpoint_imports(target) == []


def test_skips_type_checking_block_attribute_form(tmp_path: Path) -> None:
    target = _write(
        tmp_path,
        "import typing\n\nif typing.TYPE_CHECKING:\n    from langgraph.checkpoint.base import BaseCheckpointSaver  # noqa: F401\n",
    )
    assert collect_runtime_checkpoint_imports(target) == []


def test_skips_nested_type_checking_block(tmp_path: Path) -> None:
    target = _write(
        tmp_path,
        "from typing import TYPE_CHECKING\n\nif True:\n    if TYPE_CHECKING:\n        from langgraph.checkpoint.postgres import AsyncPostgresSaver  # noqa: F401\n",
    )
    assert collect_runtime_checkpoint_imports(target) == []


def test_does_not_flag_unrelated_imports(tmp_path: Path) -> None:
    target = _write(
        tmp_path,
        "import os\nfrom langgraph.graph.state import CompiledStateGraph  # noqa: F401\nfrom app.gateway.deps import get_checkpointer  # noqa: F401\n",
    )
    assert collect_runtime_checkpoint_imports(target) == []


def test_does_not_flag_string_literal_with_module_name(tmp_path: Path) -> None:
    target = _write(
        tmp_path,
        'TARGET = "langgraph.checkpoint.postgres"\n',
    )
    assert collect_runtime_checkpoint_imports(target) == []


def test_handles_syntax_error_gracefully(tmp_path: Path) -> None:
    target = _write(tmp_path, "def broken(:\n")
    assert collect_runtime_checkpoint_imports(target) == []
