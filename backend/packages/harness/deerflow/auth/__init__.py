"""Auth primitives shared by the headless API (Stage 1).

Lives in the ``deerflow`` (harness) layer because both the persistence
hot path (``ApiKeyRepository.get_active_by_hash``) and the app-layer
mint endpoint need token generation/hashing, and the harness boundary
forbids ``deerflow`` importing ``app``.
"""

from __future__ import annotations

from deerflow.auth.tokens import GeneratedKey, generate_api_key, hash_api_key, split_prefix

__all__ = ["GeneratedKey", "generate_api_key", "hash_api_key", "split_prefix"]
