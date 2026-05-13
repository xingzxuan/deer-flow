"""PR6 T6.14 — lifespan warns when legacy users/ tree still has content."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from app.gateway.app import _check_path_migration_pending


class _FakePaths:
    def __init__(self, base: Path):
        self.base_dir = base


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return tmp_path


def _patch_paths(base: Path):
    return patch("deerflow.config.paths.get_paths", return_value=_FakePaths(base))


def test_warns_when_legacy_users_dir_has_content(base: Path, caplog: pytest.LogCaptureFixture):
    (base / "users" / "alice" / "threads" / "t1").mkdir(parents=True)
    with _patch_paths(base), caplog.at_level(logging.WARNING, logger="app.gateway.app"):
        _check_path_migration_pending(app=None)  # type: ignore[arg-type]
    assert any("make migrate-paths" in rec.message for rec in caplog.records)


def test_silent_when_legacy_users_dir_missing(base: Path, caplog: pytest.LogCaptureFixture):
    with _patch_paths(base), caplog.at_level(logging.WARNING, logger="app.gateway.app"):
        _check_path_migration_pending(app=None)  # type: ignore[arg-type]
    assert not any("migrate-paths" in rec.message for rec in caplog.records)


def test_silent_when_legacy_users_dir_empty(base: Path, caplog: pytest.LogCaptureFixture):
    (base / "users").mkdir()
    with _patch_paths(base), caplog.at_level(logging.WARNING, logger="app.gateway.app"):
        _check_path_migration_pending(app=None)  # type: ignore[arg-type]
    assert not any("migrate-paths" in rec.message for rec in caplog.records)
