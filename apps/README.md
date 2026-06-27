# apps/ — 基于 DeerFlow 的应用

这个目录用于存放**消费 DeerFlow 智能体能力的上层应用**。每个应用一个子文件夹。

## 为什么放在这里

DeerFlow 的代码有一条严格的依赖方向（见根 `CLAUDE.md`）：

```
backend/packages/harness/deerflow/   ← 可发布的 Agent 框架（deerflow.*）
backend/app/                         ← Gateway / IM 通道（app.*）
apps/                                ← 你的应用（消费 deerflow，不反向依赖）  ← 本目录
```

规则：**app 可以依赖 deerflow，deerflow 不能依赖 app / apps**。本目录放在 `backend/` 之外、与 `frontend/` 平级，天然符合这条边界。

## 两种集成模式

| 模式 | 适用场景 | 怎么连 | 示例 |
|---|---|---|---|
| **HTTP Gateway**（REST+SSE） | 上层是别的服务 / 多语言 | 调 `http://localhost:2026/api/*` | [`examples/http-chat/`](examples/http-chat/) |
| **内嵌 DeerFlowClient** | 上层本身是 Python，进程内直接当 SDK 调 | `from deerflow.client import DeerFlowClient` | [`examples/embedded-chat/`](examples/embedded-chat/) |

> 还有第三种：LangGraph SDK（`langgraph_sdk.get_client(url=".../api")`，graph id `lead_agent`），用于接入 LangGraph 生态工具链。需要的话照 HTTP 示例的鉴权流程拿 cookie 即可。

### 运行示例（每个示例自带 run.sh）

```bash
# ① HTTP 模式：需要先起 Gateway（dev-gateway 或 dev-full 都行）
#    run.sh 会自动探测网关地址：优先 :2026，回退 :8001
./apps/examples/http-chat/run.sh
DF_BASE=http://localhost:8001 ./apps/examples/http-chat/run.sh   # 也可手动指定

# ② 内嵌模式：不需要起任何服务，run.sh 自动进 backend uv 环境运行
./apps/examples/embedded-chat/run.sh
```

http-chat 的 `run.sh` 优先用 `uv run --no-project --with requests`（临时环境，不污染系统），没有 uv 才回退到本地 `.venv` + pip；可用 `DF_BASE` / `DF_EMAIL` / `DF_PASSWORD` 覆盖。embedded-chat 的 `run.sh` 自动定位 `backend/`、加载 `.env` 后用 `uv run` 启动，依赖 `config.yaml` 里有可用模型。

## 前置：先把 DeerFlow 跑起来

在**仓库根目录**：

```bash
make dev          # 起 Gateway(8001) + 前端(3000) + nginx(2026)，统一入口 http://localhost:2026
```

确保 `config.yaml` 里至少配了一个可用模型 + API key。

### 本地调试脚本（推荐）

`make dev` 是前台阻塞运行。日常调试更顺手的是仓库根 `scripts/` 下两个生命周期脚本，子命令统一为 `start / stop / restart / status / logs / run`：

| 脚本 | 起什么 | 入口 | 适合 |
|---|---|---|---|
| `scripts/dev-gateway.sh` | 只起 Gateway | `http://localhost:8001` | 调后端 API / 接入示例，起得快 |
| `scripts/dev-full.sh` | Gateway + 前端 + nginx | `http://localhost:2026` | 连前端一起调，完整体验 |

```bash
./scripts/dev-gateway.sh start          # 后台启动，等就绪后返回
./scripts/dev-gateway.sh status         # PID / 端口 / HTTP 健康检查
./scripts/dev-gateway.sh logs           # tail -f 跟随日志（不影响服务）
./scripts/dev-gateway.sh stop

./scripts/dev-full.sh start             # 全量栈后台启动（首次装依赖）
SKIP_INSTALL=1 ./scripts/dev-full.sh start   # 跳过依赖安装，重启更快
./scripts/dev-full.sh status            # 三服务一览
./scripts/dev-full.sh run               # 前台运行（= make dev，gateway 带热重载）
```

环境变量：`PORT=`(换端口)、`NO_RELOAD=1`(关热重载，断点更稳)、`SKIP_INSTALL=1`(全量栈跳过装依赖)。

## 鉴权（HTTP 模式必读）

Gateway 是 **fail-closed** 的——除少数公开路径外所有请求都要带会话 cookie：

1. `GET /api/v1/auth/setup-status` → 是否还没管理员
2. 首次 `POST /api/v1/auth/initialize`（JSON `{email,password}`）建第一个管理员；之后 `POST /api/v1/auth/login/local`（**表单** `username`=邮箱 + `password`）
3. 成功后 Session 里有 `access_token`(HttpOnly) + `csrf_token` 两个 cookie
4. **所有写请求**（POST/PUT/DELETE/PATCH）必须带 `X-CSRF-Token` 头 = `csrf_token` 值

> 多租户：当前 `docs/multi-tenant-redesign` 分支的 API Key 鉴权中间件尚未接入，外部系统暂时只能走会话 cookie。等 `Authorization: Bearer dfk_live_...` 落地后再补无人值守接入。

## 新建一个应用

```bash
mkdir apps/my-app
# 放你的代码；HTTP 模式参照 examples/http-chat，内嵌模式参照 examples/embedded-chat
```
