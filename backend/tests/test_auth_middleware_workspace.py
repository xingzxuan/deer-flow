"""AuthMiddleware injects the workspace ContextVar (Stage 0 PR4 T4.7).

After PR4 every authenticated request has a workspace bound on
``deerflow.runtime.workspace_context._current_workspace``. The middleware
populates it from the JWT's ``wid`` / ``role`` claims, mirrors what it
already does for ``user_context``, and tears both down in a single
``try/finally`` so leaks don't cross requests.

Legacy 4-field tokens (no ``wid``) are rejected upstream by
``decode_token`` (T4.6) — those should never reach the workspace
injection branch; this file pins that the 401 they trigger carries
``AuthErrorCode.WORKSPACE_REQUIRED``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import jwt
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.gateway.auth import create_access_token
from app.gateway.auth.config import get_auth_config
from app.gateway.auth.models import User
from app.gateway.auth_middleware import AuthMiddleware
from deerflow.runtime.workspace_context import get_current_workspace


@pytest.fixture(autouse=True)
def _stable_jwt_secret(monkeypatch):
    monkeypatch.setenv("AUTH_JWT_SECRET", "test-secret-key-for-jwt-testing-minimum-32-chars")
    yield


def _make_app() -> FastAPI:
    """App with AuthMiddleware + an inspect route that surfaces the contextvar."""
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/api/v1/auth/setup-status")  # public — never gates on wid
    async def setup_status():
        return {"needs_setup": False}

    @app.get("/api/models")  # protected — exercises wid injection
    async def inspect_workspace():
        ws = get_current_workspace()
        if ws is None:
            return {"workspace": None}
        return {"workspace": {"id": ws.id, "role": ws.role}}

    return app


def _make_user(uid: str) -> User:
    return User(id=uid, email="t@example.com", password_hash="hash", token_version=0)


def _make_legacy_token() -> str:
    """Encode a pre-PR4 JWT (no wid/role) directly."""
    now = datetime.now(UTC)
    payload = {
        "sub": str(uuid4()),
        "exp": now + timedelta(hours=1),
        "iat": now,
        "ver": 0,
    }
    return jwt.encode(payload, get_auth_config().jwt_secret, algorithm="HS256")


def test_new_jwt_injects_workspace_into_contextvar() -> None:
    """Cookie with wid+role → route observes the workspace via the contextvar."""
    uid = str(uuid4())
    token = create_access_token(uid, workspace_id="ws-abc", role="owner")

    with patch("app.gateway.deps.get_local_provider") as fn:
        fn.return_value.get_user = AsyncMock(return_value=_make_user(uid))
        client = TestClient(_make_app())
        res = client.get("/api/models", cookies={"access_token": token})

    assert res.status_code == 200, res.text
    assert res.json() == {"workspace": {"id": "ws-abc", "role": "owner"}}


def test_legacy_jwt_rejected_with_workspace_required() -> None:
    """No-wid tokens get 401 with AuthErrorCode.WORKSPACE_REQUIRED, not generic token_invalid."""
    client = TestClient(_make_app())
    res = client.get("/api/models", cookies={"access_token": _make_legacy_token()})

    assert res.status_code == 401
    assert res.json()["detail"]["code"] == "workspace_required"


def test_public_path_skips_workspace_check() -> None:
    """Public whitelist (e.g. /api/v1/auth/setup-status) does not require wid."""
    client = TestClient(_make_app())
    res = client.get("/api/v1/auth/setup-status")  # no cookie at all
    assert res.status_code == 200


def test_workspace_contextvar_resets_between_requests() -> None:
    """After dispatch returns the contextvar must be clear (no leak across requests).

    Why we test this: if the try/finally is wired only for user_context but
    not workspace_context, two back-to-back requests can see each other's
    workspace under asyncio task switching.
    """
    uid = str(uuid4())
    token = create_access_token(uid, workspace_id="ws-first", role="owner")

    # First request resolves to ws-first
    with patch("app.gateway.deps.get_local_provider") as fn:
        fn.return_value.get_user = AsyncMock(return_value=_make_user(uid))
        client = TestClient(_make_app())
        res1 = client.get("/api/models", cookies={"access_token": token})
    assert res1.json() == {"workspace": {"id": "ws-first", "role": "owner"}}

    # Outside the request scope the contextvar must be empty again.
    assert get_current_workspace() is None

    # Second request with a different workspace must not see ws-first.
    token2 = create_access_token(uid, workspace_id="ws-second", role="owner")
    with patch("app.gateway.deps.get_local_provider") as fn:
        fn.return_value.get_user = AsyncMock(return_value=_make_user(uid))
        client = TestClient(_make_app())
        res2 = client.get("/api/models", cookies={"access_token": token2})
    assert res2.json() == {"workspace": {"id": "ws-second", "role": "owner"}}
