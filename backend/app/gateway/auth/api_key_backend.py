"""API key authentication backend (Stage 1 PR2).

Resolves an ``Authorization: Bearer dfk_...`` token into a
``ServicePrincipal`` + workspace + scopes, so ``AuthMiddleware`` can
stamp the same contextvars a cookie-authenticated human would set
(spec D1: user_id = SA.id).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

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
