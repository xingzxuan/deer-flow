# 多租户改造 ADR 与现状代码审计报告

> 审计日期：2026-05-09
> 审计基线：分支 `docs/multi-tenant-redesign` @ `dce5e959`
> 目的：在动手写迁移代码之前，逐条核对 7 份 ADR + phase-0 计划对**当前代码**的假设是否成立，避免基于错误前提做架构决策。

---

## ADR-001: 数据隔离模型

### Assumptions about current code

1. 仓储层使用 `user_id` ContextVar + `AUTO` 哨兵自动注入 — **HOLDS**
   - Evidence: `backend/packages/harness/deerflow/runtime/user_context.py:135-167`（`AUTO` sentinel + `resolve_user_id`）；`backend/packages/harness/deerflow/persistence/thread_meta/sql.py:35-41` 已按此模式调用。
2. LangGraph checkpointer 使用 `AsyncPostgresSaver` 自带连接池 — **HOLDS**
   - Evidence: `backend/packages/harness/deerflow/runtime/checkpointer/async_provider.py:73,117` `AsyncPostgresSaver.from_conn_string(...)`；DeerFlow 不传 `connection_factory`。
3. SQLAlchemy 引擎单例 + `AsyncSession` factory 已就位（即"DeerFlow 仓储侧连接池"）— **HOLDS**
   - Evidence: `backend/packages/harness/deerflow/persistence/engine.py:26-27,126`；`init_engine_from_config` 支持 sqlite/postgres/memory。
4. `AssistantsCompat` 路由可作为 thread_id↔tenant_id 校验拦截点 — **DOES NOT HOLD**
   - Evidence: `backend/app/gateway/routers/assistants_compat.py:1-50` 该路由仅服务 `/api/assistants` 的 `assistants.search/get` 静态 stub，**不**触达 thread。Thread 入口在 `backend/app/gateway/routers/threads.py` 与 `thread_runs.py`。
   - Reality: ADR 把 hook 点写错；实际拦截点应是 `threads.py` + `thread_runs.py`。
5. 仓储层 `WHERE user_id` 已是默认行为，加一层 `tenant_id` 是平行扩展 — **PARTIALLY HOLDS**
   - Evidence: `persistence/thread_meta/sql.py:69-70` 是**应用层 `if row.user_id != resolved_user_id`** 后过滤，而非 SQL `WHERE`；其他仓储多数才走 SQL WHERE。RLS"前导列必须是 tenant_id"的索引前提目前完全不存在。
6. 当前是 SQLite 默认，Postgres 是可选 backend — **HOLDS**
   - Evidence: `persistence/engine.py:80-114` 同时支持 sqlite/postgres/memory，README/CLAUDE 默认演示 sqlite。

### Highest-risk gaps

- **`AssistantsCompat` hook 点错位**：迁移时若按 ADR 字面落地，会跳过实际 thread 创建路径 (`threads.py:create`)，导致 `(tenant_id, thread_id)` 应用层校验缺位、RLS 兜底成唯一防线。
- **SQLite/Postgres 双驱动现状**：ADR §4.4 写"在 dev 同时跑双驱动"——目前 SQLite 是首选 backend，没有 Postgres 测试夹具 / RLS 测试基础设施，迁移启动成本被低估。

---

## ADR-002: 沙箱隔离模型

### Assumptions about current code

1. 存在两个 sandbox provider：`LocalSandboxProvider`（零隔离）+ `AioSandboxProvider`（Docker） — **HOLDS**
   - Evidence: `backend/packages/harness/deerflow/sandbox/local/local_sandbox_provider.py`、`backend/packages/harness/deerflow/community/aio_sandbox/`、`sandbox/security.py:6-7` 把 LocalSandboxProvider 的 host bash 显式禁用。
2. SandboxProvider 是进程级单例，`acquire(thread_id)` 在 lifespan 创建一次共享 — **HOLDS**
   - Evidence: `sandbox/sandbox_provider.py:41-58` `_default_sandbox_provider` + `get_sandbox_provider()` 单例；`sandbox/middleware.py:45-63` 直接调 `provider.acquire(thread_id)`。
3. `SandboxAuditMiddleware` 已存在并记录工具调用 — **HOLDS**
   - Evidence: `backend/packages/harness/deerflow/agents/middlewares/sandbox_audit_middleware.py` 文件存在；CLAUDE.md L164 列在中间件链。
4. K8s/Provisioner 模式存在但仅作为 sandbox 选项 — **PARTIALLY HOLDS**
   - Evidence: `backend/CLAUDE.md` 提到 `provisioner` port 8002 在配置 aio_sandbox+provisioner 时启动；但仓库中无 `K8sSandboxProvider`，没有 namespace/NetworkPolicy/gVisor 任何配套，威胁模型是"未来"而非"现状"。
5. Sandbox audit 复用业务 DB session — **UNVERIFIABLE**（未深入审计中间件 DB 写入路径）

### Highest-risk gaps

- **`AioSandboxProvider` 出网/资源/cosign 全部缺位**：ADR §1 把它列为"起点不错"，但实际上未禁出网、未限 CPU/memory、未 readOnly rootfs、未签名校验——MVP 多租户上线**绝不能直接复用现有 provider**。
- **K8s Sandbox 几乎从零开工**：ADR-002 § 5.1 估"新建 K8sSandboxProvider"是单条 bullet，实际是子系统，估工 L+。

---

## ADR-003: LLM Key 与计费模型

### Assumptions about current code

1. `create_chat_model()` 是进程级模型工厂、配置走 `config.yaml` + 环境变量替换 — **HOLDS**
   - Evidence: `backend/packages/harness/deerflow/models/factory.py:50` 函数签名 `(name, thinking_enabled, *, app_config, **kwargs)`，无 tenant 参数；`models/factory.py` 通过 `resolve_class` 反射构造 LLM。
2. 当前 `TokenUsageMiddleware` 在 `after_model` 一次性提交 token — **HOLDS**
   - Evidence: `agents/middlewares/token_usage_middleware.py:288-294` `after_model` / `aafter_model` 调 `_apply`；`_apply` 仅 log + 更新 `additional_kwargs`，**不**写 DB（更不区分 usage_category）。
3. 存在 `MemoryMiddleware` / `TitleMiddleware` / `SummarizationMiddleware` 三处内部 LLM 调用 — **HOLDS**
   - Evidence: `agents/middlewares/{memory,title,summarization}_middleware.py` 全部存在（CLAUDE L169-170）。
4. tenant_secrets / tenant_quotas / tenant_usage_daily 表已存在 — **DOES NOT HOLD**
   - Evidence: `persistence/{user,thread_meta,run,feedback}/model.py` 即全部 ORM 模型；无任何 tenant_* 表。
5. ADR 描述的 `TokenUsageMiddleware` 已"按 message id 累加 token"写表 — **DOES NOT HOLD**
   - Evidence: `token_usage_middleware.py:268-275` 仅 logger.info，**未持久化** token；`runs/model.py:35-41` 的 `total_input_tokens` 在 `RunManager.update_run_completion` 时一次写 — 没有按租户/类别维度。

### Highest-risk gaps

- **没有任何用量持久化基础**：ADR-003 §4.4 的 `QuotaMiddleware`、`tokens_reserved`、悲观预扣全部要从空白起；现状 `TokenUsageMiddleware` 仅 log，幽灵 token 防御从零开工。
- **`create_chat_model` 是同步函数**：ADR §4.2 改造目标签名是 async（要 await secret_vault.get），但当前是 sync——所有调用点（lead agent factory、memory updater 等）要同步改 async 或换 secret 注入路径。

---

## ADR-004: 租户 ↔ 用户层级与 RBAC

### Assumptions about current code

1. `users.system_role` 存在且仅 `admin`/`user` 两值 — **HOLDS**
   - Evidence: `persistence/user/model.py:33` `system_role: Mapped[str] = ... default="user"`；`auth/models.py:23` `Literal["admin", "user"]`。
2. `users.token_version` 已存在用作 JWT 失效 — **HOLDS**
   - Evidence: `persistence/user/model.py:49` `token_version: Mapped[int] ... default=0`；`auth/jwt.py:18,36` JWT payload 已带 `ver` claim。
3. JWT payload 当前结构是 `{sub, exp, iat, ver}`（无 tid/role）— **HOLDS**
   - Evidence: `app/gateway/auth/jwt.py:14-19,36`：`TokenPayload` 仅 4 字段，**没有 tid/role**。
4. 已有 `@require_permission(resource, action, owner_check=...)` 装饰器，可以扩展 — **HOLDS**
   - Evidence: `app/gateway/authz.py:197-280`；现状 `owner_check` 是 `bool`，ADR §5.4 想升级为 `"self"|"self_or_admin"|"admin_only"|"owner_only"|strict=True`，需要重构。
5. `AuthMiddleware` 在 ContextVar 注入 user — **HOLDS**
   - Evidence: `app/gateway/auth_middleware.py:122` `set_current_user(user)`；尚无 `set_current_tenant`。
6. `tenant_memberships` 表存在 — **DOES NOT HOLD**
   - Evidence: `persistence/` 目录无 tenants/memberships/invitations 任何表。

### Highest-risk gaps

- **`token_version` 只是 column，无 cache 层 / membership 失效 path**：ADR §5.2.3 的 `MembershipCache` 30s LRU + bump 触发机制全部要新建。
- **现有 `system_role="admin"` 是平台级管理员且唯一**：ADR §6 计划保留它做 platform_admin，但现状 admin 与"租户内 owner"语义未分离，迁移时首启逻辑（"创建第一个 admin"）会与新增"创建 default 租户 + 设其为 owner"耦合，需要兼容旧部署。

---

## ADR-005: 存储拓扑

### Assumptions about current code

1. memory.json 落 `{base_dir}/users/{user_id}/memory.json` 文件 — **HOLDS**
   - Evidence: `agents/memory/storage.py:84-102`；`config/paths.py:155-157` `user_memory_file()`。
2. agent SOUL.md / config.yaml 落 `{base_dir}/users/{user_id}/agents/{name}/` — **HOLDS**
   - Evidence: `config/paths.py:163-169` user_agent_dir / user_agent_memory_file 系列；CLAUDE backend.md L356-359 描述一致。
3. 自定义 skills 走 `skills/custom/` 全局共享、非 per-user — **HOLDS**
   - Evidence: `skills/storage/local_skill_storage.py:24-32` layout `<root>/{public,custom}/...`；`<root>` 来自 `config.skills.get_skills_path()`，没有 user_id 维度。
4. `extensions_config.json` 在仓库根目录、被 mtime 失效驱动 — **HOLDS**
   - Evidence: `mcp/cache.py:11-53` `_config_mtime` + `_is_cache_stale`；`config/extensions_config.py` 存在；Gateway 路由 `routers/skills.py:321,336` / `routers/mcp.py:142,164` 直接读写文件并 `reload_extensions_config()`。
5. 上传走本地 thread 目录 — **HOLDS**
   - Evidence: `uploads/manager.py:40-48` `get_paths().sandbox_uploads_dir(thread_id, user_id=...)`。
6. `ObjectStorage` 抽象 / `LocalObjectStorage` / `S3ObjectStorage` 已存在 — **DOES NOT HOLD**
   - Evidence: `find ... -name "storage*"` 仅命中 `agents/memory/storage.py` 与 `skills/storage/`；harness 内**没有** `storage/protocol.py`、`storage/s3.py` 任何 ObjectStorage 抽象。
7. `agent_configs` / `memory_facts` / `tenant_skill_state` 表存在 — **DOES NOT HOLD**
   - Evidence: `persistence/` 仅 `user/`/`thread_meta/`/`run/`/`feedback/`；ADR-005 §2.1 列出的 7 张新表全部不存在。

### Highest-risk gaps

- **三层拓扑全部要新建**：ObjectStorage 抽象（约 800 行 Protocol+实现）+ 7 张新表 + 4 个迁移脚本——ADR §5 第 1 阶段被列为 4 步实际是 12+ 步子项。
- **memory/agent 文件 → DB 迁移会触发 `agents/memory/storage.py` 全面重写**：当前缓存键 `(user_id, agent_name)` + 原子 `temp+rename` 写 + `MemoryUpdateQueue` 30s debounce 全部假设文件系统语义。

---

## ADR-006: 运行时与渠道层的租户化

### Assumptions about current code

1. `mcp/cache.py:11` 是模块级单例 `_mcp_tools_cache: list[BaseTool]` — **HOLDS**
   - Evidence: `mcp/cache.py:11-14` 完全字面命中。
2. MCP cache 失效靠 `extensions_config.json` mtime — **HOLDS**
   - Evidence: `mcp/cache.py:31-53` `_is_cache_stale` 比对 `os.path.getmtime`。
3. `MultiServerMCPClient` 实例缓存在进程内 + 持有 OAuth token — **PARTIALLY HOLDS**
   - Evidence: `mcp/oauth.py:25-31` `OAuthTokenManager` token 缓存是 `dict[str, _OAuthToken]` **进程内存**；ADR-006 §2.2 / §1 表格写"OAuth token 落本地文件"——**不正确**，目前**没有持久化**，每次进程重启重新刷 token。
   - Reality: token 在 memory only，迁移时挪到 `tenant_secrets` 是从 0 起，比"从文件挪到 DB"成本更高（要新加持久化 + 加密）。
4. `LocalSandboxProvider` / `AioSandboxProvider` 在 lifespan 创建一次单例 — **HOLDS**
   - Evidence: `sandbox/sandbox_provider.py:41-58` 全局单例 + `get_sandbox_provider`。
5. `MemoryMiddleware` / `TitleMiddleware` / `SummarizationMiddleware` 都通过 `create_chat_model()` 拿 LLM — **HOLDS**（推断）
   - Evidence: 三个 middleware 文件存在；`models/factory.py:50` 是唯一工厂（CLAUDE 已说明），统一入口意味着 ADR §2.5 改造点统一。
6. `app/channels/store.py` 把 IM 用户映射到平台 user_id，落 `~/.deer-flow/channels.yaml` — **PARTIALLY HOLDS**
   - Evidence: `app/channels/store.py:36-42` 默认路径是 `Paths.base_dir / "channels" / "store.json"`（不是 `channels.yaml`）；存的是 `channel:chat → {thread_id, user_id}`。
   - Reality: 文件名是 `store.json`、且没有 `binding` 概念（IM workspace ↔ platform 映射），ADR §2.6 需新建 `channel_bindings` 表 + 重构 store。
7. `RunManager` 创建/恢复 thread 前可以 issue `SET app.tenant_id` — **DOES NOT HOLD**（条件不具备）
   - Evidence: `runtime/runs/manager.py:41-78` 该类是**纯内存 run 注册表**，不持有 LangGraph 连接池。LangGraph saver 由 `make_checkpointer` 独立 lifespan 管理（`checkpointer/async_provider.py:73,117`），DeerFlow 无法控制每次 acquire；ADR §2.1 改造点 B 假设 RunManager 能 binding 到 saver 的 conn——目前没有这条 binding 通路。

### Highest-risk gaps

- **IM channel store 当前没有 binding 概念**（只有 `channel:chat → thread_id` 映射），ADR §2.6 描述的"webhook 来流量时按 binding 注入 tenant"需要先把 `store.json` 升级为 `channel_bindings` 表 + 重构 webhook handler 路径。
- **LangGraph `SET LOCAL` 注入路径未验证**：ADR §2.1 说"langgraph-checkpoint-postgres>=2.0 支持 connection_factory"是假设，现状 `from_conn_string` 路径不传 factory；切换前要先验证库版本是否支持。
- **MCP OAuth token 不持久化是事实但 ADR 描述错误**：迁移点不是"文件挪到 DB"，而是"无持久化 → KMS 加密 DB"——实际工作量更大。

---

## ADR-007: 路由与前端租户化

### Assumptions about current code

1. nginx 把 `/api/*` → Gateway 8001、`/api/langgraph/*` → 同 Gateway 重写 — **HOLDS**
   - Evidence: 仓库根 CLAUDE.md "Architecture at a glance" 描述；backend CLAUDE L226 也确认。
2. 前端用 Better Auth 走 cookie session — **DOES NOT HOLD**
   - Evidence: `frontend/package.json` 不依赖 `better-auth`（grep 命中 0 次）；`frontend/src/core/auth/proxy-policy.ts:52` cookie name 是 `access_token`、由后端 `app/gateway/auth/jwt.py` 自签 JWT；`frontend/src/core/auth/server.ts:25-26` 直接读 `access_token` cookie 调 Gateway `/auth/me`。
   - Reality: 前端 auth 是后端自有 JWT + cookie 直通，**不是 Better Auth**；ADR §8 "Better Auth 接入"整段需要重写为"自有 JWT 中间件接入"。
3. LangGraph SDK 在 `core/api/` 单例 — **HOLDS**
   - Evidence: `frontend/src/core/api/api-client.ts` `createCompatibleClient` 内 `new LangGraphClient(...)`；模块导出单一 client。
4. 没有租户概念，单 host 单工作区 — **HOLDS**
   - Evidence: `frontend/src/app` 路由组 `(auth)` / `[lang]` / `workspace` / `blog`，无 `(tenant)/[slug]/`；`grep -rn "tenant"` 在 frontend 命中也基本为零。
5. CSRF middleware 已存在 — **HOLDS**
   - Evidence: `app/gateway/csrf_middleware.py` 文件存在；前端 `core/api/api-client.ts:20-32` `injectCsrfHeader` 从 `csrf_token` cookie 读。
6. AuthMiddleware 从 cookie 读 JWT、注入 user_id 到 ContextVar — **HOLDS**
   - Evidence: `app/gateway/auth_middleware.py:84,112,122`。
7. 存在 `/setup` 路径处理首启 — **HOLDS**
   - Evidence: `frontend/src/app/(auth)/setup/`；`auth/repositories/sqlite.py:118` 数 admin 用户。

### Highest-risk gaps

- **Better Auth 不存在**：ADR §8 整段"Better Auth 注入 tid/role/tv"前提作废。要么重写 ADR，要么把现有 `auth/jwt.py:14-19` `TokenPayload` 直接扩字段 + bump `ver`——后者其实更简单，但需要 ADR 显式承认。
- **前端路由全部按 `(tenant)/[slug]/` 重组的工作量**：当前 `app/workspace/chats/[thread_id]` / `app/workspace/agents/...` 已是核心路径，整体 L 估工没有问题但会触动几乎所有 Server Components。

---

## Cross-cutting risks（跨 ADR）

1. **整个代码库 0 处 `tenant_id` 字段 / 类型 / 引用**：`grep -rn "tenant" backend/packages/harness/ backend/app/` 命中为空。所有 ADR 假设的 ContextVar (`set_current_tenant`)、JWT claim (`tid`)、表列 (`tenant_id`)、路径 (`/tenants/{tid}/`)、缓存键全部不存在 — 任何"加 tenant 维度"的改造都是从零起，而非"扩展现有"。

2. **没有 ObjectStorage / 没有 KMS / 没有 Postgres 测试基础设施**：ADR-001 RLS、ADR-003 secret vault、ADR-005 三层存储、ADR-006 OAuth 持久化都共用同一组缺失底座 — 这组底座必须先于任何业务改造落地，否则各 ADR 互为前置条件死锁。

3. **Better Auth 与 LangGraph connection_factory 两个外部依赖假设错误**：ADR-007 假设有 Better Auth、ADR-001/006 假设 langgraph-checkpoint-postgres 支持 connection_factory；前者**当前不存在**、后者**当前未启用**。两个 ADR 写决策时把"外部库能力"误当现状，是同一类风险。

4. **`extensions_config.json` 是当前 MCP/skills 状态的唯一真源**：`mcp/cache.py` mtime 失效、`routers/{mcp,skills}.py` 直接读写文件、`tools/tools.py:115-119` 同样依赖；它向 DB 迁移会同时触动 ADR-005 §5.4（拆库）、ADR-006 §2.2/2.3（cache 重构）、ADR-004 §5.4（写敏感操作 strict）三个 ADR。

5. **当前代码的 user_id 过滤是"应用层后过滤 + 部分 SQL WHERE 混合"**：`thread_meta/sql.py:69-70` 是 `if row.user_id != resolved_user_id` 应用层比对，不是 SQL `WHERE`。RLS 假设"加 tenant_id 是平行扩展"在现状下被打了折扣 — 索引前导列、SQL `WHERE` 形态、应用层过滤路径都需要先标准化才能加 RLS 兜底。
