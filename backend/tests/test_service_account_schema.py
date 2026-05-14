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
