# 多租户改造 · 总览与汇总索引

> 写于 2026-05-10。把 7 份 ADR + 2 份 spike/审计 + 4 份 rollout / schema 文档，按"ADR 状态 + 5 阶段（Stage 0–4）的业务目标 / 技术路径 / 验证方式"重新串一遍，让团队从任何角度切入都能找到对应位置。
>
> **范围**：仅汇总与导航，不引入新决策。具体决策正文在各自的 ADR / rollout 文档里。
>
> **目标客户画像**（来自 [phased-rollout-by-scale §0](./02-rollout/phased-rollout-by-scale.zh-CN.md)）：以个人用户为主、少量小团队；**统一只有 workspace 概念**（个人 = 1 人 workspace）；**中心化 SaaS 主线**（schema 兼容 on-prem，按合同启用）；**Freemium**（DeerFlow 付 LLM 账单）。

---

## 0. 文档总图

```
docs/multi-tenant-redesign/
├── README.zh-CN.md                        ← 本文档（入口）
├── 00-current-state/
│   └── architecture-overview.zh-CN.md     现状架构鸟瞰
├── 01-redesign/                           决策（ADR）+ 锁定文档
│   ├── adr-001-data-isolation             数据隔离（行级 + RLS）
│   ├── adr-002-sandbox-isolation          沙箱隔离（K8s + gVisor）
│   ├── adr-003-llm-key-billing            LLM Key 与计费（混合 BYO）
│   ├── adr-004-tenant-rbac                租户 RBAC（owner/admin/member）
│   ├── adr-005-storage-topology           存储拓扑（DB + S3 + emptyDir）
│   ├── adr-006-runtime-channel-tenancy    运行时夹层 + IM 渠道
│   ├── adr-007-routing-frontend           URL / Cookie / 前端
│   ├── adr-spike-langgraph-postgres       spike：LangGraph PG 注入能力
│   ├── adr-vs-code-audit                  审计：ADR vs 现状代码
│   ├── multi-tenant-phase-0-plan          Phase-0 时间盒 / 产出物
│   └── workspace-schema-design            **Stage 0 schema 锁定版**（不可逆决策点）
└── 02-rollout/                            落地路线 + 集成轨道
    ├── phased-rollout-by-scale            **Stage 0–4 主线** 路线图
    ├── stage-0-code-map                   Stage 0 现状代码地图（行号锚点）
    └── headless-api-track                 业务系统集成轨道（Pattern A / B）
```

---

## 1. ADR 与配套文档状态表

| # | 文档 | 状态 | 最近修订 | 核心决策摘要 |
|---|---|---|---|---|
| **ADR-001** | [数据隔离](./01-redesign/adr-001-data-isolation.zh-CN.md) | 草稿 · 据 spike 修订 | 2026-05-09 | 行级 `workspace_id` + Postgres RLS（仅 DeerFlow 自有表）；LangGraph 表走应用层强校验 + `UNIQUE(wid, thread_id)` 兜底 |
| **ADR-002** | [沙箱隔离](./01-redesign/adr-002-sandbox-isolation.zh-CN.md) | 草稿 | 2026-05-08 | K8s namespace + gVisor + NetworkPolicy 默认禁出网；premium 切 Kata-Firecracker。**Stage 1 仅做 AioSandbox 加固，K8s 推迟到 Stage 3** |
| **ADR-003** | [LLM Key & 计费](./01-redesign/adr-003-llm-key-billing.zh-CN.md) | 草稿 · 据审计修订 §4.2/§4.3/§4.4.2 | 2026-05-09 | 混合 BYO：Free/Pro 用平台 key + quota；Enterprise BYO；悲观预扣防"幽灵 token"；Memory/Title/Summarization 内部 LLM 全计入 workspace |
| **ADR-004** | [租户 RBAC](./01-redesign/adr-004-tenant-rbac.zh-CN.md) | 草稿 | 2026-05-08 | 二级 RBAC（owner/admin/member）；JWT 带 role + 30s LRU cache + 敏感操作 `strict=True` 必查 DB；`token_version` bump 触发失效 |
| **ADR-005** | [存储拓扑](./01-redesign/adr-005-storage-topology.zh-CN.md) | 草稿 | 2026-05-08 | 三层：Postgres（结构化）+ S3（大对象 + presigned）+ emptyDir（临时）；沙箱 pod 真正无状态 |
| **ADR-006** | [运行时夹层 + 渠道](./01-redesign/adr-006-runtime-channel-tenancy.zh-CN.md) | 草稿 · 据 spike + 审计修订 | 2026-05-09 | LangGraph 表走应用层校验；MCP cache per-workspace；OAuth token 入 KMS DB；IM 渠道加 `channel_bindings(workspace_id)` |
| **ADR-007** | [URL / 前端](./01-redesign/adr-007-routing-frontend.zh-CN.md) | 草稿 · Better Auth 假设作废 | 2026-05-09 | path-based slug `/{slug}/...`；扩现有自签 JWT 加 `wid/role`，不引入 Better Auth；切换 workspace 硬刷新 |
| spike | [LangGraph PG 注入](./01-redesign/adr-spike-langgraph-postgres.zh-CN.md) | 已结论 | 2026-05-09 | `langgraph-checkpoint-postgres==3.0.5` **不存在 connection_factory**；改走应用层强校验 + 自有表 RLS 的两层模型 |
| 审计 | [ADR vs 代码](./01-redesign/adr-vs-code-audit.zh-CN.md) | 已结论 | 2026-05-09 | 代码库 0 处 `tenant`；Better Auth 不存在；ObjectStorage / KMS / Postgres 测试夹具全缺；底座先行 §3.5 |
| 锁定 | [workspace-schema-design](./01-redesign/workspace-schema-design.zh-CN.md) | **Stage 0 锁定版** | 2026-05-10 | `workspace_id` 命名 + 7 项不可逆决策；Stage 0 PR1 动手前必读 |
| 计划 | [phase-0-plan](./01-redesign/multi-tenant-phase-0-plan.zh-CN.md) | 计划 | 2026-05-09 | Phase-0 时间盒 3 周；含底座先行（§3.5） |
| 路线 | [phased-rollout-by-scale](./02-rollout/phased-rollout-by-scale.zh-CN.md) | **当前主线路线图** | 2026-05-09 | Stage 0–4 + 触发/退出/时间盒/Go-No-Go |
| 锚点 | [stage-0-code-map](./02-rollout/stage-0-code-map.zh-CN.md) | Stage 0 用 | 2026-05-09 | 当前代码文件:行号锚点 + Stage 0 改动落点 |
| 集成 | [headless-api-track](./02-rollout/headless-api-track.zh-CN.md) | Stage 1 内并行轨道 | 2026-05-10 | API key + service account + Pattern A/B（不做嵌入式 widget） |

---

## 2. Stage 0–4 速览矩阵

| Stage | 触发 | 退出 | 时间盒 | 主要 ADR 章节 |
|---|---|---|---|---|
| **0** | 现在 / 准备开第一个付费客户 | 外部用户能登入、看到自己 workspace、隔离干净；**生产已跑在 Postgres 上** | 4–5 周（含 Postgres 切换 + testcontainers）| ADR-001 §4.1.2 / ADR-001 §4.4（Postgres 切换）/ ADR-004 §1-§3（owner-only 简化）/ ADR-007 §1-§5 / workspace-schema-design 全篇 |
| **1** | 首批付费 (10–50 / 500–2k free) + 1-2 业务系统集成 | 可放心曝光 + 业务系统 go-live | 8–13 周（双轨并行；Postgres 已在 Stage 0 切完）| ADR-001 §4.1（应用层校验完整）+ ADR-002 §3 轻量 + ADR-003 §4.3-§4.4 + ADR-007 §6-§8 + headless-api 全篇 |
| **2** | 100–500 付费 / 5k–20k 用户 | 架构能撑用户 ×10 | 10–16 周 | ADR-001 §4.2（DeerFlow 表 RLS）+ ADR-003 §4.1-§4.2（KMS）+ ADR-004 §5（完整 RBAC）+ ADR-005 §1-§5 + ADR-006 §2.2/§2.5/§2.6 |
| **3** | 1k+ 付费 / 50k+ 用户 **或** 安全/成本事故 | 撑到 enterprise 销售前夕 | 16–26 周 | ADR-002 §1-§5（K8s 完整）+ ADR-003 §4.6（BYO）+ ADR-006 §2.4（prewarm 池）+ ADR-007 §9（custom domain 预留） |
| **4** | 单 enterprise 合同（合规 / SSO / 自定义域名） | 长期持续 | 单客户 4–8 周 | ADR-001 §6（per-tenant DB）+ ADR-002 §5（gVisor）+ ADR-007 §8 SSO 段 + §9 |

---

## 3. 各阶段详细：业务目标 / 技术路径 / 验证方式

### Stage 0 · workspace 模型立起来 + Postgres 切换 + auth 收紧

**业务目标**
- 把 workspace 概念落到 schema 层、auth 层、入口路由层，给"开第一个付费客户"准备好底座
- **生产 backend 切到 Postgres**——Stage 0 没有生产数据，迁移阻力最小；省 Stage 1 重 ALTER 一遍的返工
- 不追求真隔离（无 RLS、无 K8s、无 KMS），追求**模型立得住**——后续每个 Stage 加东西都不需要重写 Stage 0 的产物

**技术路径**（按 PR 拆分）
1. **Postgres 接入 + testcontainers**：docker-compose 加 PG service、`make doctor` / CI 兼容；testcontainers 集成到 backend 测试套；现有 SQLite dev 数据导入（如有）
2. **将默认 backend 切到 Postgres**：`make setup` / `make dev` / `.env.example` 默认指向 PG；SQLite 保留为可选 dev 兜底
3. 新建 `workspaces` + `workspace_memberships` 表 + 仓储（[workspace-schema-design §2.1-§2.2](./01-redesign/workspace-schema-design.zh-CN.md)）
4. 注册 / `/auth/initialize` 改造：每个新用户自动建 1 人 workspace（owner=自己）；JWT 扩 `wid` + `role`（owner-only 简化）
5. 现有 4 张表 ALTER 加 `workspace_id` 列（直接在 Postgres 上加，先 nullable）+ 数据回填脚本（"legacy_workspace"）→ 改 NOT NULL
6. 入口路由 `(workspace_id, thread_id)` 强校验 — `threads.py` + `thread_runs.py`（ADR-001 §4.1.2）
7. CI boundary 测试：禁止任何路径绕过入口直连 LangGraph saver（ADR-006 §2.1）
8. Stage 0 末追加：`service_accounts` / `api_keys` / `external_users` schema only（不接路径，为 Stage 1 准备）

**验证方式**
- 单测：`test_workspace_repo.py` / `test_workspace_membership_repo.py`（partial unique、CASCADE、slug 黑名单）
- 集成：注册新用户 → DB 中可见 1 个 workspace + 1 条 owner membership + JWT cookie 含 `wid`
- 回归：Stage 0 部署到生产 ≥ 2 周，无 workspace 隔离 bug 报告；Postgres 上稳定运行 ≥ 2 周，无 schema / 性能 regression
- CI boundary：`test_langgraph_access_boundary.py` 静态扫描不能命中绕过路径
- testcontainers 夹具：CI 跑通至少 1 个 Postgres 集成测试模板（policy Stage 2 才启用，但夹具 Stage 0 就位）

---

### Stage 1 · 第一批付费客户 + 业务系统集成（双轨并行）

**业务目标**
- **付费 SaaS 轨道**：上线 quota + 计费 + 基础沙箱收紧，让"开放注册"不会被滥用刷爆 LLM 账单
- **Headless API 轨道**：让 1-2 个业务系统能用 API key 调通核心 endpoint go-live，含浏览器直连场景（streaming 友好）

**技术路径**

**轨道 A · 付费 SaaS**（Postgres 已在 Stage 0 切完，本轨道直接从 quota 起）：
1. `workspace_quotas` / `workspace_usage_daily` 表 + 仓储（ADR-003 §4.3）
2. `TokenUsageMiddleware` 升级为持久化 + 4 类 `usage_category`（ADR-003 §4.4 + ADR-006 §2.5）
3. `QuotaMiddleware` 加入中间件链 + 悲观预扣防幽灵 token（ADR-003 §4.4.1）
4. Stripe webhook + 订阅状态同步到 `workspace_quotas.plan`
5. AioSandbox egress 白名单 + cgroup CPU/memory 限额（ADR-002 §3 轻量版，**不上 K8s**）
6. 基础监控：per-workspace token 用量、quota 命中率、异常告警

**轨道 B · Headless API Pattern A**（与 A 并行；无前置依赖）：
1-10. service_accounts / api_keys / external_users 仓储 → APIKeyAuthBackend 双路径 → CSRF skip on bearer → `/api/v1/` 切换 → external_user_id 透传 → identity_mode 三态 → `@require_permission` 升级 → 基础 rate limit → API key 管理 CLI/UI → idempotency keys（详 [headless-api §6 Stage 1 PR 顺序](./02-rollout/headless-api-track.zh-CN.md#6-与-stage-1-的整合)）

**轨道 C · Headless API Pattern B**（依赖轨道 B 的 1-7；Stage 1 末 1-2 周）：
1-5. `workspaces.allowed_origins` 列 → `WorkspaceAwareCORSMiddleware` → `POST /api/v1/auth/exchange-token` → `ServiceTokenAuthBackend` → SSE 跨域 streaming 验证 + 业务方接入示例

**验证方式**
- 单测：quota 中间件硬限/软限/预扣/释放；APIKey 哈希存储；ServiceTokenPayload 签发与验证
- 集成：`test_billing_stream_abort.py`（客户端断流仍记 token）；2 个 workspace 互调 thread 必 404
- 端到端：业务系统 demo 应用用 API key 跑通 thread 创建 / SSE / external_user_id 透传
- 业务事实：1-2 个业务系统集成 go-live 并稳定运行 ≥ 1 个月；月活付费 ≥ 50 或免费 ≥ 1k；出现一次"差点超额"事件证明 quota gate 在工作

---

### Stage 2 · 增长期，安全与隔离深化

**业务目标**
- 把 Stage 1 的"应用层兜底"升级为"DB 层兜底"——RLS、KMS、ObjectStorage 三大底座落地
- 团队 workspace 真正可用（invitation + 完整 RBAC）
- 内部 LLM 计费透明化、per-user skill 覆盖支持小团队个性化

**技术路径**（PR 顺序）
1. testcontainers + Postgres CI 跑通（phase-0 §3.5 底座之一）
2. ObjectStorage Protocol + LocalObjectStorage + 端到端打通（ADR-005 §4-§5）
3. KMS 抽象 + AWS/阿里云 KMS 实现 + 已有 secret 灰度迁移（ADR-003 §4.1）
4. **DeerFlow 自有表启用 RLS**（testcontainers 验证后上生产；ADR-001 §4.2）
5. S3ObjectStorage 实现 + 上传/产物迁 S3
6. 多档付费（Free/Pro/Team）+ invitation 流程 + role 扩到 owner/admin/member
7. `WorkspaceMCPCache` + OAuth token 落 KMS 加密 DB（ADR-006 §2.2）
8. per-user skill 覆盖（`user_skill_overrides`）+ per-user skill config（KMS 加密；ADR-005 §5.4 扩展）
9. 内部 LLM 计费分类（Memory/Title/Summarization usage_category；ADR-006 §2.5）
10. Webhook outbound（Stage 1 推迟过来；headless-api §1）+ 分维度 rate limit（引入 Redis）
11. 基础 audit log（写业务 DB，Stage 3 才拆）

**验证方式**
- RLS 冒烟：testcontainers 起 Postgres，跨 workspace SELECT 必返空；跨 workspace UPDATE 必拒绝
- KMS：secret 写入后 DB 列只见密文；rotate 流程不破坏旧密文解密
- 计费分项：UI 报表能区分 main / memory / title / summarization；BYO key 走自己额度
- 业务事实：跨 workspace 数据访问尝试 0 次（哪怕日志里）；客户开始问 BYO key

---

### Stage 3 · 成熟期，K8s 隔离 + BYO

**业务目标**
- 沙箱从"AioSandbox 加固"升级到"K8s + namespace + NetworkPolicy"，杜绝单进程资源争用
- BYO LLM key 作为付费档福利交付
- 拆分 audit DB、加跨 region 备份，为 enterprise 销售铺路

**技术路径**
1. K8s 集群部署 + per-workspace namespace + ResourceQuota / LimitRange + NetworkPolicy 默认禁出网（ADR-002 §5）
2. `K8sSandboxProvider` 全新建 + Cosign 镜像签名 + Pod Security Standard restricted（ADR-002 §5.1-§5.5）
3. per-workspace prewarm 池 controller，按 plan 大小（ADR-006 §2.4）
4. BYO LLM key 路径：`workspace_secrets` 解密 → `create_chat_model()` 优先用 tenant key（ADR-003 §4.2 + §4.6）
5. presigned URL 全量启用 + S3 lifecycle（按 plan 设保留期）
6. audit DB 拆分（独立连接池或独立实例；ADR-006 §2.4 脚注）
7. 跨 region 备份 / DR（RTO ≤ 4h）
8. 完整监控栈：Grafana + Prometheus + APM + per-workspace SLA

**验证方式**
- 安全：渗透测试容器逃逸场景（gVisor 启用前后对比）；NetworkPolicy 阻断 169.254.169.254 / 内网 IP
- 性能：Pod 冷启动 P50 < 2s / P99 < 5s；prewarm 命中率 > 80%
- 业务：BYO 客户 ≥ 5 个；K8s 切换零数据丢失；audit DB 写入与业务 DB 解耦验证

---

### Stage 4 · 企业化，按需开启

**业务目标**
- 单客户合同驱动，不为"万一"提前投资
- SSO / 自定义域名 / per-tenant DB / gVisor / 合规审计——按客户付费决定做哪几样

**技术路径**（按合同选做）
- **SSO（SAML/OIDC）**：复用现有 oauth_provider 字段；新建 `workspace_sso_configs`；IdP 用户/组映射到 workspace_memberships（ADR-007 §8 SSO 段）
- **自定义域名**：`workspaces.custom_domain` 列 + ACME 动态签证 + nginx vhost 路由（ADR-007 §9）
- **物理数据隔离**：仅该客户切 per-tenant DB（ADR-001 §6 推翻条件）
- **私有部署**：打 enterprise tier docker 镜像 + 部署文档；放弃中心化运维优势
- **gVisor / Kata 切换**：K8s 切对应 RuntimeClass（ADR-002 §5）
- **合规审计报告（SOC2/ISO）**：强化 audit log 留存 + 评估机构对接

**验证方式**
- 合同里写明的 SLA / 合规条款逐条验收
- 安全审计 / 渗透测试报告（如客户要求）
- SSO IdP 端到端登录测试

---

## 4. Stage ↔ ADR 章节细粒度对照

| Stage | ADR-001 | ADR-002 | ADR-003 | ADR-004 | ADR-005 | ADR-006 | ADR-007 | headless-api |
|---|---|---|---|---|---|---|---|---|
| 0 | §4.1.2 入口校验 + §4.4 Postgres 切换 | — | — | §1-§3 owner-only | — | — | §1-§5（无 slug 路由可暂缓） | §2 schema only |
| 1 | §4.1 应用层校验完整版（**不含 RLS**；Postgres 已 Stage 0 切完）| §3 轻量（egress + cgroup） | §4.3-§4.4 quota + 持久化 + 预扣 | — | — | §2.5 usage_category | §6-§8 slug + JWT 扩字段 | §2-§5 全（Pattern A）+ §3.5 全（Pattern B）|
| 2 | §4.2 DeerFlow 表 RLS | — | §4.1-§4.2 KMS + create_chat_model 改造 | §5 完整 RBAC + invitation | §1-§5 ObjectStorage 完整 | §2.2 / §2.5 / §2.6 | — | §1 Webhook outbound |
| 3 | — | §1-§5 K8s 完整 | §4.6 BYO | — | §5 第 2 阶段（presigned + lifecycle）| §2.4 prewarm 池 | §9 custom domain 预留 | — |
| 4 | §6 per-tenant DB | §5 gVisor / Kata | — | §5.7 SSO/SCIM | — | — | §8 SSO 段 + §9 落地 | — |

---

## 5. 不可逆决策一览（按 Stage 集中）

| Stage | 决策 | 文档锚点 | 反悔代价 |
|---|---|---|---|
| 0 | **Postgres 切换**（dev + 生产）| [phased-rollout Stage 0](./02-rollout/phased-rollout-by-scale.zh-CN.md#stage-0--workspace-模型立起来--postgres-切换--auth-收紧) + [phase-0-plan §3.5](./01-redesign/multi-tenant-phase-0-plan.zh-CN.md#35-底座先行phase-0-之前--并行的基础设施) | Stage 0 选这个时机：没有生产数据，迁移阻力最小；切完不回头 |
| 0 | `workspace_id` 列加到所有业务表（直接 Postgres） | [workspace-schema-design §3](./01-redesign/workspace-schema-design.zh-CN.md#3-alter-现有表) | 漏一张 → Stage 1 补；不再走 SQLite → Postgres 二次迁移 |
| 0 | `workspaces` 表字段（id 类型 / slug 字符集 / owner_id 冗余） | [workspace-schema-design §5](./01-redesign/workspace-schema-design.zh-CN.md#5-不可逆决策清单) | 7 项已锁定；任何一项改主意 → revert PR1 重写 |
| 0 | JWT TokenPayload 字段集 `{sub, wid, role, exp, iat, ver}` | [workspace-schema-design §4](./01-redesign/workspace-schema-design.zh-CN.md#4-jwt-tokenpayload--一次到位的字段集) | 加新字段 → bump `token_version` 全用户重登；Stage 0 一次性加齐省一次 churn |
| 0 | 文件系统路径形态 `workspaces/{wid}/threads/{tid}/...` | [stage-0-code-map §4](./02-rollout/stage-0-code-map.zh-CN.md#4-threaddatamiddleware--路径系统) | 改了所有用户产物 URL 失效 |
| 0 | API Key 格式（`dfk_live_*` / `dfk_test_*`）+ `service_accounts` 不跨 workspace | [headless-api §8](./02-rollout/headless-api-track.zh-CN.md#8-不可逆决策动手前想清楚) | 业务系统接入后改格式所有 key 失效 |
| 1 | `/api/v1/` mount prefix + deprecation 时间 | [headless-api §4](./02-rollout/headless-api-track.zh-CN.md#4-核心设计api-版本化) | 业务系统接了之后改 prefix 全部联调 |
| 1 | identity_mode 三态语义（collapsed / external / both） | [headless-api §3](./02-rollout/headless-api-track.zh-CN.md#3-核心设计两种身份模式) | 改语义所有业务系统集成重测 |
| 1 | memory 隔离粒度（SA 共享 vs per external_user） | 同上 | 客户用上后迁移 memory 数据极麻烦 |
| 2 | ObjectStorage prefix 形态 `workspaces/{wid}/...` | [ADR-005 §2.1](./01-redesign/adr-005-storage-topology.zh-CN.md#21-各类数据的归属) | 改了所有用户产物 URL 失效 |
| 3 | K8s namespace 命名规则（`ws-{wid}` vs `tenant-{wid}`） | [ADR-002 §5.2](./01-redesign/adr-002-sandbox-isolation.zh-CN.md#52-k8s-资源每租户-namespace-一份)（落代码读 `ws-{wid}`） | 改了所有 NetworkPolicy / RBAC / 监控 dashboard |

---

## 6. 用语映射速查

> ADR 与 02-rollout 系列因写作时序不同，存在两组用语并行。这里给一张一次性映射表，避免读不同文档时反复对照。

| ADR 用语 | 落代码 / 02-rollout 用语 | 备注 |
|---|---|---|
| `tenant_id`（数据库列、Python 变量） | `workspace_id` | workspace-schema-design §1 锁定 |
| `tenants` 表 | `workspaces` 表 | 同上 |
| `tenant_memberships` | `workspace_memberships` | 同上 |
| `tenant_secrets` | `workspace_secrets` | 同上 |
| `tenant_quotas` / `tenant_usage_daily` | `workspace_quotas` / `workspace_usage_daily` | 同上 |
| `tenant_skill_state` / `tenant_mcp_configs` | `workspace_skill_state` / `workspace_mcp_configs` | 同上 |
| JWT `tid` claim | JWT `wid` claim | workspace-schema-design §4 锁定 |
| K8s `tenant-{tenant_id}` namespace | `ws-{workspace_id}` | ADR-002 §5.2 → 落代码读 |
| ObjectStorage `tenants/{tid}/...` prefix | `workspaces/{wid}/...` | ADR-005 §2.1 → 落代码读 |
| `TenantMCPCache` 类名 | `WorkspaceMCPCache` | ADR-006 §2.2 → 落代码读 |
| ADR 用语 "v1 / v2 路线图" | rollout Stage 0–4 | "v1" ≈ Stage 0+1；"v2" ≈ Stage 2+；具体见每条决策的 Stage 标注 |
| ADR-005 §5 "第 1 阶段 / 第 2 阶段" | rollout Stage 2 / Stage 3 | ADR-005 §5 已加映射注 |
| ADR-006 §3 "约 14 人周" | 对应 rollout Stage 2 必做项里的 §2.2/§2.5/§2.6 | — |

---

## 7. 阅读路径建议

**第一次进项目（30 min）**：
1. 本 README
2. [00-current-state/architecture-overview](./00-current-state/architecture-overview.zh-CN.md) — 现状是什么样的
3. [phased-rollout-by-scale](./02-rollout/phased-rollout-by-scale.zh-CN.md) §0 + §总览 + §Stage 0 — 现在在哪、下一步做什么

**准备动手做 Stage 0（半天）**：
1. [workspace-schema-design](./01-redesign/workspace-schema-design.zh-CN.md) **全文** — 不可逆决策、PR 拆分
2. [stage-0-code-map](./02-rollout/stage-0-code-map.zh-CN.md) **全文** — 当前代码锚点
3. [ADR-001 §4.1.2](./01-redesign/adr-001-data-isolation.zh-CN.md) + [ADR-007 §8](./01-redesign/adr-007-routing-frontend.zh-CN.md)
4. [ADR-006 §2.1](./01-redesign/adr-006-runtime-channel-tenancy.zh-CN.md) + [adr-spike-langgraph-postgres](./01-redesign/adr-spike-langgraph-postgres.zh-CN.md) — 为什么 LangGraph 表不挂 RLS

**准备动手做 Stage 1（一天）**：
1. [phased-rollout Stage 1](./02-rollout/phased-rollout-by-scale.zh-CN.md) — 双轨并行
2. [headless-api-track](./02-rollout/headless-api-track.zh-CN.md) **全文** — Pattern A/B 完整设计
3. [ADR-003 §4.3-§4.4](./01-redesign/adr-003-llm-key-billing.zh-CN.md) — quota + 悲观预扣
4. [ADR-002 §3](./01-redesign/adr-002-sandbox-isolation.zh-CN.md) — Stage 1 用 §3 轻量版（**不**读 §5 K8s 完整版）

**做安全/合规评审**：
1. ADR-001 / ADR-002 / ADR-003 §4.6（BYO）/ ADR-004 §5.4（strict 装饰器）
2. [adr-vs-code-audit](./01-redesign/adr-vs-code-audit.zh-CN.md) cross-cutting risks 全部
3. headless-api §3.5 + §8（短期 JWT TTL / Token revoke 策略）

**做架构评审**：
1. ADR-001 全 + ADR-005 全 + ADR-006 全
2. spike + audit
3. workspace-schema-design §5 不可逆清单

---

## 8. 常见问题（FAQ）

**Q：Postgres 切换在哪个 Stage？**
A：**Stage 0**。原稿放 Stage 1，但 Stage 0 已经要 ALTER 4 张表加 `workspace_id`，先 SQLite 加列再 Stage 1 重 ALTER 是纯返工；Stage 0 没有生产数据，迁移阻力最小，且 phase-0 §3.5 早已把 Postgres testcontainers 列为底座。详见 [phased-rollout Stage 0](./02-rollout/phased-rollout-by-scale.zh-CN.md#stage-0--workspace-模型立起来--postgres-切换--auth-收紧) + [phase-0-plan §3.5](./01-redesign/multi-tenant-phase-0-plan.zh-CN.md#35-底座先行phase-0-之前--并行的基础设施)。SQLite 仅保留为可选 dev 兜底。

**Q：ADR 写 `tenant_id`，代码写 `workspace_id`，到底哪个是对的？**
A：代码以 `workspace_id` 为准（[workspace-schema-design §1](./01-redesign/workspace-schema-design.zh-CN.md) 锁定）。ADR 不重命名是为了节省成本，每份 ADR 顶部已加交叉提示。

**Q：ADR-002 写要上 K8s + gVisor，是不是 Stage 1 就要做？**
A：不是。Stage 1 只做 ADR-002 §3 的"AioSandbox 出网白名单 + cgroup 限额"。K8s + gVisor 的完整方案推迟到 Stage 3。ADR-002 §1 已加分期落地提示。

**Q：LangGraph 表为什么不挂 RLS？**
A：spike 验证 `langgraph-checkpoint-postgres==3.0.5` 不存在 `connection_factory`，无法注入 `SET LOCAL app.tenant_id`。改走应用层强校验（`threads.py` + `thread_runs.py` 入口）+ `threads_meta` `UNIQUE(workspace_id, thread_id)` 兜底。详见 [spike 报告](./01-redesign/adr-spike-langgraph-postgres.zh-CN.md) §3.3 / [ADR-001 §4.1.1](./01-redesign/adr-001-data-isolation.zh-CN.md)。

**Q：是否引入 Better Auth？**
A：不引入。审计发现前端实际不用 Better Auth；扩现有 `app/gateway/auth/jwt.py` 加 `wid` + `role` 字段比引入框架的破坏面小得多。详见 [ADR-007 §8](./01-redesign/adr-007-routing-frontend.zh-CN.md)。

**Q：业务系统集成走哪种 pattern？**
A：默认 Pattern A（业务 backend 代理，长期 API key）；自研 web 页面 + streaming 延迟敏感的走 Pattern B（短期 JWT，浏览器直连）。**不做 Pattern C 嵌入式 widget**。详见 [headless-api §0 速览](./02-rollout/headless-api-track.zh-CN.md#集成-pattern-速览)。

**Q：on-prem 怎么定位？**
A：SaaS 是产品主线；Stage 0 的 schema 设计同时兼容 on-prem（`workspaces.id` 1:1 对应 self-host 安装），按 enterprise 客户合同启用，不作为并行产品线投入。详见 [phased-rollout §0](./02-rollout/phased-rollout-by-scale.zh-CN.md) + [headless-api §7](./02-rollout/headless-api-track.zh-CN.md#7-saas-vs-on-prem-差异saas-是主线)。

---

## 9. 推翻条件（什么会让整个分期方案重排）

来自 [phased-rollout §推翻条件](./02-rollout/phased-rollout-by-scale.zh-CN.md#推翻条件)：

- **目标客户画像突变**：拿到 enterprise 合同要求 SSO + 自定义域名 → Stage 4 部分提前到 Stage 2
- **出现安全事故**：跨 workspace 泄露 / sandbox 逃逸 → 跳过未启动 Stage，直接做 Stage 3 的 K8s + RLS
- **付费转化远不及预期**：Stage 1 上线 6 个月付费 < 10 → 重评 freemium 模型，可能不需要走完 Stage 2/3
- **LLM 价格大跌 / 自部署模型成熟**：成本控制优先级下降 → quota 与 BYO 可简化

> 单条 ADR 的"推翻条件"在每份 ADR 末尾，触发时只重排该 ADR 涉及的 Stage。
