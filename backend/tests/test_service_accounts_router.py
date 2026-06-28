"""service-accounts router tests (Stage 1 PR4)."""

from __future__ import annotations

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
        await session.commit()
    async with sf() as session:
        session.add(WorkspaceRow(id="w-1", name="WS", slug="ws", owner_id="u-alice"))
        await session.commit()


async def _cleanup():
    from deerflow.persistence.engine import close_engine

    await close_engine()


def _make_app(*, role="owner", user_id="u-alice", workspace_id="w-1"):
    """App that stamps a fixed principal + workspace, then mounts the router.

    A tiny inline middleware substitutes for AuthMiddleware so the test
    controls role/user/workspace directly.
    """
    from fastapi import FastAPI, Request
    from starlette.middleware.base import BaseHTTPMiddleware

    from app.gateway.authz import _ALL_PERMISSIONS, AuthContext
    from app.gateway.routers import service_accounts
    from deerflow.runtime.user_context import reset_current_user, set_current_user
    from deerflow.runtime.workspace_context import reset_current_workspace, set_current_workspace

    class _Stamp(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            user = type("U", (), {"id": user_id, "is_service_account": False})()
            ws = type("W", (), {"id": workspace_id, "role": role})()
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
    return app


async def test_owner_creates_and_lists_sa(tmp_path):
    await _init_db(tmp_path)
    try:
        client = TestClient(_make_app(role="owner"))
        r = client.post("/api/v1/service-accounts", json={"name": "ci-bot"})
        assert r.status_code == 201, r.text
        sa = r.json()
        assert sa["name"] == "ci-bot"
        assert sa["workspace_id"] == "w-1"
        assert sa["created_by"] == "u-alice"

        lst = client.get("/api/v1/service-accounts")
        assert lst.status_code == 200
        assert [s["id"] for s in lst.json()] == [sa["id"]]
    finally:
        await _cleanup()


async def test_member_cannot_create_sa(tmp_path):
    await _init_db(tmp_path)
    try:
        client = TestClient(_make_app(role="member"))
        r = client.post("/api/v1/service-accounts", json={"name": "x"})
        assert r.status_code == 403
    finally:
        await _cleanup()


async def test_patch_status_suspend(tmp_path):
    await _init_db(tmp_path)
    try:
        client = TestClient(_make_app(role="admin"))
        sa = client.post("/api/v1/service-accounts", json={"name": "bot"}).json()
        r = client.patch(f"/api/v1/service-accounts/{sa['id']}", json={"status": "suspended"})
        assert r.status_code == 200
        assert r.json()["status"] == "suspended"
    finally:
        await _cleanup()


async def test_patch_other_workspace_sa_404(tmp_path):
    await _init_db(tmp_path)
    try:
        # SA created in w-1
        owner_client = TestClient(_make_app(role="owner", workspace_id="w-1"))
        sa = owner_client.post("/api/v1/service-accounts", json={"name": "bot"}).json()
        # Caller in a different workspace tries to patch it → 404
        other_client = TestClient(_make_app(role="owner", workspace_id="w-2"))
        r = other_client.patch(f"/api/v1/service-accounts/{sa['id']}", json={"status": "suspended"})
        assert r.status_code == 404
    finally:
        await _cleanup()
