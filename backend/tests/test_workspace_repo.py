"""Tests for WorkspaceRepository (Stage 0 PR3).

Pattern mirrors :mod:`test_feedback`: SQLite ephemeral DB per test via
tmp_path, no real Postgres needed at the unit-test layer. Partial-unique
double-driver validation lives in :mod:`test_workspace_partial_unique`
(T3.8, runs against both backends).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from deerflow.persistence.user.model import UserRow
from deerflow.persistence.workspace import WorkspaceRepository, WorkspaceValidationError


async def _make_repo(tmp_path):
    from deerflow.persistence.engine import get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    return WorkspaceRepository(get_session_factory())


async def _cleanup():
    from deerflow.persistence.engine import close_engine

    await close_engine()


async def _seed_user(repo, user_id: str = "u-alice", email: str = "alice@example.com") -> None:
    """Create a user row so workspace.owner_id FK is satisfied."""
    async with repo._sf() as session:
        session.add(UserRow(id=user_id, email=email))
        await session.commit()


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ---------------------------------------------------------------------------
# create / get_by_slug round-trip
# ---------------------------------------------------------------------------


async def test_create_then_lookup_by_slug(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_user(repo)
        created = await repo.create(name="Alice's Workspace", slug="alice", owner_id="u-alice")
        assert created["slug"] == "alice"
        assert created["status"] == "active"
        assert created["owner_id"] == "u-alice"
        assert len(created["id"]) == 36  # UUID v4

        fetched = await repo.get_by_slug("alice")
        assert fetched is not None
        assert fetched["id"] == created["id"]
    finally:
        await _cleanup()


async def test_get_by_slug_returns_none_when_missing(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        assert await repo.get_by_slug("nonexistent") is None
    finally:
        await _cleanup()


# ---------------------------------------------------------------------------
# slug uniqueness + format + blacklist
# ---------------------------------------------------------------------------


async def test_create_rejects_duplicate_slug(tmp_path):
    """Two workspaces with the same slug — second raises IntegrityError."""
    repo = await _make_repo(tmp_path)
    try:
        await _seed_user(repo)
        await repo.create(name="A", slug="dup", owner_id="u-alice")
        with pytest.raises(IntegrityError):
            await repo.create(name="B", slug="dup", owner_id="u-alice")
    finally:
        await _cleanup()


@pytest.mark.parametrize(
    "bad_slug",
    [
        "ab",  # too short
        "x" * 33,  # too long
        "UPPER",  # uppercase
        "has space",  # space
        "-start-with-dash",  # bad start
        "end-with-dash-",  # bad end
        "double--dash",  # consecutive dashes
        "underscore_not_ok",  # underscore
    ],
)
async def test_create_rejects_invalid_slug_pattern(tmp_path, bad_slug):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_user(repo)
        with pytest.raises(WorkspaceValidationError, match="(pattern|length)"):
            await repo.create(name="x", slug=bad_slug, owner_id="u-alice")
    finally:
        await _cleanup()


@pytest.mark.parametrize("reserved", ["admin", "api", "auth", "settings", "billing", "select-workspace"])
async def test_create_rejects_reserved_slug(tmp_path, reserved):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_user(repo)
        with pytest.raises(WorkspaceValidationError, match="reserved"):
            await repo.create(name="x", slug=reserved, owner_id="u-alice")
    finally:
        await _cleanup()


# ---------------------------------------------------------------------------
# status state machine
# ---------------------------------------------------------------------------


async def test_status_state_transitions(tmp_path):
    """active → suspended → deleted are all accepted."""
    repo = await _make_repo(tmp_path)
    try:
        await _seed_user(repo)
        ws = await repo.create(name="x", slug="trans", owner_id="u-alice")
        assert ws["status"] == "active"

        await repo.update_status(ws["id"], "suspended")
        async with repo._sf() as session:
            from deerflow.persistence.workspace.model import WorkspaceRow

            row = await session.get(WorkspaceRow, ws["id"])
            assert row.status == "suspended"

        await repo.update_status(ws["id"], "deleted")
        async with repo._sf() as session:
            from deerflow.persistence.workspace.model import WorkspaceRow

            row = await session.get(WorkspaceRow, ws["id"])
            assert row.status == "deleted"
    finally:
        await _cleanup()


async def test_update_status_rejects_unknown_value(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_user(repo)
        ws = await repo.create(name="x", slug="rejstat", owner_id="u-alice")
        with pytest.raises(WorkspaceValidationError, match="allowed set"):
            await repo.update_status(ws["id"], "weird-state")
    finally:
        await _cleanup()


# ---------------------------------------------------------------------------
# CASCADE: workspace.delete() drops dependent memberships
# ---------------------------------------------------------------------------


async def test_delete_cascades_to_memberships(tmp_path):
    """Deleting a workspace removes its membership rows (FK CASCADE)."""
    from sqlalchemy import select

    from deerflow.persistence.workspace_membership.model import WorkspaceMembershipRow

    repo = await _make_repo(tmp_path)
    try:
        await _seed_user(repo)
        ws = await repo.create(name="x", slug="casc", owner_id="u-alice")
        # Insert an owner membership manually (repository pattern is single-
        # responsibility; registration flow will normally insert both rows
        # in one transaction).
        async with repo._sf() as session:
            session.add(WorkspaceMembershipRow(workspace_id=ws["id"], user_id="u-alice", role="owner"))
            await session.commit()

        await repo.delete(ws["id"])

        async with repo._sf() as session:
            remaining = (await session.execute(select(WorkspaceMembershipRow).where(WorkspaceMembershipRow.workspace_id == ws["id"]))).scalars().all()
        assert remaining == [], "memberships should be CASCADE-deleted with workspace"
    finally:
        await _cleanup()


# ---------------------------------------------------------------------------
# membership-aware get + list_by_user
# ---------------------------------------------------------------------------


@pytest.mark.no_auto_user
async def test_get_returns_none_for_non_member(tmp_path):
    """User-A creates a workspace; User-B's `get(wsA)` returns None."""
    from deerflow.persistence.workspace_membership.model import WorkspaceMembershipRow
    from deerflow.runtime.user_context import reset_current_user, set_current_user

    repo = await _make_repo(tmp_path)
    try:
        # Seed two users
        async with repo._sf() as session:
            session.add(UserRow(id="u-A", email="a@example.com"))
            session.add(UserRow(id="u-B", email="b@example.com"))
            await session.commit()

        # User A creates workspace + becomes owner
        ws = await repo.create(name="A's WS", slug="a-ws", owner_id="u-A")
        async with repo._sf() as session:
            session.add(WorkspaceMembershipRow(workspace_id=ws["id"], user_id="u-A", role="owner"))
            await session.commit()

        # User B attempts to read it via contextvar
        user_b = SimpleNamespace(id="u-B")
        token = set_current_user(user_b)
        try:
            assert await repo.get(ws["id"]) is None
        finally:
            reset_current_user(token)

        # User A's own get succeeds
        user_a = SimpleNamespace(id="u-A")
        token = set_current_user(user_a)
        try:
            row = await repo.get(ws["id"])
            assert row is not None
            assert row["slug"] == "a-ws"
        finally:
            reset_current_user(token)
    finally:
        await _cleanup()


@pytest.mark.no_auto_user
async def test_list_by_user_excludes_other_workspaces(tmp_path):
    from deerflow.persistence.workspace_membership.model import WorkspaceMembershipRow
    from deerflow.runtime.user_context import reset_current_user, set_current_user

    repo = await _make_repo(tmp_path)
    try:
        async with repo._sf() as session:
            session.add(UserRow(id="u-A", email="a@example.com"))
            session.add(UserRow(id="u-B", email="b@example.com"))
            await session.commit()

        ws_a = await repo.create(name="A", slug="ws-a", owner_id="u-A")
        ws_b = await repo.create(name="B", slug="ws-b", owner_id="u-B")
        async with repo._sf() as session:
            session.add(WorkspaceMembershipRow(workspace_id=ws_a["id"], user_id="u-A", role="owner"))
            session.add(WorkspaceMembershipRow(workspace_id=ws_b["id"], user_id="u-B", role="owner"))
            await session.commit()

        token = set_current_user(SimpleNamespace(id="u-A"))
        try:
            workspaces = await repo.list_by_user()
            assert [w["slug"] for w in workspaces] == ["ws-a"]
        finally:
            reset_current_user(token)
    finally:
        await _cleanup()


async def test_list_by_user_bypass_returns_all(tmp_path):
    """user_id=None opts out of membership filter (migration path)."""
    repo = await _make_repo(tmp_path)
    try:
        await _seed_user(repo, "u-A", "a@example.com")
        await _seed_user(repo, "u-B", "b@example.com")
        await repo.create(name="A", slug="all-a", owner_id="u-A")
        await repo.create(name="B", slug="all-b", owner_id="u-B")

        workspaces = await repo.list_by_user(user_id=None)
        # PR6 conftest auto-seeds an "autouse-test" workspace via the
        # ``Base.metadata.after_create`` hook so business-row FKs resolve.
        # ``user_id=None`` bypasses the membership filter, so it surfaces
        # alongside the two rows the test inserted — that is the intended
        # "no filter" behaviour. Assert the inserted ones are present.
        slugs = sorted(w["slug"] for w in workspaces)
        assert "all-a" in slugs
        assert "all-b" in slugs
    finally:
        await _cleanup()
