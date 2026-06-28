"""Service account persistence — ORM model only (Stage 0 PR8).

A service account is a non-human principal that lives inside a workspace
and authenticates via API keys rather than email + password. Each
service account belongs to exactly one workspace and is created by a
human user (``created_by``).

PR8 introduces only the schema + ORM row class. Repository, API-key
authentication middleware, and the ``@require_permission`` scope
upgrade live in Stage 1 alongside the headless API surface.
"""

from __future__ import annotations

from deerflow.persistence.service_account.model import ServiceAccountRow
from deerflow.persistence.service_account.sql import ServiceAccountRepository, ServiceAccountValidationError

__all__ = ["ServiceAccountRepository", "ServiceAccountRow", "ServiceAccountValidationError"]
