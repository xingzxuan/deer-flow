# API Key 控制平面 default-deny 收口 · 设计

> 写于 2026-06-28。收口 Stage 1 final review 记录的最小权限缺口（[stage-1 design §8.1](../../multi-tenant-redesign/01-redesign/stage-1-headless-api-pattern-a-auth-foundation-design.zh-CN.md)）。
>
> **承接：** [Stage 1 · Headless API Pattern A 鉴权地基](../plans/2026-06-28-stage-1-headless-api-pattern-a-auth-foundation.md)（已完成）。本设计是轨道二的安全补丁，不是新功能。

## 1. 背景与问题

Stage 1 让业务系统用 workspace-scoped API key（`Authorization: Bearer dfk_...`）server-to-server 直调 Gateway。鉴权热路径在 `AuthMiddleware` 的 bearer 分支解析 token → `ServicePrincipal`，并把 `(user_id=SA.id, workspace_id)` 写进 contextvar，使下游隔离与真人同构。

落地后的整体安全复核（2026-06-28）发现一处最小权限缺口（stage-1 design §8.1）：

- **scope 只在 `@require_permission` 装饰的路由上生效。** `AuthContext.permissions`（由 key 的 scopes 填充）只被 `@require_permission` 读取，而该装饰器目前只挂在 threads/runs/uploads/artifacts/feedback/suggestions 上。
- `mcp`（`PUT /api/v1/mcp/config`）、`skills`（`POST /api/v1/skills/install`）、`channels`（`restart`）、`models`、`agents`、`memory` 等路由**只校验"已认证",不校验 scope/role**。
- 后果：一把 `scopes="threads:read"` 的 key 仍能改全局 MCP 配置、装技能、重启 channel。
- 更严重：这些目标是**进程级全局资源**（`extensions_config.json`、磁盘上的 skills、channel 进程），不是 workspace 分区的。对它们而言 workspace 隔离也不成立——一个租户的 key 改的是所有租户共享的配置。

### 根因再定位

这不是"几条路由忘了加 scope 检查"。真正的原因是：`_ALL_PERMISSIONS` 里**只有 6 个 `threads:*` / `runs:*` 权限**——控制平面路由从来就不在权限模型里。真人能访问它们，仅仅是因为它们未被装饰。API key 出现后，这些"对所有已认证者开放"的全局控制路由,意外地也对 service principal 开放了。

## 2. 目标与非目标

**目标**
- service principal（API key 请求）只能访问**数据平面**;任何控制平面路由 → 403。
- 真人 / cookie 请求行为**完全不变**。
- 默认拒绝（default-deny）：将来新增控制平面路由,自动被拦,不复现本次"忘了保护"的缺陷。
- 读、写一律拒（不区分 method）。

**非目标（明确推后）**
- 不引入 per-scope 细粒度授权（`scopes=["mcp:write"]` 之类的词汇升级）——留到 external_user 透传 / scope 升级 PR（stage-1 design D4 已推后）。
- 不把控制平面资源改成 workspace 分区(那是更大的多租户改造)。
- 不动 `@require_permission` / `_ALL_PERMISSIONS` 现有语义。
- 不处理 Pattern B（短期 JWT / service-token 分支）——但要为其留好复用接缝。

## 3. 设计

### 3.1 数据平面边界

> **关键前提（已验证）：** nginx 把 `/api/langgraph/(.*)` **rewrite 成 `/api/$1`** 后才转给 gateway(见 `docker/nginx/nginx.local.conf` L48-51);IM channels 也直连 gateway 的 `/api/*`(`langgraph_url` 默认 `http://localhost:8001/api`)。因此 `AuthMiddleware` **永远看不到 `/api/langgraph` 前缀**——LangGraph-SDK 的调用到达中间件时就是 `/api/threads`、`/api/runs`、`/api/assistants` 这些真实 router 路径。gateway 本身没有挂任何 `/api/langgraph` 路由,该前缀是纯 nginx 别名,放进白名单是死代码。

对照 Gateway 路由表(`app/gateway/app.py` include_router + 各 router prefix),**数据平面 / SDK 接口只落在三个前缀下**：

| 前缀 | 覆盖的 router | 性质 |
|---|---|---|
| `threads*` | threads、thread-runs、uploads（`/threads/{id}/uploads`）、artifacts、suggestions、feedback(均 `/threads/...`) | 数据平面 |
| `runs*` | 无状态 runs（`/runs`） | 数据平面 |
| `/api/assistants` | `assistants_compat`——**langgraph-sdk 客户端 init 必需**(`assistants.search()`/`get()`),只读元数据,单挂(无 `/api/v1` twin) | SDK init |

因此 service principal 的白名单很小且稳定：

```
/api/threads      /api/v1/threads
/api/runs         /api/v1/runs
/api/assistants
```

> 不放 `/api/langgraph`(死代码,见上)。`assistants` 单挂故只有一条 `/api/assistants`(无版本)。

**其余一律拒绝**（对 SA）：`models`、`mcp`、`memory`、`skills`、`channels`、`agents`,以及 `service-accounts`、`api-keys`、`auth`。

- 管理类 endpoint（`service-accounts`/`api-keys`）本来就因 `require_workspace_admin`(SA role=member)对 SA 返回 403。default-deny 让它**更早、更统一**地 403,不改变可达性结论。
- `auth`（`/api/v1/auth/*`）当前对 SA 无意义,拒掉无副作用。

### 3.2 enforce 位置

在 `app/gateway/auth_middleware.py` 的 `AuthMiddleware.dispatch` bearer 分支内,`result` 校验通过之后、写 contextvar / `call_next` 之前插入路径检查：

```python
if auth_header.startswith("Bearer dfk_"):
    ...
    if result is None:
        return JSONResponse(status_code=401, ...)  # 现有
    # ↓ 新增：service principal 只能走数据平面
    if not _is_dataplane_path(request.url.path):
        return JSONResponse(
            status_code=403,
            content={"detail": AuthErrorResponse(
                code=AuthErrorCode.INSUFFICIENT_SCOPE,
                message="API keys cannot access this endpoint",
            ).model_dump()},
        )
    request.state.user = result.principal
    ...
```

放在 bearer 分支内的理由：
- 真人 cookie 路径根本不进这段,天然不受影响。
- 检查发生在 `_is_public` 早退之后——公共路径(`/health` 等)即便带 bearer 头也先被 `_is_public` 放行,不进此分支,符合预期。

### 3.3 辅助函数

仿照现有 `_is_public(path)` / `_PUBLIC_PATH_PREFIXES`,在同文件新增模块级常量与函数：

```python
_DATAPLANE_PREFIXES = (
    "/api/threads",
    "/api/v1/threads",
    "/api/runs",
    "/api/v1/runs",
    "/api/assistants",  # langgraph-sdk client init (assistants.search/get), 只读
)


def _is_dataplane_path(path: str) -> bool:
    """service principal 允许访问的数据平面 / SDK 路由前缀。

    数据平面挂在 threads / runs 下;langgraph-sdk init 需要 assistants。
    其余(全局控制平面:models/mcp/memory/skills/channels/agents 与
    管理/auth 端点)对 API key 一律拒绝。注意 `/api/langgraph` 被 nginx
    rewrite 掉,中间件看不到,故不在表内。将来 Pattern B 的 service-token
    分支可复用本函数。
    """
    return any(path.startswith(p) for p in _DATAPLANE_PREFIXES)
```

> **前缀匹配的精度**：用 `startswith`,与 `_is_public` 一致。`/api/threads` 前缀不会误放行 `/api/threads-foo` 之类的路径吗?当前路由表无此类同名前缀冲突(控制平面均为独立段:`/api/models`、`/api/mcp` 等),故 `startswith` 安全。若未来出现冲突,改为带边界的匹配即可——本设计不预防尚不存在的冲突(YAGNI)。

### 3.4 错误口径

新增 `AuthErrorCode.INSUFFICIENT_SCOPE = "insufficient_scope"`（`app/gateway/auth/errors.py`）。该枚举定位是"穷举所有 auth 失败条件",新增一个符合既有模式。

- HTTP 403(已认证但无权),区别于无效 key 的 401。
- 响应体 `{"detail": {"code": "insufficient_scope", "message": ...}}`,与现有 401 `AuthErrorResponse` 完全同构。

## 4. 测试影响

### 4.1 现有探针测试需调整(有意,非绕过)

`tests/test_auth_middleware_api_key.py` 与 `tests/test_headless_api_smoke.py` 用 `/api/probe` 作探针路由。该路径不属数据平面,default-deny 会把它 403——但这些探针的**本意**就是"SA 访问一个受保护的数据平面路由"。

处理:把探针路径挪到数据平面前缀下(如 `/api/v1/threads/_probe`)。这是让测试反映真实约束的正确修正。涉及:
- `test_valid_bearer_sets_sa_contextvars`(探针仍应 200,验证 contextvar)
- `test_headless_api_smoke` 的 `_probe_app`(mint→use 链路仍应 200)
- 其余 401/revoked 用例不受影响(它们本就期望非 200)

### 4.2 新增 `tests/test_api_key_control_plane.py`

| 用例 | 期望 |
|---|---|
| SA bearer → `PUT /api/v1/mcp/config` | 403 `insufficient_scope` |
| SA bearer → `GET /api/v1/models` | 403(**读也拒**) |
| SA bearer → `POST /api/v1/skills/install` | 403 |
| SA bearer → `/api/mcp`(无版本旧路径) | 403 |
| SA bearer → `/api/v1/channels/...`、`/api/v1/agents`、`/api/v1/memory` | 403 |
| SA bearer → `/api/v1/threads/_probe` | 放行(数据平面) |
| SA bearer → `/api/assistants/search`(SDK init) | 放行 |
| cookie/真人 → `/api/v1/mcp`(或任一控制平面) | 不受影响(回归守护——不进 bearer 分支) |
| `_is_dataplane_path` 表驱动单元 | threads/runs(含 `/api` 与 `/api/v1` 双形态)、`/api/assistants` → True;控制平面前缀(含 `/api/langgraph`,死代码也应 False)→ False |

### 4.3 回归

- `make test` 全绿(含 stage-1 的 13 个文件)。
- `make lint` clean、`test_harness_boundary` PASS(本改动全在 app 层)。

## 5. 文件清单

**修改**
- `backend/app/gateway/auth/errors.py` — 加 `AuthErrorCode.INSUFFICIENT_SCOPE`
- `backend/app/gateway/auth_middleware.py` — 加 `_DATAPLANE_PREFIXES` + `_is_dataplane_path` + bearer 分支 403 检查
- `backend/tests/test_auth_middleware_api_key.py` — 探针路径挪到 `/api/v1/threads/_probe`
- `backend/tests/test_headless_api_smoke.py` — 同上

**新增**
- `backend/tests/test_api_key_control_plane.py`

**文档**
- `docs/multi-tenant-redesign/01-redesign/stage-1-headless-api-pattern-a-auth-foundation-design.zh-CN.md` — §8.1 从"已知限制"翻成"已解决",指向本 PR

## 6. 不可逆 / 需想清楚的点

| 决策 | 取舍 |
|---|---|
| 数据平面边界 = threads/runs/assistants 三前缀 | 业务系统已接入后收窄白名单 = 破坏调用方;故白名单只增不减。本次定的是最小集,后续按需 **加**(如 Pattern B 的 `exchange-token`)。 |
| 读也拒(`GET /api/v1/models` 对 SA 403) | 若将来业务方需要列模型选型,再单独 allowlist 该 GET;default-deny 下"放开"比"收紧"安全。 |
| 错误码 `insufficient_scope` | 接入方可能据此分支处理;改名要联调。早定。 |

## 7. 与后续 PR 的接缝

- **Pattern B（service-token 分支）**:复用 `_is_dataplane_path`。新增的 `exchange-token` endpoint 若需 SA 调用,记得把其路径加入 `_DATAPLANE_PREFIXES`(或单独放行)。
- **scope 词汇升级**（`scopes=["mcp:write"]`):若将来要让特定 SA 受控访问某控制平面路由,在本 default-deny 之上叠加"白名单内再按 scope 细分"即可,不与本设计冲突。
