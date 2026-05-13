"""PR6 T6.7 — POST /api/threads writes workspace_id from contextvar.

The `routers/threads.py:create_thread` path delegates to
``ThreadMetaStore.create`` *without* an explicit ``workspace_id`` — it
relies on the AUTO sentinel pulling the value from the active workspace
contextvar that AuthMiddleware (or the test stub) sets. This test
covers the integration through the FastAPI TestClient stack: post a
thread under workspace A, then verify the persisted record carries
``workspace_id="ws-alpha"``.
"""

from __future__ import annotations

from collections.abc import Callable

from _router_auth_helpers import make_authed_test_app
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from app.gateway.auth.models import ActiveWorkspace
from app.gateway.routers import threads
from deerflow.persistence.thread_meta.memory import MemoryThreadMetaStore


def _workspace_factory(wid: str) -> Callable[[], ActiveWorkspace]:
    def _factory() -> ActiveWorkspace:
        return ActiveWorkspace(id=wid, role="owner")

    return _factory


def _build_app(workspace_id: str):
    app = make_authed_test_app(workspace_factory=_workspace_factory(workspace_id))
    store = InMemoryStore()
    checkpointer = InMemorySaver()
    app.state.store = store
    app.state.checkpointer = checkpointer
    app.state.thread_store = MemoryThreadMetaStore(store)
    app.include_router(threads.router)
    return app, store


def test_post_thread_stamps_workspace_id_from_contextvar():
    app, store = _build_app("ws-alpha")
    with TestClient(app) as client:
        response = client.post("/api/threads", json={"thread_id": "t1", "metadata": {}})
    assert response.status_code == 200, response.text

    item = store.get(("threads",), "t1")
    assert item is not None
    assert item.value["workspace_id"] == "ws-alpha"


def test_post_thread_under_different_workspace():
    app, store = _build_app("ws-beta")
    with TestClient(app) as client:
        client.post("/api/threads", json={"thread_id": "t2", "metadata": {}})
    assert store.get(("threads",), "t2").value["workspace_id"] == "ws-beta"
