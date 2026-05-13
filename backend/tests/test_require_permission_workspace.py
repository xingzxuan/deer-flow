"""PR6 T6.6 — `@require_permission(owner_check=True)` workspace upgrade.

The decorator must:

1. Pull workspace_id from ``get_effective_workspace_id()`` (set by
   AuthMiddleware per request) and pass it as the third positional to
   ``ThreadMetaStore.check_access``.
2. Raise **HTTPException 404** when check_access returns False — never
   403 — so a cross-workspace request cannot distinguish "thread exists
   in another tenant" from "thread does not exist".

These tests build a fake router with the same decorator usage as the
production code and verify the decorator's behaviour via a Mock
``thread_store`` whose ``check_access`` call we inspect, plus a TestClient
exercising the full HTTP boundary.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from _router_auth_helpers import make_authed_test_app
from fastapi import APIRouter, Request
from fastapi.testclient import TestClient

from app.gateway.auth.models import ActiveWorkspace
from app.gateway.authz import require_permission
from deerflow.runtime.workspace_context import (
    reset_current_workspace,
    set_current_workspace,
)


def _make_workspace(wid: str) -> Callable[[], ActiveWorkspace]:
    """Factory closure stable per call so the middleware reinjects the same id."""

    def _factory() -> ActiveWorkspace:
        return ActiveWorkspace(id=wid, role="owner")

    return _factory


def _mount_routes(app):
    router = APIRouter()

    @router.delete("/probe/{thread_id}")
    @require_permission("threads", "delete", owner_check=True, require_existing=True)
    async def _delete_probe(thread_id: str, request: Request):  # noqa: ARG001
        return {"ok": True, "thread_id": thread_id}

    @router.get("/probe/{thread_id}")
    @require_permission("threads", "read", owner_check=True)
    async def _get_probe(thread_id: str, request: Request):  # noqa: ARG001
        return {"ok": True, "thread_id": thread_id}

    app.include_router(router)
    return app


def test_cross_workspace_returns_404():
    """check_access returning False surfaces as 404, never 403."""
    app = make_authed_test_app(
        workspace_factory=_make_workspace("ws-alpha"),
        owner_check_passes=False,
    )
    _mount_routes(app)
    with TestClient(app) as client:
        response = client.delete("/probe/t1")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_same_workspace_delete_allowed():
    """check_access returning True lets the route execute."""
    app = make_authed_test_app(
        workspace_factory=_make_workspace("ws-alpha"),
        owner_check_passes=True,
    )
    _mount_routes(app)
    with TestClient(app) as client:
        response = client.delete("/probe/t1")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_workspace_id_passed_to_check_access():
    """check_access receives the contextvar workspace_id as the 3rd positional."""
    app = make_authed_test_app(
        workspace_factory=_make_workspace("ws-alpha"),
        owner_check_passes=True,
    )
    _mount_routes(app)
    with TestClient(app) as client:
        client.delete("/probe/t1")

    call = app.state.thread_store.check_access.call_args
    assert call is not None
    args = call.args
    # (thread_id, user_id, workspace_id)
    assert args[0] == "t1"
    assert args[2] == "ws-alpha"


@pytest.mark.no_auto_workspace
def test_no_workspace_in_context_falls_back_to_default():
    """No-auth dev mode (no workspace contextvar) uses DEFAULT_WORKSPACE_ID."""
    app = make_authed_test_app(workspace_factory=None, owner_check_passes=True)
    _mount_routes(app)
    with TestClient(app) as client:
        client.delete("/probe/t1")

    args = app.state.thread_store.check_access.call_args.args
    assert args[2] == "default"


def test_get_route_also_uses_workspace_id():
    """Read-style routes (require_existing=False) also pass workspace_id through."""
    app = make_authed_test_app(
        workspace_factory=_make_workspace("ws-beta"),
        owner_check_passes=True,
    )
    _mount_routes(app)
    with TestClient(app) as client:
        client.get("/probe/t-read")
    args = app.state.thread_store.check_access.call_args.args
    assert args[2] == "ws-beta"


@pytest.fixture
def _reset_ws():
    """Helper for direct-call paths that mutate the contextvar."""
    tokens: list = []
    yield lambda wid: tokens.append(set_current_workspace(ActiveWorkspace(id=wid, role="owner")))
    for token in reversed(tokens):
        reset_current_workspace(token)
