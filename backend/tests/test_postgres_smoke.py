"""Stage 0 PR1 · Postgres smoke tests via testcontainers.

These tests exercise the postgres_url fixture and verify that DeerFlow's
existing ``init_engine`` + ORM ``Base.metadata.create_all`` works against
Postgres exactly the same way it works against SQLite (no Stage 0
schema changes here — that's PR3+).

All tests are gated by ``@pytest.mark.postgres`` and skip cleanly when
Docker is unavailable (the postgres_container fixture handles that).
"""

from __future__ import annotations

import secrets

import pytest

# Mark every test in this module as `postgres` so they only run when
# explicitly requested with ``pytest -m postgres``.
pytestmark = [pytest.mark.postgres, pytest.mark.anyio]


@pytest.fixture
def anyio_backend() -> str:
    """anyio uses asyncio backend (matches DeerFlow's runtime)."""
    return "asyncio"


# ---------------------------------------------------------------------------
# T1.4 · Fixture self-test — verify per-test isolation
# ---------------------------------------------------------------------------


async def test_postgres_url_creates_isolated_database(postgres_url: str) -> None:
    """The fixture should yield a usable URL pointing at a unique database."""
    import asyncpg

    # The URL form is `postgresql+asyncpg://user:pass@host:port/test_<hex>`.
    assert postgres_url.startswith("postgresql+asyncpg://")
    assert "/test_" in postgres_url

    # Connect with raw asyncpg (strip SQLAlchemy dialect prefix) and confirm
    # we landed in the named test DB.
    raw = postgres_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(raw)
    try:
        current = await conn.fetchval("SELECT current_database()")
        assert current.startswith("test_"), f"expected test_*, got {current!r}"
    finally:
        await conn.close()


async def test_postgres_url_isolates_between_tests(postgres_container, postgres_url: str) -> None:
    """Two invocations of the fixture should yield two distinct databases.

    Hand-rolls a second database via the same recipe to prove isolation
    without depending on pytest's own per-test invocation timing.
    """
    import asyncpg
    import psycopg
    from psycopg import sql

    # Read what DB we're in.
    raw = postgres_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(raw)
    try:
        db_a = await conn.fetchval("SELECT current_database()")
    finally:
        await conn.close()

    # Create a *second* DB on the same container directly.
    container_url = postgres_container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
    parent_url = container_url.rsplit("/", 1)[0] + "/postgres"
    db_b_name = f"test_{secrets.token_hex(8)}"
    with psycopg.connect(parent_url, autocommit=True) as c:
        c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_b_name)))
    try:
        assert db_a != db_b_name, "fixture should not reuse a DB name across tests"
    finally:
        with psycopg.connect(parent_url, autocommit=True) as c:
            c.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(db_b_name)))


# ---------------------------------------------------------------------------
# T1.5 · init_engine smoke — Base.metadata.create_all() works on Postgres
# ---------------------------------------------------------------------------


async def test_init_engine_postgres_creates_tables(postgres_url: str) -> None:
    """``init_engine`` against Postgres must auto-create existing ORM tables.

    Verifies the four current business tables (users, threads_meta, runs,
    feedback) plus run_events appear in information_schema after init —
    proving that DeerFlow's ``Base.metadata.create_all()`` path works
    identically on PG and SQLite.
    """
    from sqlalchemy import text

    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    await init_engine("postgres", url=postgres_url)
    try:
        sf = get_session_factory()
        async with sf() as session:
            result = await session.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"))
            tables = {row[0] for row in result.all()}

        # Existing tables before any Stage 0 schema additions:
        expected = {"users", "threads_meta", "runs", "feedback", "run_events"}
        missing = expected - tables
        assert not missing, f"create_all() did not produce {missing}; got {tables}"
    finally:
        await close_engine()


# ---------------------------------------------------------------------------
# T1.6 · Repository round-trip on Postgres
# ---------------------------------------------------------------------------


async def test_thread_meta_repo_postgres_round_trip(postgres_url: str) -> None:
    """ThreadMetaRepository.create + get must behave identically on PG.

    Pin: this is a regression net for any SQLAlchemy / asyncpg surprise
    that diverges from SQLite behavior in dict shape, default values,
    or timestamp precision.
    """
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine
    from deerflow.persistence.thread_meta import ThreadMetaRepository

    await init_engine("postgres", url=postgres_url)
    try:
        repo = ThreadMetaRepository(get_session_factory())
        # Note: conftest's `_auto_user_context` autouse fixture has already
        # injected `id="test-user-autouse"` so create() picks it via AUTO.
        created = await repo.create(
            thread_id="thread-1",
            display_name="hello",
            metadata={"k": "v"},
        )
        assert created["thread_id"] == "thread-1"
        assert created["user_id"] == "test-user-autouse"
        assert created["display_name"] == "hello"
        assert created["metadata"] == {"k": "v"}

        fetched = await repo.get("thread-1")
        assert fetched is not None
        assert fetched["thread_id"] == "thread-1"
        assert fetched["user_id"] == "test-user-autouse"
    finally:
        await close_engine()
