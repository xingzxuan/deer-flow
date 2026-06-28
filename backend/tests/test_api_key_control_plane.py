"""API key control-plane default-deny tests (Stage 1 收口).

service principal (API key) 只能访问数据平面 (threads/runs/assistants);
控制平面 (models/mcp/memory/skills/channels/agents 与管理/auth) 一律 403。
真人 cookie 路径不受影响。设计见 spec
docs/superpowers/specs/2026-06-28-api-key-control-plane-default-deny-design.md。
"""

from __future__ import annotations

import pytest

from app.gateway.auth_middleware import _is_dataplane_path


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
    ],
)
def test_control_plane_paths_denied(path):
    assert _is_dataplane_path(path) is False
