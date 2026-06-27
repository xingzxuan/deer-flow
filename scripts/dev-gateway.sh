#!/usr/bin/env bash
#
# 本地调试 Gateway 管理脚本（方式 B：只起 Gateway，端口 8001）
# ------------------------------------------------------------------
# - 用 backend/.venv 虚拟环境运行（由 uv 管理）
# - 自动加载仓库根 .env（数据库、模型 key 等）
# - 支持 start / stop / restart / status / logs / run 子命令
#
# 用法：
#     ./scripts/dev-gateway.sh start      # 后台启动（写 PID + 日志）
#     ./scripts/dev-gateway.sh stop       # 关闭
#     ./scripts/dev-gateway.sh restart    # 重启
#     ./scripts/dev-gateway.sh status     # 查看状态（PID / 端口 / 健康检查）
#     ./scripts/dev-gateway.sh logs       # 实时跟随日志（Ctrl-C 退出，不影响服务）
#     ./scripts/dev-gateway.sh run        # 前台运行（断点调试，Ctrl-C 退出）
#
# 环境变量：
#     PORT=8002        换端口（默认 8001）
#     NO_RELOAD=1      关掉热重载（断点调试更稳）
#
set -euo pipefail

# ── 定位仓库根 ────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PORT="${PORT:-8001}"
PID_FILE="$REPO_ROOT/logs/gateway-dev.pid"
LOG_FILE="$REPO_ROOT/logs/gateway-dev.log"

# ── 工具函数 ──────────────────────────────────────────────────────
_running_pid() {
    # 打印存活的服务 PID（优先 PID 文件，回退到端口探测），否则空
    if [ -f "$PID_FILE" ]; then
        local pid
        pid="$(cat "$PID_FILE" 2>/dev/null || true)"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo "$pid"; return 0
        fi
    fi
    # lsof 在无监听时返回 1，配合 pipefail+set -e 会误终止脚本 → 用 || true 兜底
    { lsof -ti tcp:"$PORT" 2>/dev/null || true; } | head -1
}

_load_env() {
    if [ -f "$REPO_ROOT/.env" ]; then
        set -a
        # shellcheck disable=SC1091
        source "$REPO_ROOT/.env"
        set +a
    else
        echo "⚠ 未找到 $REPO_ROOT/.env（数据库/模型 key 可能缺失）" >&2
    fi
}

_uvicorn_flags() {
    if [ "${NO_RELOAD:-0}" != "1" ]; then
        echo "--reload --reload-include=*.yaml --reload-include=.env --reload-exclude=*.pyc --reload-exclude=__pycache__/* --reload-exclude=sandbox/* --reload-exclude=.deer-flow/*"
    fi
}

_preflight() {
    command -v uv >/dev/null 2>&1 || { echo "✗ 未找到 uv。安装：curl -LsSf https://astral.sh/uv/install.sh | sh" >&2; exit 1; }
    mkdir -p "$REPO_ROOT/logs"
    if [ ! -d "$REPO_ROOT/backend/.venv" ]; then
        echo "→ 未发现 backend/.venv，执行 uv sync 创建虚拟环境"
        (cd "$REPO_ROOT/backend" && uv sync)
    fi
}

_wait_ready() {
    # 探测 setup-status，最多 30s；就绪返回 0
    for _ in $(seq 1 30); do
        if curl -s -o /dev/null -w "%{http_code}" "http://localhost:$PORT/api/v1/auth/setup-status" 2>/dev/null | grep -qE "200|429"; then
            return 0
        fi
        sleep 1
    done
    return 1
}

# ── 子命令 ────────────────────────────────────────────────────────
cmd_start() {
    local pid; pid="$(_running_pid)"
    if [ -n "$pid" ]; then
        echo "已在运行 (PID $pid, 端口 $PORT) —— 如需重启用：$0 restart"
        return 0
    fi
    _preflight
    _load_env
    echo "→ 后台启动 Gateway @localhost:${PORT}（venv: backend/.venv, 热重载: $([ "${NO_RELOAD:-0}" = "1" ] && echo off || echo on)）"
    # shellcheck disable=SC2086
    ( cd "$REPO_ROOT/backend" && exec env PYTHONPATH=. uv run uvicorn app.gateway.app:app \
        --host 0.0.0.0 --port "$PORT" $(_uvicorn_flags) ) > "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    if _wait_ready; then
        echo "✓ 启动成功 (PID $(cat "$PID_FILE"))"
        echo "  日志: $0 logs    状态: $0 status    关闭: $0 stop"
    else
        echo "✗ 30s 内未就绪，最后 20 行日志：" >&2
        tail -n 20 "$LOG_FILE" >&2
        return 1
    fi
}

cmd_stop() {
    local pid; pid="$(_running_pid)"
    if [ -z "$pid" ]; then
        echo "未在运行"
        rm -f "$PID_FILE"
        return 0
    fi
    echo "→ 关闭 Gateway (PID $pid)"
    # 优雅终止整组进程（uv → uvicorn → reloader 子进程）
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 10); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
    # 兜底：按端口清残留（reload worker 偶尔不随父进程退出）
    lsof -ti tcp:"$PORT" 2>/dev/null | xargs kill -9 2>/dev/null || true
    rm -f "$PID_FILE"
    echo "✓ 已停止，端口 $PORT 释放"
}

cmd_status() {
    local pid; pid="$(_running_pid)"
    if [ -z "$pid" ]; then
        echo "● Gateway: 已停止 (端口 $PORT 空闲)"
        return 1
    fi
    echo "● Gateway: 运行中"
    echo "  PID:   $pid"
    echo "  端口:  $PORT"
    local code
    code="$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$PORT/api/v1/auth/setup-status" 2>/dev/null || echo "000")"
    case "$code" in
        200|429) echo "  健康:  ✓ HTTP $code (REST API 响应中)";;
        000)     echo "  健康:  ⚠ 端口占用但 HTTP 无响应（可能仍在启动）";;
        *)       echo "  健康:  ⚠ HTTP $code";;
    esac
    echo "  日志:  $LOG_FILE"
}

cmd_logs() {
    [ -f "$LOG_FILE" ] || { echo "暂无日志文件：$LOG_FILE"; return 1; }
    echo "→ 跟随日志（Ctrl-C 退出，不影响服务）：$LOG_FILE"
    tail -n 50 -f "$LOG_FILE"
}

cmd_run() {
    # 前台运行：日志直出终端，适合 IDE 断点 / 看实时堆栈
    local pid; pid="$(_running_pid)"
    [ -n "$pid" ] && { echo "已有后台实例在运行 (PID $pid)，先 $0 stop" >&2; exit 1; }
    _preflight
    _load_env
    echo "→ 前台运行 @localhost:${PORT}（Ctrl-C 退出）"
    cd "$REPO_ROOT/backend"
    # shellcheck disable=SC2046,SC2086
    exec env PYTHONPATH=. uv run uvicorn app.gateway.app:app \
        --host 0.0.0.0 --port "$PORT" $(_uvicorn_flags)
}

# ── 分发 ──────────────────────────────────────────────────────────
case "${1:-status}" in
    start)         cmd_start ;;
    stop)          cmd_stop ;;
    restart)       cmd_stop; echo; cmd_start ;;
    status|"")     cmd_status ;;
    logs)          cmd_logs ;;
    run)           cmd_run ;;
    -h|--help|help)
        sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//' ;;
    *)
        echo "未知命令: $1" >&2
        echo "可用: start | stop | restart | status | logs | run" >&2
        exit 1 ;;
esac
