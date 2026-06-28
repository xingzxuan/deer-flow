"""SQLAlchemy-backed service account repository (Stage 1 PR1).

Mirrors :class:`WorkspaceRepository`: fresh session per method,
``_row_to_dict`` static helper. Workspace scoping is enforced by the
caller (route layer reads the workspace contextvar); the repository
takes ``workspace_id`` explicitly.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.service_account.model import ServiceAccountRow

_VALID_STATUSES = frozenset({"active", "suspended", "deleted"})


class ServiceAccountValidationError(ValueError):
    """Raised when service account input fails application-layer validation."""


class ServiceAccountRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    @staticmethod
    def _row_to_dict(row: ServiceAccountRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "workspace_id": row.workspace_id,
            "name": row.name,
            "role": row.role,
            "identity_mode": row.identity_mode,
            "status": row.status,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    async def create(
        self,
        *,
        workspace_id: str,
        name: str,
        created_by: str,
        role: str = "member",
        identity_mode: str = "collapsed",
        status: str = "active",
    ) -> dict[str, Any]:
        if status not in _VALID_STATUSES:
            raise ServiceAccountValidationError(f"status {status!r} not in {_VALID_STATUSES!r}")
        now = datetime.now(UTC)
        row = ServiceAccountRow(
            id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            name=name,
            role=role,
            identity_mode=identity_mode,
            status=status,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        async with self._sf() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return self._row_to_dict(row)

    async def get(self, sa_id: str) -> dict[str, Any] | None:
        async with self._sf() as session:
            row = await session.get(ServiceAccountRow, sa_id)
            return self._row_to_dict(row) if row else None

    async def get_active(self, sa_id: str) -> dict[str, Any] | None:
        """Return the row only when ``status == 'active'`` (auth hot path)."""
        async with self._sf() as session:
            row = await session.get(ServiceAccountRow, sa_id)
            if row is None or row.status != "active":
                return None
            return self._row_to_dict(row)

    async def list_by_workspace(self, workspace_id: str) -> list[dict[str, Any]]:
        async with self._sf() as session:
            result = await session.execute(select(ServiceAccountRow).where(ServiceAccountRow.workspace_id == workspace_id).order_by(ServiceAccountRow.created_at.desc()))
            return [self._row_to_dict(r) for r in result.scalars()]

    async def update_status(self, sa_id: str, status: str) -> None:
        if status not in _VALID_STATUSES:
            raise ServiceAccountValidationError(f"status {status!r} not in {_VALID_STATUSES!r}")
        async with self._sf() as session:
            await session.execute(update(ServiceAccountRow).where(ServiceAccountRow.id == sa_id).values(status=status, updated_at=datetime.now(UTC)))
            await session.commit()
