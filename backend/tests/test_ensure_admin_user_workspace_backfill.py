"""Lifespan hook backfills missing workspaces for pre-PR4 admins.

Stage 0 PR4 T4.13. Production scenario: a deployment that pre-dates
PR4 has an admin user whose ``users.default_workspace_id`` is NULL.
After the upgrade, the first time the app boots, the lifespan hook
must create the admin's personal workspace + owner membership so the
admin can immediately log in without hitting the post-PR4 workspace
gate (T4.7).

This test directly invokes ``_ensure_admin_user`` against a fixture
SQLite DB, simulating the upgrade path.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI

from app.gateway.auth.config import AuthConfig, set_auth_config

_TEST_SECRET = "test-secret-key-admin-backfill-32-chars"


@pytest.fixture(autouse=True)
def _setup(tmp_path):
    from app.gateway import deps
    from deerflow.persistence.engine import close_engine, init_engine

    set_auth_config(AuthConfig(jwt_secret=_TEST_SECRET))
    url = f"sqlite+aiosqlite:///{tmp_path}/admin_backfill.db"
    asyncio.run(init_engine("sqlite", url=url, sqlite_dir=str(tmp_path)))
    deps._cached_local_provider = None
    deps._cached_repo = None
    try:
        yield
    finally:
        deps._cached_local_provider = None
        deps._cached_repo = None
        asyncio.run(close_engine())


async def _seed_pre_pr4_admin(email: str = "admin@example.com") -> str:
    """Insert an admin user with default_workspace_id=NULL (pre-PR4 state)."""
    from app.gateway.deps import get_local_provider

    provider = get_local_provider()
    user = await provider.create_user(email=email, password="Str0ng!Pass99", system_role="admin")
    # Belt + suspenders: pretend this user pre-dates PR4 even if the
    # provider added a workspace_id (it does not today, but explicit
    # is better).
    user.default_workspace_id = None
    await provider.update_user(user)
    return str(user.id)


async def _read_workspace_state(user_id: str) -> dict:
    from sqlalchemy import select

    from deerflow.persistence.engine import get_session_factory
    from deerflow.persistence.user.model import UserRow
    from deerflow.persistence.workspace.model import WorkspaceRow
    from deerflow.persistence.workspace_membership.model import WorkspaceMembershipRow

    sf = get_session_factory()
    async with sf() as session:
        user = await session.get(UserRow, user_id)
        memberships = (await session.execute(select(WorkspaceMembershipRow).where(WorkspaceMembershipRow.user_id == user_id))).scalars().all()
        workspaces = []
        if memberships:
            workspaces = (await session.execute(select(WorkspaceRow).where(WorkspaceRow.id.in_([m.workspace_id for m in memberships])))).scalars().all()
        return {
            "default_workspace_id": user.default_workspace_id if user else None,
            "memberships": [(m.workspace_id, m.role) for m in memberships],
            "workspaces": [(w.id, w.slug) for w in workspaces],
        }


def test_ensure_admin_user_creates_missing_workspace():
    """Pre-PR4 admin without default_workspace_id → lifespan backfills it."""
    from app.gateway.app import _ensure_admin_user

    admin_id = asyncio.run(_seed_pre_pr4_admin())
    before = asyncio.run(_read_workspace_state(admin_id))
    assert before["default_workspace_id"] is None
    assert before["workspaces"] == []

    asyncio.run(_ensure_admin_user(FastAPI()))

    after = asyncio.run(_read_workspace_state(admin_id))
    assert after["default_workspace_id"] is not None, "lifespan should set default_workspace_id"
    assert len(after["workspaces"]) == 1
    ws_id, _slug = after["workspaces"][0]
    assert after["memberships"] == [(ws_id, "owner")]


def test_ensure_admin_user_is_idempotent():
    """Running the lifespan hook twice does not create duplicate workspaces."""
    from app.gateway.app import _ensure_admin_user

    admin_id = asyncio.run(_seed_pre_pr4_admin())
    asyncio.run(_ensure_admin_user(FastAPI()))
    state_after_first = asyncio.run(_read_workspace_state(admin_id))

    asyncio.run(_ensure_admin_user(FastAPI()))
    state_after_second = asyncio.run(_read_workspace_state(admin_id))

    assert state_after_first == state_after_second, "second run must be a no-op"
    assert len(state_after_second["workspaces"]) == 1


def test_ensure_admin_user_skips_when_admin_already_has_workspace():
    """An admin with a workspace already set should not get a second one."""
    from app.gateway.app import _ensure_admin_user
    from app.gateway.deps import get_local_provider

    admin_id = asyncio.run(_seed_pre_pr4_admin())

    async def _set_default(workspace_id: str):
        provider = get_local_provider()
        user = await provider.get_user(admin_id)
        user.default_workspace_id = workspace_id
        await provider.update_user(user)

    # Run the lifespan hook once to seed a workspace, then re-run.
    asyncio.run(_ensure_admin_user(FastAPI()))
    state_seeded = asyncio.run(_read_workspace_state(admin_id))
    assert len(state_seeded["workspaces"]) == 1
    seeded_ws_id = state_seeded["workspaces"][0][0]

    asyncio.run(_set_default(seeded_ws_id))  # ensure default is still pointing at it
    asyncio.run(_ensure_admin_user(FastAPI()))

    final = asyncio.run(_read_workspace_state(admin_id))
    assert final["default_workspace_id"] == seeded_ws_id
    assert [w[0] for w in final["workspaces"]] == [seeded_ws_id]
