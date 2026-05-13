"""Tests for runtime.workspace_context — workspace contextvar semantics.

Mirrors :mod:`test_user_context` but for the workspace contextvar
introduced in Stage 0 PR3. No autouse workspace fixture exists yet
(PR4 will add it together with the AuthMiddleware injection), so these
tests run against a clean contextvar.
"""

import uuid
from types import SimpleNamespace

import pytest

from deerflow.runtime.workspace_context import (
    AUTO,
    DEFAULT_WORKSPACE_ID,
    CurrentWorkspace,
    get_current_workspace,
    get_effective_workspace_id,
    require_current_workspace,
    reset_current_workspace,
    resolve_workspace_id,
    set_current_workspace,
)

# ---------------------------------------------------------------------------
# get_current_workspace / require_current_workspace / set+reset round-trip
# ---------------------------------------------------------------------------


@pytest.mark.no_auto_workspace
def test_default_is_none():
    """Before any set, contextvar returns None."""
    assert get_current_workspace() is None


@pytest.mark.no_auto_workspace
def test_set_and_reset_roundtrip():
    """set_current_workspace returns a token that reset restores."""
    workspace = SimpleNamespace(id="ws-1", role="owner")
    token = set_current_workspace(workspace)
    try:
        assert get_current_workspace() is workspace
    finally:
        reset_current_workspace(token)
    assert get_current_workspace() is None


@pytest.mark.no_auto_workspace
def test_require_current_workspace_raises_when_unset():
    """require_current_workspace raises RuntimeError if contextvar is unset."""
    assert get_current_workspace() is None
    with pytest.raises(RuntimeError, match="without workspace context"):
        require_current_workspace()


def test_require_current_workspace_returns_workspace_when_set():
    """require_current_workspace returns the workspace when contextvar is set."""
    workspace = SimpleNamespace(id="ws-2", role="admin")
    token = set_current_workspace(workspace)
    try:
        assert require_current_workspace() is workspace
    finally:
        reset_current_workspace(token)


# ---------------------------------------------------------------------------
# CurrentWorkspace Protocol — must require BOTH .id and .role
# ---------------------------------------------------------------------------


def test_protocol_accepts_id_and_role():
    """CurrentWorkspace is satisfied by any object with .id and .role."""
    workspace = SimpleNamespace(id="ws-3", role="member")
    assert isinstance(workspace, CurrentWorkspace)


def test_protocol_rejects_missing_role():
    """An object with only .id (no .role) is NOT a workspace."""
    user_shaped = SimpleNamespace(id="ws-4")
    assert not isinstance(user_shaped, CurrentWorkspace)


def test_protocol_rejects_no_id():
    """An object without .id does not satisfy CurrentWorkspace."""
    not_a_workspace = SimpleNamespace(role="owner")
    assert not isinstance(not_a_workspace, CurrentWorkspace)


# ---------------------------------------------------------------------------
# get_effective_workspace_id / DEFAULT_WORKSPACE_ID tests
# ---------------------------------------------------------------------------


def test_default_workspace_id_is_default():
    assert DEFAULT_WORKSPACE_ID == "default"


@pytest.mark.no_auto_workspace
def test_effective_workspace_id_returns_default_when_no_workspace():
    """No workspace in context -> fallback to DEFAULT_WORKSPACE_ID."""
    assert get_effective_workspace_id() == "default"


def test_effective_workspace_id_returns_workspace_id_when_set():
    workspace = SimpleNamespace(id="ws-abc-123", role="owner")
    token = set_current_workspace(workspace)
    try:
        assert get_effective_workspace_id() == "ws-abc-123"
    finally:
        reset_current_workspace(token)


def test_effective_workspace_id_coerces_to_str():
    """workspace.id might be a UUID object; must come back as str."""
    wid = uuid.uuid4()
    workspace = SimpleNamespace(id=wid, role="owner")
    token = set_current_workspace(workspace)
    try:
        assert get_effective_workspace_id() == str(wid)
    finally:
        reset_current_workspace(token)


# ---------------------------------------------------------------------------
# resolve_workspace_id three-state semantics
# ---------------------------------------------------------------------------


def test_resolve_auto_reads_from_contextvar():
    workspace = SimpleNamespace(id="ws-resolve-1", role="owner")
    token = set_current_workspace(workspace)
    try:
        assert resolve_workspace_id(AUTO) == "ws-resolve-1"
    finally:
        reset_current_workspace(token)


@pytest.mark.no_auto_workspace
def test_resolve_auto_raises_when_unset():
    assert get_current_workspace() is None
    with pytest.raises(RuntimeError, match="workspace_id=AUTO but no workspace"):
        resolve_workspace_id(AUTO, method_name="TestRepo.search")


def test_resolve_explicit_str_overrides_contextvar():
    workspace = SimpleNamespace(id="ws-ctx", role="owner")
    token = set_current_workspace(workspace)
    try:
        # Explicit value beats contextvar — admin override / test path.
        assert resolve_workspace_id("ws-explicit") == "ws-explicit"
    finally:
        reset_current_workspace(token)


def test_resolve_explicit_none_means_no_filter():
    workspace = SimpleNamespace(id="ws-ctx-2", role="owner")
    token = set_current_workspace(workspace)
    try:
        # Explicit None opts out of workspace filtering (migration scripts).
        assert resolve_workspace_id(None) is None
    finally:
        reset_current_workspace(token)


def test_resolve_auto_coerces_uuid_to_str():
    """resolve_workspace_id with AUTO returns str even if workspace.id is UUID."""
    wid = uuid.uuid4()
    workspace = SimpleNamespace(id=wid, role="owner")
    token = set_current_workspace(workspace)
    try:
        resolved = resolve_workspace_id(AUTO)
        assert resolved == str(wid)
        assert isinstance(resolved, str)
    finally:
        reset_current_workspace(token)
