# ADR-001 · 数据隔离模型

| 项目 | 内容 |
|---|---|
| 状态 | 草稿（Draft） |
| 决策日期 | TBD |
| 决策者 | CTO + 架构 + 后端 lead |
| 关联 ADR | ADR-004 租户层级、ADR-005 存储拓扑、ADR-006 运行时与渠道 |

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

#### 4.1.1 LangGraph 自有表（checkpoints / checkpoint_writes / checkpoint_blobs）

`runtime/checkpointer/async_provider.py` 用的是 LangGraph 内置 `AsyncPostgresSaver`，**表结构不在 DeerFlow 控制下**——直接 ALTER 添加 `tenant_id` 列在 LangGraph 升级时会被它自己的 migration 覆盖。处理路径分两档（详见 ADR-006 §2.1）：

**默认（subquery 形式 RLS，零侵入）**：

```sql
-- LangGraph 自动建表后，DeerFlow 的 init migration 跑：
ALTER TABLE checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE checkpoints FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON checkpoints
    USING (
      thread_id IN (
        SELECT thread_id::text FROM threads_meta
        WHERE tenant_id = current_setting('app.tenant_id', true)::uuid
      )
    );
-- checkpoint_writes / checkpoint_blobs 同形态，按 thread_id 反查
```

**升级路径（侵入式列）**：当 subquery RLS 在大表上 EXPLAIN 出现问题时，改用：

```sql
ALTER TABLE checkpoints ADD COLUMN tenant_id UUID;
CREATE INDEX idx_checkpoints_tenant_thread ON checkpoints (tenant_id, thread_id);
-- trigger 在 INSERT 时从 threads_meta 反查 tenant_id 写入
-- 升级 LangGraph 前必须验证它的 migration 不会丢这列
```

#### 4.1.2 第一道防线：thread_id ↔ tenant_id 校验

不论用哪种 RLS 形态，`AssistantsCompat` 路由（`app/gateway/routers/assistants_compat.py`）在调用 LangGraph 之前**必须先用 `threads_meta` 校验 `(tenant_id, thread_id)` 归属**。RLS 是兜底，应用层校验是第一道防线——RLS 只能"过滤掉看不到的"，不能阻止"创建到错误租户名下"。

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

**LangGraph 自有连接池的注入**：DeerFlow 仓储和 LangGraph checkpointer 是**两套连接池**——前者是 SQLAlchemy `AsyncSession`，后者是 LangGraph 自己持有的 asyncpg 池。后者的注入路径不能复用上面的 helper：

```python
# 1. 优选：自定义 connection factory 注入到 saver 构造（langgraph-checkpoint-postgres>=2.0）
async def tenant_aware_acquire(pool):
    async with pool.acquire() as conn:
        tid = get_current_tenant_id()
        await conn.execute("SET LOCAL app.tenant_id = $1", tid)
        yield conn

saver = AsyncPostgresSaver(connection_factory=tenant_aware_acquire)

# 2. 兜底：在 RunManager 进入 thread 前主动 issue 一条 SET（要求 LangGraph 复用同 conn）
```

具体路径选择与失败模式见 ADR-006 §2.1。

**关键约束**：所有"会查 LangGraph 表"的代码路径——`AssistantsCompat` 路由、`RunManager`、checkpointer 直读——都必须保证调用栈上已注入 `app.tenant_id`，否则 RLS 会把整个会话过滤成空集。CI 加冒烟测试确认这点。

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
| 应用层漏写 tenant_id WHERE | RLS 是兜底；CI 加静态检查（detect SQL 不带 tenant_id） |
| 索引前导列错了走全表扫 | DBA 评审所有 EXPLAIN；上线前压测 |
| `current_setting('app.tenant_id')` 没设导致 RLS 全过滤掉 | 应用层 fail-closed；监控空集查询率 |
| 跨租户分析需求多 | 提供受控的 admin role + 审计日志 |
| SQLite 开发 vs Postgres 生产差异 | 测试集成层用 testcontainers 跑 Postgres；不允许用 SQLite 跑 RLS 相关测试 |
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
