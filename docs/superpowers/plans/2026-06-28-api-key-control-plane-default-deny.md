# API Key 控制平面 default-deny 收口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 service principal（API key `Authorization: Bearer dfk_...` 请求）只能访问数据平面（`/api/threads*`、`/api/runs*`、`/api/assistants`），任何控制平面路由（models/mcp/memory/skills/channels/agents 与管理/auth 端点）读写一律返回 403；真人 cookie 请求完全不受影响。

**Architecture:** 在 `AuthMiddleware.dispatch` 的 bearer 分支内、token 校验通过之后、写 contextvar 之前，加一道 default-deny 路径白名单检查（`_is_dataplane_path`）。白名单是模块级前缀元组，新增控制平面路由自动被拦，不复现"忘了保护"的缺陷。检查只在 bearer 分支内，cookie 路径天然不进。

**Tech Stack:** Python 3.12 · FastAPI · Starlette `BaseHTTPMiddleware` · `starlette.testclient.TestClient` · pytest + `pytest.mark.anyio`。

**设计来源:** [2026-06-28-api-key-control-plane-default-deny-design.md](../specs/2026-06-28-api-key-control-plane-default-deny-design.md)（策略、白名单边界、错误口径、nginx rewrite 前提皆以该 spec 为准）。

**运行约定（每条命令都从 `backend/` 目录执行）:**
- 单测：`PYTHONPATH=. uv run pytest tests/<file>.py -v`
- lint：`make lint`（ruff，行宽 240，双引号）
- 全量回归：`make test`

**关键前提（spec §3.1，已验证）:** nginx 把 `/api/langgraph/(.*)` rewrite 成 `/api/$1` 后才转给 gateway，IM channels 也直连 `/api/*`，因此 `AuthMiddleware` 永远看不到 `/api/langgraph`；LangGraph-SDK 调用到达中间件时是 `/api/threads`、`/api/runs`、`/api/assistants`。白名单因此是这三个前缀，**不含** `/api/langgraph`（死代码）。

---

## File Structure

新增 / 修改文件一览（精确路径）：

- Modify: `backend/app/gateway/auth/errors.py` — `AuthErrorCode` 加 `INSUFFICIENT_SCOPE`
- Modify: `backend/app/gateway/auth_middleware.py` — 加 `_DATAPLANE_PREFIXES` + `_is_dataplane_path`；bearer 分支加 403 检查
- Modify: `backend/tests/test_auth_middleware_api_key.py` — 探针路径 `/api/probe` → `/api/v1/threads/_probe`
- Modify: `backend/tests/test_headless_api_smoke.py` — 同上
- Create: `backend/tests/test_api_key_control_plane.py` — 单元表 + 集成 403 / 放行 / cookie 回归
- Modify: `docs/multi-tenant-redesign/01-redesign/stage-1-headless-api-pattern-a-auth-foundation-design.zh-CN.md` — §8.1 从"已知限制"翻成"已解决"

**Task → commit 边界:** 4 个 Task，顺序实现，每个 Task 末尾 commit。Task 2（挪探针路径）必须早于 Task 3（加 deny），否则 deny 落地会打断既有探针测试。

---

## Task 1: 错误码 + 数据平面白名单辅助函数

**Files:**
- Modify: `backend/app/gateway/auth/errors.py`
- Modify: `backend/app/gateway/auth_middleware.py`
- Test: `backend/tests/test_api_key_control_plane.py`

> 本 Task 只加错误码 + 纯函数 `_is_dataplane_path`（中间件尚未调用它，Task 3 才接线）。先用表驱动单测锁定边界。

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_api_key_control_plane.py`:

```python
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
        "/api/langgraph/threads",  # nginx 死代码:中间件本看不到,真混进来也应 deny
    ],
)
def test_control_plane_paths_denied(path):
    assert _is_dataplane_path(path) is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_api_key_control_plane.py -v`
Expected: FAIL — `ImportError: cannot import name '_is_dataplane_path' from 'app.gateway.auth_middleware'`

- [ ] **Step 3: 写实现 — 错误码**

Modify `backend/app/gateway/auth/errors.py` — 在 `AuthErrorCode` 枚举末尾（`WORKSPACE_REQUIRED` 之后）加一个成员。将：

```python
    NOT_AUTHENTICATED = "not_authenticated"
    SYSTEM_ALREADY_INITIALIZED = "system_already_initialized"
    WORKSPACE_REQUIRED = "workspace_required"
```

改为：

```python
    NOT_AUTHENTICATED = "not_authenticated"
    SYSTEM_ALREADY_INITIALIZED = "system_already_initialized"
    WORKSPACE_REQUIRED = "workspace_required"
    INSUFFICIENT_SCOPE = "insufficient_scope"
```

- [ ] **Step 4: 写实现 — 白名单辅助函数**

Modify `backend/app/gateway/auth_middleware.py` — 在 `_is_public` 函数定义之后（L55 后）追加数据平面前缀常量与辅助函数：

```python
# Data-plane / SDK route prefixes a service principal (API key) may reach.
# Everything else (global control plane: models/mcp/memory/skills/channels/
# agents, plus management/auth endpoints) is denied by default for API keys.
# NOTE: nginx rewrites /api/langgraph/(.*) -> /api/$1 before the gateway, so
# AuthMiddleware never sees /api/langgraph; the SDK surface arrives as
# /api/threads, /api/runs, /api/assistants. assistants.search()/get() is
# required for langgraph-sdk client init, so /api/assistants is allowed.
_DATAPLANE_PREFIXES: tuple[str, ...] = (
    "/api/threads",
    "/api/v1/threads",
    "/api/runs",
    "/api/v1/runs",
    "/api/assistants",
)


def _is_dataplane_path(path: str) -> bool:
    """True if an API key request may reach this path. Reusable by a future
    Pattern B service-token branch."""
    return any(path.startswith(prefix) for prefix in _DATAPLANE_PREFIXES)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_api_key_control_plane.py -v`
Expected: PASS（21 个 parametrize 用例）

- [ ] **Step 6: lint + commit**

```bash
cd backend && make lint && git add app/gateway/auth/errors.py app/gateway/auth_middleware.py tests/test_api_key_control_plane.py
git commit -m "feat(authz): data-plane allowlist helper + INSUFFICIENT_SCOPE code (Stage 1 收口)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: 既有探针测试路径挪到数据平面

**Files:**
- Modify: `backend/tests/test_auth_middleware_api_key.py`
- Modify: `backend/tests/test_headless_api_smoke.py`

> 这两个文件的探针路由是 `/api/probe`（非数据平面）。Task 3 的 deny 落地后，valid-key 探针会被 403 打断。这些探针的本意是"SA 访问一个受保护的数据平面路由"，故先把路径挪到 `/api/v1/threads/_probe`。本 Task 不改运行时行为（deny 尚未接线），改完探针仍应全绿。

- [ ] **Step 1: 改 `test_auth_middleware_api_key.py` 的探针路由定义**

Modify `backend/tests/test_auth_middleware_api_key.py` — 把 `_make_app` 内的探针路由声明（约 L72）从：

```python
    @app.get("/api/probe")
```

改为：

```python
    @app.get("/api/v1/threads/_probe")
```

- [ ] **Step 2: 改 `test_auth_middleware_api_key.py` 的全部 `client.get` 路径**

同文件，把所有 `client.get("/api/probe", ...)`（5 处：约 L87 / L98 / L108 / L120 / L131）的路径串 `"/api/probe"` 全部改为 `"/api/v1/threads/_probe"`。其余参数（headers）不动。

> 校验：`grep -n '/api/probe' tests/test_auth_middleware_api_key.py` 应无输出。

- [ ] **Step 3: 改 `test_headless_api_smoke.py` 的探针路由定义 + 调用**

Modify `backend/tests/test_headless_api_smoke.py`：
- 把 `_probe_app` 内的探针路由声明（约 L95）`@app.get("/api/probe")` 改为 `@app.get("/api/v1/threads/_probe")`
- 把两处调用（约 L111 / L116）`probe.get("/api/probe", ...)` 的路径串改为 `"/api/v1/threads/_probe"`

> 校验：`grep -n '/api/probe' tests/test_headless_api_smoke.py` 应无输出。

- [ ] **Step 4: 跑测试确认仍全绿（无行为变化）**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_auth_middleware_api_key.py tests/test_headless_api_smoke.py -v`
Expected: PASS（与改动前相同的用例数；deny 尚未接线，valid 探针走数据平面路径仍 200，401 用例仍 401）

- [ ] **Step 5: commit**

```bash
cd backend && git add tests/test_auth_middleware_api_key.py tests/test_headless_api_smoke.py
git commit -m "test(auth): move bearer probe routes under /api/v1/threads (Stage 1 收口)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: AuthMiddleware bearer 分支 default-deny

**Files:**
- Modify: `backend/app/gateway/auth_middleware.py`
- Test: `backend/tests/test_api_key_control_plane.py`（追加集成测试）

> deny 检查放在 bearer 分支内 `if result is None: return 401` **之后**、写 contextvar 之前——无效 key 仍是 401（不是 403），只有 valid key 命中控制平面才 403。

- [ ] **Step 1: 写失败测试**（追加到 `test_api_key_control_plane.py` 末尾）

```python
import pytest
from starlette.testclient import TestClient

from deerflow.auth.tokens import generate_api_key

pytestmark = pytest.mark.anyio


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
    from fastapi import FastAPI, Request

    from app.gateway.auth_middleware import AuthMiddleware
    from deerflow.runtime.user_context import get_effective_user_id

    app = FastAPI()
    app.add_middleware(AuthMiddleware)

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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_api_key_control_plane.py -v`
Expected: FAIL — `test_sa_denied_on_control_plane[...]` 返回 200/404 而非 403（deny 尚未接线）

- [ ] **Step 3: 写实现 — bearer 分支加 deny**

Modify `backend/app/gateway/auth_middleware.py` — 在 bearer 分支内，`if result is None: return 401` 之后、`request.state.user = result.principal` 之前插入路径检查。将：

```python
            if result is None:
                return JSONResponse(
                    status_code=401,
                    content={"detail": AuthErrorResponse(code=AuthErrorCode.TOKEN_INVALID, message="Invalid API key").model_dump()},
                )
            request.state.user = result.principal
```

改为：

```python
            if result is None:
                return JSONResponse(
                    status_code=401,
                    content={"detail": AuthErrorResponse(code=AuthErrorCode.TOKEN_INVALID, message="Invalid API key").model_dump()},
                )
            # Default-deny: a service principal may only reach the data plane
            # (threads/runs/assistants). Control-plane routes (mcp/skills/
            # channels/models/agents/memory + management/auth) are global,
            # un-partitioned config — never reachable by an API key. New
            # control-plane routes are denied automatically (allowlist, not
            # blocklist). Humans (cookie path) never enter this branch.
            if not _is_dataplane_path(request.url.path):
                return JSONResponse(
                    status_code=403,
                    content={"detail": AuthErrorResponse(code=AuthErrorCode.INSUFFICIENT_SCOPE, message="API keys cannot access this endpoint").model_dump()},
                )
            request.state.user = result.principal
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_api_key_control_plane.py -v`
Expected: PASS（Task 1 的 21 单元 + 本 Task 的集成/参数化用例全绿）

- [ ] **Step 5: 既有 bearer 测试回归**

确认探针挪位 + deny 后既有用例不破：

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_auth_middleware_api_key.py tests/test_headless_api_smoke.py tests/test_auth_middleware.py tests/test_auth_middleware_workspace.py -v`
Expected: PASS（valid 探针走 `/api/v1/threads/_probe` 数据平面 → 200；invalid/revoked/non-dfk → 401；cookie 路径不变）

- [ ] **Step 6: lint + commit**

```bash
cd backend && make lint && git add app/gateway/auth_middleware.py tests/test_api_key_control_plane.py
git commit -m "harden(gateway): API keys default-deny on control-plane routes (Stage 1 收口)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: 文档收尾 + 全量回归

**Files:**
- Modify: `docs/multi-tenant-redesign/01-redesign/stage-1-headless-api-pattern-a-auth-foundation-design.zh-CN.md`

- [ ] **Step 1: 把 spec §8.1 从"已知限制"翻成"已解决"**

Modify `docs/multi-tenant-redesign/01-redesign/stage-1-headless-api-pattern-a-auth-foundation-design.zh-CN.md` §8.1。在该节"后续 PR 决策项"那条 bullet 之后追加一行解决说明（保留原限制描述作为历史，追加 resolved 标注）：

```markdown
  - **【已解决 2026-06-28】** 改为 default-deny：service principal 只能访问数据平面（`/api/threads*`、`/api/runs*`、`/api/assistants`），所有控制平面路由（含 read）一律 403 `insufficient_scope`。实现见 `AuthMiddleware._is_dataplane_path`；设计见 [api-key-control-plane-default-deny-design](../../superpowers/specs/2026-06-28-api-key-control-plane-default-deny-design.md)。细粒度 scope 词汇升级仍按原计划推后。
```

- [ ] **Step 2: 全量回归 + lint + 边界**

```bash
cd backend && make lint && make test && PYTHONPATH=. uv run pytest tests/test_harness_boundary.py -v
```
Expected: lint clean；test 全绿（含 stage-1 的 13 个文件 + 本次新增 `test_api_key_control_plane.py`）；boundary PASS（本改动全在 app 层，未引入 deerflow→app import）

- [ ] **Step 3: commit**

```bash
cd /Users/wangguixuan/work/github/deer-flow
git add docs/multi-tenant-redesign/01-redesign/stage-1-headless-api-pattern-a-auth-foundation-design.zh-CN.md
git commit -m "docs(stage-1): mark control-plane scope limitation resolved (Stage 1 收口)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage（逐条对 spec）**
- §2 目标:service principal 仅数据平面、真人不受影响、default-deny、读写一律拒 → Task 3 deny + Task 1 白名单 + 参数化用例（含 GET `/api/v1/models` 读也拒）✅
- §3.1 白名单 = threads/runs/assistants 三前缀、不含 langgraph → Task 1 `_DATAPLANE_PREFIXES` + 单元表（含 `/api/langgraph` 应 False）✅
- §3.2 enforce 位置（bearer 分支 None 校验之后、contextvar 之前）→ Task 3 Step 3 精确锚点 ✅
- §3.3 `_is_dataplane_path` 辅助 + 复用接缝 → Task 1 Step 4 ✅
- §3.4 错误码 `INSUFFICIENT_SCOPE` + 403 同构响应 → Task 1 Step 3 + Task 3 Step 3 ✅
- §4.1 探针挪到 `/api/v1/threads/_probe` → Task 2 ✅
- §4.2 新增 `test_api_key_control_plane.py`（403 / 放行 / cookie 回归 / 单元表）→ Task 1 + Task 3 ✅
- §4.3 回归 make test + lint + boundary → Task 4 Step 2 ✅
- §5 文件清单 → 全覆盖（errors.py / auth_middleware.py / 两测试文件 / 新测试 / spec 文档）✅
- §5 文档:§8.1 翻成已解决 → Task 4 Step 1 ✅

**2. Placeholder scan:** 所有 code step 含可运行实际代码；改测试路径处给了精确行号锚点 + grep 校验命令；无 TBD/TODO/“类似上文”。✅

**3. Type consistency:**
- `_is_dataplane_path(path) -> bool` / `_DATAPLANE_PREFIXES: tuple[str, ...]` — Task 1 定义，Task 1 单元测试 + Task 3 中间件调用一致 ✅
- `AuthErrorCode.INSUFFICIENT_SCOPE = "insufficient_scope"` — Task 1 定义，Task 3 响应 + 测试断言 `code == "insufficient_scope"` 一致 ✅
- `AuthErrorResponse(code=..., message=...).model_dump()` — 与 bearer 分支既有 401 用法一致（spec §3.4）✅
- 探针路径 `/api/v1/threads/_probe` — Task 2 改既有两文件 + Task 3 新测试 app 一致 ✅
- `_seed_key` 返回 `GeneratedKey`（`.plaintext`/`.prefix`/`.key_hash`），`ApiKeyRepository.create(*, service_account_id, key_prefix, key_hash, name, scopes)` — 与 stage-1 PR1/PR2 既有签名一致 ✅
