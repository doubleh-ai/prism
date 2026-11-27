"""HTTP API routes for Prism Global Event Radar (user read/action plane + Ops-only admin control plane)."""

from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from . import event_radar
from .auth import require_admin, require_auth, verify_csrf
from .database import get_db

router = APIRouter(prefix="/api/radar", tags=["radar"])
admin_router = APIRouter(prefix="/api/admin/radar-ops", tags=["admin"])


PushProviderLiteral = Literal[
    "ntfy",
    "pushplus",
    "serverchan",
    "bark",
    "feishu",
    "wecom",
    "dingtalk",
    "telegram",
]
DeliveryModeLiteral = Literal[
    "timeSensitive",
    "critical",
    "active",
    "urgent",
    "high",
    "default",
    "markdown",
    "compact",
]


class PushConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: PushProviderLiteral = "bark"
    secret: str | None = Field(default=None, max_length=256)
    enabled: bool = True
    min_tier: Literal["T0", "T1"] = "T0"
    bark_level: DeliveryModeLiteral = "timeSensitive"
    notify_watchlist: bool = True


class PushChannelCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(default="", max_length=40)
    provider: PushProviderLiteral = "ntfy"
    secret: str = Field(..., min_length=1, max_length=256)
    enabled: bool = True
    min_tier: Literal["T0", "T1"] = "T0"
    bark_level: DeliveryModeLiteral | None = None
    notify_watchlist: bool = True


class PushChannelUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, max_length=40)
    provider: PushProviderLiteral | None = None
    secret: str | None = Field(default=None, max_length=256)
    enabled: bool | None = None
    min_tier: Literal["T0", "T1"] | None = None
    bark_level: DeliveryModeLiteral | None = None
    notify_watchlist: bool | None = None


class RadarAiConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: str | None = Field(default=None, max_length=2048)
    model: str = Field(default="gemini-2.5-flash", min_length=3, max_length=80)
    enabled: bool = True
    use_google_search: bool = True
    auto_enrich: bool = True
    auto_tier: Literal["T0_ONLY", "T0_WATCH", "T0_T1"] = "T0_WATCH"
    auto_briefing: bool = True
    daily_limit: int = Field(default=20, ge=1, le=200)
    monthly_credit_usd: float = Field(default=40.0, ge=0.0, le=5000.0)
    prior_spend_usd: float = Field(default=0.0, ge=0.0, le=5000.0)
    monthly_cap_usd: float = Field(default=38.0, ge=0.0, le=5000.0)


# ── User Plane: Pure Read / Refresh / Consume Platform Intelligence (Zero Credentials/Config) ──


@router.get("/feed")
async def get_radar_feed(request: Request, limit: int = 50, tier: str = "all"):
    user = require_auth(request)
    if tier not in {"all", "T0", "T1", "T2"}:
        raise HTTPException(422, "信号等级过滤无效")
    return await run_in_threadpool(event_radar.list_radar_feed, user, limit=limit, tier=tier)


@router.post("/refresh")
async def refresh_radar_now(request: Request):
    user = require_auth(request)
    verify_csrf(request)
    now = time.monotonic()
    if now - event_radar._last_manual_refresh_at >= 6.0:
        event_radar._last_manual_refresh_at = now
        await run_in_threadpool(event_radar.poll_once)
    return await run_in_threadpool(event_radar.list_radar_feed, user, limit=50, tier="all")


@router.post("/events/{event_id}/enrich")
async def enrich_single_event(event_id: str, request: Request):
    require_auth(request)
    verify_csrf(request)
    if len(event_id) > 64:
        raise HTTPException(422, "事件 ID 无效")
    try:
        enrichment = await run_in_threadpool(
            event_radar.enrich_event_with_gemini,
            event_id,
            username=event_radar.SYSTEM_OWNER,
            ignore_enabled_flag=False,
        )
    except ValueError as exc:
        code = str(exc)
        if code == "event_not_found":
            raise HTTPException(404, "未找到该风向快讯") from None
        if code in {"gemini_key_missing", "gemini_disabled"}:
            raise HTTPException(503, "平台尚未在 Ops 运维中心启用全球溯源引擎") from None
        if code == "gemini_daily_limit_reached":
            raise HTTPException(429, "今日平台全球溯源配额已达熔断上限") from None
        raise HTTPException(502, f"全球检索推演未完成（{code}）") from None
    return {"ok": True, "enrichment": enrichment}


@router.post("/briefing/generate")
async def generate_briefing_now(request: Request):
    require_auth(request)
    verify_csrf(request)
    brief = await run_in_threadpool(
        event_radar.generate_global_briefing,
        username=event_radar.SYSTEM_OWNER,
        slot="manual",
        force=True,
    )
    return {"ok": True, "briefing": brief}


# ── Ops Control Plane Only (`/api/admin/radar-ops/*` via Loopback Ops Gateway) ──


@admin_router.get("")
async def ops_get_radar_summary(request: Request):
    require_admin(request)
    return await run_in_threadpool(event_radar.get_radar_ops_summary)


@admin_router.put("/ai")
async def ops_save_ai_config(body: RadarAiConfigRequest, request: Request):
    require_admin(request)
    verify_csrf(request)
    try:
        return event_radar.save_user_ai_config(
            event_radar.SYSTEM_OWNER,
            api_key=body.api_key,
            model=body.model,
            enabled=body.enabled,
            use_google_search=body.use_google_search,
            auto_enrich=body.auto_enrich,
            auto_tier=body.auto_tier,
            auto_briefing=body.auto_briefing,
            daily_limit=body.daily_limit,
            monthly_credit_usd=body.monthly_credit_usd,
            prior_spend_usd=body.prior_spend_usd,
            monthly_cap_usd=body.monthly_cap_usd,
        )
    except ValueError as exc:
        code = str(exc)
        if code == "masked_gemini_api_key":
            raise HTTPException(
                422,
                "检测到粘贴内容含省略号（...）或掩码，这是 AI Studio 列表的缩略显示；请点击密钥行右侧的「Copy API key」复制图标后再粘贴（可点输入框右侧图标核对明文）",
            ) from None
        if code == "invalid_gemini_api_key":
            raise HTTPException(
                422,
                "Gemini API Key 格式无效（要求至少 16 位可见 ASCII 字符且不含空格，可点输入框右侧图标核对粘贴内容）",
            ) from None
        raise HTTPException(422, "Gemini 引擎配置参数无效") from None


@admin_router.post("/ai/discover-models")
async def ops_discover_gemini_models(request: Request):
    require_admin(request)
    verify_csrf(request)
    try:
        models = await run_in_threadpool(event_radar.discover_gemini_models, event_radar.SYSTEM_OWNER)
    except ValueError as exc:
        code = str(exc)
        if code == "gemini_key_missing":
            raise HTTPException(422, "请先保存 Gemini API Key，再拉取 Google 最新模型列表") from None
        raise HTTPException(502, f"拉取 Google 模型列表失败（{code}）") from None
    return {"ok": True, "models": models, "ai": event_radar.get_user_ai_config(event_radar.SYSTEM_OWNER)}


class RadarAiTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str | None = Field(default=None, max_length=80)
    api_key: str | None = Field(default=None, max_length=2048)


@admin_router.post("/ai/test")
async def ops_test_gemini_connection(request: Request, body: RadarAiTestRequest | None = None):
    require_admin(request)
    verify_csrf(request)
    owner = event_radar.SYSTEM_OWNER
    event_radar.init_event_radar_db()
    with get_db() as db:
        row = db.execute("SELECT * FROM user_radar_ai_config WHERE username=?", [owner]).fetchone()
    test_key = ""
    if body and body.api_key and body.api_key.strip():
        try:
            test_key = event_radar.normalize_gemini_api_key(body.api_key)
        except ValueError as exc:
            raise HTTPException(422, f"API Key 格式无效（{exc}）") from None
    elif row and row["api_key"]:
        test_key = str(row["api_key"])
    if not test_key:
        raise HTTPException(422, "请先保存或填入 Google AI Studio / Cloud API Key")

    test_model = (
        body.model.strip()
        if (body and body.model and body.model.strip())
        else (str(row["model"]) if row and row["model"] else "gemini-2.5-flash")
    )
    daily_lim = int(row["daily_limit"] or 20) if row else 20
    if not event_radar._reserve_daily_gemini_call(owner, daily_lim):
        raise HTTPException(429, "已达今日自动推演熔断上限，可在上方调高每日上限后再试")

    ok, text_out, sources = await run_in_threadpool(
        event_radar.call_gemini_grounded,
        api_key=test_key,
        model=test_model,
        prompt="请用一句话简述今天全球金融市场最受关注的一条宏观或科技动态（35字内简体中文）。",
        use_google_search=bool(row["use_google_search"]) if row else True,
        timeout=20.0,
    )
    if not ok:
        # Refund daily circuit-breaker slot on HTTP config/model 4xx failure so testing models doesn't lock out
        with get_db() as db:
            db.execute(
                "UPDATE radar_ai_daily_usage SET calls_used = MAX(0, calls_used - 1) WHERE day=? AND username=?",
                [event_radar._today_shanghai(), owner],
            )
    event_radar._record_ai_status(
        owner, text_out[:80] if not ok else f"test_ok ({test_model}, sources={len(sources)})"
    )
    if not ok:
        raise HTTPException(
            502,
            f"模型 {test_model} 连通未通过（{text_out}）。若使用 AQ. 开头的 Google Cloud Express Key，推荐选择 gemini-2.5-flash 或 gemini-2.5-pro",
        )
    return {
        "ok": True,
        "model": test_model,
        "grounded": bool(sources),
        "sample": text_out[:180],
        "sources": sources,
        "ai": event_radar.get_user_ai_config(owner),
    }


_PUSH_ERROR_MESSAGES = {
    "empty_push_secret": "请填写推送通道密钥 / 主题名 / Token",
    "unsupported_push_endpoint": "出于 SSRF 安全限制，仅允许官方 HTTPS 推送网关（ntfy.sh / PushPlus / Server酱 / Bark / 飞书 / 企微 / 钉钉 / Telegram）",
    "invalid_ntfy_topic": "ntfy 主题名格式无效，请填写 4-80 位字母/数字/下划线或 https://ntfy.sh/主题名",
    "invalid_pushplus_token": "PushPlus Token 格式无效，请从 pushplus.plus 复制 16-64 位一对一 Token",
    "invalid_serverchan_key": "Server酱 SendKey 格式无效，请复制以 SCT 开头的 SendKey",
    "invalid_bark_device_key": "Bark Device Key 格式无效，请复制 Bark App 里的设备 Key 或 https://api.day.app/ 链接",
    "invalid_wecom_key": "企业微信机器人 Webhook / Key 格式无效",
    "invalid_feishu_hook": "飞书机器人 Webhook 格式无效",
    "invalid_dingtalk_token": "钉钉机器人 Webhook / Token 格式无效",
    "invalid_telegram_secret": "Telegram 凭证格式无效，请按 BotToken@ChatID 格式填写（如 123456:AAH...@987654321）",
    "push_channel_limit_exceeded": "已达最多 16 个并行推送终端上限，请先删除闲置终端",
    "push_channel_not_found": "未找到该推送终端记录",
}


@admin_router.put("/push")
async def ops_save_push_config(body: PushConfigRequest, request: Request):
    require_admin(request)
    verify_csrf(request)
    try:
        return event_radar.save_user_push_config(
            event_radar.SYSTEM_OWNER,
            provider=body.provider,
            secret=body.secret,
            enabled=body.enabled,
            min_tier=body.min_tier,
            bark_level=body.bark_level,
            notify_watchlist=body.notify_watchlist,
        )
    except ValueError as exc:
        raise HTTPException(422, _PUSH_ERROR_MESSAGES.get(str(exc), "推送参数无效")) from None


@admin_router.post("/push/channels")
async def ops_add_push_channel(body: PushChannelCreateRequest, request: Request):
    require_admin(request)
    verify_csrf(request)
    try:
        ch = event_radar.add_ops_push_channel(
            event_radar.SYSTEM_OWNER,
            label=body.label,
            provider=body.provider,
            secret=body.secret,
            enabled=body.enabled,
            min_tier=body.min_tier,
            bark_level=body.bark_level,
            notify_watchlist=body.notify_watchlist,
        )
    except ValueError as exc:
        raise HTTPException(422, _PUSH_ERROR_MESSAGES.get(str(exc), "推送参数无效")) from None
    return {
        "ok": True,
        "channel": ch,
        "push_channels": event_radar.list_ops_push_channels(event_radar.SYSTEM_OWNER),
        "push": event_radar.get_user_push_config(event_radar.SYSTEM_OWNER),
    }


@admin_router.put("/push/channels/{channel_id}")
async def ops_update_push_channel(channel_id: int, body: PushChannelUpdateRequest, request: Request):
    require_admin(request)
    verify_csrf(request)
    try:
        ch = event_radar.update_ops_push_channel(
            channel_id,
            event_radar.SYSTEM_OWNER,
            label=body.label,
            provider=body.provider,
            secret=body.secret,
            enabled=body.enabled,
            min_tier=body.min_tier,
            bark_level=body.bark_level,
            notify_watchlist=body.notify_watchlist,
        )
    except ValueError as exc:
        code = str(exc)
        status = 404 if code == "push_channel_not_found" else 422
        raise HTTPException(status, _PUSH_ERROR_MESSAGES.get(code, "推送参数无效")) from None
    return {
        "ok": True,
        "channel": ch,
        "push_channels": event_radar.list_ops_push_channels(event_radar.SYSTEM_OWNER),
        "push": event_radar.get_user_push_config(event_radar.SYSTEM_OWNER),
    }


@admin_router.delete("/push/channels/{channel_id}")
async def ops_delete_push_channel(channel_id: int, request: Request):
    require_admin(request)
    verify_csrf(request)
    removed = event_radar.delete_ops_push_channel(channel_id, event_radar.SYSTEM_OWNER)
    if not removed:
        raise HTTPException(404, "未找到该推送终端记录")
    return {
        "ok": True,
        "push_channels": event_radar.list_ops_push_channels(event_radar.SYSTEM_OWNER),
        "push": event_radar.get_user_push_config(event_radar.SYSTEM_OWNER),
    }


@admin_router.post("/push/channels/{channel_id}/test")
async def ops_test_single_push_channel(channel_id: int, request: Request):
    require_admin(request)
    verify_csrf(request)
    owner = event_radar.SYSTEM_OWNER
    event_radar.init_event_radar_db()
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM radar_push_endpoints WHERE id=? AND owner=?",
            [channel_id, owner],
        ).fetchone()
    if not row or not row["endpoint_secret"]:
        raise HTTPException(404, "未找到该推送终端或未配置密钥")

    win = event_radar.detect_trading_window()
    ok, status_text = await run_in_threadpool(
        event_radar.send_push_notification,
        row["provider"],
        row["endpoint_secret"],
        title=f"🔴 Prism Ops · 测试提醒 ({row['label'] or row['provider']})",
        subtitle=f"{win['tag']} · 全平台推送通道已打通",
        body="当命中全球/国内 T0 顶层重磅或核心自选催化时，经跨市场价格印证与 Gemini Search 核验的作战剧本将秒推至此终端。",
        bark_level=row["bark_level"],
    )
    now_ts = time.time()
    with get_db() as db:
        db.execute(
            "UPDATE radar_push_endpoints SET last_sent_at=?, last_status=? WHERE id=?",
            [now_ts, f"test:{status_text}", channel_id],
        )
    if not ok:
        raise HTTPException(502, f"终端推送未成功（{status_text}），请核对密钥与网络连通性")
    return {
        "ok": True,
        "status": status_text,
        "provider": row["provider"],
        "label": row["label"],
        "push_channels": event_radar.list_ops_push_channels(owner),
    }


@admin_router.post("/push/test")
async def ops_test_push_notification(request: Request):
    require_admin(request)
    verify_csrf(request)
    owner = event_radar.SYSTEM_OWNER
    event_radar.init_event_radar_db()
    event_radar._sync_legacy_system_channel_if_needed(owner)
    with get_db() as db:
        eps = db.execute(
            "SELECT * FROM radar_push_endpoints WHERE owner=? AND enabled=1 AND endpoint_secret != '' ORDER BY id ASC",
            [owner],
        ).fetchall()
        if not eps:
            eps = db.execute(
                "SELECT * FROM radar_push_endpoints WHERE owner=? AND endpoint_secret != '' ORDER BY id ASC LIMIT 1",
                [owner],
            ).fetchall()
        legacy = db.execute(
            "SELECT provider, endpoint_secret, bark_level FROM user_radar_channels WHERE username=?",
            [owner],
        ).fetchone()
    if not eps and (not legacy or not legacy["endpoint_secret"]):
        raise HTTPException(
            422,
            "请先添加至少一个推送终端（支持 Android ntfy / 微信 PushPlus / Server酱 / iOS Bark / 飞书 / 企微 / 钉钉 / Telegram）",
        )

    win = event_radar.detect_trading_window()
    targets = list(eps) if eps else [legacy]
    delivered = 0
    last_status = ""
    first_prov = str(targets[0]["provider"])
    first_lvl = str(targets[0]["bark_level"])
    now_ts = time.time()
    for t in targets:
        ok, status_text = await run_in_threadpool(
            event_radar.send_push_notification,
            t["provider"],
            t["endpoint_secret"],
            title="🔴 Prism Ops · 全球风向雷达测试告警",
            subtitle=f"{win['tag']} · 全平台推送通道已打通",
            body="当命中全球/国内 T0 顶层重磅或核心自选催化时，经跨市场价格印证与 Gemini Search 核验的作战剧本将秒推至此设备。",
            bark_level=t["bark_level"],
        )
        last_status = status_text
        if eps:
            with get_db() as db:
                db.execute(
                    "UPDATE radar_push_endpoints SET last_sent_at=?, last_status=? WHERE id=?",
                    [now_ts, f"test:{status_text}", t["id"]],
                )
        if ok:
            delivered += 1

    with get_db() as db:
        db.execute(
            "UPDATE user_radar_channels SET last_sent_at=?, last_status=? WHERE username=?",
            [now_ts, f"test:{last_status}", owner],
        )
    if delivered == 0:
        raise HTTPException(502, f"推送发送未成功（{last_status}），请核对密钥与网络连通性")
    return {
        "ok": True,
        "delivered": delivered,
        "total": len(targets),
        "status": last_status,
        "provider": first_prov,
        "bark_level": first_lvl,
        "push_channels": event_radar.list_ops_push_channels(owner),
    }
