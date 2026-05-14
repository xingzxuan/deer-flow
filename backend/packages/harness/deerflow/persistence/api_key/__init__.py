"""API key persistence — ORM model only (Stage 0 PR8).

An API key is the credential a service_account uses to call the
headless API. Each key has a public ``key_prefix`` (printed in audit
logs and used for quick lookup) and a ``key_hash`` (sha-256 of the
plaintext token, never reversed). Plaintext tokens are only ever
returned to the caller at create time.

PR8 introduces only the schema + ORM row class. Token generation,
hashing, scope parsing, rate limiting, and the API-key auth
middleware live in Stage 1 alongside the headless API surface.
"""

from __future__ import annotations

from deerflow.persistence.api_key.model import ApiKeyRow

__all__ = ["ApiKeyRow"]
