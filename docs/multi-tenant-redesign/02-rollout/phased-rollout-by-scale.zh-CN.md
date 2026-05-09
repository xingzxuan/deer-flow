# 多租户改造 · 按规模分期落地方案

> 写于 2026-05-09。基于已收敛的目标客户画像 + ADR 决策。
>
> **目标客户画像**：
> - 用户群体：以个人用户为主，少量小团队
> - workspace 模型：**统一只有 workspace 概念**，个人用户 = 1 人 workspace；团队 = 多成员 workspace（无单独"个人空间"概念）
> - 部署形态：**中心化 SaaS**（DeerFlow 团队运维，不做 self-host 主线）
> - 商业化：**Freemium**——免费用户 + 付费分层，DeerFlow 付 LLM 账单
>
> 这三个画像决定了分期取舍：**成本/隔离要早做、企业级特性可以一直推迟**。

---

## 总览

| Stage | 触发条件（业务事实） | 主旋律 | 时间盒 |
|---|---|---|---|
| **0** | 现在 → 第一个付费客户准备 | workspace 模型立起来；现有 auth 收紧；不做真隔离 | 3–4 周 |
| **1** | 第一批付费客户（10–50 付费 / 500–2000 free） | **Postgres + Quota 必落**；workspace 全链路 + 入口强校验；AioSandbox 收紧 | 6–10 周 |
| **2** | 增长期（100–500 付费 / 5k–20k 用户） | DeerFlow 表 RLS、KMS、ObjectStorage S3、内部 LLM 计费分类、付费分层 | 10–16 周 |
| **3** | 成熟期（1k+ 付费 / 50k+ 用户）**或** 出现安全/成本事故 | K8s sandbox + namespace、BYO key（付费档福利）、audit DB 拆分、prewarm 池 | 16–26 周 |
| **4** | 单客户合同驱动（合规 / 企业销售） | SSO、custom domain、per-tenant DB（仅强合规） | 按需，单客户 4–8 周 |

**核心原则**：每期只做下一档规模真正逼出来的事；做了就不回头的"不可逆决策"集中在 Stage 0/1，避免后期重写。

---

## Stage 0 — workspace 模型立起来 + auth 收紧

**触发**：你现在所在的位置——刚把 ADR 收敛完，准备开第一个付费客户。
**退出**：能给一个外部用户开账号，他登进来看到自己的 workspace、能创建 thread、隔离干净。
**时间盒**：3–4 周

### 必做

| 改动 | 说明 |
|---|---|
| **`workspaces` 表 + 自动建 1 人 workspace** | 每个新注册用户自动获得 1 个 workspace；用户 = workspace owner。这是后面所有租户改造的底座。 |
| **`workspace_id` 列加到现有 SQLite 表** | `threads_meta` / `runs` / `feedback` / `users` 加 `workspace_id`。**不可逆决策**——SQLite 上加列后再迁 Postgres 比直接在 Postgres 上加痛苦得多。 |
| **`workspace_memberships` 表** | 即使个人用户也是"1 个 owner 成员"，团队功能未启用但模型先就位。`role` 字段先只有 `owner`。 |
| **JWT 扩 `wid` 字段** | 沿用现有 `app/gateway/auth/jwt.py` `TokenPayload`（参 ADR-007 §8 修订版），加 `wid`（workspace_id），不引入 Better Auth。 |
| **入口路由 `(workspace_id, thread_id)` 校验** | `threads.py` + `thread_runs.py` 入口处必校验（参 ADR-001 §4.1.2）。SQLite 阶段就上，避免 Stage 1 临时补。 |
| **CLI / admin UI 的"workspace 管理"基础** | platform admin 能看 workspace 列表、暂停/删除某个 workspace（防止滥用第一时间反应）。 |
| **现有 auth 完善** | setup flow 能创建第一个 admin、邀请用户走基本流程（不必 invitation token，可手动建账号）。`token_version` 已存在，复用。 |

### 不做（推迟到 Stage 1+）

- ❌ Postgres 切换 — Stage 1
- ❌ RLS — Stage 2
- ❌ Quota 系统 — Stage 1（早一点也行，但有了第一个付费客户再做反应快）
- ❌ K8s sandbox — Stage 3
- ❌ KMS / ObjectStorage S3 — Stage 2
- ❌ 团队 invitation 流程 — Stage 2（先不做，反正还没真团队用户）
- ❌ Stripe 对接 — Stage 1

### 关键 PR 顺序（避免半截不可运行）

1. `workspaces` + `workspace_memberships` 表 + 仓储
2. 注册流程改造（自动建 1 人 workspace）+ JWT 扩 `wid`
3. 现有表 ALTER 加 `workspace_id` 列 + 数据回填脚本（"legacy_workspace"）
4. `threads.py` / `thread_runs.py` 入口校验 + 迁移所有现有 thread 到对应 workspace
5. CI boundary 测试：禁止任何路径绕过入口直连 LangGraph saver

### Go/No-Go 进入 Stage 1

- 第一个付费意向客户出现
- Stage 0 已部署到生产 ≥ 2 周，无 workspace 隔离 bug 报告
- 团队对 SQLite 性能瓶颈有共识（用户数破百时切 Postgres）

---

## Stage 1 — 第一批付费客户（freemium 真上线）

**触发**：Stage 0 跑稳 + 拿到第一批付费用户（10–50 付费 / 500–2000 free）。
**退出**：能放心让媒体/产品社区曝光，不会被白嫖跑偏。
**时间盒**：6–10 周

### 必做

| 改动 | 说明 | 关联 ADR |
|---|---|---|
| **Postgres 切换** | 老数据 `pg_loader` 导入；`workspace_id` 已就位（Stage 0 加过）。**不可逆**。 | ADR-001 §4.4 |
| **Quota 系统 v1**（强制） | `workspace_quotas` + `workspace_usage_daily` 表；`QuotaMiddleware` 在 lead_agent 链最前；硬限到达拒调用。**Freemium 不上 quota = 信用卡递给攻击者**。 | ADR-003 §4.4 |
| **`TokenUsageMiddleware` 持久化** | 当前只 log（参 audit ADR-003）；要写入 `workspace_usage_daily(workspace_id, date, model, tokens_in, tokens_out)`。 | ADR-003 §4.3 |
| **悲观预扣**（轻量版） | 按 `model_max_input_tokens` 估上限；幽灵 token 防御。 | ADR-003 §4.4.1 |
| **Stripe 对接（基础订阅）** | 单档付费先；webhook 同步到 `workspace_quotas.plan` 字段。 | — |
| **AioSandbox 出网收紧** | egress 白名单（默认禁出网，按需放行）+ cgroup CPU/memory 限额。**不上 K8s**——AioSandbox 加这两个补丁就能撑到 Stage 3。 | ADR-002（轻量版） |
| **Sandbox 资源 quota** | 每 workspace 的"沙箱 CPU 秒/月"也进 quota（防止白嫖跑挖矿）。 | ADR-003 §4.3 |
| **基础监控** | per-workspace token 用量曲线、quota 命中率、异常用量告警。 | — |

### 不做（推迟到 Stage 2+）

- ❌ RLS — Stage 2（应用层 + 入口校验先撑着）
- ❌ KMS — Stage 2（先用环境变量管理 key）
- ❌ ObjectStorage S3 — Stage 2（先继续本地文件 + 备份脚本）
- ❌ K8s sandbox — Stage 3
- ❌ BYO key — Stage 3（先全部用平台 key + quota）
- ❌ 团队 invitation 流程 — Stage 2（除非有团队客户先到）
- ❌ 多档付费 — Stage 2

### 关键 PR 顺序

1. Postgres 切换（dev 双驱动 → 生产灰度 → 全切）
2. `workspace_quotas` / `workspace_usage_daily` 表 + 仓储
3. `TokenUsageMiddleware` 升级为持久化（参 audit + ADR-003 §4.3）
4. `QuotaMiddleware` 加入中间件链
5. Stripe webhook + 订阅状态同步到 `workspace_quotas.plan`
6. AioSandbox egress 白名单 + 资源限额
7. 监控/告警接入

### Go/No-Go 进入 Stage 2

- 月活付费用户 ≥ 50 **或** 月活免费用户 ≥ 1000
- 出现一次"差点超额"事件（quota 在悲观预扣下还是漏了一次）
- 文件存储或 secret 管理出现一次手忙脚乱（备份遗漏 / key 误提交等）

---

## Stage 2 — 增长期，安全与隔离深化

**触发**：用户量级跳到下一档（100–500 付费 / 5k–20k 用户）。
**退出**：架构能撑住"用户翻 10 倍而不爆炸"，团队功能上线。
**时间盒**：10–16 周

### 必做

| 改动 | 说明 | 关联 ADR |
|---|---|---|
| **DeerFlow 表 RLS** | 仅 DeerFlow 自有表（threads_meta / runs / feedback / workspace_*）启用 RLS；testcontainers 必须先有。LangGraph 表继续走应用层强校验。 | ADR-001 §4.1.1（修订版）+ §4.2 |
| **Postgres testcontainers + RLS smoke 测试** | phase-0 §3.5 底座之一；CI 里跑 RLS 冒烟。 | phase-0 §3.5 |
| **ObjectStorage Protocol + Local + S3 实现** | 用户上传 / 产物 / 技能包迁到对象存储；按 `workspaces/{wid}/` prefix。 | ADR-005 §4 + §5 |
| **KMS 抽象 + AWS/阿里云 KMS 接入** | secret 加密落地；envelope encryption。dev 仍可明文 fallback。 | ADR-003 §4.1 |
| **多档付费分层** | Free / Pro / Team 三档；quota 按档次配；Stripe 多 product。 | — |
| **团队 workspace invitation 流程** | `invitations` 表 + 邀请链接 + 邮件；新成员 join workspace 后 bump 用户 `token_version`。 | ADR-004 §5 |
| **per-workspace MCP cache** | `mcp/cache.py` 模块单例 → `WorkspaceMCPCache` 类；OAuth token 落 KMS 加密 DB。 | ADR-006 §2.2 |
| **内部 LLM 计费分类** | Memory/Title/Summarization 三类 LLM 调用都计入 workspace 用量，区分 `usage_category`。 | ADR-006 §2.5 |
| **role 扩到 owner/admin/member** | 团队 workspace 出现 → RBAC 真正发挥作用；`@require_permission` 装饰器升级。 | ADR-004 §5.4 |
| **基础 audit log** | 写入业务 DB（暂不拆分），关键操作（quota 改、role 改、删 workspace）记录。 | — |

### 不做（推迟到 Stage 3+）

- ❌ K8s sandbox — Stage 3
- ❌ BYO key — Stage 3
- ❌ audit DB 拆分 — Stage 3
- ❌ prewarm pool — Stage 3
- ❌ SSO / custom domain — Stage 4

### 关键 PR 顺序

1. testcontainers + Postgres CI 跑通
2. ObjectStorage Protocol + Local 实现 + 端到端打通（用户上传走对象存储）
3. KMS 抽象 + AWS KMS 实现 + 已有 secret 灰度迁移
4. DeerFlow 表 RLS（testcontainers 验证后上生产）
5. S3 实现 + 上传/产物迁移
6. 多档付费 + invitation 流程 + role 扩展（这块可并行）
7. WorkspaceMCPCache + OAuth token 加密
8. 内部 LLM 计费分类

### Go/No-Go 进入 Stage 3

- 月活付费 ≥ 500 **或** 月活总用户 ≥ 20k
- 单一 sandbox 进程出现资源争用（一个 workspace 卡死影响其他）
- 出现一次跨 workspace 数据访问尝试（哪怕只是日志里看到）
- 客户开始问"我能不能用我自己的 OpenAI key"

---

## Stage 3 — 成熟期，K8s 隔离 + BYO

**触发**：Stage 2 出口条件中任意一条。
**退出**：架构能撑到 enterprise 销售前夕。
**时间盒**：16–26 周

### 必做

| 改动 | 说明 | 关联 ADR |
|---|---|---|
| **K8s namespace + NetworkPolicy** | per-workspace namespace + 默认禁出网；`K8sSandboxProvider` 全新建。 | ADR-002 §1 + §5 |
| **prewarm pool** | per-workspace 池，按 plan 大小（Free=0、Pro=1、Team=3）。 | ADR-006 §2.4 |
| **BYO LLM key（付费档福利）** | Pro/Team 用户可填自己的 OpenAI/Anthropic key；BYO 时不走 quota。 | ADR-003 §4.6 |
| **S3 实现已就位**（Stage 2 已建）→ 加 presigned URL + lifecycle | — | ADR-005 §5 |
| **audit DB 拆分** | 独立连接池或独立实例；关键操作 + 沙箱审计独立写。 | ADR-006 §2.4（脚注） |
| **跨 region 备份 / DR** | 至少 1 个备份 region + RTO ≤ 4h。 | — |
| **完整监控栈** | Grafana + Prometheus + APM；per-workspace SLA 跟踪。 | — |

### 不做（推迟到 Stage 4）

- ❌ gVisor / Kata（K8s + NetworkPolicy 已经足够，gVisor 是 nice-to-have）
- ❌ SSO
- ❌ custom domain
- ❌ per-tenant DB

### Go/No-Go 进入 Stage 4

- 拿到第一个 enterprise 合同（合同写明 SSO / 合规要求 / 自定义域名）
- 拿到金融/医疗/政府类客户

---

## Stage 4 — 企业化，按需开启

**触发**：单客户合同驱动，不做"为了万一"的提前准备。
**退出**：— 长期持续。
**时间盒**：每个 enterprise 客户 4–8 周

### 按客户需求选做

| 客户要 | 你做 | 关联 ADR |
|---|---|---|
| SSO（SAML/OIDC） | 接入 Auth.js / 自实现 SAML provider；`workspace_memberships` 与外部 group 映射 | ADR-007 §8 SSO 段 |
| 自定义域名 | `workspaces.custom_domain` 列；ACME 动态签证；nginx vhost 路由 | ADR-007 §9 |
| 物理数据隔离（合规） | per-tenant DB（仅该客户）+ 独立连接池 | ADR-001 §6 推翻条件 |
| 私有部署（self-host）| 打 enterprise tier docker 镜像 + 部署文档；放弃中心化运维优势 | — |
| gVisor / Kata 运行时 | K8s 切 gVisor RuntimeClass | ADR-002 §5 |
| 合规审计报告（SOC2/ISO） | 强化 audit log 留存 + 评估机构对接 | — |

---

## 不可逆决策一览（重点关注）

| 决策 | 在哪 Stage 做 | 做错了的代价 |
|---|---|---|
| **`workspace_id` 列加到所有业务表** | Stage 0 | 漏了某张表 → Stage 1 还在补；SQLite 加列后再迁 Postgres 痛苦 |
| **`workspaces` 表设计**（slug、plan、status 字段） | Stage 0 | 后期改 schema 要写迁移；用户 URL 全变 |
| **JWT payload 字段** | Stage 0 + Stage 2 | 加字段时旧 cookie 失效；一次性想清楚 `wid/role/plan` 都加上，少一次 churn |
| **Postgres 切换** | Stage 1 | 老数据迁移；切完不回头 |
| **ObjectStorage prefix 形态**（`workspaces/{wid}/...`） | Stage 2 | 改了所有用户产物 URL 失效 |
| **K8s namespace 命名规则**（`ws-{wid}` 还是 `tenant-{wid}`） | Stage 3 | 改了所有 NetworkPolicy / RBAC |

**建议**：Stage 0 的 `workspaces` 表 schema、URL slug 形态、JWT 字段集 这三件事**多设计一周也不亏**——后面不可逆，错了重写代价大。

---

## 与 ADR 的对应关系

每个 Stage 对应 ADR 子集，**不是全部 ADR 一起做**：

| Stage | 启用的 ADR 章节 |
|---|---|
| 0 | ADR-001 §4.1.2（入口校验，SQLite 版）+ ADR-004 §1-§3（owner-only 简化版）+ ADR-007 §1-§5（无 slug 路由可暂缓） |
| 1 | ADR-001 §4.1（应用层校验完整版，**不含 RLS**）+ ADR-002 §3（AioSandbox 补丁版）+ ADR-003 §4.3-§4.4（quota + 持久化）+ ADR-007 §6-§8（slug 路由 + JWT 扩字段） |
| 2 | ADR-001 §4.2（DeerFlow 表 RLS）+ ADR-003 §4.1-§4.2（KMS + create_chat_model 改造）+ ADR-004 §5（完整 RBAC）+ ADR-005 §1-§5（ObjectStorage 完整）+ ADR-006 §2.2 + §2.5 + §2.6 |
| 3 | ADR-002 §1-§5（K8s 完整）+ ADR-003 §4.6（BYO）+ ADR-006 §2.4（prewarm 池）+ ADR-007 §9（custom domain 预留） |
| 4 | ADR-001 §6（per-tenant DB）+ ADR-002 §5（gVisor）+ ADR-007 §8 SSO 段 + §9（custom domain 落地） |

---

## 工时预估汇总

| Stage | 触发 | 时间盒 | 累计 |
|---|---|---|---|
| 0 | 现在 | 3–4 周 | 1 个月 |
| 1 | 首批付费 | 6–10 周 | 4 个月 |
| 2 | 增长期 | 10–16 周 | 8 个月 |
| 3 | 成熟期 | 16–26 周 | 14 个月 |
| 4 | 企业客户 | 单客户 4–8 周 | + 按需 |

**全功能落地**：~14 个月（Stage 0–3 累计），不含 Stage 4 enterprise 特性。
**最小可付费**（Stage 0 + 1）：~4 个月。
**风险可控的增长**（Stage 0 + 1 + 2）：~8 个月。

---

## 推翻条件

整个分期方案在以下情况下要重排：

- **目标客户画像突变**：比如拿到一个 enterprise 合同要求 SSO + custom domain → Stage 4 部分提前到 Stage 2
- **出现安全事故**：跨 workspace 泄露 / sandbox 逃逸 → 立即跳过未启动的 Stage，直接做 Stage 3 的 K8s + RLS
- **付费转化远不及预期**：Stage 1 上线 6 个月付费用户 < 10 → 重新评估 freemium 模型，可能不需要走完 Stage 2/3
- **LLM 价格大跌 / 自部署模型成熟**：成本控制优先级下降 → quota 与 BYO 可以简化
