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
    LEGACY_WORKSPACE_ID,
    _ensure_legacy_workspace,
    _step1_create_workspaces_for_users,
    _step2_update_table_from_users,
    _step3_assign_legacy_workspace,
    backfill,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


_BUSINESS_ROWS = (ThreadMetaRow, RunRow, FeedbackRow, RunEventRow)


@pytest.fixture(autouse=True)
def _relax_workspace_id_nullable():
    """Simulate alembic 0002 (pre-backfill) state during these tests.

    PR6 T5.11 flipped ``workspace_id`` to ``nullable=False`` on the four
    business ORM models — production correctness comes from alembic 0003.
    The backfill script's job is precisely to fill the rows that were
    inserted between 0002 (column added, nullable) and 0003 (NOT NULL),
    so tests for it must be able to insert NULL rows. We mutate
    ``column.nullable`` for the four tables before ``create_all`` runs,
    then restore on teardown so other tests see the production shape.
    """
    saved: list[tuple] = []
    for model in _BUSINESS_ROWS:
        col = model.__table__.c.workspace_id
        saved.append((col, col.nullable))
        col.nullable = True
    try:
        yield
    finally:
        for col, original in saved:
            col.nullable = original


async def _init_engine(tmp_path):
    from sqlalchemy import delete

    from deerflow.persistence.engine import get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    sf = get_session_factory()
    # The PR6 conftest seeds an autouse user + workspace so business-row FKs
    # resolve in the wider test suite. Backfill tests model "fresh DB needs
    # backfill" semantics, so wipe those rows here. Order: clear the FK
    # pointer first, then the rows.
    async with sf() as session:
        await session.execute(delete(WorkspaceMembershipRow))
        await session.execute(delete(WorkspaceRow).where(WorkspaceRow.id == "test-workspace-autouse"))
        await session.execute(delete(UserRow).where(UserRow.id == "test-user-autouse"))
        await session.commit()
    return sf


async def _close():
    from deerflow.persistence.engine import close_engine

    await close_engine()


async def _seed_user(sf, *, email: str, default_workspace_id: str | None = None, system_role: str = "user") -> str:
    user_id = str(uuid.uuid4())
    async with sf() as session:
        session.add(UserRow(id=user_id, email=email, default_workspace_id=default_workspace_id, system_role=system_role))
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


# ---------------------------------------------------------------------------
# Step 3: orphan rows -> legacy_workspace
# ---------------------------------------------------------------------------


async def test_backfill_orphan_rows_go_to_legacy_workspace(tmp_path):
    """Rows with user_id=NULL get assigned the legacy_workspace UUID after Step 3."""
    sf = await _init_engine(tmp_path)
    try:
        # Seed a platform admin so the legacy workspace has an owner.
        await _seed_user(sf, email="admin@example.com", system_role="admin")
        # Orphan business rows (user_id=NULL): legacy data from before auth.
        await _seed_business_row(sf, ThreadMetaRow, thread_id="t-orphan", user_id=None)
        await _seed_business_row(sf, RunRow, run_id="r-orphan", thread_id="t-orphan", user_id=None)
        await _seed_business_row(sf, FeedbackRow, feedback_id="f-orphan", thread_id="t-orphan", run_id="r-orphan", user_id=None, rating=1)
        await _seed_business_row(sf, RunEventRow, thread_id="t-orphan", run_id="r-orphan", user_id=None, event_type="legacy", category="lifecycle", seq=1)

        # Ensure the anchor + reassign per table.
        created = await _ensure_legacy_workspace(sf, dry_run=False)
        assert created is True
        for table in ("threads_meta", "runs", "feedback", "run_events"):
            count = await _step3_assign_legacy_workspace(sf, table, dry_run=False)
            assert count == 1, table

        # Re-running the anchor helper is a no-op.
        assert await _ensure_legacy_workspace(sf, dry_run=False) is False

        async with sf() as session:
            tm = (await session.execute(select(ThreadMetaRow))).scalar_one()
            run = (await session.execute(select(RunRow))).scalar_one()
            fb = (await session.execute(select(FeedbackRow))).scalar_one()
            ev = (await session.execute(select(RunEventRow))).scalar_one()
            legacy = (await session.execute(select(WorkspaceRow).where(WorkspaceRow.id == LEGACY_WORKSPACE_ID))).scalar_one()
            legacy_mem = (await session.execute(select(WorkspaceMembershipRow).where(WorkspaceMembershipRow.workspace_id == LEGACY_WORKSPACE_ID))).scalar_one()

        assert tm.workspace_id == LEGACY_WORKSPACE_ID
        assert run.workspace_id == LEGACY_WORKSPACE_ID
        assert fb.workspace_id == LEGACY_WORKSPACE_ID
        assert ev.workspace_id == LEGACY_WORKSPACE_ID
        assert legacy.slug == "legacy"
        assert legacy_mem.role == "owner"
    finally:
        await _close()


async def test_ensure_legacy_workspace_refuses_when_no_users(tmp_path):
    """ensure_legacy_workspace raises a clear error if the DB has no users."""
    sf = await _init_engine(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="no users exist"):
            await _ensure_legacy_workspace(sf, dry_run=False)
    finally:
        await _close()


async def test_backfill_dry_run_does_not_write(tmp_path):
    """``backfill(..., dry_run=True)`` reports counts but writes nothing."""
    sf = await _init_engine(tmp_path)
    try:
        await _seed_user(sf, email="admin@example.com", system_role="admin")
        user_id = await _seed_user(sf, email="helen@example.com")
        await _seed_business_row(sf, ThreadMetaRow, thread_id="t-owned", user_id=user_id)
        await _seed_business_row(sf, ThreadMetaRow, thread_id="t-orphan", user_id=None)
        await _seed_business_row(sf, RunRow, run_id="r-owned", thread_id="t-owned", user_id=user_id)

        # Snapshot row counts BEFORE the dry run so we can confirm
        # nothing changed AFTER.
        async with sf() as session:
            ws_before = len((await session.execute(select(WorkspaceRow))).scalars().all())
            mem_before = len((await session.execute(select(WorkspaceMembershipRow))).scalars().all())
            users_with_default_before = len((await session.execute(select(UserRow).where(UserRow.default_workspace_id.is_not(None)))).scalars().all())

        report = await backfill(sf, dry_run=True)
        assert report["dry_run"] is True
        # Step 1 reports 2 candidates (admin + helen, both without default).
        assert report["users_workspaces_created"] == 2
        # Step 2 reports 0 because Step 1 didn't actually populate
        # users.default_workspace_id under dry_run — the JOIN comes up empty.
        assert report["threads_meta_from_users"] == 0
        assert report["runs_from_users"] == 0
        # Step 3 reports the 3 NULL business rows (t-owned, t-orphan, r-owned).
        assert report["legacy_workspace_created"] is True
        assert report["threads_meta_legacy"] == 2
        assert report["runs_legacy"] == 1

        # State did not change.
        async with sf() as session:
            ws_after = len((await session.execute(select(WorkspaceRow))).scalars().all())
            mem_after = len((await session.execute(select(WorkspaceMembershipRow))).scalars().all())
            users_with_default_after = len((await session.execute(select(UserRow).where(UserRow.default_workspace_id.is_not(None)))).scalars().all())
            rows = (await session.execute(select(ThreadMetaRow.workspace_id))).scalars().all()
        assert ws_after == ws_before
        assert mem_after == mem_before
        assert users_with_default_after == users_with_default_before
        assert all(w is None for w in rows)
    finally:
        await _close()


async def test_full_backfill_orchestrator(tmp_path):
    """End-to-end: backfill() runs all three steps and reports per-step counts."""
    sf = await _init_engine(tmp_path)
    try:
        await _seed_user(sf, email="admin@example.com", system_role="admin")
        user_id = await _seed_user(sf, email="gina@example.com")
        await _seed_business_row(sf, ThreadMetaRow, thread_id="t-owned", user_id=user_id)
        await _seed_business_row(sf, ThreadMetaRow, thread_id="t-orphan", user_id=None)

        report = await backfill(sf, dry_run=False)
        assert report["dry_run"] is False
        # Two users were missing a default workspace (admin too — we
        # didn't pre-populate admin's default_workspace_id).
        assert report["users_workspaces_created"] == 2
        assert report["threads_meta_from_users"] == 1
        assert report["legacy_workspace_created"] is True
        assert report["threads_meta_legacy"] == 1

        async with sf() as session:
            rows = {r.thread_id: r.workspace_id for r in (await session.execute(select(ThreadMetaRow))).scalars().all()}
        assert rows["t-orphan"] == LEGACY_WORKSPACE_ID
        assert rows["t-owned"] != LEGACY_WORKSPACE_ID
        assert rows["t-owned"] is not None
    finally:
        await _close()
