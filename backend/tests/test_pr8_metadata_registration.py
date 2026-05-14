"""Acceptance test for PR8: ``Base.metadata.create_all()`` automatically
provisions ``service_accounts`` / ``api_keys`` / ``external_users``.

The harness layer registers all ORM models through ``deerflow.persistence.models``
(imported for side effects from ``engine.init_engine``). This test guards
against a row class being defined but accidentally left out of the
registration entry point — a class table that never gets created at
``init_engine`` time would otherwise silently break Stage 1 once the
API-key auth layer starts inserting rows.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_pr8_tables_present_after_init_engine(tmp_path):
    from deerflow.persistence.engine import close_engine, get_engine, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        engine = get_engine()
        assert engine is not None

        def _table_names(sync_conn):
            return set(inspect(sync_conn).get_table_names())

        async with engine.connect() as conn:
            tables = await conn.run_sync(_table_names)

        assert {"service_accounts", "api_keys", "external_users"}.issubset(tables), f"PR8 tables missing from create_all: have {sorted(tables)}"
    finally:
        await close_engine()
