"""Backfill ``workspace_id`` on PR5 business tables.

Three-step backfill (each idempotent — re-running picks up where a crash
left off because every step's WHERE clause filters already-processed rows):

  1. For each user without ``default_workspace_id``: create a personal
     workspace + ``owner`` membership + write back the user's
     ``default_workspace_id``.
  2. ``UPDATE`` each of ``threads_meta`` / ``runs`` / ``feedback`` /
     ``run_events`` setting ``workspace_id`` from the row's owner's
     ``users.default_workspace_id``. Only touches rows where
     ``workspace_id IS NULL`` and ``user_id IS NOT NULL``.
  3. Any rows still with ``workspace_id IS NULL`` (truly orphan — they had
     ``user_id = NULL`` to begin with) are assigned the *legacy* workspace
     UUID ``00000000-0000-0000-0000-000000000000``. The script creates
     that workspace on demand, owned by the platform admin.

Usage::

    PYTHONPATH=. python scripts/backfill_workspace_id.py [--dry-run]

T5.4 only ships the skeleton — the three step bodies are filled in by
T5.5 / T5.6 / T5.7 along with their per-step tests.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.gateway.auth.workspace_slug import auto_slug_from_email, next_available_slug
from deerflow.persistence.base import Base
from deerflow.persistence.feedback.model import FeedbackRow
from deerflow.persistence.models.run_event import RunEventRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.persistence.user.model import UserRow
from deerflow.persistence.workspace import WorkspaceRepository
from deerflow.persistence.workspace.sql import SLUG_BLACKLIST
from deerflow.persistence.workspace_membership import WorkspaceMembershipRepository

logger = logging.getLogger(__name__)

# The legacy workspace anchor. Stage 0 LOCK'd UUID — chosen as the standard
# nil UUID so SQL log scans can spot it instantly.
LEGACY_WORKSPACE_ID = "00000000-0000-0000-0000-000000000000"
LEGACY_WORKSPACE_SLUG = "legacy"
LEGACY_WORKSPACE_NAME = "Legacy Workspace"

# The four business tables that gained ``workspace_id`` in alembic 0002.
_BUSINESS_TABLES: tuple[str, ...] = ("threads_meta", "runs", "feedback", "run_events")

# Map table-name to ORM class so we can build a portable correlated UPDATE
# using SQLAlchemy expression language (SQLite < 3.33 lacks UPDATE-FROM
# but supports correlated subqueries on every version we ship).
_TABLE_MODELS: dict[str, type[Base]] = {
    "threads_meta": ThreadMetaRow,
    "runs": RunRow,
    "feedback": FeedbackRow,
    "run_events": RunEventRow,
}


async def _step1_create_workspaces_for_users(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    dry_run: bool,
) -> int:
    """Create one workspace + owner membership for each user missing default_workspace_id.

    Returns the count of users a workspace was created for. Idempotent —
    users that already have ``default_workspace_id`` are skipped so a
    crashed run can resume safely.
    """
    async with session_factory() as session:
        result = await session.execute(select(UserRow.id, UserRow.email).where(UserRow.default_workspace_id.is_(None)))
        candidates = [(row.id, row.email) for row in result]

    if not candidates:
        return 0

    ws_repo = WorkspaceRepository(session_factory)
    m_repo = WorkspaceMembershipRepository(session_factory)
    created = 0
    for user_id, email in candidates:
        base_slug = auto_slug_from_email(email)

        async def slug_exists(s: str) -> bool:
            if s in SLUG_BLACKLIST:
                return True
            return (await ws_repo.get_by_slug(s)) is not None

        unique_slug = await next_available_slug(base_slug, exists_check=slug_exists)

        if dry_run:
            logger.info("WOULD create workspace for user=%s email=%s slug=%s", user_id, email, unique_slug)
            created += 1
            continue

        display_local = email.split("@", 1)[0]
        workspace = await ws_repo.create(
            name=f"{display_local}'s Workspace"[:64],
            slug=unique_slug,
            owner_id=user_id,
        )
        await m_repo.add(workspace_id=workspace["id"], user_id=user_id, role="owner")
        async with session_factory() as session:
            await session.execute(update(UserRow).where(UserRow.id == user_id).values(default_workspace_id=workspace["id"]))
            await session.commit()
        created += 1
        logger.info("Created workspace %s (slug=%s) for user=%s", workspace["id"], unique_slug, user_id)

    return created


async def _step2_update_table_from_users(
    session_factory: async_sessionmaker[AsyncSession],
    table: str,
    *,
    dry_run: bool,
) -> int:
    """UPDATE *table* setting workspace_id from owner's users.default_workspace_id.

    Uses a correlated subquery (portable across SQLite + Postgres). Filters
    ``workspace_id IS NULL AND user_id IS NOT NULL`` so already-set rows
    and truly orphan rows are skipped (Step 3 handles the latter).

    Returns the number of rows updated (or that *would* be updated under
    ``dry_run``).
    """
    model = _TABLE_MODELS[table]
    workspace_col = model.workspace_id
    user_col = model.user_id

    # Subquery: pull the user's default_workspace_id for each row.
    correlated_default = select(UserRow.default_workspace_id).where(UserRow.id == user_col).scalar_subquery()

    if dry_run:
        # Count rows whose owner has a default_workspace_id assigned — only
        # those would get touched by the actual UPDATE.
        count_stmt = select(func.count()).select_from(model).join(UserRow, UserRow.id == user_col).where(workspace_col.is_(None), user_col.is_not(None), UserRow.default_workspace_id.is_not(None))
        async with session_factory() as session:
            count = (await session.execute(count_stmt)).scalar_one() or 0
        logger.info("WOULD update %d rows in %s from users.default_workspace_id", count, table)
        return int(count)

    stmt = update(model).where(workspace_col.is_(None), user_col.is_not(None)).values(workspace_id=correlated_default)
    async with session_factory() as session:
        result = await session.execute(stmt)
        await session.commit()
        rowcount = result.rowcount or 0
    logger.info("Updated %d rows in %s from users.default_workspace_id", rowcount, table)
    return int(rowcount)


async def _step3_assign_legacy_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    table: str,
    *,
    dry_run: bool,
) -> int:
    """Assign LEGACY_WORKSPACE_ID to *table* rows still missing workspace_id.

    Filled in by T5.7 (also responsible for ensuring the legacy workspace row exists).
    """
    _ = session_factory
    _ = table
    _ = dry_run
    return 0


async def backfill(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run all three backfill steps; return a per-step row-count report.

    Order matters: Step 1 must populate ``users.default_workspace_id``
    before Step 2 can correlate business rows back through ``users``.
    """
    report: dict[str, Any] = {"dry_run": dry_run}

    report["users_workspaces_created"] = await _step1_create_workspaces_for_users(session_factory, dry_run=dry_run)
    for table in _BUSINESS_TABLES:
        report[f"{table}_from_users"] = await _step2_update_table_from_users(session_factory, table, dry_run=dry_run)
    for table in _BUSINESS_TABLES:
        report[f"{table}_legacy"] = await _step3_assign_legacy_workspace(session_factory, table, dry_run=dry_run)

    return report


def _build_session_factory_from_config() -> async_sessionmaker[AsyncSession]:
    """Build an async session factory from the active config.yaml.

    Avoids importing on module load so unit tests can stub
    ``session_factory`` directly without booting the full config pipeline.
    """
    from deerflow.config import get_app_config
    from deerflow.persistence.engine import get_session_factory, init_engine_from_config

    asyncio.run(init_engine_from_config(get_app_config().database))
    sf = get_session_factory()
    if sf is None:
        raise RuntimeError(
            "database.backend=memory: nothing to backfill. Switch config.yaml to sqlite/postgres first.",
        )
    return sf


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill workspace_id on Stage 0 business tables (idempotent).")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the rows each step would touch without writing.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    sf = _build_session_factory_from_config()
    report = asyncio.run(backfill(sf, dry_run=args.dry_run))

    logger.info("Backfill report (dry_run=%s):", args.dry_run)
    for key, value in report.items():
        if key == "dry_run":
            continue
        logger.info("  %s: %s", key, value)


if __name__ == "__main__":
    main()
