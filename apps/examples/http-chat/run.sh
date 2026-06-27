#!/usr/bin/env bash
#
# http-chat 示例启动脚本
# ------------------------------------------------------------------
# - 自动探测网关地址：优先 :2026(nginx 全量栈)，回退 :8001(只起 Gateway)
# - 优先用 uv 临时虚拟环境带上 requests（--no-project，不污染系统/项目）
#   没有 uv 时回退到本地 .venv + pip
#
# 用法：
#     ./run.sh                      # 自动探测网关并运行
#     DF_BASE=http://localhost:8001 ./run.sh   # 手动指定网关
#     DF_EMAIL=a@b.com DF_PASSWORD=xxxx ./run.sh
#
# 前提：先起好 Gateway
#     ../../../scripts/dev-gateway.sh start   # → :8001
#     ../../../scripts/dev-full.sh   start   # → :2026
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── 探测可用网关 ──────────────────────────────────────────────────
_alive() { curl -s -o /dev/null -w "%{http_code}" "$1/api/v1/auth/setup-status" 2>/dev/null | grep -qE "200|429"; }

if [ -z "${DF_BASE:-}" ]; then
    if   _alive "http://localhost:2026"; then DF_BASE="http://localhost:2026"
    elif _alive "http://localhost:8001"; then DF_BASE="http://localhost:8001"
    else
        echo "✗ 没探测到运行中的网关（:2026 / :8001 都不通）。" >&2
        echo "  先启动：scripts/dev-gateway.sh start  或  scripts/dev-full.sh start" >&2
        echo "  或手动指定：DF_BASE=http://your-host:port ./run.sh" >&2
        exit 1
    fi
fi
export DF_BASE
echo "→ 使用网关: $DF_BASE"

# ── 运行：优先 uv，回退 venv+pip ─────────────────────────────────
if command -v uv >/dev/null 2>&1; then
    echo "→ uv 临时环境运行（--with requests）"
    exec uv run --no-project --with "requests>=2.31" python app.py
else
    echo "→ 未找到 uv，使用本地 .venv + pip"
    if [ ! -d .venv ]; then
        python3 -m venv .venv
        ./.venv/bin/pip install -q -r requirements.txt
    fi
    exec ./.venv/bin/python app.py
fi
