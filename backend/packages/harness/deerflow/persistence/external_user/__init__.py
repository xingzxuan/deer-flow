"""External user persistence — ORM model only (Stage 0 PR8).

An external user represents the end-user identity that a
service_account passes through on each call (typically via an
``X-External-User-Id`` header). The row is upserted each time a
new ``external_id`` is seen under a given service_account.

PR8 introduces only the schema + ORM row class. The upsert logic,
header parsing, and quota attribution all live in Stage 1.
"""

from __future__ import annotations

from deerflow.persistence.external_user.model import ExternalUserRow
from deerflow.persistence.external_user.sql import ExternalUserRepository

__all__ = ["ExternalUserRepository", "ExternalUserRow"]
