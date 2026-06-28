"""Tests for ApiKeyRepository (Stage 1 PR1).

get_active_by_hash is the auth hot path: must return None for revoked
and expired keys. Expiry is filtered in Python (driver-agnostic) while
revoked_at IS NULL rides the partial index idx_api_keys_active.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from deerflow.auth.tokens import generate_api_key
from deerflow.persistence.api_key import ApiKeyRepository
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
    return ApiKeyRepository(get_session_factory())


async def _cleanup():
    from deerflow.persistence.engine import close_engine

    await close_engine()


async def _seed_sa(repo, *, sa_id="sa-1") -> None:
    async with repo._sf() as session:
        session.add(UserRow(id="u-alice", email="alice@example.com"))
        await session.commit()
    async with repo._sf() as session:
        session.add(WorkspaceRow(id="w-1", name="WS", slug="ws", owner_id="u-alice"))
        await session.commit()
    async with repo._sf() as session:
        session.add(ServiceAccountRow(id=sa_id, workspace_id="w-1", name="bot", role="member", identity_mode="collapsed", status="active", created_by="u-alice"))
        await session.commit()


async def _mint(repo, *, expires_at=None, scopes="threads:read"):
    gen = generate_api_key("live")
    created = await repo.create(service_account_id="sa-1", key_prefix=gen.prefix, key_hash=gen.key_hash, name="k", scopes=scopes, expires_at=expires_at)
    return gen, created


async def test_create_then_get_active_by_hash(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_sa(repo)
        gen, created = await _mint(repo)
        assert created["key_prefix"] == gen.prefix
        assert "key_hash" not in created  # never expose the hash in dicts
        found = await repo.get_active_by_hash(gen.key_hash)
        assert found is not None
        assert found["id"] == created["id"]
        assert found["scopes"] == "threads:read"
    finally:
        await _cleanup()


async def test_get_active_by_hash_miss_returns_none(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_sa(repo)
        assert await repo.get_active_by_hash("deadbeef") is None
    finally:
        await _cleanup()


async def test_revoked_key_not_active(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_sa(repo)
        gen, created = await _mint(repo)
        await repo.revoke(created["id"])
        assert await repo.get_active_by_hash(gen.key_hash) is None
    finally:
        await _cleanup()


async def test_expired_key_not_active(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_sa(repo)
        past = datetime.now(UTC) - timedelta(hours=1)
        gen, _ = await _mint(repo, expires_at=past)
        assert await repo.get_active_by_hash(gen.key_hash) is None
    finally:
        await _cleanup()


async def test_future_expiry_still_active(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_sa(repo)
        future = datetime.now(UTC) + timedelta(hours=1)
        gen, _ = await _mint(repo, expires_at=future)
        assert await repo.get_active_by_hash(gen.key_hash) is not None
    finally:
        await _cleanup()


async def test_touch_last_used_sets_timestamp(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_sa(repo)
        gen, created = await _mint(repo)
        assert created["last_used_at"] is None
        await repo.touch_last_used(created["id"])
        refetched = await repo.get(created["id"])
        assert refetched["last_used_at"] is not None
    finally:
        await _cleanup()


async def test_list_by_service_account(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_sa(repo)
        await _mint(repo)
        await _mint(repo)
        rows = await repo.list_by_service_account("sa-1")
        assert len(rows) == 2
        assert all("key_hash" not in r for r in rows)
    finally:
        await _cleanup()
