#!/bin/bash
# Prism Native — 本地开发依赖准备。应用发布使用独立的受控发布流程。
# 用法: bash install.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${PRISM_VENV_DIR:-$APP_DIR/.venv}"
PYTHON_BIN="${PRISM_PYTHON:-python3}"

if [ "${PRISM_ENVIRONMENT:-local}" = "production" ]; then
    echo "此脚本仅准备本地开发环境；生产发布请使用受控发布流程。" >&2
    exit 1
fi

"$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else "需要 Python 3.12 或更新版本")'
echo "[1/2] 准备独立 Python 环境..."
"$PYTHON_BIN" -m venv "$VENV_DIR"

echo "[2/2] 安装 Native 应用依赖..."
"$VENV_DIR/bin/python" -m pip install -r "$APP_DIR/requirements.txt"
if [ "${PRISM_INSTALL_MARKET:-0}" = "1" ]; then
    "$VENV_DIR/bin/python" -m pip install -r "$APP_DIR/requirements-extra.txt"
fi

echo "依赖准备完成。请参考 .env.example，将模型配置加载到应用进程的环境中。"
echo "本地启动: $VENV_DIR/bin/python -m uvicorn server.main:app --host 127.0.0.1 --port 8088"
echo "账号可用 scripts/create_user.py 创建；此脚本不会启动服务或完成发布验收。"
