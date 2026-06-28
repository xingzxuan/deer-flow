"""Tests for the API key auth backend (Stage 1 PR2)."""

from __future__ import annotations

import dataclasses

import pytest

from app.gateway.auth.api_key_backend import ServicePrincipal, parse_scopes
from deerflow.auth.tokens import generate_api_key

pytestmark = pytest.mark.anyio


def test_parse_scopes_splits_and_strips():
    assert parse_scopes("threads:read, threads:write") == ["threads:read", "threads:write"]


def test_parse_scopes_empty_string_is_empty_list():
    assert parse_scopes("") == []
    assert parse_scopes("   ") == []


def test_parse_scopes_drops_empty_segments():
    assert parse_scopes("threads:read,,runs:create,") == ["threads:read", "runs:create"]


def test_service_principal_is_service_account_true_by_default():
    p = ServicePrincipal(id="sa-1")
    assert p.id == "sa-1"
    assert p.is_service_account is True


def test_service_principal_is_frozen():
    p = ServicePrincipal(id="sa-1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.id = "other"  # type: ignore[misc]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _setup_backend(tmp_path, *, sa_status="active", ws_status="active", scopes="threads:read", expires_at=None, revoke=False):
    from app.gateway.auth.api_key_backend import APIKeyAuthBackend
    from deerflow.persistence.api_key import ApiKeyRepository
    from deerflow.persistence.engine import get_session_factory, init_engine
    from deerflow.persistence.service_account import ServiceAccountRepository
    from deerflow.persistence.service_account.model import ServiceAccountRow
    from deerflow.persistence.user.model import UserRow
    from deerflow.persistence.workspace import WorkspaceRepository
    from deerflow.persistence.workspace.model import WorkspaceRow

    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    sf = get_session_factory()
    async with sf() as session:
        session.add(UserRow(id="u-alice", email="alice@example.com"))
        await session.commit()
    async with sf() as session:
        session.add(WorkspaceRow(id="w-1", name="WS", slug="ws", owner_id="u-alice", status=ws_status))
        await session.commit()
    async with sf() as session:
        session.add(ServiceAccountRow(id="sa-1", workspace_id="w-1", name="bot", role="member", identity_mode="collapsed", status=sa_status, created_by="u-alice"))
        await session.commit()

    api_key_repo = ApiKeyRepository(sf)
    gen = generate_api_key("live")
    created = await api_key_repo.create(service_account_id="sa-1", key_prefix=gen.prefix, key_hash=gen.key_hash, name="k", scopes=scopes, expires_at=expires_at)
    if revoke:
        await api_key_repo.revoke(created["id"])

    backend = APIKeyAuthBackend(api_key_repo=api_key_repo, service_account_repo=ServiceAccountRepository(sf), workspace_repo=WorkspaceRepository(sf))
    return backend, gen


async def _cleanup():
    from deerflow.persistence.engine import close_engine

    await close_engine()


async def test_authenticate_valid_key(tmp_path):
    backend, gen = await _setup_backend(tmp_path, scopes="threads:read,threads:write")
    try:
        result = await backend.authenticate(gen.plaintext)
        assert result is not None
        assert result.principal.id == "sa-1"
        assert result.principal.is_service_account is True
        assert result.workspace_id == "w-1"
        assert result.role == "member"
        assert result.permissions == ["threads:read", "threads:write"]
    finally:
        await _cleanup()


async def test_authenticate_unknown_token_returns_none(tmp_path):
    backend, _ = await _setup_backend(tmp_path)
    try:
        assert await backend.authenticate("dfk_live_doesnotexist000000000000") is None
    finally:
        await _cleanup()


async def test_authenticate_revoked_key_returns_none(tmp_path):
    backend, gen = await _setup_backend(tmp_path, revoke=True)
    try:
        assert await backend.authenticate(gen.plaintext) is None
    finally:
        await _cleanup()


async def test_authenticate_suspended_sa_returns_none(tmp_path):
    backend, gen = await _setup_backend(tmp_path, sa_status="suspended")
    try:
        assert await backend.authenticate(gen.plaintext) is None
    finally:
        await _cleanup()


async def test_authenticate_suspended_workspace_returns_none(tmp_path):
    backend, gen = await _setup_backend(tmp_path, ws_status="suspended")
    try:
        assert await backend.authenticate(gen.plaintext) is None
    finally:
        await _cleanup()


async def test_authenticate_expired_key_returns_none(tmp_path):
    from datetime import UTC, datetime

    backend, gen = await _setup_backend(tmp_path, expires_at=datetime(2000, 1, 1, tzinfo=UTC))
    try:
        assert await backend.authenticate(gen.plaintext) is None
    finally:
        await _cleanup()
