"""Tests for ``scripts/backfill_workspace_id.py`` (Stage 0 PR5).

Each step in the three-step backfill is exercised in isolation against
a SQLite-on-disk database. The script wires in ``app.gateway.auth.workspace_slug``,
so we get the same slug semantics that the registration flow uses.

Pattern mirrors :mod:`test_workspace_repo`: ``init_engine`` + per-test
tmp_path, with explicit ``close_engine`` teardown so the singleton
session factory does not leak across tests.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from deerflow.persistence.feedback.model import FeedbackRow
from deerflow.persistence.models.run_event import RunEventRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.persistence.user.model import UserRow
from deerflow.persistence.workspace.model import WorkspaceRow
from deerflow.persistence.workspace_membership.model import WorkspaceMembershipRow
from scripts.backfill_workspace_id import (
    _step1_create_workspaces_for_users,
    _step2_update_table_from_users,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _init_engine(tmp_path):
    from deerflow.persistence.engine import get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    return get_session_factory()


async def _close():
    from deerflow.persistence.engine import close_engine

    await close_engine()


async def _seed_user(sf, *, email: str, default_workspace_id: str | None = None) -> str:
    user_id = str(uuid.uuid4())
    async with sf() as session:
        session.add(UserRow(id=user_id, email=email, default_workspace_id=default_workspace_id))
        await session.commit()
    return user_id


async def _seed_business_row(sf, model, **fields) -> None:
    async with sf() as session:
        session.add(model(**fields))
        await session.commit()


# ---------------------------------------------------------------------------
# Step 1: per-user workspace creation
# ---------------------------------------------------------------------------


async def test_creates_workspace_per_user_without_default(tmp_path):
    """Each user with NULL default_workspace_id gets a workspace + owner membership."""
    sf = await _init_engine(tmp_path)
    try:
        u_alice = await _seed_user(sf, email="alice@example.com")
        u_bob = await _seed_user(sf, email="bob+spam@example.com")

        count = await _step1_create_workspaces_for_users(sf, dry_run=False)
        assert count == 2

        async with sf() as session:
            workspaces = (await session.execute(select(WorkspaceRow))).scalars().all()
            memberships = (await session.execute(select(WorkspaceMembershipRow))).scalars().all()
            users = {u.id: u for u in (await session.execute(select(UserRow))).scalars().all()}

        # 2 workspaces, each with exactly one owner membership matching its user.
        assert len(workspaces) == 2
        assert len(memberships) == 2
        owners_by_ws = {m.workspace_id: m.user_id for m in memberships if m.role == "owner"}
        assert {m.role for m in memberships} == {"owner"}
        for ws in workspaces:
            assert owners_by_ws[ws.id] == ws.owner_id
            assert users[ws.owner_id].default_workspace_id == ws.id

        # Slug semantics: alice@ → "alice", bob+spam@ → "bob-spam".
        slugs = {ws.slug for ws in workspaces}
        assert slugs == {"alice", "bob-spam"}
        _ = u_alice, u_bob  # captured for readability
    finally:
        await _close()


async def test_step1_is_idempotent(tmp_path):
    """Second run is a no-op when every user already has a default_workspace_id."""
    sf = await _init_engine(tmp_path)
    try:
        await _seed_user(sf, email="carol@example.com")
        first = await _step1_create_workspaces_for_users(sf, dry_run=False)
        assert first == 1
        # Re-running picks up the just-populated default_workspace_id, so the
        # candidate set is empty.
        second = await _step1_create_workspaces_for_users(sf, dry_run=False)
        assert second == 0
        async with sf() as session:
            ws_count = len((await session.execute(select(WorkspaceRow))).scalars().all())
            mem_count = len((await session.execute(select(WorkspaceMembershipRow))).scalars().all())
        assert ws_count == 1
        assert mem_count == 1
    finally:
        await _close()


async def test_backfill_updates_4_tables_from_users(tmp_path):
    """Step 2 propagates each user's default_workspace_id into 4 business tables."""
    sf = await _init_engine(tmp_path)
    try:
        user_id = await _seed_user(sf, email="dave@example.com")
        # Pre-seed business rows owned by the user with NULL workspace_id.
        await _seed_business_row(sf, ThreadMetaRow, thread_id="t-1", user_id=user_id)
        await _seed_business_row(sf, RunRow, run_id="r-1", thread_id="t-1", user_id=user_id)
        await _seed_business_row(sf, FeedbackRow, feedback_id="f-1", thread_id="t-1", run_id="r-1", user_id=user_id, rating=1)
        await _seed_business_row(sf, RunEventRow, thread_id="t-1", run_id="r-1", user_id=user_id, event_type="lifecycle_started", category="lifecycle", seq=1)

        # Step 1 first so users.default_workspace_id is populated.
        await _step1_create_workspaces_for_users(sf, dry_run=False)

        async with sf() as session:
            ws_id = (await session.execute(select(WorkspaceRow.id))).scalar_one()

        # Step 2 updates each table.
        for table in ("threads_meta", "runs", "feedback", "run_events"):
            count = await _step2_update_table_from_users(sf, table, dry_run=False)
            assert count == 1, table

        async with sf() as session:
            tm = (await session.execute(select(ThreadMetaRow))).scalar_one()
            run = (await session.execute(select(RunRow))).scalar_one()
            fb = (await session.execute(select(FeedbackRow))).scalar_one()
            ev = (await session.execute(select(RunEventRow))).scalar_one()
        assert tm.workspace_id == ws_id
        assert run.workspace_id == ws_id
        assert fb.workspace_id == ws_id
        assert ev.workspace_id == ws_id

        # Re-running Step 2 is a no-op (filtered by workspace_id IS NULL).
        for table in ("threads_meta", "runs", "feedback", "run_events"):
            assert await _step2_update_table_from_users(sf, table, dry_run=False) == 0
    finally:
        await _close()


async def test_backfill_step2_isolates_per_user(tmp_path):
    """Two users with different default workspaces get their own threads tagged independently."""
    sf = await _init_engine(tmp_path)
    try:
        u_eve = await _seed_user(sf, email="eve@example.com")
        u_frank = await _seed_user(sf, email="frank@example.com")
        await _seed_business_row(sf, ThreadMetaRow, thread_id="t-eve", user_id=u_eve)
        await _seed_business_row(sf, ThreadMetaRow, thread_id="t-frank", user_id=u_frank)

        await _step1_create_workspaces_for_users(sf, dry_run=False)
        await _step2_update_table_from_users(sf, "threads_meta", dry_run=False)

        async with sf() as session:
            rows = {r.thread_id: r.workspace_id for r in (await session.execute(select(ThreadMetaRow))).scalars().all()}
            users = {u.id: u.default_workspace_id for u in (await session.execute(select(UserRow))).scalars().all()}
        assert rows["t-eve"] == users[u_eve]
        assert rows["t-frank"] == users[u_frank]
        assert rows["t-eve"] != rows["t-frank"]
    finally:
        await _close()


async def test_step1_skips_blacklisted_base_slug(tmp_path):
    """A user with email like admin@... gets bumped past the slug blacklist via the walker."""
    sf = await _init_engine(tmp_path)
    try:
        await _seed_user(sf, email="admin@example.com")
        await _step1_create_workspaces_for_users(sf, dry_run=False)
        async with sf() as session:
            ws = (await session.execute(select(WorkspaceRow))).scalar_one()
        # The walker treats "admin" as taken (blacklisted), so it falls
        # through to "admin-2" — the same behaviour the registration flow
        # uses for reserved slugs.
        assert ws.slug == "admin-2"
    finally:
        await _close()
