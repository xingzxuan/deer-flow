"""Schema tests for ``ExternalUserRow`` (Stage 0 PR8).

PR8 is schema-only — no repository class, no API. Tests exercise raw
ORM behaviour: the composite UNIQUE constraint (service_account_id,
external_id) and CASCADE on service_account delete.

An external user is the end-user identity passed through by a
service_account whose ``identity_mode`` is ``external_passthrough`` or
``both``: every call carries an ``X-External-User-Id`` header which is
upserted into this table for audit / quota attribution.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from deerflow.persistence.external_user import ExternalUserRow
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
                name="passthrough-bot",
                role="member",
                identity_mode="external_passthrough",
                status="active",
                created_by="u-alice",
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()


def _make_external_user(*, eu_id: str, external_id: str) -> ExternalUserRow:
    now = datetime.now(UTC)
    return ExternalUserRow(
        id=eu_id,
        workspace_id="w-1",
        service_account_id="sa-1",
        external_id=external_id,
        display_name=None,
        metadata_json={},
        created_at=now,
        last_seen_at=None,
    )


# ---------------------------------------------------------------------------
# T8.5-1 — UNIQUE (service_account_id, external_id)
# ---------------------------------------------------------------------------


async def test_unique_service_account_id_plus_external_id(tmp_path):
    """The same external_id may be inserted twice only under different SAs."""
    sf = await _setup(tmp_path)
    try:
        await _seed_parents(sf)
        async with sf() as session:
            session.add(_make_external_user(eu_id="eu-1", external_id="client-42"))
            await session.commit()

        with pytest.raises(IntegrityError):
            async with sf() as session:
                session.add(_make_external_user(eu_id="eu-2", external_id="client-42"))
                await session.commit()
    finally:
        await _cleanup()


# ---------------------------------------------------------------------------
# T8.5-2 — CASCADE on service_account delete
# ---------------------------------------------------------------------------


async def test_cascade_on_service_account_delete(tmp_path):
    """Deleting the parent service_account removes all child external_users rows."""
    sf = await _setup(tmp_path)
    try:
        await _seed_parents(sf)
        async with sf() as session:
            session.add(_make_external_user(eu_id="eu-c1", external_id="endpoint-A"))
            session.add(_make_external_user(eu_id="eu-c2", external_id="endpoint-B"))
            await session.commit()

        async with sf() as session:
            await session.execute(delete(ServiceAccountRow).where(ServiceAccountRow.id == "sa-1"))
            await session.commit()

        async with sf() as session:
            row1 = await session.get(ExternalUserRow, "eu-c1")
            row2 = await session.get(ExternalUserRow, "eu-c2")
        assert row1 is None
        assert row2 is None
    finally:
        await _cleanup()
