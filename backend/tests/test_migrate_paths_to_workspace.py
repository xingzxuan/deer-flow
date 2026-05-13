"""PR6 T6.13 — workspace-path migration script tests.

Builds the legacy ``users/{uid}/...`` tree under a temp base dir,
points a populated sqlite ``users`` table at it via the conventional
``deer-flow.db`` location, and asserts the migration produces the new
``workspaces/{wid}/...`` layout. Covers:

- threads / memory.json / custom agents all rewritten under the workspace
- ``--dry-run`` writes nothing
- pre-existing destinations get diverted to ``migration-conflicts/``
- users without a ``default_workspace_id`` fall back to the explicit flag
- empty legacy dirs are cleaned up
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from deerflow.config.paths import Paths
from scripts.migrate_paths_to_workspace import (
    LEGACY_WORKSPACE_FALLBACK,
    _load_user_workspaces,
    migrate,
)


def _build_legacy_tree(base: Path, *, user_id: str, thread_ids: tuple[str, ...] = (), with_memory: bool = False, agent_names: tuple[str, ...] = ()) -> None:
    user_root = base / "users" / user_id
    user_root.mkdir(parents=True, exist_ok=True)
    for tid in thread_ids:
        (user_root / "threads" / tid / "user-data" / "workspace").mkdir(parents=True, exist_ok=True)
        (user_root / "threads" / tid / "user-data" / "workspace" / "marker.txt").write_text(f"{user_id}/{tid}", encoding="utf-8")
    if with_memory:
        (user_root / "memory.json").write_text(f'{{"user_id": "{user_id}"}}', encoding="utf-8")
    for name in agent_names:
        (user_root / "agents" / name).mkdir(parents=True, exist_ok=True)
        (user_root / "agents" / name / "SOUL.md").write_text(f"# {name}", encoding="utf-8")


def _seed_db(base: Path, *, users: dict[str, str | None]) -> None:
    db_path = base / "deer-flow.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY, default_workspace_id TEXT)")
        conn.executemany("INSERT INTO users (id, default_workspace_id) VALUES (?, ?)", list(users.items()))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return tmp_path


def test_migrate_threads_under_workspace(base: Path):
    _build_legacy_tree(base, user_id="alice", thread_ids=("t1",))
    _seed_db(base, users={"alice": "ws-alpha"})
    paths = Paths(base)

    report = migrate(paths, user_workspaces=_load_user_workspaces(paths), fallback_workspace=LEGACY_WORKSPACE_FALLBACK, dry_run=False)

    assert (base / "workspaces" / "ws-alpha" / "threads" / "t1" / "user-data" / "workspace" / "marker.txt").read_text(encoding="utf-8") == "alice/t1"
    assert not (base / "users" / "alice" / "threads").exists()
    assert {entry["asset"] for entry in report} == {"thread"}


def test_migrate_memory_and_agents_nested_under_workspace_and_user(base: Path):
    _build_legacy_tree(base, user_id="alice", with_memory=True, agent_names=("code-reviewer",))
    _seed_db(base, users={"alice": "ws-alpha"})
    paths = Paths(base)

    migrate(paths, user_workspaces=_load_user_workspaces(paths), fallback_workspace=LEGACY_WORKSPACE_FALLBACK, dry_run=False)

    assert (base / "workspaces" / "ws-alpha" / "users" / "alice" / "memory.json").exists()
    assert (base / "workspaces" / "ws-alpha" / "users" / "alice" / "agents" / "code-reviewer" / "SOUL.md").exists()


def test_dry_run_writes_nothing(base: Path):
    _build_legacy_tree(base, user_id="alice", thread_ids=("t1",), with_memory=True, agent_names=("a1",))
    _seed_db(base, users={"alice": "ws-alpha"})
    paths = Paths(base)

    report = migrate(paths, user_workspaces=_load_user_workspaces(paths), fallback_workspace=LEGACY_WORKSPACE_FALLBACK, dry_run=True)

    # Source unchanged
    assert (base / "users" / "alice" / "threads" / "t1" / "user-data" / "workspace" / "marker.txt").exists()
    assert (base / "users" / "alice" / "memory.json").exists()
    assert (base / "users" / "alice" / "agents" / "a1" / "SOUL.md").exists()
    # No destination created
    assert not (base / "workspaces").exists()
    # Report still populated so operator sees what *would* happen
    assert len(report) == 3


def test_fallback_workspace_used_when_user_has_no_default(base: Path):
    _build_legacy_tree(base, user_id="alice", thread_ids=("t1",))
    _seed_db(base, users={"alice": None})
    paths = Paths(base)

    migrate(paths, user_workspaces=_load_user_workspaces(paths), fallback_workspace="legacy_workspace", dry_run=False)

    assert (base / "workspaces" / "legacy_workspace" / "threads" / "t1" / "user-data" / "workspace" / "marker.txt").exists()


def test_conflict_routes_legacy_to_migration_conflicts(base: Path):
    # Pre-create the destination with a different marker so the move sees a conflict.
    _build_legacy_tree(base, user_id="alice", thread_ids=("t1",))
    (base / "workspaces" / "ws-alpha" / "threads" / "t1" / "user-data" / "workspace").mkdir(parents=True)
    (base / "workspaces" / "ws-alpha" / "threads" / "t1" / "user-data" / "workspace" / "marker.txt").write_text("preexisting", encoding="utf-8")
    _seed_db(base, users={"alice": "ws-alpha"})
    paths = Paths(base)

    report = migrate(paths, user_workspaces=_load_user_workspaces(paths), fallback_workspace=LEGACY_WORKSPACE_FALLBACK, dry_run=False)

    assert (base / "workspaces" / "ws-alpha" / "threads" / "t1" / "user-data" / "workspace" / "marker.txt").read_text(encoding="utf-8") == "preexisting"
    conflict_marker = base / "migration-conflicts" / "workspace-migration" / "threads/ws-alpha" / "t1"
    assert conflict_marker.exists()
    assert any("conflict" in entry["action"] for entry in report)


def test_empty_users_dir_removed_after_full_migration(base: Path):
    _build_legacy_tree(base, user_id="alice", thread_ids=("t1",))
    _seed_db(base, users={"alice": "ws-alpha"})
    paths = Paths(base)

    migrate(paths, user_workspaces=_load_user_workspaces(paths), fallback_workspace=LEGACY_WORKSPACE_FALLBACK, dry_run=False)

    assert not (base / "users").exists(), "Empty legacy users/ dir should be cleaned up"


def test_no_users_directory_is_noop(base: Path):
    paths = Paths(base)
    report = migrate(paths, user_workspaces={}, fallback_workspace=LEGACY_WORKSPACE_FALLBACK, dry_run=False)
    assert report == []


def test_missing_db_returns_empty_mapping(base: Path):
    paths = Paths(base)
    assert _load_user_workspaces(paths) == {}


def test_db_without_users_table_returns_empty(base: Path):
    db_path = base / "deer-flow.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE other_table (x INTEGER)")
        conn.commit()
    finally:
        conn.close()
    paths = Paths(base)
    assert _load_user_workspaces(paths) == {}
