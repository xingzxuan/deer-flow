"""Tests for ServiceAccountRepository (Stage 1 PR1).

SQLite ephemeral DB per test, mirroring test_workspace_repo / test_api_key_schema.
"""

from __future__ import annotations

import pytest

from deerflow.persistence.service_account import ServiceAccountRepository
from deerflow.persistence.user.model import UserRow
from deerflow.persistence.workspace.model import WorkspaceRow

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _make_repo(tmp_path):
    from deerflow.persistence.engine import get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    return ServiceAccountRepository(get_session_factory())


async def _cleanup():
    from deerflow.persistence.engine import close_engine

    await close_engine()


async def _seed_parents(repo, *, user_id="u-alice", workspace_id="w-1") -> None:
    async with repo._sf() as session:
        session.add(UserRow(id=user_id, email=f"{user_id}@example.com"))
        await session.commit()
    async with repo._sf() as session:
        session.add(WorkspaceRow(id=workspace_id, name="WS", slug=workspace_id, owner_id=user_id))
        await session.commit()


async def test_create_then_get_roundtrip(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_parents(repo)
        created = await repo.create(workspace_id="w-1", name="ci-bot", created_by="u-alice")
        assert created["workspace_id"] == "w-1"
        assert created["name"] == "ci-bot"
        assert created["role"] == "member"
        assert created["identity_mode"] == "collapsed"
        assert created["status"] == "active"
        assert len(created["id"]) == 36

        fetched = await repo.get(created["id"])
        assert fetched is not None
        assert fetched["id"] == created["id"]
    finally:
        await _cleanup()


async def test_get_returns_none_when_missing(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        assert await repo.get("nope") is None
    finally:
        await _cleanup()


async def test_get_active_excludes_suspended(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_parents(repo)
        sa = await repo.create(workspace_id="w-1", name="bot", created_by="u-alice")
        assert await repo.get_active(sa["id"]) is not None
        await repo.update_status(sa["id"], "suspended")
        assert await repo.get_active(sa["id"]) is None
        # get() still returns the row regardless of status
        assert await repo.get(sa["id"]) is not None
    finally:
        await _cleanup()


async def test_list_by_workspace(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_parents(repo)
        await _seed_parents(repo, user_id="u-bob", workspace_id="w-2")
        await repo.create(workspace_id="w-1", name="a", created_by="u-alice")
        await repo.create(workspace_id="w-1", name="b", created_by="u-alice")
        await repo.create(workspace_id="w-2", name="c", created_by="u-bob")
        rows = await repo.list_by_workspace("w-1")
        assert {r["name"] for r in rows} == {"a", "b"}
    finally:
        await _cleanup()
