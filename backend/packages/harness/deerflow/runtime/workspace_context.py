"""Request-scoped workspace context for multi-tenant authorization.

Sibling of :mod:`deerflow.runtime.user_context`. Holds a
:class:`~contextvars.ContextVar` that the gateway's auth middleware sets
after JWT verification (PR4 will wire this up). Repository methods read
the contextvar via a sentinel default parameter, letting routers stay
free of ``workspace_id`` boilerplate.

Three-state semantics for the repository ``workspace_id`` parameter:

- ``AUTO`` (sentinel, default): read from contextvar; raise
  :class:`RuntimeError` if unset.
- Explicit ``str``: use the provided value, overriding contextvar.
- Explicit ``None``: no WHERE clause — used only by migration scripts
  and admin CLIs that intentionally bypass workspace isolation.

Concept boundary
----------------
A workspace is the multi-tenant scope: a single-user free account is
its own 1-person workspace; a team subscription is a multi-member
workspace. The user_id contextvar narrows further to "which member of
the workspace", letting some operations be member-scoped while others
(skill install, billing, etc.) are workspace-scoped.

Dependency direction
--------------------
``persistence`` (lower layer) reads from this module; ``gateway.auth``
(higher layer) writes to it. ``CurrentWorkspace`` is defined here as a
:class:`typing.Protocol` so that ``persistence`` never needs to import
the concrete ``Workspace`` row class from ``deerflow.persistence.workspace``.
Any object with ``.id: str`` and ``.role: str`` attributes structurally
satisfies the protocol.

Asyncio semantics
-----------------
Identical to ``user_context``: ``ContextVar`` is task-local under asyncio.
``asyncio.create_task`` inherits the parent task's workspace context;
threading.Timer does **not** (callers spawning timers must capture
``get_effective_workspace_id()`` at enqueue time, the same way
:mod:`deerflow.agents.memory.queue` captures ``user_id``).
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Final, Protocol, runtime_checkable


@runtime_checkable
class CurrentWorkspace(Protocol):
    """Structural type for the current active workspace.

    Any object with ``.id: str`` and ``.role: str`` attributes satisfies
    this protocol. Concrete implementations live in
    ``app.gateway.auth.models`` (PR4 will add them).

    ``role`` is the *caller's* role within this workspace
    (``'owner'`` / ``'admin'`` / ``'member'``), not the workspace's
    own metadata. Stage 0 sees ``'owner'`` only — Stage 2 RBAC rollout
    opens up the other values.
    """

    id: str
    role: str


_current_workspace: Final[ContextVar[CurrentWorkspace | None]] = ContextVar("deerflow_current_workspace", default=None)


def set_current_workspace(workspace: CurrentWorkspace) -> Token[CurrentWorkspace | None]:
    """Set the current workspace for this async task.

    Returns a reset token that should be passed to
    :func:`reset_current_workspace` in a ``finally`` block to restore
    the previous context.
    """
    return _current_workspace.set(workspace)


def reset_current_workspace(token: Token[CurrentWorkspace | None]) -> None:
    """Restore the context to the state captured by ``token``."""
    _current_workspace.reset(token)


def get_current_workspace() -> CurrentWorkspace | None:
    """Return the current workspace, or ``None`` if unset.

    Safe to call in any context. Used by code paths that can proceed
    without a workspace (migration scripts, public endpoints).
    """
    return _current_workspace.get()


def require_current_workspace() -> CurrentWorkspace:
    """Return the current workspace, or raise :class:`RuntimeError`.

    Used by repository code that must not be called outside a
    request-authenticated context. The error message is phrased so
    that a caller debugging a stack trace can locate the offending
    code path.
    """
    workspace = _current_workspace.get()
    if workspace is None:
        raise RuntimeError("repository accessed without workspace context")
    return workspace


# ---------------------------------------------------------------------------
# Effective workspace_id helpers (filesystem isolation)
# ---------------------------------------------------------------------------

DEFAULT_WORKSPACE_ID: Final[str] = "default"


def get_effective_workspace_id() -> str:
    """Return the current workspace id as a string, or DEFAULT_WORKSPACE_ID if unset.

    Unlike :func:`require_current_workspace` this never raises — it is
    designed for filesystem-path resolution where a valid workspace
    bucket is always needed (PR6 will switch
    ``Paths.thread_dir(workspace_id=...)`` to read from here).
    """
    workspace = _current_workspace.get()
    if workspace is None:
        return DEFAULT_WORKSPACE_ID
    return str(workspace.id)


# ---------------------------------------------------------------------------
# Sentinel-based workspace_id resolution
# ---------------------------------------------------------------------------
#
# Repository methods accept a ``workspace_id`` keyword-only argument that
# defaults to ``AUTO``. The three possible values drive distinct
# behaviours; see the docstring on :func:`resolve_workspace_id`.


class _AutoSentinel:
    """Singleton marker meaning 'resolve workspace_id from contextvar'."""

    _instance: _AutoSentinel | None = None

    def __new__(cls) -> _AutoSentinel:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<AUTO>"


AUTO: Final[_AutoSentinel] = _AutoSentinel()


def resolve_workspace_id(
    value: str | None | _AutoSentinel,
    *,
    method_name: str = "repository method",
) -> str | None:
    """Resolve the workspace_id parameter passed to a repository method.

    Three-state semantics:

    - :data:`AUTO` (default): read from contextvar; raise
      :class:`RuntimeError` if no workspace is in context. This is the
      common case for request-scoped calls.
    - Explicit ``str``: use the provided id verbatim, overriding any
      contextvar value. Useful for tests and admin-override flows.
    - Explicit ``None``: no filter — the repository should skip the
      workspace_id WHERE clause entirely. Reserved for migration scripts
      and CLI tools that intentionally bypass workspace isolation.
    """
    if isinstance(value, _AutoSentinel):
        workspace = _current_workspace.get()
        if workspace is None:
            raise RuntimeError(
                f"{method_name} called with workspace_id=AUTO but no workspace context is set; pass an explicit workspace_id, set the contextvar via auth middleware, or opt out with workspace_id=None for migration/CLI paths."
            )
        # Coerce to ``str`` at the boundary; persistence stores
        # ``workspace_id`` as ``String(36)`` (UUID v4 text).
        return str(workspace.id)
    return value
