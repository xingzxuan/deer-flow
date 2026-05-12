"""Registration endpoints (POST /initialize, POST /register) auto-create a workspace.

Stage 0 PR4 T4.8 + T4.9. Every newly registered user must end up with:

- a single ``workspaces`` row (their personal workspace),
- a single ``workspace_memberships`` row with ``role='owner'``,
- ``users.default_workspace_id`` pointing at that workspace,
- a session cookie whose JWT carries the workspace as the ``wid`` claim.

Tests run against a per-test SQLite engine bootstrapped by the
fixture; the registration router is exercised through the real
TestClient so the full handler + DB transaction path is covered.
"""

from __future__ import annotations

import asyncio
import os

import jwt
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("AUTH_JWT_SECRET", "test-secret-key-register-workspace-32+")

from app.gateway.auth.config import AuthConfig, set_auth_config

_TEST_SECRET = "test-secret-key-register-workspace-32+"


@pytest.fixture(autouse=True)
def _setup_auth(tmp_path):
    from app.gateway import deps
    from app.gateway.routers.auth import _SETUP_STATUS_COOLDOWN
    from deerflow.persistence.engine import close_engine, init_engine

    set_auth_config(AuthConfig(jwt_secret=_TEST_SECRET))
    url = f"sqlite+aiosqlite:///{tmp_path}/register_ws.db"
    asyncio.run(init_engine("sqlite", url=url, sqlite_dir=str(tmp_path)))
    deps._cached_local_provider = None
    deps._cached_repo = None
    _SETUP_STATUS_COOLDOWN.clear()
    try:
        yield
    finally:
        deps._cached_local_provider = None
        deps._cached_repo = None
        _SETUP_STATUS_COOLDOWN.clear()
        asyncio.run(close_engine())


@pytest.fixture()
def client(_setup_auth):
    from app.gateway.app import create_app

    set_auth_config(AuthConfig(jwt_secret=_TEST_SECRET))
    app = create_app()
    yield TestClient(app)


def _init_payload(**extra):
    return {"email": "admin@example.com", "password": "Str0ng!Pass99", **extra}


def _register_payload(email: str = "alice@example.com", **extra):
    return {"email": email, "password": "Tr0ub4dor3a-strong!", **extra}


def _decode(token: str) -> dict:
    """Decode a JWT (signature-checked) and return the raw payload."""
    return jwt.decode(token, _TEST_SECRET, algorithms=["HS256"])


async def _read_workspace_state(user_id: str) -> dict:
    """Inspect the per-user workspace state after a registration call.

    Returns a dict with the workspace row, the owner membership row and
    the user's default_workspace_id, so each test can pick what it
    cares about.
    """
    from sqlalchemy import select

    from deerflow.persistence.engine import get_session_factory
    from deerflow.persistence.user.model import UserRow
    from deerflow.persistence.workspace.model import WorkspaceRow
    from deerflow.persistence.workspace_membership.model import WorkspaceMembershipRow

    sf = get_session_factory()
    async with sf() as session:
        user = await session.get(UserRow, user_id)
        memberships = (await session.execute(select(WorkspaceMembershipRow).where(WorkspaceMembershipRow.user_id == user_id))).scalars().all()
        workspaces = []
        if memberships:
            workspaces = (await session.execute(select(WorkspaceRow).where(WorkspaceRow.id.in_([m.workspace_id for m in memberships])))).scalars().all()
        return {
            "user_default_workspace_id": getattr(user, "default_workspace_id", None) if user else None,
            "memberships": [(m.workspace_id, m.user_id, m.role) for m in memberships],
            "workspaces": [(w.id, w.slug, w.owner_id) for w in workspaces],
        }


# ---------- T4.8 — /initialize ---------------------------------------------


def test_initialize_creates_admin_with_default_workspace(client):
    """POST /initialize → admin + workspace + owner membership + wid cookie."""
    resp = client.post("/api/v1/auth/initialize", json=_init_payload())
    assert resp.status_code == 201, resp.text
    user_id = resp.json()["id"]

    state = asyncio.run(_read_workspace_state(user_id))

    assert len(state["workspaces"]) == 1, state
    ws_id, ws_slug, ws_owner = state["workspaces"][0]
    assert ws_owner == user_id
    # base slug "admin" is reserved, walker bumps to first free suffix
    assert ws_slug == "admin-2"

    assert state["memberships"] == [(ws_id, user_id, "owner")]
    assert state["user_default_workspace_id"] == ws_id

    token = resp.cookies["access_token"]
    claims = _decode(token)
    assert claims["wid"] == ws_id
    assert claims["role"] == "owner"


# ---------- T4.9 — /register -----------------------------------------------


def test_register_creates_user_with_default_workspace(client):
    """POST /register → user + workspace + owner membership + wid cookie."""
    # Initialize an admin first so the system is past first-boot.
    client.post("/api/v1/auth/initialize", json=_init_payload())

    resp = client.post("/api/v1/auth/register", json=_register_payload())
    assert resp.status_code == 201, resp.text
    user_id = resp.json()["id"]

    state = asyncio.run(_read_workspace_state(user_id))

    assert len(state["workspaces"]) == 1, state
    ws_id, ws_slug, ws_owner = state["workspaces"][0]
    assert ws_owner == user_id
    assert ws_slug == "alice"

    assert state["memberships"] == [(ws_id, user_id, "owner")]
    assert state["user_default_workspace_id"] == ws_id

    token = resp.cookies["access_token"]
    claims = _decode(token)
    assert claims["wid"] == ws_id
    assert claims["role"] == "owner"


def test_two_registrations_isolate_workspaces_and_avoid_slug_collision(client):
    """Two users with colliding email local-parts → distinct workspaces, slug suffix bump."""
    client.post("/api/v1/auth/initialize", json=_init_payload())

    r1 = client.post("/api/v1/auth/register", json=_register_payload(email="alice@example.com"))
    r2 = client.post("/api/v1/auth/register", json=_register_payload(email="alice@somewhere.else"))
    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text

    s1 = asyncio.run(_read_workspace_state(r1.json()["id"]))
    s2 = asyncio.run(_read_workspace_state(r2.json()["id"]))

    ws1 = s1["workspaces"][0]
    ws2 = s2["workspaces"][0]
    assert ws1[0] != ws2[0], "workspaces must be distinct"
    assert ws1[1] == "alice"
    assert ws2[1] == "alice-2", "slug collision walker should land on -2"
