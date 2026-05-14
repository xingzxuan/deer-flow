"""Schema tests for ``ApiKeyRow`` (Stage 0 PR8).

PR8 is schema-only — no repository class, no API. Tests exercise raw
ORM behaviour: column-level UNIQUE on key_prefix, the dual-dialect
partial index DDL (sqlite_where + postgresql_where), and CASCADE on
service_account delete.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from deerflow.persistence.api_key import ApiKeyRow
from deerflow.persistence.service_account import ServiceAccountRow
from deerflow.persistence.user.model import UserRow
from deerflow.persistence.workspace.model import WorkspaceRow

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _setup(tmp_path):
    from deerflow.persistence.engine import get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    return get_session_factory()


async def _cleanup():
    from deerflow.persistence.engine import close_engine

    await close_engine()


async def _seed_parents(sf) -> None:
    now = datetime.now(UTC)
    async with sf() as session:
        session.add(UserRow(id="u-alice", email="alice@example.com"))
        await session.commit()
    async with sf() as session:
        session.add(WorkspaceRow(id="w-1", name="Alice WS", slug="alice", owner_id="u-alice"))
        await session.commit()
    async with sf() as session:
        session.add(
            ServiceAccountRow(
                id="sa-1",
                workspace_id="w-1",
                name="bot",
                role="member",
                identity_mode="collapsed",
                status="active",
                created_by="u-alice",
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()


def _make_key(*, key_id: str, prefix: str, revoked_at: datetime | None = None) -> ApiKeyRow:
    now = datetime.now(UTC)
    return ApiKeyRow(
        id=key_id,
        service_account_id="sa-1",
        key_prefix=prefix,
        key_hash="0" * 64,
        name="default",
        scopes="",
        rate_limit_rpm=None,
        expires_at=None,
        last_used_at=None,
        revoked_at=revoked_at,
        created_at=now,
    )


# ---------------------------------------------------------------------------
# T8.4-1 — column-level UNIQUE on key_prefix
# ---------------------------------------------------------------------------


async def test_unique_key_prefix_enforced(tmp_path):
    """Two api_key rows sharing the same key_prefix raise IntegrityError."""
    sf = await _setup(tmp_path)
    try:
        await _seed_parents(sf)
        async with sf() as session:
            session.add(_make_key(key_id="ak-1", prefix="dfk_live_abc12345"))
            await session.commit()

        with pytest.raises(IntegrityError):
            async with sf() as session:
                session.add(_make_key(key_id="ak-2", prefix="dfk_live_abc12345"))
                await session.commit()
    finally:
        await _cleanup()


# ---------------------------------------------------------------------------
# T8.4-2 — partial index DDL covers both SQLite and Postgres
# ---------------------------------------------------------------------------


def test_active_index_declares_both_dialect_where_clauses():
    """idx_api_keys_active must compile to a partial index on both drivers.

    The plan locks ``sqlite_where`` *and* ``postgresql_where`` so the same
    Index emits a partial index regardless of backend (Stage 0 dev still
    runs SQLite locally; production is Postgres). Both ``dialect_options``
    entries must be present.
    """
    active_idx = next((idx for idx in ApiKeyRow.__table__.indexes if idx.name == "idx_api_keys_active"), None)
    assert active_idx is not None, "idx_api_keys_active not declared"
    sqlite_where = active_idx.dialect_options.get("sqlite", {}).get("where")
    postgres_where = active_idx.dialect_options.get("postgresql", {}).get("where")
    assert sqlite_where is not None, "sqlite_where missing on idx_api_keys_active"
    assert postgres_where is not None, "postgresql_where missing on idx_api_keys_active"
    assert "revoked_at IS NULL" in str(sqlite_where)
    assert "revoked_at IS NULL" in str(postgres_where)


# ---------------------------------------------------------------------------
# T8.4-3 — CASCADE on service_account delete
# ---------------------------------------------------------------------------


async def test_cascade_on_service_account_delete(tmp_path):
    """Deleting the parent service_account removes all child api_keys."""
    sf = await _setup(tmp_path)
    try:
        await _seed_parents(sf)
        async with sf() as session:
            session.add(_make_key(key_id="ak-cascade-1", prefix="dfk_live_cascade1"))
            session.add(_make_key(key_id="ak-cascade-2", prefix="dfk_live_cascade2"))
            await session.commit()

        async with sf() as session:
            await session.execute(delete(ServiceAccountRow).where(ServiceAccountRow.id == "sa-1"))
            await session.commit()

        async with sf() as session:
            row1 = await session.get(ApiKeyRow, "ak-cascade-1")
            row2 = await session.get(ApiKeyRow, "ak-cascade-2")
        assert row1 is None
        assert row2 is None
    finally:
        await _cleanup()
