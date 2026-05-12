"""Workspace slug helpers for the registration / initialize flow.

Stage 0 PR4 T4.10. Two responsibilities:

1. ``auto_slug_from_email(email)`` — pure transform from an email's
   local part to a base slug that matches the schema's
   ``^[a-z0-9](-?[a-z0-9])*$`` pattern.
2. ``next_available_slug(base, exists_check=...)`` — collision walker
   that appends ``-2``, ``-3``, … until ``exists_check`` reports the
   candidate is free. Kept separate from ``auto_slug_from_email`` so
   the pure function can be tested without a database.

Lives in the auth package (not in ``persistence``) because the input
is the user's email — a registration-time concept that doesn't belong
in a generic ``WorkspaceRepository``.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Awaitable, Callable

# Mirror the schema's slug rules from
# ``deerflow.persistence.workspace.sql`` so callers of this module
# never need to import private constants from persistence.
_SLUG_MIN_LEN = 3
_SLUG_MAX_LEN = 32


def auto_slug_from_email(email: str) -> str:
    """Map an email to a deterministic, schema-valid base slug.

    Algorithm (from workspace-schema-design §3.1):

    1. Take the local part (before ``@``).
    2. Replace ``+``, ``_``, ``.`` with ``-`` and lowercase.
    3. Strip everything that isn't ``[a-z0-9-]``.
    4. Collapse repeated ``-``; strip leading/trailing ``-``.
    5. Clamp to 32 chars.
    6. If the result is shorter than the schema minimum (3 chars) or
       empty, fall back to ``user-{token_hex(4)}`` so we always emit
       a valid slug.

    The returned slug is the *base* — callers must run it through
    :func:`next_available_slug` before persisting to handle collisions.
    """
    local = email.split("@", 1)[0]
    local = re.sub(r"[+_.]", "-", local).lower()
    local = re.sub(r"[^a-z0-9-]", "", local)
    local = re.sub(r"-+", "-", local).strip("-")
    slug = local[:_SLUG_MAX_LEN]
    if len(slug) < _SLUG_MIN_LEN:
        return f"user-{secrets.token_hex(4)}"
    return slug


async def next_available_slug(
    base: str,
    *,
    exists_check: Callable[[str], Awaitable[bool]],
) -> str:
    """Return the first of ``base``, ``base-2``, ``base-3``, … that ``exists_check`` reports free.

    Caller-supplied ``exists_check`` is awaited once per candidate so
    we can swap in a repository's ``get_by_slug`` without coupling
    this module to persistence imports.

    When ``base + '-N'`` would exceed the 32-char schema limit, the
    base is truncated before the suffix is appended. The walker never
    returns an over-long slug.
    """
    if not await exists_check(base):
        return base

    n = 2
    while True:
        suffix = f"-{n}"
        max_base_len = _SLUG_MAX_LEN - len(suffix)
        candidate = f"{base[:max_base_len]}{suffix}"
        if not await exists_check(candidate):
            return candidate
        n += 1
