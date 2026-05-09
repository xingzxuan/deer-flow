# Headless API · 业务系统集成轨道

> 写于 2026-05-09。承接 [phased-rollout-by-scale.zh-CN.md](./phased-rollout-by-scale.zh-CN.md)。
>
> **触发**：现已有 1-2 个明确的业务系统集成需求，1-3 个月内要 demo / 调通。集成形态包括 IM channels（已支持）+ 业务系统自研 web 页面。
> **商业形态**：SaaS + on-prem 双主线。
> **身份模式**：service account 折叠 + external_user_id 透传，两种都支持，按 endpoint 选。
> **集成 pattern**：**Pattern A（业务系统 backend 代理）+ Pattern B（浏览器直连 + 短期 JWT）**。不做嵌入式 widget。

---

## 0. 现状评估

DeerFlow 架构已经接近"可被业务系统调用的后端"——证据：`backend/app/channels/` 下的 IM 集成（Slack / 飞书 / 钉钉 / Telegram）就是这种用法的活体范例。它们通过 `langgraph-sdk` 调 Gateway HTTP API + 把响应转给 IM 平台，**根本不经过 `frontend/` 工程**。

### 已经具备（约 80%）

| 能力 | 实现位置 |
|---|---|
| 完整 REST API | `backend/app/gateway/routers/`（threads / runs / messages / events / feedback / models / skills / mcp / memory / uploads / artifacts）|
| LangGraph SDK 兼容路径 | `/api/langgraph/*` —— 任何 `langgraph-sdk` 客户端可直接接 |
| Streaming（SSE） | `runs/stream` + `messages-tuple` delta + `values` + `custom` |
| 嵌入式 Python 客户端 | `packages/harness/deerflow/client.py` `DeerFlowClient` |
| 现成的"无 web 前端"调用证明 | `app/channels/manager.py` 完全不依赖 frontend |

### Stage 0 完成后还差什么

Stage 0 把 workspace 概念立起来了，但 **auth 模式仍是 cookie + JWT + CSRF**——这是为 web 前端设计的，不适合 server-to-server，更不适合"业务系统自研 web 页面浏览器直连"的场景。要让业务系统调，必须补两条平行 auth 路径 + 一组管理能力，详见 §1。

### 集成 pattern 速览

| Pattern | 链路 | 适用 | 优先级 |
|---|---|---|---|
| **A. Backend 代理**（默认） | browser → 业务系统 backend → DeerFlow API key → DeerFlow | 业务系统已有 backend；不在乎多一跳 | Stage 1 必做 |
| **B. Browser 直连**（streaming 友好） | 业务系统 backend 颁短期 JWT → browser 直连 DeerFlow `/api/v1/*`（含 SSE） | chat / agent 类、对 token 流延迟敏感、自研 web 页面 | Stage 1 末必做 |
| ~~C. 嵌入式 widget / iframe~~ | DeerFlow 托管 chat widget URL，业务系统 embed | 集成方零前端开发 | **不做**（产品决定） |

Pattern A 是"server-to-server"，Pattern B 是"browser-to-server"。两者共用同一组 service account / API key 数据模型，**只是 auth 路径不同**：
- Pattern A：`Authorization: Bearer dfk_live_<long-lived API key>`
- Pattern B：`Authorization: Bearer eyJ<short-lived JWT>`

---

## 1. 改造清单（按 MVP 优先级）

| # | 能力 | 是否 MVP | 缺它会怎么样 | 工作量 |
|---|---|---|---|---|
| 1 | **API Key 认证（Pattern A）** | **必须** | 业务系统 backend 没法调，跨域 + CSRF 灾难 | M |
| 2 | **Service Account 概念** | **必须** | API key 必须挂在某个"账户"上做归属、计费、quota | M |
| 3 | **CSRF bypass on bearer** | **必须** | bearer 路径走 CSRF middleware 直接 403 | XS |
| 4 | **External User ID 透传** | **必须** | 业务系统的"小明"在 DeerFlow 内不能体现，memory / 个性化失效 | M |
| 5 | **API 版本化 `/api/v1/`** | **必须**（早做便宜）| 演进时业务系统要全量改 | S（早做）/ L（晚做）|
| 6 | **Rate limit per API key** | **必须**（基础版） | 业务系统 bug 把 DeerFlow 打爆 | S（基础）/ M（分层）|
| 7 | **Token Exchange（Pattern B）** | **必须** | 浏览器只能走 backend 代理，自研 web 页面延迟差 | S |
| 8 | **CORS 中间件 + per-workspace allowed_origins** | **必须**（与 Pattern B 配套）| 浏览器请求被同源策略挡 | M |
| 9 | **短期 JWT 验证路径**（AuthMiddleware 第三条） | **必须**（与 Pattern B 配套）| 短期 JWT 没法验 | S |
| 10 | **Idempotency keys** | 推荐 | 业务系统重试时重复建 thread/run | S |
| 11 | **Webhook outbound** | 推迟 | 业务系统轮询事件，多调几次 SSE 而已 | M（推到 Stage 2）|

**MVP 包**（前 9 项）= **4-5 周**；放进 Stage 1 并行做。Pattern B（7-9）依赖 1-3 完成，建议 Stage 1 末（最后 1-2 周）。

---

## 2. 核心设计：API Key + Service Account

### 数据模型

```sql
service_accounts (
  id UUID PK,
  workspace_id UUID FK NOT NULL,        -- 必属于一个 workspace
  name VARCHAR(64),                      -- "X 业务系统集成"
  role VARCHAR(16),                      -- 在 workspace 内的 role：member / admin
  identity_mode VARCHAR(16),             -- collapsed | external_passthrough | both
  status VARCHAR(16),                    -- active / suspended / revoked
  created_by UUID,                       -- 哪个 user 创建的（必须是 workspace owner/admin）
  created_at, updated_at
)

api_keys (
  id UUID PK,
  service_account_id UUID FK NOT NULL,
  key_prefix VARCHAR(16) UNIQUE,         -- 前 16 字符明文（dfk_live_abc123...）UI 可显示
  key_hash BYTEA NOT NULL,               -- 完整 key 的 sha256，比对用
  name VARCHAR(64),                      -- "生产环境 key" / "灰度 key"
  scopes TEXT[],                         -- ["threads:read", "runs:create", "uploads:write"...]
  rate_limit_rpm INT NULL,               -- 每分钟请求数；NULL=用 workspace plan 默认
  expires_at TIMESTAMP NULL,             -- 可选过期时间
  last_used_at TIMESTAMP NULL,
  revoked_at TIMESTAMP NULL,
  created_at
)

external_users (                         -- ghost user，按需建（identity_mode=external_passthrough 时）
  id UUID PK,
  workspace_id UUID FK NOT NULL,
  service_account_id UUID FK NOT NULL,
  external_id VARCHAR(128) NOT NULL,     -- 业务系统传过来的 ID，原样存
  display_name VARCHAR(128) NULL,
  metadata JSONB,                        -- 可选业务字段
  created_at, last_active_at,
  UNIQUE (workspace_id, service_account_id, external_id)
)
```

### Key 格式约定

```
dfk_live_<24 字符随机>     # 生产 key
dfk_test_<24 字符随机>     # 测试 key
```

- 前缀 `dfk_live_` / `dfk_test_` 让一眼区分环境（防止把测试 key 投进生产）
- 写入 DB 时只存 `sha256(key)`，明文创建后只能在 UI 显示一次
- `key_prefix` 列存前 16 字符（`dfk_live_abc12345`）—— UI 列表 + 审计日志可识别但不能用

### 认证流程

```
请求 → AuthMiddleware → 检测 Authorization header
                          │
                          ├── "Bearer dfk_..."
                          │     ↓
                          │     APIKeyAuthBackend.authenticate
                          │     ↓
                          │     SELECT api_keys WHERE key_hash = sha256(token)
                          │     ↓
                          │     load service_account + workspace
                          │     ↓
                          │     set_current_workspace(workspace_id)
                          │     set_current_service_account(account)
                          │     set_current_user(None)         # 没有真人 user
                          │     ↓
                          │     CSRFMiddleware skip（bearer 路径不要 CSRF）
                          │     ↓
                          │     如有 X-External-User-Id header 且 identity_mode 允许：
                          │       → upsert external_users → set_current_external_user
                          │
                          └── "Cookie: access_token=..." → 走现有 web 前端流程
```

### `@require_permission` 装饰器升级

```python
# 现有签名（cookie 模式）
@require_permission("threads", "read", owner_check=True)

# 改为同时支持 service account 路径
@require_permission(
    resource="threads",
    action="read",
    scopes=["threads:read"],            # API key 必须有此 scope
    owner_check="workspace_or_user",    # SA: 校验 workspace 归属；user: 校验 user_id
)
```

---

## 3. 核心设计：两种身份模式

### 模式 A：service account 折叠（identity_mode=`collapsed`）

业务系统调一切操作都归到该 service account 名下。**适合工具类集成**（CRM 自动总结、安全面板分析）。

```
POST /api/v1/threads
Authorization: Bearer dfk_live_xxx

→ 创建 thread，所有归属都是 service_account_id
  threads_meta.user_id = NULL
  threads_meta.service_account_id = <SA.id>
  threads_meta.workspace_id = <SA.workspace_id>
```

memory 也是 service account 共享的（`workspaces/{wid}/service_accounts/{sa_id}/memory.json`）。

### 模式 B：external_user_id 透传（identity_mode=`external_passthrough`）

业务系统的"小明"在 DeerFlow 内独立。**适合 chatbot / 助手类集成**。

```
POST /api/v1/threads
Authorization: Bearer dfk_live_xxx
X-External-User-Id: bizsys_user_42         # 业务系统的用户 ID

→ AuthMiddleware：
   1. 解 API key → load SA
   2. 看 SA.identity_mode 允许 external_passthrough
   3. SELECT external_users WHERE (workspace_id, service_account_id, external_id="bizsys_user_42")
   4. 没找到 → 自动建 ghost external_user
   5. set_current_external_user(...)

→ 创建 thread：
   threads_meta.workspace_id = <SA.workspace_id>
   threads_meta.service_account_id = <SA.id>
   threads_meta.external_user_id = <external_users.id>
```

memory 是 per external_user 的（`workspaces/{wid}/service_accounts/{sa_id}/external_users/{eu_id}/memory.json`）。

### 模式选择策略

`SA.identity_mode` 三态：
- `collapsed`：忽略所有 `X-External-User-Id` header
- `external_passthrough`：必须传 `X-External-User-Id`，缺失时 400
- `both`：传了走透传、不传走折叠（最灵活但最复杂；建议默认不开放）

业务系统接入时由 workspace owner 创建 SA 时选定。

### 这影响哪些 endpoints

| endpoint | service account 折叠 | external_user_id 透传 |
|---|---|---|
| `POST /threads` | thread 归 SA | thread 归 external_user |
| `GET /threads` | 列出 SA 的所有 thread | 仅列出该 external_user 的 thread |
| memory 注入 | SA 共享 memory | 该 external_user 的 memory |
| `usage_daily` 写入 | `(workspace_id, SA_id, "main", ...)` | 同上 + `external_user_id` 维度 |
| feedback | feedback.user_id 留空，标 SA | feedback.external_user_id 标人 |

---

## 3.5 核心设计：Pattern B（Browser 直连 + 短期 JWT 交换）

### 为什么需要

业务系统自研的 web 页面如果走 Pattern A（Backend 代理），他们 backend 必须实现 SSE 流式转发——这是个不小的工程量，且每个 token 多一跳延迟。**Pattern B 把 streaming 直接交给浏览器**，业务系统 backend 只做一次性 token 颁发。

### 数据模型扩展

```sql
-- workspaces 表加列
workspaces.allowed_origins TEXT[]    -- ["https://app.partner.com", "https://staging.partner.com"]

-- 不需要新表；短期 JWT 不持久化（足够短就不需要 revoke list）
```

### Token Exchange 流程

```
[业务系统 backend]                     [DeerFlow]                       [浏览器]
       │                                  │                                │
       │  1. 用户在业务系统登录            │                                │
       │ ◄────────────────────────────────│────────────────────────────────│
       │                                  │                                │
       │  2. 业务 backend 鉴权后调       │                                │
       │     POST /api/v1/auth/exchange-token                            │
       │     Authorization: Bearer dfk_live_xxx                          │
       │     { external_user_id: "ming_42", expires_in: 600 }            │
       │ ────────────────────────────────►│                                │
       │                                  │                                │
       │                                  │ 3. 校验 API key + SA 状态      │
       │                                  │    upsert external_users 行    │
       │                                  │    签短期 JWT                  │
       │                                  │                                │
       │  { access_token: "eyJ...",       │                                │
       │    expires_at: "..." }           │                                │
       │ ◄────────────────────────────────│                                │
       │                                  │                                │
       │  4. 把 access_token 发给浏览器    │                                │
       │ ─────────────────────────────────────────────────────────────────►│
       │                                  │                                │
       │                                  │  5. browser 直连 DeerFlow      │
       │                                  │     GET /api/v1/threads/.../events│
       │                                  │     Authorization: Bearer eyJ...│
       │                                  │     Origin: https://app.partner.com│
       │                                  │ ◄──────────────────────────────│
       │                                  │                                │
       │                                  │  6. CORS preflight 通过        │
       │                                  │     SSE stream 200             │
       │                                  │ ──────────────────────────────►│
```

### 短期 JWT 设计

```python
# 不复用现有 cookie JWT 的 TokenPayload，新增 ServiceTokenPayload
class ServiceTokenPayload(BaseModel):
    sub: str                  # external_user_id（DeerFlow 内部 id，不是业务方原始 id）
    sa: str                   # service_account_id
    wid: str                  # workspace_id
    eid: str                  # external_id（业务方原始 id，传给 audit log）
    scopes: list[str]         # 从 api_key.scopes 继承（不能放大）
    exp: int                  # 5-15 min（默认 10 min）
    iat: int
    iss: "deerflow"           # 区分自签 vs 业务方签
    typ: "service"            # 区分 cookie JWT（typ=user）
```

### Endpoint 规格

```
POST /api/v1/auth/exchange-token
Authorization: Bearer dfk_live_xxx        (API key)
Content-Type: application/json

Request:
{
  "external_user_id": "ming_42",          // 必填（identity_mode=external_passthrough）
  "expires_in": 600,                       // 可选，默认 600s，最大 3600s
  "scopes": ["threads:write", "runs:read"] // 可选；省略则继承 API key 全部 scopes
}

Response 200:
{
  "access_token": "eyJ...",
  "token_type": "Bearer",
  "expires_at": "2026-05-09T15:20:00Z"
}

Errors:
- 401 invalid API key
- 403 SA suspended / API key revoked
- 400 identity_mode 不允许 external_user_id（collapsed 模式）
- 400 scopes 超出 API key 授权
```

### AuthMiddleware 第三条路径

```
请求 → AuthMiddleware → 检测 Authorization
                          │
                          ├── "Bearer dfk_..."        → APIKeyAuthBackend (Pattern A)
                          ├── "Bearer eyJ...typ=service" → ServiceTokenAuthBackend (Pattern B)  ← 新增
                          └── "Cookie: access_token=..." → CookieAuthBackend (web 前端)
```

`ServiceTokenAuthBackend.authenticate`：
1. 验 JWT 签名（DeerFlow 自签私钥；HS256 即可，不需要 RSA）
2. 检 `iss=deerflow, typ=service`（防止把 cookie JWT 误用）
3. 校 `sa` service account 状态（可能在签发后被 suspend）
4. 校 scope 子集合法（不能超过 SA 当前 scopes）
5. `set_current_workspace(wid)` + `set_current_service_account(sa)` + `set_current_external_user(sub)`

### CORS 设计

```python
# CORSMiddleware 在 AuthMiddleware 之前加载（FastAPI 中间件顺序）
class WorkspaceAwareCORSMiddleware:
    async def dispatch(self, request, call_next):
        origin = request.headers.get("origin")
        if not origin:
            return await call_next(request)        # 非浏览器请求
        
        # 解析当前 workspace（从 token 或 query string）
        # CORS preflight (OPTIONS) 没有 token，要从其他维度推
        # 简化方案：所有 /api/v1/* 路径在 OPTIONS 时回 wildcard，但不带 credentials；
        # 真请求时按 token 上的 wid 查 workspaces.allowed_origins 做精确匹配
        ...
```

**关键安全点**：
- `Access-Control-Allow-Credentials: false`（短期 JWT 不依赖 cookie，不需要 credentials；防止意外打开 cookie 跨域）
- `Access-Control-Allow-Origin` 精确匹配，不用通配
- `Access-Control-Max-Age` 短一点（5 分钟），方便切换 origin 时不被缓存卡

### 与现有 CSRF 的关系

CSRFMiddleware 检测到 `Authorization: Bearer ...` 直接 skip——Pattern A 和 Pattern B 都走同一条 skip 逻辑。CSRF 仅对 cookie 路径生效。

### Token revoke 策略

短期 JWT 默认**不显式 revoke**（5-15 min 过期，自然失效）。但有三个例外：
1. SA `status=suspended` → ServiceTokenAuthBackend 在 step 3 检测时直接拒
2. API key 被 `revoked_at` → 同上（短期 JWT 验证时关联回 `sa.api_key`）
3. 如果客户提需要立即 revoke 用户访问 → 业务系统调 DeerFlow `POST /api/v1/external-users/{id}/revoke-tokens` 把 `external_users.token_version` bump（`exchange-token` 签发时把它写进 JWT，验证时比对）

> 第 3 条是 nice-to-have；MVP 不做，等业务方明确提需求再加。

### Pattern B 不做的事（重要）

- **不**让浏览器直接持有 API key（`dfk_live_*`）——再短的 TTL 也不行；API key 必须留在业务方 backend
- **不**支持 OAuth 2.0 完整 flow（authorize / consent / refresh）——太重，业务方 backend 自己做完用户认证再换 token 即可
- **不**做客户端 SDK——给一份 OpenAPI + 浏览器原生 fetch / EventSource 例子，业务方自己接

---

## 4. 核心设计：API 版本化

**早做的成本**：仅是 `app/gateway/routers/__init__.py` 里 mount prefix 改 `/api/v1`。
**晚做的成本**：所有业务系统 client 全量改地址。

### 设计

```python
# 现状
app.include_router(threads_router, prefix="/api/threads")

# Stage 1 改造
app.include_router(threads_router, prefix="/api/v1/threads")
# 同时保留 /api/threads → 转发到 v1（前端用），sunset 在 Stage 3
```

**deprecation 策略**：
- `/api/*`（无版本）路径在 response 加 `X-API-Deprecated: 2027-01-01` header
- frontend 同步迁到 `/api/v1`
- LangGraph SDK 兼容路径 `/api/langgraph/` 不带版本（跟随上游 SDK 约定）

**版本演进规则**：
- 加字段：v1 内做，不升 v2
- 改语义、删字段、改默认值：v2
- 整体 endpoints 重组：v2

---

## 5. 核心设计：Rate Limit + Idempotency

### Rate Limit（基础版）

```
api_keys.rate_limit_rpm 设了值：用 key 自己的
没设：用 workspace plan 默认（free=60, pro=600, team=3000，可配）

实现：进程内 sliding window（依赖 Postgres 即可，不需要 Redis）：
  rate_limit_log(api_key_id, minute_bucket, count) 单独小表
```

进阶（Stage 2）：分维度（per endpoint、per LLM、per sandbox quota）的复合 rate limit；引入 Redis。

### Idempotency Keys

```
请求带 Idempotency-Key: <client-generated-uuid>
                          ↓
SELECT idempotency_records WHERE (api_key_id, key=...) AND created_at > NOW() - 24h
                          ↓
命中 → 直接返回上次 response（200 + body 原样）
未命中 → 处理请求 → 写 idempotency_records + 返回
```

只在写操作（POST / PUT / PATCH / DELETE）支持；GET 不需要。

---

## 6. 与 Stage 1 的整合

把 headless API MVP 包并入 Stage 1，**时间盒从 6-10 周延到 8-13 周**。

### 修订后 Stage 1 必做项

| 改动 | 类型 | 估工 |
|---|---|---|
| Postgres 切换 | 原 Stage 1 | M |
| Quota 系统 + TokenUsage 持久化 | 原 Stage 1 | M+ |
| Stripe 基础订阅 | 原 Stage 1 | M |
| AioSandbox 出网/资源收紧 | 原 Stage 1 | M |
| **API Key + Service Account 数据模型 + 仓储** | **新增（Pattern A）** | M |
| **APIKeyAuthBackend + AuthMiddleware 双路径** | **新增（Pattern A）** | M |
| **CSRF skip on bearer** | **新增** | XS |
| **External User ID 透传 + ghost user** | **新增** | M |
| **`/api/v1/` 版本化** | **新增**（早做便宜）| S |
| **Per-API-key rate limit（基础版）** | **新增** | S |
| **`@require_permission` 装饰器升级支持 SA 路径** | **新增** | S |
| **API key 管理 UI（workspace settings 内）** | **新增**（最低限：CLI 也行）| S（CLI）/ M（UI）|
| **`POST /api/v1/auth/exchange-token` endpoint** | **新增（Pattern B）** | S |
| **`ServiceTokenAuthBackend`（短期 JWT 验证）** | **新增（Pattern B）** | S |
| **`workspaces.allowed_origins` + CORS 中间件** | **新增（Pattern B）** | M |
| **Idempotency keys** | **可选**（推荐） | S |

合计原 Stage 1（M+M++M+M=4M）+ 新增 Pattern A（M+M+XS+M+S+S+S+S=4M）+ 新增 Pattern B（S+S+M=2M）= 约 10M-15 周。Pattern B 依赖 Pattern A 完成，建议放 Stage 1 末。

### 修订后 Stage 1 PR 顺序

**轨道一：付费 SaaS 基础**（与下面并行）
1. Postgres 切换（dev → 灰度 → 全切）
2. `workspace_quotas` / `workspace_usage_daily` 表 + 仓储
3. `TokenUsageMiddleware` 升级为持久化
4. `QuotaMiddleware` 加入中间件链
5. Stripe webhook + 订阅状态同步
6. AioSandbox 收紧
7. 基础监控

**轨道二：Headless API Pattern A**（与轨道一并行；步骤 1 必须先完成轨道一的 1）
1. `service_accounts` + `api_keys` + `external_users` 仓储（**先于业务路径**）
2. `APIKeyAuthBackend` + `AuthMiddleware` 双路径（cookie + bearer）
3. CSRF middleware skip on bearer
4. `/api/v1/` mount prefix 切换 + 旧路径兼容转发
5. `external_user_id` 透传机制（依赖 1-4）
6. `service_accounts.identity_mode` 三态行为分支
7. `@require_permission` 升级 + scope 校验
8. 基础 rate limit
9. API key 管理 CLI + UI
10. Idempotency keys（可选）

**轨道三：Headless API Pattern B**（依赖轨道二的 1-7 完成；建议 Stage 1 最后 1-2 周）
1. `workspaces.allowed_origins` 列 + workspace settings UI 的 origin 管理
2. `WorkspaceAwareCORSMiddleware`（在 AuthMiddleware 之前）
3. `POST /api/v1/auth/exchange-token` endpoint + `ServiceTokenPayload` 设计
4. `ServiceTokenAuthBackend`（AuthMiddleware 第三条路径）
5. SSE 在 CORS 跨域下的 streaming 验证（写一个 fixture web 页面测）
6. 业务方文档：从换 token 到浏览器直连的端到端示例（HTML + JS）

---

## 7. SaaS vs on-prem 差异

| 能力 | SaaS 形态 | on-prem 形态 |
|---|---|---|
| API key 管理 | workspace settings UI + CLI | CLI 必须；UI 可选；env var 注入预置 key 也合理 |
| 配额 / 计费 | 按 plan，Stripe 同步 | 配额作为容量管理（防内部失控），不接 Stripe |
| KMS | AWS / 阿里云 KMS（Stage 2 后落） | 客户自带 KMS（HashiCorp Vault / 客户自有）；env var 兜底（明文）|
| 监控 | 你们运维的 Grafana | 客户自有；DeerFlow 暴露 `/metrics` Prometheus 端点（Stage 2 加）|
| Webhook | 平台默认 | 客户内网回调；要求 url 可配置 |
| Rate limit | 平台分档强制 | 客户自定义；默认放宽 |
| 升级路径 | 你们灰度发布 | docker image tag + 升级文档；schema migration 跑通 |

**重要**：Stage 0 的 schema 设计已经兼容两者（workspace 是 self-hosted 时也 1 对 1 对应一个安装）。**不需要为 on-prem 单独建分支**。

### on-prem 专属工作（Stage 1+ 一次性）

- docker compose 模板（已部分有，加 service_accounts seed 流程）
- "首次安装预置 1 个 workspace + 1 个 admin + 1 个 platform key" 的 init script
- 升级脚本（schema migrations 自动跑）
- 部署文档（一份就够）

工作量约 1 人周，可以在 Stage 1 末或 Stage 2 头做。

---

## 8. 不可逆决策（动手前想清楚）

| 决策 | 难回头的原因 |
|---|---|
| API Key 格式（前缀 `dfk_` / 长度 / 加密路径）| 业务系统接入后改格式所有 key 全失效 |
| `service_accounts` 表是否能跨 workspace（暂定不允许） | 改了所有 quota / 计费归属逻辑 |
| `X-External-User-Id` header 名 | 业务系统集成后改 header 名要全部联调 |
| `identity_mode` 三态语义（collapsed / external / both） | 改语义要联调所有业务系统 |
| `/api/v1/` 是不是从 Stage 1 第 5 步开始；具体 deprecation 时间 | 业务系统接了之后再要求换 prefix 很不友好 |
| ghost user 自动创建策略（X-External-User-Id 缺失时拒绝还是 fallback collapsed） | 改了业务系统接入测试要重跑 |
| memory 隔离粒度（SA 共享 vs per external_user） | 一旦客户用上，迁移 memory 数据极麻烦 |
| **短期 JWT TTL 默认值**（5 / 10 / 15 min）和 max | 业务系统集成后调短会断当前会话；调长会留更大被偷窃的窗口 |
| **`ServiceTokenPayload` 字段集**（claim 名 / 是否带 `eid`） | 改 claim 名所有签名校验失败；早一点把审计需要的字段都设计进去 |
| **`workspaces.allowed_origins` 是 workspace 级还是 SA 级** | 暂定 workspace 级（更简单）；改成 SA 级要拆数据 |

---

## 9. 推荐推进路径

**第 0-1 周**：
- 找现有需要集成的业务系统聊一下，把"两种身份模式哪个适合 / 是否有 webhook 需求 / 是否有 rate limit 偏好"问清楚
- 写一份 API key 创建 + 第一次 hello-world 调用的快速指南草稿（不写代码，纯设计验证）

**第 2-3 周（Stage 0 末）**：
- Stage 0 收尾时把 `service_accounts` / `api_keys` / `external_users` schema 也加上（schema 加上，路径不接）

**第 4-13 周（Stage 1）**：
- 按上面的 13 步 PR 顺序推进
- 第 6-8 周可以拉一个业务系统做内测集成（用真实 API key 调真实 endpoint）
- 第 12-13 周收尾，给业务系统正式 go-live

**与 Stage 1 业务客户上线的关系**：
- Stage 1 一头是付费 SaaS 客户、一头是业务系统集成客户。两条产品线**共用同一套** workspace + auth + quota，**只是认证模式不同**（cookie vs bearer）
- 因此架构上没有冲突，团队可以并行推

---

## 10. 与现有 ADR / rollout 的关系

| 引用 | 关系 |
|---|---|
| [adr-007 §6 路由](../01-redesign/adr-007-routing-frontend.zh-CN.md) | 路由层假设 cookie + JWT；本文档加了 bearer + API key 平行路径 |
| [adr-007 §8 Auth](../01-redesign/adr-007-routing-frontend.zh-CN.md) | TokenPayload 扩 `tid/role` 已对齐；本文档新增 API key 路径不复用 JWT |
| [adr-004 RBAC](../01-redesign/adr-004-tenant-rbac.zh-CN.md) | service_accounts.role 复用 owner/admin/member；不另建 role 体系 |
| [adr-003 §4.4 quota](../01-redesign/adr-003-llm-key-billing.zh-CN.md) | quota 写入按 SA 归属时只看 workspace_id；按 external_user 归属时多一维 |
| [phased-rollout-by-scale Stage 1](./phased-rollout-by-scale.zh-CN.md) | 本文档 §6 给出 Stage 1 修订后必做项与 PR 顺序 |
| [stage-0-code-map](./stage-0-code-map.zh-CN.md) | Stage 0 schema 时把 `service_accounts` 也加上不阻塞 |
