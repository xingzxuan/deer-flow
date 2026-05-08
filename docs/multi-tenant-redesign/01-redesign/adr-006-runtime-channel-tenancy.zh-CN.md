# ADR-006 · 运行时与渠道层的租户化

| 项目 | 内容 |
|---|---|
| 状态 | 草稿（Draft） |
| 决策日期 | TBD |
| 决策者 | 后端 lead + 架构 + 渠道 owner |
| 关联 ADR | ADR-001 数据隔离、ADR-003 LLM Key 与计费、ADR-005 存储拓扑 |

---

## 1. 背景

ADR-001 ~ 005 解决了"数据/沙箱/Key/RBAC/存储"五个面，但 DeerFlow 还有几块**进程级、单例化、跨请求共享**的运行时组件，它们既不在仓储层也不在沙箱层，是 ADR-001 ~ 005 的"夹层"，独立讲一遍才不漏：

| 组件 | 现状 | 现在的 key | 多租户后的问题 |
|---|---|---|---|
| LangGraph Checkpointer | 内置 `AsyncSqliteSaver` / `AsyncPostgresSaver`（`runtime/checkpointer/async_provider.py`），表结构由 LangGraph 自己定 | `thread_id` | 表里只有 `thread_id`，没有 `tenant_id`；ADR-001 的 RLS policy 怎么挂、`SET LOCAL` 怎么注入 |
| MCP 工具缓存 | `mcp/cache.py:11` 一个进程级 `_mcp_tools_cache: list[BaseTool]`，按 `extensions_config.json` 的 mtime 失效 | 无（全局） | 每个租户启用的 MCP server 不同；当前缓存命中第一个加载的租户配置 |
| Skills loader | `skills/loader.py` 扫 `skills/public/` + `skills/custom/`，结果走 LRU；MCP 工具拼装在内 | 文件系统路径 | 多租户后 `skills/custom/` 不再是全局共享，要按租户维度拉/解压 |
| Sandbox provider 单例 | `LocalSandboxProvider` / `AioSandboxProvider` 在 lifespan 创建一次，所有 thread 共享 | thread_id | ADR-002 切到 K8s 后是 per-tenant Namespace，provider 必须知道当前租户 |
| Memory 抽取 LLM 调用 | `MemoryMiddleware` 30s debounce 后发 LLM 抽取事实 | 当前 thread 的 user_id | 这次调用的 token 算平台还是租户？BYO 时用谁的 key？ |
| Title / Summarization 内部 LLM 调用 | `TitleMiddleware` / `SummarizationMiddleware` 在 thread 上下文里复用主对话 LLM | 同上 | 同上 |
| IM 渠道 ↔ 用户绑定 | `app/channels/store.py` 把 IM 用户映射到平台 user_id；落 `~/.deer-flow/channels.yaml` | platform user_id | Slack workspace / 飞书租户 / 企微 corp 怎么映射到平台 tenant？webhook 来流量时怎么决定 tenant 上下文？ |

这一组不解决，ADR-001 的 RLS、ADR-003 的计费都是空中楼阁。

---

## 2. 决策（按组件分别给）

### 2.1 LangGraph Checkpointer

**决策**：保留 LangGraph 原生 checkpointer 表结构（不 fork），但要做四件事：

1. **逻辑租户隔离靠 thread_id 命名空间**：每个 `thread_id` 在 ADR-001 的 `threads_meta(tenant_id, thread_id)` 表里有租户归属。`AssistantsCompat` 路由收到 thread 操作时，**先用 `threads_meta` 校验 thread_id ↔ tenant_id**，再放行 LangGraph 调用。这是第一道防线。
2. **物理隔离靠 RLS + 注入**：在 LangGraph 自己的 `checkpoints` / `checkpoint_writes` / `checkpoint_blobs` 三张表上加 RLS policy，policy 通过 `app.tenant_id` session var 过滤。需要写一段 SQL 脚本在 init migration 里跑：
    ```sql
    -- LangGraph 自己创建表后跑（不动它的 schema）
    ALTER TABLE checkpoints ENABLE ROW LEVEL SECURITY;
    ALTER TABLE checkpoints FORCE ROW LEVEL SECURITY;

    -- 通过 thread_id 反查 tenant_id（subquery 形式，性能要测）
    CREATE POLICY tenant_isolation ON checkpoints
        USING (
          thread_id IN (
            SELECT thread_id::text FROM threads_meta
            WHERE tenant_id = current_setting('app.tenant_id', true)::uuid
          )
        );
    -- checkpoint_writes / checkpoint_blobs 同样形态
    ```
    缺点：subquery 形式 RLS 在大表（>千万行）上有性能风险。**备选**：在 LangGraph 表上**直接加一列 `tenant_id`**（用 ALTER TABLE，不 fork），靠每次 `put` 之前 trigger 从 thread_id 反查并写入。这是侵入更深但性能更好的路。
3. **`SET LOCAL` 注入路径**：LangGraph 的 `AsyncPostgresSaver` 自己持有连接池，DeerFlow 不能直接控制每次取连接。两条路：
    - **改造点 A**：包装一个 `TenantAwareConnectionFactory`，注入到 saver 构造参数；每次 acquire 时 `SET LOCAL app.tenant_id = '<uuid>'`。需要看 LangGraph 是否支持自定义 connection factory（`langgraph-checkpoint-postgres>=2.0` 支持）。
    - **改造点 B**：在 `RunManager` 创建/恢复 thread 前，**主动 issue 一条 `SET app.tenant_id`** 到 LangGraph 用的连接池上。需要 LangGraph 复用同一个连接（pool size = 1 模式不可行，必须能 binding）。
    - **首选 A**；如果 LangGraph 版本不支持，用 B 作为兜底，并在升级版本后切回 A。
4. **测试**：`backend/tests/test_harness_boundary.py` 之外，新增 `test_checkpointer_tenant_isolation.py` —— 建两个租户、各创建一个 thread，相互查询必须查不到。这是 RLS 是否生效的活体检测。

**推翻条件**：LangGraph 升级后表结构变化导致 RLS 不可挂 → 切换到 fork checkpointer 实现，自己控制表结构。

### 2.2 MCP 工具缓存

**决策**：模块级单例改为 **per-tenant 多级缓存**。

```python
# packages/harness/deerflow/mcp/cache.py

class TenantMCPCache:
    """Per-tenant MCP 工具缓存。

    第一级 key: tenant_id
    第二级 key: tenant_mcp_configs.updated_at（DB 版本，替代 mtime）
    """
    _caches: dict[str, _CachedTools] = {}
    _lock: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def get(self, tenant_id: str) -> list[BaseTool]:
        async with self._lock[tenant_id]:
            cached = self._caches.get(tenant_id)
            current_version = await mcp_config_repo.version(tenant_id)
            if cached and cached.version == current_version:
                return cached.tools
            # 失效或未初始化：拉 tenant_mcp_configs → 启 MultiServerMCPClient
            client = await build_mcp_client_for_tenant(tenant_id)
            tools = await client.get_tools()
            self._caches[tenant_id] = _CachedTools(tools=tools, version=current_version)
            return tools

    async def invalidate(self, tenant_id: str) -> None:
        async with self._lock[tenant_id]:
            self._caches.pop(tenant_id, None)
```

要点：

- 现在 `mcp/cache.py:31` 靠 `extensions_config.json` 的 mtime 判 stale；DB 化后用 `tenant_mcp_configs.updated_at`（或 `version` 列单调递增），失效信号走仓储层而不是文件系统。
- `MultiServerMCPClient` 实例**也要 per-tenant 持有**，因为它内部缓存了到各 MCP server 的连接 + OAuth token。租户切换不能复用别的租户的连接。
- **OAuth token 存储**：当前 `mcp/oauth.py` 的 token 落本地文件，多租户后必须挪到 `tenant_secrets`（`(tenant_id, key='mcp_oauth:<server_name>')`），KMS 加密。
- 进程内存上限：`TenantMCPCache` 加 LRU 上限（默认 1000 租户），超过淘汰最久未访问的；淘汰时关闭它的 MCP client 释放连接。
- Gateway PUT mcp 路由（`app/gateway/routers/mcp.py`）改完写 DB 后，调 `TenantMCPCache.invalidate(tenant_id)` 主动失效。

### 2.3 Skills loader

**决策**：拆成 **平台 skills（共享只读）+ 租户 skills（隔离可写）** 两套。

```
本地缓存目录 (LRU, 5GB):
  ${DEER_FLOW_SKILLS_CACHE}/platform/{skill_name}-{version}/
  ${DEER_FLOW_SKILLS_CACHE}/tenants/{tenant_id}/{skill_name}-{version}/
```

启动/调用流程：

1. Sandbox pod 启动时，从 ADR-005 的对象存储拉两类技能包：
   - 平台启用列表：`platform/skills/{name}/{version}.skill`（公司维护，所有租户可见）
   - 租户启用列表：`tenants/{tid}/skills/{name}/{version}.skill`（租户私有）
2. 解压到本地 LRU 缓存目录，挂到沙箱内的 `/mnt/skills/`（虚拟路径不变，对 agent 透明）
3. 工具拼装时 `get_available_tools()` 按 `tenant_skill_state(tenant_id, skill_name, enabled)` 过滤
4. 卸载/禁用：直接刷 `tenant_skill_state.enabled = false`，下一次 thread 启动时不挂载（在线 thread 不强制热卸，避免 in-flight 调用失败）

**LRU 淘汰策略**：按 `(tenant_id, skill_name)` 键淘汰；缓存满时优先淘汰非活跃租户的私有 skill，**永不淘汰 platform skills**（频繁命中）。

### 2.4 Sandbox provider 单例

**决策**：`SandboxProvider` 实例本身保持全局单例（每进程一个 K8s client），但 `acquire(thread_id)` 内部按 `tenant_id` 路由到对应 namespace：

```python
class K8sSandboxProvider:
    async def acquire(self, thread_id: str) -> SandboxHandle:
        tenant_id = resolve_tenant_id(AUTO, method_name="acquire")
        namespace = f"tenant-{tenant_id}"
        # 从 per-tenant prewarm 池借 pod，没有则按 tenant 配额创建
        pod = await self._pool.borrow(namespace, thread_id)
        return K8sSandboxHandle(pod=pod, tenant_id=tenant_id, thread_id=thread_id)
```

prewarm 池策略：

| 租户活跃度 | 池大小 |
|---|---|
| 7 天内有 thread 创建 | 按 plan 维度（free=0, pro=1, team=3, enterprise=可配）|
| 闲置 > 24h | 缩到 0；下次冷启动接受 P95 < 5s |
| 7 天内无活动 | 删除 namespace 的 prewarm 资源（保留 NS） |

`SandboxAuditMiddleware` 已经在 ADR-002 §5.6 加了 tenant_id；这里强调一句：**audit 写入不能复用业务 DB session**，要走独立 audit DB（避免业务回滚把审计也滚掉）。

### 2.5 Memory / Title / Summarization 的内部 LLM 计费

ADR-003 只覆盖了"主对话"的 LLM 调用计费，但 DeerFlow 的中间件链里有 3 处会**额外**触发 LLM：

| 中间件 | 触发时机 | 现状 token 归属 | 决策 |
|---|---|---|---|
| `MemoryMiddleware` | thread 闲置 30s 后异步抽取 | 主 LLM 配置 | **算入 tenant 用量**：用 tenant 当前 thread 的 model + key |
| `TitleMiddleware` | 首轮回复后给 thread 起标题 | 主 LLM 配置 | **算入 tenant 用量**：成本可见，不要藏 |
| `SummarizationMiddleware` | token 接近上限时压缩历史 | 主 LLM 配置 | **算入 tenant 用量**：和主对话不可分割 |

**统一原则**：凡是 tenant 触发的 thread 内部产生的 LLM 调用，**都计入该 tenant 的 quota 和账单**——理由：

1. 透明：客户能在 usage 报表里看到"主对话 vs 内部任务"分项，不奇怪
2. 安全：不会有"租户用 quota 跑完后还能让平台贴钱抽 memory"的 bug
3. BYO 一致：BYO 租户用自己的 key，平台不替他付任何 token

实现侧改造：

- 这 3 个中间件目前都通过 `create_chat_model()` 拿 LLM；ADR-003 §4.2 已经把这个函数改成 tenant-aware，自动会带上 tenant key
- `TokenUsageMiddleware` 现在按 message id 累加 token；多加一个 `usage_category` 字段（`main` / `memory` / `title` / `summarization`），写入 `tenant_usage_daily.usage_category`
- usage 报表 UI 区分这四类，让客户对账

**例外**：平台主动触发的 LLM 调用（比如平台 admin 跑健康检查时调用 LLM）算平台账，不算租户。

### 2.6 IM 渠道 ↔ 租户映射

这是产品形态决定的，方案分两档：

#### 形态 A：单租户独占一个 IM 集成（小客户/SaaS）

每个 Slack workspace / 飞书企业 / 钉钉 corp 绑到**一个** tenant。
现有 `app/channels/store.py` 的"channel binding"扩成：

```sql
channel_bindings (
  id UUID PK,
  tenant_id UUID FK,           -- 新增：每个绑定属于哪个租户
  platform VARCHAR(32),         -- slack / feishu / dingtalk / wecom / telegram / discord / wechat
  external_workspace_id VARCHAR(128),  -- Slack team_id / 飞书 tenant_key / 钉钉 corpId
  config_encrypted BYTEA,       -- bot token / app secret 等
  status VARCHAR(16),
  created_by UUID,
  created_at, updated_at,
  UNIQUE (platform, external_workspace_id)   -- 一个外部 workspace 只能绑一个 tenant
)
```

Webhook 路由（`app/channels/manager.py:740` 附近）：

```python
async def on_webhook(platform: str, payload: dict):
    external_id = extract_workspace_id(platform, payload)
    binding = await binding_repo.get(platform, external_id)
    if binding is None:
        return reject_unbound()
    # 把 tenant 上下文塞进去，再走 lead_agent
    set_current_tenant(binding.tenant_id, role=None)   # IM 渠道无 RBAC，按 tenant 默认权限
    user = await resolve_or_create_im_user(binding.tenant_id, payload.user)
    set_current_user(user)
    ...
```

**关键**：IM 来的请求**不走 JWT**，所以 ContextVar 注入要靠 webhook handler 自己写，不能漏。`app/channels/auth_filter.py` 加一道中间层强制每次 webhook 必经 `set_current_tenant`。

#### 形态 B：多租户共享一个 IM 集成（企业平台）

平台只装一个 Slack App，多个客户公司装在自己 workspace；通过 Slack `team_id` 自动路由到对应 tenant。逻辑同 A，但 onboarding 流程不一样：客户跳 Slack OAuth → 回调时根据登录态的 `tenant_id` 写 binding。

**取舍**：起步先做 A（每租户独立 IM 集成）。形态 B 是 enterprise marketplace 上架后才需要，不在 v1 范围。

#### IM user ↔ platform user 的映射

```sql
channel_user_links (
  tenant_id UUID FK,
  platform VARCHAR(32),
  external_user_id VARCHAR(128),
  user_id UUID FK,             -- platform user
  linked_at,
  PRIMARY KEY (tenant_id, platform, external_user_id)
)
```

未链接的 IM 用户 → 自动建 ghost user（`tenant_memberships.role = 'member'`），首次发消息时让用户在 IM 里点链接确认 → 落库。

---

## 3. 落地改造清单（与 ADR-001/005 不重叠）

| 模块 | 改动 | 估工 |
|---|---|---|
| `runtime/checkpointer/async_provider.py` | 接 LangGraph PG saver 的 connection factory，注入 `SET LOCAL` | M |
| 新建 `tests/test_checkpointer_tenant_isolation.py` | RLS 活体测试 | S |
| `mcp/cache.py` | 模块级 → `TenantMCPCache` 类 | M |
| `mcp/oauth.py` | OAuth token 存储从文件系统 → `tenant_secrets` | M |
| `app/gateway/routers/mcp.py` | PUT 接口改写 DB + 调 invalidate | S |
| `skills/loader.py` | 拆 platform / tenant 两路加载 | M |
| `app/gateway/routers/skills.py` | install 路径改为按 tenant 写 DB + S3 | M |
| `sandbox/k8s/provider.py`（ADR-002 新建） | `acquire` 注入 namespace = tenant | 已计入 ADR-002 |
| Sandbox prewarm pool | 新增 controller，按 tenant 维度管理 | L |
| `agents/memory/middleware.py` | usage_category=memory | S |
| `agents/title.py` / `agents/summarization.py` | usage_category=title/summarization | S |
| `agents/middleware/token_usage.py` | 增加 usage_category 维度 | S |
| `app/channels/store.py` | binding 表加 tenant_id | M |
| `app/channels/manager.py` | webhook handler 注入 tenant ContextVar | M |
| 新建 `app/channels/auth_filter.py` | 强制 tenant 上下文 | S |
| 新建 `channel_user_links` 仓储 | IM user ↔ platform user | M |

合计：约 12 人周（2 人 6 周 / 3 人 4 周），不含 ADR-001 ~ 005 各自的改造。

---

## 4. 风险与缓解

| 风险 | 缓解 |
|---|---|
| RLS subquery 反查 thread_id → tenant_id 全表扫 | 测试 EXPLAIN，必要时 LangGraph 表加 `tenant_id` 列（侵入但可控） |
| LangGraph 升级换 schema | CI 锁版本；升级前先跑 tenant isolation 测试集 |
| `TenantMCPCache` 内存爆 | LRU 上限 + 闲置淘汰；监控租户数 / 进程 |
| MCP server 自身泄露租户上下文 | 出网走 ADR-002 egress gateway 白名单；MCP server 在沙箱内 stdio 启动时 env 不带平台 secret |
| Memory 抽取算入租户用量被客户抗议"我没让它跑" | UI 显式开关 + 用量分项展示；默认开启可关 |
| IM webhook 没注入 tenant 上下文 → 调用 RLS 全过滤掉空集 | 中间层 fail-closed；监控空集查询率 |
| 多 IM 平台 token 在 `channel_bindings.config_encrypted` 泄露 | KMS 加密 + 审计每次 decrypt（参照 ADR-003 §4.6） |
| ghost IM user 没绑回 platform user → 数据归到 ghost | 强制首次 IM 交互弹链接卡片 + N 天未确认自动停 |

---

## 5. 推翻条件

- LangGraph 官方推出原生多租户 checkpointer → 切换并废弃本 ADR §2.1 的注入方案
- 平台决定走"完全集中式 IM"（所有租户共用一个 bot account） → §2.6 切到形态 B 为唯一形态
- MCP server 全部跑在 K8s sidecar 而非进程内 → §2.2 缓存策略需要重写，client 实例从进程内挪到 service mesh

---

## 6. 默认假设

| 项 | 默认 |
|---|---|
| Checkpointer | LangGraph PG saver + RLS（subquery 反查 thread_id） |
| MCP cache | per-tenant LRU，上限 1000 租户/进程 |
| Skills | platform 公共只读 + tenant 私有；本地 LRU 5GB |
| Sandbox prewarm | per-tenant 池，按 plan 配置大小 |
| 内部 LLM 调用计费 | 全部记入 tenant，分 4 类 usage_category |
| IM 集成形态 | 形态 A：每租户独立 binding，UNIQUE(platform, external_workspace_id) |
| IM ghost user TTL | 7 天未链接自动停 |
| 审计 DB | 与业务 DB 物理分离（独立连接池或独立实例） |
