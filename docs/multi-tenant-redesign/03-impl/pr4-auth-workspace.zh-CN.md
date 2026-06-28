# PR4 · 注册改造 + JWT 加 wid + AuthMiddleware ContextVar 注入 + /auth/me + alembic 0001

> 实现笔记。对应 [docs/superpowers/plans/2026-05-10-stage-0-multi-tenant-foundation.md](../../superpowers/plans/2026-05-10-stage-0-multi-tenant-foundation.md) PR4（T4.1-T4.14）。
>
> 状态：**已落地**，13 个 feat/test commits + 1 个 docs commit 提交到 `docs/multi-tenant-redesign`（`d98498b7..5c7753c0`）。

## 范围

PR4 把 PR3 的 schema 接到真实的 auth 流程上：每个新注册用户立刻获得 1 人 workspace + owner membership；JWT 携带 `wid` + `role`；中间件把 workspace 塞进 request-scoped ContextVar；`/auth/me` 返回 memberships 列表。同时落 alembic baseline（0001 — `users.default_workspace_id`）。

**不在范围**（后续 PR）：
- ALTER `threads_meta` / `runs` / `feedback` 加 `workspace_id`（PR5）
- 入口路由 `(wid, tid)` 校验 + `Paths` 切 workspace 维度（PR6）
- 前端 picker / role 真开放 admin/member / invitation 流程（Stage 1/2）

## 验收

- [x] **38 个新单测全过**：4 alembic + 2 jwt_workspace + 2 legacy_token_compat + 13 (existing test_auth/test_langgraph_auth/test_auth_errors 更新) + 4 middleware_workspace + 16 workspace_slug + 3 register_creates_workspace + 2 change_password_keeps_wid + 2 auth_me_returns_workspaces + 3 ensure_admin_user_workspace_backfill
- [x] 全套 `pytest tests/` **3136 passed + 26 skipped + 0 新 fail**（PR3 末 3134 + 25）；预存的 16 个 caplog 排序 flake 与 PR4 无关（已通过 stash 实验确认）
- [x] alembic 0001 在 SQLite + Postgres testcontainers 上 upgrade/downgrade 双驱动都过（PG 用例自动 skip 当 docker 未起）
- [x] JWT 新合同：legacy 4 字段 token → `TokenError.WORKSPACE_MISSING` → middleware 401 with code `WORKSPACE_REQUIRED`；新 token 解码 → `TokenPayload(wid, role)`
- [x] 注册流程端到端 (test_register_creates_workspace.py)：POST `/initialize` / `/register` 都建 workspace + owner membership + 设 `users.default_workspace_id` + JWT 含 `wid`
- [x] `/auth/me` 返回 `workspaces[{id, name, slug, role}]` + `default_workspace_id`
- [x] Slug 算法：`auto_slug_from_email` 已知输入确定性映射；`-2/-3` 冲突回退；32 字符限制时 base 自动截断；黑名单 slug（如 `admin`）被 walker 当成"已占用"自动跳到 `admin-2`
- [x] Pre-PR4 admin upgrade path：`_ensure_admin_user` lifespan hook idempotent 回填 workspace
- [ ] 真机 `make dev` smoke — **待用户**（agent 无法实际起 gateway daemon）

## 关键决策（与 plan §横切 + LOCK 清单对齐）

| 项 | 选择 | 理由 |
|---|---|---|
| TokenPayload `wid` / `role` 字段类型 | `str \| None = None`（optional 在模型层） | 既保留 legacy token 解码能力（用于在 decode_token 中 explicit reject）又不让现有调用者全栈崩溃；wid 的**强制性**靠 decode_token + middleware 实现，不靠 pydantic 字段 |
| `WorkspaceMissingError` 实现 | 加 `TokenError.WORKSPACE_MISSING` 枚举值 + `AuthErrorCode.WORKSPACE_REQUIRED` 映射 | plan 写"new error class"但实际语义跟 enum 一致；用 enum 与现有 TokenError 风格保持一致 |
| `EXPIRED` 优先级 | jwt.decode 抛 `ExpiredSignatureError` 时立即返回 EXPIRED，不进 wid 检查 | 过期 token 的修复路径是 refresh，不是 select-workspace；优先级搞错会把用户引到错误流 |
| `create_access_token` 签名 | wid / role 作为 keyword-only 参数（在 `*` 后）；老 positional `token_version` 不变 | 让前 PR4 的 ~15 个 call site（生产路由 + 测试）继续可用；T4.6 才把测试 call site 一次性更新到带 wid |
| ContextVar 注入位置 | `auth_middleware.dispatch` 内，紧跟 `set_current_user` 后 | 与 user_context 同生同死；try/finally 统一 reset |
| ActiveWorkspace 形态 | `auth.models.ActiveWorkspace(id, role)` 即满足 `CurrentWorkspace` Protocol | 不需要完整 `WorkspaceRow`；avoid 多一次 DB 查询拿 name/slug |
| `request.state.auth_payload` 中转 | `get_current_user_from_request` 把 decoded payload stash 到 request.state | middleware 避免二次 decode；显式管道比隐式重做便宜 |
| Slug 黑名单处理 | walker 的 `exists_check` 把 SLUG_BLACKLIST 当作"已占用"自然跳过 | 不需要在 `auto_slug_from_email` 里硬塞黑名单依赖；让黑名单留在 persistence 层 |
| `SLUG_BLACKLIST` 可见性 | 提升为 public（去掉前导下划线） | 注册流程要读，rename 是单次成本 |
| `auto_slug_from_email` 落点 | `app/gateway/auth/workspace_slug.py`（不在 persistence） | "email → slug" 是 registration 时序概念，不属于通用仓储 |
| `ensure_default_workspace` helper | 在 `routers/auth.py` 模块顶层，public name | 跨 4 个 router 入口（initialize/register/login/change_password）+ 1 个 lifespan 入口复用；idempotent — 已有 default_workspace_id 时 short-circuit |
| Login 也带 wid | `login_local` 调 `ensure_default_workspace` | pre-PR4 用户登录时自动回填；不依赖 lifespan 完成 |
| change_password 重签 token | bump `ver` 同时带 `wid` + `role` | 不带就把刚改完密码的用户立刻锁出去 |
| `/auth/me` 响应模型 | 新 `UserMeResponse`（不是改 `UserResponse`） | UserResponse 在其他地方也用；扩展专用 response 避免影响其它接口 |

## 跟进项（不在 PR4 范围）

- PR5 alembic 0002：ALTER `threads_meta` / `runs` / `feedback` / `run_events` 加 `workspace_id` + 回填 `legacy_workspace` 哨兵 + 改 NOT NULL
- PR6 仓储 / 路由层 workspace_id 校验：`@require_permission` 升级、`Paths` 切 workspace 维度、文件迁移脚本
- `request.state.auth_payload` 这条隐式通道：考虑改为更显式的 dataclass attached at known key
- Regular user pre-PR4 backfill：目前只在 `_ensure_admin_user` 回填 admin；如果生产有大量预存 regular user 没 workspace，可加一个 batch backfill 脚本（暂不需要，login 路径已 lazy backfill）
- 16 个 pre-existing caplog flake：与 PR4 无关但仍存在；future 集中清理一次

## 涉及文件

| 类别 | 文件 |
|---|---|
| **新增** | `backend/packages/harness/deerflow/persistence/migrations/versions/0001_users_default_workspace.py` |
| **新增** | `backend/app/gateway/auth/workspace_slug.py`（`auto_slug_from_email` + `next_available_slug`）|
| **新增 tests** | `test_alembic_default_workspace_id.py`, `test_auth_jwt_workspace.py`, `test_legacy_token_compat.py`, `test_auth_middleware_workspace.py`, `test_workspace_slug.py`, `test_register_creates_workspace.py`, `test_change_password_keeps_wid.py`, `test_auth_me_returns_workspaces.py`, `test_ensure_admin_user_workspace_backfill.py` |
| **修改 (harness)** | `persistence/user/model.py`（加 `default_workspace_id` FK）, `persistence/workspace/sql.py`（`SLUG_BLACKLIST` public）|
| **修改 (app)** | `auth/jwt.py`, `auth/errors.py`, `auth/models.py`（`User.default_workspace_id` + `ActiveWorkspace` + `UserMeResponse` + `UserMeWorkspace`）, `auth/repositories/sqlite.py`（持久化 `default_workspace_id`）, `auth_middleware.py`（注入 workspace contextvar）, `deps.py`（stash payload）, `routers/auth.py`（`ensure_default_workspace` + 4 路由集成）, `app.py`（lifespan backfill）|
| **修改 tests (compat)** | `test_auth.py`, `test_langgraph_auth.py`, `test_auth_errors.py`（13 处 `create_access_token` 加 `workspace_id="ws-test"`）|

## 测试矩阵

| 测试文件 | 用例数 | 覆盖 |
|---|---|---|
| `test_alembic_default_workspace_id.py` | 4（2 SQLite + 2 PG）| upgrade / downgrade round-trip |
| `test_auth_jwt_workspace.py` | 2 | JWT 编码 wid/role + decode 还原 |
| `test_legacy_token_compat.py` | 2 | 4 字段 token → WORKSPACE_MISSING；expired 优先级 |
| `test_auth_middleware_workspace.py` | 4 | wid contextvar 注入 / 401 WORKSPACE_REQUIRED / 公共路径放行 / try-finally reset |
| `test_workspace_slug.py` | 16 | 7 已知输入 + 6 fallback + 3 collision walker |
| `test_register_creates_workspace.py` | 3 | /initialize + /register + slug collision 隔离 |
| `test_change_password_keeps_wid.py` | 2 | change_password 重签 + login JWT 含 wid |
| `test_auth_me_returns_workspaces.py` | 2 | workspaces[] 字段 + 用户隔离 |
| `test_ensure_admin_user_workspace_backfill.py` | 3 | 首次回填 / idempotent / 已有 workspace 跳过 |

总计：**38 个 PR4 新测试** + 13 个改写的现有测试。

## Live smoke 命令（用户跟进）

```bash
# 1. clean restart
make stop && make dev

# 2. initialize admin
curl -s -X POST http://localhost:8001/api/v1/auth/initialize \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.com","password":"Str0ng!Pass99"}' | jq

# 3. 看 cookie 含 access_token + csrf_token；解码 access_token 看 wid 是否存在
# 4. 调 /auth/me
curl -s http://localhost:8001/api/v1/auth/me -b /tmp/cookies | jq

# 5. SQL 验证（连 PG）
SELECT id, email, default_workspace_id FROM users;
SELECT id, name, slug, owner_id FROM workspaces;
SELECT workspace_id, user_id, role FROM workspace_memberships;
```

预期：1 user / 1 workspace（slug = `admin-2` 因为 "admin" 在黑名单）/ 1 owner membership。
