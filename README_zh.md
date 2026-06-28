# 🦌 DeerFlow - 2.0 · 多租户改造

[English](./README.md) | 中文 | [日本語](./README_ja.md) | [Français](./README_fr.md) | [Русский](./README_ru.md)

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](./backend/pyproject.toml)
[![Node.js](https://img.shields.io/badge/Node.js-22%2B-339933?logo=node.js&logoColor=white)](./Makefile)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-default-4169E1?logo=postgresql&logoColor=white)](./config.example.yaml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

DeerFlow（**D**eep **E**xploration and **E**fficient **R**esearch **Flow**）是一个开源的 **super agent harness**：它把 **sub-agents**、**memory**、**sandbox** 组织在一起，再配合可扩展的 **skills**，让 agent 可以完成几乎任何事情。

> [!IMPORTANT]
> **本分支（`docs/multi-tenant-redesign`）是 DeerFlow 的多租户改造主线。** 在保留原有 super agent harness 全部能力的基础上，引入了 **workspace 租户模型、Postgres 为默认后端、带 `workspace_id` 的行级数据隔离、扩展后的 JWT（带 `wid`/`role`）、per-workspace 的文件系统布局、Headless API schema 底座，以及 `apps/` 上层应用脚手架**。下面的 [多租户改造](#多租户改造本分支主线) 一节是阅读本仓库的入口。

> [!NOTE]
> **DeerFlow 2.0 是一次彻底重写。** 它和 v1 没有共用代码。如果你要找的是最初的 Deep Research 框架，可以前往 [`1.x` 分支](https://github.com/bytedance/deer-flow/tree/main-1.x)。

https://github.com/user-attachments/assets/a8bcadc4-e040-4cf2-8fda-dd768b999c18

## 多租户改造（本分支主线）

DeerFlow 原本面向"单机可信环境、单用户"。本分支按"以个人用户为主、少量小团队，统一只有 **workspace** 概念（个人 = 1 人 workspace），中心化 SaaS 为主线"的目标，把租户能力分 **Stage 0–4** 渐进落地。**目前 Stage 0（底座）工程层面已全部合入。**

### Stage 0 已落地的能力

| 能力 | 说明 | 落点 |
|---|---|---|
| **workspace 租户模型** | 新增 `workspaces` + `workspace_memberships` 两张表与仓储；每个用户注册时自动建 1 人 workspace（owner=自己）；slug 唯一、黑名单校验。 | `persistence/workspace*` |
| **Postgres 成为默认后端** | `config.example.yaml` / `.env.example` / `make dev` / `make doctor` 默认走 Postgres，与生产对齐；SQLite 保留为离线开发兜底。 | 见 [数据库后端](#数据库后端) |
| **行级数据隔离** | 4 张业务表（`threads_meta` / `runs` / `run_events` / `feedback`）加 `workspace_id` 列（`NOT NULL` + `UNIQUE(workspace_id, thread_id)` 兜底），入口路由按 `(workspace_id, thread_id)` 强校验，跨 workspace 访问必 404。 | `persistence/*`、Gateway routers |
| **扩展后的 JWT** | TokenPayload 一次到位为 `{sub, wid, role, exp, iat, ver}`；登录 / 改密 / `/auth/me` 全部带上 workspace 与角色；旧版 4 字段 JWT 被识别为 `WORKSPACE_MISSING` 并要求重登。 | `app/gateway/auth/` |
| **per-workspace 文件系统** | 运行期状态从 `users/{uid}/...` 迁移到 `workspaces/{wid}/threads/{tid}/...`，提供 `make migrate-paths` 迁移脚本（支持 `DRY_RUN=1` 预览）。 | `config/paths.py`、`thread_data_middleware.py` |
| **边界扫描围栏** | CI 静态扫描禁止任何路径绕过入口直连 LangGraph checkpoint/store，确保隔离不被旁路。 | `tests/boundary_allowlist.toml`、`test_workspace_boundary*.py` |
| **Headless API schema 底座** | 预建 `service_accounts` / `api_keys`（`dfk_live_*` / `dfk_test_*`）/ `external_users` 三张表（schema-only），为 Stage 1 的无人值守接入做准备。 | `persistence/{service_account,api_key,external_user}` |
| **Alembic 迁移 + 回填** | `0001`→`0003` 迁移链 + `backfill_workspace_id.py` 回填脚本，dev 用 `create_all()` 自愈、生产用迁移。 | `persistence/migrations/` |

> 数据库的事实参考（10 张表全字段 / 外键 / 索引）见 [`database-schema-as-built.zh-CN.md`](docs/multi-tenant-redesign/01-redesign/database-schema-as-built.zh-CN.md)。

### 路线图：Stage 0–4

| Stage | 目标 | 关键内容 | 状态 |
|---|---|---|---|
| **0** | workspace 模型立起来 + Postgres 切换 + auth 收紧 | 上表全部 | ✅ 工程层面已合入（业务门 / live 验证待跟进）|
| **1** | 第一批付费客户 + 业务系统集成（双轨并行）| quota + 计费 + AioSandbox 轻量加固；Headless API（Pattern A/B）接通 PR8 三张表 | 🔜 已具备底座 |
| **2** | 增长期，安全与隔离深化 | DeerFlow 自有表启用 RLS、KMS、ObjectStorage、完整 RBAC + invitation | 📋 规划中 |
| **3** | 成熟期，K8s 隔离 + BYO | K8s namespace + NetworkPolicy、BYO LLM key、audit DB 拆分 | 📋 规划中 |
| **4** | 企业化，按需开启 | SSO、自定义域名、per-tenant DB、gVisor/Kata、合规审计 | 📋 按合同 |

### 多租户文档入口

- **汇总索引（先读这个）**：[`docs/multi-tenant-redesign/README.zh-CN.md`](docs/multi-tenant-redesign/README.zh-CN.md)
- **现状架构鸟瞰**：[`00-current-state/architecture-overview.zh-CN.md`](docs/multi-tenant-redesign/00-current-state/architecture-overview.zh-CN.md)
- **决策（7 份 ADR + spike + 审计）**：[`01-redesign/`](docs/multi-tenant-redesign/01-redesign/)
- **Stage 0 schema 锁定版 / 落地版**：[`workspace-schema-design`](docs/multi-tenant-redesign/01-redesign/workspace-schema-design.zh-CN.md) · [`database-schema-as-built`](docs/multi-tenant-redesign/01-redesign/database-schema-as-built.zh-CN.md)
- **落地路线 + 集成轨道**：[`02-rollout/`](docs/multi-tenant-redesign/02-rollout/)
- **Stage 0 进度面板（权威"现在到哪了"）**：[`03-impl/STATUS.zh-CN.md`](docs/multi-tenant-redesign/03-impl/STATUS.zh-CN.md)

## 官网

[<img width="2880" height="1600" alt="image" src="https://github.com/user-attachments/assets/a598c49f-3b2f-41ea-a052-05e21349188a" />](https://deerflow.tech)

想了解更多，或者直接看**真实演示**，可以访问[**官网**](https://deerflow.tech)。

## 字节跳动火山引擎方舟 Coding Plan

[<img width="4808" height="2400" alt="codingplan -banner 素材" src="https://github.com/user-attachments/assets/d30dae52-84f2-4021-b32f-6d281252b9ea" />](https://www.volcengine.com/activity/codingplan?utm_campaign=deer_flow&utm_content=deer_flow&utm_medium=devrel&utm_source=OWO&utm_term=deer_flow)

- 我们推荐使用 Doubao-Seed-2.0-Code、DeepSeek v3.2 和 Kimi 2.5 运行 DeerFlow
- [现在就加入 Coding Plan](https://www.volcengine.com/activity/codingplan?utm_campaign=deer_flow&utm_content=deer_flow&utm_medium=devrel&utm_source=OWO&utm_term=deer_flow)
- [海外地区的开发者请点击这里](https://www.byteplus.com/en/activity/codingplan?utm_campaign=deer_flow&utm_content=deer_flow&utm_medium=devrel&utm_source=OWO&utm_term=deer_flow)

## 目录

- [🦌 DeerFlow - 2.0 · 多租户改造](#-deerflow---20--多租户改造)
  - [多租户改造（本分支主线）](#多租户改造本分支主线)
  - [官网](#官网)
  - [字节跳动火山引擎方舟 Coding Plan](#字节跳动火山引擎方舟-coding-plan)
  - [快速开始](#快速开始)
    - [配置](#配置)
    - [数据库后端](#数据库后端)
    - [运行应用](#运行应用)
      - [部署建议与资源规划](#部署建议与资源规划)
      - [方式一：Docker（推荐）](#方式一docker推荐)
      - [方式二：本地开发](#方式二本地开发)
    - [进阶配置](#进阶配置)
      - [Sandbox 模式](#sandbox-模式)
      - [MCP Server](#mcp-server)
      - [IM 渠道](#im-渠道)
      - [LangSmith 链路追踪](#langsmith-链路追踪)
  - [多租户架构详解](#多租户架构详解)
  - [核心特性](#核心特性)
    - [Skills 与 Tools](#skills-与-tools)
      - [Claude Code 集成](#claude-code-集成)
    - [Sub-Agents](#sub-agents)
    - [Sandbox 与文件系统](#sandbox-与文件系统)
    - [Context Engineering](#context-engineering)
    - [长期记忆](#长期记忆)
  - [在 DeerFlow 之上构建应用（apps/）](#在-deerflow-之上构建应用apps)
  - [内嵌 Python Client](#内嵌-python-client)
  - [推荐模型](#推荐模型)
  - [文档](#文档)
  - [⚠️ 安全使用](#️-安全使用)
  - [参与贡献](#参与贡献)
  - [许可证](#许可证)
  - [致谢](#致谢)

## 快速开始

### 配置

1. **克隆 DeerFlow 仓库**

   ```bash
   git clone https://github.com/bytedance/deer-flow.git
   cd deer-flow
   ```

2. **生成本地配置文件**

   在项目根目录（`deer-flow/`）执行：

   ```bash
   make config
   ```

   这个命令会基于示例模板生成本地配置文件。

3. **配置你要使用的模型**

   编辑 `config.yaml`，至少定义一个模型：

   ```yaml
   models:
     - name: gpt-4                       # 内部标识
       display_name: GPT-4               # 展示名称
       use: langchain_openai:ChatOpenAI  # LangChain 类路径
       model: gpt-4                      # API 使用的模型标识
       api_key: $OPENAI_API_KEY          # API key（推荐使用环境变量）
       max_tokens: 4096                  # 单次请求最大 tokens
       temperature: 0.7                  # 采样温度

     - name: openrouter-gemini-2.5-flash
       display_name: Gemini 2.5 Flash (OpenRouter)
       use: langchain_openai:ChatOpenAI
       model: google/gemini-2.5-flash-preview
       api_key: $OPENAI_API_KEY          # 这里 OpenRouter 依然沿用 OpenAI 兼容字段名
       base_url: https://openrouter.ai/api/v1
   ```

   OpenRouter 以及类似的 OpenAI 兼容网关，建议通过 `langchain_openai:ChatOpenAI` 配合 `base_url` 来配置。如果你更想用 provider 自己的环境变量名，也可以直接把 `api_key` 指向对应变量，例如 `api_key: $OPENROUTER_API_KEY`。

4. **为已配置的模型设置 API key**

   可任选以下一种方式：

- 方式 A：编辑项目根目录下的 `.env` 文件（推荐）

   ```bash
   TAVILY_API_KEY=your-tavily-api-key
   OPENAI_API_KEY=your-openai-api-key
   # 如果配置使用的是 langchain_openai:ChatOpenAI + base_url，OpenRouter 也会读取 OPENAI_API_KEY
   # 其他 provider 的 key 按需补充
   INFOQUEST_API_KEY=your-infoquest-api-key
   ```

- 方式 B：在 shell 中导出环境变量

   ```bash
   export OPENAI_API_KEY=your-openai-api-key
   ```

- 方式 C：直接编辑 `config.yaml`（不建议用于生产环境）

   ```yaml
   models:
     - name: gpt-4
       api_key: your-actual-api-key-here  # 替换为真实 key
   ```

### 数据库后端

多租户改造后，**Stage 0+ 默认后端是 Postgres**（与生产对齐，并为后续 RLS 留好空间）。`config.example.yaml` 默认带：

```yaml
database:
  backend: postgres
  postgres_url: $DATABASE_URL
```

在 `.env` 中设置 `DATABASE_URL`：

```bash
DATABASE_URL=postgresql+asyncpg://deerflow:deerflow_dev@localhost:5432/deerflow
# 远程 RDS / Cloud SQL 示例：
# DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@HOST:5432/DBNAME
```

启动本地 Postgres 开发容器：

```bash
docker compose -f docker/docker-compose-dev.yaml up -d postgres
```

- `make doctor` 会报告当前配置的后端、尝试 asyncpg 连接，并给出可执行的修复建议。
- `make dev` 在启动各服务前会先 preflight Postgres 可达性；`DATABASE_URL` 不可达时直接中止。
- **dev** 启动时用 `Base.metadata.create_all()` 自动建缺失的表（不改已存在的表）；**生产**用 Alembic 迁移（`backend/packages/harness/deerflow/persistence/migrations/`）。目标库不存在时会自动 `CREATE DATABASE` 后重试。

<details>
<summary>离线开发（SQLite 兜底）</summary>

如果你不想起 Postgres，把 `config.yaml` 改成：

```yaml
database:
  backend: sqlite
  sqlite_dir: .deer-flow/data
```

SQLite 仍是合法的离线开发后端；但 RLS / 多节点等 Stage 2+ 能力需要 Postgres。
</details>

### 运行应用

#### 部署建议与资源规划

可以先按下面的资源档位来选择 DeerFlow 的运行方式：

| 部署场景 | 起步配置 | 推荐配置 | 说明 |
|---------|-----------|------------|-------|
| 本地体验 / `make dev` | 4 vCPU、8 GB 内存、20 GB SSD 可用空间 | 8 vCPU、16 GB 内存 | 适合单个开发者或单个轻量会话，且模型走外部 API。`2 核 / 4 GB` 通常跑不稳。 |
| Docker 开发 / `make docker-start` | 4 vCPU、8 GB 内存、25 GB SSD 可用空间 | 8 vCPU、16 GB 内存 | 镜像构建、源码挂载和 sandbox 容器都会比纯本地模式更吃资源。 |
| 长期运行服务 / `make up` | 8 vCPU、16 GB 内存、40 GB SSD 可用空间 | 16 vCPU、32 GB 内存 | 更适合共享环境、多 agent 任务、报告生成或更重的 sandbox 负载。 |

- 上面的配置只覆盖 DeerFlow 本身；如果你还要本机部署本地大模型，请单独为模型服务预留资源。
- 持续运行的服务更推荐使用 Linux + Docker。macOS 和 Windows 更适合作为开发机或体验环境。
- 如果 CPU 或内存长期打满，先降低并发会话或重任务数量，再考虑升级到更高一档配置。

#### 方式一：Docker（推荐）

**开发模式**（支持热更新，挂载源码）：

```bash
make docker-init    # 拉取 sandbox 镜像（首次运行或镜像更新时执行）
make docker-start   # 启动服务（会根据 config.yaml 自动判断 sandbox 模式）
```

如果 `config.yaml` 使用的是 provisioner 模式（`sandbox.use: deerflow.community.aio_sandbox:AioSandboxProvider` 且配置了 `provisioner_url`），`make docker-start` 才会启动 `provisioner`。

**生产模式**（本地构建镜像，并挂载运行期配置与数据）：

```bash
make up     # 构建镜像并启动全部生产服务
make down   # 停止并移除容器
```

> [!NOTE]
> 当前 LangGraph agent server 通过开源 CLI 服务 `langgraph dev` 运行。

访问地址：http://localhost:2026

更完整的 Docker 开发说明见 [CONTRIBUTING.md](CONTRIBUTING.md)。

#### 方式二：本地开发

如果你更希望直接在本地启动各个服务：

前提：先完成上面的"配置"步骤（`make config`、模型 API key、`DATABASE_URL`）。`make dev` 需要有效配置文件，默认读取项目根目录下的 `config.yaml`。可以用 `DEER_FLOW_PROJECT_ROOT` 显式指定项目根目录，也可以用 `DEER_FLOW_CONFIG_PATH` 指向某个具体配置文件。运行期状态默认写到项目根目录下的 `.deer-flow`，可用 `DEER_FLOW_HOME` 覆盖；skills 默认读取项目根目录下的 `skills/`，可用 `DEER_FLOW_SKILLS_PATH` 覆盖。
在 Windows 上，请使用 Git Bash 运行本地开发流程。基于 bash 的服务脚本不支持直接在原生 `cmd.exe` 或 PowerShell 中执行，且 WSL 也不保证可用，因为部分脚本依赖 Git for Windows 的 `cygpath` 等工具。

1. **检查依赖环境**：
   ```bash
   make check  # 校验 Node.js 22+、pnpm、uv、nginx
   ```

2. **安装依赖**：
   ```bash
   make install  # 安装 backend + frontend 依赖
   ```

3. **（可选）预拉取 sandbox 镜像**：
   ```bash
   # 如果使用 Docker / Container sandbox，建议先执行
   make setup-sandbox
   ```

4. **启动服务**：
   ```bash
   make dev
   ```

5. **访问地址**：http://localhost:2026

> [!TIP]
> `make dev` 是前台阻塞运行。日常调试更顺手的是仓库根 `scripts/` 下两个生命周期脚本（子命令统一为 `start / stop / restart / status / logs / run`）：
> - `scripts/dev-gateway.sh` — 只起 Gateway（`http://localhost:8001`），起得快，适合调后端 API / 接入示例。
> - `scripts/dev-full.sh` — Gateway + 前端 + nginx（`http://localhost:2026`），连前端一起调。
>
> 例如 `./scripts/dev-gateway.sh start`、`./scripts/dev-gateway.sh logs`、`SKIP_INSTALL=1 ./scripts/dev-full.sh start`。详见 [apps/README.md](apps/README.md)。

> [!NOTE]
> 把历史的 `users/` 目录树迁移到新的 per-workspace 布局：`make migrate-paths`（加 `DRY_RUN=1` 仅预览，`DEFAULT_WORKSPACE=<wid>` 指定未分配用户的归属 workspace）。

### 进阶配置
#### Sandbox 模式

DeerFlow 支持多种 sandbox 执行方式：
- **本地执行**（直接在宿主机上运行 sandbox 代码）
- **Docker 执行**（在隔离的 Docker 容器里运行 sandbox 代码）
- **Docker + Kubernetes 执行**（通过 provisioner 服务在 Kubernetes Pod 中运行 sandbox 代码）

Docker 开发时，服务启动行为会遵循 `config.yaml` 里的 sandbox 模式。在 Local / Docker 模式下，不会启动 `provisioner`。

如果要配置你自己的模式，参见 [Sandbox 配置指南](backend/docs/CONFIGURATION.md#sandbox)。

#### MCP Server

DeerFlow 支持可配置的 MCP Server 和 skills，用来扩展能力。
对于 HTTP/SSE MCP Server，还支持 OAuth token 流程（`client_credentials`、`refresh_token`）。
详细说明见 [MCP Server 指南](backend/docs/MCP_SERVER.md)。

#### IM 渠道

DeerFlow 支持从即时通讯应用接收任务。只要配置完成，对应渠道会自动启动，而且都不需要公网 IP。

| 渠道 | 传输方式 | 上手难度 |
|---------|-----------|------------|
| Telegram | Bot API（long-polling） | 简单 |
| Slack | Socket Mode | 中等 |
| Feishu / Lark | WebSocket | 中等 |
| 企业微信智能机器人 | WebSocket | 中等 |
| 钉钉 | Stream Push（WebSocket） | 中等 |

**`config.yaml` 中的配置示例：**

```yaml
channels:
  # LangGraph Server URL（默认：http://localhost:2024）
  langgraph_url: http://localhost:2024
  # Gateway API URL（默认：http://localhost:8001）
  gateway_url: http://localhost:8001

  # 可选：所有移动端渠道共用的全局 session 默认值
  session:
    assistant_id: lead_agent  # 也可以填自定义 agent 名；渠道层会自动转换为 lead_agent + agent_name
    config:
      recursion_limit: 100
    context:
      thinking_enabled: true
      is_plan_mode: false
      subagent_enabled: false

  feishu:
    enabled: true
    app_id: $FEISHU_APP_ID
    app_secret: $FEISHU_APP_SECRET
    # domain: https://open.feishu.cn       # 国内版（默认）
    # domain: https://open.larksuite.com   # 国际版

  wecom:
    enabled: true
    bot_id: $WECOM_BOT_ID
    bot_secret: $WECOM_BOT_SECRET

  slack:
    enabled: true
    bot_token: $SLACK_BOT_TOKEN     # xoxb-...
    app_token: $SLACK_APP_TOKEN     # xapp-...（Socket Mode）
    allowed_users: []               # 留空表示允许所有人

  telegram:
    enabled: true
    bot_token: $TELEGRAM_BOT_TOKEN
    allowed_users: []               # 留空表示允许所有人

    # 可选：按渠道 / 按用户单独覆盖 session 配置
    session:
      assistant_id: mobile-agent  # 这里同样支持自定义 agent 名
      context:
        thinking_enabled: false
      users:
        "123456789":
          assistant_id: vip-agent
          config:
            recursion_limit: 150
          context:
            thinking_enabled: true
            subagent_enabled: true

  dingtalk:
    enabled: true
    client_id: $DINGTALK_CLIENT_ID             # 钉钉开放平台 ClientId
    client_secret: $DINGTALK_CLIENT_SECRET     # 钉钉开放平台 ClientSecret
    allowed_users: []                          # 留空表示允许所有人
    card_template_id: ""                       # 可选：AI 卡片模板 ID，用于流式打字机效果
```

说明：
- `assistant_id: lead_agent` 会直接调用默认的 LangGraph assistant。
- 如果 `assistant_id` 填的是自定义 agent 名，DeerFlow 仍然会走 `lead_agent`，同时把该值注入为 `agent_name`，这样 IM 渠道也会生效对应 agent 的 SOUL 和配置。

在 `.env` 里设置对应的 API key：

```bash
# Telegram
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrSTUvwxYZ

# Slack
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...

# Feishu / Lark
FEISHU_APP_ID=cli_xxxx
FEISHU_APP_SECRET=your_app_secret

# 企业微信智能机器人
WECOM_BOT_ID=your_bot_id
WECOM_BOT_SECRET=your_bot_secret

# 钉钉
DINGTALK_CLIENT_ID=your_client_id
DINGTALK_CLIENT_SECRET=your_client_secret
```

**Telegram 配置**

1. 打开 [@BotFather](https://t.me/BotFather)，发送 `/newbot`，复制生成的 HTTP API token。
2. 在 `.env` 中设置 `TELEGRAM_BOT_TOKEN`，并在 `config.yaml` 里启用该渠道。

**Slack 配置**

1. 前往 [api.slack.com/apps](https://api.slack.com/apps) 创建 Slack App：Create New App → From scratch。
2. 在 **OAuth & Permissions** 中添加 Bot Token Scopes：`app_mentions:read`、`chat:write`、`im:history`、`im:read`、`im:write`、`files:write`。
3. 启用 **Socket Mode**，生成带 `connections:write` 权限的 App-Level Token（`xapp-...`）。
4. 在 **Event Subscriptions** 中订阅 bot events：`app_mention`、`message.im`。
5. 在 `.env` 中设置 `SLACK_BOT_TOKEN` 和 `SLACK_APP_TOKEN`，并在 `config.yaml` 中启用该渠道。

**Feishu / Lark 配置**

1. 在 [飞书开放平台](https://open.feishu.cn/) 创建应用，并启用 **Bot** 能力。
2. 添加权限：`im:message`、`im:message.p2p_msg:readonly`、`im:resource`。
3. 在 **事件订阅** 中订阅 `im.message.receive_v1`，连接方式选择 **长连接**。
4. 复制 App ID 和 App Secret，在 `.env` 中设置 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET`，并在 `config.yaml` 中启用该渠道。

**企业微信智能机器人配置**

1. 在企业微信智能机器人平台创建机器人，获取 `bot_id` 和 `bot_secret`。
2. 在 `config.yaml` 中启用 `channels.wecom`，并填入 `bot_id` / `bot_secret`。
3. 在 `.env` 中设置 `WECOM_BOT_ID` 和 `WECOM_BOT_SECRET`。
4. 安装后端依赖时确保包含 `wecom-aibot-python-sdk`，渠道会通过 WebSocket 长连接接收消息，无需公网回调地址。
5. 当前支持文本、图片和文件入站消息；agent 生成的最终图片/文件也会回传到企业微信会话中。

**钉钉配置**

1. 在 [钉钉开放平台](https://open.dingtalk.com/) 创建应用，并启用 **机器人** 能力。
2. 在机器人配置页面设置消息接收模式为 **Stream模式**。
3. 复制 `Client ID` 和 `Client Secret`，在 `.env` 中设置 `DINGTALK_CLIENT_ID` 和 `DINGTALK_CLIENT_SECRET`，并在 `config.yaml` 中启用该渠道。
4. *（可选）* 如需开启流式 AI 卡片回复（打字机效果），请在[钉钉卡片平台](https://open.dingtalk.com/document/dingstart/typewriter-effect-streaming-ai-card)创建 **AI 卡片**模板，然后在 `config.yaml` 中将 `card_template_id` 设为该模板 ID。同时需要申请 `Card.Streaming.Write` 和 `Card.Instance.Write` 权限。

**命令**

渠道连接完成后，你可以直接在聊天窗口里和 DeerFlow 交互：

| 命令 | 说明 |
|---------|-------------|
| `/new` | 开启新对话 |
| `/status` | 查看当前 thread 信息 |
| `/models` | 列出可用模型 |
| `/memory` | 查看 memory |
| `/help` | 查看帮助 |

> 没有命令前缀的消息会被当作普通聊天处理。DeerFlow 会自动创建 thread，并以对话方式回复。

#### LangSmith 链路追踪

DeerFlow 内置了 [LangSmith](https://smith.langchain.com) 集成，用于可观测性。启用后，所有 LLM 调用、agent 运行和工具执行都会被追踪，并在 LangSmith 仪表盘中展示。

在 `.env` 文件中添加以下配置：

```bash
LANGSMITH_TRACING=true
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
LANGSMITH_API_KEY=lsv2_pt_xxxxxxxxxxxxxxxx
LANGSMITH_PROJECT=xxx
```

Docker 部署时，追踪默认关闭。在 `.env` 中设置 `LANGSMITH_TRACING=true` 和 `LANGSMITH_API_KEY` 即可启用。

## 多租户架构详解

> 这一节展开 [多租户改造](#多租户改造本分支主线) 里 Stage 0 已落地的实现细节。完整决策与路线见 [`docs/multi-tenant-redesign/`](docs/multi-tenant-redesign/)。

**workspace 是唯一的隔离粒度。** 个人用户 = 1 人 workspace，小团队 = 多人 workspace。骨架是两条主线：

1. **租户骨架**：`users` ↔ `workspaces`（多对多经 `workspace_memberships`）。每个用户注册时自动建 1 人 workspace（owner=自己），slug 唯一且过黑名单校验。
2. **业务数据**：`threads_meta` → `runs` → `run_events` / `feedback`，全部挂 `workspace_id`（行级隔离），workspace 删除时级联清空。

**数据隔离怎么做的。** DeerFlow 自有表走行级 `workspace_id` + `UNIQUE(workspace_id, thread_id)` 兜底；入口路由（`threads.py` / `thread_runs.py`）按 `(workspace_id, thread_id)` 强校验，跨 workspace 访问必返 404。LangGraph 自己的 checkpointer / store 表（`langgraph-checkpoint-postgres==3.0.5` 无 `connection_factory`，无法注入 RLS）则走**应用层强校验**，并由 CI 静态扫描（`tests/boundary_allowlist.toml`）禁止任何路径绕过入口直连这些表。

**身份与会话。** JWT TokenPayload 一次到位为 `{sub, wid, role, exp, iat, ver}`：

- `wid` — 当前 workspace；`role` — workspace 内角色（Stage 0 简化为 owner-only，Stage 2 扩到 owner/admin/member）。
- `ver` — `token_version`，bump 后旧 token 全失效。
- 旧版 4 字段 JWT 会被识别为 `WORKSPACE_MISSING` 并要求重登；登录 / 改密 / `/auth/me` 都会带上 workspace 与角色（`/auth/me` 返回 `workspaces[]`，含 id/name/slug/role）。

**文件系统布局。** 运行期状态按 workspace 分目录：

```text
${DEER_FLOW_HOME:-./.deer-flow}/
└── workspaces/
    └── {workspace_id}/
        ├── threads/{thread_id}/...   ← 每个 thread 的 sandbox / 产物
        └── users/{user_id}/...       ← 用户级状态
```

历史的 `users/{uid}/...` 布局用 `make migrate-paths`（`DRY_RUN=1` 预览）迁移过来。

**Headless API schema 底座（Stage 0 末预建，schema-only）。** 为 Stage 1 的无人值守 / 业务系统接入准备：

- `service_accounts` — workspace 内的非人身份，带 `identity_mode` 三态（`collapsed` / `external_passthrough` / `both`）。
- `api_keys` — service account 的凭证，格式 `dfk_live_*` / `dfk_test_*`，`key_prefix` 全局唯一 + 部分索引 `WHERE revoked_at IS NULL`。
- `external_users` — passthrough 终端身份，`(service_account_id, external_id)` 复合唯一。

> ⚠️ Stage 0 只建表，**API Key 鉴权中间件尚未接入**。外部系统当前只能走会话 cookie（见 [apps/README.md](apps/README.md) 的鉴权说明）；等 `Authorization: Bearer dfk_live_...` 在 Stage 1 落地后再补无人值守接入。

## 核心特性

### Skills 与 Tools

Skills 是 DeerFlow 能做"几乎任何事"的关键。

标准的 Agent Skill 是一种结构化能力模块，通常就是一个 Markdown 文件，里面定义了工作流、最佳实践，以及相关的参考资源。DeerFlow 自带一批内置 skills，覆盖研究、报告生成、演示文稿制作、网页生成、图像和视频生成等场景。真正有意思的地方在于它的扩展性：你可以加自己的 skills，替换内置 skills，或者把多个 skills 组合成复合工作流。

Skills 采用按需渐进加载，不会一次性把所有内容都塞进上下文。只有任务确实需要时才加载，这样能把上下文窗口控制得更干净，也更适合对 token 比较敏感的模型。

通过 Gateway 安装 `.skill` 压缩包时，DeerFlow 会接受标准的可选 frontmatter 元数据，比如 `version`、`author`、`compatibility`，不会把本来合法的外部 skill 拒之门外。

Tools 也是同样的思路。DeerFlow 自带一组核心工具：网页搜索、网页抓取、文件操作、bash 执行；同时也支持通过 MCP Server 和 Python 函数扩展自定义工具。你可以替换任何一项，也可以继续往里加。

```text
# sandbox 容器内的路径
/mnt/skills/public
├── research/SKILL.md
├── report-generation/SKILL.md
├── slide-creation/SKILL.md
├── web-page/SKILL.md
└── image-generation/SKILL.md

/mnt/skills/custom
└── your-custom-skill/SKILL.md      ← 你的 skill
```

#### Claude Code 集成

借助 `claude-to-deerflow` skill，你可以直接在 [Claude Code](https://docs.anthropic.com/en/docs/claude-code) 里和正在运行的 DeerFlow 实例交互。不用离开终端，就能下发研究任务、查看状态、管理 threads。

**安装这个 skill：**

```bash
npx skills add https://github.com/bytedance/deer-flow --skill claude-to-deerflow
```

然后确认 DeerFlow 已经启动（默认地址是 `http://localhost:2026`），在 Claude Code 里使用 `/claude-to-deerflow` 命令即可。完整 API 说明见 [`skills/public/claude-to-deerflow/SKILL.md`](skills/public/claude-to-deerflow/SKILL.md)。

### Sub-Agents

复杂任务通常不可能一次完成，DeerFlow 会先拆解，再执行。

lead agent 可以按需动态拉起 sub-agents。每个 sub-agent 都有自己独立的上下文、工具和终止条件。只要条件允许，它们就会并行运行，返回结构化结果，最后再由 lead agent 汇总成一份完整输出。

这也是 DeerFlow 能处理从几分钟到几小时任务的原因。比如一个研究任务，可以拆成十几个 sub-agents，分别探索不同方向，最后合并成一份报告，或者一个网站，或者一套带生成视觉内容的演示文稿。一个 harness，多路并行。

### Sandbox 与文件系统

DeerFlow 不只是"会说它能做"，它是真的有一台自己的"电脑"。

每个任务都运行在隔离的 Docker 容器里，里面有完整的文件系统，包括 skills、workspace、uploads、outputs。agent 可以读写和编辑文件，可以执行 bash 命令和代码，也可以查看图片。整个过程都在 sandbox 内完成，可审计、会隔离，不会在不同 session 之间互相污染。

```text
# sandbox 容器内的路径
/mnt/user-data/
├── uploads/          ← 你的文件
├── workspace/        ← agents 的工作目录
└── outputs/          ← 最终交付物
```

### Context Engineering

**隔离的 Sub-Agent Context**：每个 sub-agent 都在自己独立的上下文里运行。它看不到主 agent 的上下文，也看不到其他 sub-agents 的上下文。这样做的目的很直接，就是让它只聚焦当前任务，不被无关信息干扰。

**摘要压缩**：在单个 session 内，DeerFlow 会比较积极地管理上下文，包括总结已完成的子任务、把中间结果转存到文件系统、压缩暂时不重要的信息。这样在长链路、多步骤任务里，它也能保持聚焦，而不会轻易把上下文窗口打爆。

### 长期记忆

大多数 agents 会在对话结束后把一切都忘掉，DeerFlow 不一样。

跨 session 使用时，DeerFlow 会逐步积累关于你的持久 memory，包括你的个人偏好、知识背景，以及长期沉淀下来的工作习惯。你用得越多，它越了解你的写作风格、技术栈和重复出现的工作流。

## 在 DeerFlow 之上构建应用（apps/）

`apps/`（仓库根目录、与 `backend/` / `frontend/` 平级）用于存放**消费 DeerFlow 能力的上层应用**，遵循严格的依赖方向：**app 可以依赖 deerflow，deerflow 不能依赖 app / apps**。

两种集成模式：

| 模式 | 适用场景 | 怎么连 | 示例 |
|---|---|---|---|
| **HTTP Gateway**（REST+SSE） | 上层是别的服务 / 多语言 | 调 `http://localhost:2026/api/*` | [`apps/examples/http-chat/`](apps/examples/http-chat/) |
| **内嵌 DeerFlowClient** | 上层本身是 Python，进程内直接当 SDK 调 | `from deerflow.client import DeerFlowClient` | [`apps/examples/embedded-chat/`](apps/examples/embedded-chat/) |

每个示例自带 `run.sh`：

```bash
# ① HTTP 模式：需要先起 Gateway（dev-gateway 或 dev-full 都行）
./apps/examples/http-chat/run.sh

# ② 内嵌模式：不需要起任何服务，run.sh 自动进 backend uv 环境运行
./apps/examples/embedded-chat/run.sh
```

完整说明、鉴权流程与新建应用约定见 [apps/README.md](apps/README.md)。

## 内嵌 Python Client

DeerFlow 也可以作为内嵌的 Python 库使用，不必启动完整的 HTTP 服务。`DeerFlowClient` 提供了进程内的直接访问方式，覆盖所有 agent 和 Gateway 能力，返回的数据结构与 HTTP Gateway API 保持一致：

```python
from deerflow.client import DeerFlowClient

client = DeerFlowClient()

# Chat
response = client.chat("Analyze this paper for me", thread_id="my-thread")

# Streaming（LangGraph SSE 协议：values、messages-tuple、end）
for event in client.stream("hello"):
    if event.type == "messages-tuple" and event.data.get("type") == "ai":
        print(event.data["content"])

# 配置与管理：返回值与 Gateway 对齐的 dict
models = client.list_models()        # {"models": [...]}
skills = client.list_skills()        # {"skills": [...]}
client.update_skill("web-search", enabled=True)
client.upload_files("thread-1", ["./report.pdf"])  # {"success": True, "files": [...]}
```

所有返回 dict 的方法都会在 CI 中通过 Gateway 的 Pydantic 响应模型校验（`TestGatewayConformance`），以确保内嵌 client 始终和 HTTP API schema 保持同步。完整 API 说明见 `backend/packages/harness/deerflow/client.py`。

## 推荐模型

DeerFlow 对模型没有强绑定，只要实现了 OpenAI 兼容 API 的 LLM，理论上都可以接入。不过在下面这些能力上表现更强的模型，通常会更适合 DeerFlow：

- **长上下文窗口**（100k+ tokens），适合深度研究和多步骤任务
- **推理能力**，适合自适应规划和复杂拆解
- **多模态输入**，适合理解图片和视频
- **稳定的 tool use 能力**，适合可靠的函数调用和结构化输出

## 文档

- [多租户改造汇总索引](docs/multi-tenant-redesign/README.zh-CN.md) - workspace / Postgres / RLS / Headless API 的决策与路线
- [Stage 0 进度面板](docs/multi-tenant-redesign/03-impl/STATUS.zh-CN.md) - "现在到哪了"的权威来源
- [数据库设计落地版](docs/multi-tenant-redesign/01-redesign/database-schema-as-built.zh-CN.md) - 10 张表全字段 / 外键 / 索引参考
- [贡献指南](CONTRIBUTING.md) - 开发环境搭建与协作流程
- [配置指南](backend/docs/CONFIGURATION.md) - 安装与配置说明
- [架构概览](backend/CLAUDE.md) - 技术架构说明
- [后端架构](backend/README.md) - 后端架构与 API 参考
- [apps/ 上层应用](apps/README.md) - 在 DeerFlow 之上构建应用

## ⚠️ 安全使用

### 不恰当的部署可能导致安全风险

DeerFlow 具备**系统指令执行、资源操作、业务逻辑调用**等关键高权限能力，默认设计为**部署在本地可信环境（仅本机 127.0.0.1 回环访问）**。若您将 agent 部署至不可信局域网、公网云服务器等可被多终端访问的网络环境，且未采取严格的安全防护措施，可能导致安全风险，例如：

- **未授权的非法调用**：agent 功能被未授权的第三方、公网恶意扫描程序探测到，进而发起批量非法调用请求，执行系统命令、文件读写等高危操作，可能导致安全后果。
- **合规与法律风险**：若 agent 被非法调用用于实施网络攻击、信息窃取等违法违规行为，可能产生法律责任与合规风险。

> [!NOTE]
> 多租户改造引入的 workspace 行级隔离 / 入口强校验 / 边界扫描，目标是**应用内**的租户隔离；它不替代上面的网络层 / 部署层防护。把 DeerFlow 曝光到不可信网络仍需配合下面的安全措施。多租户更强的 DB 层兜底（RLS / KMS / K8s）规划在 Stage 2–3。

### 安全使用建议

**注意：建议您将 DeerFlow 部署在本地可信的网络环境下。** 若您有跨设备、跨网络的部署需求，必须加入严格的安全措施。例如，采取如下手段：

- **设置访问 IP 白名单**：使用 `iptables`，或部署硬件防火墙 / 带访问控制（ACL）功能的交换机等，**配置规则设置 IP 白名单**，拒绝其他所有 IP 进行访问。
- **前置身份验证**：配置反向代理（nginx 等），并**开启高强度的前置身份验证功能**，禁止无任何身份验证的访问。
- **网络隔离**：若有可能，建议将 agent 和可信设备划分到**同一个专用 VLAN**，与其他网络设备做隔离。
- **持续关注项目更新**：请持续关注 DeerFlow 项目的安全功能更新。

## 参与贡献

欢迎参与贡献。开发环境、工作流和相关规范见 [CONTRIBUTING.md](CONTRIBUTING.md)。

提 PR 前请先在本地跑通校验（CI 会在每个 PR 上执行 backend lint + 测试，含 Postgres matrix）：

```bash
cd backend && make lint && make test     # ruff + pytest
cd frontend && pnpm lint && pnpm typecheck
```

## 许可证

本项目采用 [MIT License](./LICENSE) 开源发布。

## 致谢

DeerFlow 建立在开源社区大量优秀工作的基础上。所有让 DeerFlow 成为可能的项目和贡献者，我们都心怀感谢。

特别感谢以下项目带来的关键支持：

- **[LangChain](https://github.com/langchain-ai/langchain)**：它们提供的优秀框架支撑了我们的 LLM 交互与 chains。
- **[LangGraph](https://github.com/langchain-ai/langgraph)**：它们在多 agent 编排上的创新方式，是 DeerFlow 复杂工作流得以成立的重要基础。

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=bytedance/deer-flow&type=Date)](https://star-history.com/#bytedance/deer-flow&Date)
</content>
</invoke>
