"""CSRF bearer-skip tests (Stage 1 PR3)."""

from __future__ import annotations

from starlette.testclient import TestClient


def _make_app():
    from fastapi import FastAPI

    from app.gateway.csrf_middleware import CSRFMiddleware

    app = FastAPI()
    app.add_middleware(CSRFMiddleware)

    @app.post("/api/echo")
    async def echo():
        return {"ok": True}

    return app


def test_bearer_post_skips_csrf():
    client = TestClient(_make_app())
    # No X-CSRF-Token / csrf cookie, but bearer header present → allowed.
    r = client.post("/api/echo", headers={"Authorization": "Bearer dfk_live_anything"})
    assert r.status_code == 200


def test_cookie_post_still_requires_csrf():
    client = TestClient(_make_app())
    # No bearer, no CSRF token → 403 (regression: cookie path unchanged).
    r = client.post("/api/echo")
    assert r.status_code == 403
    assert "CSRF token missing" in r.json()["detail"]


def test_has_bearer_header_detection():
    from starlette.requests import Request

    from app.gateway.csrf_middleware import has_bearer_header

    def _req(headers):
        scope = {"type": "http", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
        return Request(scope)

    assert has_bearer_header(_req({"authorization": "Bearer x"})) is True
    assert has_bearer_header(_req({"authorization": "Basic x"})) is False
    assert has_bearer_header(_req({})) is False
