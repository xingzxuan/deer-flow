"""Test configuration for the backend test suite.

Sets up sys.path and pre-mocks modules that would cause circular import
issues when unit-testing lightweight config/registry code in isolation.
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# Make 'app' and 'deerflow' importable from any working directory
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
# Make 'fixtures.*' importable as plugin modules from this conftest.
sys.path.insert(0, str(Path(__file__).parent))

# Register fixture plugin modules so tests can request fixtures by name
# without ad-hoc imports. ``fixtures.postgres`` provides
# ``postgres_container`` (session-scoped) and ``postgres_url`` (per-test).
pytest_plugins = ["fixtures.postgres"]

# Break the circular import chain that exists in production code:
#   deerflow.subagents.__init__
#     -> .executor (SubagentExecutor, SubagentResult)
#       -> deerflow.agents.thread_state
#         -> deerflow.agents.__init__
#           -> lead_agent.agent
#             -> subagent_limit_middleware
#               -> deerflow.subagents.executor  <-- circular!
#
# By injecting a mock for deerflow.subagents.executor *before* any test module
# triggers the import, __init__.py's "from .executor import ..." succeeds
# immediately without running the real executor module.
_executor_mock = MagicMock()
_executor_mock.SubagentExecutor = MagicMock
_executor_mock.SubagentResult = MagicMock
_executor_mock.SubagentStatus = MagicMock
_executor_mock.MAX_CONCURRENT_SUBAGENTS = 3
_executor_mock.get_background_task_result = MagicMock()

sys.modules["deerflow.subagents.executor"] = _executor_mock


# ---------------------------------------------------------------------------
# Auto-seed test workspace + user when Base.metadata.create_all() runs
# ---------------------------------------------------------------------------
#
# PR6 makes every business-row INSERT carry ``workspace_id`` (resolved
# from the autouse workspace contextvar = "test-workspace-autouse"). The
# Stage 0 schema has a NOT NULL FK from those rows to ``workspaces`` and
# from ``workspaces.owner_id`` to ``users``. Without the seed below,
# every legacy repo test would fail with a FOREIGN KEY error the moment
# it tries to insert a thread.
#
# We register an ``after_create`` hook on ``Base.metadata`` so that
# whenever ``init_engine`` finishes ``create_all()`` (the auto-create
# path used by tests and dev), the two anchor rows are present. Alembic
# migration tests don't trigger create_all so they are unaffected and
# keep exercising real FK constraints in isolation.


def _register_test_seed_listener() -> None:
    """Attach an after_create hook that seeds the autouse user + workspace."""
    try:
        from sqlalchemy import event, update
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        from deerflow.persistence.base import Base
        from deerflow.persistence.user.model import UserRow
        from deerflow.persistence.workspace.model import WorkspaceRow
    except ImportError:
        return

    from datetime import UTC, datetime

    def _seed(_target, connection, **kw):  # noqa: ARG001
        tables = {t.name for t in kw.get("tables", []) or []}
        if "users" not in tables or "workspaces" not in tables:
            return

        dialect = connection.dialect.name
        now = datetime.now(UTC)

        # Seed both rows with a consistent ``default_workspace_id`` so the
        # PR5 backfill script (which scans ``users.default_workspace_id IS
        # NULL``) does not pick up the test fixtures as candidates.
        # Insert user first with NULL default_workspace_id (chicken-and-egg
        # with workspaces.owner_id FK), then workspace, then UPDATE the user
        # row to point at the workspace so the PR5 backfill script does not
        # pick up the autouse user as a candidate.
        user_values = {
            "id": "test-user-autouse",
            "email": "test-user-autouse@local",
            "password_hash": None,
            "system_role": "user",
            "created_at": now,
            "oauth_provider": None,
            "oauth_id": None,
            "needs_setup": False,
            "token_version": 0,
            "default_workspace_id": None,
        }
        workspace_values = {
            "id": "test-workspace-autouse",
            "name": "Autouse Test Workspace",
            "slug": "autouse-test",
            "status": "active",
            "owner_id": "test-user-autouse",
            "created_at": now,
            "updated_at": now,
        }

        if dialect == "sqlite":
            user_stmt = sqlite_insert(UserRow.__table__).values(**user_values).on_conflict_do_nothing(index_elements=["id"])
            ws_stmt = sqlite_insert(WorkspaceRow.__table__).values(**workspace_values).on_conflict_do_nothing(index_elements=["id"])
        elif dialect == "postgresql":
            user_stmt = pg_insert(UserRow.__table__).values(**user_values).on_conflict_do_nothing(index_elements=["id"])
            ws_stmt = pg_insert(WorkspaceRow.__table__).values(**workspace_values).on_conflict_do_nothing(index_elements=["id"])
        else:
            return

        connection.execute(user_stmt)
        connection.execute(ws_stmt)
        connection.execute(update(UserRow.__table__).where(UserRow.__table__.c.id == "test-user-autouse").where(UserRow.__table__.c.default_workspace_id.is_(None)).values(default_workspace_id="test-workspace-autouse"))

    event.listen(Base.metadata, "after_create", _seed)


_register_test_seed_listener()


@pytest.fixture()
def provisioner_module():
    """Load docker/provisioner/app.py as an importable test module.

    Shared by test_provisioner_kubeconfig and test_provisioner_pvc_volumes so
    that any change to the provisioner entry-point path or module name only
    needs to be updated in one place.
    """
    repo_root = Path(__file__).resolve().parents[2]
    module_path = repo_root / "docker" / "provisioner" / "app.py"
    spec = importlib.util.spec_from_file_location("provisioner_app_test", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Auto-set user context for every test unless marked no_auto_user
# ---------------------------------------------------------------------------
#
# Repository methods read ``user_id`` from a contextvar by default
# (see ``deerflow.runtime.user_context``). Without this fixture, every
# pre-existing persistence test would raise RuntimeError because the
# contextvar is unset. The fixture sets a default test user on every
# test; tests that explicitly want to verify behaviour *without* a user
# context should mark themselves ``@pytest.mark.no_auto_user``.


@pytest.fixture(autouse=True)
def _reset_skill_storage_singleton():
    """Reset the SkillStorage singleton between tests to prevent cross-test contamination."""
    try:
        from deerflow.skills.storage import reset_skill_storage
    except ImportError:
        yield
        return
    reset_skill_storage()
    try:
        yield
    finally:
        reset_skill_storage()


@pytest.fixture(autouse=True)
def _auto_user_context(request):
    """Inject a default ``test-user-autouse`` into the contextvar.

    Opt-out via ``@pytest.mark.no_auto_user``. Uses lazy import so that
    tests which don't touch the persistence layer never pay the cost
    of importing runtime.user_context.
    """
    if request.node.get_closest_marker("no_auto_user"):
        yield
        return

    try:
        from deerflow.runtime.user_context import (
            reset_current_user,
            set_current_user,
        )
    except ImportError:
        yield
        return

    user = SimpleNamespace(id="test-user-autouse", email="test@local")
    token = set_current_user(user)
    try:
        yield
    finally:
        reset_current_user(token)


@pytest.fixture(autouse=True)
def _auto_workspace_context(request):
    """Inject a default ``test-workspace-autouse`` into the workspace contextvar.

    Mirror of :func:`_auto_user_context`. PR6 adds ``workspace_id=AUTO``
    sentinels to every repository method; without an autouse workspace
    fixture every legacy persistence test would raise RuntimeError.

    Opt-out via ``@pytest.mark.no_auto_workspace``.
    """
    if request.node.get_closest_marker("no_auto_workspace"):
        yield
        return

    try:
        from deerflow.runtime.workspace_context import (
            reset_current_workspace,
            set_current_workspace,
        )
    except ImportError:
        yield
        return

    workspace = SimpleNamespace(id="test-workspace-autouse", role="owner")
    token = set_current_workspace(workspace)
    try:
        yield
    finally:
        reset_current_workspace(token)
