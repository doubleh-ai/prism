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
from .auth import require_admin, verify_csrf
from .database import get_db
from .model_routes import init_model_routes_db

router = APIRouter(prefix="/api/admin/billing-ops", tags=["admin"])
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
    },
    {
        "slot": "minimax_cn",
        "label": "MiniMax 国内直连 (minimaxi.com)",
        "endpoint": "https://api.minimax.cn/anthropic/v1/",
        "config_attr": "MINIMAX_CN_API_KEY",
        "env_var": "MINIMAX_CN_API_KEY",
        "placeholder": "粘贴国内开放平台 API Key (eyJ... / sk-...)",
        "models": ("minimax-cn-m3", "minimax-cn-m3.1-flash"),
    },
    {
        "slot": "minimax",
        "label": "MiniMax 国际站 (minimax.io)",
        "endpoint": "https://api.minimax.io/anthropic/v1/",
        "config_attr": "MINIMAX_API_KEY",
        "env_var": "MINIMAX_API_KEY",
        "placeholder": "粘贴国际站 API Key",
        "models": ("minimax-m3", "minimax-m3.1-flash", "minimax-m2.7", "minimax-m2.7-fast"),
    },
    {
        "slot": "openai",
        "label": "OpenAI 官方平台",
        "endpoint": "https://api.openai.com/v1",
        "config_attr": "OPENAI_API_KEY",
        "env_var": "OPENAI_API_KEY",
        "placeholder": "粘贴 sk-proj-...",
        "models": ("openai",),
        "needs_model_name": True,
        "default_model_name": "gpt-4.1",
    },
)


class ProviderCredentialUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: str | None = Field(default=None, max_length=1024)
    model_override: str | None = Field(default=None, max_length=80)
    clear: bool = False


def init_billing_ops_db() -> None:
    with get_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS ops_provider_credentials (
                slot TEXT PRIMARY KEY,
                api_key TEXT NOT NULL DEFAULT '',
                model_override TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL
            )
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
        raise ValueError("empty_api_key")
    if "..." in cleaned or "…" in cleaned or "***" in cleaned or "•" in cleaned:
        raise ValueError("masked_api_key")
    if "=" in cleaned and ("API_KEY" in cleaned.upper() or cleaned.lower().startswith("export ")):
        cleaned = cleaned.split("=", 1)[1].strip()
    cleaned = cleaned.strip("\"'` \t\r\n")
    if cleaned.lower().startswith("bearer "):
        cleaned = cleaned[7:].strip("\"'` ")
    if "\n" in cleaned or "\r" in cleaned:
        cleaned = re.sub(r"[\r\n\t ]+", "", cleaned)
    if not _SAFE_API_KEY_RE.fullmatch(cleaned):
        raise ValueError("invalid_api_key")
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
            raise ValueError("empty_api_key")

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
    slot: Literal["deepseek", "minimax_cn", "minimax", "openai"],
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
            "empty_api_key": "请粘贴该服务商的完整 API Key",
            "masked_api_key": "检测到粘贴内容包含省略号（...）或掩码，请从服务商后台复制完整未脱敏 Key",
            "invalid_api_key": "API Key 格式无效（需为 12 位以上可见 ASCII 字符且不含空格）",
            "invalid_model_override": "OpenAI 模型名称格式无效（如 gpt-4.1 / gpt-5 / o4-mini）",
        }
        raise HTTPException(422, err_map.get(str(exc), "凭证参数无效")) from None
    return await run_in_threadpool(compute_prism_spend_summary)
