# ADR-006 · 运行时与渠道层的租户化

| 项目 | 内容 |
|---|---|
| 状态 | 草稿（Draft） · 2026-05-09 据 spike + 审计修订 §1 / §2.1 / §2.2 / §2.5 / §2.6 / §3 / §4 / §6 |
| 决策日期 | TBD |
| 决策者 | 后端 lead + 架构 + 渠道 owner |
| 关联 ADR | ADR-001 数据隔离、ADR-003 LLM Key 与计费、ADR-005 存储拓扑 |
| 关联 spike / 审计 | [adr-vs-code-audit](./adr-vs-code-audit.zh-CN.md) · [langgraph-postgres spike](./adr-spike-langgraph-postgres.zh-CN.md) |
| 代码命名 | 本 ADR 写 `tenant_id` / `TenantMCPCache` / `tenant-{tenant_id}` namespace，落代码统一读作 `workspace_id` / `WorkspaceMCPCache` / `ws-{workspace_id}`（详 [workspace-schema-design §1](./workspace-schema-design.zh-CN.md#1-命名约定--workspace-vs-tenant)） |

---

## 1. 背景

ADR-001 ~ 005 解决了"数据/沙箱/Key/RBAC/存储"五个面，但 DeerFlow 还有几块**进程级、单例化、跨请求共享**的运行时组件，它们既不在仓储层也不在沙箱层，是 ADR-001 ~ 005 的"夹层"，独立讲一遍才不漏：

| 组件 | 现状 | 现在的 key | 多租户后的问题 |
|---|---|---|---|
| LangGraph Checkpointer | 内置 `AsyncSqliteSaver` / `AsyncPostgresSaver`（`runtime/checkpointer/async_provider.py:73,117`，走 `from_conn_string`），表结构由 LangGraph 自己定 | `thread_id` | 表里只有 `thread_id`，没有 `tenant_id`；`langgraph-checkpoint-postgres==3.0.5` 不存在 `connection_factory`（[spike](./adr-spike-langgraph-postgres.zh-CN.md) §2），`SET LOCAL` 注入路径不通；只能走应用层强校验 |
| MCP 工具缓存 | `mcp/cache.py:11` 一个进程级 `_mcp_tools_cache: list[BaseTool]`，按 `extensions_config.json` 的 mtime 失效 | 无（全局） | 每个租户启用的 MCP server 不同；当前缓存命中第一个加载的租户配置 |
| MCP OAuth token | `mcp/oauth.py:25-31` `OAuthTokenManager` token 缓存为**进程内存 `dict[str, _OAuthToken]`**，**无任何持久化**——进程重启后重新刷 token | 无（全局） | 多租户后必须按 tenant 隔离 + 持久化，从"无持久化"直接到"KMS 加密 DB"——比"文件挪到 DB"成本高 |
| Skills loader | `skills/loader.py` 扫 `skills/public/` + `skills/custom/`，结果走 LRU；MCP 工具拼装在内 | 文件系统路径 | 多租户后 `skills/custom/` 不再是全局共享，要按租户维度拉/解压 |
| Sandbox provider 单例 | `LocalSandboxProvider` / `AioSandboxProvider` 在 lifespan 创建一次，所有 thread 共享 | thread_id | ADR-002 切到 K8s 后是 per-tenant Namespace，provider 必须知道当前租户 |
| Memory 抽取 LLM 调用 | `MemoryMiddleware` 30s debounce 后发 LLM 抽取事实 | 当前 thread 的 user_id | 这次调用的 token 算平台还是租户？BYO 时用谁的 key？ |
| Title / Summarization 内部 LLM 调用 | `TitleMiddleware` / `SummarizationMiddleware` 在 thread 上下文里复用主对话 LLM | 同上 | 同上 |
| IM 渠道 ↔ 用户绑定 | `app/channels/store.py:36-42` 把 IM `channel:chat[:topic]` 映射到 `{thread_id, user_id}`；落 `${DEER_FLOW_HOME}/channels/store.json`（不是 `channels.yaml`），**当前没有 binding / workspace 概念** | platform user_id | Slack workspace / 飞书租户 / 企微 corp 怎么映射到平台 tenant？webhook 来流量时怎么决定 tenant 上下文？需要新建 `channel_bindings` 表 |

这一组不解决，ADR-001 的 RLS、ADR-003 的计费都是空中楼阁。

---

## 2. 决策（按组件分别给）

### 2.1 LangGraph Checkpointer

**决策**：保留 LangGraph 原生 checkpointer（不 fork、不 ALTER 它的表），租户隔离走**应用层强校验 + thread_id 唯一约束兜底**。详见 ADR-001 §4.1.1 两层模型。

> 历史背景：原稿设想用 `connection_factory` 注入 `SET LOCAL app.tenant_id` + 在 LangGraph 表上挂 RLS。spike 验证（[adr-spike-langgraph-postgres](./adr-spike-langgraph-postgres.zh-CN.md)）发现 `langgraph-checkpoint-postgres==3.0.5` 不存在 `connection_factory` 参数；备选方案（子类化 `psycopg_pool` / 包装 saver / ALTER 加列）都有不可接受的维护成本。改用应用层强校验。

具体落地三件事：

1. **入口路径强校验**：所有调 LangGraph saver 的入口——**`app/gateway/routers/threads.py`** 和 **`app/gateway/routers/thread_runs.py`**——在调用前必须先在 `threads_meta` 上做 `(tenant_id=current, thread_id=requested)` 查询；未命中即 404，命中后才放行。
   - 创建 thread：先在 `threads_meta` 写入 `(tenant_id, thread_id, user_id)`，依赖 `UNIQUE (tenant_id, thread_id)` 复合索引兜底防重；再调 LangGraph 创建对应 thread
   - 读/写 thread：先 SELECT `threads_meta`，命中后再放行
   - **不在 LangGraph 自有表上挂 RLS**——`SET LOCAL app.tenant_id` 只对 DeerFlow 自有表生效（ADR-001 §4.2）
2. **CI boundary 测试**：`backend/tests/` 下新增 `test_langgraph_access_boundary.py`——静态扫描禁止任何路径 import LangGraph saver/client 而绕过 `threads.py` / `thread_runs.py`。LangGraph Studio 直连必须走相同入口或显式审批
3. **活体测试**：`tests/test_checkpointer_tenant_isolation.py`——建两个租户、各创建一个 thread，互相通过对方 thread_id 调 `/api/threads/{tid}` / `/api/threads/{tid}/runs` 必须 404；同 tid 走自己路径必须正常。这是入口校验是否生效的回归网

**推翻条件**：
- LangGraph 上游加入 `connection_factory` 或等价 hook → 切回"DeerFlow 表 + LangGraph 表统一 RLS"模型
- 应用层校验在压测中暴露 perf 瓶颈 → 评估 fork checkpointer 自控 schema

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
- **OAuth token 存储**：当前 `mcp/oauth.py:25-31` 的 token 是**进程内存 `dict[str, _OAuthToken]`，无任何持久化**——进程重启后重新刷 token。多租户化要直接做"租户隔离 + 持久化 + 加密"三步并发：落 `tenant_secrets`（`(tenant_id, key='mcp_oauth:<server_name>')`）+ KMS 加密 + 失败回退到刷新流程。工作量比"文件挪到 DB"高一档，估工 M+。
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

- 这 3 个中间件目前都通过 `create_chat_model()` 拿 LLM；ADR-003 §4.2 把这个函数改成 tenant-aware，自动会带上 tenant key
- **`TokenUsageMiddleware` 当前只 log，不持久化**（`agents/middlewares/token_usage_middleware.py:268-275`）；新增 `usage_category` 字段（`main` / `memory` / `title` / `summarization`）+ 持久化路径都要从空白起，写入 `tenant_usage_daily(tenant_id, date, usage_category, tokens_in, tokens_out)`。详见 ADR-003 §4.4
- usage 报表 UI 区分这四类，让客户对账

**例外**：平台主动触发的 LLM 调用（比如平台 admin 跑健康检查时调用 LLM）算平台账，不算租户。

### 2.6 IM 渠道 ↔ 租户映射

这是产品形态决定的，方案分两档：

#### 形态 A：单租户独占一个 IM 集成（小客户/SaaS）

每个 Slack workspace / 飞书企业 / 钉钉 corp 绑到**一个** tenant。

> 现状澄清：当前 `app/channels/store.py:36-42` 只存 `channel:chat[:topic] → {thread_id, user_id}` 的 JSON 字典（路径 `${DEER_FLOW_HOME}/channels/store.json`），**没有 binding / workspace 概念**。下面的 `channel_bindings` 表是新建，不是扩展现有结构。

新建 `channel_bindings` 表：

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
| `app/gateway/routers/threads.py` + `thread_runs.py` | 入口注入 `(tenant_id, thread_id)` 校验；写路径先入 `threads_meta` | M |
| 新建 `tests/test_checkpointer_tenant_isolation.py` | LangGraph 入口校验活体测试（不再是 RLS 测试） | S |
| 新建 `tests/test_langgraph_access_boundary.py` | 静态扫描禁止绕过入口直连 saver | S |
| `mcp/cache.py` | 模块级 → `TenantMCPCache` 类 | M |
| `mcp/oauth.py` | OAuth token 从**进程内存** → `tenant_secrets`（持久化 + KMS 加密 + 失败回退到刷新） | M+ |
| `app/gateway/routers/mcp.py` | PUT 接口改写 DB + 调 invalidate；不再依赖 `extensions_config.json` mtime | S |
| `skills/loader.py` | 拆 platform / tenant 两路加载 | M |
| `app/gateway/routers/skills.py` | install 路径改为按 tenant 写 DB + S3 | M |
| `sandbox/k8s/provider.py`（ADR-002 新建） | `acquire` 注入 namespace = tenant | 已计入 ADR-002 |
| Sandbox prewarm pool | 新增 controller，按 tenant 维度管理 | L |
| `agents/middlewares/memory_middleware.py` | usage_category=memory | S |
| `agents/middlewares/title_middleware.py` / `summarization_middleware.py` | usage_category=title/summarization | S |
| `agents/middlewares/token_usage_middleware.py` | **从只 log 升级为持久化**（参 ADR-003 §4.4） + 增加 usage_category 维度 | M |
| 新建 `channel_bindings` 表 + 仓储 | IM workspace ↔ tenant 映射（不复用 store.json） | M |
| `app/channels/store.py` | 现有 `channel:chat → {thread_id, user_id}` 字典升级为 SQL 表，并按 tenant 分区 | M |
| `app/channels/manager.py` | webhook handler 注入 tenant ContextVar | M |
| 新建 `app/channels/auth_filter.py` | 强制 tenant 上下文 fail-closed | S |
| 新建 `channel_user_links` 仓储 | IM user ↔ platform user | M |

合计：约 14 人周（2 人 7 周 / 3 人 5 周），不含 ADR-001 ~ 005 各自的改造。

---

## 4. 风险与缓解

| 风险 | 缓解 |
|---|---|
| **LangGraph 表无 RLS，应用层校验失效则跨租户**（§2.1 trade-off） | `threads_meta` `UNIQUE (tenant_id, thread_id)` 兜底；CI boundary 测试禁止绕过入口路由直连 saver；任何新增 LangGraph 直连路径必须 PR review |
| LangGraph 升级换 schema | CI 锁版本；升级前先跑 tenant isolation 测试集 |
| `TenantMCPCache` 内存爆 | LRU 上限 + 闲置淘汰；监控租户数 / 进程 |
| MCP server 自身泄露租户上下文 | 出网走 ADR-002 egress gateway 白名单；MCP server 在沙箱内 stdio 启动时 env 不带平台 secret |
| **MCP OAuth token 持久化是从无到有**，错误处理面更大 | 持久化失败时回退到"无持久化进程内存"模式（不阻塞业务）+ 报警；KMS 不可用时 fail-closed |
| Memory 抽取算入租户用量被客户抗议"我没让它跑" | UI 显式开关 + 用量分项展示；默认开启可关 |
| IM webhook 没注入 tenant 上下文 → 调用 RLS 全过滤掉空集 | 中间层 fail-closed；监控空集查询率 |
| 多 IM 平台 token 在 `channel_bindings.config_encrypted` 泄露 | KMS 加密 + 审计每次 decrypt（参照 ADR-003 §4.6） |
| ghost IM user 没绑回 platform user → 数据归到 ghost | 强制首次 IM 交互弹链接卡片 + N 天未确认自动停 |

---

## 5. 推翻条件

- LangGraph 官方推出原生多租户 checkpointer 或上游加入 `connection_factory` 等价 hook → 切回 ADR-001 §4.1.1 "DeerFlow 表 + LangGraph 表统一 RLS" 模型，废弃本 ADR §2.1 的应用层强校验作为唯一防线
- 平台决定走"完全集中式 IM"（所有租户共用一个 bot account） → §2.6 切到形态 B 为唯一形态
- MCP server 全部跑在 K8s sidecar 而非进程内 → §2.2 缓存策略需要重写，client 实例从进程内挪到 service mesh

---

## 6. 默认假设

| 项 | 默认 |
|---|---|
| Checkpointer | LangGraph PG saver 原状（不挂 RLS、不 ALTER 表）+ 入口路由强校验 + `threads_meta` `UNIQUE(tenant_id, thread_id)` 兜底 |
| MCP cache | per-tenant LRU，上限 1000 租户/进程 |
| MCP OAuth token | `tenant_secrets` 持久化 + KMS 加密；持久化失败回退到进程内存 + 报警 |
| Skills | platform 公共只读 + tenant 私有；本地 LRU 5GB |
| Sandbox prewarm | per-tenant 池，按 plan 配置大小 |
| 内部 LLM 调用计费 | 全部记入 tenant，分 4 类 usage_category；`TokenUsageMiddleware` 必须先升级为持久化 |
| IM 集成形态 | 形态 A：每租户独立 binding，UNIQUE(platform, external_workspace_id) |
| IM ghost user TTL | 7 天未链接自动停 |
| 审计 DB | 与业务 DB 物理分离（独立连接池或独立实例） |
