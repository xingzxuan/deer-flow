"""Tests for ExternalUserRepository (Stage 1 PR1).

Repository is built but not yet wired to any auth path. upsert is
idempotent on (service_account_id, external_id) per the table's
UniqueConstraint uq_external_users_sa_external.
"""

from __future__ import annotations

import pytest

from deerflow.persistence.external_user import ExternalUserRepository
from deerflow.persistence.service_account.model import ServiceAccountRow
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
    return ExternalUserRepository(get_session_factory())


async def _cleanup():
    from deerflow.persistence.engine import close_engine

    await close_engine()


async def _seed_sa(repo) -> None:
    async with repo._sf() as session:
        session.add(UserRow(id="u-alice", email="alice@example.com"))
        await session.commit()
    async with repo._sf() as session:
        session.add(WorkspaceRow(id="w-1", name="WS", slug="ws", owner_id="u-alice"))
        await session.commit()
    async with repo._sf() as session:
        session.add(ServiceAccountRow(id="sa-1", workspace_id="w-1", name="bot", role="member", identity_mode="external_passthrough", status="active", created_by="u-alice"))
        await session.commit()


async def test_upsert_inserts_then_updates_same_row(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_sa(repo)
        first = await repo.upsert(workspace_id="w-1", service_account_id="sa-1", external_id="ext-42", display_name="alice")
        assert first["external_id"] == "ext-42"
        assert first["last_seen_at"] is not None
        second = await repo.upsert(workspace_id="w-1", service_account_id="sa-1", external_id="ext-42")
        # same logical row (no duplicate)
        assert second["id"] == first["id"]
        rows = await repo.list_by_workspace("w-1")
        assert len(rows) == 1
    finally:
        await _cleanup()


async def test_get_by_id_hit_and_miss(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_sa(repo)
        created = await repo.upsert(workspace_id="w-1", service_account_id="sa-1", external_id="ext-9")
        fetched = await repo.get(created["id"])
        assert fetched is not None
        assert fetched["id"] == created["id"]
        assert await repo.get("nonexistent") is None
    finally:
        await _cleanup()


async def test_get_by_external_id(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_sa(repo)
        await repo.upsert(workspace_id="w-1", service_account_id="sa-1", external_id="ext-7")
        found = await repo.get_by_external_id(service_account_id="sa-1", external_id="ext-7")
        assert found is not None
        assert found["external_id"] == "ext-7"
        assert await repo.get_by_external_id(service_account_id="sa-1", external_id="nope") is None
    finally:
        await _cleanup()
