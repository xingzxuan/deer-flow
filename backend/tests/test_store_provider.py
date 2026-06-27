"""Tests for the async Store factory's backend selection.

Mirrors the checkpointer factory: when no legacy ``checkpointer`` section
is configured but a unified ``database`` section is, the store must use
that database backend instead of silently falling back to InMemoryStore.
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deerflow.config.database_config import DatabaseConfig
from deerflow.runtime.store.async_provider import make_store


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class TestStoreDatabaseFallback:
    @pytest.mark.anyio
    async def test_postgres_store_from_database_when_no_checkpointer_section(self):
        """make_store uses AsyncPostgresStore from the database section when
        no legacy checkpointer section is present. The +asyncpg dialect prefix
        is stripped so the same DATABASE_URL satisfies both SQLAlchemy and
        LangGraph's psycopg-based store."""
        mock_config = MagicMock()
        mock_config.checkpointer = None
        mock_config.database = DatabaseConfig(
            backend="postgres",
            postgres_url="postgresql+asyncpg://postgres:pw@localhost:5432/deerflow",
        )

        mock_store = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__.return_value = mock_store
        mock_cm.__aexit__.return_value = False

        mock_store_cls = MagicMock()
        mock_store_cls.from_conn_string.return_value = mock_cm

        mock_module = MagicMock()
        mock_module.AsyncPostgresStore = mock_store_cls

        with (
            patch("deerflow.runtime.store.async_provider.get_app_config", return_value=mock_config),
            patch.dict(sys.modules, {"langgraph.store.postgres.aio": mock_module}),
        ):
            async with make_store() as store:
                assert store is mock_store

        # dialect prefix stripped to libpq conninfo
        mock_store_cls.from_conn_string.assert_called_once_with("postgresql://postgres:pw@localhost:5432/deerflow")
        mock_store.setup.assert_awaited_once()

    @pytest.mark.anyio
    async def test_memory_when_no_checkpointer_and_no_database(self):
        """With neither a checkpointer section nor a non-memory database,
        the store falls back to InMemoryStore."""
        from langgraph.store.memory import InMemoryStore

        mock_config = MagicMock()
        mock_config.checkpointer = None
        mock_config.database = DatabaseConfig(backend="memory")

        with patch("deerflow.runtime.store.async_provider.get_app_config", return_value=mock_config):
            async with make_store() as store:
                assert isinstance(store, InMemoryStore)

    @pytest.mark.anyio
    async def test_checkpointer_section_takes_precedence_over_database(self):
        """A legacy checkpointer section still wins over the database section."""
        from deerflow.config.checkpointer_config import CheckpointerConfig

        mock_config = MagicMock()
        mock_config.checkpointer = CheckpointerConfig(type="memory")
        mock_config.database = DatabaseConfig(
            backend="postgres",
            postgres_url="postgresql+asyncpg://postgres:pw@localhost:5432/deerflow",
        )

        from langgraph.store.memory import InMemoryStore

        with patch("deerflow.runtime.store.async_provider.get_app_config", return_value=mock_config):
            async with make_store() as store:
                # checkpointer.type == memory → InMemoryStore, database ignored
                assert isinstance(store, InMemoryStore)
