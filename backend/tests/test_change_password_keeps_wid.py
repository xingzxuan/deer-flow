"""``change_password`` and ``login_local`` re-issue JWTs carrying wid + role.

Stage 0 PR4 T4.12. The contract:

- After ``POST /auth/change-password`` the new session cookie's JWT must
  still encode the user's workspace under ``wid`` (and ``role='owner'``),
  with ``ver`` bumped. Dropping ``wid`` here would lock the user out of
  every protected endpoint immediately after a password change.
- Same for ``POST /auth/login/local`` — it issues a fresh JWT and must
  also encode ``wid``.
"""

from __future__ import annotations

import asyncio
import os

import jwt
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("AUTH_JWT_SECRET", "test-secret-key-change-password-wid-32x")

from app.gateway.auth.config import AuthConfig, set_auth_config

_TEST_SECRET = "test-secret-key-change-password-wid-32x"


@pytest.fixture(autouse=True)
def _setup_auth(tmp_path):
    from app.gateway import deps
    from app.gateway.routers.auth import _SETUP_STATUS_COOLDOWN
    from deerflow.persistence.engine import close_engine, init_engine

    set_auth_config(AuthConfig(jwt_secret=_TEST_SECRET))
    url = f"sqlite+aiosqlite:///{tmp_path}/change_pwd.db"
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


def _decode(token: str) -> dict:
    return jwt.decode(token, _TEST_SECRET, algorithms=["HS256"])


def _bootstrap_user(client) -> tuple[str, dict]:
    """Register a user and return (user_id, initial claims)."""
    client.post("/api/v1/auth/initialize", json={"email": "admin@example.com", "password": "Str0ng!Pass99"})
    resp = client.post("/api/v1/auth/register", json={"email": "alice@example.com", "password": "Tr0ub4dor3a-strong!"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], _decode(resp.cookies["access_token"])


def test_change_password_keeps_wid_and_bumps_ver(client):
    """change_password re-signs the JWT with wid + role; ver moves forward."""
    user_id, initial_claims = _bootstrap_user(client)

    # /register set the csrf cookie; the matching header is required on
    # the change-password POST (Double Submit Cookie pattern).
    csrf = client.cookies.get("csrf_token")
    assert csrf, "register must have set csrf_token cookie"

    resp = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "Tr0ub4dor3a-strong!", "new_password": "Tr0ub4dor3a-strong2!"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200, resp.text

    new_claims = _decode(resp.cookies["access_token"])
    assert new_claims["sub"] == user_id
    assert new_claims["wid"] == initial_claims["wid"], "wid must survive a password change"
    assert new_claims["role"] == "owner"
    assert new_claims["ver"] == initial_claims["ver"] + 1, "token_version must advance"


def test_login_issues_wid_carrying_jwt(client):
    """/auth/login/local issues a JWT that the workspace middleware will accept."""
    user_id, _ = _bootstrap_user(client)

    # Clear the session cookie set by /register so the login response is observed in isolation.
    client.cookies.clear()
    resp = client.post(
        "/api/v1/auth/login/local",
        data={"username": "alice@example.com", "password": "Tr0ub4dor3a-strong!"},
    )
    assert resp.status_code == 200, resp.text

    claims = _decode(resp.cookies["access_token"])
    assert claims["sub"] == user_id
    assert claims.get("wid"), "login must include wid"
    assert claims["role"] == "owner"
