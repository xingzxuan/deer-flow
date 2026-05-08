# 多租户改造 · 第 0 阶段计划

> 目标：在写第一行代码前，对齐设计、产出可评审的文档。这一阶段不动代码，时间盒 **两周封顶**。

---

## 产出物清单（8 份文档 + 1 份代码盘点）

| 产出物 | 形式 | 谁批 | 作用 |
|---|---|---|---|
| [ADR-001 数据隔离模型](./adr-001-data-isolation.zh-CN.md) | ADR（决策记录） | CTO / 架构 | 锁定行级 / schema / 库级；含 LangGraph checkpoint 表的注入路径 |
| [ADR-002 沙箱隔离模型](./adr-002-sandbox-isolation.zh-CN.md) | ADR + 威胁模型 | 安全 + 架构 | 锁定 K8s / Firecracker / VM |
| [ADR-003 LLM Key 与计费模型](./adr-003-llm-key-billing.zh-CN.md) | ADR | 产品 + CTO | 锁定 BYO / 平台付费 / 混合；含悲观预扣 + 内部 LLM 计费 |
| [ADR-004 租户 ↔ 用户层级](./adr-004-tenant-rbac.zh-CN.md) | ADR | 产品 | 锁定二级 RBAC + JWT/cache 一致性策略 |
| [ADR-005 存储拓扑](./adr-005-storage-topology.zh-CN.md) | ADR + ObjectStorage 接口签名 | 架构 + SRE | 锁定 DB / 对象存储 / 临时区分层 |
| [ADR-006 运行时与渠道租户化](./adr-006-runtime-channel-tenancy.zh-CN.md) | ADR | 后端 lead + 渠道 owner | 锁定 checkpointer / MCP cache / skills loader / 内部 LLM 计费 / IM 渠道 ↔ 租户 |
| [ADR-007 路由与前端租户化](./adr-007-routing-frontend.zh-CN.md) | ADR | 前端 lead + 后端 lead | 锁定 URL 形态 / cookie / Better Auth / SDK 切换 |
| Tenant 数据模型设计 | DB schema 草稿 + ER 图 | 后端 lead | 第 1 阶段直接落地用 |
| 多租户改造代码盘点 | 表格 / spreadsheet | 后端 lead | 估工 + 拆 PR 用 |

每份 ADR 用统一结构：**目标客户画像 → 评估维度 → 选项对比 → 选 X 的理由 → 推翻条件**。

---

## 1. 四个决策怎么定（决策框架）

> 下面只是决策框架的 1 页概览。每份 ADR 的完整正文（背景 / 备选方案 / 落地影响 / 风险 / 推翻条件 / 默认假设）已分别成独立文档：
> - [ADR-001 数据隔离模型](./adr-001-data-isolation.zh-CN.md)
> - [ADR-002 沙箱隔离模型](./adr-002-sandbox-isolation.zh-CN.md)
> - [ADR-003 LLM Key 与计费模型](./adr-003-llm-key-billing.zh-CN.md)
> - [ADR-004 租户 ↔ 用户层级](./adr-004-tenant-rbac.zh-CN.md)
> - [ADR-005 存储拓扑与持久化策略](./adr-005-storage-topology.zh-CN.md)
> - [ADR-006 运行时与渠道租户化](./adr-006-runtime-channel-tenancy.zh-CN.md)
> - [ADR-007 路由与前端租户化](./adr-007-routing-frontend.zh-CN.md)

### ADR-001 数据隔离

| 维度 | 行级 (tenant_id WHERE) | per-tenant schema | per-tenant DB |
|---|---|---|---|
| 实现成本 | 低 | 中 | 高 |
| 跨租户 bug 爆炸半径 | 高 | 中 | 极低 |
| 备份/恢复粒度 | 全量 | 按 schema | 按 DB |
| 合规友好度（SOC2/HIPAA） | 一般 | 好 | 最好 |
| 跨租户分析查询 | 容易 | 中 | 难 |
| 适用客户规模 | <10k 租户 | 10k–100 大客户 | <100 大客户 |

**90% 团队选 行级 + Postgres RLS（双保险）**。理由：DeerFlow 现在的仓储层已经是 `user_id` 过滤模式，RLS 加上去几乎是平行扩展。

**推翻条件**：拿到金融/医疗类客户、客户合同里写明"物理数据隔离"——直接跳到 per-tenant DB。

### ADR-002 沙箱隔离

威胁模型表（每行一个攻击场景）：

| 攻击 | 共享 Docker | per-tenant Namespace | per-tenant VM |
|---|---|---|---|
| 容器逃逸 | 全员沦陷 | 单租户沦陷 | 单租户沦陷 |
| 侧信道（CPU 缓存等） | 可行 | 可行 | 难 |
| 出网到云 metadata | 可行（必须默认禁） | 可行（必须默认禁） | 可行（必须默认禁） |
| 资源耗尽（fork bomb） | 影响他人 | 仅影响自己 | 仅影响自己 |
| 提权 | 看 K8s 配置 | 看 K8s 配置 | VM 边界更强 |

**推荐：K8s namespace + gVisor/Kata runtime + NetworkPolicy 默认禁出网**。Firecracker 是更强的方案但运维成本翻倍，留给"premium 租户专属"档位。

### ADR-003 Key & 计费

三种模式选一种主线：

- **BYO key**：客户自己带 OpenAI/Anthropic key。优点：你不背模型成本、不背滥用；缺点：客户体验差，需要 secret vault。
- **平台付费**：你统一付，按 token 加价转售给客户。优点：体验顺；缺点：你要做精细的 quota+成本归账，否则会被刷爆。
- **混合**（推荐起步）：默认平台 key + 限额；高级套餐切 BYO key 不限额。

**写 ADR 时要把 "成本归因路径" 画清楚**：哪个表记 `tenant_id × model × token`，谁算月度账单，怎么和 Stripe 对账。

### ADR-004 租户内层级

最常见的两种：

- **扁平**：tenant 直接装 user，所有 user 等价。简单，适合自助型 SaaS。
- **二级 RBAC**：tenant 有 owner/admin/member，admin 能管 member 的 skill 安装权限和 quota 分配。适合企业销售。

如果要 SSO（SAML/OIDC），那默认要二级 RBAC——因为客户 IT 部门要能管理"哪些员工进哪些 workspace"。

### ADR-005 存储拓扑

详见独立文档：[ADR-005 · 存储拓扑与持久化策略](./adr-005-storage-topology.zh-CN.md)。

### ADR-006 运行时与渠道租户化

详见独立文档：[ADR-006 · 运行时与渠道层的租户化](./adr-006-runtime-channel-tenancy.zh-CN.md)。

要点：

- **LangGraph checkpointer**：保留原表结构，靠 `thread_id ∈ threads_meta(tenant_id=...)` 子查询 RLS 兜底；连接注入 `SET LOCAL app.tenant_id` 走自定义 connection factory。
- **MCP 工具缓存**：模块级单例 → per-tenant LRU；OAuth token 从文件挪到 `tenant_secrets`。
- **Skills loader**：拆 platform 共享只读 + tenant 私有；按 `tenant_skill_state.enabled` 过滤工具。
- **Sandbox provider**：实例单例，`acquire(thread_id)` 内按 tenant 路由到 K8s namespace；prewarm 池按 plan 大小。
- **内部 LLM 计费**：Memory / Title / Summarization 调用都算 tenant 用量，分 `usage_category` 报表展示。
- **IM 渠道**：每个 binding 加 `tenant_id`，webhook handler 强制注入 tenant ContextVar；ghost user 7 天未链接自动停。

### ADR-007 路由与前端租户化

详见独立文档：[ADR-007 · URL 路由与前端租户化](./adr-007-routing-frontend.zh-CN.md)。

要点：

- **URL 形态**：`/{slug}/...`，子域名留给 v2 自定义域名。
- **Cookie**：`Path=/` + JWT 内 `tid`；切换 tenant 重签 JWT + 硬刷新。
- **前端**：`app/(tenant)/[slug]/layout.tsx` 注入 `TenantProvider`；SDK 实例单例但调用读 `useTenant()`；切换时 `cancelAllStreams + window.location.assign`。
- **Better Auth**：登录后跳 picker / 直进 / onboarding；session 注入 `{tid, role, tv}`。

要点：

- **现状问题**：自定义 skill / agent / memory / 上传 / 产物全部在容器本地文件系统。多副本不一致、容器重建丢数据、无备份。
- **决策**：三层拓扑——**结构化进 Postgres**（agent SOUL/config、memory facts、skill enabled、tenant secrets）+ **大对象进对象存储**（上传、产物、技能包）+ **临时区**（沙箱 workspace，不持久化）。
- **接口**：`ObjectStorage` Protocol，实现 `LocalObjectStorage`（dev）/ `S3ObjectStorage`（prod）/ `MinIOObjectStorage`（自部署）。
- **关键约束**：单 bucket + `tenants/{tid}/` prefix；presigned URL 短 TTL；HTML/SVG 强制 `Content-Disposition: attachment`（保留当前 XSS 防护）。
- **拒绝方案**：① 全部进 PVC（小文件读写差、备份难）；② 全部进 S3（强一致差、列表慢、无事务）。

---

## 2. Tenant 数据模型草稿

第 0 阶段就把这张表画出来，第 1 阶段直接落地：

```sql
-- 新表
tenants (
  id UUID PK,
  slug VARCHAR(64) UNIQUE,         -- URL 用，/t/{slug}/...
  display_name VARCHAR(128),
  plan VARCHAR(32),                -- free / pro / enterprise
  status VARCHAR(16),              -- active / suspended / deleted
  created_at, updated_at
)

tenant_memberships (
  tenant_id UUID FK,
  user_id UUID FK,
  role VARCHAR(16),                -- owner / admin / member
  invited_by UUID,
  joined_at,
  PRIMARY KEY (tenant_id, user_id)
)

tenant_secrets (
  tenant_id UUID FK,
  key VARCHAR(64),                 -- e.g. OPENAI_API_KEY
  encrypted_value BYTEA,           -- KMS 加密
  rotated_at,
  PRIMARY KEY (tenant_id, key)
)

tenant_quotas (
  tenant_id UUID FK,
  metric VARCHAR(32),              -- tokens_monthly / runs_concurrent / sandbox_cpu_seconds
  hard_limit BIGINT,
  soft_limit BIGINT,
  PRIMARY KEY (tenant_id, metric)
)

tenant_usage_daily (
  tenant_id UUID,
  date DATE,
  metric VARCHAR(32),
  value BIGINT,
  PRIMARY KEY (tenant_id, date, metric)
)

invitations (
  id UUID PK,
  tenant_id UUID FK,
  email VARCHAR(320),
  role VARCHAR(16),
  token VARCHAR(64) UNIQUE,
  expires_at,
  used_at NULL
)

-- 已有表加列
users  +  default_tenant_id UUID
threads_meta  +  tenant_id UUID  + INDEX (tenant_id, user_id, updated_at)
runs          +  tenant_id UUID  + INDEX (tenant_id, created_at)
run_events    +  tenant_id UUID
feedback      +  tenant_id UUID

-- 未来 Tier 2 还要加：
skills_state  (tenant_id, skill_name, enabled, source)
agent_configs (tenant_id, user_id, agent_name, soul_md, config_yaml)
mcp_configs   (tenant_id, server_name, transport, url, encrypted_config)
```

画完后让 DBA / 后端 lead 评审两件事：**索引覆盖**（每个查询 path 是否走索引）和 **RLS policy 草稿**（每张带 tenant_id 的表写一条 policy）。

---

## 3. 多租户改造代码盘点

第 0 阶段最容易被忽略的是**先量一下工作量**。在 spreadsheet 里把"目前涉及 user_id / 全局状态"的代码点全部列出来：

```bash
grep -rn "user_id\|get_effective_user_id\|DEFAULT_USER_ID" backend/packages/harness/deerflow/ backend/app/
grep -rn "users/\|/.deer-flow/" backend/  scripts/
grep -rn "extensions_config\|skills/public\|skills/custom" backend/
```

把命中点分成五类：

| 类别 | 改造动作 | 估计点位 |
|---|---|---|
| **DB 仓储**（`persistence/*/sql.py`） | 增加 tenant_id 解析与 WHERE | ~10–15 处 |
| **文件系统路径**（`ThreadDataMiddleware`、memory storage、agents 存储） | 路径加 tenant 维度 | ~5–8 处 |
| **配置/Secret 读取**（`models/factory.py`、MCP client、community tools） | 改成 tenant 上下文取 key | ~8–12 处 |
| **路由 handler**（`app/gateway/routers/*.py`） | 加 `@require_permission` + tenant 上下文 | ~14 个 router 文件 |
| **全局单例**（沙箱 provider、MCP cache、skills loader） | 缓存 key 加 tenant 维度 | ~5 处 |

每条点位估一个 S/M/L 工作量。这张表是后面拆 PR、估工期、估钱的依据。

---

## 4. 第 0 阶段的"完成定义" (DoD)

走完这阶段，团队应该能回答：

- [ ] 数据存哪、用什么数据库、怎么隔离 → ADR-001 给出
- [ ] 客户的 bash/工具跑在哪、能访问什么、爆炸半径多大 → ADR-002 给出
- [ ] 客户的 LLM 调用钱谁出、怎么算 → ADR-003 给出
- [ ] 客户内部能不能自己加员工、怎么加 → ADR-004 给出
- [ ] Skill / agent / 上传 / 产物 / memory 各自存哪、丢失怎么办 → ADR-005 给出
- [ ] LangGraph / MCP / 内部 LLM / IM 渠道这些"夹层"怎么按租户隔离 → ADR-006 给出
- [ ] 浏览器地址栏长什么样、Cookie 怎么 scope、租户切换怎么走 → ADR-007 给出
- [ ] 第一阶段 PR 怎么拆、估几人周 → 代码盘点给出
- [ ] 第一个内测客户长什么样、什么时候能上 → 项目经理排期

---

## 5. 时间盒与节奏

第 0 阶段 **两周封顶**，再长就是过度设计。

- **第 1 周**：写 ADR-001/002/003/004 草稿，团队读、challenge、收敛
- **第 2 周**：定 schema、做代码盘点、估工、定第一阶段范围与 design partner 客户

如果两周后还有 ADR 定不下来，**绝大多数情况是因为缺一个真实客户做参照**——这时候应该先去签一个 design partner（哪怕免费），用他们的合同和合规要求来反推决策。

---

## 6. 默认假设（如无特殊情况按此推进）

为避免决策瘫痪，先写下一个"默认值"，所有 ADR 在没有相反证据前按这个走：

| 决策 | 默认值 | 选它的理由 |
|---|---|---|
| 数据隔离 | 行级 + Postgres RLS（含 LangGraph 表 subquery RLS） | 改造成本低，DeerFlow 现状几乎平行扩展 |
| 沙箱隔离 | K8s namespace + gVisor + NetworkPolicy 默认禁出网 | 强度足够 + 运维可控 |
| LLM Key | 混合：默认平台 key + 限额，premium 切 BYO；悲观预扣防超额 | 体验与成本兼顾 |
| 租户层级 | 二级 RBAC（owner/admin/member）+ JWT/cache 双层 | 为 SSO 和企业销售留口 |
| 存储拓扑 | Postgres（结构化）+ S3 兼容对象存储（大对象）+ emptyDir（临时） | 沙箱 pod 真正无状态；备份/灾备/横向扩展直接通 |
| 运行时夹层 | per-tenant MCP cache + skills 拆双路 + 内部 LLM 计费分类 + IM binding 加 tenant | 关上"非仓储非沙箱"那一组进程级单例的隔离漏洞 |
| 前端路由 | `/{slug}/...` 路径 + JWT 内 tid + 硬刷新切换 | UX 简单，与 ADR-001/004 cookie 模型契合 |

> 这是"中等强度方案"，覆盖 90% B2B SaaS。如果客户画像偏极端（大企业 / 强合规 / 自助小客户），再调整。
