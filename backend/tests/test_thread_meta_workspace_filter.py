"""Tests for ThreadMetaRepository workspace_id filtering (PR6 T6.1-T6.4).

The repository's three-state ``workspace_id`` semantics mirror ``user_id``:

- :data:`AUTO` (default): read from workspace contextvar
- Explicit ``str``: use the provided id
- Explicit ``None``: bypass workspace filter (migration / CLI)

Cross-workspace access (a thread in workspace A queried with workspace B)
must return ``None``, never the row. This is the load-bearing isolation
boundary tested here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from deerflow.persistence.thread_meta import ThreadMetaRepository
from deerflow.runtime.workspace_context import (
    reset_current_workspace,
    set_current_workspace,
)


async def _seed_workspace(wid: str, *, owner_id: str = "test-user-autouse") -> None:
    """Insert a workspace row so threads_meta.workspace_id FK resolves."""
    from deerflow.persistence.engine import get_session_factory
    from deerflow.persistence.workspace.model import WorkspaceRow

    factory = get_session_factory()
    async with factory() as session:
        existing = await session.get(WorkspaceRow, wid)
        if existing is not None:
            return
        now = datetime.now(UTC)
        session.add(
            WorkspaceRow(
                id=wid,
                name=f"WS {wid}",
                slug=wid.replace("_", "-")[:32],
                status="active",
                owner_id=owner_id,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()


async def _make_repo(tmp_path, *, workspaces: tuple[str, ...] = ()):
    from deerflow.persistence.engine import get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    for wid in workspaces:
        await _seed_workspace(wid)
    return ThreadMetaRepository(get_session_factory())


async def _cleanup():
    from deerflow.persistence.engine import close_engine

    await close_engine()


def _use_workspace(wid: str, role: str = "owner"):
    """Replace the autouse workspace contextvar inside a single test."""
    return set_current_workspace(SimpleNamespace(id=wid, role=role))


class TestCreateWorkspace:
    @pytest.mark.anyio
    async def test_create_uses_workspace_context(self, tmp_path):
        """AUTO sentinel pulls workspace_id from the contextvar."""
        repo = await _make_repo(tmp_path, workspaces=("ws-alpha",))
        token = _use_workspace("ws-alpha")
        try:
            record = await repo.create("t1")
            assert record["workspace_id"] == "ws-alpha"
        finally:
            reset_current_workspace(token)
            await _cleanup()

    @pytest.mark.anyio
    async def test_create_explicit_workspace_overrides_context(self, tmp_path):
        repo = await _make_repo(tmp_path, workspaces=("ws-alpha", "ws-beta"))
        token = _use_workspace("ws-alpha")
        try:
            record = await repo.create("t1", workspace_id="ws-beta")
            assert record["workspace_id"] == "ws-beta"
        finally:
            reset_current_workspace(token)
            await _cleanup()

    @pytest.mark.anyio
    async def test_create_workspace_none_bypasses(self, tmp_path):
        """Explicit None creates an orphan row (migration / CLI path)."""
        repo = await _make_repo(tmp_path)
        record = await repo.create("t1", workspace_id=None)
        assert record["workspace_id"] is None
        await _cleanup()


class TestGetWorkspace:
    @pytest.mark.anyio
    async def test_get_filters_by_workspace(self, tmp_path):
        """Cross-workspace get returns None even when user_id matches."""
        repo = await _make_repo(tmp_path, workspaces=("ws-alpha", "ws-beta"))
        token = _use_workspace("ws-alpha")
        try:
            await repo.create("t1", user_id="alice")
        finally:
            reset_current_workspace(token)

        token = _use_workspace("ws-beta")
        try:
            assert await repo.get("t1", user_id="alice") is None
        finally:
            reset_current_workspace(token)
            await _cleanup()

    @pytest.mark.anyio
    async def test_get_returns_row_in_same_workspace(self, tmp_path):
        repo = await _make_repo(tmp_path, workspaces=("ws-alpha",))
        token = _use_workspace("ws-alpha")
        try:
            await repo.create("t1", user_id="alice")
            record = await repo.get("t1", user_id="alice")
        finally:
            reset_current_workspace(token)
            await _cleanup()
        assert record is not None
        assert record["thread_id"] == "t1"
        assert record["workspace_id"] == "ws-alpha"

    @pytest.mark.anyio
    async def test_get_workspace_none_bypasses_filter(self, tmp_path):
        """Explicit workspace_id=None lets migration scripts see any row."""
        repo = await _make_repo(tmp_path, workspaces=("ws-alpha",))
        token = _use_workspace("ws-alpha")
        try:
            await repo.create("t1", user_id="alice")
        finally:
            reset_current_workspace(token)

        token = _use_workspace("ws-beta")
        try:
            assert await repo.get("t1", user_id=None, workspace_id=None) is not None
        finally:
            reset_current_workspace(token)
            await _cleanup()


class TestSearchUpdateDeleteWorkspace:
    @pytest.mark.anyio
    async def test_search_only_returns_current_workspace(self, tmp_path):
        repo = await _make_repo(tmp_path, workspaces=("ws-alpha", "ws-beta"))
        token = _use_workspace("ws-alpha")
        try:
            await repo.create("t1", user_id="alice")
        finally:
            reset_current_workspace(token)
        token = _use_workspace("ws-beta")
        try:
            await repo.create("t2", user_id="alice")
            rows = await repo.search(user_id="alice")
        finally:
            reset_current_workspace(token)
            await _cleanup()
        ids = {r["thread_id"] for r in rows}
        assert ids == {"t2"}

    @pytest.mark.anyio
    async def test_update_status_blocked_across_workspace(self, tmp_path):
        repo = await _make_repo(tmp_path, workspaces=("ws-alpha", "ws-beta"))
        token = _use_workspace("ws-alpha")
        try:
            await repo.create("t1", user_id="alice")
        finally:
            reset_current_workspace(token)
        token = _use_workspace("ws-beta")
        try:
            await repo.update_status("t1", "busy", user_id="alice")
        finally:
            reset_current_workspace(token)

        token = _use_workspace("ws-alpha")
        try:
            row = await repo.get("t1", user_id="alice")
        finally:
            reset_current_workspace(token)
            await _cleanup()
        assert row["status"] == "idle"

    @pytest.mark.anyio
    async def test_update_display_name_blocked_across_workspace(self, tmp_path):
        repo = await _make_repo(tmp_path, workspaces=("ws-alpha", "ws-beta"))
        token = _use_workspace("ws-alpha")
        try:
            await repo.create("t1", user_id="alice", display_name="A")
        finally:
            reset_current_workspace(token)
        token = _use_workspace("ws-beta")
        try:
            await repo.update_display_name("t1", "B", user_id="alice")
        finally:
            reset_current_workspace(token)
        token = _use_workspace("ws-alpha")
        try:
            row = await repo.get("t1", user_id="alice")
        finally:
            reset_current_workspace(token)
            await _cleanup()
        assert row["display_name"] == "A"

    @pytest.mark.anyio
    async def test_update_metadata_blocked_across_workspace(self, tmp_path):
        repo = await _make_repo(tmp_path, workspaces=("ws-alpha", "ws-beta"))
        token = _use_workspace("ws-alpha")
        try:
            await repo.create("t1", user_id="alice", metadata={"k": "alpha"})
        finally:
            reset_current_workspace(token)
        token = _use_workspace("ws-beta")
        try:
            await repo.update_metadata("t1", {"k": "beta"}, user_id="alice")
        finally:
            reset_current_workspace(token)
        token = _use_workspace("ws-alpha")
        try:
            row = await repo.get("t1", user_id="alice")
        finally:
            reset_current_workspace(token)
            await _cleanup()
        assert row["metadata"] == {"k": "alpha"}

    @pytest.mark.anyio
    async def test_delete_blocked_across_workspace(self, tmp_path):
        repo = await _make_repo(tmp_path, workspaces=("ws-alpha", "ws-beta"))
        token = _use_workspace("ws-alpha")
        try:
            await repo.create("t1", user_id="alice")
        finally:
            reset_current_workspace(token)
        token = _use_workspace("ws-beta")
        try:
            await repo.delete("t1", user_id="alice")
        finally:
            reset_current_workspace(token)
        token = _use_workspace("ws-alpha")
        try:
            row = await repo.get("t1", user_id="alice")
        finally:
            reset_current_workspace(token)
            await _cleanup()
        assert row is not None and row["thread_id"] == "t1"
