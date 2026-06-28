"""User Pydantic models for authentication."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, EmailStr, Field


def _utc_now() -> datetime:
    """Return current UTC time (timezone-aware)."""
    return datetime.now(UTC)


class User(BaseModel):
    """Internal user representation."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4, description="Primary key")
    email: EmailStr = Field(..., description="Unique email address")
    password_hash: str | None = Field(None, description="bcrypt hash, nullable for OAuth users")
    system_role: Literal["admin", "user"] = Field(default="user")
    created_at: datetime = Field(default_factory=_utc_now)

    # OAuth linkage (optional)
    oauth_provider: str | None = Field(None, description="e.g. 'github', 'google'")
    oauth_id: str | None = Field(None, description="User ID from OAuth provider")

    # Auth lifecycle
    needs_setup: bool = Field(default=False, description="True for auto-created admin until setup completes")
    token_version: int = Field(default=0, description="Incremented on password change to invalidate old JWTs")

    # Headless API discriminator (Stage 1 PR2). Always False for human
    # users; ServicePrincipal sets it True. Lets downstream code branch
    # on principal kind without isinstance gymnastics.
    is_service_account: bool = Field(default=False, description="True only for API-key service accounts, never for human users")

    # Workspace linkage (Stage 0 PR4)
    default_workspace_id: str | None = Field(
        default=None,
        description="The workspace the user lands in by default after login. NULL → /select-workspace.",
    )


class UserResponse(BaseModel):
    """Response model for user info endpoint."""

    id: str
    email: str
    system_role: Literal["admin", "user"]
    needs_setup: bool = False


class UserMeWorkspace(BaseModel):
    """One workspace entry in ``GET /auth/me`` (Stage 0 PR4)."""

    id: str
    name: str
    slug: str
    role: str


class UserMeResponse(BaseModel):
    """Response model for ``GET /auth/me`` — extends UserResponse with workspaces."""

    id: str
    email: str
    system_role: Literal["admin", "user"]
    needs_setup: bool = False
    default_workspace_id: str | None = None
    workspaces: list[UserMeWorkspace] = []


class ActiveWorkspace(BaseModel):
    """Lightweight workspace proxy injected into the request-scoped contextvar.

    Implements the structural ``CurrentWorkspace`` protocol expected by
    ``deerflow.runtime.workspace_context``: only ``.id`` (str) and
    ``.role`` (str) are required. We intentionally do *not* embed the
    full ``WorkspaceRow`` here — the middleware needs to set the
    contextvar on every request and an extra DB lookup just to populate
    a name/slug we don't use yet would be wasted work.
    """

    id: str
    role: str
