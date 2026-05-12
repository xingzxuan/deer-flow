"""Legacy 4-field JWT compatibility (Stage 0 PR4 T4.6).

Before PR4 every JWT carried only `{sub, exp, iat, ver}`. After PR4 the
server expects `wid` (workspace_id) on every protected request. Old
cookies in the wild must NOT collapse into ``TokenError.MALFORMED`` —
that hides the actual problem (workspace required) and prevents the
frontend from steering the user to ``/select-workspace``.

The contract: ``decode_token`` returns ``TokenError.WORKSPACE_MISSING``
specifically when the JWT signature checks out and the payload is
otherwise well-formed but does NOT carry a ``wid`` claim.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.gateway.auth.config import get_auth_config
from app.gateway.auth.errors import TokenError
from app.gateway.auth.jwt import decode_token


@pytest.fixture(autouse=True)
def _stable_jwt_secret(monkeypatch):
    monkeypatch.setenv("AUTH_JWT_SECRET", "test-secret-key-for-jwt-testing-minimum-32-chars")
    yield


def _make_legacy_token(*, expired: bool = False) -> str:
    """Encode a pre-PR4 JWT directly (bypassing create_access_token)."""
    now = datetime.now(UTC)
    payload = {
        "sub": "u-legacy",
        "exp": now + (timedelta(seconds=-1) if expired else timedelta(hours=1)),
        "iat": now,
        "ver": 0,
    }
    return jwt.encode(payload, get_auth_config().jwt_secret, algorithm="HS256")


def test_decode_legacy_token_returns_workspace_missing_error() -> None:
    """4-field token (no wid) → TokenError.WORKSPACE_MISSING (not MALFORMED)."""
    token = _make_legacy_token()
    result = decode_token(token)
    assert result == TokenError.WORKSPACE_MISSING


def test_decode_legacy_token_with_expired_still_reports_expired() -> None:
    """Expired legacy tokens keep reporting EXPIRED — that signal takes priority.

    Why: an expired token must trigger /auth/refresh logic before we
    decide it also lacks workspace; reporting WORKSPACE_MISSING on an
    expired token would steer the user to /select-workspace instead.
    """
    token = _make_legacy_token(expired=True)
    result = decode_token(token)
    assert result == TokenError.EXPIRED
