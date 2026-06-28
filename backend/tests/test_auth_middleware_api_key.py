"""AuthMiddleware bearer-path integration tests (Stage 1 PR2).

Drives the real middleware via a minimal app with a probe route that
echoes the resolved contextvars, proving user_id=SA.id / workspace_id
are stamped identically to a human request.

Note: ``from __future__ import annotations`` is intentionally absent here.
The probe route's ``request: Request`` annotation must resolve at class-definition
time (inside ``_make_app``) so FastAPI recognises it as the special ASGI
injection type, not a query parameter. With the futures import active the
annotation becomes the string ``"Request"`` and ``get_type_hints`` cannot
resolve it from the module's global namespace (the import lives in a local
scope inside ``_make_app``), causing FastAPI to emit a 422.
"""

import pytest
from starlette.testclient import TestClient

from deerflow.auth.tokens import generate_api_key

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _seed_key(tmp_path, *, scopes="threads:read", revoke=False):
    from deerflow.persistence.api_key import ApiKeyRepository
    from deerflow.persistence.engine import get_session_factory, init_engine
    from deerflow.persistence.service_account.model import ServiceAccountRow
    from deerflow.persistence.user.model import UserRow
    from deerflow.persistence.workspace.model import WorkspaceRow

    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    sf = get_session_factory()
    async with sf() as session:
        session.add(UserRow(id="u-alice", email="alice@example.com"))
        await session.commit()
    async with sf() as session:
        session.add(WorkspaceRow(id="w-1", name="WS", slug="ws", owner_id="u-alice"))
        await session.commit()
    async with sf() as session:
        session.add(ServiceAccountRow(id="sa-1", workspace_id="w-1", name="bot", role="member", identity_mode="collapsed", status="active", created_by="u-alice"))
        await session.commit()
    repo = ApiKeyRepository(sf)
    gen = generate_api_key("live")
    created = await repo.create(service_account_id="sa-1", key_prefix=gen.prefix, key_hash=gen.key_hash, name="k", scopes=scopes)
    if revoke:
        await repo.revoke(created["id"])
    return gen


async def _cleanup():
    from deerflow.persistence.engine import close_engine

    await close_engine()


def _make_app():
    from fastapi import FastAPI, Request

    from app.gateway.auth_middleware import AuthMiddleware
    from deerflow.runtime.user_context import get_effective_user_id
    from deerflow.runtime.workspace_context import get_effective_workspace_id

    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/api/v1/threads/_probe")
    async def probe(request: Request):
        return {
            "user_id": get_effective_user_id(),
            "workspace_id": get_effective_workspace_id(),
            "is_sa": getattr(request.state.user, "is_service_account", None),
        }

    return app


async def test_valid_bearer_sets_sa_contextvars(tmp_path):
    gen = await _seed_key(tmp_path)
    try:
        client = TestClient(_make_app())
        r = client.get("/api/v1/threads/_probe", headers={"Authorization": f"Bearer {gen.plaintext}"})
        assert r.status_code == 200
        assert r.json() == {"user_id": "sa-1", "workspace_id": "w-1", "is_sa": True}
    finally:
        await _cleanup()


async def test_invalid_bearer_returns_401(tmp_path):
    await _seed_key(tmp_path)
    try:
        client = TestClient(_make_app())
        r = client.get("/api/v1/threads/_probe", headers={"Authorization": "Bearer dfk_live_bogus00000000000000000"})
        assert r.status_code == 401
    finally:
        await _cleanup()


async def test_revoked_bearer_returns_401(tmp_path):
    gen = await _seed_key(tmp_path, revoke=True)
    try:
        client = TestClient(_make_app())
        r = client.get("/api/v1/threads/_probe", headers={"Authorization": f"Bearer {gen.plaintext}"})
        assert r.status_code == 401
    finally:
        await _cleanup()


async def test_non_dfk_bearer_falls_through_to_cookie_path(tmp_path):
    await _seed_key(tmp_path)
    try:
        client = TestClient(_make_app())
        # A non-dfk bearer is NOT the API-key path; with no cookie the
        # cookie path 401s (NOT_AUTHENTICATED), proving no mis-route.
        r = client.get("/api/v1/threads/_probe", headers={"Authorization": "Bearer some.jwt.token"})
        assert r.status_code == 401
        assert r.json()["detail"]["code"] == "not_authenticated"
    finally:
        await _cleanup()


async def test_bare_prefix_bearer_returns_401(tmp_path):
    await _seed_key(tmp_path)
    try:
        client = TestClient(_make_app())
        r = client.get("/api/v1/threads/_probe", headers={"Authorization": "Bearer dfk_"})
        assert r.status_code == 401
    finally:
        await _cleanup()
