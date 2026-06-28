# Stage 0 多租户改造 · DeerFlow Master Plan

> **For agentic workers:** REQUIRED SUB-SKILL: 本 plan 是 8 个 PR 串成的 stage-level 拆解。**每个 PR 是独立可执行的子项目**，执行时各自调 `superpowers:executing-plans`（inline 多 PR 串行）或 `superpowers:subagent-driven-development`（subagent 派发，每 PR 一个 task）。Steps 用 checkbox（`- [ ]`）syntax 跟踪。
>
> **Plan file 落点（执行 session 时）**：本 plan 应该 copy 到 `docs/superpowers/plans/2026-05-10-stage-0-multi-tenant-foundation.md`（项目内 superpowers 约定路径），然后随项目 git 走。当前 `~/.claude/plans/soft-whistling-sundae.md` 仅是 plan-mode session 临时位置。
>
> **执行节奏建议**：每个 PR 独立 review checkpoint。完成 PR1 → human review → 合入 → 才开 PR2。**绝不要把 8 个 PR 一口气跑完不 check-in**——多租户改造的不可逆决策密集，每 PR 都该让人看一眼。

**Goal**：把 workspace 概念落到 schema/auth/入口路由层，给"开第一个付费客户"准备好底座；生产 backend 切到 Postgres。

**Architecture**：在现有 SQLAlchemy 仓储 + AUTO sentinel ContextVar 模式上**水平扩展**——给 4 张业务表加 `workspace_id` 列、新建 2 张 workspace 表、JWT 加 `wid+role`、`@require_permission` 升级跨 workspace 校验。LangGraph checkpointer 表**不动**（spike 验证 `connection_factory` hook 不存在），靠 `threads_meta` `UNIQUE(workspace_id, thread_id)` + 入口路由强校验兜底。

**Tech Stack**：FastAPI / SQLAlchemy 2.x async / PostgreSQL 16 / Alembic / pytest + testcontainers / asyncpg + psycopg / langgraph-checkpoint-postgres 3.0.5。

---

## Context

DeerFlow 多租户改造分 5 个 Stage（详见 [docs/multi-tenant-redesign/README.zh-CN.md](/Users/wangguixuan/work/github/deer-flow/docs/multi-tenant-redesign/README.zh-CN.md)）。Stage 0 的业务目标：把 workspace 概念落到 schema/auth/入口路由层，给"开第一个付费客户"准备好底座；同时把生产 backend 切到 Postgres（Stage 0 没有生产数据，迁移阻力最小，省 Stage 1 重 ALTER 4 张表的返工）。

**Stage 0 不做**：RLS、KMS、ObjectStorage、K8s sandbox、团队 invitation、多档付费、Stripe 对接——这些是 Stage 1+。

**测试基线**：现有 277 个 backend 测试不破坏；新增测试用 testcontainers ephemeral PG（不污染共享 RDS）；CI 加可选 Postgres job。

**远程 PG**：`pgm-bp133hb7gna78yp7vo.pg.rds.aliyuncs.com:5432` 用作个人 dev + 后续生产；密码只入用户本地 `.env`，不进任何 git 跟踪文件。

---

## 横切设计（已锁定）

| 项 | 选择 | 备注 |
|---|---|---|
| DB 列名 | `workspace_id` | 不用 `tenant_id`（ADR 文档保留 `tenant_id` 用语，但代码统一 workspace_id） |
| Python 标识符 | `WorkspaceRow` / `WorkspaceMembershipRow` / `WorkspaceRepository` | |
| JWT claim | `wid` + `role` | Stage 0 一次性加齐两字段，避免 Stage 2 再 bump token_version 全用户重登 |
| ContextVar 模块 | `backend/packages/harness/deerflow/runtime/workspace_context.py` | 仿 `user_context.py` 同款 AUTO 哨兵 |
| ContextVar 名 | `_current_workspace` + `set_current_workspace` + `resolve_workspace_id(value, *, method_name)` + `get_effective_workspace_id()` | `get_effective_*` 不抛错（fallback `"default"`），`resolve_*` 三态严格 |
| id 类型 | `String(36)` (UUID v4) | 跨 SQLite/Postgres 可移植，与 `users.id` 对齐 |
| partial unique | `Index(..., unique=True, sqlite_where=text(...), postgresql_where=text(...))` | 双驱动并存 |
| 文件系统 path | `{base_dir}/workspaces/{wid}/threads/{tid}/...` | 替代 `{base_dir}/users/{uid}/threads/...` |

### Alembic 现状

- `backend/packages/harness/deerflow/persistence/migrations/{alembic.ini, env.py}` 已配，`env.py` 用 `render_as_batch=True` 支持 SQLite ALTER
- `versions/` 目录是空的（只有 `.gitkeep`）—— 现状所有表都靠 `init_engine()` → `Base.metadata.create_all()` 自动建
- **Stage 0 策略**（用户已确认）：
  - **新表**（PR3 workspaces / workspace_memberships、PR8 service_accounts / api_keys / external_users）→ 直接加 ORM 模型，`create_all()` 自动建，**不引入 Alembic 迁移文件**
  - **ALTER 现有表**（PR4 加 `default_workspace_id`、PR5 4 张业务表加 `workspace_id`）→ 必须用 Alembic
  - PR4 创建首个 alembic revision `0001_users_default_workspace.py`；PR5 创建 `0002_business_tables_workspace.py`

### 测试夹具

新建 `backend/tests/fixtures/postgres.py`：
- `postgres_container` (session-scoped) — `testcontainers[postgres]` 起一个 `postgres:16-alpine` 容器
- `postgres_url` (function-scoped) — 每个测试新建 `CREATE SCHEMA test_xxx`，结束后 drop。比 per-test 整库快 100×

新增 autouse fixture `_auto_workspace_context` 注入 `id="test-workspace-autouse"`（保持与现有 `_auto_user_context` 字面对齐）；opt-out 用 `@pytest.mark.no_auto_workspace`。

测试标记策略：`@pytest.mark.postgres` 仅这些跑 PG；既有 277 测试默认仍跑 in-memory/SQLite。

### 老 JWT 兼容

旧 4 字段 token (`{sub, exp, iat, ver}`) 被新 AuthMiddleware 解码 → 缺 `wid` → 401 with code `WORKSPACE_REQUIRED` → 前端引导 `/select-workspace` reissue（前端路径 Stage 1 才完整做，Stage 0 后端 `/api/v1/auth/me` 至少返回 `workspaces[]`）。Stage 0 部署后默认 7 天 token TTL 内自然轮替完。

### 不可逆决策一次性 LOCK 清单

合入 PR3 之前必须团队 review 拍板（来自 [workspace-schema-design §5](/Users/wangguixuan/work/github/deer-flow/docs/multi-tenant-redesign/01-redesign/workspace-schema-design.zh-CN.md#5-不可逆决策清单)）：

1. id 类型 `String(36)` UUID v4
2. 命名 `workspace_id`（DB） / `wid`（JWT）
3. slug 字符集 `^[a-z0-9](-?[a-z0-9])*$` 3-32 字符
4. memberships PK 复合 `(workspace_id, user_id)`
5. TokenPayload 字段集 `{sub, wid, role, exp, iat, ver}`
6. `users.default_workspace_id` 而非 `current_workspace_id`
7. FK 删除策略 ON DELETE CASCADE（workspace 删 → memberships/threads CASCADE）

---

## PR 依赖图

```
PR1 (PG 接入 + testcontainers fixture)
  │
  ├──► PR2 (默认 backend → PG；保留 create_all + alembic 仅 ALTER)
  │     │
  │     ├──► PR3 (workspaces + workspace_memberships 表 + 仓储)
  │     │     │
  │     │     ├──► PR4 (注册改造 + JWT wid+role + ContextVar 注入 + /auth/me + alembic 0001 加 default_workspace_id)
  │     │     │     │
  │     │     │     └──► PR5 (alembic 0002 ALTER 4 表加 workspace_id + 回填 + 改 NOT NULL)
  │     │     │            │
  │     │     │            └──► PR6 (路由 (wid,tid) 强校验 + Paths workspace 化 + 仓储哨兵 + 文件迁移)
  │     │     │                   │
  │     │     │                   └──► PR7 (CI boundary 静态扫描)
  │     │     │
  │     │     └──► PR8 (headless schema only — service_accounts / api_keys / external_users)
  │     │            ※ 仅依赖 PR3 的 workspaces；可与 PR4-PR7 并行
```

**关键路径**：PR1 → PR2 → PR3 → PR4 → PR5 → PR6 → PR7（7 步串行）。**PR8 可与 PR4-PR7 任意时点并行**。

---

## PR1 — Postgres 接入 + testcontainers fixture

### Scope

- docker-compose 加 `postgres` service（dev 用）
- `make doctor` / `scripts/check.py` 兼容 PG 健康检查
- `.env.example` 加 `DATABASE_URL` 模板（仅占位符，真凭据只入用户本地 `.env`）
- `backend/packages/harness/pyproject.toml` 加 optional group `postgres-test`
- `backend/tests/fixtures/postgres.py` + smoke test
- CI: 新加 workflow / job `backend-postgres-tests`，跑 `pytest -m postgres`

**不做**：默认 backend 切换（PR2）、workspace 表（PR3+）、生产 RDS 凭据进 git

### 关键文件

**新增**：
- `backend/tests/fixtures/postgres.py` — testcontainers fixture
- `backend/tests/test_postgres_smoke.py` — `@pytest.mark.postgres` smoke
- `.github/workflows/backend-postgres-tests.yml`（或扩 backend-unit-tests.yml 加 job）

**修改**：
- `docker/docker-compose-dev.yaml` + `docker/docker-compose.yaml` — 加 `postgres:16-alpine` service + healthcheck
- `scripts/doctor.py` / `scripts/check.py` — 加 PG 探测
- `scripts/setup_wizard.py` — 加 "数据库后端" 交互项
- `backend/packages/harness/pyproject.toml` — `optional-dependencies.postgres-test = ["testcontainers[postgres]>=4.0", "pytest-asyncio>=0.23"]`
- `backend/pyproject.toml` — 同步暴露 `postgres-test = ["deerflow-harness[postgres-test]"]`
- `.env.example` — 加 `# DATABASE_URL=postgresql+asyncpg://...` 模板行
- `backend/tests/conftest.py` — register pytest mark `postgres`

### 关键代码草稿

`backend/tests/fixtures/postgres.py`:

```python
import pytest, secrets
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

@pytest.fixture(scope="session")
def postgres_container():
    with PostgresContainer("postgres:16-alpine") as pg:
        pg.start()
        yield pg

@pytest.fixture
async def postgres_url(postgres_container):
    schema = f"test_{secrets.token_hex(8)}"
    raw = postgres_container.get_connection_url().replace("psycopg2", "asyncpg")
    engine = create_async_engine(raw)
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    await engine.dispose()
    yield f"{raw}?options=-csearch_path%3D{schema}"
    engine = create_async_engine(raw)
    async with engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    await engine.dispose()
```

### 测试

1. `test_postgres_smoke.py::test_init_engine_postgres_creates_tables` — 调 `init_engine_from_config(backend='postgres', url=postgres_url)` → 4 张现有表在 `information_schema.tables` 出现
2. `test_postgres_smoke.py::test_init_engine_auto_create_db` — 给一个不存在的 DB 名，验证 engine.py 的 `_auto_create_postgres_db` 触发
3. `test_postgres_smoke.py::test_thread_meta_repo_postgres_round_trip` — `ThreadMetaRepository.create()` + `get()` 一来一回
4. fixture self-test：建 schema → 列表 → drop → 列表都过

### LOCK

- Postgres 镜像 `postgres:16-alpine`（与生产 RDS 大版本对齐——执行 session 第一件事是用提供的 RDS 凭据跑 `SELECT version()` 确认大版本，若不匹配再调）
- 测试用 testcontainers（非 pytest-postgresql）
- per-test schema 隔离 + session-scoped container

### 验收

- [ ] `docker compose -f docker/docker-compose-dev.yaml up postgres` 启动成功，`pg_isready` OK
- [ ] `cd backend && uv sync --group dev --extra postgres-test` 装 testcontainers 成功
- [ ] `cd backend && PYTHONPATH=. uv run pytest -m postgres -v` ≥ 3 通过
- [ ] CI 新 job 绿
- [ ] 既有 277 测试 100% 通过（`make test`）
- [ ] `make doctor`（backend=sqlite 时）不查 PG（不破坏现有用户）

### Tasks (bite-sized, TDD)

> 执行节奏：每条 task 一次 commit；tests 先失败再实现；`@pytest.mark.postgres` 标 postgres-only test。

- [ ] **T1.1 加 postgres 物理依赖**：`backend/packages/harness/pyproject.toml` 加 `postgres-test = ["testcontainers[postgres]>=4.0", "pytest-asyncio>=0.23"]`；`backend/pyproject.toml` 加 `postgres-test = ["deerflow-harness[postgres-test]"]`；跑 `cd backend && uv sync --group dev --extra postgres-test` 验证装上；commit
- [ ] **T1.2 docker-compose-dev 加 postgres**：在 `docker/docker-compose-dev.yaml` `services:` 加 `postgres` block（image, env, ports 5432, healthcheck, volume）+ 顶层 `volumes: postgres-data:`；`docker compose -f docker/docker-compose-dev.yaml up -d postgres` → `docker exec deer-flow-postgres pg_isready -U deerflow` 返 OK；commit
- [ ] **T1.3 写 fixture（testcontainers）**：新建 `backend/tests/fixtures/postgres.py` 含 `postgres_container` (session) + `postgres_url` (function, per-test schema)；`backend/tests/conftest.py` register pytest mark `postgres`；commit
- [ ] **T1.4 写 fixture self-test（先失败）**：新建 `backend/tests/test_postgres_smoke.py::test_postgres_url_creates_isolated_schema`；`pytest -m postgres -v` 应该 PASS（因为只测 fixture）；commit
- [ ] **T1.5 写 init_engine smoke 测试（红→绿）**：写 `test_init_engine_postgres_creates_tables` 调 `init_engine_from_config(backend='postgres', url=postgres_url)`；先用错的参数让它失败一次确认 fixture 真在跑 PG → 改对参数后通过；commit
- [ ] **T1.6 写 thread_meta 仓储 PG round-trip**：`test_thread_meta_repo_postgres_round_trip`：用 `postgres_url` 起 engine → `ThreadMetaRepository.create(...)` + `get(...)` → 断言 dict 字段对齐；commit
- [ ] **T1.7 doctor.py 加 PG 探测**：仅在 `database.backend == 'postgres'` 时调 `asyncpg.connect(url)` + 报 PG version；测试 `database.backend: sqlite` 时不查 PG（regression）；commit
- [ ] **T1.8 setup_wizard.py 加交互**：选数据库后端时新增 postgres 选项 + DATABASE_URL 引导；commit
- [ ] **T1.9 加 CI workflow**：新建 `.github/workflows/backend-postgres-tests.yml`（用 docker service 或让 testcontainers 在 GitHub runner 起 PG）跑 `pytest -m postgres -v`；本地推到 fork 验证 CI 绿；commit
- [ ] **T1.10 验收 + 文档**：跑全套 `cd backend && make test` 验证既有 277 测试不破；`docs/multi-tenant-redesign/03-impl/pr1-postgres-setup.md` 记录 PG 版本对齐结论 + fixture 用法；commit

---

## PR2 — 默认 backend 切到 Postgres

### Scope

- `config.example.yaml` 默认 `database.backend: postgres`，`postgres_url: $DATABASE_URL`
- `.env.example` `DATABASE_URL` 取消注释
- `make setup` / `make dev` / `scripts/serve.sh` / `scripts/check.py` 推荐并 preflight PG
- `docker-compose` 让 `gateway` `depends_on: { postgres: { condition: service_healthy } }`
- 一次性 `scripts/migrate_sqlite_to_postgres.py` 工具（dev 用，可选）

**保留**：SQLite 仍是 valid backend，配 `database.backend: sqlite` 仍可用——dev 兜底  
**不做**：alembic 强制 upgrade（用户已选保留 create_all 自动建表）

### 关键文件

**新增**：
- `scripts/migrate_sqlite_to_postgres.py` — SQLAlchemy reflection 把现有 4 张表数据搬过去
- `docs/multi-tenant-redesign/03-impl/pr2-postgres-default.md`（implementation note，可选）

**修改**：
- `config.example.yaml` — `database` 段默认 postgres
- `.env.example` — 激活 `DATABASE_URL=postgresql+asyncpg://...`
- `scripts/setup_wizard.py` — 默认推荐 postgres
- `scripts/serve.sh` — `--dev` preflight 检查 PG 可达
- `scripts/check.py` — 加 PG 连通性
- `scripts/doctor.py` — 默认就跑 PG 检查
- `docker/docker-compose-{dev,}.yaml` — `gateway depends_on: postgres`
- `backend/CLAUDE.md` — 数据库段
- `README.md` / `Install.md` — install 步骤加 "Start postgres"

### 测试

1. `test_postgres_smoke.py::test_default_config_picks_postgres` — `AppConfig.from_file('config.example.yaml')` → `database.backend == 'postgres'`
2. `test_postgres_smoke.py::test_sqlite_backend_still_works` — 显式 `database.backend: sqlite` 仍能 `init_engine` + 跑全套 thread_meta repo（regression 防丢 SQLite 兼容）
3. `scripts/migrate_sqlite_to_postgres.py` 自检：用 fixture 起 sqlite + pg，迁移 N 行，count 对齐

### LOCK

- `DATABASE_URL` env 字段名（业务系统集成 / on-prem / Stripe webhook 都依赖这个名字）
- `config.example.yaml` 默认值
- Docker compose `gateway depends_on postgres`

### 验收

- [ ] `cp config.example.yaml config.yaml && cp .env.example .env`（填 RDS 密码）→ `make dev` 起服务跑通
- [ ] `make doctor` 报 "Postgres OK"
- [ ] CI 不破（既有 277 + PR1 新 PG 测试都过）
- [ ] `database.backend: sqlite` 配置下 `make test` 仍过

### Tasks

- [ ] **T2.1 切默认 config 为 postgres**：编辑 `config.example.yaml` `database` 段：`backend: postgres` + `postgres_url: $DATABASE_URL`，SQLite 配置移到注释里作为 fallback；commit
- [ ] **T2.2 .env.example 激活 DATABASE_URL**：把现有注释行 `# DATABASE_URL=postgresql+asyncpg://...` 改为活值（占位符密码 `password`）；commit
- [ ] **T2.3 写 sqlite-backend regression 测试**：`test_sqlite_backend_still_works` 显式配 `database.backend: sqlite` → 跑 thread_meta 完整 CRUD；这条要先写、确保后续切默认时 SQLite 不被悄无声息破坏；commit
- [ ] **T2.4 写 default-config 测试**：`test_default_config_picks_postgres` → `AppConfig.from_file('config.example.yaml').database.backend == 'postgres'`；commit
- [ ] **T2.5 docker-compose gateway depends_on postgres**：`docker/docker-compose-dev.yaml` + `docker/docker-compose.yaml` `gateway` 段加 `depends_on: { postgres: { condition: service_healthy } }`；`docker compose up gateway` 验证启动顺序；commit
- [ ] **T2.6 serve.sh / check.py 加 PG preflight**：`--dev` 启动前检测 `psql -c 'SELECT 1'` 失败时报错；`scripts/check.py` 加 PG 连通性检查；commit
- [ ] **T2.7 setup_wizard.py 推荐 PG**：默认选 postgres；用户选择时引导本地 PG 或填远程 URL；commit
- [ ] **T2.8 写 sqlite→pg 数据迁移工具（可选）**：`scripts/migrate_sqlite_to_postgres.py` 用 SQLAlchemy reflection 搬 4 张表；带 `--dry-run`；写自检测试 `test_migrate_sqlite_to_postgres.py`；commit
- [ ] **T2.9 文档**：更新 `backend/CLAUDE.md` 数据库段；`README.md` / `Install.md` install 步骤加 "Start postgres"；commit
- [ ] **T2.10 验收烟雾**：`make stop && rm -rf .deer-flow/data && cp config.example.yaml config.yaml && make dev` → 注册 → 创 thread → 看 PG 中数据；恢复后 commit

---

## PR3 — workspaces + workspace_memberships 表 + 仓储

### Scope

- 新建 `WorkspaceRow` / `WorkspaceMembershipRow` ORM 模型
- 新建 `WorkspaceRepository` / `WorkspaceMembershipRepository`（仿 `ThreadMetaRepository` AUTO sentinel 模式）
- 新建 ContextVar 模块 `backend/packages/harness/deerflow/runtime/workspace_context.py`（仿 `user_context.py`）
- 加单测（仿 `test_thread_meta_repo.py` + `test_user_context.py`）

**不做**：注册流程改造（PR4）、ALTER 现有表（PR5）、路由校验（PR6）、role 扩到 admin/member（Stage 2）

### 关键文件

**新增**（仿现有 `persistence/thread_meta/` 模块结构）：
- `backend/packages/harness/deerflow/persistence/workspace/{__init__.py, model.py, sql.py, base.py}`
- `backend/packages/harness/deerflow/persistence/workspace_membership/{__init__.py, model.py, sql.py}`
- `backend/packages/harness/deerflow/runtime/workspace_context.py`
- `backend/tests/test_workspace_repo.py`
- `backend/tests/test_workspace_membership_repo.py`
- `backend/tests/test_workspace_context.py`

### Schema 草稿

`workspace/model.py`:
```python
class WorkspaceRow(Base):
    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    slug: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    owner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))
```

`workspace_membership/model.py`:
```python
class WorkspaceMembershipRow(Base):
    __tablename__ = "workspace_memberships"
    workspace_id: Mapped[str] = mapped_column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # Stage 0 仅写 'owner'，schema 允许 'owner/admin/member'
    invited_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (
        Index("idx_workspace_memberships_user", "user_id", "workspace_id"),  # 倒查索引：列 user 所有 workspace
        Index(
            "idx_one_owner_per_workspace",
            "workspace_id",
            unique=True,
            sqlite_where=text("role = 'owner'"),
            postgresql_where=text("role = 'owner'"),
        ),
    )
```

`runtime/workspace_context.py`：完全仿 `user_context.py`，提供 `_current_workspace` ContextVar、`set_current_workspace` / `reset_current_workspace`、`AUTO` sentinel、`resolve_workspace_id(value, *, method_name)` 三态、`get_effective_workspace_id()` fallback `"default"`、`require_current_workspace()` 抛错版本、`CurrentWorkspace` Protocol（仅要求 `.id: str` 和 `.role: str`）。

`workspace/sql.py` `WorkspaceRepository` 方法（≥6 个，对齐 ThreadMetaRepository 风格）：
- `create(name, slug, owner_id, *, workspace_id=None)` → 自动生成 UUID 若没传
- `get(workspace_id, *, user_id=AUTO)` — 校验 user 是该 workspace member 才返
- `get_by_slug(slug)` — slug 唯一可任意查（不带 user 校验，public lookup）
- `list_by_user(user_id=AUTO)` — 倒查
- `update_status(workspace_id, status)`
- `delete(workspace_id)` — CASCADE 删 memberships

`WorkspaceMembershipRepository`：`add(workspace_id, user_id, role)` / `remove(workspace_id, user_id)` / `list_by_workspace(workspace_id)` / `list_by_user(user_id)` / `get_role(workspace_id, user_id)` / `change_role(workspace_id, user_id, new_role)`

### slug 黑名单

应用层校验（不写 DB constraint），常量列在 `workspace/sql.py`，包括 [workspace-schema-design §2.1](/Users/wangguixuan/work/github/deer-flow/docs/multi-tenant-redesign/01-redesign/workspace-schema-design.zh-CN.md#21-workspaces) 列出的 ~30 个保留字。

### 测试

`test_workspace_repo.py`（≥5 个用例）：
1. CRUD smoke（create / get / list / update_status / delete）
2. slug 唯一性约束（重复 slug → IntegrityError）
3. slug 黑名单 → ValueError
4. status 状态机（active → suspended → deleted）
5. CASCADE 删 workspace 后 memberships 消失（这条要起 PG fixture，SQLite 也支持 ON DELETE CASCADE 但要 `PRAGMA foreign_keys=ON`，engine.py 已开）

`test_workspace_membership_repo.py`（≥4 个用例）：
1. add / remove / list_by_user / list_by_workspace
2. 同一 workspace 不能加第二个 owner（partial unique index）—— sqlite + pg 都跑
3. CASCADE 删 user 后 memberships 消失
4. `list_by_user` 返回顺序

`test_workspace_context.py`（≥4 个用例，仿 `test_user_context.py`）：
1. AUTO 三态（实际 user / 显式 str / 显式 None）
2. `set_current_workspace` + `reset_current_workspace` 不污染其他任务
3. `get_effective_workspace_id` fallback "default"
4. `require_current_workspace` 抛 RuntimeError

### LOCK

- 表名 `workspaces` / `workspace_memberships`
- PK 类型 `String(36)` UUID v4
- memberships 复合 PK `(workspace_id, user_id)`
- FK CASCADE 策略
- partial unique on owner（双 sqlite_where + postgresql_where）
- role 用 `String(16)` 而非 PG enum（避免 ALTER TYPE 痛苦）

### 验收

- [ ] 13 个新单测全过（PR1 fixture 加持下，partial unique 在 PG + SQLite 都验证）
- [ ] `make test` 既有 277 + 13 新测全过
- [ ] CASCADE / partial unique 在 testcontainers PG 上验证

### Tasks

> 严格 TDD：先 schema → 仓储测试（红） → 仓储实现 → 测试绿 → ContextVar → 集成。

- [ ] **T3.1 ContextVar 模块**：新建 `backend/packages/harness/deerflow/runtime/workspace_context.py` 完全仿 `user_context.py`（`_current_workspace` ContextVar、`_AutoSentinel`、`AUTO`、`set_current_workspace` / `reset_current_workspace`、`resolve_workspace_id(value, *, method_name)` 三态、`get_effective_workspace_id()` fallback `"default"`、`require_current_workspace()`、`CurrentWorkspace` Protocol）；commit
- [ ] **T3.2 ContextVar 单测（红→绿）**：`backend/tests/test_workspace_context.py` 4 用例：AUTO 三态、set/reset 不污染、`get_effective_workspace_id` fallback、`require_current_workspace` 抛错；先跑应该 import 失败 → 写完 T3.1 后跑应 PASS；commit
- [ ] **T3.3 WorkspaceRow ORM**：新建 `backend/packages/harness/deerflow/persistence/workspace/{__init__.py, model.py}`；按 plan 草稿建表 + slug UNIQUE + owner_id FK ON DELETE RESTRICT；commit
- [ ] **T3.4 WorkspaceRepo 仓储 + 单测（红）**：新建 `persistence/workspace/{base.py, sql.py}` 接口 + 实现；`backend/tests/test_workspace_repo.py` 5 用例（CRUD smoke / slug 唯一 / slug 黑名单 / status 状态机 / CASCADE）；先跑应有红；commit
- [ ] **T3.5 WorkspaceRepo 实现到绿**：补全方法 `create / get / get_by_slug / list_by_user / update_status / delete`；slug 黑名单常量 + 应用层校验；用 `@pytest.mark.postgres` 跑 PG fixture；commit
- [ ] **T3.6 WorkspaceMembershipRow ORM**：新建 `persistence/workspace_membership/{__init__.py, model.py}` 复合 PK + partial unique on owner（`sqlite_where` + `postgresql_where` 双维护）；commit
- [ ] **T3.7 WorkspaceMembershipRepo + 单测**：`persistence/workspace_membership/sql.py` `add / remove / list_by_user / list_by_workspace / get_role / change_role`；`backend/tests/test_workspace_membership_repo.py` 4 用例（add/remove smoke / 不能加第二个 owner / CASCADE 删 user / list_by_user 顺序）；commit
- [ ] **T3.8 partial unique on owner 双驱动验证**：单独写 `test_one_owner_per_workspace_partial_unique` 同时跑 SQLite 和 `@pytest.mark.postgres` PG，确认两边都拒第二个 owner；commit
- [ ] **T3.9 ORM 注册到 Base.metadata**：在 `persistence/__init__.py`（或合适位置）import 新 model 让 `create_all()` 自动建；跑 fresh fixture 起 PG → 验证 `workspaces` + `workspace_memberships` 表存在；commit
- [ ] **T3.10 验收**：`make test` 既有 277 + 13 新测全过；checklist 走完 commit

---

## PR4 — 注册改造 + JWT 加 wid+role + AuthMiddleware ContextVar 注入 + /auth/me + alembic 0001

### Scope

- `auth/jwt.py` `TokenPayload` 加 `wid` + `role` 字段
- `auth_middleware.py` dispatch 在 `set_current_user` 之后注入 `set_current_workspace`
- `routers/auth.py` `/auth/initialize` 创建首个 admin 时同步建 default workspace + membership；`/auth/register` 同样
- `routers/auth.py` `/auth/me` 返回值加 `workspaces: [{id, name, slug, role}]`
- `users.default_workspace_id` 列：alembic revision `0001_users_default_workspace.py`（PR4 内单独建）
- 旧 4 字段 token 兼容：解码缺 `wid` → 401 with code `WORKSPACE_REQUIRED`
- `_ensure_admin_user(app)` lifespan 扩：建完 admin 顺带建 default workspace（兼容老部署）

**不做**：前端 picker（Stage 1）、role 真的开放 admin/member（Stage 2）、邀请流程（Stage 2）

### 关键文件

**新增**：
- `backend/packages/harness/deerflow/persistence/migrations/versions/0001_users_default_workspace.py`
- `backend/tests/test_auth_jwt_workspace.py`
- `backend/tests/test_auth_middleware_workspace.py`
- `backend/tests/test_register_creates_workspace.py`
- `backend/tests/test_legacy_token_compat.py`

**修改**：
- `backend/app/gateway/auth/jwt.py` — `TokenPayload` 加 `wid`/`role`；`create_access_token` 签发；`decode_token` 不在解码层抛错（缺字段返 `WorkspaceMissingError` 让 middleware 处理 401）
- `backend/app/gateway/auth/models.py` — `User` 加 `default_workspace_id: str | None`；新增 `UserMeWorkspace(BaseModel)`、`UserMeResponse(workspaces: list[UserMeWorkspace])`
- `backend/app/gateway/auth_middleware.py` — `dispatch` line 122 后加 workspace ContextVar 注入；try/finally 同时 reset 两个；缺 `wid` 走 `WORKSPACE_REQUIRED` 401
- `backend/app/gateway/routers/auth.py`：
  - `initialize_admin` (line 429-458)：建完 admin 后调 `WorkspaceRepository.create(...)` + `WorkspaceMembershipRepository.add(role='owner')` + `users.default_workspace_id = workspace.id`
  - `register` (line 304-323)：每个新用户同样
  - `change_password` (line 328-375)：bump token_version 后重签 JWT 时带上 `wid`/`role`
  - `get_me` (line 378-382)：返回值改 `UserMeResponse`，列出该 user 所有 memberships
- `backend/packages/harness/deerflow/persistence/user/model.py` — 加 `default_workspace_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True)`
- `backend/app/gateway/app.py` `_ensure_admin_user` — 扩：建完 admin 顺带建 default workspace；已存在 admin 时若无 workspace 也回填一次

### alembic revision 0001 草稿

`0001_users_default_workspace.py`:
```python
def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("default_workspace_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_users_default_workspace",
            "workspaces",
            ["default_workspace_id"],
            ["id"],
            ondelete="SET NULL",
        )
```
注意：alembic 的 baseline——本 PR 之前 `versions/` 是空的，所以 0001 是首个 revision，`down_revision = None`。

### TokenPayload 草稿

```python
class TokenPayload(BaseModel):
    sub: str
    wid: str           # workspace_id（Stage 0 新增）
    role: str          # 'owner' / 'admin' / 'member'（Stage 0 仅写 owner）
    exp: datetime
    iat: datetime | None = None
    ver: int = 0
```

### auto_slug_from_email 算法

```python
def auto_slug_from_email(email: str) -> str:
    """foo.bar+spam@example.com → 'foo-bar'。冲突时尾加 '-2', '-3'..."""
    local = email.split("@", 1)[0]
    local = re.sub(r"[+_\.]", "-", local).lower()
    local = re.sub(r"[^a-z0-9-]", "", local)
    local = re.sub(r"-+", "-", local).strip("-")
    return local[:32] or f"user-{secrets.token_hex(4)}"
```
slug 冲突时仓储层自动加后缀（`{slug}-2`、`{slug}-3`）。

### 测试

`test_auth_jwt_workspace.py`（≥4 用例）：
1. 新签 JWT 带 `wid`/`role` 字段
2. 解码新 JWT → TokenPayload 含 `wid`/`role`
3. 解码旧 4 字段 token → 返回 `WorkspaceMissingError`（不是 ValidationError）
4. `change_password` 后重签的 JWT `ver` bump 且带 `wid`

`test_auth_middleware_workspace.py`（≥4 用例）：
1. 带新 JWT 请求 → ContextVar 中能读到 user + workspace
2. 带旧 JWT 请求 → 401 `WORKSPACE_REQUIRED`
3. 公共白名单路径不要求 wid（如 `/api/v1/auth/login/local`）
4. ContextVar 在 try/finally 块正确 reset（asyncio task 间不污染）

`test_register_creates_workspace.py`（≥3 用例）：
1. POST `/api/v1/auth/register` → DB 中可见 1 user + 1 workspace + 1 owner membership + JWT cookie 含 `wid`
2. 同 email 注册第二次 → 409
3. 多个用户注册 → 各自 workspace 隔离，slug 不冲突

`test_legacy_token_compat.py`（≥2 用例）：
1. 用旧版 4 字段 token 调 `/auth/me` → 401 `WORKSPACE_REQUIRED`
2. `/auth/me` 返回 `workspaces[]` 字段且至少含一个 owner

### LOCK

- TokenPayload 字段集 `{sub, wid, role, exp, iat, ver}` （Stage 0 一次加齐，Stage 2 不再 bump）
- ContextVar 模块路径 `runtime/workspace_context.py`
- `auto_slug_from_email` 算法
- 401 错误码 `WORKSPACE_REQUIRED`
- alembic revision 命名规则 `{NNNN}_{snake_case_description}.py`

### 风险与缓解

| 风险 | 缓解 |
|---|---|
| `_ensure_admin_user` lifespan 在 admin 已存在但无 workspace 的"中间态"用户上跑空 | 一次性 idempotent 回填：每次启动都检查 admin 是否有 workspace，无则补 |
| 旧 cookie 用户突发 401 影响体验 | `/auth/me` 401 时前端检测 `WORKSPACE_REQUIRED` → 自动调 `/auth/logout` → 重定向到 `/login` |
| `change_password` 后 token_version bump 但漏带 wid | TestClient 集成测试覆盖 |

### 验收

- [ ] 13+ 个新单测过 + 既有 277 测试 100% 过
- [ ] alembic revision `0001` 在 PG 和 SQLite 上都能 upgrade + downgrade
- [ ] 手工 smoke：注册新用户 → 看 DB 有 1 workspace + 1 owner membership + cookie 含 wid

### Tasks

> 有 alembic 介入。先确认 alembic baseline 行为（开放问题 #2）；本 PR 是首个 revision。

- [ ] **T4.1 确认 alembic baseline**：跑 `cd backend && PYTHONPATH=. uv run alembic current` 看默认行为；如果发现需要 `alembic stamp head` 之类的 baseline，加到 doctor.py 自动检测；commit（可能空 commit + 文档说明）
- [ ] **T4.2 alembic revision 0001（红→绿）**：手写 `migrations/versions/0001_users_default_workspace.py` upgrade 用 `op.batch_alter_table("users")` 加 `default_workspace_id` 列 + FK；downgrade 反向；本地 PG fixture 跑 `alembic upgrade head` + `alembic downgrade -1` 都成功；commit
- [ ] **T4.3 写 alembic 测试**：`test_alembic_default_workspace_id.py` 起 fresh PG → upgrade 0001 → 验证 `users.default_workspace_id` 列存在 + FK 指向 workspaces；downgrade → 列消失；SQLite 同样跑一次（batch_alter_table 兼容）；commit
- [ ] **T4.4 UserRow model 加 default_workspace_id**：`persistence/user/model.py` 加列定义对齐 alembic schema；commit
- [ ] **T4.5 TokenPayload 扩字段（红）**：写 `test_auth_jwt_workspace.py::test_jwt_includes_wid_and_role`（先红）；改 `auth/jwt.py` `TokenPayload` + `create_access_token` 接受 wid/role 参数；测试转绿；commit
- [ ] **T4.6 旧 token 兼容**：写 `test_legacy_token_compat.py::test_decode_legacy_token_returns_workspace_missing_error`；`decode_token` 检测缺 `wid` 时返回 `WorkspaceMissingError`（新建 error class）；测试转绿；commit
- [ ] **T4.7 AuthMiddleware ContextVar 注入（红→绿）**：写 `test_auth_middleware_workspace.py` 4 用例（带新 JWT/带旧 JWT/公共白名单/try-finally reset）；改 `auth_middleware.py` line 122 后注入 `set_current_workspace`；缺 wid 走 401 `WORKSPACE_REQUIRED`；测试转绿；commit
- [ ] **T4.8 /auth/initialize 建 default workspace（红→绿）**：写 `test_register_creates_workspace.py::test_initialize_creates_admin_with_default_workspace`；改 `routers/auth.py:initialize_admin` 调 `WorkspaceRepository.create + MembershipRepository.add(role='owner') + users.default_workspace_id = ws.id`；测试转绿；commit
- [ ] **T4.9 /auth/register 建 default workspace（红→绿）**：写同款 `test_register_creates_default_workspace`；改 `routers/auth.py:register`；commit
- [ ] **T4.10 auto_slug_from_email helper**：实现按 plan 草稿；slug 冲突自动加后缀 `-2/-3`；写 `test_auto_slug_collision`；commit
- [ ] **T4.11 /auth/me 返回 workspaces[]**：扩 `auth/models.py` 加 `UserMeWorkspace` + `UserMeResponse`；改 `routers/auth.py:get_me` 返回 memberships；写 `test_auth_me_returns_workspaces`；commit
- [ ] **T4.12 change_password 重签 JWT 带 wid**：写 `test_change_password_keeps_wid`；改 `routers/auth.py:change_password` 重签时带上当前 user.default_workspace_id + role；commit
- [ ] **T4.13 lifespan _ensure_admin_user 扩**：在 `app.py` `_ensure_admin_user` 建完 admin 后建 default workspace；已存在 admin 但无 workspace 时回填一次（idempotent）；写 `test_ensure_admin_user_workspace_backfill`；commit
- [ ] **T4.14 验收烟雾**：`make stop && make dev` → 注册 → 检查 DB 有 1 workspace + 1 owner membership + cookie 含 wid + `/auth/me` 返回 workspaces[]；commit final

---

## PR5 — alembic 0002 ALTER 4 表 + 回填 + 改 NOT NULL

### Scope

- alembic revision `0002_business_tables_workspace.py`：threads_meta / runs / feedback / run_events 加 `workspace_id String(36)` 列（先 nullable）+ 复合索引
- `scripts/backfill_workspace_id.py`（带 `--dry-run`）：扫现有 user → 用 `users.default_workspace_id` UPDATE 4 张表；残留 `user_id IS NULL` 行 → 兜底 `workspaces.id = legacy_workspace`
- alembic revision `0003_business_tables_workspace_not_null.py`：回填后 ALTER 改 NOT NULL + 加 UNIQUE(workspace_id, thread_id)（仅 threads_meta）
- 4 个 ORM model 加 `workspace_id` 字段定义（与 alembic schema 对齐）

**不做**：仓储层加 workspace_id 哨兵参数（PR6）、路由校验（PR6）、文件系统迁移（PR6）

### 关键文件

**新增**：
- `backend/packages/harness/deerflow/persistence/migrations/versions/0002_business_tables_workspace.py`
- `backend/packages/harness/deerflow/persistence/migrations/versions/0003_business_tables_workspace_not_null.py`
- `scripts/backfill_workspace_id.py`
- `backend/tests/test_alembic_business_tables.py`
- `backend/tests/test_backfill_workspace_id.py`

**修改**：
- `backend/packages/harness/deerflow/persistence/thread_meta/model.py` — 加 workspace_id 列定义 + UNIQUE 复合索引
- `backend/packages/harness/deerflow/persistence/run/model.py` — 加 workspace_id 列定义
- `backend/packages/harness/deerflow/persistence/feedback/model.py` — 加 workspace_id 列定义
- `backend/packages/harness/deerflow/persistence/models/run_event.py` — 加 workspace_id 列定义

### legacy_workspace 哨兵

UUID 用 `00000000-0000-0000-0000-000000000000`（采纳 plan agent 推荐的标准 nil UUID，便于识别）；Stage 0 启动时 `_ensure_admin_user` 检查若不存在则建（owner = platform admin）。

### 回填脚本草稿

```python
# scripts/backfill_workspace_id.py
async def backfill(dry_run: bool):
    # Step 1: 给每个无 default_workspace_id 的 user 建 workspace
    for user in await user_repo.list_without_default_workspace():
        if dry_run:
            print(f"WOULD create workspace for {user.email}")
            continue
        ws = await workspace_repo.create(name=f"{user.email_prefix}'s Workspace", slug=...)
        await membership_repo.add(ws.id, user.id, "owner")
        await user_repo.update_default_workspace(user.id, ws.id)
    # Step 2: UPDATE 4 张表
    for table in (threads_meta, runs, feedback, run_events):
        sql = f"""
            UPDATE {table} SET workspace_id = users.default_workspace_id
            FROM users WHERE {table}.user_id = users.id AND {table}.workspace_id IS NULL
        """
        if dry_run:
            cnt = await session.execute(text(sql.replace("UPDATE", "SELECT count(*) FROM"))).scalar()
            print(f"WOULD update {cnt} rows in {table}")
        else:
            await session.execute(text(sql))
    # Step 3: 残留无 user 行 → legacy_workspace
    for table in (threads_meta, runs, feedback, run_events):
        sql = f"UPDATE {table} SET workspace_id = '00000000-0000-0000-0000-000000000000' WHERE workspace_id IS NULL"
        if dry_run:
            cnt = await session.execute(text(...))
        else:
            await session.execute(text(sql))
```

幂等：每步都 `WHERE workspace_id IS NULL`，可重跑。

### 测试

`test_alembic_business_tables.py`（≥4 用例，跑 PG fixture）：
1. revision 0002 upgrade → 4 张表都有 `workspace_id` 列且 nullable
2. revision 0002 downgrade → 列消失（且不破坏数据）
3. revision 0003 upgrade（前提先跑 0002 + 回填）→ NOT NULL 生效；threads_meta 加了 UNIQUE(wid, tid)
4. UNIQUE 约束触发：同 wid + 同 tid 第二次 INSERT 抛 IntegrityError

`test_backfill_workspace_id.py`（≥5 用例）：
1. 空数据库 → 0 行更新
2. 1 个 user + 3 thread → workspace 建好 + 3 thread 都回填
3. 多 user → 各自 workspace 隔离
4. `--dry-run` 不写
5. 残留无 owner 行（user_id NULL）→ 进 legacy_workspace

### LOCK

- alembic revision 编号
- legacy_workspace UUID `00000000-0000-0000-0000-000000000000`
- ON DELETE CASCADE（workspace 删 → threads/runs/feedback/run_events 全 cascade）
- 复合索引名 `idx_threads_meta_workspace_thread`（UNIQUE）+ `idx_threads_meta_workspace_user_updated`

### 风险与缓解

| 风险 | 缓解 |
|---|---|
| 回填脚本中途崩 | 幂等设计 + transaction per table；下次重跑从 `WHERE workspace_id IS NULL` 接续 |
| 改 NOT NULL 时仍有 NULL 行 | 0003 upgrade 前 SELECT count(\*) WHERE workspace_id IS NULL；非零拒绝迁移 |
| SQLite 加 NOT NULL 列要 `batch_alter_table` | env.py 已 `render_as_batch=True` |
| 远程 RDS 上 ALTER 大表锁表 | Stage 0 数据量小可接受；Stage 1+ 上量后用 `ALTER TABLE ... ADD COLUMN` 不锁的形式（PG 11+ 默认） |

### 验收

- [ ] 9+ 新单测过 + 既有 277 + PR3/PR4 新测全过
- [ ] 0002 + 0003 在 PG 和 SQLite 都能 upgrade
- [ ] `python scripts/backfill_workspace_id.py --dry-run` 输出可读 + 不写库
- [ ] 实际跑回填后，4 张表 NOT NULL 检查 0 行违例

### Tasks

> 关键策略：先 nullable ALTER → 跑回填 → 改 NOT NULL。中间不能爆。

- [ ] **T5.1 alembic 0002 upgrade（红→绿）**：写 `migrations/versions/0002_business_tables_workspace.py` `op.batch_alter_table` 4 张表加 `workspace_id String(36)` 列（**nullable**）+ FK 到 workspaces（ON DELETE CASCADE）+ 复合索引 `idx_threads_meta_workspace_user_updated`；downgrade 反向；本地跑 `alembic upgrade head` 成功；commit
- [ ] **T5.2 alembic 0002 测试**：`test_alembic_business_tables.py::test_upgrade_0002_adds_workspace_id_column` PG 和 SQLite 都跑；4 张表都有 nullable workspace_id 列；commit
- [ ] **T5.3 4 个 model.py 加 workspace_id 字段**：`thread_meta/run/feedback/run_event` 4 个 model.py 加 `workspace_id: Mapped[str | None]` 列定义对齐 alembic schema；先标 nullable=True；commit
- [ ] **T5.4 backfill 脚本骨架**：新建 `scripts/backfill_workspace_id.py` argparse 接 `--dry-run`；空实现先；commit
- [ ] **T5.5 backfill Step 1（每 user 建 workspace）+ 测试**：写 `test_backfill_workspace_id.py::test_creates_workspace_per_user_without_default`；实现脚本第 1 步：扫现有 user → 建 workspace + membership + 回填 default_workspace_id；幂等（`WHERE default_workspace_id IS NULL`）；commit
- [ ] **T5.6 backfill Step 2（UPDATE 4 表）+ 测试**：`test_backfill_updates_4_tables_from_users` 验证 UPDATE FROM users 关联回填；commit
- [ ] **T5.7 backfill Step 3（残留 → legacy_workspace）+ 测试**：`test_backfill_orphan_rows_go_to_legacy_workspace`；如 legacy_workspace 不存在则建（id `00000000-0000-0000-0000-000000000000`，owner = platform admin）；commit
- [ ] **T5.8 dry-run 模式测试**：`test_backfill_dry_run_does_not_write` 验证 `--dry-run` 只打印不写；commit
- [ ] **T5.9 alembic 0003（NOT NULL + UNIQUE）**：写 `migrations/versions/0003_business_tables_workspace_not_null.py`：upgrade 前 SELECT count(*) WHERE workspace_id IS NULL → 非 0 raise；改 NOT NULL；threads_meta 加 `idx_threads_meta_workspace_thread UNIQUE`；commit
- [ ] **T5.10 alembic 0003 测试**：`test_alembic_business_tables.py::test_upgrade_0003_requires_no_null_workspace_id` 故意留 NULL 行 → upgrade 应 raise；先填 → upgrade 应过；UNIQUE 约束触发测试 `test_threads_meta_unique_workspace_thread`；commit
- [ ] **T5.11 4 个 model.py 改 NOT NULL**：4 个 model.py `workspace_id` 改 `nullable=False`；commit
- [ ] **T5.12 验收烟雾**：本地 PG 完整跑：`alembic upgrade 0002` → `python scripts/backfill_workspace_id.py` → `alembic upgrade 0003` → `psql -c "SELECT count(*) FROM threads_meta WHERE workspace_id IS NULL"` 应返 0；`make test` 不破；commit final

---

## PR6 — 入口路由强校验 + Paths workspace 化 + 仓储 workspace_id 哨兵 + 文件迁移

### Scope

- 4 个仓储（ThreadMetaRepository / RunRepository / FeedbackRepository / RunEventRepository）的所有方法加 `workspace_id: str | None | _AutoSentinel = AUTO` 参数 + WHERE 子句
- `ThreadMetaRepository.check_access` 升级为 `check_access(thread_id, user_id, workspace_id, ...)` 三参数
- `@require_permission` 装饰器升级：`owner_check=True` 调 `check_access(thread_id, user_id, workspace_id)`，跨 workspace 返 **404 不是 403**（防 enumeration leak）
- `routers/threads.py` `POST /api/threads` create thread_meta 时写 `workspace_id`；`DELETE` / `PATCH` 经 `@require_permission` 已自动校验
- `routers/thread_runs.py` 三个 endpoint 都自动经 `@require_permission` 走升级后的校验
- `Paths` 切 workspace 维度：`thread_dir(thread_id, workspace_id)` 返回 `{base}/workspaces/{wid}/threads/{tid}/...`；保留 user_id 参数走 legacy fallback（若 workspace_id 是 None）
- `ThreadDataMiddleware` 用 `get_effective_workspace_id()` 替代 `get_effective_user_id()`
- `Memory` / `agent SOUL` 路径同样：`{base}/workspaces/{wid}/users/{uid}/memory.json` 等
- `scripts/migrate_paths_to_workspace.py`（带 `--dry-run`）：扫 `{base}/users/{uid}/threads/...` 搬到 `{base}/workspaces/{wid}/threads/...`
- lifespan 加 `_check_path_migration_pending(app)`：探测到 `users/` 目录还有内容时 log warning 引导用户跑 `make migrate-paths`
- `Makefile` 加 `migrate-paths` target → `cd backend && PYTHONPATH=. uv run python ../scripts/migrate_paths_to_workspace.py`
- CI boundary 测试**先**就位（不算 PR7，PR7 是更广的扫描）：`test_workspace_isolation_boundary.py` 起两个 workspace 各创 thread → 互调对方 thread → 必 404

**不做**：`tenant_*` → `workspace_*` 大规模 rename（ADR 文档保留 tenant 用语）；frontend picker（Stage 1）；workspace switching（Stage 1）

### 关键文件

**新增**：
- `scripts/migrate_paths_to_workspace.py`（带 --dry-run，仿 `scripts/migrate_user_isolation.py`）
- `backend/tests/test_workspace_isolation_boundary.py` — 跨 workspace 404 e2e
- `backend/tests/test_paths_workspace.py` — Paths workspace 化单测
- `backend/tests/test_thread_meta_workspace_filter.py` — 仓储 workspace_id 过滤单测
- `backend/tests/test_require_permission_workspace.py` — 装饰器升级单测
- `backend/tests/test_thread_data_middleware_workspace.py` — middleware 用 workspace path

**修改**：
- `backend/packages/harness/deerflow/persistence/thread_meta/{base.py, sql.py, memory.py}` — 所有方法加 workspace_id 哨兵参数 + SQL WHERE
- `backend/packages/harness/deerflow/persistence/run/sql.py` — 同上
- `backend/packages/harness/deerflow/persistence/feedback/sql.py` — 同上
- `backend/packages/harness/deerflow/persistence/models/run_event.py` — 仓储同上（如有 repo）
- `backend/app/gateway/authz.py` — `@require_permission` `owner_check=True` 时同时取 workspace_id 校验
- `backend/app/gateway/routers/threads.py` — `POST /api/threads` 写 workspace_id；其它路由经装饰器自动
- `backend/packages/harness/deerflow/config/paths.py` — `thread_dir` / `user_memory_file` / `user_agents_dir` 等加 `workspace_id` 参数
- `backend/packages/harness/deerflow/agents/middlewares/thread_data_middleware.py` — `before_agent` 用 `get_effective_workspace_id()`
- `backend/app/gateway/app.py` `_check_path_migration_pending` — lifespan 探测
- `Makefile` — 加 `migrate-paths` target

### check_access 升级

```python
async def check_access(
    self,
    thread_id: str,
    user_id: str,
    workspace_id: str,    # 新增；不接受 None
    *,
    require_existing: bool = False,
) -> bool:
    """跨 workspace 一律 False（让上层装饰器返 404 而不是 403）。"""
    async with self._sf() as session:
        row = await session.get(ThreadMetaRow, thread_id)
        if row is None:
            return not require_existing
        # 第一道：跨 workspace 直接拒（即使 user 在该 thread 上有名）
        if row.workspace_id != workspace_id:
            return False
        # 第二道：thread 在本 workspace 内的 user 校验（保持原语义）
        if row.user_id is None:
            return True
        return row.user_id == user_id
```

### `@require_permission` 装饰器升级

```python
@require_permission("threads", "delete", owner_check=True, require_existing=True)
async def delete_thread(thread_id: str, request: Request):
    ...
```

装饰器内部：
```python
auth = request.state.auth
workspace_id = request.state.workspace.id  # 来自 AuthMiddleware ContextVar
ok = await thread_store.check_access(
    thread_id,
    str(auth.user.id),
    workspace_id,
    require_existing=require_existing,
)
if not ok:
    raise HTTPException(404)  # 跨 workspace 返 404 不是 403
```

### 路径形态

新：`{base_dir}/workspaces/{wid}/threads/{tid}/user-data/{workspace,uploads,outputs}/`
旧：`{base_dir}/users/{uid}/threads/{tid}/user-data/...`（lifespan 探测到则 warning）

`paths.py::thread_dir()` 兼容两条形态：传 `workspace_id` 走新；只传 `user_id` 走 legacy 兼容（PR6 后 lifespan 在生产环境探测到 legacy 路径会 warning + 引导跑 `make migrate-paths`）。

### 测试

`test_workspace_isolation_boundary.py`（核心 e2e，≥3 用例）：
1. 起两 workspace 各创 thread → 用 workspace A 的 cookie 访问 workspace B 的 thread → 404
2. workspace A 的 thread DELETE 被 workspace B 调 → 404
3. workspace A 的 thread RUN STREAM 被 workspace B 调 → 404

`test_paths_workspace.py`（≥4 用例）：
1. `thread_dir(tid, workspace_id=wid)` 返回 `{base}/workspaces/{wid}/threads/{tid}/...`
2. `thread_dir(tid, user_id=uid)` 返回 legacy `{base}/users/{uid}/threads/{tid}/...`（兼容）
3. `ensure_thread_dirs(tid, wid)` 创建目录树
4. path traversal 防御（注入 `../` 拒绝）

`test_thread_meta_workspace_filter.py`（≥5 用例）：
1. `create(thread_id, workspace_id=AUTO)` 用 ContextVar
2. `get(thread_id)` 跨 workspace 返 None
3. `search(workspace_id=AUTO)` 仅本 workspace
4. `get(thread_id, workspace_id=None)` 显式 bypass（迁移用）
5. 索引前导列校验（`idx_threads_meta_workspace_user_updated` EXPLAIN 走索引）

`test_require_permission_workspace.py`（≥3 用例）：
1. 跨 workspace DELETE → 404
2. 同 workspace 同 user DELETE → 200
3. 同 workspace 不同 user 但 admin → 当前 Stage 0 是 403（admin role 行为 Stage 2 才扩；Stage 0 默认是 owner-only）

`test_thread_data_middleware_workspace.py`（≥2 用例）：
1. ContextVar 有 workspace → 创建 `{base}/workspaces/{wid}/threads/{tid}/...`
2. 无 workspace → fallback `default` workspace（避免 no-auth dev 模式炸）

### LOCK

- 路径形态 `{base}/workspaces/{wid}/threads/{tid}/`
- 跨 workspace 返 **404 不是 403**
- `check_access` 签名顺序 `(thread_id, user_id, workspace_id, *, require_existing)`

### 风险与缓解

| 风险 | 缓解 |
|---|---|
| 老 dev 数据 path 没迁移 → ThreadDataMiddleware 写到老路径 → 数据不一致 | lifespan 探测 + warning；`migrate_paths_to_workspace.py --dry-run` 让用户先看 diff |
| 仓储参数顺序变更破坏调用方 | 全部 keyword-only，新增 `workspace_id` 在 `*` 之后；调用方不传则 AUTO |
| 跨 workspace 404 不利于 debug | server log 记 `cross_workspace_access_attempt={uid, wid_actual, wid_requested}` |

### 验收

- [ ] 17+ 新单测过 + 既有 277 + PR3/PR4/PR5 新测全过
- [ ] `python scripts/migrate_paths_to_workspace.py --dry-run` 输出可读
- [ ] 实际跑迁移后 lifespan warning 消失
- [x] 手工 smoke：起 dev 服务，注册两个 user → 创各自 thread → 互访 404 ✅ 2026-06-27 `multi_tenant.py` PASS（N 租户并发版，含 search 不泄漏 + 跨租户 GET 404）

### Tasks

> 这是最大的一个 PR。建议拆 `T6.{1-15}` 提交，每条独立 commit。强 TDD：每个仓储改造都先红再绿。

- [ ] **T6.1 ThreadMetaRepository.create 加 workspace_id 哨兵（红→绿）**：写 `test_thread_meta_workspace_filter.py::test_create_uses_workspace_context`；改 `thread_meta/sql.py:create()` 加 `workspace_id: str | None | _AutoSentinel = AUTO` 参数 + INSERT 时写入；测试转绿；commit
- [ ] **T6.2 ThreadMetaRepository.get 改 SQL WHERE（红→绿）**：写 `test_thread_meta_get_filters_by_workspace`：跨 workspace get 返 None；改 `get()` 用 `WHERE workspace_id == :wid`（替代当前 line 70 应用层 if）；commit
- [ ] **T6.3 ThreadMetaRepository.search/update_*/delete 加 workspace_id 哨兵**：所有方法签名加 `workspace_id`；search 加 WHERE；4 个测试用例覆盖；commit
- [ ] **T6.4 check_access 升级为 3 参数**：`check_access(thread_id, user_id, workspace_id, *, require_existing)` 跨 workspace 一律 False；写 `test_check_access_cross_workspace_false`；commit
- [ ] **T6.5 RunRepository / FeedbackRepository / RunEventRepository 同款改造**：每个仓储重复 T6.1-T6.3 模式；commit per repo
- [ ] **T6.6 @require_permission 装饰器升级**：写 `test_require_permission_workspace.py::test_cross_workspace_returns_404`；改 `authz.py` 取 `request.state.workspace.id` 传给 `check_access(thread_id, user_id, workspace_id)`；跨 workspace 抛 HTTPException(404) 而非 403；commit
- [ ] **T6.7 routers/threads.py POST /api/threads 写 workspace_id**：thread_meta create 时显式 `workspace_id=AUTO`（让 ContextVar 注入）；commit
- [ ] **T6.8 跨 workspace 404 e2e 测试（红→绿）**：写 `test_workspace_isolation_boundary.py` 3 用例（GET / DELETE / RUN STREAM 跨 workspace 都 404）；用 TestClient + 两套 workspace fixture；commit
- [ ] **T6.9 Paths.thread_dir 加 workspace_id 参数**：`paths.py:thread_dir(thread_id, *, workspace_id=None, user_id=None)`；workspace_id 给走新路径 `{base}/workspaces/{wid}/threads/{tid}/...`；user_id 给走 legacy；写 `test_paths_workspace.py` 4 用例；commit
- [ ] **T6.10 Paths.user_memory_file / user_agents_dir 同款**：路径形态 `{base}/workspaces/{wid}/users/{uid}/memory.json` 等；commit
- [ ] **T6.11 ThreadDataMiddleware 切 workspace 维度**：`thread_data_middleware.py:before_agent` 用 `get_effective_workspace_id()`；写 `test_thread_data_middleware_workspace.py` 2 用例；commit
- [ ] **T6.12 文件系统迁移脚本**：新建 `scripts/migrate_paths_to_workspace.py` 仿 `scripts/migrate_user_isolation.py`，带 `--dry-run` `--user-id`；扫 `{base}/users/{uid}/threads/...` 搬到 `{base}/workspaces/{wid}/threads/...`（wid 从 DB `users.default_workspace_id` 查）；commit
- [ ] **T6.13 迁移脚本测试**：`test_migrate_paths_to_workspace.py` 用 tmpdir 模拟 legacy 目录树 → 跑迁移 → 验证新形态 + dry-run 不写；commit
- [ ] **T6.14 Makefile + lifespan 探测**：`Makefile` 加 `migrate-paths` target；`app.py` `_check_path_migration_pending(app)` 探测 `{base}/users/` 还有内容时 log warning；写 `test_migration_pending_warning`；commit
- [ ] **T6.15 验收烟雾**：起 dev 服务，注册两个 user → 创各自 thread → curl 互访 404；跑 `make migrate-paths --dry-run` 输出空（fresh DB）；commit final

---

## PR7 — CI boundary 静态扫描

### Scope

- 新建 `backend/tests/test_workspace_boundary.py`：AST 静态扫描 backend 代码，断言**只有** `app/gateway/routers/threads.py` 和 `app/gateway/routers/thread_runs.py` 才能直接 import LangGraph saver / checkpointer client
- 配套 allowlist 文件 `backend/tests/boundary_allowlist.toml`（让 lifespan、`make_checkpointer` 工厂等合法路径显式列在白名单）
- 复用现有 `test_harness_boundary.py` 模式（已经在做 harness 不能 import app 的扫描）

**不做**：运行时插桩（用 import-hook 拦截违规 import）—— 静态扫描足够 + 不引入运行时开销

### 关键文件

**新增**：
- `backend/tests/test_workspace_boundary.py`
- `backend/tests/boundary_allowlist.toml`

**修改**：
- `backend/tests/conftest.py` — register pytest mark 若需要

### 扫描算法草稿

```python
# test_workspace_boundary.py
import ast
import tomllib
from pathlib import Path
import pytest

ALLOWED_IMPORTERS = tomllib.loads(Path("tests/boundary_allowlist.toml").read_text())["langgraph_saver_importers"]
TARGET_MODULES = {"langgraph.checkpoint", "langgraph_checkpoint_postgres", "langgraph.checkpoint.postgres"}

def _scan_file(py: Path) -> set[str]:
    tree = ast.parse(py.read_text())
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = node.module if isinstance(node, ast.ImportFrom) else None
            if mod and any(mod.startswith(t) for t in TARGET_MODULES):
                imports.add(mod)
            for n in node.names:
                if any(n.name.startswith(t) for t in TARGET_MODULES):
                    imports.add(n.name)
    return imports

def test_only_allowed_importers_touch_langgraph_saver():
    violations = []
    for py in Path("backend").rglob("*.py"):
        if str(py).endswith(("_test.py", "/test_")) or "test_" in py.name:
            continue
        if str(py.relative_to(Path.cwd())) in ALLOWED_IMPORTERS:
            continue
        if _scan_file(py):
            violations.append(str(py))
    assert not violations, f"Unauthorized LangGraph saver importers: {violations}"
```

### 测试自检

`test_workspace_boundary.py` 自身就是测试。再加：
- `test_workspace_boundary_self.py` 验证扫描器本身（feed 一个临时 py 文件包含违规 import → 扫到）
- 验证 allowlist 文件解析

### LOCK

- allowlist 文件位置 `tests/boundary_allowlist.toml`
- 静态 AST 扫描 vs 运行时插桩

### 验收

- [ ] CI 跑 `pytest tests/test_workspace_boundary.py` 绿
- [ ] 故意在 lead_agent 里加一行 `from langgraph.checkpoint.postgres import AsyncPostgresSaver` 试 → 扫描红灯（人工验一次后还原）
- [ ] allowlist 当前列出 `app/gateway/routers/threads.py` / `thread_runs.py` / `runtime/checkpointer/async_provider.py` / lifespan

### Tasks

- [ ] **T7.1 allowlist toml**：新建 `backend/tests/boundary_allowlist.toml`，把当前合法 importers 列入：`backend/packages/harness/deerflow/runtime/checkpointer/async_provider.py` / `app/gateway/routers/threads.py` / `app/gateway/routers/thread_runs.py` / `app/gateway/app.py`（lifespan）；commit
- [ ] **T7.2 写 boundary 静态扫描（红→绿）**：新建 `backend/tests/test_workspace_boundary.py` 按 plan 草稿用 AST 扫；先跑应该 PASS（allowlist 准确时）；commit
- [ ] **T7.3 self-test 反向验证**：写 `test_workspace_boundary_self.py` feed 一个临时 py 文件含违规 import → 扫到；保证扫描器不是空跑；commit
- [ ] **T7.4 注入故意违规验证**：临时在 `app/gateway/routers/feedback.py` 加一行 `from langgraph.checkpoint.postgres import AsyncPostgresSaver` → `pytest tests/test_workspace_boundary.py` 应红灯；revert；commit（仅 commit 测试自身完善）
- [ ] **T7.5 docs**：在 `backend/CLAUDE.md` "Boundary check" 段加这条 boundary 说明；commit

---

## PR8 — service_accounts + api_keys + external_users schema only

### Scope

- 新建 3 张表 schema + ORM 模型（仿 PR3 模式）
- 不接路径、不写仓储仅暴露 ORM
- 为 Stage 1 headless API 准备底座

**不做**：API key 认证 / 路由 / `@require_permission` scope 升级 / Pattern A/B endpoint —— 全部 Stage 1

### 关键文件

**新增**（schema only，仓储到 Stage 1 才完整）：
- `backend/packages/harness/deerflow/persistence/service_account/{__init__.py, model.py}`
- `backend/packages/harness/deerflow/persistence/api_key/{__init__.py, model.py}`
- `backend/packages/harness/deerflow/persistence/external_user/{__init__.py, model.py}`
- `backend/tests/test_service_account_schema.py`
- `backend/tests/test_api_key_schema.py`
- `backend/tests/test_external_user_schema.py`

### Schema 草稿（来自 [workspace-schema-design §2.3-§2.5](/Users/wangguixuan/work/github/deer-flow/docs/multi-tenant-redesign/01-redesign/workspace-schema-design.zh-CN.md)）

`service_account/model.py`:
```python
class ServiceAccountRow(Base):
    __tablename__ = "service_accounts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")
    identity_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="collapsed")  # collapsed / external_passthrough / both
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]

    __table_args__ = (Index("idx_service_accounts_workspace", "workspace_id", "status"),)
```

`api_key/model.py`:
```python
class ApiKeyRow(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    service_account_id: Mapped[str] = mapped_column(String(36), ForeignKey("service_accounts.id", ondelete="CASCADE"), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)  # 'dfk_live_abc12345'
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False)  # sha256 hex
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    scopes: Mapped[str] = mapped_column(String(1024), nullable=False, default="")  # 逗号分隔（保 String 兼容 SQLite dev 兜底）
    rate_limit_rpm: Mapped[int | None]
    expires_at: Mapped[datetime | None]
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
    created_at: Mapped[datetime]

    __table_args__ = (
        Index("idx_api_keys_sa", "service_account_id"),
        Index("idx_api_keys_active", "key_prefix", sqlite_where=text("revoked_at IS NULL"), postgresql_where=text("revoked_at IS NULL")),
    )
```

`external_user/model.py`:
```python
class ExternalUserRow(Base):
    __tablename__ = "external_users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    service_account_id: Mapped[str] = mapped_column(String(36), ForeignKey("service_accounts.id", ondelete="CASCADE"), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(128))
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime]
    last_seen_at: Mapped[datetime | None]

    __table_args__ = (UniqueConstraint("service_account_id", "external_id", name="uq_external_users_sa_external"),)
```

### 测试

`test_service_account_schema.py`（≥3 用例）：
1. INSERT smoke
2. CASCADE：删 workspace 后 service_accounts 消失
3. RESTRICT：删 created_by user 时阻拦（除非先转移 SA）

`test_api_key_schema.py`（≥3 用例）：
1. UNIQUE key_prefix 约束
2. partial unique active key（双驱动）
3. CASCADE：删 service_account → api_keys 消失

`test_external_user_schema.py`（≥2 用例）：
1. UNIQUE (service_account_id, external_id)
2. CASCADE：删 service_account → external_users 消失

### LOCK

- 表名 `service_accounts` / `api_keys` / `external_users`
- key_prefix 长度 16
- scopes 用 String(1024) 不用 PG `text[]`（双驱动兼容；Stage 2 切纯 PG 后可平滑迁）
- identity_mode 三态字面值：`collapsed` / `external_passthrough` / `both`
- API Key 格式 `dfk_live_*` / `dfk_test_*`（Stage 1 才生效，但 schema 已锁）

### 验收

- [ ] 8+ 新单测过 + 既有 277 + PR3-7 新测全过
- [ ] 3 张表 `Base.metadata.create_all()` 自动建（无 alembic）

### Tasks

> 仿 PR3 的纯 schema 模式；3 张表各自独立可测。可与 PR4-PR7 并行。

- [ ] **T8.1 ServiceAccountRow ORM + 测试（红→绿）**：`persistence/service_account/{__init__.py, model.py}` 按 plan 草稿建表；写 `test_service_account_schema.py::test_insert_smoke`；commit
- [ ] **T8.2 ServiceAccount CASCADE 测试**：`test_service_account_cascade_on_workspace_delete`；commit
- [ ] **T8.3 ServiceAccount RESTRICT 测试**：`test_service_account_restrict_on_user_delete` 验证删 created_by user 时阻拦；commit
- [ ] **T8.4 ApiKeyRow ORM + 测试**：`persistence/api_key/{__init__.py, model.py}`；UNIQUE key_prefix；partial unique active key（双 sqlite_where + postgresql_where）；写 `test_api_key_schema.py` 3 用例；commit
- [ ] **T8.5 ExternalUserRow ORM + 测试**：`persistence/external_user/{__init__.py, model.py}`；UNIQUE (service_account_id, external_id)；写 `test_external_user_schema.py` 2 用例；commit
- [ ] **T8.6 ORM 注册 + 验收**：让 `Base.metadata.create_all()` 自动建 3 张表；fresh PG 起来后 `\dt` 看 3 张表存在；commit

---

## Stage 0 退出 Go/No-Go（来自 phased-rollout-by-scale）

工程层面：
- [x] PR1-PR8 全部合入 ✅（见 STATUS.md 8 PR 状态表，全 merged 进分支）
- [x] 既有 277 + 新增 ~70 测试全 100% 通过 ✅（实际 3250 passed + 31 skipped，新增 ~163）
- [x] CI（含 backend-postgres-tests）绿 ✅ 2026-05-12 用户确认
- [x] 手工 smoke：注册新用户 → workspace 自动建 → JWT 含 wid → 创建 thread → 跨 workspace 互调 404 ✅ 2026-06-27 `apps/examples/http-chat/multi_tenant.py` PASS（N 租户真并发 + 多轮链式上下文 + 双向隔离 search/404；注：JWT wid claim 未显式解码断言，由隔离端到端间接覆盖）
- [ ] 部署到生产 ≥ 2 周，无 workspace 隔离 bug 报告
- [ ] 生产稳定运行在 Postgres 上 ≥ 2 周，无 schema/性能 regression
- [x] 7 项不可逆 LOCK 决策经团队 review 拍板 ✅ 2026-05-12 全 7 项 sign-off

业务层面：
- [ ] 第一个付费意向客户出现（业务条件，非工程）

---

## 测试规模预估

| PR | 新增测试数 | 备注 |
|---|---|---|
| PR1 | 4 | PG smoke + fixture self-test |
| PR2 | 3 | 默认 backend 切换 + sqlite regression |
| PR3 | 13 | workspace + membership + workspace_context |
| PR4 | 13+ | JWT/middleware/register/initialize/me/legacy token |
| PR5 | 9 | alembic + backfill |
| PR6 | 17+ | repos workspace_id 过滤 + paths + middleware + cross-workspace 404 e2e + 装饰器 |
| PR7 | 2 | boundary 静态扫描 + self-test |
| PR8 | 8 | 3 张表 schema 自检 + cascade |
| **总计** | **~70** | CI 时间增加预计 30-60s（per-test schema 优化） |

既有 277 测试不破坏。

---

## 关键开放问题（执行 session 第一件事处理）

1. **远程 RDS 大版本验证**：连上 RDS 跑 `SELECT version()`，对齐 testcontainers `postgres:16-alpine` 镜像；若实际是 PG 14/15/17 → 调镜像
2. **是否需要 alembic baseline revision**：第一个 alembic revision (PR4 0001) 之前没有任何 versions——确认 alembic 默认行为是把"当前 schema 作为 baseline"还是"必须有显式 baseline"。如需 baseline 加 0000_baseline.py
3. **`_ensure_admin_user(app)` 的"孤立 thread 迁移"现状**：本计划 PR4/PR5 假设它能扩；执行 session 第一步要确认它现在做什么、能否扩

## 文件清单速查

| PR | 关键路径 |
|---|---|
| PR1 | `backend/tests/fixtures/postgres.py`, `docker-compose-dev.yaml`, `pyproject.toml` |
| PR2 | `config.example.yaml`, `.env.example`, `Makefile`, `scripts/serve.sh` |
| PR3 | `persistence/workspace/*`, `persistence/workspace_membership/*`, `runtime/workspace_context.py` |
| PR4 | `migrations/versions/0001_*.py`, `auth/jwt.py`, `auth_middleware.py`, `routers/auth.py`, `user/model.py` |
| PR5 | `migrations/versions/{0002,0003}_*.py`, `scripts/backfill_workspace_id.py`, 4 个 model.py |
| PR6 | `scripts/migrate_paths_to_workspace.py`, 4 个仓储 sql.py, `paths.py`, `authz.py`, `threads.py`, `thread_runs.py`, `thread_data_middleware.py` |
| PR7 | `tests/test_workspace_boundary.py`, `tests/boundary_allowlist.toml` |
| PR8 | `persistence/service_account/*`, `persistence/api_key/*`, `persistence/external_user/*` |

---

## Self-Review（writing-plans 要求）

**1. Spec coverage** — 8 个 PR 全覆盖 [phased-rollout Stage 0 必做项](/Users/wangguixuan/work/github/deer-flow/docs/multi-tenant-redesign/02-rollout/phased-rollout-by-scale.zh-CN.md#必做)：

| Stage 0 必做项 | 落到 |
|---|---|
| Postgres 切换 | PR1 + PR2 |
| Postgres testcontainers | PR1（fixture） |
| `workspaces` 表 + 自动建 1 人 workspace | PR3（表）+ PR4（注册流自动建） |
| `workspace_id` 列加到现有表 | PR5（ALTER + 回填）|
| `workspace_memberships` 表 | PR3 |
| `service_accounts` / `api_keys` / `external_users` schema | PR8 |
| JWT 扩 `wid` 字段 | PR4 |
| 入口路由 `(workspace_id, thread_id)` 校验 | PR6 |
| CLI / admin UI 的"workspace 管理"基础 | **gap：未排入 8 PR**（platform admin 看 workspace 列表 / 暂停 / 删除）|
| 现有 auth 完善 | PR4 |
| CI boundary 测试 | PR7 |

**Gap 修复**：本计划**未含** "platform admin 管理 workspace 的 CLI / UI"。建议作为 PR9（可选）追加，或推迟到 Stage 1（业务上 Stage 0 期间 platform admin 的"暂停滥用 workspace"操作可手工 SQL 处理；正式 admin UI 在 Stage 1 + 多档付费时一起做更划算）。

**2. Placeholder scan** — 全文 grep "TBD" / "TODO" / "implement later" / "fill in details" 无命中。所有 "Stage 1+" 引用都是显式 cross-reference 不是占位符。✓

**3. Type consistency** — 全文统一：
- `WorkspaceRow` / `WorkspaceMembershipRow` / `WorkspaceRepository` ✓
- `workspace_id` (DB) / `wid` (JWT) ✓
- ContextVar `_current_workspace`, helpers `set_current_workspace` / `resolve_workspace_id` / `get_effective_workspace_id` / `require_current_workspace` ✓
- `check_access(thread_id, user_id, workspace_id, *, require_existing)` 参数顺序贯穿 PR3 / PR6 ✓
- 路径 `{base}/workspaces/{wid}/threads/{tid}/...` 贯穿 PR6 / Paths.py / 迁移脚本 ✓
- legacy_workspace UUID `00000000-0000-0000-0000-000000000000` 在 PR5 横切要点中定义，PR5 backfill / lifespan ensure 引用一致 ✓

**4. Skill 说明 vs 本 plan 的粒度选择** — writing-plans skill 标准是"每 step 2-5 分钟 + 完整 code block"。本 plan 选择**"stage-level master plan + commit-sized tasks"**：
- 每个 `T*.*` task 是一次 commit（30-120 分钟），不是 2-5 分钟 step
- 完整代码草稿在 PR 顶层"关键代码草稿"小节，避免 task list 重复肿胀
- 执行时**每个 PR 单独调 `superpowers:executing-plans` 或 `subagent-driven-development`** 把 commit-sized task 进一步展成 2-5 分钟 step

这是 trade-off：用户明确要"Stage 0 全量 TaskList"——一份串起 8 个 PR 的总览，而非 8 份独立全展开 plan。如果 reviewer 觉得需要更细，**可以让我现在把 PR1（最阻塞的那个）单独 expand 成 2-5 分钟 step + 完整 code block 形态另存一份**。

---

## Execution Handoff（writing-plans 要求）

Plan 完成后请把 `~/.claude/plans/soft-whistling-sundae.md` 复制到 `docs/superpowers/plans/2026-05-10-stage-0-multi-tenant-foundation.md` 入项目 git。

执行时按 PR 一个一个走，每个 PR 单独选执行模式：

**1. Subagent-Driven（推荐 PR3-PR8）** — 每 task 起 fresh subagent，主 agent 在 task 间 review。适合纯 schema / 仓储类 PR（PR3 / PR8）和重复模式多的 PR（PR5 alembic / PR6 仓储）。
- 触发 skill：`superpowers:subagent-driven-development`

**2. Inline Execution（推荐 PR1 / PR2 / PR4）** — 在当前 session 直接执行，每完成几条 task 一个 checkpoint。适合需要主 agent 持续上下文的 PR（PR1 docker/CI 调通要看错误信息、PR4 auth 改造跨多个 router 文件耦合紧）。
- 触发 skill：`superpowers:executing-plans`

每个 PR 之间建议有 human review checkpoint，**绝不要 8 个 PR 一气连跑**——多租户改造的不可逆决策密集，每个 PR 都该让人看一眼。
