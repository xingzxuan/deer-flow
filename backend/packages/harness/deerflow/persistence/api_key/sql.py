"""SQLAlchemy-backed API key repository (Stage 1 PR1).

``get_active_by_hash`` is the auth hot path. ``revoked_at IS NULL``
rides the partial index ``idx_api_keys_active``; expiry is filtered in
Python so the behaviour is identical across sqlite/postgres drivers.

``_row_to_dict`` deliberately omits ``key_hash`` — no dict this
repository returns ever carries the secret material.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.api_key.model import ApiKeyRow


class ApiKeyRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    @staticmethod
    def _row_to_dict(row: ApiKeyRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "service_account_id": row.service_account_id,
            "key_prefix": row.key_prefix,
            "name": row.name,
            "scopes": row.scopes,
            "rate_limit_rpm": row.rate_limit_rpm,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
            "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    async def create(
        self,
        *,
        service_account_id: str,
        key_prefix: str,
        key_hash: str,
        name: str,
        scopes: str,
        expires_at: datetime | None = None,
    ) -> dict[str, Any]:
        row = ApiKeyRow(
            id=str(uuid.uuid4()),
            service_account_id=service_account_id,
            key_prefix=key_prefix,
            key_hash=key_hash,
            name=name,
            scopes=scopes,
            expires_at=expires_at,
            created_at=datetime.now(UTC),
        )
        async with self._sf() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return self._row_to_dict(row)

    async def get(self, key_id: str) -> dict[str, Any] | None:
        async with self._sf() as session:
            row = await session.get(ApiKeyRow, key_id)
            return self._row_to_dict(row) if row else None

    async def get_active_by_hash(self, key_hash: str) -> dict[str, Any] | None:
        """Auth hot path: return the key iff not revoked and not expired."""
        async with self._sf() as session:
            result = await session.execute(select(ApiKeyRow).where(ApiKeyRow.key_hash == key_hash, ApiKeyRow.revoked_at.is_(None)))
            row = result.scalar_one_or_none()
            if row is None:
                return None
            if row.expires_at is not None:
                expires_at = row.expires_at
                # SQLite (aiosqlite) returns naive datetimes even for DateTime(timezone=True);
                # treat them as UTC so the comparison works driver-agnostically.
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
                if expires_at <= datetime.now(UTC):
                    return None
            return self._row_to_dict(row)

    async def list_by_service_account(self, service_account_id: str) -> list[dict[str, Any]]:
        async with self._sf() as session:
            result = await session.execute(select(ApiKeyRow).where(ApiKeyRow.service_account_id == service_account_id).order_by(ApiKeyRow.created_at.desc()))
            return [self._row_to_dict(r) for r in result.scalars()]

    async def revoke(self, key_id: str) -> None:
        """Soft-revoke: set ``revoked_at`` (row is kept for audit)."""
        async with self._sf() as session:
            await session.execute(update(ApiKeyRow).where(ApiKeyRow.id == key_id).values(revoked_at=datetime.now(UTC)))
            await session.commit()

    async def touch_last_used(self, key_id: str) -> None:
        """Best-effort: stamp ``last_used_at`` after a successful auth."""
        async with self._sf() as session:
            await session.execute(update(ApiKeyRow).where(ApiKeyRow.id == key_id).values(last_used_at=datetime.now(UTC)))
            await session.commit()
