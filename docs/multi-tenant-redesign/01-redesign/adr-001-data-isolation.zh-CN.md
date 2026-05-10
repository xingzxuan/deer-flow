# ADR-001 · 数据隔离模型

| 项目 | 内容 |
|---|---|
| 状态 | 草稿（Draft） · 2026-05-09 据 spike 结果修订 §4.1.1 / §4.1.2 / §4.2 |
| 决策日期 | TBD |
| 决策者 | CTO + 架构 + 后端 lead |
| 关联 ADR | ADR-004 租户层级、ADR-005 存储拓扑、ADR-006 运行时与渠道 |
| 关联 spike / 审计 | [adr-vs-code-audit](./adr-vs-code-audit.zh-CN.md) · [langgraph-postgres spike](./adr-spike-langgraph-postgres.zh-CN.md) |
| 代码命名 | 本 ADR 写 `tenant_id`，落代码统一读作 `workspace_id`（详 [workspace-schema-design §1](./workspace-schema-design.zh-CN.md#1-命名约定--workspace-vs-tenant)） |

---

## 1. 背景

DeerFlow 当前是 **"多用户单租户"** 模型：所有用户的会话、运行、记忆、产物都共用同一套表，仓储层用 `user_id` WHERE 过滤做个人空间隔离（`runtime/user_context.py:138-167`）。这套机制设计良好——repository 用 ContextVar + `AUTO` 哨兵自动注入当前用户，下层不依赖上层（harness 不能 import app）。

多租户化需要在 `user_id` 之上再加一层 `tenant_id`。问题是：**用什么物理隔离强度？**

三种主流方案：

| 维度 | 行级（tenant_id WHERE） | per-tenant schema | per-tenant DB |
|---|---|---|---|
| 实现成本 | 低 | 中 | 高 |
| 跨租户 bug 爆炸半径 | 高 | 中 | 极低 |
| 备份/恢复粒度 | 全量 | 按 schema | 按 DB |
| 合规友好度（SOC2/HIPAA） | 一般 | 好 | 最好 |
| 跨租户分析查询 | 容易 | 中 | 难 |
| 升级 schema | 一次完成 | 要遍历所有 schema | 要遍历所有 DB |
| 适用客户规模 | <10k 租户 | 10k–100 大客户 | <100 大客户 |
| 运维复杂度 | 低 | 中 | 高 |

---

## 2. 决策

**采用 行级 `tenant_id` + Postgres Row-Level Security（RLS）** 作为双保险。

理由：

1. **DeerFlow 仓储层现状几乎平行扩展**——已经有 `resolve_user_id()` 哨兵模式，把 `tenant_id` 按同样模式补一遍，改造面集中、风险可控。
2. **Postgres RLS 是 DB 层兜底**——即使应用层有 bug 漏写 `WHERE tenant_id = ...`，DB 也会强制过滤，第二道防线。
3. **覆盖目标客户规模**：B2B 中小客户为主、租户数 1k–10k，行级方案足够。
4. **不放弃跨租户分析能力**：平台需要做用量统计、监控、健康检查，单库行级最方便。

---

## 3. 备选方案与拒绝理由

### A. per-tenant schema（同库不同 schema）

**拒绝。** 看似比行级更隔离，实际坑很多：

- **schema 数量爆炸**：1000 个租户 = 1000 个 schema × 每张表，pg_class 体积膨胀，连接池里 search_path 切换有性能抖动
- **schema migration 痛苦**：每发布一次 schema 改动要遍历所有 schema 跑 migration，失败回滚极复杂
- **跨租户查询难**：要写 `UNION ALL` 跨所有 schema，运营仪表盘几乎无法实现
- **依然需要应用层过滤**：连接进哪个 schema 仍由应用层决定，没真正消除"应用层 bug 跨租户"

### B. per-tenant database（独立物理库）

**拒绝（默认场景）。** 隔离最强但成本极高：

- **运维负担**：1000 个 DB = 1000 套备份/恢复/监控/连接池
- **冷启动延迟**：每个租户新建 DB 时间从秒级飙到分钟级
- **跨租户操作不可能**：平台级查询、聚合、迁移全部失效
- **连接池复杂度爆炸**：每租户独立连接池或者共用动态切库，都是噩梦

**仅在两种情况切换到此方案：** ① 拿到强合规客户（金融/医疗/政府），合同里写明物理数据隔离；② 客户付费足够覆盖每租户独立 DB 的运维成本（典型企业级订阅）。

---

## 4. 落地影响

### 4.1 表结构改造

所有业务表加 `tenant_id` 列 + 复合索引（`tenant_id` 作为前导列）：

```sql
ALTER TABLE threads_meta ADD COLUMN tenant_id UUID NOT NULL;
CREATE INDEX idx_threads_meta_tenant_user ON threads_meta (tenant_id, user_id, updated_at DESC);

ALTER TABLE runs ADD COLUMN tenant_id UUID NOT NULL;
CREATE INDEX idx_runs_tenant_created ON runs (tenant_id, created_at DESC);

ALTER TABLE run_events ADD COLUMN tenant_id UUID NOT NULL;
CREATE INDEX idx_run_events_tenant_run ON run_events (tenant_id, run_id, seq);

ALTER TABLE feedback ADD COLUMN tenant_id UUID NOT NULL;
CREATE INDEX idx_feedback_tenant_run ON feedback (tenant_id, run_id);

-- ADR-005 引入的新表也要带 tenant_id（建表时就有）
-- agent_configs, memory_facts, memory_context,
-- tenant_skill_state, tenant_mcp_configs, tenant_secrets, tenant_quotas
```

**关键索引原则**：每个 `tenant_id` 都必须是复合索引的**第一列**——RLS policy 走的就是这条路径，前导列错了 RLS 会全表扫。

#### 4.1.1 LangGraph 自有表（checkpoints / checkpoint_writes / checkpoint_blobs / checkpoint_migrations）

`runtime/checkpointer/async_provider.py` 用的是 LangGraph 内置 `AsyncPostgresSaver`，**表结构不在 DeerFlow 控制下**。原稿讨论过两条路（subquery RLS / 列升级），spike（[adr-spike-langgraph-postgres](./adr-spike-langgraph-postgres.zh-CN.md)）验证后我们改用 **两层隔离模型**：

| 表归属 | 隔离机制 | 防线性质 |
|---|---|---|
| **DeerFlow 自有表**（threads_meta、runs、run_events、feedback、users、tenant_*） | RLS + `SET LOCAL app.tenant_id` via SQLAlchemy session（DeerFlow 完全控制 conn pool） | DB 强约束 |
| **LangGraph checkpoint 表** | **应用层强校验**——入口路由在调 LangGraph 前必查 `threads_meta` 上的 `(tenant_id, thread_id)` 归属 | 应用层强约束 + 表 unique constraint 兜底 |

**为何对 LangGraph 表放弃 RLS**：

- `langgraph-checkpoint-postgres==3.0.5` **不存在 `connection_factory` 参数**（spike §2 实测）；其连接池注入路径只有 `__init__(conn=AsyncConnectionPool)` 这一个口子，且 `psycopg_pool` 自带的 `configure` callback 只在物理连接首次创建时跑——拿不到运行期 ContextVar 里的 tenant_id
- 子类化 `AsyncConnectionPool` 重写 `getconn` 注入 `SET app.tenant_id`/`RESET` 是可行的 hack，但侵入 psycopg-pool 内部，库升级风险高（spike §3.1）
- 给 LangGraph 表 ALTER 加 `tenant_id` 列同样不可取——LangGraph 用 `MIGRATIONS` 数组管理 schema，每次升级都要 diff 防漏（spike §3.4）

**LangGraph 表的安全模型**（接受的 trade-off）：

- 安全等级从"DB 强约束"降级为"应用层强约束 + 表 unique constraint"
- 强约束点是 **`threads_meta` 表上的 `UNIQUE (tenant_id, thread_id)` 复合索引** + 入口路由的强校验：任何代码路径要写 LangGraph 表前必须先在 `threads_meta` 找到对应行，且行的 `tenant_id` 与当前 ContextVar 一致
- CI 加 boundary 测试，禁止任何路径绕过 `threads.py` / `thread_runs.py` 直连 LangGraph saver（包括 LangGraph Studio 必须走相同入口或显式审批）
- 平台 admin 路径走 `BYPASSRLS` role 时同样必须经过应用层 audit，不直接跳过 thread 归属检查

**未来可升级路径**（不阻塞 phase-0）：若上游接受 PR 加入 `connection_factory`，可平滑切回"DeerFlow 表 + LangGraph 表统一 RLS"模型。

#### 4.1.2 第一道防线：thread_id ↔ tenant_id 校验

LangGraph 调用入口在 **`app/gateway/routers/threads.py`**（thread CRUD）和 **`app/gateway/routers/thread_runs.py`**（run 创建/恢复/事件流）。这两个路由在调用 LangGraph 之前**必须先用 `threads_meta` 校验 `(tenant_id, thread_id)` 归属**：

- **创建路径**：先在 `threads_meta` 写入 `(tenant_id=current, thread_id, user_id=current)`，依赖 `UNIQUE (tenant_id, thread_id)` 防重；再调 LangGraph 创建对应 thread
- **读/写路径**：先用 `(current_tenant_id, requested_thread_id)` SELECT `threads_meta`，未命中即 404；命中后才允许调 LangGraph

> 注：原稿写"在 `AssistantsCompat` 路由强制校验"是错的——`assistants_compat.py:1-50` 只服务 `assistants.search/get` 静态 stub，**不**触达 thread 入口（审计报告 §ADR-001 已修正）。

应用层校验是第一道防线、`UNIQUE` 约束是 DB 层兜底——任何对 LangGraph 表的访问都经过这一关。

### 4.2 RLS policy 模板

所有带 `tenant_id` 的表都加同样形态的 policy：

```sql
ALTER TABLE threads_meta ENABLE ROW LEVEL SECURITY;
ALTER TABLE threads_meta FORCE ROW LEVEL SECURITY;  -- 即使表所有者也走 policy

CREATE POLICY tenant_isolation ON threads_meta
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
```

应用层在每次拿到连接时，先 `SET LOCAL app.tenant_id = '<uuid>'`：

```python
# packages/harness/deerflow/persistence/engine.py
async def _set_session_tenant(session: AsyncSession, tenant_id: str) -> None:
    """Bind session to tenant; RLS policy enforces filter."""
    await session.execute(text("SET LOCAL app.tenant_id = :tid"), {"tid": tenant_id})
```

每个仓储方法的开头自动调用，从 ContextVar 取 tenant_id（仿照现有 `resolve_user_id` 模式）。

**LangGraph 自有连接池不参与 SET LOCAL**：DeerFlow 仓储和 LangGraph checkpointer 是**两套连接池**——前者是 SQLAlchemy `AsyncSession`（DeerFlow 控制），后者是 LangGraph 自己持有的 psycopg 池（DeerFlow 不可控）。按 §4.1.1 的两层模型：

- **DeerFlow 自有表**：上面 `_set_session_tenant` helper 在每次仓储调用前注入 `SET LOCAL`，RLS 兜底
- **LangGraph 表**：**不注入 `SET LOCAL`**——LangGraph 表上不启用 RLS，租户隔离靠应用层强校验（§4.1.2）实现。`AsyncPostgresSaver` 仍按现状用 `from_conn_string`，无侵入

> 原稿设想的"自定义 `connection_factory` 注入到 saver 构造"在 `langgraph-checkpoint-postgres==3.0.5` 不可行——库不存在该参数（[spike](./adr-spike-langgraph-postgres.zh-CN.md) §2.2/2.4）。详细备选方案与拒绝理由见 spike §3。

**关键约束**：所有"会查 DeerFlow 自有表"的代码路径都必须保证调用栈上已注入 `app.tenant_id`，否则 RLS 会把整个会话过滤成空集。CI 加冒烟测试确认这点。LangGraph 表上的访问则必须经过 §4.1.2 的入口校验。

### 4.3 ContextVar 扩展

在 `runtime/user_context.py` 旁边加 `tenant_context.py`：

```python
_current_tenant: Final[ContextVar[CurrentTenant | None]] = ContextVar("deerflow_current_tenant", default=None)

class _AutoSentinel: ...   # 同 user_id 模式
AUTO: Final[_AutoSentinel] = _AutoSentinel()

def resolve_tenant_id(value, *, method_name) -> str:
    """与 resolve_user_id 同款三态：AUTO / 显式 str / 显式 None。
    SaaS 模式下 None 是 forbidden（除非显式 admin override）。"""
```

`AuthMiddleware` 在解析完 JWT 后两个 ContextVar 同时注入。

### 4.4 SQLite → Postgres 迁移

**SQLite 不支持 RLS**，多租户上线必须切 Postgres。迁移路径：

1. 第 0 阶段：在 dev 环境同时跑 SQLite 和 Postgres，用 `aiosqlite` / `asyncpg` 双驱动
2. 第 1 阶段：生产切 Postgres，老数据用 `pg_loader` 导入；DeerFlow 现有的 `Base.metadata.create_all()` 直接接 Postgres
3. 老用户的 `user_id` 在没有 tenant_id 时归到一个"legacy_tenant"，迁移脚本同步把 `tenant_id` 回填

### 4.5 跨租户操作（平台后台）

平台 admin / 运维需要跨租户查询时，不能简单"绕过 RLS"——而是用一个**专用 role** 配 `BYPASSRLS`，只给受限运维账号使用，操作审计入库：

```sql
CREATE ROLE deerflow_admin BYPASSRLS;
-- 应用层 admin 路由用这个 role 的连接池，并强制审计日志
```

**绝不允许应用主连接池有 `BYPASSRLS`。**

---

## 5. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 应用层漏写 tenant_id WHERE | RLS 是兜底（DeerFlow 表）；CI 加静态检查（detect SQL 不带 tenant_id） |
| **LangGraph 表无 RLS，仅应用层强约束**（§4.1.1 trade-off） | `threads_meta` `UNIQUE (tenant_id, thread_id)` 兜底；CI boundary 测试禁止绕过 `threads.py` / `thread_runs.py` 直连 saver；定期审计任何新增的 LangGraph 直连路径 |
| 索引前导列错了走全表扫 | DBA 评审所有 EXPLAIN；上线前压测 |
| `current_setting('app.tenant_id')` 没设导致 RLS 全过滤掉 | 应用层 fail-closed；监控空集查询率 |
| 跨租户分析需求多 | 提供受控的 admin role + 审计日志 |
| SQLite 开发 vs Postgres 生产差异 | 测试集成层用 testcontainers 跑 Postgres；不允许用 SQLite 跑 RLS 相关测试 |
| **当前不存在 Postgres 测试夹具基础设施**（审计报告 §ADR-001 highest-risk gap） | phase-0 必须先落 testcontainers + RLS 冒烟测试，再做仓储改造；否则 RLS bug 进生产 |
| 单 DB 容量上限（>1TB 后维护困难） | 监控 DB 体积；超过阈值切 per-tenant DB（推翻方案） |

---

## 6. 推翻条件

切换到 **per-tenant DB** 当且仅当：

1. 拿到强合规客户（金融/医疗/政府），合同要求物理数据隔离
2. 单 DB 容量 / 写 TPS 触顶，垂直扩展不经济
3. 出现一次跨租户数据泄露事故，董事会要求最强隔离

---

## 7. 默认假设

| 项 | 默认 |
|---|---|
| 数据库 | PostgreSQL 16+ |
| RLS 启用 | 所有带 `tenant_id` 的业务表 |
| 主键 | UUID v7（时间排序） |
| 索引前导列 | `tenant_id` |
| Connection pool | 每应用进程 20–50 conns，PgBouncer transaction mode |
| 备份 | 每日全量 + WAL streaming，保留 30 天 |
| 跨租户查询 | 仅通过 `deerflow_admin` role + 审计 |
