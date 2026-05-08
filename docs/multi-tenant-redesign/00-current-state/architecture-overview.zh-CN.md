# DeerFlow 整体架构鸟瞰

按"自外向内、自顶向下"分层讲，并指出每一层对应的代码位置，方便后续深入。

## 一、进程与部署拓扑

DeerFlow 表面是 4 个端口，本质是 **3 个进程 + 1 个反向代理**。

```
                      ┌──────────────────────┐
浏览器 / IM ─────────▶│  nginx :2026         │  统一入口
                      │  (含 CORS、SSE 透传) │
                      └──────────┬───────────┘
                                 │
              ┌──────────────────┴──────────────────┐
              │                                     │
              ▼                                     ▼
   ┌────────────────────┐               ┌──────────────────────────┐
   │ Frontend (Next.js) │               │  Gateway (uvicorn)       │
   │      :3000         │               │  :8001                   │
   │ pnpm dev / preview │               │ ┌──────────────────────┐ │
   └────────────────────┘               │ │ FastAPI 路由层       │ │
                                        │ │ /api/models, /skills │ │
                                        │ │ /threads, /runs ...  │ │
                                        │ ├──────────────────────┤ │
                                        │ │ LangGraph Runtime    │ │
                                        │ │ (RunManager,         │ │
                                        │ │  StreamBridge,       │ │
                                        │ │  Checkpointer)       │ │
                                        │ ├──────────────────────┤ │
                                        │ │ lead_agent 图        │ │
                                        │ │  + 18 个中间件       │ │
                                        │ │  + Sandbox / Tools   │ │
                                        │ └──────────────────────┘ │
                                        └────────────┬─────────────┘
                                                     │
                          ┌──────────────────────────┼─────────────────────────┐
                          ▼                          ▼                         ▼
               ┌──────────────────┐      ┌──────────────────┐       ┌──────────────────┐
               │ Sandbox          │      │ MCP Servers      │       │ LLM 提供商       │
               │ Local / AIO Docker│     │ (stdio/sse/http) │       │ OpenAI/Anthropic │
               │ 提供 bash/fs     │      │                  │       │ /vLLM/Codex CLI  │
               └──────────────────┘      └──────────────────┘       └──────────────────┘
```

关键事实（容易踩坑）：

- **LangGraph 运行时不是独立进程**，而是嵌在 Gateway 这同一个 uvicorn 进程里。`scripts/serve.sh:225` 只起了一个后端进程：`uvicorn app.gateway.app:app`。
- nginx 把 `/api/langgraph/*` 重写成 `/api/*` 再代理到 Gateway（`docker/nginx/nginx.local.conf:49-73`），所以前端用的"标准 LangGraph SDK 协议"和 DeerFlow 自己的 REST 是同一个 8001 端口。
- 路由协议在 nginx 里特意为 SSE 关闭了缓冲（`proxy_buffering off; X-Accel-Buffering no`），否则流式响应会被吃掉。

## 二、后端代码二分：Harness vs App

后端最重要的边界，**决定新写代码该放哪里**。

```
backend/
├── packages/harness/deerflow/   ← 可发布的智能体框架（import: deerflow.*）
│   ├── agents/        lead_agent + memory + middlewares + ThreadState
│   ├── runtime/       checkpointer / runs / stream_bridge / events / store
│   ├── sandbox/       Sandbox 抽象 + local 实现 + 文件/bash 工具
│   ├── subagents/     子代理注册表 + 后台执行池
│   ├── tools/         内建工具（present_files / ask_clarification / view_image）
│   ├── mcp/           MultiServerMCPClient + 缓存 + OAuth
│   ├── skills/        SKILL.md 加载、工具白名单
│   ├── models/        模型工厂、vLLM/Codex/Claude 自定义 provider
│   ├── community/     tavily / jina / firecrawl / aio_sandbox（可选实现）
│   ├── memory/        长期记忆（事实抽取、debounce 队列）
│   ├── persistence/   SQLAlchemy 模型（用户、运行、事件、反馈）
│   ├── guardrails/    工具调用前置鉴权（可插拔 provider）
│   ├── tracing/       LangSmith / Langfuse callback
│   ├── reflection/    "module:variable" 字符串 → 实例（配置驱动的关键）
│   ├── uploads/       上传文件转换 markdown
│   └── client.py      DeerFlowClient（嵌入式 Python 客户端）
│
└── app/                          ← 应用代码（import: app.*）
    ├── gateway/
    │   ├── app.py                FastAPI 入口 + lifespan
    │   ├── auth_middleware.py    会话/Token 鉴权
    │   ├── csrf_middleware.py    双重 cookie CSRF
    │   ├── langgraph_auth.py     注入到 langgraph.json 的鉴权钩子
    │   └── routers/              ↓ 下表
    └── channels/                 IM（Slack/Telegram/Feishu/DingTalk/微信/企微）
```

**铁律：app 可以 import deerflow，deerflow 不能 import app**（CI 用 `tests/test_harness_boundary.py` 强制）。这意味着 harness 必须自给自足——任何"agent 运行时需要的能力"都要在 harness 里完成抽象，app 层只做 HTTP/IM 适配。

## 三、Gateway 路由总览

`backend/app/gateway/routers/` 14 个路由文件，分三类职责：

| 类别 | 路由 | 干什么 |
|---|---|---|
| **配置/资源管理** | `models` `skills` `mcp` `memory` `agents` | 列出/启停 LLM 模型、技能、MCP、自定义 agent |
| **会话/数据** | `threads` `uploads` `artifacts` `suggestions` | 管理线程、上传文件、产物下载、追问建议 |
| **运行（核心）** | `thread_runs` `runs` `feedback` `assistants_compat` | 创建运行、SSE 流、消息分页、反馈打分、LangGraph 兼容协议 |
| **横切** | `auth` `channels` | 用户登录注册、IM 渠道状态 |

`assistants_compat.py` 是关键：它把前端用的 LangGraph SDK 协议（`POST /threads/{id}/runs/stream`、`messages-tuple` 流模式等）翻译成 DeerFlow 内部的 `RunManager` 调用——这就是 nginx 那条 `/api/langgraph/*` 重写规则的接收端。

## 四、一次对话的完整生命周期

把上面所有零件串起来——用户在前端输入一句话，会发生这些事：

```
1. 前端 useThreadStream hook
   └─▶ LangGraph SDK 调用 POST /api/langgraph/threads/{id}/runs/stream
                         (stream_mode=["values","messages-tuple","custom"])

2. nginx 重写 → /api/threads/{id}/runs/stream → Gateway

3. Gateway thread_runs 路由
   ├─▶ AuthMiddleware 解析 session → user_id 注入到 user_context（contextvar）
   ├─▶ CSRFMiddleware 校验
   └─▶ runtime.RunManager 创建 Run → 落库（runs / run_events 表）

4. RunManager 调用 lead_agent 图（langgraph.json: deerflow.agents:make_lead_agent）
   ├─▶ 解析 configurable: model_name / thinking_enabled / is_plan_mode / subagent_enabled
   ├─▶ create_chat_model() 实例化 LLM（reflection 从 "module:Class" 字符串实例化）
   └─▶ create_agent(model, tools, middlewares, state_schema=ThreadState)

5. 18 个中间件按顺序拦截每一轮 model→tool→model：
   ThreadDataMiddleware       创建 .deer-flow/users/{uid}/threads/{tid}/...
   UploadsMiddleware          注入新上传文件
   SandboxMiddleware          acquire 沙箱，state.sandbox_id 写入
   DanglingToolCall           修复中断的 tool_call 序列
   LLMErrorHandling           LLM 报错降级
   Guardrail                  工具调用前鉴权（可选）
   SandboxAudit               记录 bash/fs 操作
   ToolErrorHandling          tool 异常 → ToolMessage 不中断
   Summarization              token 接近上限时压缩历史
   TodoList                   plan_mode 才挂
   TokenUsage                 累计 token
   Title                      首轮后自动起标题
   Memory                     队列异步抽取记忆
   ViewImage                  视觉模型注入 base64
   DeferredToolFilter         需要时才暴露 tool schema
   SubagentLimit              限制 task 并发到 3
   LoopDetection              检测重复工具循环
   Clarification              ask_clarification 触发 interrupt(END)

6. Tools 由 get_available_tools() 拼装：
   ├─ Sandbox 工具：bash / ls / read_file / write_file / str_replace
   ├─ 内建工具：present_files / ask_clarification / view_image / setup_agent
   ├─ MCP 工具：从 extensions_config.json 启用的 server 拉取
   ├─ Community 工具：tavily / jina / firecrawl / image_search（按 config.yaml）
   └─ task 工具（可选）：派遣 subagent

7. StreamBridge 把图执行的事件流转换成 SSE：
   - "values"          完整状态快照
   - "messages-tuple"  增量 token / 工具调用 / 工具返回
   - "custom"          StreamWriter 自定义事件
   - "end"             收尾，附 token usage

8. 前端 LangGraph SDK 接 SSE，按 message id 累加 delta，更新 UI

9. 运行结束后，MemoryMiddleware 后台 30s debounce 抽取记忆事实写入
   .deer-flow/users/{uid}/memory.json
```

## 五、状态与持久化的几条线

DeerFlow 的状态被有意拆成"快/慢/历史"三层，因为它要同时支持长会话、跨进程恢复、文件级产物：

| 状态 | 位置 | 谁写 |
|---|---|---|
| **会话状态（messages, todos, artifacts）** | LangGraph checkpointer（内置 SQLite/PG，路径在 `runtime/checkpointer/async_provider.py`） | 每个 step 自动 |
| **运行元数据/事件流** | `persistence/` 下 SQLAlchemy 模型（`runs`、`run_events`、`feedback`、`threads_meta`） | RunManager + StreamBridge |
| **每用户每线程文件** | `.deer-flow/users/{uid}/threads/{tid}/user-data/{workspace,uploads,outputs}` | ThreadDataMiddleware + 沙箱工具 |
| **长期记忆** | `.deer-flow/users/{uid}/memory.json`（可叠加 per-agent） | MemoryMiddleware（异步） |
| **配置** | `config.yaml`（模型、工具、沙箱、记忆…） + `extensions_config.json`（MCP、技能开关） | `make setup` 或 Gateway PUT |

agent 看到的永远是 **虚拟路径** `/mnt/user-data/...` 和 `/mnt/skills/...`，由 `sandbox/tools.py` 的 `replace_virtual_path()` 翻译成上面物理路径。这层抽象让"本地沙箱"和"Docker 沙箱"对 agent 完全透明。

## 六、前端架构（一行总结）

`frontend/src/core/threads/hooks.ts` 里的 `useThreadStream` / `useSubmitThread` / `useThreads` 是整个前端的"主动脉"——它们包了 LangGraph SDK 单例（`core/api/`），所有 UI 组件订阅 thread 状态做渲染。Server Components 默认，需要交互的才 `"use client"`。`core/` 下其它子目录（artifacts/skills/mcp/memory/settings）都是为这条主动脉提供周边能力。

## 七、一图记住"它在做什么"

DeerFlow 本质上是一个 **"LangGraph 智能体 + 18 段切面 + 沙箱 + 记忆"** 的组合：

- **LangGraph** 提供图执行、checkpoint、stream 协议
- **18 个中间件** 是 DeerFlow 自己加的"切面层"，每个解决一个具体的健壮性/能力问题（错误恢复、上下文压缩、记忆、子代理限流……）
- **沙箱+技能+MCP+工具** 是 agent 的"手脚"
- **Gateway + IM Channels + 嵌入式 Client** 是同一个 agent 的三种暴露方式（HTTP/聊天/Python 直调）

> 想继续往里钻的话，建议下一步选三个之一：（a）走读 lead_agent + 中间件链，理解 agent 一轮 think/act 的完整代码路径；（b）走读 sandbox + tools，理解虚拟路径和工具拼装；（c）走读 runtime + StreamBridge，理解 SSE 协议怎么映射回 LangGraph SDK。
