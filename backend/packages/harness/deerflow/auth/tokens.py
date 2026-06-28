"""API key generation, hashing, and prefix extraction (Stage 1 PR1).

Format is irreversible once business systems integrate (spec D5):
``dfk_live_<24>`` / ``dfk_test_<24>``. The public ``key_prefix`` is the
first 16 chars (``dfk_live_`` + 7 random) and is stored UNIQUE for audit
logging; the DB only ever stores ``sha256(plaintext)`` hex, never the
plaintext.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Literal

_PREFIX_LEN = 16
# token_urlsafe(18) yields ceil(18 * 4 / 3) = 24 url-safe chars.
_RANDOM_BYTES = 18


@dataclass(frozen=True)
class GeneratedKey:
    """A freshly minted key. ``plaintext`` is returned to the caller
    exactly once; only ``prefix`` + ``key_hash`` are persisted."""

    plaintext: str
    prefix: str
    key_hash: str


def hash_api_key(plaintext: str) -> str:
    """Return the sha-256 hex digest of a plaintext token."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def split_prefix(plaintext: str) -> str:
    """Return the public, loggable prefix (first 16 chars) of a token."""
    return plaintext[:_PREFIX_LEN]


def generate_api_key(env: Literal["live", "test"]) -> GeneratedKey:
    """Generate a new API key for the given environment.

    Raises ``ValueError`` for any env other than ``"live"`` / ``"test"``.
    """
    if env not in ("live", "test"):
        raise ValueError(f"env must be 'live' or 'test', got {env!r}")
    random_part = secrets.token_urlsafe(_RANDOM_BYTES)
    plaintext = f"dfk_{env}_{random_part}"
    return GeneratedKey(plaintext=plaintext, prefix=split_prefix(plaintext), key_hash=hash_api_key(plaintext))
