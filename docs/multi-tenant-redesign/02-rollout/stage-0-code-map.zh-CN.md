# Stage 0 · 当前代码地图

> 目的：把 Stage 0 必做项落到具体文件 + 行号上，让你在动手前能快速找到要改的位置。
>
> **范围**：仅 Stage 0 涉及的 7 个子系统。Stage 1+ 的代码（quota / KMS / RLS / sandbox 等）不在本文档。
>
> **配套阅读**：
> - [02-rollout/phased-rollout-by-scale.zh-CN.md](./phased-rollout-by-scale.zh-CN.md)：Stage 0 必做项清单
> - [01-redesign/adr-vs-code-audit.zh-CN.md](../01-redesign/adr-vs-code-audit.zh-CN.md)：原始审计来源（部分行号引用此处）

---

## 0. 整体链路速览

```
Browser ─→ nginx :2026 ─→ Gateway :8001
                            │
                            │  请求
                            ▼
                 ┌──────────────────────┐
                 │ CSRFMiddleware       │  写操作校验 X-CSRF-Token
                 │ AuthMiddleware       │  cookie → JWT → ContextVar
                 └──────────┬───────────┘
                            │ ContextVar 注入 _current_user
                            ▼
                 ┌──────────────────────┐
                 │ Routers              │  threads.py / thread_runs.py / auth.py / ...
                 │ @require_permission  │  owner_check via threads_meta
                 └──────────┬───────────┘
                            │
                            ▼
                 ┌──────────────────────┐
                 │ Repositories         │  resolve_user_id(AUTO) → 从 ContextVar 读
                 │ ThreadMetaRepository │  仓储自动填充 user_id 列
                 │ RunRepository        │
                 │ ...                  │
                 └──────────┬───────────┘
                            │
                            ▼  SQLAlchemy AsyncSession
                 ┌──────────────────────┐
                 │ Postgres / SQLite    │  threads_meta / runs / feedback / users
                 └──────────────────────┘

                 ┌──────────────────────┐
                 │ Agent 运行时         │  ThreadDataMiddleware → Paths.thread_dir(user_id, thread_id)
                 │ Sandbox 工具         │  replace_virtual_path 把 /mnt/... 翻译到物理路径
                 └──────────────────────┘
```

**Stage 0 的工作就是**：在这条链路上每个层级都补一个 `workspace_id` 维度，并在入口路由层强制 `(workspace_id, thread_id)` 校验。

---

## 1. Auth 体系

### 关键文件

| 文件 | 行号 | 作用 |
|---|---|---|
| `backend/app/gateway/auth/jwt.py` | `12-19` | `TokenPayload` 定义：`{sub, exp, iat, ver}` |
| `backend/app/gateway/auth/jwt.py` | `21-37` | `create_access_token()` 签发 |
| `backend/app/gateway/auth/jwt.py` | `40-55` | `decode_token()` 验证 |
| `backend/app/gateway/auth_middleware.py` | `52-127` | `AuthMiddleware`：cookie → JWT → ContextVar 注入 |
| `backend/app/gateway/auth_middleware.py` | `112,122,124-126` | 注入/重置 ContextVar 的关键三行 |
| `backend/app/gateway/csrf_middleware.py` | `169-216` | `CSRFMiddleware` 双重 cookie |
| `backend/app/gateway/authz.py` | `197-301` | `@require_permission(resource, action, owner_check)` 装饰器 |
| `backend/packages/harness/deerflow/runtime/user_context.py` | `52-167` | `CurrentUser` Protocol + ContextVar + AUTO sentinel + `resolve_user_id` |

### 关键函数

| 名称 | 位置 | 现状签名 |
|---|---|---|
| `TokenPayload` | `auth/jwt.py:12` | `BaseModel` 字段：`sub:str, exp:datetime, iat:datetime|None, ver:int` |
| `set_current_user(user)` | `user_context.py:55-62` | 注入 ContextVar，返回 reset token |
| `resolve_user_id(value, *, method_name)` | `user_context.py:138-167` | 三态：`AUTO`/explicit `str`/`None` |
| `AuthMiddleware.dispatch` | `auth_middleware.py:75-126` | cookie 检查 → JWT 解码 → user 入 ContextVar + `request.state.user` |

### 当前数据流

1. 登录：`POST /auth/login/local` 验 password → 签 JWT 含 `sub=user.id, ver=user.token_version` → 写 `access_token` cookie（HttpOnly）
2. 后续请求：`AuthMiddleware` 读 cookie → `decode_token` → 查库验 user 存在且 `ver` 匹配 → `set_current_user(user)` 注入 ContextVar → `request.state.user = user`
3. 路由：`@require_permission` 取 `request.state.user`，对 thread 资源调 `ThreadMetaStore.check_access(thread_id, user.id)`

### Stage 0 改动锚点

- `TokenPayload`：在 `jwt.py:12-19` 加 `wid: str` 字段（参 ADR-007 §8）；`create_access_token` 签发处一并加
- `AuthMiddleware.dispatch`：在 `set_current_user` 之后再调一个新的 `set_current_workspace(workspace_id)`（要新建 `workspace_context.py`，仿照 `user_context.py`）
- `decode_token`：兼容旧 4 字段 token，缺 `wid` 时强制走 `/select-workspace` 重发（参 ADR-007 §11）

---

## 2. User 数据模型 + 现有业务表

### 关键文件

| 文件 | 行号 | 作用 |
|---|---|---|
| `backend/packages/harness/deerflow/persistence/user/model.py` | `22-59` | `UserRow` ORM |
| `backend/app/gateway/auth/models.py` | `15-42` | `User` Pydantic（API 接口）|
| `backend/packages/harness/deerflow/persistence/engine.py` | `26-27,126` | engine 单例 + `AsyncSession` factory |

### 现有 ORM 模型清单（**Stage 0 全部要加 `workspace_id` 列**）

| 表 | 模型文件 | 现有 user_id 列 | 备注 |
|---|---|---|---|
| `users` | `persistence/user/model.py:22-59` | — (它就是 user 本身) | 加 `default_workspace_id`（用户登录后默认进哪个 workspace）|
| `threads_meta` | `persistence/thread_meta/model.py:18` | `user_id String(64) index` | 加 `workspace_id` + `UNIQUE(workspace_id, thread_id)` 复合索引 |
| `runs` | `persistence/run/model.py:19` | `user_id String(64) index` | 加 `workspace_id` |
| `feedback` | `persistence/feedback/model.py:21` | `user_id String(64) index` | 加 `workspace_id` |
| `run_events` | `persistence/run/model.py` (推测同目录) | (unverified — 部分版本是 DB，部分是内存)| 如果是 DB 持久化则加 `workspace_id` |

### `UserRow` 关键列

| 列 | 行号 | 现状 | Stage 0 |
|---|---|---|---|
| `id` | `model.py:26` | `String(36)` PK | 不变 |
| `email` | `model.py` | unique | 不变 |
| `password_hash` | `model.py` | nullable | 不变 |
| `system_role` | `model.py:33` | default `"user"`，可为 `"admin"` | 保留为**平台级 role**（platform_admin），不和 workspace role 混 |
| `token_version` | `model.py:49` | default 0；改密时 bump | 沿用——加入/退出 workspace 时 bump |
| `needs_setup` | `model.py:48` | default False | 不变 |
| **`default_workspace_id`** | — | (新增) | 登录后默认进哪个 workspace；可为 NULL（用户多 workspace 时强制走 picker）|

### Stage 0 改动锚点

- 新建 `persistence/workspace/model.py` + `persistence/workspace/sql.py`（仓储）
- 新建 `persistence/workspace_membership/model.py` + 仓储
- 现有 4 张表 ALTER 加 `workspace_id` 列（先 nullable，回填后改 NOT NULL）
- 写迁移脚本：所有现有 `users` 自动建 1 个 workspace（owner=自己），把 `threads_meta`/`runs`/`feedback` 的现有行 `workspace_id` 回填为对应 user 的 default_workspace_id

---

## 3. Thread 入口路由（Stage 0 强校验落点）

### 关键文件

| 文件 | 行数 | 作用 |
|---|---|---|
| `backend/app/gateway/routers/threads.py` | 622 行 | thread CRUD |
| `backend/app/gateway/routers/thread_runs.py` | 377 行 | run 创建/恢复/事件流 |

### threads.py 关键 endpoint

| 路由 | 行号 | 现状 |
|---|---|---|
| `POST /api/threads` | `224-286` | 调 `ThreadMetaRepository.create(thread_id, user_id=AUTO)` + 初始 checkpoint |
| `DELETE /api/threads/{thread_id}` | `190-221` | `_delete_thread_data` + 移 checkpoint + 删 thread_meta |
| `POST /api/threads/search` | `289+` | 委托 `ThreadMetaStore` |
| `GET /api/threads/{thread_id}` | (其他) | 状态查询 |
| `PATCH /api/threads/{thread_id}` | (其他) | 更新 metadata |

### thread_runs.py 关键 endpoint

| 路由 | 行号 | 现状 |
|---|---|---|
| `POST /api/threads/{tid}/runs` | `95-100` | `start_run()` 后台 |
| `POST /api/threads/{tid}/runs/stream` | (其他) | run + SSE |
| `POST /api/threads/{tid}/runs/wait` | (其他) | run + 阻塞 |
| `GET /.../runs/{rid}/messages` | (其他) | 分页消息 |
| `GET /.../runs/{rid}/events` | (其他) | 完整事件流 |

### 当前权限模型

所有 thread 端点都用 `@require_permission("threads", "<action>", owner_check=True)` 装饰；`owner_check=True` 触发 `ThreadMetaStore.check_access(thread_id, user.id)`，只比对 `threads_meta.user_id == current_user.id`。

### Stage 0 改动锚点

- **`@require_permission` 装饰器升级**：`owner_check` 当前是 bool，要扩成支持 `(workspace_id, thread_id)` 复合校验（参 ADR-001 §4.1.2 + ADR-004 §5.4）
- **创建路径**：`threads.py:224-286` 写 thread_meta 时也要写 `workspace_id`（从 ContextVar 读）；依赖 thread_meta 上的 `UNIQUE(workspace_id, thread_id)` 复合索引兜底
- **读/写路径**：先用 `(current_workspace_id, requested_thread_id)` SELECT thread_meta，未命中即 404
- 这两个文件就是 ADR-001 §4.1.2 修订后的"第一道防线"落点

---

## 4. ThreadDataMiddleware + 路径系统

### 关键文件

| 文件 | 行号 | 作用 |
|---|---|---|
| `backend/packages/harness/deerflow/agents/middlewares/thread_data_middleware.py` | `24-79` | 中间件创建 thread 目录树 |
| `backend/packages/harness/deerflow/config/paths.py` | `1-250` | `Paths` 类（虚拟路径系统）|
| `backend/packages/harness/deerflow/sandbox/middleware.py` | `45-63` | `SandboxMiddleware`（紧随 ThreadDataMiddleware 之后）|

### Paths 关键方法

| 方法 | 位置 | 现状返回 |
|---|---|---|
| `Paths.thread_dir(thread_id, user_id)` | `paths.py:171-189` | `{base_dir}/users/{user_id}/threads/{thread_id}/` |
| `Paths.sandbox_work_dir` | `paths.py:191-197` | `{thread_dir}/user-data/workspace/` |
| `Paths.sandbox_uploads_dir` | `paths.py` | `{thread_dir}/user-data/uploads/` |
| `Paths.sandbox_outputs_dir` | `paths.py` | `{thread_dir}/user-data/outputs/` |
| `Paths.user_memory_file(user_id)` | `paths.py:155-157` | `{base_dir}/users/{user_id}/memory.json` |
| `Paths.user_agent_dir(user_id, agent_name)` | `paths.py:163-169` | `{base_dir}/users/{user_id}/agents/{name}/` |

### ThreadDataMiddleware 关键函数

| 方法 | 位置 | 现状 |
|---|---|---|
| `_get_thread_paths(thread_id, user_id)` | `thread_data_middleware.py:52-66` | 算 workspace_path / uploads_path / outputs_path |
| `_create_thread_directories(thread_id, user_id)` | `thread_data_middleware.py:68-79` | 调 `Paths.ensure_thread_dirs` |

### 当前数据流

agent 启动 → ThreadDataMiddleware 在 `before_model` 调 `_create_thread_directories(thread_id, user_id=get_effective_user_id())` → 沙箱工具（bash 等）通过 `replace_virtual_path` 把 `/mnt/user-data/workspace/foo.txt` 翻译到 `{base_dir}/users/{user_id}/threads/{thread_id}/user-data/workspace/foo.txt`。`user_id` 没认证时 fallback `"default"`。

### Stage 0 改动锚点

| 文件 | 改什么 |
|---|---|
| `paths.py:171-189` `thread_dir()` | 路径加 workspace 维度：`{base_dir}/workspaces/{wid}/threads/{thread_id}/`（**注意**：原 `{base_dir}/users/{user_id}/threads/...` 是不可逆的迁移点，要写迁移脚本把现有目录搬到新形态）|
| `paths.py:155-157` `user_memory_file` | 同上加 `/workspaces/{wid}/users/{uid}/memory.json`（memory 仍 per-user，但放 workspace 下）|
| `paths.py:163-169` `user_agent_dir` | 同上 |
| `thread_data_middleware.py:52-79` | 取 workspace_id 也从 ContextVar（新建 `get_effective_workspace_id`），传给 Paths |

> **重要**：路径迁移是 Stage 0 的不可逆点之一。建议 Stage 0 PR 顺序里把"加路径维度"放最后一步，前面所有 PR 跑通后再切。

---

## 5. 仓储层访问模式

### 当前模式（以 `ThreadMetaRepository.create` 为例）

```python
# persistence/thread_meta/sql.py:30-56
async def create(
    self,
    thread_id: str,
    *,
    user_id: str | None | _AutoSentinel = AUTO,    # 哨兵默认值
    ...
) -> dict:
    resolved_user_id = resolve_user_id(user_id, method_name="create")
    row = ThreadMetaRow(
        thread_id=thread_id,
        user_id=resolved_user_id,
        ...
    )
```

### 关键点

- 所有仓储遵循相同模式：参数默认 `AUTO`、入口调 `resolve_user_id` 一次、WHERE/INSERT 使用 resolved 值
- 跨 ContextVar 边界（后台任务、线程）调仓储要显式传 `user_id=...` 或 `user_id=None`（绕过隔离）

### `check_access` 用法（owner_check 逻辑）

```python
# persistence/thread_meta/sql.py:74-101
async def check_access(self, thread_id: str, user_id: str, *, require_existing: bool = False):
    # SELECT 行，比 row.user_id == user_id；不匹配 raise 403
```

### Stage 0 改动锚点

- 在 `runtime/` 下新建 `workspace_context.py`，仿 `user_context.py` 提供 `_current_workspace` ContextVar + `set_current_workspace` + `resolve_workspace_id` + `_AutoSentinel`
- 所有仓储方法加 `workspace_id: str | None | _AutoSentinel = AUTO` 参数（仿 user_id 模式）
- WHERE 子句加 `workspace_id = :wid`（必须放索引前导列，复合索引重新设计）
- `check_access` 升级为 `check_access(thread_id, user_id, workspace_id, *, require_existing)`

---

## 6. Setup / 注册流程

### 后端流程

| 步骤 | 路由 / 文件 | 行号 | 现状 |
|---|---|---|---|
| 1. 检查首启 | `GET /auth/setup-status` | `auth.py:390-417` | 数 `admin_count == 0` |
| 2. 建首个 admin | `POST /auth/initialize` | `auth.py:429-458` | `system_role="admin", needs_setup=False` + auto-login |
| 3. 普通注册 | `POST /auth/register` | `auth.py:304-323` | `system_role="user"` + auto-login |
| 4. 改密码/finish setup | `POST /auth/change-password` | `auth.py:328-375` | bump `token_version`，重签 JWT |
| 5. 当前用户 | `GET /auth/me` | `auth.py:378-382` | 返回 `User` Pydantic |

### 前端

- `frontend/src/app/(auth)/setup/page.tsx`（位置 unverified，需要 ls 确认）
- `frontend/src/core/auth/server.ts:25-26` 读 `access_token` cookie 调 `/auth/me`
- `frontend/src/core/auth/proxy-policy.ts:52` cookie name `access_token`

### Stage 0 改动锚点

- `POST /auth/initialize` (`auth.py:429-458`)：建完 admin 后**同步**建 1 个默认 workspace（owner=该 admin），写 `users.default_workspace_id`
- `POST /auth/register` (`auth.py:304-323`)：每次注册新用户也建 1 个 1 人 workspace（owner=新用户）
- `POST /auth/change-password` (`auth.py:328-375`)：不变
- `GET /auth/me` (`auth.py:378-382`)：返回值加 `workspaces: [{id, name, role}]`，前端 picker 用
- 前端 setup flow：登录成功后如果用户只有 1 个 workspace，直接进；多个走 picker（Stage 1+ 的事，但 Stage 0 数据结构要支持）

---

## 7. CSRF + ContextVar 注入（一并）

### CSRFMiddleware 行为

- `csrf_middleware.py:169-216`：对 POST/PUT/DELETE/PATCH 校验
- Auth 端点（login/register/initialize）：仅校 Origin（跨源保护）
- 其他端点：要求 `X-CSRF-Token` header 与 `csrf_token` cookie 完全相等

### AuthMiddleware ContextVar 流程

| 行号 | 动作 |
|---|---|
| `auth_middleware.py:112` | `user = await get_current_user_from_request(request)` (JWT 验 + 库查) |
| `auth_middleware.py:120` | `request.state.user = user` |
| `auth_middleware.py:122` | `token = set_current_user(user)` |
| `auth_middleware.py:124-126` | `try / finally: reset_current_user(token)` |

### 当前数据流

每个 FastAPI 请求是独立 task → 独立 ContextVar context → user 注入即可让所有下游 await 链都能取到。`asyncio.create_task` 子任务自动继承父 context；`threading.Timer` 不继承（memory queue 已经显式捕获 user_id 解决）。

### Stage 0 改动锚点

- 在 `auth_middleware.py:122` 之后再调 `workspace_token = set_current_workspace(user.default_workspace_id_or_resolved_from_jwt)`
- `finally` 块同时 reset 两个
- 所有 `threading.Timer` / 后台 task 都要显式捕获 workspace_id（参考现有 memory queue 已对 user_id 做的）

---

## Stage 0 改动影响面总览

把 Stage 0 必做项（来自 02-rollout/phased-rollout-by-scale.zh-CN.md）映射到代码位置：

| Stage 0 必做项 | 主要文件 | 涉及 §节 |
|---|---|---|
| `workspaces` 表 + 自动建 1 人 workspace | 新建 `persistence/workspace/{model,sql}.py`；改 `auth.py:429-458` `/auth/initialize` 与 `auth.py:304-323` `/auth/register` | §2 + §6 |
| `workspace_id` 列加到现有表 | `thread_meta/model.py:18`、`run/model.py:19`、`feedback/model.py:21`、`user/model.py`（加 `default_workspace_id`）+ 迁移脚本 | §2 |
| `workspace_memberships` 表 | 新建 `persistence/workspace_membership/{model,sql}.py` | §2 |
| JWT 扩 `wid` 字段 | `auth/jwt.py:12-19,21-37,40-55` | §1 |
| 入口路由 `(workspace_id, thread_id)` 校验 | `routers/threads.py:224-286,190-221`、`routers/thread_runs.py:95-100`；`authz.py:197-301` 装饰器升级 | §3 |
| ThreadDataMiddleware 加 workspace 维度 | `thread_data_middleware.py:52-79`、`config/paths.py:171-197,155-169` + 文件系统迁移脚本 | §4 |
| 仓储层加 workspace_id 哨兵 | 新建 `runtime/workspace_context.py`；所有 `persistence/*/sql.py` 加参数 | §5 |
| AuthMiddleware 注入 workspace ContextVar | `auth_middleware.py:122,124-126` | §1 + §7 |
| Setup flow 自动建 workspace | `auth.py:429-458,304-323` | §6 |

---

## 不可逆决策落点（动手前想清楚）

| 决策 | 一旦合入难回头的原因 |
|---|---|
| `workspaces` 表 schema 字段（slug / plan / status / 自定义域名预留列） | 改 schema 要写迁移；如果 URL 用了 slug，所有用户书签失效 |
| `workspace_id` 列类型（UUID v7 vs string vs int） | 一致性；和 `users.id` 类型对齐 |
| JWT `TokenPayload` 字段集（一次性想清楚 `wid + role + plan` 都加上还是分次加）| 每次加字段 bump `ver` 让所有用户重新登录；少一次 churn 用户体验好 |
| 文件系统路径形态（`workspaces/{wid}/threads/{tid}/` vs `tenants/{tid}/...`） | 改了所有用户产物 URL 失效；迁移脚本要重写 |
| ContextVar 命名（`_current_workspace` 还是 `_current_tenant`） | 影响 import 全链路；和 ADR 用语保持一致（建议用 `workspace`，因为产品概念用了它）|

---

## 推荐阅读顺序（最快上手）

按依赖关系从底层到上层读，每读完一份能对下一份有更准确的预期：

1. **`runtime/user_context.py:52-167`** — AUTO sentinel 模式是整个改造的核心模式，先吃透
2. **`persistence/user/model.py:22-59`** — 看清 UserRow 现有字段
3. **`persistence/thread_meta/{model.py,sql.py}`** — 选一个仓储看完整 CRUD pattern
4. **`auth/jwt.py:12-55`** — TokenPayload + sign/decode（最小、5 分钟读完）
5. **`app/gateway/auth_middleware.py:75-126`** — 看 ContextVar 怎么注入
6. **`app/gateway/authz.py:197-301`** — `@require_permission` 装饰器实现
7. **`app/gateway/routers/threads.py:190-286`** — Stage 0 强校验的落点 1
8. **`app/gateway/routers/thread_runs.py:95-100`** — Stage 0 强校验的落点 2
9. **`app/gateway/routers/auth.py:304-458`** — register / initialize / change-password
10. **`agents/middlewares/thread_data_middleware.py` + `config/paths.py:155-197`** — 文件系统迁移用得到
11. **`app/gateway/csrf_middleware.py:169-216`**（可选，CSRF 现成不太需要改）

预计阅读时间：1.5–2.5 小时（粗读）/ 半天（精读 + 跑一遍 dev 调用链）

---

## 出现疑问时

- ADR 现状假设错了 → 检查 [adr-vs-code-audit.zh-CN.md](../01-redesign/adr-vs-code-audit.zh-CN.md)
- LangGraph saver 注入相关 → [adr-spike-langgraph-postgres.zh-CN.md](../01-redesign/adr-spike-langgraph-postgres.zh-CN.md)
- 改造方向 → [adr-001](../01-redesign/adr-001-data-isolation.zh-CN.md) §4.1.2 + [adr-007](../01-redesign/adr-007-routing-frontend.zh-CN.md) §8
