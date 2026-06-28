# PR5 · alembic 0002 + 回填 + alembic 0003 — 4 张业务表 workspace_id

> 实现笔记。对应 [docs/superpowers/plans/2026-05-10-stage-0-multi-tenant-foundation.md](../../superpowers/plans/2026-05-10-stage-0-multi-tenant-foundation.md) PR5（T5.1-T5.12）。
>
> 状态：**已落地**，11 个 feat/test commits 提交到 `docs/multi-tenant-redesign`（PR5 起始 `a7326978..` 结束 `30f2bd00`）。

## 范围

PR5 把 PR4 的 `users.default_workspace_id` 关系真正接到 4 张业务表上：

1. **alembic 0002**：`threads_meta` / `runs` / `feedback` / `run_events` 加 *nullable* `workspace_id String(36)` 列 + FK 到 `workspaces`（`ON DELETE CASCADE`）+ `threads_meta` 复合索引 `idx_threads_meta_workspace_user_updated`
2. **backfill 脚本** `scripts/backfill_workspace_id.py`（带 `--dry-run`）：三步幂等回填
   - Step 1：每个 `default_workspace_id IS NULL` 的 user 建 1 人 workspace + owner membership + 回填 `users.default_workspace_id`
   - Step 2：correlated subquery `UPDATE` 4 张业务表，`workspace_id` 从行所属 user 的 `default_workspace_id` 拉
   - Step 3：剩余 `workspace_id IS NULL`（user_id 本来就 NULL 的孤儿）→ `LEGACY_WORKSPACE_ID = 00000000-0000-0000-0000-000000000000`（按需建，owner = platform admin）
3. **alembic 0003**：pre-flight 拒迁（任一表残留 NULL → `RuntimeError`）→ 4 表 `workspace_id` 改 `NOT NULL` → `threads_meta` 加 `idx_threads_meta_workspace_thread UNIQUE(workspace_id, thread_id)`
4. **4 个 ORM model**：加 `Mapped[str | None] workspace_id` 列定义对齐 0002 schema，`threads_meta` 模型也加 `idx_threads_meta_workspace_user_updated` Index 以便 `create_all()` 在 dev DB 上落同样形状

**不在范围**（推迟到 PR6）：
- ORM model 把 `workspace_id` 翻成 `nullable=False`（plan 的 T5.11，见下文"偏差说明"）
- 仓储 `create()` 方法加 `workspace_id` 哨兵参数 + WHERE 子句
- `@require_permission` 升级 `check_access(thread_id, user_id, workspace_id)` 三参数
- 入口路由 `/api/threads` 写 `workspace_id`
- `Paths` 切 workspace 维度 + 文件系统迁移

## 验收

- [x] **17 个新单测全过**：9 alembic 0002+0003 (5 SQLite + 4 PG-skip) + 9 backfill (含 dry-run + 端到端 orchestrator + no-users 报错)
- [x] **全套 `make test` 3150 passed + 30 skipped + 16 pre-existing caplog flake**（PR4 末 3136 + 26 + 同 16 flake；+14 passed / +4 skipped = +18 PR5 测试）。16 个 flake 在 isolate 跑里都 PASS，已 PR4 时确认与本 stage 无关，**继续 follow-up 推迟**
- [x] **alembic 0002 + 0003 在 SQLite tempdir 上 upgrade/downgrade 双驱动都过**（PG 自动 skip 当 docker 未起）
- [x] **回填脚本 `--dry-run` 测试断言无写入**：snapshot workspaces/memberships/users.default_workspace_id 计数 → 跑 backfill(dry_run=True) → 计数不变
- [x] **0003 拒迁断言**：threads_meta 留 1 个 `workspace_id IS NULL` 行 → upgrade 抛 `RuntimeError` 含 `Cannot ALTER` + 提示去跑 backfill
- [x] **UNIQUE(workspace_id, thread_id) 触发**：scratch table 验 `IntegrityError`（生产 schema thread_id 已是 PK，shadow 了 UNIQUE，所以用独立表验语义）
- [x] **lint**：`make lint` 全过（ruff check + format）
- [ ] **真机 PG 完整跑 `alembic upgrade 0002` → `python scripts/backfill_workspace_id.py` → `alembic upgrade 0003`** — **待用户**（agent 不能起 RDS 操作）

## 偏差说明：T5.11 推迟到 PR6

**plan 原 task**："4 个 model.py workspace_id 改 nullable=False；commit"

**为什么推迟**：

- ORM model 的 `nullable=False` 影响两条链路：
  1. **生产**：alembic 0003 已经把 DB 列改 NOT NULL，这是真正承载不变式的边界。ORM 层翻不翻不影响数据库约束
  2. **测试 / dev**：`init_engine()` 走 `Base.metadata.create_all()`，从 ORM 推 DDL。如果模型 `nullable=False` 而仓储 `create()` 还不传 `workspace_id`（PR6 才动），所有创建 `ThreadMetaRow / RunRow / FeedbackRow / RunEventRow` 的 production code path（6 个 INSERT 站点）会在 DB 层炸 NOT NULL 约束
- PR6 的明确 scope 是"仓储 workspace_id 哨兵 + 路由层 workspace 化"——翻 ORM `nullable=False` 必须与 6 个 INSERT 站点 + repo 签名 + contextvar 接入一起做，**不能独立翻**
- plan T5.12 验收"`make test` 不破"与 T5.11 单独翻 `nullable=False` 在当前 codebase 状态下**互斥**。本 PR 选择维护 T5.12 验收，将 T5.11 推迟到 PR6 自然接入处

**承载不变式不变**：alembic 0003 在 DB 层强制 NOT NULL；任何走 alembic 路径（生产）的部署都拿到正确约束。ORM 层 `nullable=True` 只在 dev `create_all()` 路径生效，PR6 翻完后整个体系收敛。

## 关键决策（与 plan §LOCK + workspace-schema-design 对齐）

| 项 | 选择 | 理由 |
|---|---|---|
| 4 表 FK 删除策略 | `ON DELETE CASCADE` | 删 workspace 时业务行随之清空（避免外键悬空） |
| `idx_threads_meta_workspace_user_updated` 列序 | `(workspace_id, user_id, updated_at)` | PR6 路由"列出 workspace X 下 user Y 的会话按时间倒序"查询的覆盖索引 |
| `idx_threads_meta_workspace_thread` UNIQUE 加在 0003 不是 0002 | nullable 列上 UNIQUE 在 PG 允许多 NULL，在 SQLite 默认也允许；但回填中途的 UNIQUE 校验语义模糊，等 NOT NULL 一起加更干净 | 风险更可控 |
| 回填 Step 2 SQL 形态 | SQLAlchemy `update().values(workspace_id=correlated_subquery)` | SQLite 3.33+ 才支持 `UPDATE ... FROM`；correlated subquery 跨方言可移植 |
| 回填 Step 1 slug 走 walker + blacklist | `auto_slug_from_email` + `next_available_slug` + 把 `SLUG_BLACKLIST` 当"已占用" | 与 PR4 注册流程语义一致；admin@example.com → `admin-2`（"admin" 在黑名单） |
| `legacy_workspace` owner 选取 | 先找 `system_role='admin'` 最早一个，没有则用最早的任意 user，都没有则 `RuntimeError` | 不能创建无 owner 的 workspace；空 DB 上明确报错强制操作员先 bootstrap admin |
| `_BUSINESS_TABLES` 顺序 | `threads_meta / runs / feedback / run_events` | 仅人眼可读性，无依赖 |
| `_ensure_legacy_workspace` 拆出 orchestrator 调一次 | 不内联 Step 3 内（避免循环 4 次 check） | 简单 + 显式步骤分隔 |
| 0003 pre-flight raise 类型 | `RuntimeError` with `_BackfillRequiredError` 子类 | Alembic 抛错语义最自然；消息含具体表 + 行数 + 修复命令 |
| Step 3 dry-run 报告 | `WOULD assign N orphan rows in {table}` 包括 Step 2 没 cover 的全部 NULL 行 | 操作员能看到具体多少行会被 lump 进 legacy_workspace |

## 跟进项（不在 PR5 范围）

- **PR6**：4 个仓储 `create()` 加 `workspace_id` 参数 + INSERT；翻 ORM model `nullable=False`；`@require_permission` 三参数；`/api/threads` 路由层校验
- **真机 PG smoke**：`alembic upgrade 0002` → `python scripts/backfill_workspace_id.py --dry-run` → 看输出 → 实跑 → `alembic upgrade 0003`。命令清单见下方
- **远程 RDS testcontainers**：本 PR 复用 PR3 测试 fixture，PG twin 测试自动 skip 当 docker 未起；用户需起 docker daemon 实跑一遍

## 涉及文件

| 类别 | 文件 |
|---|---|
| **新增 (harness)** | `persistence/migrations/versions/0002_business_tables_workspace.py` |
| **新增 (harness)** | `persistence/migrations/versions/0003_business_tables_workspace_not_null.py` |
| **新增 (script)** | `backend/scripts/backfill_workspace_id.py`（3 个 step + 1 个 ensure + 1 个 orchestrator + main）|
| **新增 tests** | `test_alembic_business_tables.py`, `test_backfill_workspace_id.py` |
| **修改 (harness models)** | `persistence/thread_meta/model.py`（+`workspace_id` FK + composite index），`persistence/run/model.py`，`persistence/feedback/model.py`，`persistence/models/run_event.py` |
| **修改 (test compat)** | `test_alembic_default_workspace_id.py`（"head" → "0001_users_default_workspace" 显式 pin）|

## 测试矩阵

| 测试文件 | 用例数 | 覆盖 |
|---|---|---|
| `test_alembic_business_tables.py` | 9（5 SQLite + 4 PG-skip）| 0002 upgrade/downgrade + 0003 happy/refuse-NULL/UNIQUE |
| `test_backfill_workspace_id.py` | 9 | Step 1 创建/幂等/黑名单 + Step 2 4 表/多 user 隔离 + Step 3 orphan/无 user 报错 + orchestrator + dry-run no-write |

总计：**18 个 PR5 新测试** + 1 个改写（pin 0001 revision）。

## Live smoke 命令（用户跟进）

```bash
# 1. 当前 DB state（应在 PR4 之后状态）
psql "$DEER_FLOW_PG_URL" -c "SELECT count(*) FROM users WHERE default_workspace_id IS NULL;"

# 2. dry-run 看 backfill 会动什么
cd backend && PYTHONPATH=. python scripts/backfill_workspace_id.py --dry-run

# 3. 跑 0002（加 nullable 列）
cd backend/packages/harness/deerflow/persistence && \
  PYTHONPATH=../../../.. alembic -c migrations/alembic.ini upgrade 0002_business_tables_workspace

# 4. 真跑 backfill
cd backend && PYTHONPATH=. python scripts/backfill_workspace_id.py

# 5. 应返 0
psql "$DEER_FLOW_PG_URL" -c "SELECT count(*) FROM threads_meta WHERE workspace_id IS NULL;"
psql "$DEER_FLOW_PG_URL" -c "SELECT count(*) FROM runs WHERE workspace_id IS NULL;"
psql "$DEER_FLOW_PG_URL" -c "SELECT count(*) FROM feedback WHERE workspace_id IS NULL;"
psql "$DEER_FLOW_PG_URL" -c "SELECT count(*) FROM run_events WHERE workspace_id IS NULL;"

# 6. 跑 0003（改 NOT NULL + UNIQUE）
cd backend/packages/harness/deerflow/persistence && \
  PYTHONPATH=../../../.. alembic -c migrations/alembic.ini upgrade 0003_business_tables_workspace_not_null

# 7. 确认 legacy_workspace
psql "$DEER_FLOW_PG_URL" -c "SELECT id, name, slug FROM workspaces WHERE id = '00000000-0000-0000-0000-000000000000';"
```

如果 dry-run 输出里 `legacy_workspace_created=True` 而 DB 里还没用户，先去 `/auth/initialize` 建管理员再跑——backfill 不会替你 bootstrap。
