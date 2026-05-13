"""PR6 T6.9 / T6.10 — workspace-scoped path resolution.

The Paths class learns a new top-level dimension for multi-tenant
filesystems: ``{base_dir}/workspaces/{wid}/...``. Precedence:

- ``workspace_id`` given → new shape ``workspaces/{wid}/threads/{tid}/...``
- only ``user_id`` given → legacy shape ``users/{uid}/threads/{tid}/...``
- neither → very-legacy shape ``threads/{tid}/...``

Per-user filesystem state (memory.json, custom agents) lives under the
workspace too: ``workspaces/{wid}/users/{uid}/memory.json`` etc. — so a
user's memory cannot be reused across workspaces by mistake.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deerflow.config.paths import Paths


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(tmp_path)


class TestValidateWorkspaceId:
    def test_valid_workspace_id(self, paths: Paths):
        d = paths.workspace_dir("ws-abc-123")
        assert d == paths.base_dir / "workspaces" / "ws-abc-123"

    def test_rejects_path_traversal(self, paths: Paths):
        with pytest.raises(ValueError, match="Invalid workspace_id"):
            paths.workspace_dir("../escape")

    def test_rejects_slash(self, paths: Paths):
        with pytest.raises(ValueError, match="Invalid workspace_id"):
            paths.workspace_dir("ws/bar")

    def test_rejects_empty(self, paths: Paths):
        with pytest.raises(ValueError, match="Invalid workspace_id"):
            paths.workspace_dir("")


class TestWorkspaceScopedThreadDir:
    def test_workspace_takes_precedence_over_user(self, paths: Paths):
        """When both are given, workspace wins — user_id is recorded in the row, not the filesystem."""
        expected = paths.base_dir / "workspaces" / "ws-alpha" / "threads" / "t1"
        assert paths.thread_dir("t1", workspace_id="ws-alpha", user_id="alice") == expected

    def test_workspace_only(self, paths: Paths):
        expected = paths.base_dir / "workspaces" / "ws-alpha" / "threads" / "t1"
        assert paths.thread_dir("t1", workspace_id="ws-alpha") == expected

    def test_user_only_still_legacy(self, paths: Paths):
        """Legacy callers keep the user_id-only shape until migration runs."""
        expected = paths.base_dir / "users" / "alice" / "threads" / "t1"
        assert paths.thread_dir("t1", user_id="alice") == expected

    def test_no_ids_very_legacy(self, paths: Paths):
        expected = paths.base_dir / "threads" / "t1"
        assert paths.thread_dir("t1") == expected


class TestEnsureThreadDirsWorkspace:
    def test_creates_workspace_layout(self, paths: Paths):
        paths.ensure_thread_dirs("t1", workspace_id="ws-alpha")
        root = paths.base_dir / "workspaces" / "ws-alpha" / "threads" / "t1"
        for sub in ("user-data/workspace", "user-data/uploads", "user-data/outputs", "acp-workspace"):
            assert (root / sub).is_dir(), f"missing {sub}"


class TestSandboxDirsWorkspace:
    def test_sandbox_work_dir(self, paths: Paths):
        expected = paths.base_dir / "workspaces" / "ws-alpha" / "threads" / "t1" / "user-data" / "workspace"
        assert paths.sandbox_work_dir("t1", workspace_id="ws-alpha") == expected

    def test_sandbox_uploads_dir(self, paths: Paths):
        expected = paths.base_dir / "workspaces" / "ws-alpha" / "threads" / "t1" / "user-data" / "uploads"
        assert paths.sandbox_uploads_dir("t1", workspace_id="ws-alpha") == expected

    def test_sandbox_outputs_dir(self, paths: Paths):
        expected = paths.base_dir / "workspaces" / "ws-alpha" / "threads" / "t1" / "user-data" / "outputs"
        assert paths.sandbox_outputs_dir("t1", workspace_id="ws-alpha") == expected


class TestUserMemoryUnderWorkspace:
    def test_user_memory_file_under_workspace(self, paths: Paths):
        expected = paths.base_dir / "workspaces" / "ws-alpha" / "users" / "alice" / "memory.json"
        assert paths.user_memory_file("alice", workspace_id="ws-alpha") == expected

    def test_user_memory_file_legacy_without_workspace(self, paths: Paths):
        expected = paths.base_dir / "users" / "alice" / "memory.json"
        assert paths.user_memory_file("alice") == expected

    def test_user_agents_dir_under_workspace(self, paths: Paths):
        expected = paths.base_dir / "workspaces" / "ws-alpha" / "users" / "alice" / "agents"
        assert paths.user_agents_dir("alice", workspace_id="ws-alpha") == expected

    def test_user_agent_memory_file_under_workspace(self, paths: Paths):
        expected = paths.base_dir / "workspaces" / "ws-alpha" / "users" / "alice" / "agents" / "code-reviewer" / "memory.json"
        assert paths.user_agent_memory_file("alice", "code-reviewer", workspace_id="ws-alpha") == expected


class TestVirtualPathResolutionWorkspace:
    def test_resolve_virtual_path_workspace_scope(self, paths: Paths):
        expected = paths.base_dir / "workspaces" / "ws-alpha" / "threads" / "t1" / "user-data" / "outputs" / "x.json"
        actual = paths.resolve_virtual_path("t1", "/mnt/user-data/outputs/x.json", workspace_id="ws-alpha")
        assert actual == expected.resolve()

    def test_resolve_virtual_path_rejects_traversal_under_workspace(self, paths: Paths):
        with pytest.raises(ValueError, match="path traversal"):
            paths.resolve_virtual_path("t1", "/mnt/user-data/../../etc/passwd", workspace_id="ws-alpha")
