"""API key control-plane default-deny tests (Stage 1 收口).

service principal (API key) 只能访问数据平面 (threads/runs/assistants);
控制平面 (models/mcp/memory/skills/channels/agents 与管理/auth) 一律 403。
真人 cookie 路径不受影响。设计见 spec
docs/superpowers/specs/2026-06-28-api-key-control-plane-default-deny-design.md。
"""

from __future__ import annotations

import pytest
from fastapi import Request
from starlette.testclient import TestClient

from app.gateway.auth_middleware import _is_dataplane_path
from deerflow.auth.tokens import generate_api_key

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "path",
    [
        "/api/threads",
        "/api/threads/abc",
        "/api/v1/threads",
        "/api/v1/threads/abc/runs/xyz/feedback",
        "/api/runs",
        "/api/runs/stream",
        "/api/v1/runs/stream",
        "/api/assistants",
        "/api/assistants/search",
    ],
)
def test_dataplane_paths_allowed(path):
    assert _is_dataplane_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/api/models",
        "/api/v1/models",
        "/api/mcp/config",
        "/api/v1/mcp/config",
        "/api/v1/memory",
        "/api/v1/skills/install",
        "/api/v1/channels/restart",
        "/api/v1/agents",
        "/api/v1/service-accounts",
        "/api/v1/api-keys",
        "/api/v1/auth/me",
        "/api/v1/assistants",  # assistants 是 LangGraph 兼容 shim,无 /api/v1 孪生:只放行 /api/assistants,缺 v1 变体是有意为之
        "/api/langgraph/threads",  # nginx 死代码:中间件本看不到,真混进来也应 deny
        "/api/threads-export",  # boundary guard — prefix must end at a path segment
        "/api/runsX",  # boundary guard — prefix must end at a path segment
    ],
)
def test_control_plane_paths_denied(path):
    assert _is_dataplane_path(path) is False


# ---------------------------------------------------------------------------
# Integration tests: AuthMiddleware bearer default-deny (Task 3)
# ---------------------------------------------------------------------------


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _seed_key(tmp_path, *, scopes="threads:read"):
    from deerflow.persistence.api_key import ApiKeyRepository
    from deerflow.persistence.engine import get_session_factory, init_engine
    from deerflow.persistence.service_account.model import ServiceAccountRow
    from deerflow.persistence.user.model import UserRow
    from deerflow.persistence.workspace.model import WorkspaceRow

    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    sf = get_session_factory()
    async with sf() as session:
        session.add(UserRow(id="u-alice", email="alice@example.com"))
        await session.commit()
    async with sf() as session:
        session.add(WorkspaceRow(id="w-1", name="WS", slug="ws", owner_id="u-alice"))
        await session.commit()
    async with sf() as session:
        session.add(ServiceAccountRow(id="sa-1", workspace_id="w-1", name="bot", role="member", identity_mode="collapsed", status="active", created_by="u-alice"))
        await session.commit()
    repo = ApiKeyRepository(sf)
    gen = generate_api_key("live")
    await repo.create(service_account_id="sa-1", key_prefix=gen.prefix, key_hash=gen.key_hash, name="k", scopes=scopes)
    return gen


async def _cleanup():
    from deerflow.persistence.engine import close_engine

    await close_engine()


def _make_app():
    from fastapi import FastAPI

    from app.gateway.auth_middleware import AuthMiddleware
    from deerflow.runtime.user_context import get_effective_user_id

    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    # NOTE: Request must NOT be imported locally here. With `from __future__ import
    # annotations` active, local imports are invisible to get_type_hints, causing
    # FastAPI to treat `request: Request` as a query param → 422. Module-level
    # import (above) makes it resolvable. See test_auth_middleware_api_key.py docstring.
    @app.get("/api/v1/threads/_probe")
    async def threads_probe(request: Request):
        return {"user_id": get_effective_user_id()}

    @app.get("/api/assistants/search")
    async def assistants_probe():
        return {"ok": True}

    return app


async def test_sa_allowed_on_dataplane(tmp_path):
    gen = await _seed_key(tmp_path)
    try:
        client = TestClient(_make_app())
        r = client.get("/api/v1/threads/_probe", headers={"Authorization": f"Bearer {gen.plaintext}"})
        assert r.status_code == 200
        assert r.json() == {"user_id": "sa-1"}
    finally:
        await _cleanup()


async def test_sa_allowed_on_assistants_init(tmp_path):
    gen = await _seed_key(tmp_path)
    try:
        client = TestClient(_make_app())
        r = client.get("/api/assistants/search", headers={"Authorization": f"Bearer {gen.plaintext}"})
        assert r.status_code == 200
    finally:
        await _cleanup()


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/mcp/config",
        "/api/mcp/config",
        "/api/v1/models",
        "/api/v1/skills/install",
        "/api/v1/channels/restart",
        "/api/v1/agents",
        "/api/v1/memory",
        "/api/v1/service-accounts",
    ],
)
async def test_sa_denied_on_control_plane(tmp_path, path):
    gen = await _seed_key(tmp_path)
    try:
        client = TestClient(_make_app())
        r = client.get(path, headers={"Authorization": f"Bearer {gen.plaintext}"})
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "insufficient_scope"
    finally:
        await _cleanup()


async def test_invalid_key_still_401_not_403(tmp_path):
    # 无效 key 命中控制平面路径,应是 401 (TOKEN_INVALID),不是 403 ——
    # deny 检查在 None 校验之后。
    await _seed_key(tmp_path)
    try:
        client = TestClient(_make_app())
        r = client.get("/api/v1/mcp/config", headers={"Authorization": "Bearer dfk_live_bogus00000000000000000"})
        assert r.status_code == 401
    finally:
        await _cleanup()


async def test_cookie_path_unaffected_by_deny(tmp_path):
    # 非 bearer-dfk 请求不进 bearer 分支:控制平面路径走 cookie 路径,
    # 无 cookie → 401 not_authenticated,绝不会拿到 403 insufficient_scope。
    await _seed_key(tmp_path)
    try:
        client = TestClient(_make_app())
        r = client.get("/api/v1/mcp/config")
        assert r.status_code == 401
        assert r.json()["detail"]["code"] != "insufficient_scope"
    finally:
        await _cleanup()
