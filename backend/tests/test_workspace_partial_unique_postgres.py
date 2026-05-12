"""Partial unique index `idx_one_owner_per_workspace` works on Postgres.

The same assertion against SQLite is in
:mod:`test_workspace_membership_repo::test_cannot_add_second_owner`.
This file adds the Postgres twin via the @pytest.mark.postgres
testcontainers fixture from PR1.

Why duplicate the test:
  - SQLite and Postgres parse ``WHERE`` clauses differently. We declare
    both ``sqlite_where`` and ``postgresql_where`` on the Index; this
    test pins that Postgres genuinely enforces the partial-unique
    constraint, not just that SQLAlchemy emits the DDL.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from deerflow.persistence.user.model import UserRow
from deerflow.persistence.workspace import WorkspaceRepository
from deerflow.persistence.workspace_membership import WorkspaceMembershipRepository

pytestmark = [pytest.mark.postgres, pytest.mark.anyio]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_partial_unique_on_owner_enforced_on_postgres(postgres_url: str) -> None:
    """Postgres: inserting a 2nd owner must raise IntegrityError, same as SQLite."""
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    await init_engine("postgres", url=postgres_url)
    try:
        sf = get_session_factory()
        ws_repo = WorkspaceRepository(sf)
        m_repo = WorkspaceMembershipRepository(sf)

        # Seed two users
        async with sf() as session:
            session.add(UserRow(id="pg-u1", email="pg1@example.com"))
            session.add(UserRow(id="pg-u2", email="pg2@example.com"))
            await session.commit()

        ws = await ws_repo.create(name="PG W", slug="pg-one-owner", owner_id="pg-u1")
        await m_repo.add(workspace_id=ws["id"], user_id="pg-u1", role="owner")

        with pytest.raises(IntegrityError):
            await m_repo.add(workspace_id=ws["id"], user_id="pg-u2", role="owner")
    finally:
        await close_engine()


async def test_multiple_admins_allowed_on_postgres(postgres_url: str) -> None:
    """Postgres: partial unique on owner must NOT block multiple admins."""
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    await init_engine("postgres", url=postgres_url)
    try:
        sf = get_session_factory()
        ws_repo = WorkspaceRepository(sf)
        m_repo = WorkspaceMembershipRepository(sf)

        async with sf() as session:
            session.add(UserRow(id="pg-a1", email="a1@example.com"))
            session.add(UserRow(id="pg-a2", email="a2@example.com"))
            session.add(UserRow(id="pg-a3", email="a3@example.com"))
            await session.commit()

        ws = await ws_repo.create(name="PG W", slug="pg-multi-admin", owner_id="pg-a1")
        await m_repo.add(workspace_id=ws["id"], user_id="pg-a1", role="owner")
        await m_repo.add(workspace_id=ws["id"], user_id="pg-a2", role="admin")
        await m_repo.add(workspace_id=ws["id"], user_id="pg-a3", role="admin")

        members = await m_repo.list_by_workspace(workspace_id=ws["id"])
        assert {(m["user_id"], m["role"]) for m in members} == {
            ("pg-a1", "owner"),
            ("pg-a2", "admin"),
            ("pg-a3", "admin"),
        }
    finally:
        await close_engine()
