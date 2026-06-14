"""Prism — 应用配置"""
import os
import secrets
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── 目录 ──
DATA_DIR = Path(os.environ.get("STOCK_DATA_DIR", PROJECT_ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATE_DIR = PROJECT_ROOT / "templates"
STATIC_DIR = PROJECT_ROOT / "static"

# ── 密钥（持久化到 data/，不会进 git） ──
SECRET_KEY_FILE = DATA_DIR / "secret_key"
if SECRET_KEY_FILE.exists():
    SECRET_KEY = SECRET_KEY_FILE.read_text().strip()
else:
    SECRET_KEY = secrets.token_hex(32)
    SECRET_KEY_FILE.write_text(SECRET_KEY)

# ── LLM API Keys（每个平台独立配置）──
# 兼容旧变量 LLM_API_KEY 作为 fallback
_LEGACY_KEY = os.environ.get("LLM_API_KEY", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", _LEGACY_KEY)
MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY", _LEGACY_KEY)

# ── 模型列表 ──
# 每个模型需配置：provider 的 Anthropic 兼容端点 + model ID
# name 是前端显示的标签
# api_key 优先使用平台专属 key，fallback 到 LLM_API_KEY（兼容旧配置）
MODELS = {
    # ── DeepSeek ──
    "deepseek-v4": {
        "name": "DeepSeek V4 Pro",
        "base_url": "https://api.deepseek.com/anthropic",
        "api_key": DEEPSEEK_API_KEY,
        "model": "deepseek-v4-pro",
    },
    "deepseek-v4-flash": {
        "name": "DeepSeek V4 Flash",
        "base_url": "https://api.deepseek.com/anthropic",
        "api_key": DEEPSEEK_API_KEY,
        "model": "deepseek-v4-flash",
    },
    # ── MiniMax (Anthropic 兼容端点) ──
    "minimax-m3": {
        "name": "MiniMax M3",
        "base_url": "https://api.minimax.io/anthropic/v1/",
        "api_key": MINIMAX_API_KEY,
        "model": "MiniMax-M3",
    },
    "minimax-m2.7": {
        "name": "MiniMax M2.7",
        "base_url": "https://api.minimax.io/anthropic/v1/",
        "api_key": MINIMAX_API_KEY,
        "model": "MiniMax-M2.7",
    },
    "minimax-m2.7-fast": {
        "name": "MiniMax M2.7 Fast",
        "base_url": "https://api.minimax.io/anthropic/v1/",
        "api_key": MINIMAX_API_KEY,
        "model": "MiniMax-M2.7-highspeed",
    },
}
DEFAULT_MODEL = "minimax-m3"

# ── Claude Code CLI ──
CLAUDE_BIN = os.environ.get("CLAUDE_BIN",
    os.path.expanduser("~/.npm-global/bin/claude"))
SKILLS_HOME = Path(os.environ.get("CLAUDE_SKILLS_HOME",
    Path.home() / ".claude" / "skills"))

# ── Skill 映射（Claude Code 加载时用目录名查找） ──
SKILL_MAP = {
    "bottleneck-hunter": {"dir": "serenity-bottleneck-hunter", "name": "瓶颈猎手"},
    "serenity":           {"dir": "serenity-skill",            "name": "Serenity 卡点分析"},
    "cn":                 {"dir": "force-chinese",             "name": "强制中文输出"},
}

# ── 速率限制 ──
RATE_CHAT_PER_MIN = int(os.environ.get("RATE_CHAT_PER_MIN", "20"))
RATE_LOGIN_PER_MIN = int(os.environ.get("RATE_LOGIN_PER_MIN", "10"))

# ── 日志 ──
LOG_FILE = os.environ.get("STOCK_LOG_FILE", str(PROJECT_ROOT / "app.log"))
