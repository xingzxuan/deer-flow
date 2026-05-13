"""PR6 T6.11 — ThreadDataMiddleware writes under workspace layout."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from deerflow.agents.middlewares.thread_data_middleware import ThreadDataMiddleware
from deerflow.config.paths import Paths
from deerflow.runtime.workspace_context import (
    reset_current_workspace,
    set_current_workspace,
)


class _FakeRuntime:
    def __init__(self, *, thread_id: str = "t1", run_id: str = "r1"):
        self.context = {"thread_id": thread_id, "run_id": run_id}


def test_paths_resolve_under_workspace(tmp_path):
    paths = Paths(tmp_path)
    middleware = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=True)
    middleware._paths = paths

    token = set_current_workspace(SimpleNamespace(id="ws-alpha", role="owner"))
    try:
        out = middleware.before_agent({"messages": []}, _FakeRuntime())
    finally:
        reset_current_workspace(token)

    expected_root = tmp_path / "workspaces" / "ws-alpha" / "threads" / "t1" / "user-data"
    assert out["thread_data"]["workspace_path"] == str(expected_root / "workspace")
    assert out["thread_data"]["uploads_path"] == str(expected_root / "uploads")
    assert out["thread_data"]["outputs_path"] == str(expected_root / "outputs")
    assert out["thread_data"]["workspace_id"] == "ws-alpha"


@pytest.mark.no_auto_workspace
def test_falls_back_to_default_workspace(tmp_path):
    """Without a workspace contextvar, `get_effective_workspace_id` returns 'default'."""
    paths = Paths(tmp_path)
    middleware = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=True)
    middleware._paths = paths

    out = middleware.before_agent({"messages": []}, _FakeRuntime())

    expected_root = tmp_path / "workspaces" / "default" / "threads" / "t1" / "user-data"
    assert out["thread_data"]["workspace_path"] == str(expected_root / "workspace")
    assert out["thread_data"]["workspace_id"] == "default"


def test_eager_creates_directories_under_workspace(tmp_path):
    paths = Paths(tmp_path)
    middleware = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=False)
    middleware._paths = paths

    token = set_current_workspace(SimpleNamespace(id="ws-beta", role="owner"))
    try:
        middleware.before_agent({"messages": []}, _FakeRuntime(thread_id="t2"))
    finally:
        reset_current_workspace(token)

    root = tmp_path / "workspaces" / "ws-beta" / "threads" / "t2" / "user-data"
    assert (root / "workspace").is_dir()
    assert (root / "uploads").is_dir()
    assert (root / "outputs").is_dir()


def test_get_config_fallback_still_workspace_scoped(tmp_path):
    """Thread_id resolution via LangGraph config still routes through workspace."""
    paths = Paths(tmp_path)
    middleware = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=True)
    middleware._paths = paths

    class _Runtime:
        context: dict = {}

    with patch("deerflow.agents.middlewares.thread_data_middleware.get_config", return_value={"configurable": {"thread_id": "t-cfg"}}):
        token = set_current_workspace(SimpleNamespace(id="ws-gamma", role="owner"))
        try:
            out = middleware.before_agent({"messages": []}, _Runtime())
        finally:
            reset_current_workspace(token)

    assert "workspaces/ws-gamma/threads/t-cfg/user-data/workspace" in out["thread_data"]["workspace_path"]
