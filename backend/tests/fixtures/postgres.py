"""Postgres testcontainer fixtures for Stage 0 PR1.

Provides per-test ephemeral database isolation atop a single session-scoped
container. Tests marked ``@pytest.mark.postgres`` request the ``postgres_url``
fixture, which yields an asyncpg connection URL pointing at a freshly-created
database. The database is force-dropped after the test (any leaked connections
get pg_terminate_backend'd first).

Why per-database rather than per-schema:
  asyncpg (the SQLAlchemy async driver we use) doesn't honor URL-embedded
  search_path the way psycopg does. Per-database isolation is one extra
  CREATE/DROP per test (~50ms), but lets test app code use its full schema
  unchanged.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session")
def postgres_container():
    """Session-scoped Postgres 16 container shared across all postgres tests.

    Started once per pytest session. Subsequent tests piggy-back on the same
    container; each gets its own database via the ``postgres_url`` fixture.

    Skipped (and the test marked skip) if Docker is unavailable on the host —
    testcontainers raises ``DockerException`` when it can't reach the daemon.
    """
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError as exc:  # pragma: no cover - install boundary
        pytest.skip(f"testcontainers[postgres] not installed: {exc}")

    try:
        # Image tag aligned with the production Aliyun RDS (PostgreSQL 17.9
        # confirmed by `make doctor` 2026-05-11). Bump together with RDS upgrades.
        with PostgresContainer("postgres:17-alpine") as pg:
            yield pg
    except Exception as exc:  # pragma: no cover - environment-dependent
        # DockerException, ConnectionError, etc. — surface a skip rather than
        # an error so devs without Docker can still run the rest of the suite.
        pytest.skip(f"could not start Postgres container ({exc})")


@pytest.fixture
def postgres_url(postgres_container) -> Iterator[str]:
    """Per-test ephemeral database URL (asyncpg dialect).

    Each invocation creates a unique database on the shared container and
    yields its URL. Teardown force-drops the database, terminating any
    backend connections the test forgot to close.
    """
    import psycopg
    from psycopg import sql

    db_name = f"test_{secrets.token_hex(8)}"
    raw = postgres_container.get_connection_url()  # postgresql+psycopg2://...
    # Strip the SQLAlchemy dialect prefix so plain psycopg can connect.
    base = raw.replace("postgresql+psycopg2://", "postgresql://")
    parent_url = base.rsplit("/", 1)[0] + "/postgres"

    # CREATE DATABASE must run outside a transaction; psycopg autocommit=True.
    with psycopg.connect(parent_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))

    asyncpg_url = raw.replace("postgresql+psycopg2://", "postgresql+asyncpg://").rsplit("/", 1)[0] + f"/{db_name}"

    try:
        yield asyncpg_url
    finally:
        with psycopg.connect(parent_url, autocommit=True) as conn:
            # Kick any leaked connections so DROP DATABASE doesn't block.
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                (db_name,),
            )
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(db_name)))
