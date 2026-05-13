"""Alembic revision 0001 round-trips on both Postgres and SQLite.

Verifies the first DeerFlow migration adds `users.default_workspace_id`
(with FK to `workspaces`) on upgrade and removes it on downgrade. Both
backends are exercised because the migration relies on
`op.batch_alter_table` for SQLite ALTER compatibility — we want to know
if either dialect regresses.

These tests are synchronous: alembic's command layer is sync, and our
`env.py` calls `asyncio.run(...)` internally. Running under
pytest-anyio would put us inside an event loop and crash that
`asyncio.run` call, so we keep the test bodies plain `def`.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

_ALEMBIC_INI = Path(__file__).resolve().parents[1] / "packages" / "harness" / "deerflow" / "persistence" / "migrations" / "alembic.ini"


def _make_alembic_config(url: str) -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _bootstrap_pre_pr4_schema(sync_url: str) -> None:
    """Create the minimal pre-PR4 schema the migration needs to ALTER.

    Only `users` (without `default_workspace_id`) and `workspaces` (just `id`)
    are required so the FK target resolves. The rest of the production
    schema is irrelevant to this migration.
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
                    email VARCHAR(255) NOT NULL
                )
                """
            )
        )
    engine.dispose()


def _assert_column_present(sync_url: str) -> None:
    engine = create_engine(sync_url)
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("users")}
    assert "default_workspace_id" in cols, f"column missing; got {cols}"
    fks = insp.get_foreign_keys("users")
    fk_to_ws = [fk for fk in fks if fk.get("referred_table") == "workspaces"]
    assert fk_to_ws, f"FK to workspaces missing; got {fks}"
    assert fk_to_ws[0]["constrained_columns"] == ["default_workspace_id"]
    assert fk_to_ws[0]["referred_columns"] == ["id"]
    engine.dispose()


def _assert_column_absent(sync_url: str) -> None:
    engine = create_engine(sync_url)
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("users")}
    assert "default_workspace_id" not in cols, f"column still present; got {cols}"
    engine.dispose()


# ---------- SQLite tests ----------------------------------------------------


def test_sqlite_upgrade_adds_default_workspace_id_with_fk() -> None:
    """SQLite: upgrade 0001 adds column + FK; batch_alter_table works."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        sync_url = f"sqlite:///{db_path}"
        async_url = f"sqlite+aiosqlite:///{db_path}"

        _bootstrap_pre_pr4_schema(sync_url)

        cfg = _make_alembic_config(async_url)
        command.upgrade(cfg, "0001_users_default_workspace")

        _assert_column_present(sync_url)


def test_sqlite_downgrade_removes_default_workspace_id() -> None:
    """SQLite: downgrade 0001 removes the column it added."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        sync_url = f"sqlite:///{db_path}"
        async_url = f"sqlite+aiosqlite:///{db_path}"

        _bootstrap_pre_pr4_schema(sync_url)

        cfg = _make_alembic_config(async_url)
        command.upgrade(cfg, "0001_users_default_workspace")
        _assert_column_present(sync_url)

        command.downgrade(cfg, "-1")
        _assert_column_absent(sync_url)


# ---------- Postgres tests --------------------------------------------------


@pytest.mark.postgres
def test_postgres_upgrade_adds_default_workspace_id_with_fk(postgres_url: str) -> None:
    """Postgres: upgrade 0001 adds column + FK pointing at workspaces(id)."""
    sync_url = postgres_url.replace("+asyncpg", "+psycopg")
    _bootstrap_pre_pr4_schema(sync_url)

    cfg = _make_alembic_config(postgres_url)
    command.upgrade(cfg, "head")

    _assert_column_present(sync_url)


@pytest.mark.postgres
def test_postgres_downgrade_removes_default_workspace_id(postgres_url: str) -> None:
    """Postgres: downgrade 0001 cleanly drops the FK and column."""
    sync_url = postgres_url.replace("+asyncpg", "+psycopg")
    _bootstrap_pre_pr4_schema(sync_url)

    cfg = _make_alembic_config(postgres_url)
    command.upgrade(cfg, "head")
    _assert_column_present(sync_url)

    command.downgrade(cfg, "-1")
    _assert_column_absent(sync_url)
