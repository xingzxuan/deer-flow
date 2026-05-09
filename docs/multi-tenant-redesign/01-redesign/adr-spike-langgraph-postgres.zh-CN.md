# Spike: langgraph-checkpoint-postgres 的 per-acquire 注入能力验证

> 验证日期：2026-05-09
> 触发原因：ADR-001 §4 / ADR-006 §2.1 假设 `langgraph-checkpoint-postgres>=2.0` 提供 `connection_factory`，可在每次连接 acquire 时注入 `SET LOCAL app.tenant_id`。本 spike 验证此假设是否成立，并给出可落地的备选路径。
> 影响范围：ADR-001（数据隔离）、ADR-006（运行时与渠道层）

---

## 1. 验证基线

- **库版本**：`langgraph-checkpoint-postgres==3.0.5`（PyPI 上传时间 2026-03-18）
- **当前调用点**：`backend/packages/harness/deerflow/runtime/checkpointer/async_provider.py:73,117`
  ```python
  async with AsyncPostgresSaver.from_conn_string(conn_string) as saver:
      await saver.setup()
      yield saver
  ```
- **依赖锁定**：`backend/uv.lock` `>=3.0.5`（postgres extra）

## 2. 库实际 API 形态

直接解包 v3.0.5 wheel 检查源码：

### 2.1 `AsyncPostgresSaver.__init__`

```python
# /tmp/lgcp/langgraph/checkpoint/postgres/aio.py:37-53
def __init__(
    self,
    conn: _ainternal.Conn,
    pipe: AsyncPipeline | None = None,
    serde: SerializerProtocol | None = None,
) -> None: ...
```

`Conn` 类型定义（`_ainternal.py:10`）：

```python
Conn = AsyncConnection[DictRow] | AsyncConnectionPool[AsyncConnection[DictRow]]
```

→ 可以传一个**自建的 `AsyncConnectionPool`** 进去；这是唯一一个有运行期介入空间的口子。

### 2.2 `from_conn_string` classmethod

```python
# aio.py:55-80
@classmethod
@asynccontextmanager
async def from_conn_string(
    cls,
    conn_string: str,
    *,
    pipeline: bool = False,
    serde: SerializerProtocol | None = None,
) -> AsyncIterator[AsyncPostgresSaver]: ...
```

→ **没有 `connection_factory` / `configure` / 任何 callback 参数**。这条路径完全封死。

### 2.3 `_cursor` 实现

```python
# aio.py:352-392
@asynccontextmanager
async def _cursor(self, *, pipeline: bool = False):
    async with self.lock, _ainternal.get_connection(self.conn) as conn:
        ...
```

`get_connection` 行为（`_ainternal.py:13-23`）：
- `self.conn` 是单 `AsyncConnection`：始终复用同一条
- `self.conn` 是 `AsyncConnectionPool`：每次现取（`async with conn.connection() as conn`）

→ 如果传 pool，**每次 cursor 调用确实独立 acquire**。

### 2.4 全局搜索

```bash
grep -rn -E "connection_factory|configure_connection|on_acquire" /tmp/lgcp
# 命中 0 处
```

→ **库不存在 `connection_factory`**。ADR 写"langgraph-checkpoint-postgres>=2.0 支持 connection_factory"是事实错误。

## 3. 备选注入路径分析

### 3.1 Option A：自建 `AsyncConnectionPool` + 自定义 `getconn`

```python
class TenantAwarePool(AsyncConnectionPool):
    @asynccontextmanager
    async def connection(self):
        async with super().connection() as conn:
            tenant_id = get_current_tenant()  # ContextVar
            await conn.execute(f"SET app.tenant_id = '{tenant_id}'")
            try:
                yield conn
            finally:
                await conn.execute("RESET app.tenant_id")

pool = TenantAwarePool(conn_string, ...)
saver = AsyncPostgresSaver(conn=pool)
```

**可行性**：可以做。  
**问题**：
- 侵入 `psycopg_pool` 内部，psycopg-pool 升级时要 retest
- `psycopg_pool.AsyncConnectionPool.configure` callback 只在**物理连接首次创建**时跑，不能用——拿不到运行期 ContextVar
- `SET app.tenant_id`（不带 LOCAL）会在 conn 复用时残留 → 必须在 release 前 `RESET`，否则跨租户泄漏
- 每次 acquire 多 2 次 round-trip（SET + RESET），延迟成本不可忽略

### 3.2 Option B：包一层 `TenantAwarePostgresSaver`

在 `aput`/`aget`/`alist` 外面起事务 + `SET LOCAL`。

**问题**：
- `AsyncPostgresSaver` 内部已有 `self.lock` + 自己的 cursor 管理（`_cursor` 走 `async with self.lock`），外面套事务和内部锁/事务结构会冲突
- `SET LOCAL` 仅在事务内有效，需要确保 langgraph 内部所有 SQL 都跑在同一个事务里——库当前实现并不全是这样（pipeline 模式下行为不一）
- 维护成本高，跟随 langgraph 升级风险大

### 3.3 Option C：放弃 langgraph 表上的 RLS（推荐）

把租户隔离拆成两层：

| 表归属 | 隔离机制 |
|---|---|
| **DeerFlow 自有表**（thread_meta / runs / feedback / users / 新增 tenant_*） | 标准 Postgres RLS + `SET LOCAL app.tenant_id` via SQLAlchemy session（DeerFlow 完全控制 conn pool） |
| **langgraph checkpoint 表**（checkpoints / checkpoint_blobs / checkpoint_writes / checkpoint_migrations） | 纯应用层兜底——入口处（`threads.py` + `thread_runs.py`）强制 `(tenant_id, thread_id)` 校验，依赖 thread_meta 表上 `(tenant_id, thread_id)` unique constraint |

**优点**：
- 不依赖 langgraph 库的任何 hook，库升级风险归零
- ADR-001 §4 里讨论的"subquery RLS"和"column upgrade path"的纠结都消解了——subquery RLS 只能保护读，写仍然要应用层兜，两条路径在 Option C 下统一为"应用层兜 + 自有表 RLS"
- 应用层校验点正好对齐审计报告里修正后的 ADR-001 hook 点（`threads.py` + `thread_runs.py`），不再绕错点
- 实现路径短：thread_meta 加 `tenant_id` 列 + unique constraint + 入口校验 = 几十行；不用动 langgraph 表 schema

**不足**：
- langgraph 表本身不是租户感知的，安全模型从"DB 强约束"降级为"应用层强约束"。需要承认这个 trade-off
- 如果将来出现绕过 `threads.py` / `thread_runs.py` 的写入路径（例如 LangGraph Studio 直连），租户隔离失效。需要在 CI 里加 boundary 测试禁止此类直连

### 3.4 不推荐路径：column upgrade（修改 langgraph 表 schema）

ADR-001 §4 提到的"给 langgraph 表加 tenant_id 列再做 RLS"。

**风险**：langgraph 用 `MIGRATIONS` 数组管理 schema 升级（`aio.py:82-109`）。我们 ALTER 出来的列会跟库升级冲突——每次升 `langgraph-checkpoint-postgres` 都要 diff 一遍 `MIGRATIONS` 数组。**不接受这个长期维护成本**。

## 4. 结论与对 ADR 的修订建议

### 4.1 事实纠正

- ❌ **错误假设**："langgraph-checkpoint-postgres>=2.0 支持 `connection_factory`"
- ✅ **现实**：v3.0.5 不存在 `connection_factory`；`from_conn_string` 无 hook；唯一能介入的只有 `__init__(conn=AsyncConnectionPool)` 这一个口子，且 pool 自带的 `configure` callback 不是 per-acquire

### 4.2 推荐落地方案

**Option C：两层隔离模型**

- DeerFlow 自有表：RLS + `SET LOCAL` via SQLAlchemy（沿用 ADR-001 思路）
- langgraph 表：应用层强校验（`threads.py` + `thread_runs.py`）+ thread_meta 表 unique constraint 兜底

### 4.3 待修订的 ADR 段落

- **ADR-001 §4**：删除 "subquery RLS" 和 "column upgrade path" 两段；替换为 "DeerFlow 表 RLS + langgraph 表应用层兜底" 的两层模型描述；hook 点保留审计报告修正后的 `threads.py` + `thread_runs.py`
- **ADR-006 §2.1 改造点 B**：删除 "RunManager binding 到 saver 的 conn"；明确 RunManager 不持有 langgraph conn pool，租户校验在 router 层完成
- **ADR-006 §1 表格**：MCP OAuth token 的描述同步修正（参见审计报告 cross-cutting risk #3）

### 4.4 后续再确认事项（非阻塞）

- 如果未来需要 langgraph 表也走 RLS，可以**向上游 PR 加 `connection_factory` 参数**，比 fork 或 hack pool 都干净。当前 v3.0.5 不阻塞 phase-0
- psycopg_pool 是否有更优雅的 per-acquire hook（v3.x 有无新增），可以在 phase-1 再 spike——phase-0 用不上
