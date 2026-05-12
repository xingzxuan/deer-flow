"""Slug helpers for registration / initialize (Stage 0 PR4 T4.10).

Two surfaces under test:

- ``auto_slug_from_email`` — pure transform; no DB dependency.
- ``next_available_slug`` — async collision walker; we stub the
  ``exists_check`` callable so the test stays a unit test.
"""

from __future__ import annotations

import re

import pytest

from app.gateway.auth.workspace_slug import auto_slug_from_email, next_available_slug

# Mirror of the schema's slug pattern. Keeping it inline keeps this
# test self-contained — if the schema regex changes we want this test
# to refuse to lie about validity.
_SLUG_PATTERN = re.compile(r"^[a-z0-9](-?[a-z0-9])*$")


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("foo@example.com", "foo"),
        ("foo.bar@example.com", "foo-bar"),
        ("foo+spam@example.com", "foo-spam"),
        ("foo_bar@example.com", "foo-bar"),
        ("Foo.Bar@example.com", "foo-bar"),
        ("foo.bar+spam@example.com", "foo-bar-spam"),
        ("aaaaaaaaaabbbbbbbbbbccccccccccddddd@example.com", "aaaaaaaaaabbbbbbbbbbccccccccccdd"),
    ],
)
def test_auto_slug_known_inputs(email: str, expected: str) -> None:
    """Deterministic mapping for the inputs called out in the design doc."""
    assert auto_slug_from_email(email) == expected
    assert _SLUG_PATTERN.fullmatch(auto_slug_from_email(email)), "schema regex must accept the output"


@pytest.mark.parametrize(
    "email",
    [
        "@example.com",  # no local part
        "a@example.com",  # too short
        "ab@example.com",  # still too short
        "...+_+...@example.com",  # only separators
        "---@example.com",  # only hyphens
        "🎉@example.com",  # non-ASCII
    ],
)
def test_auto_slug_falls_back_when_unusable(email: str) -> None:
    """Pathological emails fall back to ``user-{token}`` so the slug is always valid."""
    slug = auto_slug_from_email(email)
    assert slug.startswith("user-"), f"expected fallback, got {slug!r}"
    assert _SLUG_PATTERN.fullmatch(slug)
    assert 3 <= len(slug) <= 32


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_next_available_slug_returns_base_when_free(anyio_backend) -> None:
    """No collision → ``base`` is returned unchanged."""
    seen: set[str] = set()

    async def exists(s: str) -> bool:
        return s in seen

    assert await next_available_slug("foo", exists_check=exists) == "foo"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_next_available_slug_walks_through_collisions(anyio_backend) -> None:
    """``foo``, ``foo-2`` taken → walker lands on ``foo-3``."""
    seen = {"foo", "foo-2"}

    async def exists(s: str) -> bool:
        return s in seen

    assert await next_available_slug("foo", exists_check=exists) == "foo-3"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_next_available_slug_truncates_base_to_fit_suffix(anyio_backend) -> None:
    """A 32-char base + ``-2`` would exceed the limit → base is shortened."""
    base = "a" * 32  # exactly at the limit
    seen = {base}

    async def exists(s: str) -> bool:
        return s in seen

    result = await next_available_slug(base, exists_check=exists)
    assert len(result) <= 32
    assert result.endswith("-2")
    assert _SLUG_PATTERN.fullmatch(result)
