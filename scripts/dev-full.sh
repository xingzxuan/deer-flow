#!/usr/bin/env bash
#
# 本地调试全量栈管理脚本（方式 A：Gateway + Frontend + Nginx）
# ------------------------------------------------------------------
# 复用仓库已有的 scripts/serve.sh（处理 config-upgrade / postgres extras /
# nginx 临时目录 / 依赖同步 / 端口等待），在其守护进程模式之上补齐
# status 和 logs，子命令风格与 scripts/dev-gateway.sh 保持一致。
#
# 服务与端口：
#     Gateway   localhost:8001   (REST API + agent runtime)
#     Frontend  localhost:3000   (Next.js)
#     Nginx     localhost:2026   (统一入口 / 反向代理)  ← 浏览器访问这个
#
# 用法：
#     ./scripts/dev-full.sh start            # 后台启动整套（首次会装依赖）
#     ./scripts/dev-full.sh stop             # 关闭整套
#     ./scripts/dev-full.sh restart          # 重启整套
#     ./scripts/dev-full.sh status           # 三个服务的端口 / 健康检查
#     ./scripts/dev-full.sh logs [服务]      # 跟随日志，默认三个一起；可指定 gateway|frontend|nginx
#     ./scripts/dev-full.sh run              # 前台运行（= make dev，Ctrl-C 全停，gateway 带热重载）
#
# 环境变量：
#     SKIP_INSTALL=1   跳过依赖安装，重启更快
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVE="$REPO_ROOT/scripts/serve.sh"

# 服务清单：名称:端口:日志文件:健康检查路径（空=只查端口）
SERVICES=(
    "Gateway:8001:gateway.log:/api/v1/auth/setup-status"
    "Frontend:3000:frontend.log:/"
    "Nginx:2026:nginx.log:/"
)

# ── 工具函数 ──────────────────────────────────────────────────────
_port_pid() { { lsof -ti tcp:"$1" 2>/dev/null || true; } | head -1; }

_any_running() {
    for svc in "${SERVICES[@]}"; do
        local port="${svc#*:}"; port="${port%%:*}"
        [ -n "$(_port_pid "$port")" ] && return 0
    done
    return 1
}

_serve_flags() {
    # serve.sh 守护模式启动；可选跳过依赖安装
    local flags="--dev --daemon"
    [ "${SKIP_INSTALL:-0}" = "1" ] && flags="$flags --skip-install"
    echo "$flags"
}

# ── 子命令 ────────────────────────────────────────────────────────
cmd_start() {
    if _any_running; then
        echo "检测到已有服务在运行 —— 如需重启用：$0 restart"
        cmd_status || true
        return 0
    fi
    echo "→ 后台启动全量栈（serve.sh $(_serve_flags)）"
    [ "${SKIP_INSTALL:-0}" = "1" ] || echo "  首次启动会执行 uv sync + pnpm install，可能较慢；重启可加 SKIP_INSTALL=1"
    # shellcheck disable=SC2046
    bash "$SERVE" $(_serve_flags)
    echo
    cmd_status || true
}

cmd_stop() {
    if ! _any_running; then
        echo "未在运行"
        return 0
    fi
    echo "→ 关闭全量栈（serve.sh --stop）"
    bash "$SERVE" --stop
}

cmd_status() {
    local all_up=0
    printf "%-10s %-7s %-9s %s\n" "服务" "端口" "状态" "健康"
    printf "%-10s %-7s %-9s %s\n" "----" "----" "----" "----"
    for svc in "${SERVICES[@]}"; do
        local name port log path rest
        name="${svc%%:*}"; rest="${svc#*:}"
        port="${rest%%:*}"; rest="${rest#*:}"
        log="${rest%%:*}"; path="${rest#*:}"
        local pid; pid="$(_port_pid "$port")"
        if [ -z "$pid" ]; then
            printf "%-10s %-7s %-9s %s\n" "$name" "$port" "✗ 停止" "-"
            all_up=1
        else
            local code="-"
            if [ -n "$path" ]; then
                code="$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$port$path" 2>/dev/null || echo 000)"
                case "$code" in 200|429|301|302|307) code="✓ HTTP $code";; 000) code="⚠ 无响应";; *) code="⚠ HTTP $code";; esac
            fi
            printf "%-10s %-7s %-9s %s\n" "$name" "$port" "● 运行 ($pid)" "$code"
        fi
    done
    if [ "$all_up" = "0" ]; then
        echo
        echo "  🌐 统一入口: http://localhost:2026"
    fi
    return "$all_up"
}

cmd_logs() {
    local target="${1:-}"
    cd "$REPO_ROOT"
    local files=()
    if [ -n "$target" ]; then
        local f="logs/${target}.log"
        [ -f "$f" ] || { echo "暂无日志：$f（可选 gateway|frontend|nginx）" >&2; return 1; }
        files=("$f")
    else
        for svc in "${SERVICES[@]}"; do
            local log; log="${svc#*:}"; log="${log#*:}"; log="${log%%:*}"
            [ -f "logs/$log" ] && files+=("logs/$log")
        done
        [ ${#files[@]} -gt 0 ] || { echo "暂无日志文件（logs/ 为空）" >&2; return 1; }
    fi
    echo "→ 跟随日志（Ctrl-C 退出，不影响服务）：${files[*]}"
    tail -n 30 -f "${files[@]}"
}

cmd_run() {
    if _any_running; then
        echo "已有后台实例在运行，先 $0 stop" >&2
        exit 1
    fi
    echo "→ 前台运行全量栈（= make dev，Ctrl-C 全停）"
    exec bash "$SERVE" --dev
}

# ── 分发 ──────────────────────────────────────────────────────────
case "${1:-status}" in
    start)       cmd_start ;;
    stop)        cmd_stop ;;
    restart)     cmd_stop; echo; SKIP_INSTALL="${SKIP_INSTALL:-1}" cmd_start ;;
    status|"")   cmd_status ;;
    logs)        shift || true; cmd_logs "${1:-}" ;;
    run)         cmd_run ;;
    -h|--help|help)
        sed -n '2,33p' "$0" | sed 's/^# \{0,1\}//' ;;
    *)
        echo "未知命令: $1" >&2
        echo "可用: start | stop | restart | status | logs [服务] | run" >&2
        exit 1 ;;
esac
