#!/bin/bash
# Prism — 一键部署
# 用法:
#   git clone --recurse-submodules <repo-url> stock-analyzer
#   ADMIN_USER=admin ADMIN_PASS=changeme bash install.sh
set -e

APP_DIR="/home/ubuntu/stock-analyzer"
VENV_DIR="/home/ubuntu/stock-venv"
DATA_DIR="${STOCK_DATA_DIR:-/home/ubuntu/stock-data}"

echo "╔══════════════════════════════════════╗"
echo "║   Prism — AI 投资分析 部署          ║"
echo "╚══════════════════════════════════════╝"

# ── 0. 预检 ──
if [ ! -f "$APP_DIR/.env" ] && [ -z "$LLM_API_KEY" ]; then
    echo "⚠ 请先创建 .env 文件或设置 LLM_API_KEY 环境变量"
    echo "  cp .env.example .env && nano .env"
    exit 1
fi

# ── 1. 系统依赖 ──
echo "[1/5] 系统包..."
sudo apt update -qq
sudo apt install -y python3.12-venv nginx

if ! command -v node &>/dev/null; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
    sudo apt install -y nodejs
fi

# ── 2. Claude Code CLI ──
echo "[2/5] Claude Code CLI..."
if ! command -v claude &>/dev/null; then
    npm install -g @anthropic-ai/claude-code
fi

# ── 3. Python venv ──
echo "[3/5] Python venv..."
python3 -m venv "$VENV_DIR" 2>/dev/null || true
source "$VENV_DIR/bin/activate"
pip install -r "$APP_DIR/requirements.txt"

# ── 4. Skills ──
echo "[4/5] Skills..."
SKILLS_DIR="$HOME/.claude/skills"
mkdir -p "$SKILLS_DIR"

# 核心技能: 锁定版本，升级时更新 commit hash 即可
install_skill() {
    local name="$1" repo="$2" ref="$3"
    if [ -d "$SKILLS_DIR/$name" ]; then
        echo "  → $name 已安装 (版本: $ref)"
        cd "$SKILLS_DIR/$name" && git fetch origin && git checkout "$ref" 2>/dev/null || true
    else
        git clone "$repo" "$SKILLS_DIR/$name" && cd "$SKILLS_DIR/$name" && git checkout "$ref" && echo "  ✓ $name @ $ref"
    fi
}

install_skill serenity-bottleneck-hunter https://github.com/Mrjie7205/serenity-bottleneck-hunter.git c480f6d
install_skill serenity-skill            https://github.com/ZadAnthony/serenity-skill.git            ab7fb2c

# 可选: 金融数据 skills（取消注释即可安装）
# echo "  → 安装 Vibe-Trading..."
# npx skills add https://github.com/HKUDS/Vibe-Trading
# echo "  → 安装 claude-for-financial-services-cn..."
# claude plugin marketplace add jwangkun/claude-for-financial-services-cn

# Claude Code 全局配置（从模板生成，注入真实 key）
mkdir -p "$HOME/.claude"
if [ -f "$APP_DIR/.env" ]; then
    export $(grep -v '^#' "$APP_DIR/.env" | xargs)
fi
if [ -n "$LLM_API_KEY" ]; then
    sed "s/sk-your-key-here/${LLM_API_KEY//\//\\/}/g" \
        "$APP_DIR/config/claude-settings.example.json" \
        > "$HOME/.claude/settings.json"
    echo "  ✓ Claude Code 配置已生成"
else
    echo "  ⚠ 未设置 LLM_API_KEY，跳过 Claude Code 配置"
fi

# ── 5. 用户 + 服务 ──
echo "[5/5] 用户 + 服务..."
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASS="${ADMIN_PASS:-}"
if [ -z "$ADMIN_PASS" ]; then
    ADMIN_PASS=$(openssl rand -base64 12 2>/dev/null || python3 -c "import secrets; print(secrets.token_urlsafe(12))")
    echo "  ⚠ 未设置 ADMIN_PASS，已生成随机密码: $ADMIN_PASS"
    echo "  请妥善保存！"
fi
"$VENV_DIR/bin/python" "$APP_DIR/scripts/create_user.py" --username "$ADMIN_USER" --password "$ADMIN_PASS"

sudo cp "$APP_DIR/config/stock-analyzer.service" /etc/systemd/system/
sudo cp "$APP_DIR/config/nginx-site.conf" /etc/nginx/sites-available/stock-analyzer
sudo ln -sf /etc/nginx/sites-available/stock-analyzer /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

sudo systemctl daemon-reload
sudo systemctl enable stock-analyzer
sudo systemctl restart stock-analyzer

sleep 2
echo ""
echo "╔══════════════════════════════════════╗"
echo "║   ✅ Prism 部署完成                 ║"
echo "║   地址: http://$(hostname -I | awk '{print $1}')         ║"
echo "║   用户: $ADMIN_USER                  ║"
echo "╚══════════════════════════════════════╝"

bash "$APP_DIR/scripts/verify.sh"
