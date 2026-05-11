"""Stage 0 PR2 · default backend regression + sqlite-still-works.

Two facts pinned here:
  T2.3 — explicit ``database.backend: sqlite`` still produces a working
         config (backwards-compat for users who deliberately stay on SQLite).
  T2.4 — ``config.example.yaml`` default ``database.backend`` is ``postgres``
         (Stage 0 PR2 flipped this from sqlite).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from deerflow.config.app_config import AppConfig

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_extensions_config(path: Path) -> None:
    path.write_text(json.dumps({"mcpServers": {}, "skills": {}}), encoding="utf-8")


# ---------------------------------------------------------------------------
# T2.3 · sqlite backend regression
# ---------------------------------------------------------------------------


def test_explicit_sqlite_backend_still_works(tmp_path, monkeypatch) -> None:
    """A config with explicit ``database.backend: sqlite`` must still parse.

    Pin: PR2 made postgres the example default. This test guarantees that
    users who copy the SQLite fallback block to their config.yaml do not
    silently regress.
    """
    config_path = tmp_path / "config.yaml"
    extensions_path = tmp_path / "extensions_config.json"
    _write_extensions_config(extensions_path)
    config_path.write_text(
        yaml.safe_dump(
            {
                "models": [
                    {
                        "name": "test",
                        "use": "langchain_openai:ChatOpenAI",
                        "model": "gpt-4",
                    }
                ],
                "database": {
                    "backend": "sqlite",
                    "sqlite_dir": "/custom/sqlite/path",
                },
                "sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DEER_FLOW_EXTENSIONS_CONFIG_PATH", str(extensions_path))

    config = AppConfig.from_file(str(config_path))

    assert config.database.backend == "sqlite"
    assert config.database.sqlite_dir == "/custom/sqlite/path"


# ---------------------------------------------------------------------------
# T2.4 · default config.example.yaml uses postgres
# ---------------------------------------------------------------------------


def test_config_example_default_backend_is_postgres(monkeypatch) -> None:
    """Pin Stage 0 PR2's commitment: the example config defaults to Postgres.

    Loaded directly from the on-disk ``config.example.yaml`` so any future
    accidental flip back to sqlite would fail this test.
    """
    example_path = REPO_ROOT / "config.example.yaml"
    if not example_path.exists():
        pytest.skip(f"config.example.yaml not found at {example_path}")

    raw = yaml.safe_load(example_path.read_text(encoding="utf-8")) or {}
    db = raw.get("database") or {}

    assert db.get("backend") == "postgres", f"config.example.yaml database.backend is {db.get('backend')!r}; PR2 requires 'postgres' as the default"
    # The PG URL must come from the env (referenced as $DATABASE_URL),
    # never hardcoded with credentials.
    assert db.get("postgres_url") == "$DATABASE_URL", f"postgres_url should be '$DATABASE_URL' env reference, got {db.get('postgres_url')!r}"
