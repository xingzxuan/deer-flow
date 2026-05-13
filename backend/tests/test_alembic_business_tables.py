"""Alembic 0002 / 0003 round-trips on both Postgres and SQLite.

PR5 has two migrations:

* ``0002_business_tables_workspace`` — adds *nullable* ``workspace_id``
  + FK to ``workspaces`` on ``threads_meta`` / ``runs`` / ``feedback`` /
  ``run_events``, plus a composite index on threads_meta for the common
  "list a workspace's threads for a user, newest first" query.
* ``0003_business_tables_workspace_not_null`` — flips the column to
  ``NOT NULL`` (refusing to upgrade if NULL rows remain) and adds the
  UNIQUE (workspace_id, thread_id) index on ``threads_meta``.

Pattern mirrors ``test_alembic_default_workspace_id.py``: synchronous
test bodies (alembic's command layer is sync, ``env.py`` calls
``asyncio.run`` internally — running under pytest-anyio would explode
that nested event loop). The pre-migration schema is bootstrapped with
just the columns those migrations touch, so we don't depend on the
production ORM staying frozen.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

_ALEMBIC_INI = Path(__file__).resolve().parents[1] / "packages" / "harness" / "deerflow" / "persistence" / "migrations" / "alembic.ini"

# Tables the PR5 migrations touch.
_BUSINESS_TABLES = ("threads_meta", "runs", "feedback", "run_events")


def _make_alembic_config(url: str) -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _bootstrap_pre_pr5_schema(sync_url: str) -> None:
    """Create the post-PR4 schema PR5 migrations expect.

    Includes ``workspaces`` (FK target), ``users`` (already has
    ``default_workspace_id`` from 0001 — but we skip 0001 here and bootstrap
    the columns directly so the migration's behaviour can be tested in
    isolation), and the four business tables.
    """
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE workspaces (
                    id VARCHAR(36) PRIMARY KEY,
                    name VARCHAR(64) NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE users (
                    id VARCHAR(36) PRIMARY KEY,
                    email VARCHAR(255) NOT NULL,
                    default_workspace_id VARCHAR(36)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE threads_meta (
                    thread_id VARCHAR(64) PRIMARY KEY,
                    user_id VARCHAR(64),
                    updated_at TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE runs (
                    run_id VARCHAR(64) PRIMARY KEY,
                    thread_id VARCHAR(64) NOT NULL,
                    user_id VARCHAR(64)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE feedback (
                    feedback_id VARCHAR(64) PRIMARY KEY,
                    thread_id VARCHAR(64) NOT NULL,
                    run_id VARCHAR(64) NOT NULL,
                    user_id VARCHAR(64),
                    rating INTEGER NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE run_events (
                    id INTEGER PRIMARY KEY,
                    thread_id VARCHAR(64) NOT NULL,
                    run_id VARCHAR(64) NOT NULL,
                    user_id VARCHAR(64),
                    event_type VARCHAR(32) NOT NULL,
                    category VARCHAR(16) NOT NULL,
                    content TEXT,
                    seq INTEGER NOT NULL
                )
                """
            )
        )
        # Pretend 0001 has already run so 0002 is the next revision applied.
        conn.execute(
            text(
                """
                CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)
                """
            )
        )
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES ('0001_users_default_workspace')"))
    engine.dispose()


def _assert_workspace_id_present(sync_url: str, *, nullable: bool) -> None:
    engine = create_engine(sync_url)
    insp = inspect(engine)
    for table in _BUSINESS_TABLES:
        cols = {c["name"]: c for c in insp.get_columns(table)}
        assert "workspace_id" in cols, f"{table}: workspace_id missing; got {list(cols)}"
        col = cols["workspace_id"]
        assert col["nullable"] is nullable, f"{table}.workspace_id nullable expected {nullable}, got {col['nullable']}"
        fks = insp.get_foreign_keys(table)
        ws_fks = [fk for fk in fks if fk.get("referred_table") == "workspaces" and fk.get("constrained_columns") == ["workspace_id"]]
        assert ws_fks, f"{table}: FK to workspaces missing; got {fks}"
    idxs = {i["name"] for i in insp.get_indexes("threads_meta")}
    assert "idx_threads_meta_workspace_user_updated" in idxs, f"threads_meta composite index missing; got {idxs}"
    engine.dispose()


def _assert_workspace_id_absent(sync_url: str) -> None:
    engine = create_engine(sync_url)
    insp = inspect(engine)
    for table in _BUSINESS_TABLES:
        cols = {c["name"] for c in insp.get_columns(table)}
        assert "workspace_id" not in cols, f"{table}: workspace_id still present; got {cols}"
    idxs = {i["name"] for i in insp.get_indexes("threads_meta")}
    assert "idx_threads_meta_workspace_user_updated" not in idxs, f"threads_meta composite index still present; got {idxs}"
    engine.dispose()


# ---------- SQLite tests ----------------------------------------------------


def test_sqlite_upgrade_0002_adds_workspace_id_column() -> None:
    """0002 adds nullable workspace_id + FK + composite index on SQLite."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        sync_url = f"sqlite:///{db_path}"
        async_url = f"sqlite+aiosqlite:///{db_path}"

        _bootstrap_pre_pr5_schema(sync_url)
        cfg = _make_alembic_config(async_url)
        command.upgrade(cfg, "0002_business_tables_workspace")
        _assert_workspace_id_present(sync_url, nullable=True)


def test_sqlite_downgrade_0002_removes_workspace_id_column() -> None:
    """0002 downgrade drops the column + FK + composite index cleanly."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        sync_url = f"sqlite:///{db_path}"
        async_url = f"sqlite+aiosqlite:///{db_path}"

        _bootstrap_pre_pr5_schema(sync_url)
        cfg = _make_alembic_config(async_url)
        command.upgrade(cfg, "0002_business_tables_workspace")
        _assert_workspace_id_present(sync_url, nullable=True)
        command.downgrade(cfg, "-1")
        _assert_workspace_id_absent(sync_url)


# ---------- Postgres tests --------------------------------------------------


@pytest.mark.postgres
def test_postgres_upgrade_0002_adds_workspace_id_column(postgres_url: str) -> None:
    """Postgres: 0002 adds nullable workspace_id + FK + composite index."""
    sync_url = postgres_url.replace("+asyncpg", "+psycopg")
    _bootstrap_pre_pr5_schema(sync_url)

    cfg = _make_alembic_config(postgres_url)
    command.upgrade(cfg, "0002_business_tables_workspace")
    _assert_workspace_id_present(sync_url, nullable=True)


@pytest.mark.postgres
def test_postgres_downgrade_0002_removes_workspace_id_column(postgres_url: str) -> None:
    """Postgres: 0002 downgrade drops column + FK + index cleanly."""
    sync_url = postgres_url.replace("+asyncpg", "+psycopg")
    _bootstrap_pre_pr5_schema(sync_url)

    cfg = _make_alembic_config(postgres_url)
    command.upgrade(cfg, "0002_business_tables_workspace")
    _assert_workspace_id_present(sync_url, nullable=True)
    command.downgrade(cfg, "-1")
    _assert_workspace_id_absent(sync_url)


# ---------- 0003 will be exercised once T5.9 lands -------------------------
# Placeholders for IntegrityError + raise-on-null behaviour are wired
# alongside the 0003 implementation; see T5.10.

_ = IntegrityError  # silence unused-import warning until 0003 tests land
