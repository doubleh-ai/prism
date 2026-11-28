"""Admin/Ops-only model rate card, durable token spend ledger, live provider balance & model discovery."""

from __future__ import annotations

import os
import re
import time
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from . import config, public_network
from .agent.provider import configuration_status
from .auth import require_admin, require_auth, verify_csrf
from .database import get_db
from .model_routes import init_model_routes_db

router = APIRouter(prefix="/api/admin/billing-ops", tags=["admin"])
user_router = APIRouter(prefix="/api/user", tags=["user"])
SHANGHAI = ZoneInfo("Asia/Shanghai")
USD_TO_CNY = 7.24

_SAFE_API_KEY_RE = re.compile(r"^[A-Za-z0-9_.:+/=-]{12,256}$")
_INVISIBLE_CHARS_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\xa0]")
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{2,80}$")

PROVIDER_SLOTS_META: tuple[dict[str, Any], ...] = (
    {
        "slot": "deepseek",
        "label": "DeepSeek 官方平台",
        "endpoint": "https://api.deepseek.com/anthropic",
        "config_attr": "DEEPSEEK_API_KEY",
        "env_var": "DEEPSEEK_API_KEY",
        "placeholder": "粘贴 sk-... (platform.deepseek.com)",
        "models": ("deepseek-v4", "deepseek-v4-flash", "deepseek-flash"),
        "hint": "支持实时余额探测 · 北京时间闲时享 5 折优惠",
    },
    {
        "slot": "minimax_cn",
        "label": "MiniMax 国内直连 (minimaxi.com)",
        "endpoint": "https://api.minimax.cn/anthropic/v1/",
        "config_attr": "MINIMAX_CN_API_KEY",
        "env_var": "MINIMAX_CN_API_KEY",
        "placeholder": "粘贴国内开放平台 API Key (eyJ... / sk-...)",
        "models": ("minimax-cn-m3", "minimax-cn-m3.1-flash"),
        "hint": "国内低延迟直连端点 · 推荐用于日常敏捷随行与深度研究",
    },
    {
        "slot": "minimax",
        "label": "MiniMax 国际站 (minimax.io)",
        "endpoint": "https://api.minimax.io/anthropic/v1/",
        "config_attr": "MINIMAX_API_KEY",
        "env_var": "MINIMAX_API_KEY",
        "placeholder": "粘贴国际站 API Key",
        "models": ("minimax-m3", "minimax-m3.1-flash", "minimax-m2.7", "minimax-m2.7-fast"),
        "hint": "与国内站密钥物理隔离，按账号区域选择填写其一即可",
    },
    {
        "slot": "kimi",
        "label": "Moonshot Kimi 官方 (moonshot.cn)",
        "endpoint": "https://api.moonshot.cn/anthropic",
        "config_attr": "KIMI_API_KEY",
        "env_var": "KIMI_API_KEY",
        "placeholder": "粘贴 sk-... (platform.moonshot.cn)",
        "models": ("kimi-k2.5", "kimi-k3"),
        "hint": "官方 Anthropic 协议端点 · 支持实时人民币余额探测与超长上下文",
    },
    {
        "slot": "glm",
        "label": "智谱 BigModel GLM (open.bigmodel.cn)",
        "endpoint": "https://open.bigmodel.cn/api/anthropic",
        "config_attr": "GLM_API_KEY",
        "env_var": "GLM_API_KEY",
        "placeholder": "粘贴智谱 API Key (open.bigmodel.cn)",
        "models": ("glm-5", "glm-4.7"),
        "hint": "官方 Anthropic 协议端点 · 兼容 GLM Coding Plan 包月套餐与按量余额",
    },
    {
        "slot": "gemini",
        "label": "Google Gemini 官方 (AI Studio / Ultra 赠金)",
        "endpoint": "https://generativelanguage.googleapis.com/v1beta",
        "config_attr": "GEMINI_API_KEY",
        "env_var": "GEMINI_API_KEY",
        "placeholder": "粘贴 AQ... 或 AIza... (aistudio.google.com)",
        "models": ("gemini-3.8-flash", "gemini-3.5-pro", "gemini-3.5-flash-lite"),
        "hint": "支持 Google AI Ultra 每月 $40 赠金与免费层 Key · 直连主路由与随行 Agent",
    },
    {
        "slot": "claude",
        "label": "Anthropic Claude 官方 (anthropic.com)",
        "endpoint": "https://api.anthropic.com/v1",
        "config_attr": "ANTHROPIC_API_KEY",
        "env_var": "ANTHROPIC_API_KEY",
        "placeholder": "粘贴 sk-ant-... (console.anthropic.com)",
        "models": ("claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"),
        "hint": "原生 Anthropic Messages 流式与工具调用协议 · 1M 上下文旗舰推理",
    },
    {
        "slot": "openai",
        "label": "OpenAI 官方平台 (api.openai.com)",
        "endpoint": "https://api.openai.com/v1",
        "config_attr": "OPENAI_API_KEY",
        "env_var": "OPENAI_API_KEY",
        "placeholder": "粘贴 sk-proj-... (platform.openai.com)",
        "models": ("openai",),
        "needs_model_name": True,
        "default_model_name": "gpt-4.1",
        "hint": "支持按量 API Key 及 ChatGPT Pro 附赠月度 API 额度；ChatGPT Plus 网页订阅不开放服务端 API",
    },
)


class ProviderCredentialUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: str | None = Field(default=None, max_length=1024)
    model_override: str | None = Field(default=None, max_length=80)
    clear: bool = False


class UserModelPreferenceUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preferred_model: str = Field(default="", max_length=80)


class AdminUserQuotaUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unlimited_quota: bool = False
    daily_calls_limit: int | None = Field(default=None, ge=1, le=100000)
    monthly_budget_usd: float | None = Field(default=None, gt=0.0, le=100000.0)
    preferred_model: str | None = Field(default=None, max_length=64)


class AdminBatchUserQuotaUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    usernames: list[str] = Field(min_length=1, max_length=200)
    unlimited_quota: bool = False
    daily_calls_limit: int | None = Field(default=None, ge=1, le=100000)
    monthly_budget_usd: float | None = Field(default=None, gt=0.0, le=100000.0)
    preferred_model_mode: Literal["keep", "set"] = "keep"
    preferred_model: str = Field(default="", max_length=64)


def init_billing_ops_db() -> None:
    with get_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS ops_provider_credentials (
                slot TEXT PRIMARY KEY,
                api_key TEXT NOT NULL DEFAULT '',
                model_override TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_model_settings (
                username TEXT PRIMARY KEY,
                preferred_model TEXT NOT NULL DEFAULT '',
                unlimited_quota INTEGER NOT NULL DEFAULT 0,
                daily_calls_limit INTEGER,
                monthly_budget_usd REAL,
                updated_at REAL NOT NULL
            );
            """
        )


def _mask_key(key: str) -> str:
    val = (key or "").strip()
    if not val:
        return ""
    if len(val) <= 8:
        return "****"
    return f"{val[:4]}****{val[-4:]}"


def _clean_provider_api_key(raw: str) -> str:
    cleaned = _INVISIBLE_CHARS_RE.sub("", str(raw or "")).strip()
    if not cleaned:
        raise ValueError("key_empty")
    if "..." in cleaned or "…" in cleaned or "***" in cleaned or "•" in cleaned:
        raise ValueError("key_masked")
    if "=" in cleaned and ("API_KEY" in cleaned.upper() or cleaned.lower().startswith("export ")):
        cleaned = cleaned.split("=", 1)[1].strip()
    cleaned = cleaned.strip("\"'` \t\r\n")
    if cleaned.lower().startswith("bearer "):
        cleaned = cleaned[7:].strip("\"'` ")
    if "\n" in cleaned or "\r" in cleaned:
        cleaned = re.sub(r"[\r\n\t ]+", "", cleaned)
    if not _SAFE_API_KEY_RE.fullmatch(cleaned):
        raise ValueError("key_invalid_format")
    return cleaned


def _env_key_for_slot(slot_meta: dict[str, Any]) -> str:
    env_name = str(slot_meta["env_var"])
    val = os.environ.get(env_name, "")
    if not val and slot_meta["slot"] == "deepseek":
        val = os.environ.get("LLM_API_KEY", "")
    return val.strip()


def sync_ops_provider_credentials() -> list[dict[str, Any]]:
    """Apply Ops-saved provider credentials strictly per-slot without cross-provider inheritance."""
    init_billing_ops_db()
    with get_db() as db:
        rows = {
            str(r["slot"]): {
                "api_key": str(r["api_key"] or ""),
                "model_override": str(r["model_override"] or ""),
                "updated_at": float(r["updated_at"] or 0.0),
            }
            for r in db.execute(
                "SELECT slot, api_key, model_override, updated_at FROM ops_provider_credentials"
            ).fetchall()
        }

    # Only mutate config.MODELS for slots explicitly managed in ops_provider_credentials
    # so synthetic test fixtures (when ops_provider_credentials is empty) remain untouched.
    for meta in PROVIDER_SLOTS_META:
        slot = str(meta["slot"])
        if slot not in rows:
            continue
        rec = rows[slot]
        effective_key = rec["api_key"] or _env_key_for_slot(meta)
        setattr(config, str(meta["config_attr"]), effective_key)
        expected_ep = str(meta["endpoint"]).rstrip("/")
        for mid in meta["models"]:
            m_entry = config.MODELS.get(mid)
            if not m_entry or str(m_entry.get("base_url", "")).rstrip("/") != expected_ep:
                continue
            m_entry["api_key"] = effective_key
            if slot == "openai":
                eff_model = (
                    rec["model_override"]
                    or os.environ.get("OPENAI_MODEL", "").strip()
                    or (str(meta.get("default_model_name") or "gpt-4.1") if effective_key else "")
                )
                m_entry["model"] = eff_model

    if rows:
        preferred = os.environ.get("PRISM_DEFAULT_MODEL", "minimax-m3")
        config.DEFAULT_MODEL = next(
            (
                k
                for k in [preferred, *config.MODELS]
                if k in config.MODELS and config.MODELS[k].get("api_key") and config.MODELS[k].get("model")
            ),
            "minimax-m3",
        )

    out: list[dict[str, Any]] = []
    for meta in PROVIDER_SLOTS_META:
        slot = str(meta["slot"])
        rec = rows.get(slot)
        sample_mid = meta["models"][0]
        live_entry = config.MODELS.get(sample_mid, {})
        live_key = str(live_entry.get("api_key") or "")
        is_synthetic_test = live_key == "synthetic-isolated-test"
        eff_key = "" if is_synthetic_test else live_key
        source = "none"
        if rec and rec["api_key"]:
            source = "ops"
        elif eff_key:
            source = "env"
        model_override = (
            (rec["model_override"] if rec else "") or str(live_entry.get("model") or "")
            if meta.get("needs_model_name")
            else ""
        )
        out.append(
            {
                "slot": slot,
                "label": meta["label"],
                "endpoint": meta["endpoint"],
                "placeholder": meta["placeholder"],
                "models": list(meta["models"]),
                "configured": bool(eff_key) and (not meta.get("needs_model_name") or bool(model_override)),
                "source": source,
                "masked_key": _mask_key(eff_key),
                "needs_model_name": bool(meta.get("needs_model_name")),
                "model_override": model_override,
                "hint": str(meta.get("hint") or ""),
            }
        )
    return out


def save_ops_provider_credential(
    slot: str,
    *,
    api_key: str | None,
    model_override: str | None = None,
    clear: bool = False,
) -> list[dict[str, Any]]:
    meta = next((m for m in PROVIDER_SLOTS_META if m["slot"] == slot), None)
    if not meta:
        raise ValueError("unknown_provider_slot")
    init_billing_ops_db()
    now = time.time()
    if clear:
        with get_db() as db:
            db.execute(
                """INSERT INTO ops_provider_credentials(slot, api_key, model_override, updated_at)
                   VALUES (?, '', '', ?)
                   ON CONFLICT(slot) DO UPDATE SET api_key='', model_override='', updated_at=excluded.updated_at""",
                [slot, now],
            )
        return sync_ops_provider_credentials()

    with get_db() as db:
        existing = db.execute(
            "SELECT api_key, model_override FROM ops_provider_credentials WHERE slot=?",
            [slot],
        ).fetchone()
        if api_key is not None and api_key.strip():
            clean_key = _clean_provider_api_key(api_key)
        elif existing and existing["api_key"]:
            clean_key = str(existing["api_key"])
        else:
            raise ValueError("key_empty")

        clean_model = ""
        if meta.get("needs_model_name"):
            raw_m = (
                model_override.strip()
                if (model_override is not None and model_override.strip())
                else (
                    str(existing["model_override"]) if existing and existing["model_override"] else "gpt-4.1"
                )
            )
            if not _MODEL_NAME_RE.fullmatch(raw_m):
                raise ValueError("invalid_model_override")
            clean_model = raw_m

        db.execute(
            """INSERT INTO ops_provider_credentials(slot, api_key, model_override, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(slot) DO UPDATE SET
                 api_key=excluded.api_key,
                 model_override=excluded.model_override,
                 updated_at=excluded.updated_at""",
            [slot, clean_key, clean_model, now],
        )
    return sync_ops_provider_credentials()


# Official published standard rates (USD per 1,000,000 tokens) & context limits
MODEL_RATE_CARD: dict[str, dict[str, Any]] = {
    "deepseek-v4": {
        "vendor": "DeepSeek",
        "upstream_model": "deepseek-v4-pro",
        "input_usd_per_1m": 1.32,
        "output_usd_per_1m": 3.96,
        "context_window": "1M",
        "note": "北京时间闲时享 5 折 ($0.66 / $1.98)；命中缓存输入 $0.044/1M",
    },
    "deepseek-v4-flash": {
        "vendor": "DeepSeek",
        "upstream_model": "deepseek-v4-flash",
        "input_usd_per_1m": 0.30,
        "output_usd_per_1m": 1.20,
        "context_window": "1M",
        "note": "北京时间闲时享 5 折 ($0.15 / $0.60)",
    },
    "deepseek-flash": {
        "vendor": "DeepSeek",
        "upstream_model": "deepseek-flash",
        "input_usd_per_1m": 0.30,
        "output_usd_per_1m": 1.20,
        "context_window": "1M",
        "note": "DeepSeek V4.1 Flash 最新主力极速版 · 闲时 5 折 ($0.15 / $0.60)",
    },
    "minimax-m3": {
        "vendor": "MiniMax (Global)",
        "upstream_model": "MiniMax-M3",
        "input_usd_per_1m": 0.30,
        "output_usd_per_1m": 1.20,
        "context_window": "1M",
        "note": "标准 <=512K 输入按 $0.30/$1.20 计费",
    },
    "minimax-m3.1-flash": {
        "vendor": "MiniMax (Global)",
        "upstream_model": "MiniMax-M3.1-Flash",
        "input_usd_per_1m": 0.15,
        "output_usd_per_1m": 0.60,
        "context_window": "1M",
        "note": "M3.1 极速高性价比版",
    },
    "minimax-m2.7": {
        "vendor": "MiniMax (Global)",
        "upstream_model": "MiniMax-M2.7",
        "input_usd_per_1m": 0.25,
        "output_usd_per_1m": 1.00,
        "context_window": "205K",
        "note": "兼容保留版本",
    },
    "minimax-m2.7-fast": {
        "vendor": "MiniMax (Global)",
        "upstream_model": "MiniMax-M2.7-highspeed",
        "input_usd_per_1m": 0.25,
        "output_usd_per_1m": 1.00,
        "context_window": "205K",
        "note": "高速低延迟兼容版",
    },
    "minimax-cn-m3": {
        "vendor": "MiniMax (国内)",
        "upstream_model": "MiniMax-M3",
        "input_usd_per_1m": 0.30,
        "output_usd_per_1m": 1.20,
        "context_window": "1M",
        "note": "国内直连端点 · 深度研究实测调优配额",
    },
    "minimax-cn-m3.1-flash": {
        "vendor": "MiniMax (国内)",
        "upstream_model": "MiniMax-M3.1-Flash",
        "input_usd_per_1m": 0.15,
        "output_usd_per_1m": 0.60,
        "context_window": "1M",
        "note": "国内直连端点 · 敏捷随行极速高性价比",
    },
    "kimi-k2.5": {
        "vendor": "Moonshot Kimi",
        "upstream_model": "kimi-k2.5",
        "input_usd_per_1m": 0.60,
        "output_usd_per_1m": 2.50,
        "context_window": "256K",
        "note": "Moonshot 主力长文与 Agent 工具调用版",
    },
    "kimi-k3": {
        "vendor": "Moonshot Kimi",
        "upstream_model": "kimi-k3",
        "input_usd_per_1m": 1.20,
        "output_usd_per_1m": 4.80,
        "context_window": "256K",
        "note": "Kimi 旗舰深度推理版",
    },
    "glm-5": {
        "vendor": "智谱 GLM",
        "upstream_model": "glm-5",
        "input_usd_per_1m": 0.80,
        "output_usd_per_1m": 3.20,
        "context_window": "200K",
        "note": "智谱旗舰 · 兼容 GLM Coding Plan 套餐",
    },
    "glm-4.7": {
        "vendor": "智谱 GLM",
        "upstream_model": "glm-4.7",
        "input_usd_per_1m": 0.30,
        "output_usd_per_1m": 1.20,
        "context_window": "200K",
        "note": "智谱高性价比主力 · 兼容 Coding Plan",
    },
    "claude-sonnet-5-5": {
        "vendor": "Anthropic Claude",
        "upstream_model": "claude-sonnet-5-5",
        "input_usd_per_1m": 3.00,
        "output_usd_per_1m": 15.00,
        "context_window": "1M",
        "note": "速度与深度推理均衡旗舰",
    },
    "claude-opus-5-5": {
        "vendor": "Anthropic Claude",
        "upstream_model": "claude-opus-5-5",
        "input_usd_per_1m": 15.00,
        "output_usd_per_1m": 75.00,
        "context_window": "1M",
        "note": "长程深度投研与复杂推演旗舰",
    },
    "claude-haiku-5-5": {
        "vendor": "Anthropic Claude",
        "upstream_model": "claude-haiku-5-5",
        "input_usd_per_1m": 0.80,
        "output_usd_per_1m": 4.00,
        "context_window": "1M",
        "note": "超低延迟轻量随行版",
    },
    "gemini-3.8-flash": {
        "vendor": "Google Gemini",
        "upstream_model": "gemini-3.8-flash",
        "input_usd_per_1m": 0.30,
        "output_usd_per_1m": 2.50,
        "context_window": "1M",
        "note": "Google 主力快模 · 可用 Ultra $40/月赠金抵扣",
    },
    "gemini-3.5-pro": {
        "vendor": "Google Gemini",
        "upstream_model": "gemini-3.5-pro",
        "input_usd_per_1m": 1.25,
        "output_usd_per_1m": 10.00,
        "context_window": "1M",
        "note": "Google 深度推理旗舰 · 可用 Ultra 赠金抵扣",
    },
    "gemini-3.5-flash-lite": {
        "vendor": "Google Gemini",
        "upstream_model": "gemini-3.5-flash-lite",
        "input_usd_per_1m": 0.10,
        "output_usd_per_1m": 0.40,
        "context_window": "1M",
        "note": "极速超低耗 · 兼容免费层与 Ultra 赠金",
    },
    "openai": {
        "vendor": "OpenAI",
        "upstream_model": "OPENAI_MODEL",
        "input_usd_per_1m": 1.25,
        "output_usd_per_1m": 10.00,
        "context_window": "400K",
        "note": "按实际 OPENAI_MODEL 官方单价折算",
    },
}

_provider_probe_cache: dict[str, Any] = {"checked_at": None, "providers": {}}


def _estimate_cost_usd(model_id: str, input_tokens: int, output_tokens: int) -> float:
    card = MODEL_RATE_CARD.get(model_id) or {"input_usd_per_1m": 0.30, "output_usd_per_1m": 1.20}
    cost = (max(0, int(input_tokens)) / 1_000_000.0) * float(card["input_usd_per_1m"]) + (
        max(0, int(output_tokens)) / 1_000_000.0
    ) * float(card["output_usd_per_1m"])
    return round(cost, 6)


def compute_prism_spend_summary() -> dict[str, Any]:
    """Aggregate Prism Native model usage & estimated USD/CNY spend from durable reservations."""
    init_model_routes_db()
    provider_slots = sync_ops_provider_credentials()
    now_sh = datetime.now(SHANGHAI)
    today = now_sh.strftime("%Y-%m-%d")
    month_prefix = now_sh.strftime("%Y-%m")

    with get_db() as db:
        rows = db.execute(
            """
            SELECT model_id, day, status,
                   COUNT(*) AS calls,
                   COALESCE(SUM(input_tokens), 0) AS in_tok,
                   COALESCE(SUM(output_tokens), 0) AS out_tok
            FROM model_request_reservations
            GROUP BY model_id, day, status
            """
        ).fetchall()

    today_stats = {"calls": 0, "unknown_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    month_stats = {"calls": 0, "unknown_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    by_model_month: dict[str, dict[str, Any]] = {}

    for r in rows:
        mid = str(r["model_id"] or "unknown")
        day = str(r["day"] or "")
        calls = int(r["calls"] or 0)
        unk = calls if r["status"] == "unknown" else 0
        in_tok = int(r["in_tok"] or 0)
        out_tok = int(r["out_tok"] or 0)
        cost = _estimate_cost_usd(mid, in_tok, out_tok)

        if day == today:
            today_stats["calls"] += calls
            today_stats["unknown_calls"] += unk
            today_stats["input_tokens"] += in_tok
            today_stats["output_tokens"] += out_tok
            today_stats["cost_usd"] = round(today_stats["cost_usd"] + cost, 6)

        if day.startswith(month_prefix):
            month_stats["calls"] += calls
            month_stats["unknown_calls"] += unk
            month_stats["input_tokens"] += in_tok
            month_stats["output_tokens"] += out_tok
            month_stats["cost_usd"] = round(month_stats["cost_usd"] + cost, 6)
            m_acc = by_model_month.setdefault(
                mid, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
            )
            m_acc["calls"] += calls
            m_acc["input_tokens"] += in_tok
            m_acc["output_tokens"] += out_tok
            m_acc["cost_usd"] = round(m_acc["cost_usd"] + cost, 6)

    today_stats["cost_cny"] = round(today_stats["cost_usd"] * USD_TO_CNY, 4)
    month_stats["cost_cny"] = round(month_stats["cost_usd"] * USD_TO_CNY, 4)

    catalog = []
    for mid, mcfg in config.MODELS.items():
        st = configuration_status(mcfg)
        rc = MODEL_RATE_CARD.get(mid, {})
        m_use = by_model_month.get(mid, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
        catalog.append(
            {
                "id": mid,
                "name": mcfg.get("name", mid),
                "upstream_model": mcfg.get("model") or rc.get("upstream_model") or "",
                "vendor": rc.get("vendor") or st.get("endpoint_host"),
                "configured": st["configured"],
                "context_window": rc.get("context_window", "—"),
                "input_usd_per_1m": rc.get("input_usd_per_1m"),
                "output_usd_per_1m": rc.get("output_usd_per_1m"),
                "note": rc.get("note", ""),
                "month_calls": m_use["calls"],
                "month_input_tokens": m_use["input_tokens"],
                "month_output_tokens": m_use["output_tokens"],
                "month_cost_usd": m_use["cost_usd"],
                "month_cost_cny": round(m_use["cost_usd"] * USD_TO_CNY, 4),
            }
        )

    return {
        "today": today,
        "month": month_prefix,
        "usd_to_cny_ref": USD_TO_CNY,
        "prism_today": today_stats,
        "prism_month": month_stats,
        "catalog": catalog,
        "provider_slots": provider_slots,
        "provider_probes": _provider_probe_cache,
    }


def probe_upstream_providers() -> dict[str, Any]:
    """Query live upstream balance & available model endpoints for configured server keys.

    Never returns, logs, or echoes any API key material.
    """
    providers: dict[str, Any] = {}

    # 1. DeepSeek (/user/balance + /models)
    ds_key = (config.DEEPSEEK_API_KEY or "").strip()
    if ds_key:
        ds_info: dict[str, Any] = {"configured": True, "balance": None, "models": [], "status": "ok"}
        headers = {"Authorization": f"Bearer {ds_key}", "Accept": "application/json"}
        try:
            with public_network.sync_client(timeout=6.0) as client:
                b_resp = client.get("https://api.deepseek.com/user/balance", headers=headers)
                if b_resp.status_code == 200:
                    b_json = b_resp.json()
                    infos = b_json.get("balance_infos") or []
                    ds_info["is_available"] = bool(b_json.get("is_available", True))
                    ds_info["balances"] = [
                        {
                            "currency": str(item.get("currency") or "CNY"),
                            "total_balance": str(item.get("total_balance") or "0.00"),
                            "granted_balance": str(item.get("granted_balance") or "0.00"),
                            "topped_up_balance": str(item.get("topped_up_balance") or "0.00"),
                        }
                        for item in infos
                        if isinstance(item, dict)
                    ]
                else:
                    ds_info["status"] = f"balance_http_{b_resp.status_code}"

                m_resp = client.get("https://api.deepseek.com/models", headers=headers)
                if m_resp.status_code == 200:
                    m_data = (m_resp.json() or {}).get("data") or []
                    ds_info["models"] = [
                        str(m.get("id")) for m in m_data[:20] if isinstance(m, dict) and m.get("id")
                    ]
        except Exception as exc:
            ds_info["status"] = f"error:{type(exc).__name__}"
        providers["deepseek"] = ds_info
    else:
        providers["deepseek"] = {"configured": False}

    # 2. MiniMax CN & Global (/v1/token_plan/remains check)
    for slot, key_val, host_label, remains_url in (
        (
            "minimax_cn",
            (config.MINIMAX_CN_API_KEY or "").strip(),
            "MiniMax 国内",
            "https://www.minimaxi.com/v1/token_plan/remains",
        ),
        (
            "minimax_global",
            (config.MINIMAX_API_KEY or "").strip(),
            "MiniMax 国际",
            "https://www.minimax.io/v1/token_plan/remains",
        ),
    ):
        if not key_val:
            providers[slot] = {"configured": False, "label": host_label}
            continue
        mm_info: dict[str, Any] = {
            "configured": True,
            "label": host_label,
            "status": "pay_as_you_go_or_console",
            "note": "按量充值账户余额请见开放平台账单页；本面板实时统计 Prism 精确消耗",
        }
        try:
            with public_network.sync_client(timeout=5.5) as client:
                r = client.get(
                    remains_url,
                    headers={"Authorization": f"Bearer {key_val}", "Content-Type": "application/json"},
                )
                if r.status_code == 200:
                    rj = r.json()
                    if isinstance(rj, dict) and rj.get("model_remains"):
                        mm_info["status"] = "token_plan_active"
                        mm_info["model_remains"] = rj["model_remains"][:6]
        except Exception:
            pass
        providers[slot] = mm_info

    # 3. OpenAI (/v1/models)
    oa_key = (config.OPENAI_API_KEY or "").strip()
    if oa_key:
        oa_info: dict[str, Any] = {"configured": True, "models": [], "status": "ok"}
        try:
            with public_network.sync_client(timeout=6.0) as client:
                r = client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {oa_key}"},
                )
                if r.status_code == 200:
                    data = (r.json() or {}).get("data") or []
                    oa_info["models"] = sorted(
                        str(m.get("id"))
                        for m in data
                        if isinstance(m, dict) and str(m.get("id", "")).startswith(("gpt-", "o3", "o4"))
                    )[:25]
                else:
                    oa_info["status"] = f"http_{r.status_code}"
        except Exception as exc:
            oa_info["status"] = f"error:{type(exc).__name__}"
        providers["openai"] = oa_info
    else:
        providers["openai"] = {"configured": False}

    # 4. Moonshot Kimi (/v1/users/me/balance + /v1/models)
    kimi_key = (config.KIMI_API_KEY or "").strip()
    if kimi_key:
        km_info: dict[str, Any] = {"configured": True, "models": [], "status": "ok"}
        headers = {"Authorization": f"Bearer {kimi_key}", "Accept": "application/json"}
        try:
            with public_network.sync_client(timeout=6.0) as client:
                br = client.get("https://api.moonshot.cn/v1/users/me/balance", headers=headers)
                if br.status_code == 200:
                    bd = (br.json() or {}).get("data") or {}
                    km_info["available_balance_cny"] = bd.get("available_balance")
                    km_info["voucher_balance_cny"] = bd.get("voucher_balance")
                    km_info["cash_balance_cny"] = bd.get("cash_balance")
                else:
                    km_info["status"] = f"balance_http_{br.status_code}"
                mr = client.get("https://api.moonshot.cn/v1/models", headers=headers)
                if mr.status_code == 200:
                    km_info["models"] = [
                        str(m.get("id"))
                        for m in ((mr.json() or {}).get("data") or [])[:15]
                        if isinstance(m, dict) and m.get("id")
                    ]
        except Exception as exc:
            km_info["status"] = f"error:{type(exc).__name__}"
        providers["kimi"] = km_info
    else:
        providers["kimi"] = {"configured": False}

    # 5. Google Gemini (/v1beta/models)
    gm_key = (config.GEMINI_API_KEY or "").strip()
    if gm_key:
        gm_info: dict[str, Any] = {"configured": True, "models": [], "status": "ok"}
        try:
            with public_network.sync_client(timeout=6.0) as client:
                r = client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    headers={"x-goog-api-key": gm_key},
                )
                if r.status_code == 200:
                    gm_info["models"] = [
                        str(m.get("name", "")).removeprefix("models/")
                        for m in ((r.json() or {}).get("models") or [])
                        if isinstance(m, dict) and "gemini-" in str(m.get("name", ""))
                    ][:20]
                else:
                    gm_info["status"] = f"http_{r.status_code}"
        except Exception as exc:
            gm_info["status"] = f"error:{type(exc).__name__}"
        providers["gemini"] = gm_info
    else:
        providers["gemini"] = {"configured": False}

    # 6. Zhipu GLM & Anthropic Claude (configured flag)
    providers["glm"] = {"configured": bool((config.GLM_API_KEY or "").strip()), "status": "ready"}
    providers["claude"] = {"configured": bool((config.ANTHROPIC_API_KEY or "").strip()), "status": "ready"}

    _provider_probe_cache["checked_at"] = int(time.time())
    _provider_probe_cache["providers"] = providers
    return dict(_provider_probe_cache)


@router.get("/summary")
async def get_billing_summary(request: Request):
    require_admin(request)
    return await run_in_threadpool(compute_prism_spend_summary)


@router.post("/probe")
async def trigger_provider_probe(request: Request):
    require_admin(request)
    verify_csrf(request)
    await run_in_threadpool(probe_upstream_providers)
    return await run_in_threadpool(compute_prism_spend_summary)


@router.put("/providers/{slot}")
async def update_provider_credential(
    slot: Literal["deepseek", "minimax_cn", "minimax", "kimi", "glm", "gemini", "claude", "openai"],
    body: ProviderCredentialUpdateRequest,
    request: Request,
):
    require_admin(request)
    verify_csrf(request)
    try:
        await run_in_threadpool(
            save_ops_provider_credential,
            slot,
            api_key=body.api_key,
            model_override=body.model_override,
            clear=body.clear,
        )
    except ValueError as exc:
        err_map = {
            "key_empty": "请粘贴该服务商的完整 API Key",
            "key_masked": "检测到粘贴内容包含省略号（...）或掩码，请从服务商后台复制完整未脱敏 Key",
            "key_invalid_format": "API Key 格式无效（需为 12 位以上可见 ASCII 字符且不含空格）",
            "invalid_model_override": "OpenAI 模型名称格式无效（如 gpt-4.1 / gpt-5 / o4-mini）",
        }
        raise HTTPException(422, err_map.get(str(exc), "凭证参数无效")) from None
    return await run_in_threadpool(compute_prism_spend_summary)


def get_user_quota_config(username: str) -> dict[str, Any]:
    init_billing_ops_db()
    with get_db() as db:
        row = db.execute(
            """
            SELECT preferred_model, unlimited_quota, daily_calls_limit, monthly_budget_usd, updated_at
            FROM user_model_settings
            WHERE username=?
            """,
            [username],
        ).fetchone()
    if not row:
        return {
            "preferred_model": "",
            "unlimited_quota": False,
            "daily_calls_limit": None,
            "monthly_budget_usd": None,
            "updated_at": 0.0,
        }
    return {
        "preferred_model": str(row["preferred_model"] or ""),
        "unlimited_quota": bool(row["unlimited_quota"]),
        "daily_calls_limit": int(row["daily_calls_limit"]) if row["daily_calls_limit"] is not None else None,
        "monthly_budget_usd": (
            round(float(row["monthly_budget_usd"]), 4) if row["monthly_budget_usd"] is not None else None
        ),
        "updated_at": float(row["updated_at"] or 0.0),
    }


def compute_user_usage_summary(username: str) -> dict[str, Any]:
    init_model_routes_db()
    now_sh = datetime.now(SHANGHAI)
    today = now_sh.strftime("%Y-%m-%d")
    month_prefix = now_sh.strftime("%Y-%m")
    with get_db() as db:
        rows = db.execute(
            """
            SELECT model_id, day, status,
                   COUNT(*) AS calls,
                   COALESCE(SUM(input_tokens), 0) AS in_tok,
                   COALESCE(SUM(output_tokens), 0) AS out_tok
            FROM model_request_reservations
            WHERE actor=?
            GROUP BY model_id, day, status
            """,
            [username],
        ).fetchall()

    today_stats = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    month_stats = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    by_model_month: dict[str, dict[str, Any]] = {}

    for r in rows:
        mid = str(r["model_id"] or "unknown")
        day = str(r["day"] or "")
        calls = int(r["calls"] or 0)
        in_tok = int(r["in_tok"] or 0)
        out_tok = int(r["out_tok"] or 0)
        cost = _estimate_cost_usd(mid, in_tok, out_tok)
        if day == today:
            today_stats["calls"] += calls
            today_stats["input_tokens"] += in_tok
            today_stats["output_tokens"] += out_tok
            today_stats["cost_usd"] = round(today_stats["cost_usd"] + cost, 6)
        if day.startswith(month_prefix):
            month_stats["calls"] += calls
            month_stats["input_tokens"] += in_tok
            month_stats["output_tokens"] += out_tok
            month_stats["cost_usd"] = round(month_stats["cost_usd"] + cost, 6)
            acc = by_model_month.setdefault(
                mid, {"model_id": mid, "calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
            )
            acc["calls"] += calls
            acc["input_tokens"] += in_tok
            acc["output_tokens"] += out_tok
            acc["cost_usd"] = round(acc["cost_usd"] + cost, 6)

    today_stats["cost_cny"] = round(today_stats["cost_usd"] * USD_TO_CNY, 4)
    month_stats["cost_cny"] = round(month_stats["cost_usd"] * USD_TO_CNY, 4)
    return {
        "today": today_stats,
        "month": month_stats,
        "by_model_month": sorted(by_model_month.values(), key=lambda x: (-x["calls"], x["model_id"])),
    }


def get_user_model_settings_payload(username: str) -> dict[str, Any]:
    from .model_routes import _route

    sync_ops_provider_credentials()
    init_model_routes_db()
    qcfg = get_user_quota_config(username)
    usage = compute_user_usage_summary(username)
    with get_db() as db:
        route = _route(db, "research")
    platform_default = str(route.get("model_id") or config.DEFAULT_MODEL)
    platform_daily_cap = int(route.get("daily_calls_max", 200))

    available = []
    for mid, mcfg in config.MODELS.items():
        st = configuration_status(mcfg)
        if not st["configured"]:
            continue
        rc = MODEL_RATE_CARD.get(mid, {})
        available.append(
            {
                "id": mid,
                "name": mcfg.get("name", mid),
                "vendor": rc.get("vendor") or st.get("endpoint_host"),
                "context_window": rc.get("context_window", "—"),
                "input_usd_per_1m": rc.get("input_usd_per_1m"),
                "output_usd_per_1m": rc.get("output_usd_per_1m"),
                "note": rc.get("note", ""),
                "is_platform_default": mid == platform_default,
            }
        )

    avail_ids = {m["id"] for m in available}
    pref = qcfg["preferred_model"] if qcfg["preferred_model"] in avail_ids else ""
    effective = pref or (
        platform_default if platform_default in avail_ids else (available[0]["id"] if available else "")
    )

    return {
        "username": username,
        "preferred_model": pref,
        "effective_model": effective,
        "platform_default_model": platform_default,
        "available_models": available,
        "quota": {
            "unlimited": qcfg["unlimited_quota"],
            "daily_calls_limit": None if qcfg["unlimited_quota"] else qcfg["daily_calls_limit"],
            "effective_daily_calls_limit": (
                None if qcfg["unlimited_quota"] else (qcfg["daily_calls_limit"] or platform_daily_cap)
            ),
            "platform_daily_calls_limit": platform_daily_cap,
            "daily_calls_used": usage["today"]["calls"],
            "monthly_budget_usd": None if qcfg["unlimited_quota"] else qcfg["monthly_budget_usd"],
            "monthly_spend_usd": usage["month"]["cost_usd"],
        },
        "usage": usage,
    }


def save_user_preferred_model(username: str, preferred_model: str) -> dict[str, Any]:
    sync_ops_provider_credentials()
    init_billing_ops_db()
    clean = str(preferred_model or "").strip()
    if clean:
        mcfg = config.MODELS.get(clean)
        if not mcfg or not configuration_status(mcfg)["configured"]:
            raise ValueError("model_not_available")
    now = time.time()
    with get_db() as db:
        db.execute(
            """
            INSERT INTO user_model_settings(username, preferred_model, unlimited_quota, daily_calls_limit, monthly_budget_usd, updated_at)
            VALUES (?, ?, 0, NULL, NULL, ?)
            ON CONFLICT(username) DO UPDATE SET preferred_model=excluded.preferred_model, updated_at=excluded.updated_at
            """,
            [username, clean, now],
        )
    return get_user_model_settings_payload(username)


def save_admin_user_quota(
    username: str,
    *,
    unlimited_quota: bool,
    daily_calls_limit: int | None,
    monthly_budget_usd: float | None,
    preferred_model: str | None = None,
) -> dict[str, Any]:
    sync_ops_provider_credentials()
    init_billing_ops_db()
    clean_model: str | None = None
    if preferred_model is not None:
        clean_model = str(preferred_model).strip()
        if clean_model:
            mcfg = config.MODELS.get(clean_model)
            if not mcfg or not configuration_status(mcfg)["configured"]:
                raise ValueError("model_not_available")
    now = time.time()
    eff_daily = None if unlimited_quota else daily_calls_limit
    eff_budget = (
        None
        if unlimited_quota
        else (round(float(monthly_budget_usd), 4) if monthly_budget_usd is not None else None)
    )
    with get_db() as db:
        if clean_model is not None:
            db.execute(
                """
                INSERT INTO user_model_settings(username, preferred_model, unlimited_quota, daily_calls_limit, monthly_budget_usd, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    preferred_model=excluded.preferred_model,
                    unlimited_quota=excluded.unlimited_quota,
                    daily_calls_limit=excluded.daily_calls_limit,
                    monthly_budget_usd=excluded.monthly_budget_usd,
                    updated_at=excluded.updated_at
                """,
                [username, clean_model, 1 if unlimited_quota else 0, eff_daily, eff_budget, now],
            )
        else:
            db.execute(
                """
                INSERT INTO user_model_settings(username, preferred_model, unlimited_quota, daily_calls_limit, monthly_budget_usd, updated_at)
                VALUES (?, '', ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    unlimited_quota=excluded.unlimited_quota,
                    daily_calls_limit=excluded.daily_calls_limit,
                    monthly_budget_usd=excluded.monthly_budget_usd,
                    updated_at=excluded.updated_at
                """,
                [username, 1 if unlimited_quota else 0, eff_daily, eff_budget, now],
            )
    return get_user_quota_config(username)


def save_admin_batch_user_quota(
    usernames: list[str],
    *,
    unlimited_quota: bool,
    daily_calls_limit: int | None,
    monthly_budget_usd: float | None,
    preferred_model_mode: str = "keep",
    preferred_model: str = "",
) -> dict[str, Any]:
    sync_ops_provider_credentials()
    init_billing_ops_db()
    clean_model: str | None = None
    if preferred_model_mode == "set":
        clean_model = str(preferred_model or "").strip()
        if clean_model:
            mcfg = config.MODELS.get(clean_model)
            if not mcfg or not configuration_status(mcfg)["configured"]:
                raise ValueError("model_not_available")

    deduped = []
    seen = set()
    for raw_u in usernames:
        u = str(raw_u or "").strip()
        if u and u not in seen:
            seen.add(u)
            deduped.append(u)
    if not deduped:
        raise ValueError("no_users_selected")

    now = time.time()
    eff_daily = None if unlimited_quota else daily_calls_limit
    eff_budget = (
        None
        if unlimited_quota
        else (round(float(monthly_budget_usd), 4) if monthly_budget_usd is not None else None)
    )
    updated_users: list[str] = []
    with get_db() as db:
        placeholders = ",".join("?" for _ in deduped)
        valid_rows = db.execute(
            f"SELECT username FROM users WHERE username IN ({placeholders})", deduped
        ).fetchall()
        valid_set = {str(r["username"]) for r in valid_rows}
        target_users = [u for u in deduped if u in valid_set]
        if not target_users:
            raise ValueError("no_valid_users")
        db.execute("BEGIN IMMEDIATE")
        try:
            for uname in target_users:
                if clean_model is not None:
                    db.execute(
                        """
                        INSERT INTO user_model_settings(username, preferred_model, unlimited_quota, daily_calls_limit, monthly_budget_usd, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(username) DO UPDATE SET
                            preferred_model=excluded.preferred_model,
                            unlimited_quota=excluded.unlimited_quota,
                            daily_calls_limit=excluded.daily_calls_limit,
                            monthly_budget_usd=excluded.monthly_budget_usd,
                            updated_at=excluded.updated_at
                        """,
                        [uname, clean_model, 1 if unlimited_quota else 0, eff_daily, eff_budget, now],
                    )
                else:
                    db.execute(
                        """
                        INSERT INTO user_model_settings(username, preferred_model, unlimited_quota, daily_calls_limit, monthly_budget_usd, updated_at)
                        VALUES (?, '', ?, ?, ?, ?)
                        ON CONFLICT(username) DO UPDATE SET
                            unlimited_quota=excluded.unlimited_quota,
                            daily_calls_limit=excluded.daily_calls_limit,
                            monthly_budget_usd=excluded.monthly_budget_usd,
                            updated_at=excluded.updated_at
                        """,
                        [uname, 1 if unlimited_quota else 0, eff_daily, eff_budget, now],
                    )
                updated_users.append(uname)
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
    return {"updated_count": len(updated_users), "updated_users": updated_users}


def all_users_quota_and_spend_map() -> dict[str, dict[str, Any]]:
    init_billing_ops_db()
    init_model_routes_db()
    now_sh = datetime.now(SHANGHAI)
    today = now_sh.strftime("%Y-%m-%d")
    month_prefix = now_sh.strftime("%Y-%m")
    out: dict[str, dict[str, Any]] = {}
    with get_db() as db:
        for row in db.execute(
            "SELECT username, preferred_model, unlimited_quota, daily_calls_limit, monthly_budget_usd FROM user_model_settings"
        ).fetchall():
            out[str(row["username"])] = {
                "preferred_model": str(row["preferred_model"] or ""),
                "unlimited_quota": bool(row["unlimited_quota"]),
                "daily_calls_limit": int(row["daily_calls_limit"])
                if row["daily_calls_limit"] is not None
                else None,
                "monthly_budget_usd": (
                    round(float(row["monthly_budget_usd"]), 4)
                    if row["monthly_budget_usd"] is not None
                    else None
                ),
                "today_calls": 0,
                "month_calls": 0,
                "month_cost_usd": 0.0,
            }
        rows = db.execute(
            """
            SELECT actor, model_id, day, COUNT(*) AS calls,
                   COALESCE(SUM(input_tokens), 0) AS in_tok,
                   COALESCE(SUM(output_tokens), 0) AS out_tok
            FROM model_request_reservations
            WHERE day LIKE ?
            GROUP BY actor, model_id, day
            """,
            [f"{month_prefix}%"],
        ).fetchall()
    for r in rows:
        actor = str(r["actor"] or "")
        if not actor:
            continue
        rec = out.setdefault(
            actor,
            {
                "preferred_model": "",
                "unlimited_quota": False,
                "daily_calls_limit": None,
                "monthly_budget_usd": None,
                "today_calls": 0,
                "month_calls": 0,
                "month_cost_usd": 0.0,
            },
        )
        calls = int(r["calls"] or 0)
        cost = _estimate_cost_usd(str(r["model_id"] or ""), int(r["in_tok"] or 0), int(r["out_tok"] or 0))
        rec["month_calls"] += calls
        rec["month_cost_usd"] = round(rec["month_cost_usd"] + cost, 6)
        if str(r["day"] or "") == today:
            rec["today_calls"] += calls
    return out


@user_router.get("/model-settings")
async def api_get_user_model_settings(request: Request):
    username = require_auth(request)
    return await run_in_threadpool(get_user_model_settings_payload, username)


@user_router.put("/model-settings")
async def api_update_user_model_settings(body: UserModelPreferenceUpdateRequest, request: Request):
    username = require_auth(request)
    verify_csrf(request)
    try:
        return await run_in_threadpool(save_user_preferred_model, username, body.preferred_model)
    except ValueError:
        raise HTTPException(422, "所选模型当前不可用或未配置，请刷新重试") from None
