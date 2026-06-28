"""SQLAlchemy-backed external user repository (Stage 1 PR1).

Built but NOT yet wired to any auth path — the X-External-User-Id
passthrough that calls ``upsert`` lands in a later track-2 PR. ``upsert``
is idempotent on (service_account_id, external_id).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.external_user.model import ExternalUserRow


class ExternalUserRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    @staticmethod
    def _row_to_dict(row: ExternalUserRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "workspace_id": row.workspace_id,
            "service_account_id": row.service_account_id,
            "external_id": row.external_id,
            "display_name": row.display_name,
            "metadata": dict(row.metadata_json or {}),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
        }

    async def get(self, external_user_id: str) -> dict[str, Any] | None:
        async with self._sf() as session:
            row = await session.get(ExternalUserRow, external_user_id)
            return self._row_to_dict(row) if row else None

    async def get_by_external_id(self, *, service_account_id: str, external_id: str) -> dict[str, Any] | None:
        async with self._sf() as session:
            result = await session.execute(
                select(ExternalUserRow).where(
                    ExternalUserRow.service_account_id == service_account_id,
                    ExternalUserRow.external_id == external_id,
                )
            )
            row = result.scalar_one_or_none()
            return self._row_to_dict(row) if row else None

    async def list_by_workspace(self, workspace_id: str) -> list[dict[str, Any]]:
        async with self._sf() as session:
            result = await session.execute(select(ExternalUserRow).where(ExternalUserRow.workspace_id == workspace_id).order_by(ExternalUserRow.created_at.desc()))
            return [self._row_to_dict(r) for r in result.scalars()]

    async def upsert(
        self,
        *,
        workspace_id: str,
        service_account_id: str,
        external_id: str,
        display_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Insert a new external user or refresh ``last_seen_at`` on an
        existing (service_account_id, external_id) row."""
        now = datetime.now(UTC)
        async with self._sf() as session:
            result = await session.execute(
                select(ExternalUserRow).where(
                    ExternalUserRow.service_account_id == service_account_id,
                    ExternalUserRow.external_id == external_id,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                row = ExternalUserRow(
                    id=str(uuid.uuid4()),
                    workspace_id=workspace_id,
                    service_account_id=service_account_id,
                    external_id=external_id,
                    display_name=display_name,
                    metadata_json=metadata or {},
                    created_at=now,
                    last_seen_at=now,
                )
                session.add(row)
            else:
                row.last_seen_at = now
                if display_name is not None:
                    row.display_name = display_name
                if metadata is not None:
                    row.metadata_json = metadata
            await session.commit()
            await session.refresh(row)
            return self._row_to_dict(row)
