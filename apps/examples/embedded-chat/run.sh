#!/usr/bin/env bash
#
# embedded-chat 示例启动脚本
# ------------------------------------------------------------------
# 内嵌 SDK 模式：进程内直接 import deerflow.*，不需要起任何服务。
# 必须在 backend 的 uv 虚拟环境里跑（才能解析 deerflow-harness / app 包），
# 本脚本自动 cd 到 backend 并用 uv run 启动。
#
# 用法：
#     ./run.sh
#
# 前提：
#     - 已 `cd backend && uv sync`（或跑过任意一个 dev 脚本，venv 已建好）
#     - config.yaml 里配好至少一个可用模型 + API key
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
APP="$SCRIPT_DIR/app.py"
BACKEND="$REPO_ROOT/backend"

# ── 前置检查 ──────────────────────────────────────────────────────
command -v uv >/dev/null 2>&1 || { echo "✗ 未找到 uv。安装：curl -LsSf https://astral.sh/uv/install.sh | sh" >&2; exit 1; }
[ -f "$REPO_ROOT/config.yaml" ] || echo "⚠ 未找到 $REPO_ROOT/config.yaml —— 没有可用模型会启动失败" >&2

if [ ! -d "$BACKEND/.venv" ]; then
    echo "→ 未发现 backend/.venv，执行 uv sync"
    (cd "$BACKEND" && uv sync)
fi

# ── 加载 .env（模型 key / 数据库等）──────────────────────────────
if [ -f "$REPO_ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$REPO_ROOT/.env"
    set +a
fi

# ── 在 backend uv 环境里运行（config.yaml 解析依赖运行目录为 backend/）──
echo "→ 在 backend uv 环境中运行 embedded-chat"
cd "$BACKEND"
exec env PYTHONPATH=. uv run python "$APP"
