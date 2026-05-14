"""Schema tests for ``ServiceAccountRow`` (Stage 0 PR8).

Pattern mirrors :mod:`test_workspace_repo`: ephemeral SQLite per test via
``tmp_path``, no Postgres required at this layer.

PR8 is schema-only — no repository class, no API. Tests exercise raw
ORM behaviour: insert smoke, CASCADE on workspace delete, and RESTRICT
on the ``created_by`` user FK.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

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


async def _seed_user(sf, user_id: str = "u-alice", email: str = "alice@example.com") -> None:
    async with sf() as session:
        session.add(UserRow(id=user_id, email=email))
        await session.commit()


async def _seed_workspace(sf, workspace_id: str = "w-1", owner_id: str = "u-alice", slug: str = "alice") -> None:
    async with sf() as session:
        session.add(WorkspaceRow(id=workspace_id, name="Alice's WS", slug=slug, owner_id=owner_id))
        await session.commit()


# ---------------------------------------------------------------------------
# T8.1 — insert smoke
# ---------------------------------------------------------------------------


async def test_insert_smoke(tmp_path):
    """A minimal service_account row can be inserted and read back."""
    sf = await _setup(tmp_path)
    try:
        await _seed_user(sf)
        await _seed_workspace(sf)

        now = datetime.now(UTC)
        async with sf() as session:
            session.add(
                ServiceAccountRow(
                    id="sa-1",
                    workspace_id="w-1",
                    name="ci-bot",
                    role="member",
                    identity_mode="collapsed",
                    status="active",
                    created_by="u-alice",
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.commit()

        async with sf() as session:
            row = await session.get(ServiceAccountRow, "sa-1")
        assert row is not None
        assert row.workspace_id == "w-1"
        assert row.name == "ci-bot"
        assert row.role == "member"
        assert row.identity_mode == "collapsed"
        assert row.status == "active"
        assert row.created_by == "u-alice"
    finally:
        await _cleanup()


# ---------------------------------------------------------------------------
# T8.2 — CASCADE on workspace delete
# ---------------------------------------------------------------------------


async def test_cascade_on_workspace_delete(tmp_path):
    """Deleting the parent workspace removes the service_account row (FK CASCADE)."""
    sf = await _setup(tmp_path)
    try:
        await _seed_user(sf)
        await _seed_workspace(sf)
        now = datetime.now(UTC)
        async with sf() as session:
            session.add(
                ServiceAccountRow(
                    id="sa-2",
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

        async with sf() as session:
            await session.execute(delete(WorkspaceRow).where(WorkspaceRow.id == "w-1"))
            await session.commit()

        async with sf() as session:
            row = await session.get(ServiceAccountRow, "sa-2")
        assert row is None
    finally:
        await _cleanup()


# ---------------------------------------------------------------------------
# T8.3 — RESTRICT on created_by user delete
# ---------------------------------------------------------------------------


async def test_restrict_on_created_by_user_delete(tmp_path):
    """Deleting the creator user is blocked while their service_account survives."""
    sf = await _setup(tmp_path)
    try:
        await _seed_user(sf)
        await _seed_workspace(sf)
        now = datetime.now(UTC)
        async with sf() as session:
            session.add(
                ServiceAccountRow(
                    id="sa-3",
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

        with pytest.raises(IntegrityError):
            async with sf() as session:
                await session.execute(delete(UserRow).where(UserRow.id == "u-alice"))
                await session.commit()

        # The service_account is still there after the rollback.
        async with sf() as session:
            row = await session.get(ServiceAccountRow, "sa-3")
        assert row is not None
    finally:
        await _cleanup()
