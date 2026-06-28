"""api-keys router tests (Stage 1 PR4).

plaintext is returned exactly once at create time; never on list.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _init_db_with_sa(tmp_path, *, sa_id="sa-1", workspace_id="w-1"):
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
        session.add(WorkspaceRow(id=workspace_id, name="WS", slug=f"ws-{workspace_id}", owner_id="u-alice"))
        await session.commit()
    async with sf() as session:
        session.add(ServiceAccountRow(id=sa_id, workspace_id=workspace_id, name="bot", role="member", identity_mode="collapsed", status="active", created_by="u-alice"))
        await session.commit()


async def _cleanup():
    from deerflow.persistence.engine import close_engine

    await close_engine()


def _make_app(*, role="owner", workspace_id="w-1"):
    from fastapi import FastAPI, Request
    from starlette.middleware.base import BaseHTTPMiddleware

    from app.gateway.authz import _ALL_PERMISSIONS, AuthContext
    from app.gateway.routers import api_keys
    from deerflow.runtime.user_context import reset_current_user, set_current_user
    from deerflow.runtime.workspace_context import reset_current_workspace, set_current_workspace

    class _Stamp(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            user = type("U", (), {"id": "u-alice", "is_service_account": False})()
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
    app.include_router(api_keys.router)
    return app


async def test_create_returns_plaintext_once(tmp_path):
    await _init_db_with_sa(tmp_path)
    try:
        client = TestClient(_make_app())
        r = client.post("/api/v1/api-keys", json={"service_account_id": "sa-1", "name": "ci", "scopes": "threads:read"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["plaintext"].startswith("dfk_live_")
        assert body["key_prefix"] == body["plaintext"][:16]

        lst = client.get("/api/v1/api-keys", params={"service_account_id": "sa-1"})
        assert lst.status_code == 200
        rows = lst.json()
        assert len(rows) == 1
        assert "plaintext" not in rows[0]
        assert "key_hash" not in rows[0]
    finally:
        await _cleanup()


async def test_create_for_other_workspace_sa_404(tmp_path):
    await _init_db_with_sa(tmp_path, sa_id="sa-1", workspace_id="w-1")
    try:
        # Caller is in w-2 but targets sa-1 which lives in w-1 → 404.
        client = TestClient(_make_app(workspace_id="w-2"))
        r = client.post("/api/v1/api-keys", json={"service_account_id": "sa-1", "name": "x", "scopes": ""})
        assert r.status_code == 404
    finally:
        await _cleanup()


async def test_revoke_key(tmp_path):
    await _init_db_with_sa(tmp_path)
    try:
        client = TestClient(_make_app())
        created = client.post("/api/v1/api-keys", json={"service_account_id": "sa-1", "name": "k", "scopes": ""}).json()
        r = client.delete(f"/api/v1/api-keys/{created['id']}")
        assert r.status_code == 204
        rows = client.get("/api/v1/api-keys", params={"service_account_id": "sa-1"}).json()
        assert rows[0]["revoked_at"] is not None
    finally:
        await _cleanup()


async def test_member_cannot_create_key(tmp_path):
    await _init_db_with_sa(tmp_path)
    try:
        client = TestClient(_make_app(role="member"))
        r = client.post("/api/v1/api-keys", json={"service_account_id": "sa-1", "name": "x", "scopes": ""})
        assert r.status_code == 403
    finally:
        await _cleanup()


async def test_revoke_other_workspace_key_404(tmp_path):
    await _init_db_with_sa(tmp_path, sa_id="sa-1", workspace_id="w-1")
    try:
        client_a = TestClient(_make_app(workspace_id="w-1"))
        created = client_a.post("/api/v1/api-keys", json={"service_account_id": "sa-1", "name": "k", "scopes": ""}).json()
        client_b = TestClient(_make_app(workspace_id="w-2"))
        assert client_b.delete(f"/api/v1/api-keys/{created['id']}").status_code == 404
    finally:
        await _cleanup()


async def test_list_other_workspace_sa_404(tmp_path):
    await _init_db_with_sa(tmp_path, sa_id="sa-1", workspace_id="w-1")
    try:
        client_b = TestClient(_make_app(workspace_id="w-2"))
        assert client_b.get("/api/v1/api-keys", params={"service_account_id": "sa-1"}).status_code == 404
    finally:
        await _cleanup()


async def test_create_for_suspended_sa_409(tmp_path):
    await _init_db_with_sa(tmp_path)
    try:
        from deerflow.persistence.engine import get_session_factory
        from deerflow.persistence.service_account import ServiceAccountRepository

        await ServiceAccountRepository(get_session_factory()).update_status("sa-1", "suspended")
        client = TestClient(_make_app())
        r = client.post("/api/v1/api-keys", json={"service_account_id": "sa-1", "name": "x", "scopes": ""})
        assert r.status_code == 409
    finally:
        await _cleanup()


async def test_revoke_404_bodies_are_indistinguishable(tmp_path):
    await _init_db_with_sa(tmp_path, sa_id="sa-1", workspace_id="w-1")
    try:
        client_a = TestClient(_make_app(workspace_id="w-1"))
        created = client_a.post("/api/v1/api-keys", json={"service_account_id": "sa-1", "name": "k", "scopes": ""}).json()
        client_b = TestClient(_make_app(workspace_id="w-2"))
        # cross-workspace existing key, and a non-existent key, must return identical 404 bodies
        cross = client_b.delete(f"/api/v1/api-keys/{created['id']}")
        missing = client_b.delete("/api/v1/api-keys/does-not-exist")
        assert cross.status_code == 404
        assert missing.status_code == 404
        assert cross.json() == missing.json()
    finally:
        await _cleanup()
