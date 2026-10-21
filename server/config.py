"""Prism — 应用配置"""

import contextlib
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
    # Restrict permissions: only the owner can read the secret. This
    # is best-effort on POSIX; ignored on Windows. In containers,
    # run as a non-root user so the file isn't world-readable.
    with contextlib.suppress(OSError):
        os.chmod(SECRET_KEY_FILE, 0o600)

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
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", os.path.expanduser("~/.npm-global/bin/claude"))
SKILLS_HOME = Path(os.environ.get("CLAUDE_SKILLS_HOME", Path.home() / ".claude" / "skills"))

_DEFAULT_SKILL_FALLBACK_ROOTS = [
    PROJECT_ROOT / "skills",
    Path.home() / "skills",
]
_ENV_SKILL_FALLBACK_ROOTS = [
    Path(p.strip()) for p in os.environ.get("CLAUDE_SKILL_FALLBACKS", "").split(os.pathsep) if p.strip()
]
SKILL_FALLBACK_ROOTS = []
for _root in [*_ENV_SKILL_FALLBACK_ROOTS, *_DEFAULT_SKILL_FALLBACK_ROOTS]:
    if _root not in SKILL_FALLBACK_ROOTS and _root != SKILLS_HOME:
        SKILL_FALLBACK_ROOTS.append(_root)

# ── Skill 映射（Claude Code 加载时用目录名查找） ──
SKILL_MAP = {
    "prism-research": {"dir": "prism-research", "name": "Prism研究"},
    "bottleneck-hunter": {"dir": "serenity-bottleneck-hunter", "name": "瓶颈猎手"},
    "serenity": {"dir": "serenity-skill", "name": "Serenity 卡点分析"},
    "cn": {"dir": "force-chinese", "name": "强制中文输出"},
}

# ── 速率限制 ──
RATE_CHAT_PER_MIN = int(os.environ.get("RATE_CHAT_PER_MIN", "20"))
RATE_LOGIN_PER_MIN = int(os.environ.get("RATE_LOGIN_PER_MIN", "10"))
RATE_EXPLAIN_PER_MIN = int(os.environ.get("RATE_EXPLAIN_PER_MIN", "10"))
CHAT_STREAM_HEARTBEAT_SECONDS = int(os.environ.get("PRISM_CHAT_STREAM_HEARTBEAT", "20"))
CHAT_STREAM_IDLE_TIMEOUT_SECONDS = int(os.environ.get("PRISM_CHAT_STREAM_IDLE_TIMEOUT", "300"))

# ── Admin ──
# Comma-separated list of admin usernames (e.g. "admin,alice").
# Default is strict: only ADMIN_USER (or "admin" when unset) can access
# the management console unless ADMIN_USERS explicitly overrides it.
_DEFAULT_ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
_ADMIN_RAW = os.environ.get("ADMIN_USERS", _DEFAULT_ADMIN_USER)
ADMIN_USERS = {u.strip() for u in _ADMIN_RAW.split(",") if u.strip()}

# ── SQLite ──
SQLITE_MMAP_SIZE = int(os.environ.get("PRISM_SQLITE_MMAP_SIZE", "268435456"))  # 256 MiB

# ── Cookie 安全 ──
# secure=True 要求 HTTPS 才会发送 cookie。生产环境必须开启。
# COOKIE_SAMESITE 默认为 "strict"（最强 CSRF 防护）。开发环境可设 "lax"。
COOKIE_SECURE = os.environ.get("PRISM_COOKIE_SECURE", "0").lower() in ("1", "true", "yes")
COOKIE_SAMESITE = os.environ.get("PRISM_COOKIE_SAMESITE", "strict").lower()
# CSRF 防护 — 状态变更请求必须带 X-Requested-With: XMLHttpRequest 头。
# 浏览器跨站请求不会自动加这个头，所以可以有效阻断 CSRF。
CSRF_REQUIRED_HEADER = "X-Requested-With"
CSRF_HEADER_VALUE = "XMLHttpRequest"

# ── 允许的源（CSRF Origin 校验）──
# 留空表示不做 Origin 校验（仅靠 samesite + X-Requested-With）。
# 部署时建议填具体域名，如 "https://prism.example.com,https://www.example.com"
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("PRISM_ALLOWED_ORIGINS", "").split(",") if o.strip()]

# ── CORS ──
# Same-origin deployments don't need CORS, but enable it here for
# reverse-proxy frontends and development with a separate origin.
# PRISM_CORS_ORIGINS is a comma-separated list of allowed origins;
# empty disables CORS (the browser will block cross-origin requests).
CORS_ORIGINS = [o.strip() for o in os.environ.get("PRISM_CORS_ORIGINS", "").split(",") if o.strip()]
CORS_ALLOW_CREDENTIALS = os.environ.get("PRISM_CORS_CREDENTIALS", "0").lower() in ("1", "true", "yes")

# ── 日志 ──
LOG_FILE = os.environ.get("STOCK_LOG_FILE", str(PROJECT_ROOT / "app.log"))

# ── 行情数据源 ──
# 逗号分隔的 provider 优先级队列，缺包的 provider 自动跳过。
# akshare 在前：实测它在当前部署网络里最稳（走 sina，hq/finance.sina
# 均可达，且能穿透本地代理）；baostock 的自有 TCP 端口 baostock.com:10087
# 在本机被拦，作为兜底，待其可达时自动顶上。
# 链路对单个 provider 的任意异常都做 failover，故顺序只影响首试延迟。
MARKET_PROVIDERS = [
    p.strip()
    for p in os.environ.get("PRISM_MARKET_PROVIDERS", "tencent,akshare,baostock").split(",")
    if p.strip()
]
# 各数据类型的 TTL（秒）。QUOTE 短、KLINE 中、FUNDAMENTALS 长。
QUOTE_TTL = int(os.environ.get("PRISM_QUOTE_TTL", "60"))
KLINE_TTL = int(os.environ.get("PRISM_KLINE_TTL", "300"))
FUNDAMENTALS_TTL = int(os.environ.get("PRISM_FUNDAMENTALS_TTL", "86400"))
# Provider 失败后的失败缓存窗口（秒），避免反复重试死掉的源。
PROVIDER_FAIL_CACHE_SECONDS = int(os.environ.get("PRISM_PROVIDER_FAIL_CACHE", "30"))
# Provider 单次调用超时（秒）。
AKSHARE_TIMEOUT = int(os.environ.get("PRISM_AKSHARE_TIMEOUT", "10"))
BAOSTOCK_TIMEOUT = int(os.environ.get("PRISM_BAOSTOCK_TIMEOUT", "10"))

# ── astock 分析引擎 ──
# TradingAgents-astock 项目路径（默认与本项目同级）
ASTOCK_PROJECT = os.environ.get("PRISM_ASTOCK_PROJECT", str(PROJECT_ROOT.parent / "TradingAgents-astock"))
# LLM 配置：quick 用于分析师/辩论，deep 用于研判/决策
ASTOCK_LLM_QUICK = os.environ.get("ASTOCK_LLM_QUICK", "deepseek-chat")
ASTOCK_LLM_DEEP = os.environ.get("ASTOCK_LLM_DEEP", "deepseek-chat")
ASTOCK_LLM_PROVIDER = os.environ.get("ASTOCK_LLM_PROVIDER", "deepseek")
ASTOCK_LLM_BASE_URL = os.environ.get("ASTOCK_LLM_BASE_URL", os.environ.get("ANTHROPIC_BASE_URL", ""))
ASTOCK_LLM_API_KEY = os.environ.get("ASTOCK_LLM_API_KEY", os.environ.get("ANTHROPIC_API_KEY", ""))
ASTOCK_DATA_VENDOR = os.environ.get("ASTOCK_DATA_VENDOR", "a_stock")
ASTOCK_MAX_DEBATE_ROUNDS = int(os.environ.get("ASTOCK_MAX_DEBATE_ROUNDS", "1"))
ASTOCK_OUTPUT_LANGUAGE = os.environ.get("ASTOCK_OUTPUT_LANGUAGE", "Chinese")
ASTOCK_RESULTS_DIR = os.environ.get("ASTOCK_RESULTS_DIR", str(DATA_DIR / "analysis_reports"))
ASTOCK_HARD_TIMEOUT_SECONDS = int(os.environ.get("PRISM_ASTOCK_HARD_TIMEOUT", "1800"))
ASTOCK_CACHE_TTL_SECONDS = int(os.environ.get("PRISM_ASTOCK_CACHE_TTL", "86400"))
ASTOCK_FAST_ANALYSTS = [
    a.strip()
    for a in os.environ.get("PRISM_ASTOCK_FAST_ANALYSTS", "market,fundamentals").split(",")
    if a.strip()
]
ASTOCK_DEEP_ANALYSTS = [
    a.strip()
    for a in os.environ.get(
        "PRISM_ASTOCK_DEEP_ANALYSTS",
        "market,social,news,fundamentals,policy,hot_money,lockup",
    ).split(",")
    if a.strip()
]
ASTOCK_FAST_DEBATE_ROUNDS = int(os.environ.get("PRISM_ASTOCK_FAST_DEBATE_ROUNDS", "0"))
ASTOCK_FAST_RISK_ROUNDS = int(os.environ.get("PRISM_ASTOCK_FAST_RISK_ROUNDS", "0"))
ASTOCK_NO_PROXY_DOMAINS = [
    d.strip()
    for d in os.environ.get(
        "PRISM_ASTOCK_NO_PROXY",
        "eastmoney.com,push2.eastmoney.com,quote.eastmoney.com,"
        "ths.com,thsi.cn,10jqka.com.cn,cls.cn,finance.sina.com.cn,"
        "hq.sinajs.cn,sina.com.cn",
    ).split(",")
    if d.strip()
]

# Official HKUDS/Vibe-Trading sidecar. It is intentionally loopback-only in
# production; Prism owns authentication and persists the user-facing record.
VIBE_ENGINE_URL = os.environ.get("PRISM_VIBE_ENGINE_URL", "http://127.0.0.1:8899").rstrip("/")
VIBE_ENGINE_TIMEOUT_SECONDS = int(os.environ.get("PRISM_VIBE_ENGINE_TIMEOUT", "1200"))
VIBE_ENGINE_REVISION = os.environ.get("PRISM_VIBE_ENGINE_REVISION", "d456025")
VIBE_ENGINE_VERSION = os.environ.get("PRISM_VIBE_ENGINE_VERSION", "0.1.11")

# The stdio bridge may only call this same Prism server over numeric loopback.
CAPABILITY_INTERNAL_URL = os.environ.get("PRISM_INTERNAL_API_URL", "http://127.0.0.1:8088").rstrip("/")
CAPABILITY_TOKEN_TTL = max(60, min(3600, int(os.environ.get("PRISM_CAPABILITY_TOKEN_TTL", "1800"))))
