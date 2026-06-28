"""API key authentication backend (Stage 1 PR2).

Resolves an ``Authorization: Bearer dfk_...`` token into a
``ServicePrincipal`` + workspace + scopes, so ``AuthMiddleware`` can
stamp the same contextvars a cookie-authenticated human would set
(spec D1: user_id = SA.id).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from deerflow.auth.tokens import hash_api_key, split_prefix

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServicePrincipal:
    """Non-human principal backing an API key. Satisfies the
    ``deerflow.runtime.user_context.CurrentUser`` protocol."""

    id: str
    is_service_account: bool = True


def parse_scopes(scopes: str) -> list[str]:
    """Parse a comma-separated scope string into a permission list.

    ``"threads:read, threads:write"`` -> ``["threads:read", "threads:write"]``.
    Empty / whitespace-only segments are dropped.
    """
    return [s.strip() for s in scopes.split(",") if s.strip()]


@dataclass(frozen=True)
class ApiKeyAuthResult:
    """Everything ``AuthMiddleware`` needs to stamp request state +
    contextvars from a verified API key."""

    principal: ServicePrincipal
    workspace_id: str
    role: str
    permissions: list[str]


class APIKeyAuthBackend:
    def __init__(self, *, api_key_repo, service_account_repo, workspace_repo) -> None:
        self._api_key_repo = api_key_repo
        self._service_account_repo = service_account_repo
        self._workspace_repo = workspace_repo

    async def authenticate(self, token: str) -> ApiKeyAuthResult | None:
        """Resolve a plaintext token to an auth result, or None (→ 401)."""
        # Look up by the indexed public prefix; the repo constant-time
        # verifies the full hash.
        key = await self._api_key_repo.get_active_by_hash(hash_api_key(token), key_prefix=split_prefix(token))
        if key is None:
            return None

        sa = await self._service_account_repo.get_active(key["service_account_id"])
        if sa is None:
            return None

        # SA is not a workspace *member* — bypass the membership filter
        # with the documented user_id=None admin/migration path.
        workspace = await self._workspace_repo.get(sa["workspace_id"], user_id=None)
        if workspace is None or workspace["status"] != "active":
            return None

        # Best-effort: never block the request if the timestamp write fails.
        try:
            await self._api_key_repo.touch_last_used(key["id"])
        except Exception:  # noqa: BLE001 — best-effort, log and continue
            logger.warning("touch_last_used failed for api_key %s", key["id"], exc_info=True)

        return ApiKeyAuthResult(
            principal=ServicePrincipal(id=sa["id"]),
            workspace_id=sa["workspace_id"],
            role=sa["role"],
            permissions=parse_scopes(key["scopes"]),
        )


def build_api_key_backend() -> APIKeyAuthBackend | None:
    """Construct a backend from the global session factory, or None when
    persistence is the in-memory backend (no DB → no API keys)."""
    from deerflow.persistence.api_key import ApiKeyRepository
    from deerflow.persistence.engine import get_session_factory
    from deerflow.persistence.service_account import ServiceAccountRepository
    from deerflow.persistence.workspace import WorkspaceRepository

    sf = get_session_factory()
    if sf is None:
        return None
    return APIKeyAuthBackend(api_key_repo=ApiKeyRepository(sf), service_account_repo=ServiceAccountRepository(sf), workspace_repo=WorkspaceRepository(sf))
