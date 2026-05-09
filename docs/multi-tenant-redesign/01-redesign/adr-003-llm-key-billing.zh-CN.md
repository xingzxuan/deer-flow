# ADR-003 · LLM Key 与计费模型

| 项目 | 内容 |
|---|---|
| 状态 | 草稿（Draft） · 2026-05-09 据审计修订 §4.2 / §4.3 / §4.4.2（`create_chat_model` 是 sync、`TokenUsageMiddleware` 不持久化） |
| 决策日期 | TBD |
| 决策者 | 产品 + CTO + 财务 |
| 关联 ADR | ADR-001 数据隔离、ADR-005 存储拓扑、ADR-006 运行时与渠道 |
| 关联审计 | [adr-vs-code-audit](./adr-vs-code-audit.zh-CN.md) |

---

## 1. 背景

DeerFlow 当前的 LLM 配置是**进程级全局**：`config.yaml` 写 `api_key: $OPENAI_API_KEY`，环境变量在容器启动时注入，所有用户共用同一个 key。`models/factory.py` 在 `create_chat_model()` 时做反射 + 环境变量替换。

多租户场景下，这套模式有四个问题：

1. **成本归因不清**：所有租户的 token 消费混在同一个 key 上，月底无法精确按租户计费
2. **滥用风险大**：一个租户写循环 prompt 把 key 刷爆，所有租户一起被拉黑
3. **客户合规问题**：部分企业要求"我的数据只能走我的 LLM 账户"（数据驻留 / 审计闭环）
4. **MCP / 第三方 API key 同样问题**：Tavily / Firecrawl / Jina 的 key 也是全局的

需要决定：**LLM Key 由谁出？怎么算账？**

---

## 2. 决策

**采用混合模式（Hybrid BYO）**：

- **Free / Pro 套餐**：默认走 **平台 key**，按月配额限制（token / 并发 / 沙箱 CPU 秒）
- **Enterprise / BYO 套餐**：客户上传自己的 LLM key（OpenAI / Anthropic / Azure / Bedrock），不限平台 quota，按"管理费 + 沙箱用量"收费

理由：

1. **降低小客户上手摩擦**——他们不想注册 OpenAI 账号、不想填信用卡，平台 key 体验最顺
2. **大客户控制权要求**——他们已经有 OpenAI / Anthropic 企业合同（折扣 / 数据条款），希望复用
3. **平台风险可控**——平台 key 有 quota 兜底；BYO key 客户自己滥用自己的额度
4. **现有 `models/factory.py` 改造可控**——加一层 tenant 上下文 + secret vault 即可

---

## 3. 备选方案与拒绝理由

### A. 纯平台 key（所有租户共用，按 token 加价转售）

**拒绝。** 看似简单实则雷区遍地：

- 大客户必拒——数据合规审查过不去（数据流过你的 key 等于过你的账户）
- 一旦 OpenAI 临时封号 / rate limit，全员宕机
- 加价定价天花板低（客户自己注册也能买，溢价空间小）
- 你要替每个客户做信用评估和反滥用，运维成本飙升

### B. 纯 BYO key（强制客户自带）

**拒绝。** 与早期增长冲突：

- Onboarding 多一步"去注册 OpenAI"——转化率显著下降
- 试用客户体验差（"还没用就要填卡"）
- 小客户会嫌烦直接放弃

### C. 平台 key + 严苛 quota（不开 BYO）

**部分采纳，作为 Free/Pro 默认。** 但不开 BYO 会卡企业客户，不能作为唯一选项。

---

## 4. 落地影响

### 4.1 Tenant Secrets 表（ADR-005 已含）

```sql
tenant_secrets (
  tenant_id   UUID FK,
  key         VARCHAR(64),         -- 'OPENAI_API_KEY' / 'ANTHROPIC_API_KEY' / 'TAVILY_API_KEY' / ...
  encrypted_value BYTEA,           -- KMS 加密
  rotated_at  TIMESTAMP,
  created_at  TIMESTAMP,
  PRIMARY KEY (tenant_id, key)
)
```

加密策略：每个租户一个 KMS data key（DEK），KEK 在 AWS KMS / 阿里云 KMS 集中管。

### 4.2 改造 `create_chat_model()`

当前签名（伪）：

```python
def create_chat_model(name: str = None, *, thinking_enabled: bool, app_config: AppConfig = None) -> BaseChatModel:
    model_config = app_config.get_model_config(name)
    api_key = resolve_env_var(model_config.api_key)   # 从 process env 取
    return reflect(model_config.use)(api_key=api_key, ...)
```

改造后：

```python
async def create_chat_model(
    name: str = None,
    *,
    thinking_enabled: bool,
    tenant_id: str = AUTO,             # 从 ContextVar 取
    app_config: AppConfig = None,
) -> BaseChatModel:
    tenant = resolve_tenant_id(tenant_id, method_name="create_chat_model")
    model_config = app_config.get_model_config(name)

    # 优先级：tenant 自带 key > 平台 key（带 quota）
    api_key = await secret_vault.get(tenant, model_config.api_key_secret_name)
    if api_key is None:
        api_key = await platform_keys.get(model_config.api_key_secret_name)
        # 走平台 key 的 LLM 调用必须挂 QuotaMiddleware（见 4.4）

    return reflect(model_config.use)(api_key=api_key, ...)
```

> **sync → async 的连带影响**：当前 `create_chat_model` 是同步函数（`models/factory.py:50`）。改成 async 后所有调用点（lead_agent factory、`MemoryMiddleware` / `TitleMiddleware` / `SummarizationMiddleware` 等）都要同步改 await——这是一次跨多个文件的改动，不是单点 patch。phase-1 实现时按"factory 改 async + 一次性扫所有调用点 await"作为单个 PR 落地，不要分批，避免中间态不可运行。

### 4.3 Quota 表与 Usage 表（ADR-005 已含）

```sql
tenant_quotas (
  tenant_id  UUID,
  metric     VARCHAR(32),    -- tokens_monthly / runs_concurrent / sandbox_cpu_seconds_daily
  hard_limit BIGINT,         -- 超过即拒绝
  soft_limit BIGINT,         -- 超过即告警 / 降速
  PRIMARY KEY (tenant_id, metric)
)

tenant_usage_daily (
  tenant_id  UUID,
  date       DATE,
  metric     VARCHAR(32),
  model_name VARCHAR(64),    -- 区分 OpenAI / Anthropic / 本地 vLLM
  value      BIGINT,
  PRIMARY KEY (tenant_id, date, metric, model_name)
)
```

写入时机：

- token：**当前 `TokenUsageMiddleware` 只 log 不持久化**（`agents/middlewares/token_usage_middleware.py:268-275`）；本 ADR 要求新增持久化路径，按 (tenant_id, model, date) 累加，并配合 `usage_category` 区分主对话 / 内部任务（参 ADR-006 §2.5）
- 沙箱 CPU 秒：K8s metrics-server / Prometheus 抓取，每 5 min 聚合写入
- 并发 runs：`RunManager` 启动/结束时增减计数器

### 4.4 新增 `QuotaMiddleware`

放在 lead_agent 中间件链最前（在 ThreadDataMiddleware 之后、SandboxMiddleware 之前），LLM 调用前检查：

```python
class QuotaMiddleware(AgentMiddleware):
    async def before_model(self, state, runtime):
        tenant = get_current_tenant()
        usage = await usage_repo.current_month(tenant.id, "tokens_monthly")
        quota = await quota_repo.get(tenant.id, "tokens_monthly")

        if quota.hard_limit and usage >= quota.hard_limit:
            raise QuotaExceeded(
                "Monthly token quota exhausted. Upgrade plan or wait for reset.",
                next_reset=first_day_of_next_month(),
            )
        if quota.soft_limit and usage >= quota.soft_limit:
            # 软限：仍允许调用，但触发告警 + 在 UI 显示警告
            await alert_soft_limit(tenant.id)
```

`QuotaExceeded` 通过 `LLMErrorHandlingMiddleware` 转成 user-facing 错误（不是 500），保持一致性。

#### 4.4.1 悲观预扣 vs 事后结算（"幽灵 token"问题）

`before_model` 只能看到"调用前累计用量"，但 token 消耗是 `after_model` 才知道的——硬限到达时**最后一次调用一定超额**（典型可超 32k–200k token，单次可达 quota 的 1-5%）。处理：

| 阶段 | 动作 |
|---|---|
| `before_model` | **悲观预扣**：按 `model_max_input_tokens` 估上限（按 model 配置查表），累加到 "reserved" 列。reserved + actual ≥ hard_limit 时拒绝 |
| `after_model`（非流式） | 拿到真实 token，把对应 reservation 从 reserved 移到 actual，差额返还 |
| `after_model`（流式） | 流尾 `usage` event 到达时同上；流被 cancel 时按已收到的增量扣，剩余 reservation 释放 |

```sql
tenant_usage_daily (
  ...,
  metric VARCHAR(32),     -- tokens_input / tokens_output / tokens_reserved
  value BIGINT,
  ...
)
```

**reserved 不进账单**，只用于 quota gate。月底对账只看 `tokens_input` + `tokens_output`。

#### 4.4.2 流式 token 的提交时机

LangGraph SDK 的 `messages-tuple` 流模式按 chunk 推 delta。当前 `TokenUsageMiddleware` 在 `after_model` 一次性 **log**（不持久化）——多租户加上持久化后，这有两个隐患：

1. **客户端 abort 流时漏记**：用户关浏览器、SSE 断开 → middleware 没收到 `after_model` → token 漏算
2. **provider 本身的 usage 帧晚到**：OpenAI/Anthropic 把 usage 放在最后一个 chunk；StreamBridge 必须在 finalizing 时强制等这一帧

强约束：

- StreamBridge 收到 abort/disconnect 信号时，**仍需等 LLM provider 流自然结束** + 把 usage 提交后再断 client（限超时 5s 兜底）
- 提交时机：`after_model` 终态（成功/失败/abort）三选一时立刻 commit；不允许"等会话结束再批量"
- 流式调用的 `usage_category`（见 ADR-006 §2.5）按触发中间件分类
- 测试覆盖：`test_billing_stream_abort.py` 模拟客户端断连，断言 token 仍被记录

#### 4.4.3 内部 LLM 调用的归属

详见 ADR-006 §2.5。简表如下：

| 触发 | 计费归属 | usage_category |
|---|---|---|
| 主对话 | tenant | `main` |
| MemoryMiddleware 抽取 | tenant | `memory` |
| TitleMiddleware 起标题 | tenant | `title` |
| SummarizationMiddleware 历史压缩 | tenant | `summarization` |
| 平台 admin 主动 LLM 工具（健康检查等） | platform | `platform` |

`tenant_usage_daily` schema 在 §4.3 基础上补 `usage_category` 列。报表 UI 展示这五类分项，避免"为什么我没说话也产生 token"这类客户投诉。

### 4.5 计费对账

平台 key 模式下，"实际成本"和"客户账单"要分清：

| 维度 | 数据来源 | 用途 |
|---|---|---|
| **OpenAI 实际账单** | OpenAI API usage report（每日拉） | 与平台财务对账 |
| **客户应付** | `tenant_usage_daily`（你自己记的） | 月底生成账单 |
| **差额** | OpenAI 实际 - sum(客户应付) | 监控异常（>5% 触发审计） |

差额监控很重要——如果你少记了 token（比如 streaming 异常时漏记），平台会替客户埋单。每月对账。

### 4.6 BYO key 验证流程

客户填 key 时：

1. 加密前先做一次 test call（小 prompt，验证 key 有效）
2. 通过后加密入库
3. UI 显示"已配置"但永远不回显原始 key（防泄露）
4. 提供轮换流程（rotate）和撤销流程（revoke）

---

## 5. 套餐建议（产品决策，仅参考）

| 套餐 | LLM Key | 月度 token quota | 并发 runs | 沙箱 CPU 秒/月 | 价格 |
|---|---|---|---|---|---|
| **Free** | 平台 key | 100k | 1 | 1k | $0 |
| **Pro** | 平台 key | 5M | 5 | 50k | $X |
| **Team** | 平台 key（按 token 转售） | 50M | 20 | 500k | $XX |
| **Enterprise BYO** | 客户自带 | 不限 | 协商 | 协商 | $XXX 管理费 + 沙箱用量 |

具体数字由 PMM 和财务定，不在本 ADR 范围。

---

## 6. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 平台 key 被某租户刷爆 | QuotaMiddleware 硬限 + 异常用量告警（>3σ）+ 单 run token 上限 |
| 平台 key 被 OpenAI 临时封禁 | 多备份 key 轮询（多 tier API key）+ 多 provider 兜底（OpenAI 挂了切 Anthropic） |
| BYO key 在 DB 泄露 | KMS 加密 + 审计每次 decrypt + 仅在沙箱 pod 启动时 inject 进环境，不出 pod |
| 客户 BYO key 滥用导致他自己被 OpenAI 封 | 不归我们管（合同里写明） |
| 计费错算（少记 token） | 月度对账 + 5% 阈值告警 + 流式调用结束时强制 commit |
| 客户跨币种 / 退款 | 接 Stripe 完整闭环，不要自己手搓 |

---

## 7. 推翻条件

- **退回纯平台 key**：如果 BYO 流量 < 5%，可考虑下线 BYO 简化运维（但企业客户已签合同的不能强制迁回）
- **强制 BYO**：如果平台 key 滥用/欺诈损失年化 > $X，关掉免费档
- **per-tenant 物理 LLM 资源**：如果监管客户要求"专属推理实例"，需要专用 vLLM / Bedrock provisioned throughput

---

## 8. 默认假设

| 项 | 默认 |
|---|---|
| Secret 加密 | 信封加密：DEK（per-tenant）+ KEK（KMS） |
| Quota 维度 | tokens_monthly + runs_concurrent + sandbox_cpu_seconds_daily |
| Quota 重置 | 月度 token 按 UTC 月初；并发是实时；沙箱秒按 UTC 日初 |
| 软限：硬限比例 | soft = 0.8 × hard |
| BYO 支持的 provider | OpenAI / Anthropic / Azure / AWS Bedrock / vLLM-compatible |
| 计费货币 | USD（多币种由 Stripe 处理） |
| 对账周期 | 每日抓取 OpenAI usage，月度核对 |
| 单 run token 上限 | 平台 key：100k tokens；BYO：不限 |
