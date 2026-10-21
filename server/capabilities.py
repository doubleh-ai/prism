"""Explicit, user-scoped platform tools for the conversational research agent.

The stdio subprocess has no database or engine access. It calls this router with
a short-lived run credential, so background work stays in the application process.
No tool accepts a URL, SQL, shell command, file path, username or session cookie.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import math
import re
import secrets
import time
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .auth import require_auth
from .config import CAPABILITY_TOKEN_TTL
from .database import check_rate_limit, get_db

router = APIRouter(prefix="/api")
Ticker = Annotated[str, Field(pattern=r"^[A-Za-z0-9.\-]{1,16}$")]
RecordId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_\-]{1,128}$")]
View = Literal["market", "industry", "indicators", "screener", "reports", "analysis", "vibe"]


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class EmptyArgs(Arguments):
    pass


class QuoteArgs(Arguments):
    ticker: Ticker


class KlineArgs(QuoteArgs):
    period: Literal["1d", "1w", "1m"] = "1d"
    limit: int = Field(default=120, ge=1, le=500)


class SearchArgs(Arguments):
    query: str = Field(min_length=1, max_length=32)
    limit: int = Field(default=10, ge=1, le=30)


class ListArgs(Arguments):
    limit: int = Field(default=20, ge=1, le=50)


class IdArgs(Arguments):
    id: RecordId


class IndicatorArgs(QuoteArgs):
    indicator_id: RecordId
    period: Literal["1d", "1w", "1m"] = "1d"
    params: dict[str, float] = Field(default_factory=dict, max_length=30)


class ScanArgs(Arguments):
    indicator_id: RecordId
    scope: Literal["watchlist", "market"] = "watchlist"
    tickers: list[Ticker] = Field(default_factory=list, max_length=200)
    period: Literal["1d", "1w", "1m"] = "1d"
    lookback: int = Field(default=120, ge=30, le=500)
    trigger_window: int = Field(default=5, ge=1, le=30)
    params: dict[str, float] = Field(default_factory=dict, max_length=30)
    max_universe: int = Field(default=0, ge=0, le=6000)
    max_results: int = Field(default=100, ge=10, le=300)


class IndustryArgs(Arguments):
    ticker: Ticker


class AnalysisArgs(QuoteArgs):
    name: str = Field(default="", max_length=80)
    mode: Literal["fast", "deep"] = "deep"


class VibeArgs(Arguments):
    prompt: str = Field(min_length=2, max_length=2000)
    market: str = Field(default="A股", min_length=1, max_length=32)
    horizon: Literal["intraday", "swing", "position", "long"] = "swing"
    risk_level: Literal["defensive", "balanced", "aggressive"] = "balanced"
    research_depth: Literal["focused", "deep", "swarm"] = "focused"


class CallRequest(Arguments):
    name: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict, max_length=30)
    request_id: RecordId


@dataclass(frozen=True)
class Capability:
    id: str
    label: str
    description: str
    schema: type[Arguments]
    source_kind: str
    view: str
    read_only: bool = True


# Adding an endpoint elsewhere never makes it available to the agent implicitly.
_TOOLS = (
    Capability(
        "market_quote",
        "查询行情",
        "查询一个代码的报价，保留来源与时间。",
        QuoteArgs,
        "platform_market",
        "market",
    ),
    Capability(
        "market_fundamentals",
        "查询基本面",
        "查询可用财务指标；空值代表未知。",
        QuoteArgs,
        "platform_market",
        "market",
    ),
    Capability(
        "market_kline",
        "查询 K 线",
        "查询日、周或月 K 线，最多500根。",
        KlineArgs,
        "platform_market",
        "market",
    ),
    Capability(
        "market_search", "搜索股票", "按名称或代码查询行情标的。", SearchArgs, "platform_market", "market"
    ),
    Capability(
        "indicator_list",
        "查看指标库",
        "列出当前用户的技术和基本面指标。",
        EmptyArgs,
        "platform_indicator",
        "indicators",
    ),
    Capability(
        "indicator_eval",
        "计算指标",
        "对指定股票执行已有指标，返回数据不足或计算结果。",
        IndicatorArgs,
        "platform_indicator",
        "indicators",
    ),
    Capability(
        "scan_start",
        "发起选股扫描",
        "发起当前用户的自选或全市场后台扫描，立即返回任务入口；仅在用户请求扫描时使用。",
        ScanArgs,
        "platform_indicator",
        "screener",
        False,
    ),
    Capability(
        "scan_status",
        "查看扫描结果",
        "读取当前用户的扫描进度和匹配股票。",
        IdArgs,
        "platform_indicator",
        "screener",
    ),
    Capability(
        "industry_overview",
        "查看产业总览",
        "读取已有图谱概况；种子图谱不是已核验商业供货证据。",
        EmptyArgs,
        "curated_unverified",
        "industry",
    ),
    Capability(
        "industry_chain",
        "查询产业链",
        "读取股票对应的现有节点和连线；不等同于已披露供货关系。",
        IndustryArgs,
        "curated_unverified",
        "industry",
    ),
    Capability(
        "industry_context",
        "查看产业研究动态",
        "读取当前用户已有研究在产业图谱上的关联。",
        EmptyArgs,
        "curated_unverified",
        "industry",
    ),
    Capability(
        "report_list", "查询报告历史", "查询当前用户已保存的报告历史。", ListArgs, "prior_research", "reports"
    ),
    Capability(
        "report_get",
        "读取历史报告",
        "读取当前用户的一份已保存报告；历史观点不作为最新原始事实。",
        IdArgs,
        "prior_research",
        "reports",
    ),
    Capability(
        "analysis_start",
        "发起个股研究",
        "启动指定股票的后台研究，立即返回记录；仅在用户请求分析时使用。",
        AnalysisArgs,
        "platform_research",
        "analysis",
        False,
    ),
    Capability(
        "analysis_status",
        "查看个股研究",
        "读取当前用户的研究状态及已完成报告。",
        IdArgs,
        "platform_research",
        "analysis",
    ),
    Capability(
        "vibe_start",
        "发起 Vibe 研究",
        "启动后台主题研究，立即返回记录；仅在用户请求研究时使用。",
        VibeArgs,
        "platform_research",
        "vibe",
        False,
    ),
    Capability(
        "vibe_status",
        "查看 Vibe 研究",
        "读取当前用户的主题研究状态和报告。",
        IdArgs,
        "platform_research",
        "vibe",
    ),
)
REGISTRY = {tool.id: tool for tool in _TOOLS}


def capability_catalog() -> list[dict]:
    return [
        {
            "id": t.id,
            "label": t.label,
            "description": t.description,
            "input_schema": t.schema.model_json_schema(),
            "source_kind": t.source_kind,
            "view": t.view,
            "target_view": t.view,
            "read_only": t.read_only,
        }
        for t in _TOOLS
    ]


def _owned_conversation(username: str, conv_id: str) -> None:
    with get_db() as db:
        row = db.execute(
            "SELECT c.id FROM conversations c JOIN users u ON u.username=c.username "
            "WHERE c.id=? AND c.username=? AND c.deleted_at IS NULL AND u.disabled_at IS NULL",
            [conv_id, username],
        ).fetchone()
    if not row:
        raise HTTPException(404, "会话不存在或不可访问")


def issue_run_token(username: str, conv_id: str, run_id: str) -> str:
    _owned_conversation(username, conv_id)
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with get_db() as db:
        db.execute("DELETE FROM capability_runs WHERE expires_at < ?", [now - 86400])
        db.execute(
            "INSERT INTO capability_runs(run_id,username,conv_id,token_hash,expires_at,created_at) "
            "VALUES(?,?,?,?,?,?)",
            [
                run_id,
                username,
                conv_id,
                hashlib.sha256(token.encode()).hexdigest(),
                now + CAPABILITY_TOKEN_TTL,
                now,
            ],
        )
    return token


def revoke_run_token(run_id: str) -> None:
    with get_db() as db:
        db.execute("UPDATE capability_runs SET revoked=1 WHERE run_id=?", [run_id])


def _authenticate_bridge(request: Request) -> dict:
    try:
        local = ipaddress.ip_address(request.client.host).is_loopback if request.client else False
    except ValueError:
        local = False
    if not local or request.url.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(403, "仅供本机研究桥访问")
    if any(request.headers.get(k) for k in ("origin", "forwarded", "x-forwarded-for", "x-real-ip")):
        raise HTTPException(403, "不接受代理或浏览器访问")
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer ") or len(authorization) > 256:
        raise HTTPException(401, "研究授权无效或已过期")
    digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM capability_runs WHERE token_hash=? AND revoked=0 AND expires_at>?",
            [digest, int(time.time())],
        ).fetchone()
    if not row:
        raise HTTPException(401, "研究授权无效或已过期")
    context = dict(row)
    _owned_conversation(context["username"], context["conv_id"])
    return context


_SECRET_KEYS = re.compile(
    r"(?i)(?:password|secret|api.?key|auth.?token|cookie|authorization|proxy|token_hash|(?:access|refresh|id)[_-]?token|^token$)"
)
_SECRET_TEXT = re.compile(r"(?:sk-(?:ant-|proj-)?[\w-]{16,}|gh[pousr]_[\w]{20,}|(?i:Bearer)\s+[\w.\-]{16,})")
_URL_AUTH = re.compile(r"(?i)(https?://)[^\s/@]+:[^\s/@]+@")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|token|password|secret|authorization|cookie)[\"']?\s*[=:]\s*[\"']?)[^\s&;\"'<>]+"
)
_PRIVATE_KEY = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|$)")


def safe_result(value: Any, depth: int = 0) -> Any:
    """Bound output and remove credential-shaped fields; never serialize engine objects."""
    if depth > 8:
        return None
    if isinstance(value, dict):
        return {
            str(k): safe_result(v, depth + 1)
            for k, v in list(value.items())[:100]
            if not _SECRET_KEYS.search(str(k)) and not str(k).startswith("_")
        }
    if isinstance(value, (list, tuple)):
        return [safe_result(v, depth + 1) for v in value[:500]]
    if isinstance(value, str):
        value = _PRIVATE_KEY.sub("[redacted]", value)
        value = _URL_AUTH.sub(r"\1[redacted]@", value)
        value = _SECRET_ASSIGNMENT.sub(r"\1[redacted]", value)
        return _SECRET_TEXT.sub("[redacted]", value[:16000])
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (int, bool)):
        return value
    return None


def _public_call(row) -> dict:
    result = dict(row)
    tool = REGISTRY[result["capability_id"]]
    return {
        k: result[k]
        for k in (
            "call_id",
            "run_id",
            "conv_id",
            "capability_id",
            "status",
            "input_summary",
            "result_summary",
            "created_at",
            "completed_at",
        )
    } | {"label": tool.label, "source_kind": tool.source_kind, "target": json.loads(result["target_json"])}


def list_calls(
    username: str, *, conv_id: str = "", run_id: str = "", view: str = "", limit: int = 100
) -> list[dict]:
    clauses = ["username=?"]
    args: list[Any] = [username]
    if conv_id:
        clauses.append("conv_id=?")
        args.append(conv_id)
    if run_id:
        clauses.append("run_id=?")
        args.append(run_id)
    if view:
        ids = [t.id for t in _TOOLS if t.view == view]
        if not ids:
            return []
        clauses.append("capability_id IN (" + ",".join("?" for _ in ids) + ")")
        args.extend(ids)
    args.append(max(1, min(200, limit)))
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM capability_calls WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC, rowid DESC LIMIT ?",
            args,
        ).fetchall()
    return [_public_call(row) for row in reversed(rows)]


async def _execute(name: str, args: Arguments, context: dict) -> tuple[dict, dict, str]:
    """Dispatch only explicitly named application functions, in this server process."""
    user = context["username"]
    data = args.model_dump()
    target = {"view": REGISTRY[name].view}
    if "ticker" in data:
        target["ticker"] = data["ticker"]
    if "id" in data:
        target["id"] = data["id"]
    if name.startswith("market_"):
        from .market.factory import get_chain

        chain = get_chain()
        if name == "market_search":
            hits = await chain.search(data["query"], limit=data["limit"])
            return {"hits": [h.model_dump() for h in hits]}, target, f"找到 {len(hits)} 个匹配标的"
        if name == "market_quote":
            value, stale = await chain.get_quote(data["ticker"])
        elif name == "market_fundamentals":
            value, stale = await chain.get_fundamentals(data["ticker"])
        else:
            bars, stale = await chain.get_kline(**data)
            if not bars:
                raise RuntimeError("kline unavailable")
            return (
                {
                    "ticker": data["ticker"],
                    "period": data["period"],
                    "bars": [b.model_dump() for b in bars],
                    "stale": stale,
                },
                target,
                f"取得 {len(bars)} 根 K 线" + ("（备用过期数据）" if stale else ""),
            )
        return (
            {"value": value.model_dump(), "stale": stale},
            target,
            "已取得平台数据" + ("（备用过期数据）" if stale else ""),
        )
    if name in {"indicator_list", "indicator_eval", "scan_start", "scan_status"}:
        import pandas as pd

        from .indicators import router as indicators
        from .indicators import scanner
        from .indicators.dsl import evaluate, is_signal, validate
        from .indicators.models import ScanRequest

        if name == "indicator_list":
            with get_db() as db:
                indicators._ensure_builtin_library(db, user)
                rows = db.execute(
                    "SELECT * FROM indicators WHERE username=? ORDER BY updated_at DESC LIMIT 100", [user]
                ).fetchall()
            return (
                {"indicators": [indicators._row_to_dict(r) for r in rows]},
                target,
                f"取得 {len(rows)} 个指标",
            )
        if name == "scan_status":
            job = scanner.get_job(data["id"], user)
            if not job:
                raise HTTPException(404)
            return {"job": job}, target, f"扫描状态：{job['status']}"
        indicator = indicators._load_owned(data.pop("indicator_id"), user)
        params = indicators._param_values(indicator.get("params"), data.get("params"))
        if not validate(indicator["dsl"], params):
            raise ValueError("invalid indicator")
        if name == "scan_start":
            req = ScanRequest(**data)
            if req.scope == "watchlist" and not req.tickers:
                raise ValueError("empty watchlist")
            if scanner.active_job(user):
                raise HTTPException(409)
            job = scanner.create_job(user, indicator, req.model_dump())
            scanner.start_job(job, indicator, indicators._build_df)
            target["id"] = job["id"]
            return {"id": job["id"], "status": job["status"]}, target, "扫描已启动，可在选股页面查看进度"
        df, fund = await indicators._build_df(
            data["ticker"], data["period"], include_fundamentals=scanner.uses_fundamentals(indicator["dsl"])
        )
        if df is None or df.empty:
            raise RuntimeError("data unavailable")

        def missing_funds(node):
            if isinstance(node, dict):
                if "fund" in node and fund.get(node["fund"]) is None:
                    return True
                return any(missing_funds(v) for v in node.values())
            return isinstance(node, list) and any(missing_funds(v) for v in node)

        if missing_funds(indicator["dsl"]):
            raise RuntimeError("fundamentals unavailable")
        values = evaluate(indicator["dsl"], df, fund, params)
        target["id"] = indicator["id"]
        signal = is_signal(values)
        result = {
            "indicator_id": indicator["id"],
            "ticker": data["ticker"],
            "kind": "signal" if signal else "plot",
            "points": [
                {"t": int(t), "value": None if pd.isna(v) else bool(v) if signal else float(v)}
                for t, v in zip(df["t"], values, strict=True)
            ],
        }
        return result, target, "指标已计算；结果仅对应本次取得的数据"
    if name.startswith("industry_"):
        from .industry_context import build_industry_context
        from .industry_router import overview, ticker_chain

        if name == "industry_overview":
            value = await overview()
        elif name == "industry_chain":
            value = await ticker_chain(data["ticker"])
        else:
            value = build_industry_context(user)
        value["evidence_notice"] = "现有种子连线未经商业关系核验；平台研究动态不是原始披露。"
        return value, target, "已读取现有产业图谱与研究动态"
    if name.startswith("report_"):
        from .reports import get_saved_report, list_saved_reports

        if name == "report_list":
            records = list_saved_reports(user, data["limit"])
            return {"reports": records}, target, f"找到 {len(records)} 份历史报告"
        report = get_saved_report(user, data["id"])
        if not report:
            raise HTTPException(404)
        return {"report": report}, target, "已读取历史报告，观点和数据日期以报告为准"
    if name.startswith("analysis_"):
        from .analysis_engine import get_analysis_record, start_analysis
        from .research_context import build_research_context

        if name == "analysis_start":
            aid = await start_analysis(**data, date=build_research_context()["date"], username=user)
            target["id"] = aid
            result = get_analysis_record(aid, user) or {"id": aid, "status": "starting"}
            if result.get("status") == "error":
                return result, target, "分析引擎不可用，已保留失败记录；没有启动新研究"
            return result, target, "个股研究已启动，可打开记录查看进度"
        entry = get_analysis_record(data["id"], user)
        if not entry:
            raise HTTPException(404)
        target["ticker"] = entry.get("ticker", "")
        status_label = {
            "starting": "准备中",
            "running": "进行中",
            "done": "已完成",
            "error": "失败",
            "interrupted": "已中断",
        }.get(entry.get("status"), "未知")
        return entry, target, f"研究状态：{status_label}"
    if name.startswith("vibe_"):
        from .models import VibePlanRequest
        from .vibe import _insert_vibe_plan, _load_vibe_plan, _start_task, build_vibe_plan

        if name == "vibe_start":
            plan = build_vibe_plan(VibePlanRequest(**data, agent=True))
            plan["id"] = secrets.token_hex(10)
            _insert_vibe_plan(user, plan)
            _start_task(plan["id"], user)
            target["id"] = plan["id"]
        else:
            plan = _load_vibe_plan(data["id"], user)
            if not plan:
                raise HTTPException(404)
        result = {
            k: plan.get(k)
            for k in (
                "id",
                "title",
                "status",
                "stage_label",
                "progress",
                "created_at",
                "updated_at",
                "report_markdown",
                "candidates",
                "gaps",
                "research",
            )
        }
        return (
            {"plan": result},
            target,
            "Vibe 研究已启动" if name == "vibe_start" else f"Vibe 状态：{plan.get('status', 'unknown')}",
        )
    raise HTTPException(404)


async def dispatch_call(context: dict, request: CallRequest) -> dict:
    tool = REGISTRY.get(request.name)
    if not tool:
        raise HTTPException(404, "平台能力不存在")
    try:
        args = tool.schema.model_validate(request.arguments)
    except ValidationError:
        raise HTTPException(422, "平台能力参数无效") from None
    canonical = json.dumps(args.model_dump(), sort_keys=True, ensure_ascii=False)
    args_hash = hashlib.sha256(canonical.encode()).hexdigest()
    with get_db() as db:
        existing = db.execute(
            "SELECT * FROM capability_calls WHERE run_id=? AND request_id=?",
            [context["run_id"], request.request_id],
        ).fetchone()
        if existing:
            if existing["capability_id"] != tool.id or existing["arguments_hash"] != args_hash:
                raise HTTPException(409, "调用标识已被使用")
            if existing["status"] == "running":
                return {"ok": True, "call": _public_call(existing), "data": {"status": "running"}}
            return json.loads(existing["response_json"])
        count = db.execute(
            "SELECT COUNT(*) FROM capability_calls WHERE run_id=?", [context["run_id"]]
        ).fetchone()[0]
        if count >= 100:
            raise HTTPException(429, "本轮平台调用已达上限")
    if not tool.read_only and not check_rate_limit(f"cap-start:{context['username']}:{tool.id}", 3, 60):
        raise HTTPException(429, "研究发起过于频繁")
    call_id = secrets.token_hex(12)
    target = {"view": tool.view}
    # Do not persist raw prompts or arbitrary user strings in the audit ledger.
    summary = tool.label + (f" · {args.ticker}" if hasattr(args, "ticker") else "")
    with get_db() as db:
        db.execute(
            "INSERT INTO capability_calls(call_id,run_id,username,conv_id,request_id,capability_id,arguments_hash,status,input_summary,target_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [
                call_id,
                context["run_id"],
                context["username"],
                context["conv_id"],
                request.request_id,
                tool.id,
                args_hash,
                "running",
                summary,
                json.dumps(target),
                int(time.time()),
            ],
        )
    error = None
    try:
        value, target, result_summary = await asyncio.wait_for(_execute(tool.id, args, context), timeout=50)
        value = safe_result(value)
        if len(json.dumps(value, ensure_ascii=False)) > 120000:
            value = {"truncated": True, "notice": "完整结果请打开平台记录查看。"}
        status = "completed"
        if tool.id == "analysis_start" and value.get("status") == "error":
            status = "failed"
            error = {"code": "unavailable", "message": result_summary}
    except asyncio.CancelledError:
        with get_db() as db:
            db.execute(
                "UPDATE capability_calls SET status='failed',result_summary='调用已中断',completed_at=? WHERE call_id=?",
                [int(time.time()), call_id],
            )
            row = db.execute("SELECT * FROM capability_calls WHERE call_id=?", [call_id]).fetchone()
            response = {
                "ok": False,
                "call": _public_call(row),
                "data": {},
                "error": {"code": "unavailable", "message": "调用已中断，请查看已有任务记录"},
            }
            db.execute(
                "UPDATE capability_calls SET response_json=? WHERE call_id=?",
                [json.dumps(response, ensure_ascii=False), call_id],
            )
        raise
    except Exception as exc:
        status = "failed"
        if isinstance(exc, HTTPException) and exc.status_code == 404:
            code, result_summary = "not_found", "记录不存在或不可访问"
        elif isinstance(exc, (ValidationError, ValueError, TypeError)):
            code, result_summary = "invalid_arguments", "参数或指标输入无效"
        elif isinstance(exc, HTTPException) and exc.status_code == 409:
            code, result_summary = "conflict", "已有研究任务运行，请先查看已有记录"
        else:
            code, result_summary = "unavailable", "本次平台数据或引擎不可用，请稍后重试"
        value, error = {}, {"code": code, "message": result_summary}
    with get_db() as db:
        db.execute(
            "UPDATE capability_calls SET status=?,result_summary=?,target_json=?,completed_at=? WHERE call_id=?",
            [status, result_summary, json.dumps(target), int(time.time()), call_id],
        )
        row = db.execute("SELECT * FROM capability_calls WHERE call_id=?", [call_id]).fetchone()
        response = {
            "ok": status == "completed",
            "call": _public_call(row),
            "data": value,
            "source_kind": tool.source_kind,
            "evidence_status": "platform_result_not_external_verification",
        }
        if error:
            response["error"] = error
        db.execute(
            "UPDATE capability_calls SET response_json=? WHERE call_id=?",
            [json.dumps(response, ensure_ascii=False, allow_nan=False), call_id],
        )
    return response


@router.get("/capabilities")
async def api_capabilities(request: Request):
    require_auth(request)
    return {"capabilities": capability_catalog()}


@router.get("/convs/{conv_id}/capability-calls")
async def api_conversation_calls(conv_id: str, request: Request):
    user = require_auth(request)
    _owned_conversation(user, conv_id)
    return {"calls": list_calls(user, conv_id=conv_id)}


@router.get("/capability-calls")
async def api_capability_calls(request: Request, view: View | None = None, limit: int = 20):
    user = require_auth(request)
    return {"calls": list_calls(user, view=view or "", limit=limit)}


@router.get("/internal/capabilities/tools")
async def api_bridge_tools(request: Request):
    _authenticate_bridge(request)
    return {
        "tools": [
            {
                "name": t.id,
                "title": t.label,
                "description": t.description,
                "inputSchema": t.schema.model_json_schema(),
                "annotations": {"readOnlyHint": t.read_only, "destructiveHint": False},
            }
            for t in _TOOLS
        ]
    }


@router.post("/internal/capabilities/call")
async def api_bridge_call(request: Request):
    context = _authenticate_bridge(request)
    body = await request.body()
    if len(body) > 32768:
        raise HTTPException(413, "调用参数过大")
    try:
        call = CallRequest.model_validate_json(body)
    except ValidationError:
        raise HTTPException(422, "平台调用格式无效") from None
    return await dispatch_call(context, call)
