"""JWT carries wid + role claims (Stage 0 PR4).

Tokens issued after PR4 must include `wid` (workspace_id) and `role`
(owner/admin/member) so the AuthMiddleware can resolve the active
workspace without a DB hit. Legacy token compatibility lives in
:mod:`test_legacy_token_compat`.
"""

from __future__ import annotations

from uuid import uuid4

import jwt
import pytest

from app.gateway.auth import create_access_token, decode_token
from app.gateway.auth.config import get_auth_config


@pytest.fixture(autouse=True)
def _stable_jwt_secret(monkeypatch):
    """Pin a deterministic JWT secret across tests in this module."""
    monkeypatch.setenv("AUTH_JWT_SECRET", "test-secret-key-for-jwt-testing-minimum-32-chars")
    yield


def test_jwt_includes_wid_and_role() -> None:
    """create_access_token records wid + role in the encoded payload."""
    user_id = str(uuid4())
    workspace_id = str(uuid4())

    token = create_access_token(user_id, workspace_id=workspace_id, role="owner")

    raw = jwt.decode(token, get_auth_config().jwt_secret, algorithms=["HS256"])
    assert raw["sub"] == user_id
    assert raw["wid"] == workspace_id
    assert raw["role"] == "owner"


def test_decode_round_trip_keeps_wid_and_role() -> None:
    """decode_token returns a TokenPayload exposing wid + role attributes."""
    user_id = str(uuid4())
    workspace_id = str(uuid4())

    token = create_access_token(user_id, workspace_id=workspace_id, role="member")
    payload = decode_token(token)

    # decode_token returns TokenError on failure — must be the success branch here.
    assert hasattr(payload, "wid"), f"got {payload!r}"
    assert payload.sub == user_id
    assert payload.wid == workspace_id
    assert payload.role == "member"
