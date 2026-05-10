# Workspace Schema 设计 · Stage 0 锁定版

> 写于 2026-05-10。Stage 0 PR1 动手前的 schema 锁定文档。
>
> **数据库基线**：Stage 0 的 PR1 必须**先**完成 [phased-rollout Stage 0](../02-rollout/phased-rollout-by-scale.zh-CN.md#stage-0--workspace-模型立起来--postgres-切换--auth-收紧) 的 PR1-PR2（Postgres 接入 + 默认切换），再起 schema PR。下面所有 ALTER 都直接在 Postgres 上跑，**不再走 SQLite → Postgres 二次迁移**。SQLite 仅保留为可选 dev 兜底。
>
> **范围**：仅 Stage 0 必须落地的 schema —— `workspaces` / `workspace_memberships` 两张新表，`users` / `threads_meta` / `runs` / `feedback` 的 ALTER，`service_accounts` / `api_keys` / `external_users` 的预建（Stage 0 末，schema only），以及 JWT `TokenPayload` 一次到位的字段集。
>
> **不在范围**：仓储实现细节、ContextVar、路径迁移、路由校验、Stage 1+ 才加的列（`plan` / `allowed_origins` / `custom_domain` 等）。
>
> **配套阅读**：
> - [02-rollout/phased-rollout-by-scale.zh-CN.md](../02-rollout/phased-rollout-by-scale.zh-CN.md) Stage 0 必做项
> - [02-rollout/stage-0-code-map.zh-CN.md](../02-rollout/stage-0-code-map.zh-CN.md) 现状代码锚点
> - [adr-001-data-isolation.zh-CN.md](./adr-001-data-isolation.zh-CN.md) §4.1 表结构改造
> - [adr-004-tenant-rbac.zh-CN.md](./adr-004-tenant-rbac.zh-CN.md) §5.1 / §5.2 RBAC + JWT
> - [adr-007-routing-frontend.zh-CN.md](./adr-007-routing-frontend.zh-CN.md) §4 slug 规范、§8 JWT 改造
> - [02-rollout/headless-api-track.zh-CN.md](../02-rollout/headless-api-track.zh-CN.md) §2 service account / API key

---

## 1. 命名约定 · workspace vs tenant

| 维度 | 选择 |
|---|---|
| 数据库列名 | `workspace_id` |
| Python 标识符 | `workspace_id` / `WorkspaceRow` / `_current_workspace` |
| JWT claim | `wid`（紧凑） |
| 用户可见用语 | "Workspace"（团队 workspace 也叫 workspace，不分"个人空间"） |

ADR-001 / 004 / 007 原稿写 `tenant_id`——这些 ADR**不重命名**（成本不抵收益），落代码时统一读作 `workspace_id`。本文档与 02-rollout 系列保持 `workspace` 用语一致。

> 推翻条件：拿到强企业客户后真的出现"租户内多 workspace"的层级（tenant > workspace > user），那时再分裂概念。Stage 0/1/2 不预留这层。

---

## 2. 新增表

### 2.1 `workspaces`

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | `String(36)` | PK | UUID v4 字符串。与 `users.id` 类型对齐，跨 DB 可移植（SQLite/Postgres 都用 36 字符 CHAR） |
| `name` | `String(64)` | NOT NULL | 显示名（用户首次注册时默认 `<email 前缀>'s Workspace`） |
| `slug` | `String(32)` | UNIQUE NOT NULL | URL 标识，正则 `^[a-z0-9](-?[a-z0-9])*$`，3-32 字符；DB 存小写 |
| `status` | `String(16)` | NOT NULL default `'active'` | `active` / `suspended` / `deleted`（platform admin 暂停/删 workspace） |
| `owner_id` | `String(36)` | NOT NULL FK `users.id` | 冗余字段，便于查询；与 `workspace_memberships.role='owner'` 严格一致（事务保证） |
| `created_at` | `DateTime(timezone=True)` | NOT NULL | UTC |
| `updated_at` | `DateTime(timezone=True)` | NOT NULL | UTC，写入自动更新 |

**索引**：
- PK: `id`
- UNIQUE: `slug`
- 不加 `(status)` 索引——Stage 0 用户量小，全表扫够用；Stage 1+ 视情况补

**slug 黑名单**（应用层校验，不写进 DB constraint）：

```
admin, api, auth, login, signup, accept-invite, pricing, docs, status,
platform, system, health, static, public, favicon.ico, robots.txt,
sitemap.xml, _next, .well-known, settings, billing, onboarding, select-workspace
```

> ADR-007 §4 列了一份基础黑名单；本表是落代码版（含 Next.js 保留路径）。

**Stage 0 不加的列**（决策记录）：

| 列 | 推迟到 | 理由 |
|---|---|---|
| `plan` | Stage 1（与 `workspace_quotas.plan` 一起加） | Stage 0 没有付费分层 |
| `allowed_origins` | Stage 1 末（headless API Pattern B） | 用到时加 ARRAY/JSON 列代价低 |
| `custom_domain` | Stage 4（enterprise） | 长尾需求，列加在哪一层都行 |
| `billing_email` | Stage 1（Stripe 对接） | 一并加 |
| `settings_json` | 永不加 | 扩展点用专门的 `workspace_settings` 表 + 枚举 key，比 JSON dump 易迁移 |

### 2.2 `workspace_memberships`

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `workspace_id` | `String(36)` | PK / FK `workspaces.id` ON DELETE CASCADE | |
| `user_id` | `String(36)` | PK / FK `users.id` ON DELETE CASCADE | |
| `role` | `String(16)` | NOT NULL | Stage 0 只允许 `owner`；Stage 2 起 `owner`/`admin`/`member` |
| `invited_by` | `String(36)` | NULL FK `users.id` ON DELETE SET NULL | Stage 0 暂不写入；Stage 2 invitation 流程才用 |
| `joined_at` | `DateTime(timezone=True)` | NOT NULL | UTC |

**为什么 `role` 用 `String(16)` 不用 DB enum**：Postgres enum ALTER 加值需要 `ALTER TYPE ... ADD VALUE`，且不可删；string + 应用层校验 = 后续随便加 `viewer` / `auditor` 等角色不动 DB schema。

**索引**：
- PK: `(workspace_id, user_id)` 复合主键
- `idx_workspace_memberships_user`: `(user_id, workspace_id)` —— 倒查索引，用于 `/auth/me` 列出当前 user 所有 workspace
- `idx_one_owner_per_workspace`: UNIQUE on `(workspace_id)` WHERE `role = 'owner'` —— partial unique index

**partial unique 兼容性**：
- SQLite **支持** `CREATE UNIQUE INDEX ... WHERE ...`（参 `users.idx_users_oauth_identity` 现有用法）
- Postgres 同样支持
- SQLAlchemy 通过 `Index(..., unique=True, sqlite_where=text(...), postgresql_where=text(...))` 表达；这条索引**保留两套 where 条件等价**

### 2.3 `service_accounts`（Stage 0 末加，schema only）

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | `String(36)` | PK | |
| `workspace_id` | `String(36)` | NOT NULL FK `workspaces.id` ON DELETE CASCADE | 必属于一个 workspace |
| `name` | `String(64)` | NOT NULL | 业务系统起的标识名 |
| `role` | `String(16)` | NOT NULL default `'member'` | SA 在 workspace 内的 role |
| `identity_mode` | `String(16)` | NOT NULL default `'collapsed'` | `collapsed` / `external_passthrough` / `both` |
| `status` | `String(16)` | NOT NULL default `'active'` | `active` / `suspended` / `revoked` |
| `created_by` | `String(36)` | NOT NULL FK `users.id` ON DELETE RESTRICT | 必须是 workspace owner/admin |
| `created_at` | `DateTime(tz)` | NOT NULL | |
| `updated_at` | `DateTime(tz)` | NOT NULL | |

索引：`idx_service_accounts_workspace`: `(workspace_id, status)`

### 2.4 `api_keys`（Stage 0 末加，schema only）

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | `String(36)` | PK | |
| `service_account_id` | `String(36)` | NOT NULL FK `service_accounts.id` ON DELETE CASCADE | |
| `key_prefix` | `String(16)` | UNIQUE NOT NULL | 前 16 字符明文（`dfk_live_...`），UI 展示用 |
| `key_hash` | `String(128)` | NOT NULL | 完整 key 的 sha256 hex（64 字符）+ 余量 |
| `name` | `String(64)` | NOT NULL | "生产环境 key" |
| `scopes` | `String(1024)` | NOT NULL default `''` | 逗号分隔字符串。Postgres 已是 Stage 0 默认，但保持 `String` 以兼容 SQLite dev 兜底；如果未来确认完全弃用 SQLite，可平滑迁 `text[]` |
| `rate_limit_rpm` | `Integer` | NULL | NULL = 用 workspace plan 默认 |
| `expires_at` | `DateTime(tz)` | NULL | |
| `last_used_at` | `DateTime(tz)` | NULL | |
| `revoked_at` | `DateTime(tz)` | NULL | 软删除标记 |
| `created_at` | `DateTime(tz)` | NOT NULL | |

索引：
- `idx_api_keys_sa`: `(service_account_id)`
- `idx_api_keys_active`: `(key_prefix)` WHERE `revoked_at IS NULL`（partial）

### 2.5 `external_users`（Stage 0 末加，schema only）

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | `String(36)` | PK | ghost user id（DeerFlow 内部） |
| `workspace_id` | `String(36)` | NOT NULL FK `workspaces.id` ON DELETE CASCADE | |
| `service_account_id` | `String(36)` | NOT NULL FK `service_accounts.id` ON DELETE CASCADE | |
| `external_id` | `String(128)` | NOT NULL | 业务系统传入 ID，原样存 |
| `display_name` | `String(128)` | NULL | |
| `metadata_json` | `JSON` | NOT NULL default `{}` | 业务字段 |
| `created_at` | `DateTime(tz)` | NOT NULL | |
| `last_seen_at` | `DateTime(tz)` | NULL | |

索引：UNIQUE `(service_account_id, external_id)` —— 同一 SA 下 external_id 唯一

---

## 3. ALTER 现有表

### 3.1 `users`

```python
# 新增列
default_workspace_id: Mapped[str | None] = mapped_column(
    String(36),
    ForeignKey("workspaces.id", ondelete="SET NULL"),
    nullable=True,
    comment="登录后默认进入的 workspace；NULL 时强制走 picker（user 多 workspace 场景）"
)
```

**为什么不加 `current_workspace_id`**：每次登录时从 `default` 或 `/select-workspace` 决定，写入 JWT 的 `wid` claim；DB 不存"当前激活"状态，避免多设备冲突。

`system_role` 字段保留——它是**平台级** role（`platform_admin` / `user`），与 workspace role 正交（参 ADR-004 §6）。

### 3.2 `threads_meta`

```python
# 新增列（Stage 0 PR3 先 nullable，回填后 ALTER 改 NOT NULL）
workspace_id: Mapped[str | None] = mapped_column(
    String(36),
    ForeignKey("workspaces.id", ondelete="CASCADE"),
    nullable=True,  # PR3 中段；回填脚本跑完改 NOT NULL
    comment="所属 workspace；与 (thread_id) 复合 UNIQUE 防跨 workspace 复用同 ID"
)

# 新增索引（在 __table_args__ 里）
Index("idx_threads_meta_workspace_thread", "workspace_id", "thread_id", unique=True),
Index("idx_threads_meta_workspace_user_updated", "workspace_id", "user_id", "updated_at"),
```

**关键索引说明**：
- `(workspace_id, thread_id)` UNIQUE 是 ADR-001 §4.1.1 修订版的"应用层强约束 + DB 兜底"防线
- `(workspace_id, user_id, updated_at)` 覆盖前端 thread list 默认查询模式
- 现有 `user_id` 上的非复合索引可以**保留**（删了某些后台 cleanup 脚本会变慢；不阻塞主路径）

### 3.3 `runs`

```python
workspace_id: Mapped[str | None] = mapped_column(
    String(36),
    ForeignKey("workspaces.id", ondelete="CASCADE"),
    nullable=True,  # PR3 中段
)

Index("idx_runs_workspace_created", "workspace_id", "created_at"),
```

### 3.4 `feedback`

```python
workspace_id: Mapped[str | None] = mapped_column(
    String(36),
    ForeignKey("workspaces.id", ondelete="CASCADE"),
    nullable=True,
)

Index("idx_feedback_workspace_run", "workspace_id", "run_id"),
```

### 3.5 `run_events`（待确认）

stage-0-code-map §2 标注 `run_events` 是否 DB 持久化"unverified"。PR3 第一步先 grep 确认；若是 DB 表就比照 `runs` 加 `workspace_id`，若是内存队列则跳过。

---

## 4. JWT TokenPayload · 一次到位的字段集

> Stage 0 落 `wid` + `role`，**Stage 2 不再 bump**。理由：每次扩字段都要 bump `token_version` 让所有用户重登，churn 体验差；一次加齐两次的份。

```python
# backend/app/gateway/auth/jwt.py
class TokenPayload(BaseModel):
    sub: str           # user_id（沿用）
    wid: str           # workspace_id（Stage 0 新增）
    role: str          # owner / admin / member（Stage 0 新增；1 人 workspace 默认 'owner'）
    exp: datetime      # 沿用
    iat: datetime | None = None  # 沿用
    ver: int = 0       # token_version（沿用；任何 membership 变更 bump）
```

**Stage 0 实际填充值**：
- `wid` = 用户 `default_workspace_id`（注册时自动建的 1 人 workspace）
- `role` = 总是 `'owner'`（Stage 0 还没有团队 workspace）

**Stage 2 启用时**：
- 加入团队 workspace 后 → `role` 真正区分 admin/member
- bump `token_version` 让旧 JWT（仍写 `'owner'`）过期重发

**兼容性**：
- 旧 4 字段 token（`{sub, exp, iat, ver}`）解码失败时**强制走 `/select-workspace`** 重发新 JWT（参 ADR-007 §11）。Stage 0 部署后 7 天（默认 token TTL）内所有老 token 自然轮替完。

---

## 5. 不可逆决策清单

| 决策 | 选择 | 反悔代价 |
|---|---|---|
| id 类型 | `String(36)` (UUID v4 字符串) | 改 native UUID → 全表 schema rewrite + 所有 FK 重建 + Python `str` ↔ `UUID` 边界改造 |
| 命名（workspace_id） | `workspace_id` | 改 `tenant_id` → 全代码改名 + 所有 ADR 文档同步 |
| slug 字符集 | `^[a-z0-9](-?[a-z0-9])*$` 3-32 | 改 → 老 URL 全失效（v1 还没暴露 slug 路由前改是免费的） |
| memberships PK | `(workspace_id, user_id)` 复合 | 改 surrogate id → migration 脚本要写 dedup 逻辑 |
| TokenPayload 字段 | `sub/wid/role/exp/iat/ver` | 加新字段 → 必 bump `token_version`，全用户重登（Stage 0 一次性加 `wid` + `role`，省一次） |
| `users.default_workspace_id` 而非 `current_workspace_id` | 默认 + JWT 决定当前 | 改成 `current_*` → 多设备语义混乱 |
| FK 删除策略（workspace 删 → memberships/threads CASCADE）| CASCADE | 改 RESTRICT → 平台 admin 删 workspace 时手动级联，运营负担大 |

> Stage 0 PR1 合入前，上面这 7 项**全部**要在团队 review 中拍板；任何一项改主意都要 revert PR1 重写。

---

## 6. PR 拆分（Stage 0 内的 5 个 schema PR）

> **前置 PR**：本表 PR1 之前必须先完成 [phased-rollout Stage 0 PR1-2](../02-rollout/phased-rollout-by-scale.zh-CN.md#stage-0--workspace-模型立起来--postgres-切换--auth-收紧)：Postgres 接入 + testcontainers + 默认 backend 切换。本文档下面的 PR1 = phased-rollout 的 PR3，本文档 PR5 = phased-rollout 的 PR8。
>
> 在 Postgres 已就绪的基础上，schema 改动按下面 5 个 PR 推：

| PR | 范围 | 落本文档的哪些章节 | 状态 |
|---|---|---|---|
| **PR1** | `workspaces` + `workspace_memberships` 表 + 仓储 + 单测 | §2.1 + §2.2 | 设计完，可写 |
| **PR2** | 注册/initialize 流程改造（自动建 1 人 workspace）+ JWT 扩 `wid`/`role` + AuthMiddleware ContextVar 注入 | §3.1 (`default_workspace_id`) + §4 | 依赖 PR1 |
| **PR3** | 现有 4 表 ALTER 加 `workspace_id`（直接 Postgres，先 nullable）+ 数据回填脚本（legacy_workspace）→ ALTER 改 NOT NULL | §3.2-§3.5 | 依赖 PR1+PR2 |
| **PR4** | 路由层 `(workspace_id, thread_id)` 校验 + `Paths` 切 workspace 维度 + 文件系统迁移脚本 | 不在本文档（仓储/路径设计） | 依赖 PR3 |
| **PR5** | `service_accounts` / `api_keys` / `external_users` schema（不接路径） | §2.3-§2.5 | 与 PR4 并行 |

每个 PR 必须独立可上线、可回滚。PR1 单独合入后系统行为不变（新表无人写入）。

---

## 7. 测试要点（PR1 范围）

仿现有 `tests/test_*.py` 模式：

- `test_workspace_repo.py`
  - create / get / update / delete workspace
  - slug 唯一性约束
  - slug 黑名单校验（应用层）
  - status 状态机（active → suspended → deleted）
- `test_workspace_membership_repo.py`
  - create membership
  - 同一 workspace 不能有 2 个 owner（partial unique index）
  - CASCADE 删（删 workspace 后 memberships 消失）
  - `list_workspaces_by_user(user_id)` 返回正确顺序
- 不写：路由测试（PR4 才有路由）、JWT 测试（PR2 才扩字段）

---

## 8. 推翻条件

整份 schema 设计要重排只在两种情况：

1. **拿到强企业客户必须自定义角色**：role 列从 String(16) + 应用层校验 → 完整 RBAC engine（`roles` / `permissions` / `role_permissions` 表）。schema **加表**，不改现有列，影响小。
2. **决定改用 native UUID 类型**（Postgres 切换时一并）：String(36) → `UUID`。需要全表 ALTER + Python 边界改造。建议**不**做，String(36) 在 Postgres 上落地为 `CHAR(36)`，性能差异 < 5%，可接受。

> 上面 §5 七项不可逆决策不在"推翻条件"覆盖范围——那些一旦发布到生产就只能往前走。
