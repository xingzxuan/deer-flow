# Stage 1 · Headless API Pattern A 鉴权地基 — 设计

> 设计稿。日期 2026-06-28。承接 Stage 0（PR1-PR8 全部 merge，见 [STATUS.zh-CN.md](../03-impl/STATUS.zh-CN.md)）与 headless API 轨道设计 [headless-api-track.zh-CN.md](../02-rollout/headless-api-track.zh-CN.md)。
>
> 范围：headless-api-track **轨道二（Pattern A）** 的鉴权地基。让业务系统 backend 能用 API key（`Authorization: Bearer dfk_...`）调通 DeerFlow Gateway——server-to-server。Pattern B（浏览器直连 + 短期 JWT）、external_user 透传、identity_mode 三态行为、rate limit 不在本 spec 范围（轨道二后续 PR / 轨道三）。

## 1. 目标与非目标

### 目标

业务系统 backend 拿一把 workspace-scoped API key，`Authorization: Bearer dfk_live_<...>` 直调 `/api/threads` 等现有 REST endpoint，请求被正确归属到该 key 背后的 service account + workspace，并被 workspace 隔离（跨 workspace 资源一律 404）。形成可自助的最小闭环：workspace owner 经管理 endpoint 建 service account → 建 key → 业务侧用 key 调通。

### 非目标（明确推后）

- **Pattern B**（`exchange-token` / 短期 JWT / CORS / `allowed_origins`）— 轨道三。
- **external_user 透传**（`X-External-User-Id` upsert、ghost user、per-external-user memory）— 轨道二后续 PR。本 spec 只到 `collapsed` 语义（一切归 SA）。
- **identity_mode 三态行为分支** — 同上。SA 的 `identity_mode` 列存在（PR8 schema），但本 spec 一律按 collapsed 处理，不读该列做分支。
- **rate limit / idempotency** — 轨道二后续 PR。
- **API key 管理前端 UI** — 本 spec 只做管理 **endpoint**；UI 接这些 endpoint 是后续 PR。
- **`@require_permission` 的 `scopes=[...]` 显式参数升级** — 本 spec 用"把 scopes 灌进现有 `AuthContext.permissions`"取得 scope 校验，decorator 签名不改。

## 2. 现状锚点（实现时镜像，防漂移）

| 组件 | 文件 | 关键符号 |
|---|---|---|
| Auth 中间件 | `backend/app/gateway/auth_middleware.py` | `AuthMiddleware.dispatch`（检 `_is_public` → cookie/JWT → 设 contextvar，L77-143） |
| JWT / TokenPayload | `backend/app/gateway/auth/jwt.py` | `TokenPayload`、`create_access_token`、`decode_token` |
| 用户解析 | `backend/app/gateway/deps.py` | `get_current_user_from_request`（L186）、`get_optional_user_from_request`（L230） |
| CSRF | `backend/app/gateway/csrf_middleware.py` | `CSRFMiddleware`、`should_check_csrf`（L32）、`is_auth_endpoint`（L58） |
| user contextvar | `backend/packages/harness/deerflow/runtime/user_context.py` | `CurrentUser`、`set_current_user`、`get_effective_user_id` |
| workspace contextvar | `backend/packages/harness/deerflow/runtime/workspace_context.py` | `CurrentWorkspace`、`set_current_workspace`、`get_effective_workspace_id` |
| 授权装饰器 | `backend/app/gateway/authz.py` | `require_permission`（L197）、`AuthContext`（L62，含 `permissions: list[str]` + `has_permission`） |
| 仓储样板 | `backend/packages/harness/deerflow/persistence/workspace/sql.py` | `WorkspaceRepository`（构造收 `session_factory`，每方法开 fresh session，`_row_to_dict`） |
| session 工厂 | `backend/packages/harness/deerflow/persistence/engine.py` | `get_session_factory()` |
| PR8 ORM | `persistence/{service_account,api_key,external_user}/model.py` | `ServiceAccountRow` / `ApiKeyRow` / `ExternalUserRow`（schema 已落，见 [pr8 impl note](../03-impl/pr8-headless-api-schema.zh-CN.md)） |
| 路由挂载 | `backend/app/gateway/app.py` | `create_app()` 内 15 个 `include_router`（L379-421） |
| 路由前缀约定 | `backend/app/gateway/routers/*.py` | 前缀**写死在 `APIRouter(prefix=...)`**；`auth.py` 已用 `/api/v1/auth`，证明 v1 与旧前缀共存 |
| 前端 API 路径 | `frontend/src/core/*/api.ts`、`src/core/threads/hooks.ts` 等 | `getBackendBaseURL()` + 路径串；auth 已用 `/api/v1/auth`；langgraph-sdk 走 `/api/langgraph/*` |
| 测试夹具 | `backend/tests/conftest.py` | autouse `_auto_user_context` / `_auto_workspace_context`（可 `@pytest.mark.no_auto_*` opt-out） |
| 中间件测试样板 | `backend/tests/test_auth_middleware.py`、`test_csrf_middleware.py` | `_make_app()` + `starlette.testclient.TestClient` |

## 3. 已锁的设计决策（来自 brainstorm）

| # | 决策 | 取舍 |
|---|---|---|
| **D1** | **SA 身份映射为 `CurrentUser`** | SA 鉴权后 `set_current_user(CurrentUser(id=SA.id, is_service_account=True))` + `set_current_workspace(SA.workspace_id)`。thread 归属落 `user_id = SA.id`，复用现有 `workspace_id + user_id` 隔离逻辑，**不改业务表 schema**。代价：SA id 坐在 user_id 列，将来拆 external_user 要迁移——可接受，因为 external_user 透传是后续 PR 且届时本就要动归属维度。**否决**「独立 `service_account` contextvar + thread 加 `service_account_id` 列」方案，因其要改业务表 schema + 所有读 `user_id` 的 consumer，超出地基范围。 |
| **D2** | **mint key 走管理 endpoint** | `/api/v1/service-accounts` + `/api/v1/api-keys`，gated 到 workspace owner/admin（复用 `@require_permission`）。业务侧可自助；plaintext 仅创建时返一次。**否决**纯 CLI（业务侧拿不到自助接口）。CLI 包装留作 on-prem bootstrap 的后续可选项。 |
| **D3** | **旧路由现在就全量迁 `/api/v1`** | 所有 router 双挂 `/api` + `/api/v1`，前端同步迁 `/api/v1`。**否决**「只新 surface 用 v1」。因面较广、回归风险较大，本 PR 放在**最后**（PR5），让 PR1-4 的鉴权地基能独立证伪。 |
| **D4** | **scope 校验"白捡"进 PR2** | API key 路径把 `key.scopes`（逗号分隔）解析进 `AuthContext.permissions`，现有 `@require_permission` 的 `has_permission(resource, action)` 即对 API key 生效——无需改 decorator 签名。完整 `scopes=[...]` 参数化升级推后。 |
| **D5** | **key 格式锁 `dfk_live_<24>` / `dfk_test_<24>`** | 沿用 track 文档不可逆决策。`key_prefix` = 前 16 字符（含 `dfk_live_`），全局 UNIQUE（PR8 schema 已定）。DB 只存 `sha256(plaintext)` hex。 |

## 4. 架构与数据流

```
业务系统 backend ──▶ AuthMiddleware ──┬── "Bearer dfk_..." ──▶ APIKeyAuthBackend
                                       │        │
                                       │        ├─ sha256(token) → ApiKeyRepository.get_active_by_hash
                                       │        ├─ 校 key.expires_at 未过期；load SA 校 status=active；load workspace
                                       │        ├─ set_current_user(CurrentUser(id=SA.id, is_service_account=True))
                                       │        ├─ set_current_workspace(SA.workspace_id)
                                       │        ├─ AuthContext.permissions = parse(key.scopes)
                                       │        └─ touch_last_used(key.id)（异步/best-effort）
                                       │
                                       └── "Cookie: access_token=..." ──▶ 现有 cookie 路径（不变）
                                                │
                          CSRFMiddleware: 见 Authorization: Bearer 即 skip（PR3）
                                                │
                                                ▼
                          路由 + @require_permission（scope via permissions, owner_check via workspace_id+user_id）
                                                ▼
                          thread store / sandbox（按 user_id=SA.id + workspace_id 隔离，零改动）
```

**关键不变量**：API key 路径走完后，下游（thread store、sandbox、`@require_permission` 的 owner_check）看到的 `(user_id, workspace_id)` 与一个真人用户在该 workspace 下完全同构——这正是 D1 让地基零改动下游的原因。

## 5. 逐 PR 设计

### PR1 · 三表仓储 + token 工具（`deerflow` 层）

**新增**
- `persistence/service_account/sql.py` — `ServiceAccountRepository`：`create(*, workspace_id, name, created_by, role="member", identity_mode="collapsed", status="active")`、`get(sa_id)`、`get_active(sa_id)`（status==active 才返）、`list_by_workspace(workspace_id)`、`update_status(sa_id, status)`。
- `persistence/api_key/sql.py` — `ApiKeyRepository`：`create(*, service_account_id, key_prefix, key_hash, name, scopes, expires_at=None)`、`get_active_by_hash(key_hash)`（**热路径**，`revoked_at IS NULL` 且未过期 → 走 `idx_api_keys_active`）、`list_by_service_account(sa_id)`、`revoke(key_id)`（设 `revoked_at`）、`touch_last_used(key_id)`。
- `persistence/external_user/sql.py` — `ExternalUserRepository`：本 PR 仅建 `get`/`list_by_workspace` 等读方法 + 基础 `upsert(*, workspace_id, service_account_id, external_id, ...)` 骨架；**透传调用方留到后续 PR**（建仓储不接业务，与 PR8 建 schema 不接路由同思路）。
- `deerflow/auth/tokens.py` — `generate_api_key(env: Literal["live","test"]) -> GeneratedKey(plaintext, prefix, key_hash)`；`hash_api_key(plaintext) -> str`（sha256 hex）；`split_prefix(plaintext) -> str`（前 16）。用 `secrets.token_urlsafe`。**落 `deerflow` 层**（不是 `app`）——因仓储的 `get_active_by_hash`（deerflow）与管理 endpoint 的建 key（app）都要用，按 harness boundary（app 可 import deerflow，反之不可）必须在 deerflow 侧。

**测试**（严格 TDD 红→绿）：每仓储 CRUD round-trip；`get_active_by_hash` 对 revoked / expired key 返 None；token 生成格式（前缀、长度、prefix 截取）、`hash_api_key` 确定性、plaintext 不可从 hash 反推（仅断言 hash≠plaintext + 长度）。镜像 `test_*_schema.py` 与 workspace 仓储测试风格。

**不在范围**：任何路由、中间件、contextvar。

### PR2 · APIKeyAuthBackend + AuthMiddleware 双路径（`app` 层）

**改动**
- `CurrentUser`（`runtime/user_context.py`）加 `is_service_account: bool = False` 字段（默认 False，cookie 路径不受影响）。
- 新 `app/gateway/auth/api_key_backend.py` — `APIKeyAuthBackend.authenticate(token: str) -> AuthResult | None`：
  1. `hash = hash_api_key(token)` → `ApiKeyRepository.get_active_by_hash(hash)`；未命中/已撤销/已过期 → None（→ 401）。
  2. `ServiceAccountRepository.get_active(key.service_account_id)`；非 active → None（→ 401/403）。
  3. load workspace（校 status）。
  4. 返回足以让中间件设 contextvar 的结构：`(CurrentUser(id=SA.id, is_service_account=True), workspace_id, role, permissions=parse_scopes(key.scopes))`。
  5. best-effort `touch_last_used(key.id)`（失败不阻断请求）。
- `AuthMiddleware.dispatch`：在 cookie 分支**之前**插入 bearer 分支——`Authorization` 头以 `Bearer dfk_` 开头 → 走 `APIKeyAuthBackend` → 设 `request.state.user` / `request.state.auth`（含 permissions）+ 两个 contextvar；否则落回现有 cookie 流程。`_is_public` / 内部 auth 头逻辑不变。
- `AuthContext.permissions` 由 API key 的 scopes 填充（D4）。

**测试**：有效 key → 200 且 contextvar 正确（SA.id / workspace_id）；无效/撤销/过期 key → 401；非 `dfk_` 的 Bearer → 不误入此路径；有 scope 的 key 调对应 endpoint 通过、无 scope 被 `@require_permission` 拒；cookie 路径回归不破。用 `_make_app()` 样板 + 直接 insert 的 key（PR1 仓储）构造。

### PR3 · CSRF skip on bearer

**改动**
- `csrf_middleware.py` 加 `has_bearer_header(request) -> bool`（`Authorization` 头存在且以 `Bearer ` 起）；`should_check_csrf` 在其为真时返 False。cookie 路径 CSRF 行为完全不变。

**测试**：带 `Authorization: Bearer ...` 的 POST 跳过 CSRF（无 `X-CSRF-Token` 也 200）；cookie POST 仍要 CSRF token（回归）。

### PR4 · 管理 endpoint（mint 闭环）

**新增**
- `app/gateway/routers/service_accounts.py` — `APIRouter(prefix="/api/v1/service-accounts")`：
  - `POST /` 建 SA（body: name, role?, identity_mode?）→ 归当前 workspace，`created_by` = 当前 user。`@require_permission` gated 到 owner/admin。
  - `GET /` 列当前 workspace 的 SA。
  - `PATCH /{sa_id}` 改 status（suspend/active）。
- `app/gateway/routers/api_keys.py` — `APIRouter(prefix="/api/v1/api-keys")`：
  - `POST /` 为指定 SA 建 key（body: service_account_id, name, scopes, env?, expires_at?）→ 调 `generate_api_key` → 存 hash+prefix → **响应体含 plaintext，仅此一次**。
  - `GET /?service_account_id=` 列 key（只返 prefix / name / scopes / 时间戳，**绝不**返 hash/plaintext）。
  - `DELETE /{key_id}` revoke（设 `revoked_at`）。
- 两 router 在 `app.py` `include_router`。owner/admin gating 复用现有 authz；SA / key 必须属于当前 workspace（跨 workspace 操作 → 404）。

**测试 + 端到端 smoke**：owner 建 SA → 建 key（断言 plaintext 仅返一次、再查不含 plaintext）→ 用该 key 调 `/api/threads` 跑通 → 另一 workspace 的 key 访问首 workspace 资源得 404 → member（非 owner/admin）建 SA 被拒。

### PR5 · `/api/v1` 全量迁移 + 旧路径兼容（最后做）

**后端**
- 把 15 个 router 的写死前缀从 `APIRouter(prefix="/api...")` 改为相对前缀（如 `/threads`），在 `app.py` include 时**双挂**：一次 `/api` + 一次 `/api/v1`（保持向后兼容）。`auth.py`（已 `/api/v1/auth`）与 PR4 新 router（已 `/api/v1/*`）按需统一。
- `/api/langgraph/*`（LangGraph SDK 兼容路径）**不版本化**，不动。
- 旧 `/api/*`（无版本）响应加 `X-API-Deprecated` header（sunset 日期取 track 约定 `2027-01-01`）。

**前端**
- `frontend/src/core/*/api.ts`、`src/core/threads/hooks.ts`、`src/core/artifacts/utils.ts`、`src/core/uploads/api.ts`、`src/core/api/feedback.ts`、`src/core/models/api.ts` 等处的 `/api/...` 路径串迁到 `/api/v1/...`。langgraph-sdk 客户端路径（`/api/langgraph/*`）不动。
- 验证：`pnpm lint && pnpm typecheck`；若动到 env/auth/routing 则 `pnpm build`。

**测试**：旧 `/api/threads` 与新 `/api/v1/threads` 均 200 且行为一致；旧路径带 `X-API-Deprecated`；langgraph 路径不受影响。后端全量 `make lint && make test` 回归。

## 6. 错误处理约定

| 情况 | 响应 |
|---|---|
| key 不存在 / hash 不匹配 / 已 revoke / 已过期 | 401 |
| SA suspended/deleted、workspace 非 active | 401（不泄漏"key 有效但账户停用"细节，保守口径；如业务方需区分再放宽到 403） |
| 有效 key 但缺 scope | 403（沿用 `@require_permission` 现有语义） |
| 跨 workspace 访问资源（owner_check） | 404（沿用现有租户隔离：藏存在性，非 403） |
| 非 owner/admin 调管理 endpoint | 403 |

## 7. 测试策略

- 每 PR 严格 TDD（红→绿），镜像现有 `test_auth_middleware.py` / `test_csrf_middleware.py` / 仓储测试风格。
- 仓储层用 SQLite（autouse fixture），鉴权热路径的 partial index 行为不依赖驱动（逻辑层过滤）。
- PR4 的端到端 smoke 是地基的"活体证明"——比单测更有说服力（仿 Stage 0 `multi_tenant.py` 思路）。
- 全程不引入新 caplog flake；既有 18 个 flake 不在本 spec 处理范围。

## 8. 不可逆决策清单（动手前确认，沿用 track 已锁口径）

| 决策 | 不可逆原因 | 本 spec 取值 |
|---|---|---|
| API key 格式 | 业务接入后改格式所有 key 失效 | `dfk_live_<24>` / `dfk_test_<24>`，prefix 16，sha256 存储（D5） |
| SA 归属映射 | 改了 thread 归属语义 | `user_id = SA.id`（D1）；external_user 维度留后续 |
| 管理 endpoint 路径 | 业务/前端接入后改 path 要联调 | `/api/v1/service-accounts`、`/api/v1/api-keys`（D2） |
| `/api/v1` 启用与 deprecation | 业务接了再换 prefix 不友好 | 全量双挂 + 旧路径 `X-API-Deprecated: 2027-01-01`（D3） |
| scope 字符串格式 | 存量 key 的 scopes 解析依赖它 | 逗号分隔 `resource:action`（沿用 PR8 schema + 现有 permission 串） |

## 8.1 已知限制（落地后复核确认，需后续 PR 决策）

> 实现完成后的整体安全复核（2026-06-28）发现一处**符合本 spec 范围但值得显式记录**的最小权限缺口：

- **scope 仅在 threads/runs 等 `@require_permission` 装饰的路由上生效。** `AuthContext.permissions`（由 key 的 scopes 填充）只被 `@require_permission` 读取，而该装饰器目前只挂在 threads/runs/uploads/artifacts/feedback/suggestions 上。`mcp`（`PUT /api/v1/mcp/config`）、`skills`（`POST /api/v1/skills/install`）、`channels`（`restart`）、`models`、`agents`、`memory` 等路由**只校验"已认证"，不校验 scope/role**。后果：一把 `scopes="threads:read"` 的 key 仍能改全局 MCP 配置、装技能、重启 channel；且这些目标是**进程级全局**（非 workspace 分区），对它们而言 workspace 隔离也不成立。
  - 这与现有真人模型一致（真人拿 `_ALL_PERMISSIONS`，这些路由本就无授权），且 D4 / 非目标已把 `scopes=[...]` 显式参数化升级推后——故属**设计内的已知限制，非缺陷**。
  - **后续 PR 决策项**：要么把这些全局配置路由 gated 到 `require_workspace_admin` / 专门 scope，要么显式声明"Stage 1 的 API key 在未被 `@require_permission` 装饰处为全权"。在 external_user 透传 / `scopes=[...]` 升级 PR 中一并处理。
  - **【已解决 2026-06-28】** 改为 default-deny：service principal 只能访问数据平面（`/api/threads*`、`/api/runs*`、`/api/assistants`），所有控制平面路由（含 read）一律 403 `insufficient_scope`。实现见 `AuthMiddleware._is_dataplane_path`；设计见 [api-key-control-plane-default-deny-design](../../superpowers/specs/2026-06-28-api-key-control-plane-default-deny-design.md)。细粒度 scope 词汇升级仍按原计划推后。

## 9. 与后续 PR 的接口

本 spec 的地基为轨道二后续 / 轨道三留好接缝：
- `ExternalUserRepository`（PR1 建好）+ `is_service_account` 标记 → external_user 透传 PR 直接接。
- `APIKeyAuthBackend` 返回结构里已带 scopes/permissions → `@require_permission` 的 `scopes=[...]` 参数化升级可平滑替换。
- 管理 endpoint → 前端 API key 管理 UI 直接消费。
- `/api/v1` 命名空间 → Pattern B 的 `/api/v1/auth/exchange-token` 落在同一前缀。

## 10. 阅读路径

- 宏观背景 → [headless-api-track.zh-CN.md](../02-rollout/headless-api-track.zh-CN.md)
- Stage 0 现状 / 测试基线 → [STATUS.zh-CN.md](../03-impl/STATUS.zh-CN.md)
- PR8 三表 schema → [pr8-headless-api-schema.zh-CN.md](../03-impl/pr8-headless-api-schema.zh-CN.md)
- 本 spec 的实现计划 → [2026-06-28-stage-1-headless-api-pattern-a-auth-foundation.md](../../superpowers/plans/2026-06-28-stage-1-headless-api-pattern-a-auth-foundation.md)
