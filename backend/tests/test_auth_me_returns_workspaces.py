"""``GET /auth/me`` returns the user's workspace memberships.

Stage 0 PR4 T4.11. After PR4 the frontend needs to discover which
workspaces the current user belongs to (eventually for a picker UI).
Each membership entry surfaces ``id``, ``name``, ``slug``, ``role``
so the picker can render the list without a second roundtrip.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("AUTH_JWT_SECRET", "test-secret-key-auth-me-workspaces-32+")

from app.gateway.auth.config import AuthConfig, set_auth_config

_TEST_SECRET = "test-secret-key-auth-me-workspaces-32+"


@pytest.fixture(autouse=True)
def _setup_auth(tmp_path):
    from app.gateway import deps
    from app.gateway.routers.auth import _SETUP_STATUS_COOLDOWN
    from deerflow.persistence.engine import close_engine, init_engine

    set_auth_config(AuthConfig(jwt_secret=_TEST_SECRET))
    url = f"sqlite+aiosqlite:///{tmp_path}/auth_me.db"
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
    yield TestClient(create_app())


def test_auth_me_returns_workspaces_list(client):
    """After registration, /auth/me lists the user's single personal workspace."""
    client.post("/api/v1/auth/initialize", json={"email": "admin@example.com", "password": "Str0ng!Pass99"})
    reg = client.post("/api/v1/auth/register", json={"email": "alice@example.com", "password": "Tr0ub4dor3a-strong!"})
    user_id = reg.json()["id"]

    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["id"] == user_id
    assert body["email"] == "alice@example.com"
    assert body["default_workspace_id"], "user should land with a default workspace"

    assert len(body["workspaces"]) == 1, body
    ws = body["workspaces"][0]
    assert ws["id"] == body["default_workspace_id"]
    assert ws["slug"] == "alice"
    assert ws["role"] == "owner"
    assert ws["name"]  # whatever the helper picks — just sanity check it's non-empty


def test_auth_me_workspaces_isolated_per_user(client):
    """Two users see only their own workspaces in /auth/me."""
    client.post("/api/v1/auth/initialize", json={"email": "admin@example.com", "password": "Str0ng!Pass99"})
    client.post("/api/v1/auth/register", json={"email": "alice@example.com", "password": "Tr0ub4dor3a-strong!"})
    a_id = client.get("/api/v1/auth/me").json()["id"]
    a_workspaces = client.get("/api/v1/auth/me").json()["workspaces"]

    client.cookies.clear()
    client.post("/api/v1/auth/register", json={"email": "bob@example.com", "password": "Tr0ub4dor3a-strong!"})
    b_id = client.get("/api/v1/auth/me").json()["id"]
    b_workspaces = client.get("/api/v1/auth/me").json()["workspaces"]

    assert a_id != b_id
    assert {w["id"] for w in a_workspaces}.isdisjoint({w["id"] for w in b_workspaces})
    assert {w["slug"] for w in a_workspaces} == {"alice"}
    assert {w["slug"] for w in b_workspaces} == {"bob"}
