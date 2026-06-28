"""Deprecation header tests (Stage 1 PR5)."""

from __future__ import annotations

from starlette.testclient import TestClient


def _make_app():
    from fastapi import FastAPI

    from app.gateway.deprecation_middleware import ApiDeprecationMiddleware

    app = FastAPI()
    app.add_middleware(ApiDeprecationMiddleware)

    @app.get("/api/threads")
    async def legacy():
        return {"ok": True}

    @app.get("/api/v1/threads")
    async def versioned():
        return {"ok": True}

    @app.get("/api/langgraph/info")
    async def lg():
        return {"ok": True}

    return app


def test_legacy_path_gets_deprecation_header():
    client = TestClient(_make_app())
    r = client.get("/api/threads")
    assert r.headers.get("X-API-Deprecated") == "2027-01-01"


def test_versioned_path_no_header():
    client = TestClient(_make_app())
    r = client.get("/api/v1/threads")
    assert "X-API-Deprecated" not in r.headers


def test_langgraph_path_no_header():
    client = TestClient(_make_app())
    r = client.get("/api/langgraph/info")
    assert "X-API-Deprecated" not in r.headers
