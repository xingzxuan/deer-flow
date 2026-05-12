"""Tests for WorkspaceMembershipRepository (Stage 0 PR3 T3.7)."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from deerflow.persistence.user.model import UserRow
from deerflow.persistence.workspace import WorkspaceRepository
from deerflow.persistence.workspace_membership import (
    MembershipValidationError,
    WorkspaceMembershipRepository,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _setup(tmp_path):
    """Create both repos against a fresh SQLite DB."""
    from deerflow.persistence.engine import get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    sf = get_session_factory()
    return (
        WorkspaceRepository(sf),
        WorkspaceMembershipRepository(sf),
        sf,
    )


async def _cleanup():
    from deerflow.persistence.engine import close_engine

    await close_engine()


async def _seed_user(sf, user_id: str, email: str) -> None:
    async with sf() as session:
        session.add(UserRow(id=user_id, email=email))
        await session.commit()


# ---------------------------------------------------------------------------
# add / remove smoke
# ---------------------------------------------------------------------------


async def test_add_then_get_role(tmp_path):
    ws_repo, m_repo, sf = await _setup(tmp_path)
    try:
        await _seed_user(sf, "u-1", "u1@example.com")
        ws = await ws_repo.create(name="W1", slug="w-1", owner_id="u-1")

        added = await m_repo.add(workspace_id=ws["id"], user_id="u-1", role="owner")
        assert added["role"] == "owner"

        role = await m_repo.get_role(workspace_id=ws["id"], user_id="u-1")
        assert role == "owner"
    finally:
        await _cleanup()


async def test_remove_returns_true_when_deleted_false_when_missing(tmp_path):
    ws_repo, m_repo, sf = await _setup(tmp_path)
    try:
        await _seed_user(sf, "u-1", "u1@example.com")
        ws = await ws_repo.create(name="W1", slug="rm-w", owner_id="u-1")
        await m_repo.add(workspace_id=ws["id"], user_id="u-1", role="owner")

        assert await m_repo.remove(workspace_id=ws["id"], user_id="u-1") is True
        # second remove of same row -> nothing to delete
        assert await m_repo.remove(workspace_id=ws["id"], user_id="u-1") is False
    finally:
        await _cleanup()


# ---------------------------------------------------------------------------
# partial unique: exactly one owner per workspace
# ---------------------------------------------------------------------------


async def test_cannot_add_second_owner(tmp_path):
    """Inserting a second role='owner' in the same workspace must raise IntegrityError."""
    ws_repo, m_repo, sf = await _setup(tmp_path)
    try:
        await _seed_user(sf, "u-1", "u1@example.com")
        await _seed_user(sf, "u-2", "u2@example.com")
        ws = await ws_repo.create(name="W", slug="one-owner", owner_id="u-1")

        await m_repo.add(workspace_id=ws["id"], user_id="u-1", role="owner")
        with pytest.raises(IntegrityError):
            await m_repo.add(workspace_id=ws["id"], user_id="u-2", role="owner")
    finally:
        await _cleanup()


async def test_admin_and_member_dont_trigger_partial_unique(tmp_path):
    """Multiple admin/member rows in one workspace are fine (Stage 2 forward compat)."""
    ws_repo, m_repo, sf = await _setup(tmp_path)
    try:
        await _seed_user(sf, "u-1", "u1@example.com")
        await _seed_user(sf, "u-2", "u2@example.com")
        await _seed_user(sf, "u-3", "u3@example.com")
        ws = await ws_repo.create(name="W", slug="multi-admin", owner_id="u-1")

        await m_repo.add(workspace_id=ws["id"], user_id="u-1", role="owner")
        await m_repo.add(workspace_id=ws["id"], user_id="u-2", role="admin")
        # Second admin OK
        await m_repo.add(workspace_id=ws["id"], user_id="u-3", role="admin")

        members = await m_repo.list_by_workspace(workspace_id=ws["id"])
        assert len(members) == 3
    finally:
        await _cleanup()


# ---------------------------------------------------------------------------
# CASCADE: deleting a user wipes their memberships
# ---------------------------------------------------------------------------


async def test_cascade_delete_user_removes_memberships(tmp_path):
    """FK ON DELETE CASCADE on user_id."""
    from sqlalchemy import delete

    ws_repo, m_repo, sf = await _setup(tmp_path)
    try:
        await _seed_user(sf, "u-keep", "keep@example.com")
        await _seed_user(sf, "u-purge", "purge@example.com")
        ws = await ws_repo.create(name="W", slug="cascade", owner_id="u-keep")
        await m_repo.add(workspace_id=ws["id"], user_id="u-keep", role="owner")
        await m_repo.add(workspace_id=ws["id"], user_id="u-purge", role="admin")

        async with sf() as session:
            await session.execute(delete(UserRow).where(UserRow.id == "u-purge"))
            await session.commit()

        members = await m_repo.list_by_workspace(workspace_id=ws["id"])
        member_ids = [m["user_id"] for m in members]
        assert "u-purge" not in member_ids
        assert "u-keep" in member_ids
    finally:
        await _cleanup()


# ---------------------------------------------------------------------------
# list_by_user ordering (most recent joined_at first)
# ---------------------------------------------------------------------------


async def test_list_by_user_orders_recent_first(tmp_path):
    import asyncio

    ws_repo, m_repo, sf = await _setup(tmp_path)
    try:
        await _seed_user(sf, "u-1", "u1@example.com")
        await _seed_user(sf, "u-2", "u2@example.com")

        # u-1 owns workspace A
        ws_a = await ws_repo.create(name="A", slug="ord-a", owner_id="u-1")
        await m_repo.add(workspace_id=ws_a["id"], user_id="u-1", role="owner")
        await asyncio.sleep(0.01)  # ensure distinct joined_at

        # u-1 later joins workspace C as a member (u-2 owns it)
        ws_c = await ws_repo.create(name="C", slug="ord-c", owner_id="u-2")
        await m_repo.add(workspace_id=ws_c["id"], user_id="u-1", role="member")

        memberships = await m_repo.list_by_user(user_id="u-1")
        slugs_in_order = [(m["workspace_id"], m["role"]) for m in memberships]
        # 'ws_c member' joined AFTER 'ws_a owner' → ws_c first
        assert slugs_in_order[0] == (ws_c["id"], "member")
        assert slugs_in_order[1] == (ws_a["id"], "owner")
    finally:
        await _cleanup()


# ---------------------------------------------------------------------------
# role validation + change_role
# ---------------------------------------------------------------------------


async def test_add_rejects_unknown_role(tmp_path):
    ws_repo, m_repo, sf = await _setup(tmp_path)
    try:
        await _seed_user(sf, "u-1", "u1@example.com")
        ws = await ws_repo.create(name="W", slug="invrole", owner_id="u-1")
        with pytest.raises(MembershipValidationError, match="allowed set"):
            await m_repo.add(workspace_id=ws["id"], user_id="u-1", role="viewer")
    finally:
        await _cleanup()


async def test_change_role_admin_to_member(tmp_path):
    ws_repo, m_repo, sf = await _setup(tmp_path)
    try:
        await _seed_user(sf, "u-1", "u1@example.com")
        await _seed_user(sf, "u-2", "u2@example.com")
        ws = await ws_repo.create(name="W", slug="chg", owner_id="u-1")
        await m_repo.add(workspace_id=ws["id"], user_id="u-1", role="owner")
        await m_repo.add(workspace_id=ws["id"], user_id="u-2", role="admin")

        ok = await m_repo.change_role(workspace_id=ws["id"], user_id="u-2", new_role="member")
        assert ok is True
        assert await m_repo.get_role(workspace_id=ws["id"], user_id="u-2") == "member"

        # change_role on a non-member returns False
        miss = await m_repo.change_role(workspace_id=ws["id"], user_id="u-nonexistent", new_role="member")
        assert miss is False
    finally:
        await _cleanup()
