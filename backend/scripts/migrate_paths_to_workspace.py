"""One-time migration: lift per-user thread / memory / agent dirs into per-workspace layout.

PR4 introduced ``{base_dir}/users/{user_id}/...`` for per-user isolation. PR6
adds the multi-tenant top dimension: ``{base_dir}/workspaces/{wid}/...``,
with per-user state nested under each workspace
(``{base_dir}/workspaces/{wid}/users/{uid}/memory.json`` etc.). This script
walks the old PR4 layout, looks up each user's ``default_workspace_id``
from the ``users`` table, and rewrites the path.

Usage:
    PYTHONPATH=. python scripts/migrate_paths_to_workspace.py [--dry-run] [--default-workspace WID]

The script is idempotent — re-running it after a successful migration is a no-op.
A ``--dry-run`` invocation must not write anything; it only logs what would happen.

Mapping rules:

- ``{base_dir}/users/{uid}/threads/{tid}/`` -> ``{base_dir}/workspaces/{wid}/threads/{tid}/``
- ``{base_dir}/users/{uid}/memory.json``    -> ``{base_dir}/workspaces/{wid}/users/{uid}/memory.json``
- ``{base_dir}/users/{uid}/agents/{name}/``  -> ``{base_dir}/workspaces/{wid}/users/{uid}/agents/{name}/``

``{wid}`` is read from ``users.default_workspace_id``. Users without one
fall through to ``--default-workspace`` (defaults to the special bucket
``"legacy_workspace"`` so they can be triaged manually). Pre-existing
destinations are preserved; legacy copies are moved to
``{base_dir}/migration-conflicts/<...>`` for human review.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
from pathlib import Path

from deerflow.config.paths import Paths, get_paths

logger = logging.getLogger(__name__)

LEGACY_WORKSPACE_FALLBACK = "legacy_workspace"


def _load_user_workspaces(paths: Paths) -> dict[str, str | None]:
    """Read ``user_id -> default_workspace_id`` from the local sqlite DB.

    Returns an empty dict when the database does not exist (fresh install
    / Postgres-only deployments). The Postgres path is out of scope for
    this script — the operator should run it with ``--default-workspace``
    set to the target workspace and skip the DB lookup.
    """
    db_path = paths.base_dir / "deer-flow.db"
    if not db_path.exists():
        logger.info("No sqlite database at %s — every user will use the fallback workspace.", db_path)
        return {}

    conn = sqlite3.connect(str(db_path))
    try:
        try:
            cursor = conn.execute("SELECT id, default_workspace_id FROM users")
        except sqlite3.OperationalError as e:
            logger.warning("Failed to query users.default_workspace_id: %s", e)
            return {}
        return {row[0]: row[1] for row in cursor.fetchall()}
    finally:
        conn.close()


def _resolve_workspace(user_id: str, user_workspaces: dict[str, str | None], fallback: str) -> str:
    wid = user_workspaces.get(user_id)
    return wid or fallback


def _move(src: Path, dest: Path, conflict_root: Path, *, dry_run: bool, label: str) -> str:
    """Move ``src`` to ``dest``; on conflict, divert legacy under ``conflict_root``.

    Returns a short string describing what happened (for the report).
    """
    if dest.exists():
        conflict_dest = conflict_root / label / src.name
        if not dry_run:
            conflict_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(conflict_dest))
        logger.warning("Conflict for %s/%s: legacy copy diverted to %s", label, src.name, conflict_dest)
        return f"conflict -> {conflict_dest}"

    if not dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
    return f"moved -> {dest}"


def migrate_user_tree(
    paths: Paths,
    user_id: str,
    workspace_id: str,
    *,
    dry_run: bool,
) -> list[dict]:
    """Lift one user's tree under a workspace. Returns per-asset report rows."""
    report: list[dict] = []
    legacy_user_dir = paths.base_dir / "users" / user_id
    if not legacy_user_dir.exists():
        return report

    conflict_root = paths.base_dir / "migration-conflicts" / "workspace-migration"

    # 1. threads/{tid}/ -> workspaces/{wid}/threads/{tid}/
    legacy_threads = legacy_user_dir / "threads"
    if legacy_threads.exists():
        for thread_dir in sorted(legacy_threads.iterdir()):
            if not thread_dir.is_dir():
                continue
            dest = paths.thread_dir(thread_dir.name, workspace_id=workspace_id)
            action = _move(thread_dir, dest, conflict_root, dry_run=dry_run, label=f"threads/{workspace_id}")
            report.append({"asset": "thread", "user_id": user_id, "workspace_id": workspace_id, "name": thread_dir.name, "action": action})
        if not dry_run and legacy_threads.exists() and not any(legacy_threads.iterdir()):
            legacy_threads.rmdir()

    # 2. memory.json -> workspaces/{wid}/users/{uid}/memory.json
    legacy_mem = legacy_user_dir / "memory.json"
    if legacy_mem.exists():
        dest = paths.user_memory_file(user_id, workspace_id=workspace_id)
        action = _move(legacy_mem, dest, conflict_root, dry_run=dry_run, label=f"users/{user_id}/memory")
        report.append({"asset": "memory", "user_id": user_id, "workspace_id": workspace_id, "name": "memory.json", "action": action})

    # 3. agents/{name}/ -> workspaces/{wid}/users/{uid}/agents/{name}/
    legacy_agents = legacy_user_dir / "agents"
    if legacy_agents.exists():
        for agent_dir in sorted(legacy_agents.iterdir()):
            if not agent_dir.is_dir():
                continue
            dest = paths.user_agent_dir(user_id, agent_dir.name, workspace_id=workspace_id)
            action = _move(agent_dir, dest, conflict_root, dry_run=dry_run, label=f"users/{user_id}/agents")
            report.append({"asset": "agent", "user_id": user_id, "workspace_id": workspace_id, "name": agent_dir.name, "action": action})
        if not dry_run and legacy_agents.exists() and not any(legacy_agents.iterdir()):
            legacy_agents.rmdir()

    # 4. If the user dir is now empty, remove it so lifespan warnings clear.
    if not dry_run and legacy_user_dir.exists() and not any(legacy_user_dir.iterdir()):
        legacy_user_dir.rmdir()

    return report


def migrate(
    paths: Paths,
    *,
    user_workspaces: dict[str, str | None],
    fallback_workspace: str,
    dry_run: bool,
) -> list[dict]:
    """Top-level entry point: iterate every user under ``{base_dir}/users``."""
    legacy_users = paths.base_dir / "users"
    if not legacy_users.exists():
        logger.info("No legacy ``users/`` directory under %s — nothing to migrate.", paths.base_dir)
        return []

    report: list[dict] = []
    for user_dir in sorted(legacy_users.iterdir()):
        if not user_dir.is_dir():
            continue
        user_id = user_dir.name
        workspace_id = _resolve_workspace(user_id, user_workspaces, fallback_workspace)
        logger.info("Migrating user %s -> workspace %s", user_id, workspace_id)
        report.extend(migrate_user_tree(paths, user_id, workspace_id, dry_run=dry_run))

    if not dry_run and legacy_users.exists() and not any(legacy_users.iterdir()):
        legacy_users.rmdir()

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Lift legacy per-user paths into per-workspace layout (PR6).")
    parser.add_argument("--dry-run", action="store_true", help="Log actions without making changes.")
    parser.add_argument(
        "--default-workspace",
        default=LEGACY_WORKSPACE_FALLBACK,
        metavar="WID",
        help=(f"Workspace id to use for users without a ``default_workspace_id`` in the DB. Defaults to ``{LEGACY_WORKSPACE_FALLBACK}`` (matches the orphan-row bucket used by PR5 backfill)."),
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    paths = get_paths()
    logger.info("Base directory: %s", paths.base_dir)
    logger.info("Dry run: %s", args.dry_run)
    logger.info("Fallback workspace: %s", args.default_workspace)

    user_workspaces = _load_user_workspaces(paths)
    logger.info("Loaded %d user->workspace mappings from DB", len(user_workspaces))

    report = migrate(
        paths,
        user_workspaces=user_workspaces,
        fallback_workspace=args.default_workspace,
        dry_run=args.dry_run,
    )

    if not report:
        logger.info("Nothing to migrate.")
        return

    logger.info("Migration report (%d entries):", len(report))
    for entry in report:
        logger.info("  asset=%s user=%s workspace=%s name=%s action=%s", entry["asset"], entry["user_id"], entry["workspace_id"], entry["name"], entry["action"])


if __name__ == "__main__":
    main()
