"""JWT token creation and verification."""

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from pydantic import BaseModel

from app.gateway.auth.config import get_auth_config
from app.gateway.auth.errors import TokenError


class TokenPayload(BaseModel):
    """JWT token payload.

    `wid` / `role` were added in Stage 0 PR4 and are optional at the
    model level so legacy 4-field tokens still parse into a value —
    callers (middleware, decode_token) decide what "missing wid" means.
    Post-PR4 production tokens always carry both fields.
    """

    sub: str  # user_id
    wid: str | None = None  # workspace_id (Stage 0 PR4)
    role: str | None = None  # owner / admin / member (Stage 0 PR4)
    exp: datetime
    iat: datetime | None = None
    ver: int = 0  # token_version — must match User.token_version


def create_access_token(
    user_id: str,
    expires_delta: timedelta | None = None,
    token_version: int = 0,
    *,
    workspace_id: str | None = None,
    role: str | None = None,
) -> str:
    """Create a JWT access token.

    Args:
        user_id: The user's UUID as string.
        expires_delta: Optional custom expiry, defaults to 7 days.
        token_version: User's current token_version for invalidation.
        workspace_id: Optional active workspace id; encoded as the ``wid`` claim.
        role: Optional workspace role; encoded as the ``role`` claim.

    Returns:
        Encoded JWT string.
    """
    config = get_auth_config()
    expiry = expires_delta or timedelta(days=config.token_expiry_days)

    now = datetime.now(UTC)
    payload: dict[str, Any] = {"sub": user_id, "exp": now + expiry, "iat": now, "ver": token_version}
    if workspace_id is not None:
        payload["wid"] = workspace_id
    if role is not None:
        payload["role"] = role
    return jwt.encode(payload, config.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> TokenPayload | TokenError:
    """Decode and validate a JWT token.

    Returns:
        TokenPayload if valid, or a specific TokenError variant.
    """
    config = get_auth_config()
    try:
        payload = jwt.decode(token, config.jwt_secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        return TokenError.EXPIRED
    except jwt.InvalidSignatureError:
        return TokenError.INVALID_SIGNATURE
    except jwt.PyJWTError:
        return TokenError.MALFORMED

    # Reject legacy pre-PR4 tokens that lack the wid claim. Reported as
    # WORKSPACE_MISSING (not MALFORMED) so middleware can surface a
    # specific 401 telling the frontend to re-issue via /select-workspace.
    if "wid" not in payload or payload.get("wid") is None:
        return TokenError.WORKSPACE_MISSING

    try:
        return TokenPayload(**payload)
    except Exception:
        return TokenError.MALFORMED
