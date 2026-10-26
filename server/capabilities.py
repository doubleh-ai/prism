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
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .auth import require_auth
from .config import CAPABILITY_TOKEN_TTL
from .database import check_rate_limit, get_db
from .work_lifecycle import Lease, LifecycleError, current_lease, get_manager

router = APIRouter(prefix="/api")
Ticker = Annotated[str, Field(pattern=r"^[A-Za-z0-9.\-]{1,16}$")]
RecordId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_\-]{1,128}$")]
View = Literal["market", "industry", "indicators", "screener", "reports", "analysis", "vibe"]


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class DataInsufficient(ValueError):
    """The query is valid, but the required observations are unavailable."""


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
    markets: list[Literal["SH", "SZ", "BJ"]] = Field(
        default_factory=lambda: ["SH", "SZ", "BJ"], min_length=1, max_length=3
    )
    boards: list[Literal["main", "gem", "star", "bse"]] = Field(
        default_factory=lambda: ["main", "gem", "star", "bse"], min_length=1, max_length=4
    )
    exclude_st: bool = True
    min_turnover: float = Field(default=0, ge=0, le=100000000000)
    sort: Literal["strength", "recent_signal", "latest_value"] = "strength"
    direction: Literal["asc", "desc"] = "desc"


class TranslateArgs(Arguments):
    input: str = Field(min_length=1, max_length=200)


class IndicatorCreateArgs(Arguments):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=300)
    dsl: dict[str, Any]
    kind: Literal["plot", "signal"]
    params: dict[str, Any] = Field(default_factory=dict, max_length=30)
    plan: dict[str, Any] = Field(default_factory=dict, max_length=20)
    visual: dict[str, Any] = Field(default_factory=dict, max_length=20)


class FollowupArgs(IdArgs):
    question: str = Field(min_length=2, max_length=3000)


class SaveArgs(IdArgs):
    source: Literal["analysis", "vibe", "research"]
    title: str = Field(default="", max_length=160)
    turn_id: RecordId | None = None


class WatchItemArgs(Arguments):
    ticker: Ticker
    name: str = Field(default="", max_length=80)


class WatchUpdateArgs(Arguments):
    operation: Literal["add", "remove"]
    items: list[WatchItemArgs] = Field(min_length=1, max_length=200)


class ClaimsArgs(Arguments):
    claims: list[dict[str, Any]] = Field(min_length=1, max_length=20)


class RelationsArgs(Arguments):
    relations: list[dict[str, Any]] = Field(min_length=1, max_length=20)


class ResearchScopeArgs(Arguments):
    information_cutoff: str = Field(min_length=10, max_length=40)
    requested_period: str | None = Field(default=None, max_length=120)


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
        "本版暂停执行：Vibe 隔离环境尚待验收；可读取本账号已有历史。",
        VibeArgs,
        "platform_research",
        "vibe",
        False,
    ),
    Capability(
        "vibe_status",
        "查看 Vibe 研究",
        "只读取当前用户已保存的主题研究状态和报告；不是旧引擎实时状态，不恢复执行。",
        IdArgs,
        "platform_research",
        "vibe",
    ),
    Capability(
        "watchlist_get",
        "查看我的自选",
        "读取当前账号的自选，不读取其他用户或浏览器旧缓存。",
        EmptyArgs,
        "platform_market",
        "market",
    ),
    Capability(
        "watchlist_update",
        "更新我的自选",
        "按用户要求添加或移除指定自选，返回实际保存结果。",
        WatchUpdateArgs,
        "platform_market",
        "market",
        False,
    ),
    Capability(
        "indicator_translate",
        "设计指标草案",
        "将明确的自然语言条件翻译成受限指标DSL草案；不自动保存。",
        TranslateArgs,
        "platform_indicator",
        "indicators",
    ),
    Capability(
        "indicator_create",
        "保存指标",
        "将用户要求的有效指标DSL存入当前账号指标库。",
        IndicatorCreateArgs,
        "platform_indicator",
        "indicators",
        False,
    ),
    Capability(
        "scan_list",
        "查看已有扫描",
        "列出当前账号已有扫描和实际筛选条件，不新启动扫描。",
        ListArgs,
        "platform_indicator",
        "screener",
    ),
    Capability(
        "scan_cancel",
        "停止扫描",
        "请求取消指定的当前用户扫描；以返回状态为准。",
        IdArgs,
        "platform_indicator",
        "screener",
        False,
    ),
    Capability(
        "analysis_list",
        "查看已有个股研究",
        "列出当前用户已保存的分析任务，不重新执行。",
        ListArgs,
        "platform_research",
        "analysis",
    ),
    Capability(
        "vibe_list",
        "查看已有主题研究",
        "只读取当前用户已保存的主题研究历史，不恢复或查询共享侧车。",
        ListArgs,
        "platform_research",
        "vibe",
    ),
    Capability(
        "vibe_followup",
        "追问主题研究",
        "本版暂停追问：Vibe 隔离环境尚待验收；原报告和时间保持不变。",
        FollowupArgs,
        "platform_research",
        "vibe",
        False,
    ),
    Capability(
        "vibe_cancel",
        "停止主题研究",
        "请求停止指定研究；未得到引擎确认不能当作已停止。",
        IdArgs,
        "platform_research",
        "vibe",
        False,
    ),
    Capability(
        "report_save",
        "保存研究报告",
        "保存当前用户已有研究版本。Vibe默认原报告，保存已完成追问必须指定turn_id。",
        SaveArgs,
        "prior_research",
        "reports",
        False,
    ),
    Capability(
        "research_read",
        "查看本轮证据",
        "读取本轮研究真实工具结果生成的文档、段落、Claim及缺口。",
        EmptyArgs,
        "research_ledger",
        "reports",
    ),
    Capability(
        "research_record_claims",
        "登记本轮研究判断",
        "登记事实/计算/推断/假设候选及支持或反方证据ID；不能自行声明已核验。",
        ClaimsArgs,
        "research_ledger",
        "reports",
        False,
    ),
    Capability(
        "research_record_relations",
        "登记本轮产业机制",
        "登记本轮关系候选、阶段、传导条件、反证与失效条件；不自动升级为已披露事实。",
        RelationsArgs,
        "research_ledger",
        "industry",
        False,
    ),
    Capability(
        "research_scope",
        "固定研究信息截止",
        "历史研究须在检索前固定信息截止与所属期间，保留真实观察时间；已有资料后不得改写范围。",
        ResearchScopeArgs,
        "research_ledger",
        "reports",
        False,
    ),
)
REGISTRY = {tool.id: tool for tool in _TOOLS}
_PAUSED_VIBE_TOOLS = frozenset({"vibe_start", "vibe_followup"})


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
            **(
                {"available": False, "unavailable_reason": "Vibe 隔离执行环境尚待验收"}
                if t.id in _PAUSED_VIBE_TOOLS
                else {}
            ),
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


@dataclass(frozen=True)
class _RunBinding:
    parent: Lease
    username: str
    conv_id: str
    expires_at: int


_run_bindings: dict[str, _RunBinding] = {}
_run_binding_lock = threading.RLock()
_call_finalizers: set[asyncio.Task] = set()


def _run_parent(context: dict) -> Lease:
    with _run_binding_lock:
        now = int(time.time())
        for run_id, binding in list(_run_bindings.items()):
            if binding.expires_at <= now:
                _run_bindings.pop(run_id, None)
        binding = _run_bindings.get(context.get("run_id"))
        if (
            binding is None
            or binding.username != context.get("username")
            or binding.conv_id != context.get("conv_id")
        ):
            raise HTTPException(401, "研究授权无效或已过期")
        return binding.parent


def issue_run_token(username: str, conv_id: str, run_id: str, *, parent_lease=None) -> str:
    _owned_conversation(username, conv_id)
    parent = parent_lease if parent_lease is not None else current_lease()
    if type(parent) is not Lease or parent.kind != "agent":
        raise HTTPException(503, "当前研究进程不可用")
    # A real child admission validates the opaque parent capability against its
    # manager, including a concurrent fence; serialized booleans cannot grant it.
    probe = parent.manager.admit("request", parent=parent, persist_required=False)
    probe.release()
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
    with _run_binding_lock:
        for old_run, binding in list(_run_bindings.items()):
            if binding.expires_at <= now:
                _run_bindings.pop(old_run, None)
        _run_bindings[run_id] = _RunBinding(parent, username, conv_id, now + CAPABILITY_TOKEN_TTL)
    return token


def revoke_run_token(run_id: str) -> None:
    with _run_binding_lock:
        _run_bindings.pop(run_id, None)
    with get_db() as db:
        db.execute("UPDATE capability_runs SET revoked=1 WHERE run_id=?", [run_id])


@contextmanager
def native_run_context(username: str, conv_id: str, run_id: str, *, parent_lease):
    """Bind only a real in-process Agent owner; no bridge credential is issued.

    The ordinary dispatcher still enforces schemas, audit, idempotency and child
    ownership. A browser-supplied context or serialized parent cannot grant work.
    """
    _owned_conversation(username, conv_id)
    if type(parent_lease) is not Lease or parent_lease.kind != "agent":
        raise HTTPException(503, "当前研究任务不可用")
    probe = parent_lease.manager.admit("request", parent=parent_lease, persist_required=False)
    probe.release()
    now = int(time.time())
    with get_db() as db:
        if not db.execute(
            "SELECT 1 FROM research_runs WHERE run_id=? AND username=? AND conv_id=?",
            [run_id, username, conv_id],
        ).fetchone():
            raise HTTPException(404, "研究不存在或不可访问")
        db.execute(
            "INSERT INTO capability_runs(run_id,username,conv_id,token_hash,expires_at,created_at) "
            "VALUES(?,?,?,?,?,?)",
            [
                run_id,
                username,
                conv_id,
                hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
                now + CAPABILITY_TOKEN_TTL,
                now,
            ],
        )
    with _run_binding_lock:
        if run_id in _run_bindings:
            raise HTTPException(409, "研究任务已经绑定")
        _run_bindings[run_id] = _RunBinding(parent_lease, username, conv_id, now + CAPABILITY_TOKEN_TTL)
    try:
        yield {"username": username, "conv_id": conv_id, "run_id": run_id}
    finally:
        revoke_run_token(run_id)


def recover_interrupted_calls() -> None:
    """Single-worker startup only: old subprocess grants never survive restart."""
    with _run_binding_lock:
        _run_bindings.clear()
    with get_db() as db:
        db.execute("UPDATE capability_runs SET revoked=1 WHERE revoked=0")
        db.execute(
            "UPDATE capability_calls SET status='interrupted',result_summary='原调用进程已结束；请查看已有任务，未自动重发',completed_at=? WHERE status='running'",
            [int(time.time())],
        )


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
            "SELECT * FROM capability_runs WHERE token_hash=?",
            [digest],
        ).fetchone()
    if not row or row["revoked"] or row["expires_at"] <= int(time.time()):
        if row:
            with _run_binding_lock:
                _run_bindings.pop(row["run_id"], None)
        raise HTTPException(401, "研究授权无效或已过期")
    context = dict(row)
    _owned_conversation(context["username"], context["conv_id"])
    _run_parent(context)
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
    tool = REGISTRY.get(result["capability_id"])
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
    } | {
        "label": tool.label if tool else result["capability_id"],
        "source_kind": tool.source_kind if tool else "unknown",
        "target": json.loads(result["target_json"]),
    }


def list_calls(
    username: str, *, conv_id: str = "", run_id: str = "", view: str = "", limit: int = 100
) -> list[dict]:
    clauses = [
        "username=?",
        "EXISTS(SELECT 1 FROM conversations c WHERE c.id=capability_calls.conv_id AND c.username=capability_calls.username AND c.deleted_at IS NULL)",
    ]
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
    if name in _PAUSED_VIBE_TOOLS:
        from .vibe import require_vibe_execution

        require_vibe_execution()
    user = context["username"]
    data = args.model_dump()
    target = {"view": REGISTRY[name].view}
    if "ticker" in data:
        target["ticker"] = data["ticker"]
    if "id" in data:
        target["id"] = data["id"]
    if "period" in data and name in {"market_kline", "indicator_eval"}:
        target["period"] = data["period"]
    if "limit" in data and name == "market_kline":
        target["limit"] = data["limit"]
    if name.startswith("research_"):
        from .research import read_research_run, record_claims, record_relations

        target["run_id"] = context["run_id"]
        if name == "research_scope":
            from .research import set_research_scope

            return (
                set_research_scope(
                    user, context["run_id"], data["information_cutoff"], data["requested_period"]
                ),
                target,
                "研究信息截止已固定，观察时间保留",
            )
        if name == "research_read":
            result = read_research_run(user, context["run_id"])
            return result, target, "已读取本轮证据账本；出处关联不等于语义已核验"
        if name == "research_record_relations":
            return (
                record_relations(user, context["run_id"], data["relations"]),
                target,
                "产业机制候选已登记，未自动宣称事实已核验",
            )
        result = record_claims(user, context["run_id"], data["claims"])
        return result, target, "本轮判断已登记，按实际引用检查结果显示缺口"
    if name.startswith("watchlist_"):
        from .watchlist import get_watchlist, set_watchlist

        items = get_watchlist(user)
        if name == "watchlist_update":
            indexed = {item["ticker"]: item for item in items}
            for item in data["items"]:
                if data["operation"] == "add":
                    indexed[item["ticker"]] = item
                else:
                    indexed.pop(item["ticker"], None)
            items = set_watchlist(user, list(indexed.values()))
        return {"items": items}, target, f"当前账号共有 {len(items)} 个自选标的"
    if name == "report_save":
        from .reports import save_completed_analysis, save_report_snapshot, save_vibe_report

        if data["turn_id"] and data["source"] != "vibe":
            raise ValueError("turn_id is only valid for Vibe follow-ups")

        if data["source"] == "analysis":
            saved = save_completed_analysis(user, data["id"], title=data["title"], conv_id=context["conv_id"])
        elif data["source"] == "vibe":
            saved = save_vibe_report(
                user, data["id"], title=data["title"], turn_id=data["turn_id"], conv_id=context["conv_id"]
            )
        else:
            from .research import read_research_run

            ledger = read_research_run(user, data["id"])
            revisions = ledger.get("report_revisions") or []
            if not revisions:
                raise HTTPException(409, "研究尚未产生报告版本")
            latest = revisions[-1]
            revision = latest.get("id") or latest.get("revision_id")
            saved = save_report_snapshot(
                user,
                {
                    "source": "research",
                    "source_id": str(revision),
                    "conv_id": context["conv_id"],
                    "title": data["title"] or "研究报告",
                    "report": {
                        "research_run_id": data["id"],
                        "research_revision_id": revision,
                        "research": ledger.get("run", {}),
                        "markdown": latest.get("narrative", ""),
                    },
                },
            )
        target["id"] = saved["id"]
        return {"report": saved}, target, "已保存该研究版本，后续追问不会覆盖"
    if name == "analysis_list":
        from .analysis_engine import get_analysis_record

        with get_db() as db:
            rows = db.execute(
                "SELECT id FROM analysis_runs WHERE username=? ORDER BY started_at DESC LIMIT ?",
                [user, data["limit"]],
            ).fetchall()
        fields = ("id", "ticker", "name", "status", "mode", "trade_date", "started_at", "research")
        records = [
            {key: record.get(key) for key in fields}
            for row in rows
            if (record := get_analysis_record(row["id"], user))
        ]
        return {"analyses": records}, target, f"找到 {len(records)} 条已有个股研究"
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
            from .market.models import describe_kline

            bars, stale = await chain.get_kline(**data)
            if not bars:
                raise RuntimeError("kline unavailable")
            return (
                {
                    "ticker": data["ticker"],
                    "period": data["period"],
                    "bars": [b.model_dump(exclude={"provenance"}) for b in bars],
                    **describe_kline(bars, stale=stale),
                },
                target,
                f"取得 {len(bars)} 根 K 线" + ("（使用已过期缓存）" if stale else ""),
            )
        return (
            {"value": value.model_dump(), "stale": stale},
            target,
            "已取得平台数据" + ("（备用过期数据）" if stale else ""),
        )
    if name in {
        "indicator_list",
        "indicator_eval",
        "indicator_translate",
        "indicator_create",
        "scan_start",
        "scan_status",
        "scan_list",
        "scan_cancel",
    }:
        import pandas as pd

        from .indicators import router as indicators
        from .indicators import scanner
        from .indicators.dsl import MissingData, evaluate, is_signal, validate
        from .indicators.models import ScanRequest

        if name == "indicator_translate":
            from .indicators.translator import translate

            result = await get_manager().run_blocking("capability", translate, data["input"])
            if not result:
                raise ValueError("indicator translation unavailable")
            return {"draft": indicators.enrich_indicator(result)}, target, "已生成指标草案，尚未保存或扫描"
        if name == "indicator_create":
            from .indicators.models import IndicatorCreate

            result = indicators.create_owned_indicator(user, IndicatorCreate(**data, source="agent"))
            target["id"] = result["id"]
            return result, target, "指标已保存到当前账号"
        if name == "scan_list":
            scanner.reconcile_jobs(user)
            records = scanner.list_jobs(user, data["limit"])
            return {"jobs": records}, target, f"找到 {len(records)} 个已有扫描"
        if name == "scan_cancel":
            if not scanner.cancel_job(data["id"], user):
                raise HTTPException(404)
            return {"job": scanner.get_job(data["id"], user)}, target, "已请求停止扫描，请核对任务状态"

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
            scanner.reconcile_jobs(user)
            job = scanner.get_job(data["id"], user)
            if not job:
                raise HTTPException(404)
            return {"job": job}, target, f"扫描状态：{job['status']}"
        indicator = indicators._load_owned(data.pop("indicator_id"), user)
        params = indicators._param_values(indicator.get("params"), data.get("params"))
        if not validate(indicator["dsl"], params):
            raise ValueError("invalid indicator")
        if name == "scan_start":
            if data["scope"] == "watchlist" and not data["tickers"]:
                from .watchlist import get_watchlist

                items = get_watchlist(user)
                data["tickers"] = [item["ticker"] for item in items]
                data["ticker_names"] = {item["ticker"]: item["name"] for item in items}
            req = ScanRequest(**data)
            if req.scope == "watchlist" and not req.tickers:
                raise ValueError("empty watchlist")
            if scanner.active_job(user):
                raise HTTPException(409)
            job = scanner.create_job(user, indicator, req.model_dump())
            scanner.start_job(job, indicator, indicators._build_df)
            target["id"] = job["id"]
            return (
                {"id": job["id"], "status": job["status"], "config": req.model_dump()},
                target,
                "扫描已启动，可在选股页面查看进度与实际条件",
            )
        df, fund = await indicators._build_df(
            data["ticker"], data["period"], include_fundamentals=scanner.uses_fundamentals(indicator["dsl"])
        )
        if df is None or df.empty:
            raise RuntimeError("data unavailable")

        try:
            values = evaluate(indicator["dsl"], df, fund, params)
        except MissingData:
            raise DataInsufficient("required fundamentals unavailable") from None
        if values.empty or bool(values.iloc[-1:].isna().any()):
            raise DataInsufficient("latest indicator is not available")
        target["id"] = indicator["id"]
        signal = is_signal(values)
        result = {
            "indicator_id": indicator["id"],
            "ticker": data["ticker"],
            "period": data["period"],
            "params": params,
            "kline": df.to_dict("records"),
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
        from .vibe import (
            _insert_vibe_plan,
            _start_task,
            build_vibe_plan,
            cancel_owned_plan,
            execution_status,
            get_owned_plan,
            list_owned_plans,
            start_owned_followup,
        )

        if name == "vibe_list":
            fields = (
                "id",
                "title",
                "status",
                "stage_label",
                "created_at",
                "updated_at",
                "research",
                "report_research",
            )
            records = [
                {key: plan.get(key) for key in fields} for plan in list_owned_plans(user, data["limit"])
            ]
            return (
                {"plans": records, "history_only": True, "execution": execution_status()},
                target,
                f"找到 {len(records)} 条已保存主题研究；本版不连接旧引擎补取或恢复执行",
            )
        if name == "vibe_cancel":
            result = await cancel_owned_plan(user, data["id"])
            return result, target, "研究已停止" if result["cancelled"] else "尚未确认引擎停止，请核对当前任务"
        if name == "vibe_followup":
            plan = start_owned_followup(user, data["id"], data["question"])
            return {"plan": plan}, target, "追问已排队，原报告保留"

        if name == "vibe_start":
            plan = build_vibe_plan(VibePlanRequest(**data, agent=True))
            plan["id"] = secrets.token_hex(10)
            _insert_vibe_plan(user, plan)
            _start_task(plan["id"], user)
            target["id"] = plan["id"]
        else:
            plan = get_owned_plan(user, data["id"])
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
                "report_research",
                "report_quality",
                "followups",
            )
        }
        return (
            {"plan": result, "history_only": True, "execution": execution_status()},
            target,
            "Vibe 研究已启动"
            if name == "vibe_start"
            else f"已保存 Vibe 记录状态：{plan.get('status', 'unknown')}；不是旧引擎实时状态，本版暂停执行",
        )
    raise HTTPException(404)


@contextmanager
def _terminal_db():
    """The terminal status and replayable response commit together, without await."""
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise


async def dispatch_call(context: dict, request: CallRequest) -> dict:
    parent = _run_parent(context)
    if request.name in _PAUSED_VIBE_TOOLS:
        from .vibe import require_vibe_execution

        # Reject before audit/task creation and before idempotent replay of an
        # earlier execution response can imply that this release starts Vibe.
        require_vibe_execution()
    tool = REGISTRY.get(request.name)
    if not tool:
        raise HTTPException(404, "平台能力不存在")
    try:
        args = tool.schema.model_validate(request.arguments)
    except ValidationError:
        raise HTTPException(422, "平台能力参数无效") from None
    try:
        canonical = json.dumps(args.model_dump(), sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError):
        raise HTTPException(422, "平台能力参数无效") from None
    if len(canonical.encode()) > 32768:
        raise HTTPException(413, "调用参数过大")
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
    try:
        with _run_binding_lock:
            parent = _run_parent(context)
            lease = parent.manager.admit("capability", parent=parent, persist_required=True)
    except LifecycleError:
        raise HTTPException(503, "当前研究进程不可用") from None
    expected_response = {}

    async def owned_call():
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
            # Snapshots retain reproducible market/indicator conditions, never raw
            # prompts, follow-up questions or model-authored claim prose in audit.
            replay_args = {
                key: value
                for key, value in args.model_dump().items()
                if key not in {"prompt", "question", "input", "claims", "relations"}
            }
            db.execute(
                "UPDATE capability_calls SET arguments_json=? WHERE call_id=?",
                [json.dumps(safe_result(replay_args), ensure_ascii=False, allow_nan=False), call_id],
            )
        error = None
        try:
            value, target, result_summary = await asyncio.wait_for(
                _execute(tool.id, args, context), timeout=50
            )
            value = safe_result(value)
            if len(json.dumps(value, ensure_ascii=False)) > 120000:
                value = {"truncated": True, "notice": "完整结果请打开平台记录查看。"}
            status = "completed"
            if tool.id == "analysis_start" and value.get("status") == "error":
                status = "failed"
                error = {"code": "unavailable", "message": result_summary}
        except asyncio.CancelledError:
            with _terminal_db() as db:
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
            expected_response["value"] = json.dumps(response, ensure_ascii=False, allow_nan=False)
            raise
        except Exception as exc:
            status = "failed"
            if isinstance(exc, HTTPException) and exc.status_code == 404:
                code, result_summary = "not_found", "记录不存在或不可访问"
            elif isinstance(exc, DataInsufficient):
                code, result_summary = "data_insufficient", "所需资料或最新指标预热不足，未生成有效计算结果"
            elif (
                isinstance(exc, (ValidationError, ValueError, TypeError))
                or isinstance(exc, HTTPException)
                and exc.status_code in {400, 422}
            ):
                code, result_summary = "invalid_arguments", "参数或指标输入无效"
            elif isinstance(exc, HTTPException) and exc.status_code == 409:
                code, result_summary = "conflict", "已有研究任务运行，请先查看已有记录"
            else:
                code, result_summary = "unavailable", "本次平台数据或引擎不可用，请稍后重试"
            value, error = {}, {"code": code, "message": result_summary}
        with _terminal_db() as db:
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
        expected_response["value"] = json.dumps(response, ensure_ascii=False, allow_nan=False)
        return response

    def verify_terminal():
        with get_db() as db:
            row = db.execute(
                "SELECT status,response_json FROM capability_calls WHERE call_id=? AND run_id=?",
                [call_id, context["run_id"]],
            ).fetchone()
        expected = expected_response.get("value")
        if (
            not row
            or row["status"] not in {"completed", "failed"}
            or expected is None
            or json.loads(row["response_json"]) != json.loads(expected)
        ):
            raise RuntimeError("capability_terminal_persistence_unconfirmed")

    async def complete():
        try:
            await lease.wait_owned()
            await lease.finish(verify_terminal)
        finally:
            lease.release()

    failure, response = None, None
    with lease:
        try:
            response = await owned_call()
        except BaseException as exc:
            failure = exc
            if not expected_response:
                lease.block("persistence_failed")
    finalizer = asyncio.create_task(complete())
    _call_finalizers.add(finalizer)

    def completed(task):
        _call_finalizers.discard(task)
        if not task.cancelled():
            task.exception()

    finalizer.add_done_callback(completed)
    # With no retained descendants finish completes now. A start response remains
    # immediate while its real business child and this durable parent stay owned.
    await asyncio.sleep(0)
    if failure is not None:
        raise failure
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


def get_call_snapshot(username: str, call_id: str) -> dict:
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM capability_calls WHERE call_id=? AND username=?", [call_id, username]
        ).fetchone()
    if not row:
        raise HTTPException(404, "调用记录不存在或不可访问")
    _owned_conversation(username, row["conv_id"])
    response = json.loads(row["response_json"] or "{}")
    call = _public_call(row)
    return {
        "call": call,
        "params": json.loads(row["arguments_json"] or "{}"),
        "result": response.get("data", {}),
        "target": call["target"],
        "error": response.get("error"),
        "notice": "原调用快照，未重新请求行情或计算；私密自由文本不在调用参数账本中保存。",
    }


@router.get("/capabilities/calls/{call_id}")
@router.get("/capability-calls/{call_id}")
async def api_call_snapshot(call_id: str, request: Request):
    return get_call_snapshot(require_auth(request), call_id)


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
            if t.id not in _PAUSED_VIBE_TOOLS
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
