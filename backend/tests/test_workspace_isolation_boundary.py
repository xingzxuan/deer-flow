"""PR6 T6.8 — cross-workspace isolation boundary (e2e).

Wires a ``MemoryThreadMetaStore`` (LangGraph BaseStore backed) into a
stub-authed FastAPI app and asserts that any request from workspace B
against a thread created in workspace A returns **404**, regardless of
matching user_id. The cross-workspace block fires in
``ThreadMetaStore.check_access`` and is converted to 404 by
``@require_permission(owner_check=True)``.

We use the memory-backed implementation so the test stays in the test
event loop end-to-end (the SQL engine binds to whatever loop owns
``init_engine`` and the TestClient spins its own loop, which would
collide). The decorator path it exercises is the same as production;
the SQL repository's identical workspace filter is unit-covered by
``test_thread_meta_workspace_filter.py``.

Covers:
- ``GET    /api/threads/{tid}`` — read (require_existing=False)
- ``DELETE /api/threads/{tid}`` — destructive (require_existing=True)
- ``PATCH  /api/threads/{tid}`` — destructive write
- positive control: same-workspace GET still succeeds
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from uuid import uuid4

from _router_auth_helpers import make_authed_test_app
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from app.gateway.auth.models import ActiveWorkspace, User
from app.gateway.routers import threads
from deerflow.persistence.thread_meta.memory import MemoryThreadMetaStore
from deerflow.runtime.workspace_context import (
    reset_current_workspace,
    set_current_workspace,
)


def _workspace_factory(wid: str) -> Callable[[], ActiveWorkspace]:
    def _factory() -> ActiveWorkspace:
        return ActiveWorkspace(id=wid, role="owner")

    return _factory


def _user_factory(uid: str) -> Callable[[], User]:
    def _factory() -> User:
        return User(email=f"{uid}@example.com", password_hash="x", system_role="user", id=uid)

    return _factory


def _seed_thread(store, *, thread_id: str, user_id: str, workspace_id: str) -> None:
    """Insert a thread record under a specific workspace, bypassing the autouse fixture."""
    import asyncio

    async def _go():
        meta_store = MemoryThreadMetaStore(store)
        token = set_current_workspace(SimpleNamespace(id=workspace_id, role="owner"))
        try:
            await meta_store.create(thread_id, user_id=user_id)
        finally:
            reset_current_workspace(token)

    asyncio.run(_go())


def _build_app(*, user_id: str, workspace_id: str):
    app = make_authed_test_app(
        user_factory=_user_factory(user_id),
        workspace_factory=_workspace_factory(workspace_id),
        override_user_contextvar=True,
    )
    store = InMemoryStore()
    app.state.store = store
    app.state.checkpointer = InMemorySaver()
    app.state.thread_store = MemoryThreadMetaStore(store)
    app.include_router(threads.router, prefix="/api")
    return app, store


def test_cross_workspace_get_returns_404():
    user_id = str(uuid4())
    app, store = _build_app(user_id=user_id, workspace_id="ws-beta")
    _seed_thread(store, thread_id="t1", user_id=user_id, workspace_id="ws-alpha")

    with TestClient(app) as client:
        response = client.get("/api/threads/t1")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_cross_workspace_delete_returns_404():
    user_id = str(uuid4())
    app, store = _build_app(user_id=user_id, workspace_id="ws-beta")
    _seed_thread(store, thread_id="t1", user_id=user_id, workspace_id="ws-alpha")

    with TestClient(app) as client:
        response = client.delete("/api/threads/t1")
    assert response.status_code == 404


def test_cross_workspace_patch_returns_404():
    user_id = str(uuid4())
    app, store = _build_app(user_id=user_id, workspace_id="ws-beta")
    _seed_thread(store, thread_id="t1", user_id=user_id, workspace_id="ws-alpha")

    with TestClient(app) as client:
        response = client.patch("/api/threads/t1", json={"metadata": {"k": "v"}})
    assert response.status_code == 404


def test_same_workspace_get_succeeds():
    """Positive control: when the workspace matches, the row is returned."""
    user_id = str(uuid4())
    app, store = _build_app(user_id=user_id, workspace_id="ws-alpha")
    _seed_thread(store, thread_id="t1", user_id=user_id, workspace_id="ws-alpha")

    with TestClient(app) as client:
        response = client.get("/api/threads/t1")
    assert response.status_code == 200
    assert response.json()["thread_id"] == "t1"
