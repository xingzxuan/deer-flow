"""Tests for Run/Feedback/RunEvent repository workspace_id filtering (PR6 T6.5)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from deerflow.runtime.workspace_context import (
    reset_current_workspace,
    set_current_workspace,
)


async def _init_engine(tmp_path, *, workspaces: tuple[str, ...] = ()):
    from deerflow.persistence.engine import get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    for wid in workspaces:
        await _seed_workspace(wid)
    return get_session_factory()


async def _seed_workspace(wid: str) -> None:
    from deerflow.persistence.engine import get_session_factory
    from deerflow.persistence.workspace.model import WorkspaceRow

    factory = get_session_factory()
    async with factory() as session:
        if await session.get(WorkspaceRow, wid) is not None:
            return
        now = datetime.now(UTC)
        session.add(
            WorkspaceRow(
                id=wid,
                name=f"WS {wid}",
                slug=wid.replace("_", "-")[:32],
                status="active",
                owner_id="test-user-autouse",
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()


async def _cleanup():
    from deerflow.persistence.engine import close_engine

    await close_engine()


def _use_workspace(wid: str):
    return set_current_workspace(SimpleNamespace(id=wid, role="owner"))


class TestRunRepositoryWorkspace:
    @pytest.mark.anyio
    async def test_put_records_workspace_id(self, tmp_path):
        from deerflow.persistence.run import RunRepository

        sf = await _init_engine(tmp_path, workspaces=("ws-alpha",))
        repo = RunRepository(sf)
        token = _use_workspace("ws-alpha")
        try:
            await repo.put("r1", thread_id="t1", user_id="alice")
            record = await repo.get("r1", user_id="alice")
        finally:
            reset_current_workspace(token)
            await _cleanup()
        assert record["workspace_id"] == "ws-alpha"

    @pytest.mark.anyio
    async def test_get_filters_cross_workspace(self, tmp_path):
        from deerflow.persistence.run import RunRepository

        sf = await _init_engine(tmp_path, workspaces=("ws-alpha", "ws-beta"))
        repo = RunRepository(sf)
        token = _use_workspace("ws-alpha")
        try:
            await repo.put("r1", thread_id="t1", user_id="alice")
        finally:
            reset_current_workspace(token)
        token = _use_workspace("ws-beta")
        try:
            assert await repo.get("r1", user_id="alice") is None
        finally:
            reset_current_workspace(token)
            await _cleanup()

    @pytest.mark.anyio
    async def test_list_by_thread_filters_workspace(self, tmp_path):
        from deerflow.persistence.run import RunRepository

        sf = await _init_engine(tmp_path, workspaces=("ws-alpha", "ws-beta"))
        repo = RunRepository(sf)
        token = _use_workspace("ws-alpha")
        try:
            await repo.put("r1", thread_id="t1", user_id="alice")
        finally:
            reset_current_workspace(token)
        token = _use_workspace("ws-beta")
        try:
            await repo.put("r2", thread_id="t1", user_id="alice")
            rows = await repo.list_by_thread("t1", user_id="alice")
        finally:
            reset_current_workspace(token)
            await _cleanup()
        assert [r["run_id"] for r in rows] == ["r2"]


class TestFeedbackRepositoryWorkspace:
    @pytest.mark.anyio
    async def test_create_records_workspace_id(self, tmp_path):
        from deerflow.persistence.feedback.sql import FeedbackRepository

        sf = await _init_engine(tmp_path, workspaces=("ws-alpha",))
        repo = FeedbackRepository(sf)
        token = _use_workspace("ws-alpha")
        try:
            row = await repo.create(run_id="r1", thread_id="t1", rating=1, user_id="alice")
        finally:
            reset_current_workspace(token)
            await _cleanup()
        assert row["workspace_id"] == "ws-alpha"

    @pytest.mark.anyio
    async def test_list_by_thread_filters_workspace(self, tmp_path):
        from deerflow.persistence.feedback.sql import FeedbackRepository

        sf = await _init_engine(tmp_path, workspaces=("ws-alpha", "ws-beta"))
        repo = FeedbackRepository(sf)
        token = _use_workspace("ws-alpha")
        try:
            await repo.create(run_id="r1", thread_id="t1", rating=1, user_id="alice")
        finally:
            reset_current_workspace(token)
        token = _use_workspace("ws-beta")
        try:
            rows = await repo.list_by_thread("t1", user_id="alice")
        finally:
            reset_current_workspace(token)
            await _cleanup()
        assert rows == []


class TestRunEventStoreWorkspace:
    @pytest.mark.anyio
    async def test_put_records_workspace_id(self, tmp_path):
        from deerflow.runtime.events.store.db import DbRunEventStore

        sf = await _init_engine(tmp_path, workspaces=("ws-alpha",))
        store = DbRunEventStore(sf)
        token = _use_workspace("ws-alpha")
        try:
            row = await store.put(thread_id="t1", run_id="r1", event_type="msg", category="message", content="hi")
        finally:
            reset_current_workspace(token)
            await _cleanup()
        assert row["workspace_id"] == "ws-alpha"

    @pytest.mark.anyio
    async def test_list_messages_filters_cross_workspace(self, tmp_path):
        from deerflow.runtime.events.store.db import DbRunEventStore

        sf = await _init_engine(tmp_path, workspaces=("ws-alpha", "ws-beta"))
        store = DbRunEventStore(sf)
        token = _use_workspace("ws-alpha")
        try:
            await store.put(thread_id="t1", run_id="r1", event_type="msg", category="message", content="from-alpha")
        finally:
            reset_current_workspace(token)
        token = _use_workspace("ws-beta")
        try:
            rows = await store.list_messages("t1", user_id="test-user-autouse")
        finally:
            reset_current_workspace(token)
            await _cleanup()
        assert rows == []
