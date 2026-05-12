"""SQLAlchemy-backed workspace membership repository.

Manages the ``workspace_memberships`` join table. Stage 0 only writes
``role='owner'`` (single-user workspaces); the repository accepts the
full Stage 2 RBAC role enum so the schema is forward-compatible.

Owner transfer is intentionally NOT modelled here as a single method —
it requires a two-row transactional swap with careful retry semantics,
and belongs in the auth router (PR4+) where it can be wrapped in a
permission check.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.workspace_membership.model import WorkspaceMembershipRow

# 允许的 role 字面值。Stage 0 实际仅写 owner；admin/member 留给 Stage 2 RBAC。
_VALID_ROLES = frozenset({"owner", "admin", "member"})


class MembershipValidationError(ValueError):
    """Raised when role is not in the allowed enum."""


def _validate_role(role: str) -> None:
    if role not in _VALID_ROLES:
        raise MembershipValidationError(f"role {role!r} is not in allowed set {_VALID_ROLES!r}")


class WorkspaceMembershipRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    @staticmethod
    def _row_to_dict(row: WorkspaceMembershipRow) -> dict[str, Any]:
        return {
            "workspace_id": row.workspace_id,
            "user_id": row.user_id,
            "role": row.role,
            "invited_by": row.invited_by,
            "joined_at": row.joined_at.isoformat() if row.joined_at else None,
        }

    async def add(
        self,
        *,
        workspace_id: str,
        user_id: str,
        role: str,
        invited_by: str | None = None,
    ) -> dict[str, Any]:
        """Insert a new membership row.

        Raises:
          - :class:`MembershipValidationError` for unknown role values
          - :class:`sqlalchemy.exc.IntegrityError` for:
            - duplicate (workspace_id, user_id) — composite PK collision
            - second ``owner`` in the same workspace — partial unique index
            - invalid workspace_id / user_id / invited_by FK
        """
        _validate_role(role)
        row = WorkspaceMembershipRow(
            workspace_id=workspace_id,
            user_id=user_id,
            role=role,
            invited_by=invited_by,
            joined_at=datetime.now(UTC),
        )
        async with self._sf() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return self._row_to_dict(row)

    async def remove(self, *, workspace_id: str, user_id: str) -> bool:
        """Delete a membership row; returns True if a row was deleted.

        Owner removal is allowed at the repository layer — auth router
        (PR4) layers the "cannot remove last owner" rule on top.
        """
        async with self._sf() as session:
            result = await session.execute(
                delete(WorkspaceMembershipRow).where(
                    WorkspaceMembershipRow.workspace_id == workspace_id,
                    WorkspaceMembershipRow.user_id == user_id,
                )
            )
            await session.commit()
            return (result.rowcount or 0) > 0

    async def list_by_user(self, *, user_id: str) -> list[dict[str, Any]]:
        """Return all memberships for ``user_id``, ordered by joined_at desc.

        No contextvar resolution here — caller is responsible for passing
        the correct user_id. Used by ``/auth/me`` to list workspaces a
        user belongs to.
        """
        async with self._sf() as session:
            result = await session.execute(select(WorkspaceMembershipRow).where(WorkspaceMembershipRow.user_id == user_id).order_by(WorkspaceMembershipRow.joined_at.desc()))
            return [self._row_to_dict(r) for r in result.scalars()]

    async def list_by_workspace(self, *, workspace_id: str) -> list[dict[str, Any]]:
        """Return all members of a workspace, ordered by joined_at asc."""
        async with self._sf() as session:
            result = await session.execute(select(WorkspaceMembershipRow).where(WorkspaceMembershipRow.workspace_id == workspace_id).order_by(WorkspaceMembershipRow.joined_at.asc()))
            return [self._row_to_dict(r) for r in result.scalars()]

    async def get_role(self, *, workspace_id: str, user_id: str) -> str | None:
        """Return the role string, or None if user is not a member."""
        async with self._sf() as session:
            result = await session.execute(
                select(WorkspaceMembershipRow.role).where(
                    WorkspaceMembershipRow.workspace_id == workspace_id,
                    WorkspaceMembershipRow.user_id == user_id,
                )
            )
            return result.scalar_one_or_none()

    async def change_role(
        self,
        *,
        workspace_id: str,
        user_id: str,
        new_role: str,
    ) -> bool:
        """Update a member's role; returns True iff a row was updated.

        Validates ``new_role`` against the allowed enum. Owner-transfer
        flow needs to swap two rows atomically — do that with a manual
        transaction in the caller; this method is for non-owner changes.
        """
        _validate_role(new_role)
        async with self._sf() as session:
            result = await session.execute(
                update(WorkspaceMembershipRow)
                .where(
                    WorkspaceMembershipRow.workspace_id == workspace_id,
                    WorkspaceMembershipRow.user_id == user_id,
                )
                .values(role=new_role)
            )
            await session.commit()
            return (result.rowcount or 0) > 0
