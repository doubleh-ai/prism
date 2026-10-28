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
_PREFERRED_MODEL = os.environ.get("PRISM_DEFAULT_MODEL", "minimax-m3")
DEFAULT_MODEL = next(
    (key for key in [_PREFERRED_MODEL, *MODELS] if key in MODELS and MODELS[key].get("api_key")),
    "minimax-m3",
)

# ── 速率限制 ──
RATE_CHAT_PER_MIN = int(os.environ.get("RATE_CHAT_PER_MIN", "20"))
RATE_LOGIN_PER_MIN = int(os.environ.get("RATE_LOGIN_PER_MIN", "10"))
RATE_EXPLAIN_PER_MIN = int(os.environ.get("RATE_EXPLAIN_PER_MIN", "10"))
CHAT_STREAM_HEARTBEAT_SECONDS = int(os.environ.get("PRISM_CHAT_STREAM_HEARTBEAT", "20"))
CHAT_STREAM_IDLE_TIMEOUT_SECONDS = int(os.environ.get("PRISM_CHAT_STREAM_IDLE_TIMEOUT", "300"))

RESEARCH_SOURCE_HOSTS = tuple(
    h.strip().lower() for h in os.environ.get("PRISM_RESEARCH_SOURCE_HOSTS", "").split(",") if h.strip()
)

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

# Official HKUDS/Vibe-Trading sidecar. It is intentionally loopback-only in
# production; Prism owns authentication and persists the user-facing record.
VIBE_ENGINE_URL = os.environ.get("PRISM_VIBE_ENGINE_URL", "http://127.0.0.1:8899").rstrip("/")
VIBE_ENGINE_TIMEOUT_SECONDS = int(os.environ.get("PRISM_VIBE_ENGINE_TIMEOUT", "1200"))
VIBE_ENGINE_REVISION = os.environ.get("PRISM_VIBE_ENGINE_REVISION", "d456025")
VIBE_ENGINE_VERSION = os.environ.get("PRISM_VIBE_ENGINE_VERSION", "0.1.11")

# The stdio bridge may only call this same Prism server over numeric loopback.
CAPABILITY_INTERNAL_URL = os.environ.get("PRISM_INTERNAL_API_URL", "http://127.0.0.1:8088").rstrip("/")
CAPABILITY_TOKEN_TTL = max(60, min(3600, int(os.environ.get("PRISM_CAPABILITY_TOKEN_TTL", "1800"))))

# Operator-supplied label only; never treated as proof of production deployment.
_OPERATIONS_ENVIRONMENT = os.environ.get("PRISM_ENVIRONMENT", "unknown").lower()
OPERATIONS_ENVIRONMENT = (
    _OPERATIONS_ENVIRONMENT if _OPERATIONS_ENVIRONMENT in {"local", "staging", "production"} else "unknown"
)
