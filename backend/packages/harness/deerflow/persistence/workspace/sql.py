"""SQLAlchemy-backed workspace repository.

CRUD + slug lookup for ``workspaces``. Membership-aware methods
(``get``, ``list_by_user``) JOIN against ``workspace_memberships`` so
callers cannot read workspaces they don't belong to.

Three-state ``user_id`` parameter (same convention as
:class:`ThreadMetaRepository`):
  - ``AUTO`` → read from contextvar; raise if unset
  - explicit ``str`` → override contextvar (admin / tests)
  - explicit ``None`` → no filter (migration / CLI only)
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.workspace.model import WorkspaceRow
from deerflow.persistence.workspace_membership.model import WorkspaceMembershipRow
from deerflow.runtime.user_context import AUTO, _AutoSentinel, resolve_user_id

# slug 字符集 / 长度（与 workspace-schema-design §2.1 锁定一致）。
_SLUG_PATTERN = re.compile(r"^[a-z0-9](-?[a-z0-9])*$")
_SLUG_MIN_LEN = 3
_SLUG_MAX_LEN = 32

# slug 黑名单（应用层校验，不写 DB constraint）。包含 ADR-007 §4 保留 slug
# + 路径 + Next.js 保留 + 业务保留词。
SLUG_BLACKLIST = frozenset(
    {
        "admin",
        "api",
        "auth",
        "login",
        "signup",
        "accept-invite",
        "pricing",
        "docs",
        "status",
        "platform",
        "system",
        "health",
        "static",
        "public",
        "favicon.ico",
        "robots.txt",
        "sitemap.xml",
        "_next",
        ".well-known",
        "settings",
        "billing",
        "onboarding",
        "select-workspace",
    }
)

# 允许的 status 集合。
_VALID_STATUSES = frozenset({"active", "suspended", "deleted"})


class WorkspaceValidationError(ValueError):
    """Raised when workspace input fails application-layer validation
    (slug format / blacklist / status enum)."""


def _validate_slug(slug: str) -> None:
    """Raise :class:`WorkspaceValidationError` if slug is invalid."""
    if not isinstance(slug, str):
        raise WorkspaceValidationError(f"slug must be a string, got {type(slug).__name__}")
    if not (_SLUG_MIN_LEN <= len(slug) <= _SLUG_MAX_LEN):
        raise WorkspaceValidationError(f"slug length must be between {_SLUG_MIN_LEN} and {_SLUG_MAX_LEN}, got {len(slug)}")
    if not _SLUG_PATTERN.fullmatch(slug):
        raise WorkspaceValidationError(f"slug {slug!r} does not match required pattern ^[a-z0-9](-?[a-z0-9])*$")
    if slug in SLUG_BLACKLIST:
        raise WorkspaceValidationError(f"slug {slug!r} is reserved")


def _validate_status(status: str) -> None:
    if status not in _VALID_STATUSES:
        raise WorkspaceValidationError(f"status {status!r} is not in allowed set {_VALID_STATUSES!r}")


class WorkspaceRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    @staticmethod
    def _row_to_dict(row: WorkspaceRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "name": row.name,
            "slug": row.slug,
            "status": row.status,
            "owner_id": row.owner_id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    async def create(
        self,
        *,
        name: str,
        slug: str,
        owner_id: str,
        workspace_id: str | None = None,
        status: str = "active",
    ) -> dict[str, Any]:
        """Create a new workspace.

        ``workspace_id`` is optional — UUID v4 is generated if omitted.
        Caller is responsible for creating the matching ``owner`` row
        in ``workspace_memberships`` (this is typically done in the same
        transaction by the registration flow; we deliberately don't bundle
        it here to keep the repository single-responsibility).

        Raises :class:`WorkspaceValidationError` for invalid slug / status.
        Raises :class:`sqlalchemy.exc.IntegrityError` for slug collision
        or invalid owner_id FK.
        """
        _validate_slug(slug)
        _validate_status(status)
        wid = workspace_id or str(uuid.uuid4())
        now = datetime.now(UTC)
        row = WorkspaceRow(
            id=wid,
            name=name,
            slug=slug,
            status=status,
            owner_id=owner_id,
            created_at=now,
            updated_at=now,
        )
        async with self._sf() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return self._row_to_dict(row)

    async def get(
        self,
        workspace_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
    ) -> dict[str, Any] | None:
        """Return workspace row IFF caller is a member.

        ``user_id=None`` bypasses the membership filter (migration / CLI).
        Returns ``None`` if the workspace does not exist or the caller is
        not a member.
        """
        resolved_user_id = resolve_user_id(user_id, method_name="WorkspaceRepository.get")
        async with self._sf() as session:
            row = await session.get(WorkspaceRow, workspace_id)
            if row is None:
                return None
            if resolved_user_id is None:
                # Explicit bypass (migration / admin path).
                return self._row_to_dict(row)
            # Membership check via separate SELECT (cheap; index covers it).
            membership = await session.execute(
                select(WorkspaceMembershipRow).where(
                    WorkspaceMembershipRow.workspace_id == workspace_id,
                    WorkspaceMembershipRow.user_id == resolved_user_id,
                )
            )
            if membership.scalar_one_or_none() is None:
                return None
            return self._row_to_dict(row)

    async def get_by_slug(self, slug: str) -> dict[str, Any] | None:
        """Public slug lookup — does NOT check membership.

        Used by path-based routing (``/{slug}/...``) where we need to
        resolve slug → workspace_id BEFORE we know if caller belongs.
        Membership check happens downstream in the route handler.
        """
        async with self._sf() as session:
            result = await session.execute(select(WorkspaceRow).where(WorkspaceRow.slug == slug))
            row = result.scalar_one_or_none()
            return self._row_to_dict(row) if row else None

    async def list_by_user(
        self,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
    ) -> list[dict[str, Any]]:
        """Return all workspaces caller is a member of, ordered by joined_at desc.

        ``user_id=None`` lists ALL workspaces (migration / admin path).
        """
        resolved_user_id = resolve_user_id(user_id, method_name="WorkspaceRepository.list_by_user")
        async with self._sf() as session:
            stmt = select(WorkspaceRow).order_by(WorkspaceRow.created_at.desc())
            if resolved_user_id is not None:
                stmt = stmt.join(
                    WorkspaceMembershipRow,
                    WorkspaceMembershipRow.workspace_id == WorkspaceRow.id,
                ).where(WorkspaceMembershipRow.user_id == resolved_user_id)
            result = await session.execute(stmt)
            return [self._row_to_dict(r) for r in result.scalars()]

    async def update_status(self, workspace_id: str, status: str) -> None:
        """Platform-admin operation: change workspace status (active/suspended/deleted).

        No membership check — this is for platform-level operations. Audit
        logging belongs at the route layer.
        """
        _validate_status(status)
        async with self._sf() as session:
            await session.execute(update(WorkspaceRow).where(WorkspaceRow.id == workspace_id).values(status=status, updated_at=datetime.now(UTC)))
            await session.commit()

    async def delete(self, workspace_id: str) -> None:
        """Hard-delete a workspace. CASCADE drops all memberships.

        Intentionally no membership check — caller (platform admin route)
        must enforce authorization. Stage 0 doesn't expose this to end
        users; Stage 2+ adds it behind owner_only permission.
        """
        async with self._sf() as session:
            row = await session.get(WorkspaceRow, workspace_id)
            if row is not None:
                await session.delete(row)
                await session.commit()
