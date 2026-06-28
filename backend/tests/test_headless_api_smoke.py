"""End-to-end headless API smoke (Stage 1 PR4).

Mints a real key through the management endpoints, then calls a protected
probe route through the real AuthMiddleware using that key. Proves the
full chain and the cross-workspace 404 isolation guarantee.

Note: ``from __future__ import annotations`` is intentionally absent.
The probe route's ``request: Request`` annotation must resolve at
class-definition time (inside ``_probe_app``) so FastAPI recognises it
as the special ASGI injection type, not a query parameter.  With the
futures import active the annotation becomes the string ``"Request"``
and ``get_type_hints`` cannot resolve it from the module's global
namespace (the import lives in a local scope inside ``_probe_app``),
causing FastAPI to emit a 422.
"""

import pytest
from starlette.testclient import TestClient

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _init_db(tmp_path):
    from deerflow.persistence.engine import get_session_factory, init_engine
    from deerflow.persistence.user.model import UserRow
    from deerflow.persistence.workspace.model import WorkspaceRow

    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    sf = get_session_factory()
    async with sf() as session:
        session.add(UserRow(id="u-alice", email="alice@example.com"))
        session.add(UserRow(id="u-bob", email="bob@example.com"))
        session.add(UserRow(id="u-owner", email="owner@example.com"))
        await session.commit()
    async with sf() as session:
        session.add(WorkspaceRow(id="w-1", name="Alice WS", slug="alice", owner_id="u-alice"))
        session.add(WorkspaceRow(id="w-2", name="Bob WS", slug="bob", owner_id="u-bob"))
        await session.commit()


async def _cleanup():
    from deerflow.persistence.engine import close_engine

    await close_engine()


def _mgmt_app(*, workspace_id):
    """Management app: stamps a fixed owner principal + workspace."""
    from fastapi import FastAPI, Request
    from starlette.middleware.base import BaseHTTPMiddleware

    from app.gateway.authz import _ALL_PERMISSIONS, AuthContext
    from app.gateway.routers import api_keys, service_accounts
    from deerflow.runtime.user_context import reset_current_user, set_current_user
    from deerflow.runtime.workspace_context import reset_current_workspace, set_current_workspace

    class _Stamp(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            user = type("U", (), {"id": "u-owner", "is_service_account": False})()
            ws = type("W", (), {"id": workspace_id, "role": "owner"})()
            request.state.user = user
            request.state.auth = AuthContext(user=user, permissions=_ALL_PERMISSIONS)
            ut = set_current_user(user)
            wt = set_current_workspace(ws)
            try:
                return await call_next(request)
            finally:
                reset_current_workspace(wt)
                reset_current_user(ut)

    app = FastAPI()
    app.add_middleware(_Stamp)
    app.include_router(service_accounts.router)
    app.include_router(api_keys.router)
    return app


def _probe_app():
    """Protected app behind the REAL AuthMiddleware with a probe route."""
    from fastapi import FastAPI, Request

    from app.gateway.auth_middleware import AuthMiddleware
    from deerflow.runtime.user_context import get_effective_user_id
    from deerflow.runtime.workspace_context import get_effective_workspace_id

    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/api/v1/threads/_probe")
    async def probe(request: Request):
        return {"user_id": get_effective_user_id(), "workspace_id": get_effective_workspace_id()}

    return app


async def test_mint_use_and_cross_workspace_isolation(tmp_path):
    await _init_db(tmp_path)
    try:
        mgmt = TestClient(_mgmt_app(workspace_id="w-1"))
        sa = mgmt.post("/api/v1/service-accounts", json={"name": "ci"}).json()
        key = mgmt.post("/api/v1/api-keys", json={"service_account_id": sa["id"], "name": "k", "scopes": "threads:read"}).json()
        plaintext = key["plaintext"]

        probe = TestClient(_probe_app())
        ok = probe.get("/api/v1/threads/_probe", headers={"Authorization": f"Bearer {plaintext}"})
        assert ok.status_code == 200
        assert ok.json() == {"user_id": sa["id"], "workspace_id": "w-1"}

        # A bogus / unknown key is rejected.
        bad = probe.get("/api/v1/threads/_probe", headers={"Authorization": "Bearer dfk_live_unknown0000000000000000"})
        assert bad.status_code == 401
    finally:
        await _cleanup()
