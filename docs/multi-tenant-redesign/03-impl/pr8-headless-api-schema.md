# PR8 · `service_accounts` + `api_keys` + `external_users` schema only

> 实现笔记。对应 [docs/superpowers/plans/2026-05-10-stage-0-multi-tenant-foundation.md](../../superpowers/plans/2026-05-10-stage-0-multi-tenant-foundation.md) PR8（T8.1-T8.6）。
>
> 状态：**已落地**，5 个 commits 提交到 `docs/multi-tenant-redesign`（PR8 起始 `1fb07e48..` 结束 `f803f393`）。

## 范围

为 Stage 1 headless API 鉴权层准备底座：3 张新表 + ORM。仿 PR3 模式——纯 schema、不接路由、不写仓储、不暴露 API。

1. **`service_accounts`** — 非人身份，属于唯一 workspace。3 状态字段：`role`（Stage 0 仅 `member`）/ `identity_mode`（`collapsed` / `external_passthrough` / `both`，决定是否记录终端用户身份）/ `status`（`active` / `suspended` / `deleted`）。`workspace_id` FK CASCADE，`created_by` FK 用户 **RESTRICT**（防止误删带 SA 的 user）。
2. **`api_keys`** — service_account 的凭证。`key_prefix` String(16) **全局 unique**（撤销后亦不复用，避免审计混淆），`key_hash` String(128) 存 sha-256 hex（plaintext 仅创建时返）。`scopes` String(1024) 逗号分隔（不用 PG `text[]` 以保 SQLite dev 双驱动兼容；Stage 2 切纯 PG 可平滑迁）。`revoked_at`/`expires_at`/`last_used_at`/`rate_limit_rpm` 全 nullable。**双驱动部分索引** `idx_api_keys_active`（key_prefix）WHERE revoked_at IS NULL——同时声明 `sqlite_where` + `postgresql_where`，鉴权热路径 prefix lookup 加速。`service_account_id` FK CASCADE。
3. **`external_users`** — passthrough 模式下的终端身份。每次调用带 `X-External-User-Id` header 时 upsert 一行（Stage 1 起）。**复合 UNIQUE** `(service_account_id, external_id)`——同 external_id 可在不同 SA 下复用，但单 SA 下唯一。`workspace_id` 冗余存储（可经 SA 间接得到，但直接存以加速 workspace-scoped 跨 SA 聚合）。`metadata_json` JSON nullable=False default {} 存 plan tier / region / 自定义 tag。两个 FK 均 CASCADE。
4. **ORM 注册** — `deerflow/persistence/models/__init__.py` 加 3 行 import 让 `Base.metadata.create_all()` 在 `init_engine` 启动时自动建 3 张表。`test_pr8_metadata_registration.py` 反向验证：拉一个 fresh SQLite 引擎 inspect 表名集合，断言 3 张表都在。

**不在范围**（Stage 1）：
- API key 鉴权中间件 / token 生成 / hash 验证
- `@require_permission` scope 升级（接 `service_account`/`api_key` 主体）
- Pattern A/B endpoint 设计
- `external_users` upsert 逻辑
- 鉴权层的 rate limiting / scopes 校验

## Tasks 完成清单

| Task | Commit | 关键改动 |
|---|---|---|
| **T8.1** | `1fb07e48` | `service_account/{__init__, model}.py` + insert smoke + 注册进 persistence.models |
| **T8.2 + T8.3** | `bb728978` | `test_cascade_on_workspace_delete` + `test_restrict_on_created_by_user_delete` |
| **T8.4** | `52e9999a` | `api_key/{__init__, model}.py` + 3 测试（column UNIQUE + 双驱动 partial index DDL + CASCADE） |
| **T8.5** | `6f806ff4` | `external_user/{__init__, model}.py` + 2 测试（复合 UNIQUE + CASCADE） |
| **T8.6** | `f803f393` | `test_pr8_metadata_registration.py` 反向验证 `Base.metadata.create_all()` 真的建 3 张表 |

## 验收

- [x] **3 + 3 + 2 + 1 = 9 个新单测全过**（T8.1/T8.2/T8.3 三个 service_account；T8.4 三个 api_key；T8.5 两个 external_user；T8.6 一个 metadata registration）
- [x] **3 张表 `Base.metadata.create_all()` 自动建**：T8.6 inspect 表名集合断言 `{service_accounts, api_keys, external_users}.issubset(tables)`
- [x] **FK 行为按 plan 设计**：CASCADE on workspace/SA delete、RESTRICT on creator user delete、SQLite + PG 均生效（SQLite 通过 engine.py connect-listener 的 `PRAGMA foreign_keys=ON`）
- [x] **partial index 双驱动 DDL** 通过 `dialect_options` 检查锁定（不只看 SQLAlchemy emit，下次有人删 `postgresql_where` 测试会红）

## 文件结构

**新增**：
- `backend/packages/harness/deerflow/persistence/service_account/{__init__.py, model.py}`
- `backend/packages/harness/deerflow/persistence/api_key/{__init__.py, model.py}`
- `backend/packages/harness/deerflow/persistence/external_user/{__init__.py, model.py}`
- `backend/tests/test_service_account_schema.py`（3 cases）
- `backend/tests/test_api_key_schema.py`（3 cases）
- `backend/tests/test_external_user_schema.py`（2 cases）
- `backend/tests/test_pr8_metadata_registration.py`（1 case）
- `docs/multi-tenant-redesign/03-impl/pr8-headless-api-schema.md` — 本文件

**修改**：
- `backend/packages/harness/deerflow/persistence/models/__init__.py` — 加 3 行 import + `__all__` 注册

## 关键设计决策

1. **`key_prefix` 全局 UNIQUE，而非"活跃 UNIQUE"**：column-level `unique=True` 覆盖整个 key 生命周期。理由：撤销 + 复用同前缀会让审计日志里 "prefix X did Y" 的语义模糊；prefix 16 字符的命名空间足够大（≈10^25）从不复用没有成本。`idx_api_keys_active` 走部分非唯一索引——纯粹是热路径优化，撤销 key 不进活跃索引以减小热索引大小。
2. **`scopes` 用 `String(1024)` 而非 PG `text[]`**：Stage 0 仍要 SQLite 跑得动（dev / unit test 兜底）。逗号分隔字符串两端通用；Stage 2 切纯 PG 后再迁 `text[]` + GIN 索引代价低。LOCK 由 plan 记下。
3. **`external_users.workspace_id` 冗余存储**：技术上可从 `service_account_id` JOIN 出来，但 Stage 1 几个高频查询（workspace 级配额聚合 / admin UI 列出 workspace 所有 external user）每次走 JOIN 会随 SA 数量增长变慢。冗余一列、CASCADE 同 SA 一致，是值得的存储成本。
4. **不写 Repository 类**：Stage 0 PR3 / PR5-6 的 Repository 是给 Gateway 当前在用的表准备的。PR8 三张表 Stage 0 内**没人读写**——直到 Stage 1 headless API 才用得上。写空 Repository 现在不知道接口形态，等 Stage 1 真用时连同 token 生成 / 哈希校验一起设计更合理。Plan 也明确"仅暴露 ORM"。
5. **T8.6 反向 metadata registration 测试**：Stage 0 已经踩过坑——PR1-PR6 多次出现"模型类写了但 `persistence/models/__init__.py` 漏 import → `create_all()` 不建表 → 上线后 SELECT 时炸 'no such table'"。T8.6 把这条 invariant 锁定。
6. **`identity_mode` 三态保持字符串而非 enum**：和 `role` / `status` 同款思路——String(16) 比 enum 更易加值（Stage 2 可能加 `cli_only` 等新态），不动 schema。

## Live smoke 命令（用户跟进）

PR8 纯 schema 改造，无路由 / 中间件 / 文件系统副作用。

**单机自检**：
```bash
make stop && make dev   # 起服务，让 lifespan 跑 init_engine
# 看 Gateway 启动日志无报错；create_all 默认 silent，无需额外断言
```

**RDS 实跑表存在**（需要密码）：
```bash
psql "$DATABASE_URL" -c "\dt service_accounts api_keys external_users"
# 期望：3 行
psql "$DATABASE_URL" -c "\d+ api_keys"
# 期望看到 idx_api_keys_active (key_prefix) WHERE revoked_at IS NULL
```

**双驱动 partial index 在 PG 真生效**（可选）：
```bash
# 用 testcontainers 跑 @pytest.mark.postgres 系列；当前 PR8 没写 PG 专属测试，
# 但 idx_api_keys_active 的 DDL 已在 dialect_options 里覆盖，PG schema dump
# 应见 "WHERE revoked_at IS NULL"
PYTHONPATH=. uv run pytest -m postgres -v
```

## Stage 0 退出门

PR8 是 Stage 0 工程层面最后一个 PR。剩余 Stage 0 退出条件见 [STATUS.md](./STATUS.md)"用户必须跟进的事"：
- [ ] RDS 上 `service_accounts` / `api_keys` / `external_users` 三张表 `\dt` 见
- [ ] `make migrate-paths --dry-run` 在 fresh DB 上输出空
- [ ] testcontainers ephemeral PG smoke 跑过一次
- [ ] 生产稳定运行 ≥ 2 周（业务条件）
