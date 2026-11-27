"""Explicit in-process Agent tools; no CLI, MCP bridge or self-HTTP dispatch.

Platform work retains the original dispatcher/audit/owner checks. Additional
source/knowledge work stays under the real Agent lease's owned blocking pool.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import time
from datetime import date
from urllib.parse import urlsplit

import httpx

from ..research_context import EvidenceTracker
from ..source_urls import public_source_url, redact_source_text
from ..work_lifecycle import Lease

PLATFORM_TOOLS = (
    "futures_snapshot",
    "futures_inventory",
    "futures_spread",
    "futures_basis",
    "futures_spot_history",
    "market_quote",
    "market_fundamentals",
    "market_kline",
    "market_search",
    "indicator_list",
    "indicator_eval",
    "scan_status",
    "industry_overview",
    "industry_chain",
    "industry_context",
    "report_list",
    "report_get",
    "analysis_status",
    "watchlist_get",
    "research_read",
    "research_record_claims",
    "research_record_relations",
    "research_scope",
)
MAX_ARGUMENT_BYTES = 32768
# One claim batch carries the report's numbers with verbatim quotes, so it is far
# larger than a search or read call. Production run 8c746a5a submitted a
# legitimate thirteen-claim batch above the general cap; the argument-shape
# checks rejected it before pydantic saw it and no claim could be retried. The
# claim tool therefore gets its own allowance while every other tool keeps the
# general cap.
MAX_CLAIM_ARGUMENT_BYTES = 131072
_ARGUMENT_BYTES = {"research_record_claims": MAX_CLAIM_ARGUMENT_BYTES}
MAX_OUTPUT_BYTES = 120000
_CUSTOM = {
    "portfolio_risk": ("核对我的持仓风险", "portfolio_snapshot"),
    "futures_intelligence": ("读取期货V2分位与产业利润", "platform_data"),
    "persona_propose_trait": ("提议写入投研画像", "historical_research"),
    "knowledge_search": ("检索我的历史研究", "historical_research"),
    "knowledge_get": ("读取我的历史研究", "historical_research"),
    "source_read": ("读取公开资料节选", "external_web"),
    "source_find": ("查找公开资料", "external_web"),
    "source_original": ("获取巨潮公告原文", "external_web"),
}
_ERRORS = {
    "risk_permission_required": "请先在持仓与风险检查中允许公开行情对价及新研究风险摘要，再新建研究；本次未查询行情",
    "risk_snapshot_changed": "持仓、授权或研究状态已变化，请核对后新建研究；本次结果未返回",
    "unknown_tool": "该工具不可用",
    "invalid_arguments": "工具参数无效",
    "unavailable_context": "当前研究授权不可用",
    "not_found": "记录不存在或不可访问",
    "conflict": "调用标识已被使用",
    "rate_limited": "本轮工具调用已达上限",
    "source_unavailable": "原始页面资料暂不可用，未形成有效节选",
    "data_insufficient": "所需资料不足，未形成有效计算结果",
    "original_required": "本次研究尚未取得任何原文文档，标 verified_source 的登记已被拒绝：请先调用 source_original 取得公告PDF原文并记录 document_id 和命中页码，或把该结论改标 secondhand 后重新登记。",
    "unavailable": "本次工具未能完成，请保留研究缺口",
}


# "arguments_too_large" is a retryable shape failure: the model can submit the
# same claims in a smaller batch, so the runtime treats it like a pydantic
# rejection instead of an opaque "other" error.
_ERROR_KINDS = ("pydantic", "provider", "timeout", "arguments_too_large", "other")
_MAX_ERROR_DETAILS = 3


class NativeToolError(RuntimeError):
    """Tool failure carrying a stable code plus privacy-safe failure details.

    ``details`` only ever contains pydantic field paths and error types: tool
    arguments may contain research content, so submitted values never leave the
    model call. ``kind`` classifies the origin for observability events.
    """

    def __init__(self, code="unavailable", *, details=(), kind="other"):
        self.code = code if code in _ERRORS else "unavailable"
        self.details = _clean_details(details)
        self.kind = kind if kind in _ERROR_KINDS else "other"
        super().__init__(self.code)


def _clean_details(details):
    cleaned = []
    for item in details or ():
        if not isinstance(item, dict):
            continue
        path, kind = item.get("loc"), item.get("type")
        if type(path) is not str or type(kind) is not str or not path or not kind:
            continue
        cleaned.append({"loc": path[:200], "type": kind[:80]})
        if len(cleaned) >= _MAX_ERROR_DETAILS:
            break
    return tuple(cleaned)


def _pydantic_details(error):
    """Keep the first failing field paths and error types from a validation error.

    Values are deliberately dropped: a rejected claim carries report text and
    source references, so only ``loc``/``type`` may leave the tool.
    """
    collect = getattr(error, "errors", None)
    if not callable(collect):
        return []
    details = []
    for item in collect():
        if not isinstance(item, dict):
            continue
        location = item.get("loc") or ()
        path = ".".join(str(part) for part in location if type(part) in {str, int})
        kind = item.get("type")
        if path and type(kind) is str:
            details.append({"loc": path, "type": kind})
        if len(details) >= _MAX_ERROR_DETAILS:
            break
    return details


def _failure_kind(error):
    if isinstance(error, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(error, httpx.HTTPError) or getattr(error, "status_code", None) is not None:
        return "provider"
    return "other"


def _platform():
    from .. import capabilities

    return capabilities


def _custom_definitions():
    from .discovery import SCHEMA

    return [
        {
            "name": "portfolio_risk",
            "description": "在本人已允许行情对价及风险摘要分享时，按本轮固定持仓版本核对风险。只返回汇总金额、规则和行情日期，不返回代码、数量或持仓明细；保留缺项与假设，不连接券商、不下单。一轮只采集一次；变更或撤销授权需新建研究。",
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "futures_intelligence",
            "description": "读取期货研究 V2 本体引擎的品种全景情报：包含近5年历史价格分位、历年同期季节性分位与同期中位数、20日波动率热度分位、跨品种产业链盘面利润（如钢厂利润、压榨利润、金银比等）、近端现实与中期结构因果摘要、主力换月预警以及期股联动A股龙头映射。支持全部46个活跃品种代码（如 AU、CU、RB、M、SC、PS、LC、EC 等）。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "pattern": "^[A-Za-z]{1,3}$",
                        "description": "期货品种代码，如 AU、RB、PS、LC、M、EC。",
                    }
                },
                "required": ["symbol"],
                "additionalProperties": False,
            },
        },
        {
            "name": "persona_propose_trait",
            "description": "当用户在对话中明确表达希望长期遵循的个人研究习惯、投资纪律、负面回避准则或输出格式偏好时，生成一张待用户人工点击确认的画像记忆提议卡（不会未经确认静默改写用户画像）。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "trait_text": {
                        "type": "string",
                        "minLength": 2,
                        "maxLength": 200,
                        "description": "简练清晰的个人投研准则描述，如‘分析消费股优先核对经营现金流与合同负债变化’。",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["discipline", "focus", "format", "avoid"],
                        "default": "focus",
                    },
                    "reason": {
                        "type": "string",
                        "maxLength": 240,
                        "description": "简短说明提议依据。",
                    },
                },
                "required": ["trait_text"],
                "additionalProperties": False,
            },
        },
        {
            "name": "source_find",
            "description": "用短的公开关键词或沪深六位证券代码查找公告与新闻线索，不需猜测URL或提供搜索密钥。覆盖上交所和深交所公告、新浪滚动索引及财联社最新电报首页；新浪和财联社仅在有界列表内匹配，非全文历史搜索。每次最多两个来源，保留日期、分页、来源和缺口。搜索结果和摘要只是线索，不能直接登记为 claim 来源；涉及财务数字时必须先用 source_original 取得原文。不要发送用户持仓、个人信息或凭证。",
            "input_schema": copy.deepcopy(SCHEMA),
        },
        {
            "name": "source_original",
            "description": "取得 verified_source 的唯一路径：按公开关键词在巨潮资讯网检索首个匹配公告，下载完整PDF原文并把每个物理页登记为本轮证据。凡涉及财报、公告或交易披露中的财务数字，必须先调用本工具定位原文页码，再用返回的 document_id 和 page 填写 source_locator；本轮没有本工具返回的原文文档时，服务端会拒绝 verified_source 登记并返回 original_required。返回 document_id、页数、文件sha256及目标数字命中页；原文按页保存但内容仍待语义核验。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 160},
                    "start_date": {
                        "type": "string",
                        "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
                        "description": "公告日期范围起点，可与end_date一起限定。",
                    },
                    "end_date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
                    "contains": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 100,
                        "description": "需要在原文中核对的数字，如170,899,152,276.34；返回命中页和上下文。",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "knowledge_search",
            "description": "检索当前账号的历史研究；排除本轮，历史观点不等于最新事实。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 160},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "knowledge_get",
            "description": "读取当前账号的一份历史研究；不可读取本轮或其他账号资料。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "pattern": "^(research|report):[A-Za-z0-9_-]{1,128}$"}
                },
                "required": ["id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "source_read",
            "description": "读取获准公开来源的实际节选。PDF 默认前10页；可先查目录，再指定物理页 first_page 和 page_count 读取相关章节，勿要求用户提供页码。节选不代表全文已读或事实已核验，也不会登记原文文档；涉及财务数字须先用 source_original 取得原文页。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "maxLength": 2048, "format": "uri"},
                    "first_page": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 2000,
                        "description": "仅用于 PDF，从1开始的物理页码，不是文中印刷页码。",
                    },
                    "page_count": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["url"],
                "additionalProperties": False,
            },
        },
    ]


def native_catalog() -> list[dict]:
    """Static public catalogue; no fake lease, transport or execution authority."""
    rows = [copy.deepcopy(row) for row in _platform().capability_catalog() if row["id"] in PLATFORM_TOOLS]
    for spec in _custom_definitions():
        label, kind = _CUSTOM[spec["name"]]
        rows.append(
            {
                "id": spec["name"],
                "label": label,
                "description": spec["description"],
                "input_schema": copy.deepcopy(spec["input_schema"]),
                "source_kind": kind,
                "view": "reports",
                "target_view": "reports",
                "read_only": True,
            }
        )
    return rows


def _fetch_source(url, *, allowed_hosts, **page_selection):
    from .sources import fetch_public_source

    return fetch_public_source(url, allowed_hosts=allowed_hosts, **page_selection)


def _find_sources(arguments):
    from .discovery import find

    return find(arguments)


def _knowledge_call(name, username, arguments, run_id):
    from . import knowledge

    if name == "knowledge_search":
        value = knowledge.search(
            username, arguments["query"], limit=arguments.get("limit", 8), exclude_run_id=run_id
        )
        if value.get("reviewed_claims"):
            from ..research.knowledge import record_usage

            record_usage(username, run_id, value["reviewed_claims"])
        return value
    value = knowledge.get(username, arguments["id"])
    if (
        isinstance(value, dict)
        and isinstance(value.get("document"), dict)
        and value["document"].get("run_id") == run_id
    ):
        raise NativeToolError("not_found")
    return value


def _ingest(username, run_id, call, block, refs):
    from ..research.ledger import ingest_tool_result

    return ingest_tool_result(username, run_id, call, block, refs)


def _original_fetch(arguments):
    from .origin_source import fetch_original

    return fetch_original(arguments)


def _ingest_original(username, run_id, call, payload):
    from ..research.ledger import ingest_original_document

    return ingest_original_document(username, run_id, call, payload)


def _snapshot_evidence(username, run_id, call, observation):
    """Render one futures snapshot as evidence pages and record it for this run.

    Runs in the auxiliary worker: rendering and the SQLite write never belong on
    the event loop. ``None`` means the observation is too incomplete to register;
    the tool result itself is left untouched either way.
    """
    from ..futures import snapshot_document
    from ..research.ledger import ingest_market_snapshot

    document = snapshot_document(observation)
    if document is None:
        return None
    return ingest_market_snapshot(username, run_id, call, document)


def _inventory_evidence(username, run_id, call, observation):
    """Render one collected inventory observation as evidence pages for this run.

    Mirrors ``_snapshot_evidence``; ``futures_inventory.snapshot_document``
    returns ``None`` for an unavailable product, so a missing warrant report
    never becomes a document.
    """
    from ..futures_inventory import snapshot_document
    from ..research.ledger import ingest_market_snapshot

    document = snapshot_document(observation)
    if document is None:
        return None
    return ingest_market_snapshot(
        username,
        run_id,
        call,
        document,
        access_method="futures_inventory",
    )


def _spread_evidence(username, run_id, call, observation):
    """Render one collected spread as evidence pages and record it for this run.

    Mirrors ``_snapshot_evidence``; ``spread_document`` returns ``None`` for an
    unavailable spread, so a missing month or price never becomes a document.
    """
    from ..futures_spread import spread_document
    from ..research.ledger import ingest_market_snapshot

    document = spread_document(observation)
    if document is None:
        return None
    return ingest_market_snapshot(
        username,
        run_id,
        call,
        document,
        access_method="futures_spread",
        request={
            "symbol": document.get("symbol"),
            "trading_day": document.get("data_period"),
        },
    )


def _basis_evidence(username, run_id, call, observation):
    """Render one collected spot-futures basis as evidence pages for this run.

    Mirrors ``_snapshot_evidence``; ``futures_basis.snapshot_document`` returns
    ``None`` for an unavailable basis, so an unsolved check page, a missing
    quote or an inconsistent unit never becomes a document.
    """
    from ..futures_basis import snapshot_document
    from ..research.ledger import ingest_market_snapshot

    document = snapshot_document(observation)
    if document is None:
        return None
    return ingest_market_snapshot(
        username,
        run_id,
        call,
        document,
        access_method="futures_basis",
        request={
            "symbol": document.get("symbol"),
            "date": document.get("data_period"),
        },
    )


def _spot_history_evidence(username, run_id, call, observation):
    """Render one collected spot-price series as evidence pages for this run.

    Mirrors ``_snapshot_evidence``; ``futures_spot_history.history_document``
    returns ``None`` for an unavailable or incomplete series, so missing dates
    never become numbers and only an observed, unit-checked series is stored as
    this run's original document.
    """
    from ..futures_spot_history import history_document
    from ..research.ledger import ingest_market_snapshot

    document = history_document(observation)
    if document is None:
        return None
    return ingest_market_snapshot(
        username,
        run_id,
        call,
        document,
        access_method="futures_spot_history",
        request={
            "symbol": document.get("symbol"),
            "start_date": observation.get("requested_start"),
            "end_date": observation.get("requested_end"),
        },
    )


_MARKET_EVIDENCE = {
    "futures_snapshot": _snapshot_evidence,
    "futures_inventory": _inventory_evidence,
    "futures_spread": _spread_evidence,
    "futures_basis": _basis_evidence,
    "futures_spot_history": _spot_history_evidence,
}


def _encoded(value):
    try:
        return json.dumps(
            value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise NativeToolError("invalid_arguments") from None


def _valid_url(value):
    if type(value) is not str or not 1 <= len(value) <= 2048 or public_source_url(value) is None:
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme == "https" and parsed.port in (None, 443)
    except ValueError:
        return False


def _iso_date(value):
    if type(value) is not str or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


class NativeTools:
    def __init__(
        self,
        context: dict,
        lease: Lease,
        evidence: EvidenceTracker,
        *,
        allowed_source_hosts: tuple[str, ...] = (),
    ):
        if (
            type(context) is not dict
            or set(context) != {"username", "conv_id", "run_id"}
            or not all(type(v) is str and 0 < len(v) <= 200 for v in context.values())
            or type(lease) is not Lease
            or lease.kind != "agent"
            or type(evidence) is not EvidenceTracker
            or evidence.run_id != context["run_id"]
        ):
            raise NativeToolError("unavailable_context")
        if (
            type(allowed_source_hosts) is not tuple
            or len(allowed_source_hosts) > 32
            or not all(
                type(h) is str and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", h)
                for h in allowed_source_hosts
            )
        ):
            raise NativeToolError("invalid_arguments")
        self.context = dict(context)
        self.lease, self.evidence = lease, evidence
        self.allowed_source_hosts = tuple(h.lower() for h in allowed_source_hosts)
        self._cache = {}
        self._source_dates = {}
        self._risk_result = None
        self._lock = asyncio.Lock()
        self._authorize()

    def _authorize(self):
        try:
            platform = _platform()
            if platform._run_parent(self.context) is not self.lease:
                raise NativeToolError("unavailable_context")
            platform._owned_conversation(self.context["username"], self.context["conv_id"])
            probe = self.lease.manager.admit("request", parent=self.lease, persist_required=False)
            probe.release()
        except Exception:
            raise NativeToolError("unavailable_context") from None

    def definitions(self) -> list[dict]:
        self._authorize()
        return [
            {"name": row["id"], "description": row["description"], "input_schema": row["input_schema"]}
            for row in native_catalog()
        ]

    def _validate(self, name, arguments, call_id):
        if type(name) is not str or name not in {*PLATFORM_TOOLS, *_CUSTOM}:
            raise NativeToolError("unknown_tool")
        if type(arguments) is not dict:
            # The provider substitutes None when a streamed tool_use input JSON
            # cannot be parsed; keep that distinguishable from schema failures.
            raise NativeToolError(
                "invalid_arguments",
                details=[{"loc": "arguments", "type": "invalid_json"}],
            )
        if (
            type(call_id) is not str
            or re.fullmatch(r"[A-Za-z0-9_-]{1,200}", call_id) is None
            or not all(type(k) is str for k in arguments)
            or len(arguments) > 30
        ):
            raise NativeToolError("invalid_arguments")
        if len(_encoded(arguments)) > _ARGUMENT_BYTES.get(name, MAX_ARGUMENT_BYTES):
            # Distinguishable from an opaque rejection: the same claims in a
            # smaller batch are a valid retry, so the runtime may buy one more
            # claim call instead of losing the whole batch.
            raise NativeToolError(
                "invalid_arguments",
                details=[{"loc": "arguments", "type": "arguments_too_large"}],
                kind="arguments_too_large",
            )
        if name in PLATFORM_TOOLS:
            spec = _platform().REGISTRY.get(name)
            if spec is None:
                raise NativeToolError("unknown_tool")
            try:
                spec.schema.model_validate(arguments)
            except Exception as error:
                raise NativeToolError(
                    "invalid_arguments",
                    details=_pydantic_details(error),
                    kind="pydantic",
                ) from None
        elif name == "portfolio_risk":
            if arguments:
                raise NativeToolError("invalid_arguments")
        elif name == "futures_intelligence":
            from .. import futures_workspace

            if (
                set(arguments) != {"symbol"}
                or type(arguments["symbol"]) is not str
                or futures_workspace.normalize_symbol(arguments["symbol"]) is None
            ):
                raise NativeToolError("invalid_arguments")
        elif name == "persona_propose_trait":
            if (
                not {"trait_text"} <= set(arguments) <= {"trait_text", "category", "reason"}
                or type(arguments["trait_text"]) is not str
                or not 2 <= len(arguments["trait_text"].strip()) <= 200
                or (
                    "category" in arguments
                    and arguments["category"] not in {"discipline", "focus", "format", "avoid"}
                )
                or (
                    "reason" in arguments
                    and (type(arguments["reason"]) is not str or len(arguments["reason"]) > 240)
                )
            ):
                raise NativeToolError("invalid_arguments")
        elif name == "knowledge_search":
            if (
                not {"query"} <= set(arguments) <= {"query", "limit"}
                or type(arguments["query"]) is not str
                or not arguments["query"].strip()
                or len(arguments["query"]) > 160
                or type(arguments.get("limit", 8)) is not int
                or not 1 <= arguments.get("limit", 8) <= 20
            ):
                raise NativeToolError("invalid_arguments")
        elif name == "knowledge_get":
            if (
                set(arguments) != {"id"}
                or type(arguments["id"]) is not str
                or re.fullmatch(r"(?:research|report):[A-Za-z0-9_-]{1,128}", arguments["id"]) is None
            ):
                raise NativeToolError("invalid_arguments")
        elif name == "source_find":
            from .discovery import validate

            try:
                validate(arguments)
            except (ValueError, TypeError, OverflowError):
                raise NativeToolError("invalid_arguments") from None
        elif name == "source_original":
            if (
                not {"query"} <= set(arguments) <= {"query", "start_date", "end_date", "contains"}
                or type(arguments["query"]) is not str
                or not arguments["query"].strip()
                or len(arguments["query"]) > 160
                or ("start_date" in arguments) != ("end_date" in arguments)
                or any(
                    key in arguments and not _iso_date(arguments[key]) for key in ("start_date", "end_date")
                )
                or ("start_date" in arguments and arguments["start_date"] > arguments["end_date"])
                or (
                    "contains" in arguments
                    and (
                        type(arguments["contains"]) is not str
                        or not arguments["contains"].strip()
                        or len(arguments["contains"]) > 100
                    )
                )
            ):
                raise NativeToolError("invalid_arguments")
        elif (
            not {"url"} <= set(arguments) <= {"url", "first_page", "page_count"}
            or not _valid_url(arguments["url"])
            or any(
                key in arguments and (type(arguments[key]) is not int or not 1 <= arguments[key] <= maximum)
                for key, maximum in (("first_page", 2000), ("page_count", 20))
            )
        ):
            raise NativeToolError("invalid_arguments")

    def _error(self, code, *, source_kind="unknown", call=None, details=(), kind="other"):
        code = code if code in _ERRORS else "unavailable"
        error = {
            "code": code,
            "kind": kind if kind in _ERROR_KINDS else "other",
            "message": _ERRORS[code],
        }
        safe_details = _clean_details(details)
        if safe_details:
            error["details"] = [dict(item) for item in safe_details]
        result = {
            "ok": False,
            "data": {},
            "refs": [],
            "source_kind": source_kind,
            "error": error,
        }
        if call is not None:
            result["call"] = call
        return result

    def _safe(self, value):
        value = _platform().safe_result(value)
        if len(_encoded(value)) > MAX_OUTPUT_BYTES:
            return {"truncated": True, "notice": "结果过大，完整资料请查看平台记录。"}
        return value

    def _final(self, value):
        value = _platform().safe_result(value)
        if len(_encoded(value)) > MAX_OUTPUT_BYTES:
            value["data"] = {"truncated": True, "notice": "结果过大，完整资料请查看平台记录。"}
            value["refs"] = []
        if len(_encoded(value)) > MAX_OUTPUT_BYTES:
            return self._error("unavailable")
        return value

    def _custom_call(self, name, call_id, started, *, ok, arguments=None):
        label, kind = _CUSTOM[name]
        target = {"view": "reports", "run_id": self.context["run_id"]}
        if name == "futures_intelligence" and isinstance(arguments, dict):
            sym = str(arguments.get("symbol") or "").strip().upper()
            if sym:
                target = {"view": "futures", "symbol": sym, "href": f"/futures?symbol={sym}"}
        return {
            "call_id": hashlib.sha256((self.context["run_id"] + "\0" + call_id).encode()).hexdigest(),
            "run_id": self.context["run_id"],
            "conv_id": self.context["conv_id"],
            "capability_id": name,
            "label": label,
            "source_kind": kind,
            "status": "completed" if ok else "failed",
            "target": target,
            "created_at": started,
            "completed_at": int(time.time()),
            "input_summary": label,
            "result_summary": (
                "已返回风险估算摘要，缺项仍保留；不是实际账户认证"
                if name == "portfolio_risk"
                else (
                    "已取得公告原文并按页登记，内容仍待语义核验"
                    if name == "source_original"
                    else "已读取资料，内容仍待语义核验"
                )
            )
            if ok
            else "未形成可用资料，保留研究缺口",
        }

    def _persist_custom_call(self, call, fingerprint):
        """Indexed shared history keeps metadata, not query text or documents.

        Evidence stays in the owned research. A persistence failure propagates
        rather than presenting an unrecorded operation as recorded success.
        """
        from ..database import get_db

        self._authorize()
        context = self.context
        receipt = {
            "ok": call["status"] == "completed",
            "call": call,
            "data": {"research_run_id": context["run_id"]},
            "source_kind": call["source_kind"],
        }
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if not db.execute(
                    "SELECT c.id FROM conversations c JOIN users u ON u.username=c.username "
                    "WHERE c.id=? AND c.username=? AND c.deleted_at IS NULL AND u.disabled_at IS NULL",
                    [context["conv_id"], context["username"]],
                ).fetchone():
                    raise NativeToolError("unavailable_context")
                db.execute(
                    "INSERT INTO capability_calls(call_id,run_id,username,conv_id,request_id,capability_id,"
                    "arguments_hash,status,input_summary,result_summary,target_json,response_json,created_at,"
                    "completed_at,arguments_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        call["call_id"],
                        context["run_id"],
                        context["username"],
                        context["conv_id"],
                        "native-" + call["call_id"],
                        call["capability_id"],
                        fingerprint,
                        call["status"],
                        call["input_summary"],
                        call["result_summary"],
                        json.dumps(call["target"]),
                        json.dumps(receipt, ensure_ascii=False),
                        call["created_at"],
                        call["completed_at"],
                        "{}",
                    ],
                )
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    async def _read_source(self, arguments, call_id):
        existing = self.evidence.calls.get(call_id)
        if call_id in self.evidence.completed or existing and existing.get("name") != "source_read":
            raise NativeToolError("conflict")
        value = await self.lease.manager.run_blocking(
            "auxiliary",
            _fetch_source,
            arguments["url"],
            parent=self.lease,
            allowed_hosts=self.allowed_source_hosts,
            **{key: arguments[key] for key in ("first_page", "page_count") if key in arguments},
        )
        if type(value) is not dict:
            raise NativeToolError("source_unavailable")
        data = self._safe(value)
        if data.get("published_at") and not data.get("published_at_basis"):
            data["published_at_basis"] = "tool_metadata"
        index = self._source_dates.get(value.get("url"))
        if index and not data.get("published_at"):
            data["published_at"] = index["published_at"]
            data["published_at_basis"] = "source_index_metadata"
            data["publication_index"] = {"url": index["index_url"], "body_sha256": index["index_body_sha256"]}
        data["semantic_status"], data["original_verified"] = "unreviewed", False
        if isinstance(value.get("text"), str) and isinstance(data.get("text"), str):
            data["text_truncated"] = bool(value.get("text_truncated")) or len(data["text"]) < len(
                value["text"]
            )
        if (
            value.get("status") != "ok"
            or not _valid_url(value.get("url"))
            or type(data.get("text")) is not str
            or not data["text"].strip()
        ):
            return {
                "ok": False,
                "data": data,
                "refs": [],
                "source_kind": "external_web",
                "error": {"code": "source_unavailable", "message": _ERRORS["source_unavailable"]},
            }
        call = {"type": "tool_use", "id": call_id, "name": "WebFetch", "input": {"url": value["url"]}}
        block = {"type": "tool_result", "tool_use_id": call_id, "content": data["text"]}
        self.evidence.record_call(call)
        refs = self.evidence.record_result(block, retrieved_at=value.get("retrieved_at"))
        refs = [
            {
                **ref,
                "title": data.get("title") or ref["url"],
                "published_at": data.get("published_at"),
                "published_at_basis": data.get("published_at_basis"),
                "index_source_url": index.get("index_url") if index else None,
                "index_body_sha256": index.get("index_body_sha256") if index else None,
                "semantic_status": "unreviewed",
                "original_obtained": False,
                "original_verified": False,
                "original_preserved": False,
                "content_scope": "server_extracted_excerpt",
                "body_sha256": value.get("body_sha256"),
                "page_range": value.get("page_range"),
                "total_pages": value.get("total_pages"),
            }
            for ref in refs
        ]
        refs = await self.lease.manager.run_blocking(
            "auxiliary",
            _ingest,
            self.context["username"],
            self.context["run_id"],
            call,
            block,
            refs,
            parent=self.lease,
        )
        return {"ok": True, "data": data, "refs": self._safe(refs), "source_kind": "external_web"}

    async def _read_original(self, arguments, call_id):
        existing = self.evidence.calls.get(call_id)
        if call_id in self.evidence.completed or existing and existing.get("name") != "source_original":
            raise NativeToolError("conflict")
        value = await self.lease.manager.run_blocking(
            "auxiliary", _original_fetch, arguments, parent=self.lease
        )
        if type(value) is not dict:
            raise NativeToolError("source_unavailable")
        if value.get("status") != "ok":
            return {
                "ok": False,
                "data": self._safe({"status": value.get("status"), "detail": value.get("detail")}),
                "refs": [],
                "source_kind": "external_web",
                "error": {"code": "source_unavailable", "message": _ERRORS["source_unavailable"]},
            }
        call = {
            "type": "tool_use",
            "id": call_id,
            "name": "source_original",
            "input": {
                key: arguments[key]
                for key in ("query", "start_date", "end_date", "contains")
                if key in arguments
            },
        }
        try:
            stored = await self.lease.manager.run_blocking(
                "auxiliary",
                _ingest_original,
                self.context["username"],
                self.context["run_id"],
                call,
                value,
                parent=self.lease,
            )
        except NativeToolError:
            raise
        except Exception:
            return self._error("source_unavailable", source_kind="external_web")
        document = stored.get("document") if isinstance(stored, dict) else None
        passages = stored.get("passages") if isinstance(stored, dict) else None
        if not isinstance(document, dict) or not isinstance(passages, list):
            raise NativeToolError("source_unavailable")
        page_meta = [
            {
                "page": row["page"],
                "passage_id": row["passage_id"],
                "chars": row["chars"],
                "sha256": row["sha256"],
            }
            for row in passages
            if isinstance(row, dict) and isinstance(row.get("page"), int)
        ]
        matches = []
        for match in value.get("matches") or []:
            if not isinstance(match, dict):
                continue
            snippet = match.get("snippet")
            matches.append(
                {
                    "page": match.get("page"),
                    "char_offset": match.get("char_offset"),
                    "snippet": redact_source_text(snippet)[:600] if isinstance(snippet, str) else "",
                }
            )
        data = {
            "document_id": document["document_id"],
            "title": document.get("title"),
            "publisher": document.get("publisher"),
            "published_at": document.get("published_at"),
            "retrieved_at": document.get("retrieved_at"),
            "page_count": document.get("page_count"),
            "file_sha256": document.get("file_sha256"),
            "pdf_url": document.get("url"),
            "query": value.get("query"),
            "contains": value.get("contains"),
            "matches": matches,
            "pages": page_meta,
            "notice": "原文已按物理页登记；内容仍待语义核验，标 verified_source 时须填写 source_locator{document_id,page}。",
        }
        refs = [
            {
                "url": document["url"],
                "title": document.get("title"),
                "snippet": "",
                "snippet_truncated": False,
                "run_id": self.context["run_id"],
                "tool_call_id": call_id,
                "source_kind": "external_web",
                "content_scope": "original_pdf",
                "original_obtained": True,
                "original_verified": False,
                "semantic_status": "unreviewed",
                "document_id": document["document_id"],
                "passage_ids": list(document.get("passage_ids") or []),
                "body_sha256": document.get("file_sha256"),
                "page_range": [1, document.get("page_count")],
                "total_pages": document.get("page_count"),
                "retrieved_at": document.get("retrieved_at"),
                "published_at": document.get("published_at"),
            }
        ]
        return {
            "ok": True,
            "data": self._safe(data),
            "refs": self._safe(refs),
            "source_kind": "external_web",
        }

    async def _discover(self, arguments, call_id):
        value = await self.lease.manager.run_blocking(
            "auxiliary", _find_sources, arguments, parent=self.lease
        )
        if type(value) is not dict or type(value.get("items")) is not list or len(value["items"]) > 20:
            raise NativeToolError("source_unavailable")
        data = self._safe(value)
        if value.get("status") != "ok":
            return {
                "ok": False,
                "data": data,
                "refs": [],
                "source_kind": "external_web",
                "error": {"code": "source_unavailable", "message": _ERRORS["source_unavailable"]},
            }
        items = data.get("items", [])
        if any(
            type(i) is not dict or not _valid_url(i.get("url")) or type(i.get("title")) is not str
            for i in items
        ):
            raise NativeToolError("source_unavailable")
        if not items:
            return {"ok": True, "data": data, "refs": [], "source_kind": "external_web"}
        call = {
            "type": "tool_use",
            "id": call_id,
            "name": "WebSearch",
            "input": {"query": arguments.get("query") or arguments.get("stock_code")},
        }
        block = {
            "type": "tool_result",
            "tool_use_id": call_id,
            "content": json.dumps({"results": items}, ensure_ascii=False),
        }
        self.evidence.record_call(call)
        refs = self.evidence.record_result(block, retrieved_at=value.get("as_of"))
        indexed = {i["url"]: i for i in items}
        enriched = []
        for ref in refs:
            item = indexed.get(ref["url"])
            if not item:
                continue
            self._source_dates[item["url"]] = item
            enriched.append(
                {
                    **ref,
                    "published_at": item.get("published_at"),
                    "published_at_basis": "source_index_metadata",
                    "index_source_url": item.get("index_url"),
                    "index_body_sha256": item.get("index_body_sha256"),
                    "retrieved_at": item.get("retrieved_at") or ref.get("retrieved_at"),
                    "original_obtained": False,
                    "original_verified": False,
                    "content_scope": "search_snippet",
                }
            )
        refs = await self.lease.manager.run_blocking(
            "auxiliary",
            _ingest,
            self.context["username"],
            self.context["run_id"],
            call,
            block,
            enriched,
            parent=self.lease,
        )
        return {"ok": True, "data": data, "refs": self._safe(refs), "source_kind": "external_web"}

    async def _register_market_evidence(self, output, arguments, call_id, name, renderer):
        """Record a successful market-data observation as source evidence for this run.

        The report writer still receives the same tool result: this only adds the
        document reference that lets the ledger match report figures to pages.
        Evidence bookkeeping must never fail the market-data tool itself.
        """
        observation = output.get("data")
        if type(observation) is not dict:
            return
        call = {"type": "tool_use", "id": call_id, "name": name, "input": arguments}
        try:
            stored = await self.lease.manager.run_blocking(
                "auxiliary",
                renderer,
                self.context["username"],
                self.context["run_id"],
                call,
                observation,
                parent=self.lease,
            )
        except Exception:  # noqa: BLE001 - ignored so data availability is unaffected
            return
        if type(stored) is not dict or type(stored.get("document")) is not dict:
            return
        document = stored["document"]
        output["data_snapshot"] = self._safe(
            {
                "document_id": document.get("document_id"),
                "data_period": document.get("data_period"),
                "title": document.get("title"),
                "page_count": document.get("page_count"),
                "original_obtained": document.get("original_obtained") is True,
            }
        )

    async def _perform(self, name, arguments, call_id):
        if name in PLATFORM_TOOLS:
            platform = _platform()
            request_id = hashlib.sha256((self.context["run_id"] + "\0" + call_id).encode()).hexdigest()
            result = await platform.dispatch_call(
                self.context,
                platform.CallRequest(
                    name=name,
                    arguments=arguments,
                    request_id=request_id,
                ),
            )
            if type(result) is not dict or type(result.get("ok")) is not bool:
                raise NativeToolError("unavailable")
            output = {
                "ok": result["ok"],
                "data": self._safe(result.get("data", {})),
                "refs": [],
                "source_kind": platform.REGISTRY[name].source_kind,
            }
            if type(result.get("call")) is dict:
                output["call"] = self._safe(result["call"])
            if result["ok"] is False:
                code = (
                    result.get("error", {}).get("code")
                    if type(result.get("error")) is dict
                    else "unavailable"
                )
                output["error"] = self._error(code)["error"]
            output["evidence_status"] = "platform_result_not_external_verification"
            renderer = _MARKET_EVIDENCE.get(name)
            if renderer is not None and output["ok"]:
                await self._register_market_evidence(output, arguments, call_id, name, renderer)
            return output
        if name == "portfolio_risk":
            from ..portfolio_valuation import agent_inputs, reprice

            try:
                version, frozen = agent_inputs(self.context["username"], self.context["run_id"])
            except Exception as error:
                from fastapi import HTTPException

                if isinstance(error, HTTPException) and error.status_code in {403, 409}:
                    raise NativeToolError(
                        "risk_permission_required" if error.status_code == 403 else "risk_snapshot_changed"
                    ) from None
                raise
            if self._risk_result is not None:
                cached = copy.deepcopy(self._risk_result)
                if cached.get("ok"):
                    cached["data"]["cached_for_this_run"] = True
                return cached
            # A failed/unknown read cannot restart with a new model call ID.
            self._risk_result = self._error("unavailable", source_kind="portfolio_snapshot")
            value = await reprice(
                self.context["username"], version, frozen_profile=frozen, authorize=self._authorize
            )
            self._risk_result = {
                "ok": True,
                "data": self._safe(
                    {
                        "risk": value["risk"],
                        "portfolio_version": value["portfolio_version"],
                        "profile_version": value["profile_version"],
                        "observed_at": value["observed_at"],
                        "snapshot_as_of": value["snapshot_as_of"],
                        "market_basis": value["market_basis"],
                        "notice": "仅为原持仓不变的对价估算，非券商账户或下单依据。",
                        "fixed_for_this_run": True,
                    }
                ),
                "refs": [],
                "source_kind": "portfolio_snapshot",
            }
            return copy.deepcopy(self._risk_result)
        if name == "futures_intelligence":
            from .. import futures_interpretation, futures_workspace

            code = futures_workspace.normalize_symbol(arguments["symbol"])
            if code is None:
                raise NativeToolError("invalid_arguments")
            enriched = futures_interpretation.enrich(await futures_workspace.card(code))
            mkt = (enriched.get("market") or {}).get("values") or {}
            compact = {
                "symbol": enriched.get("symbol") or code,
                "name": enriched.get("name"),
                "exchange": enriched.get("exchange"),
                "archetype_label": enriched.get("archetype_label"),
                "market": {
                    "status": (enriched.get("market") or {}).get("status"),
                    "data_as_of": (enriched.get("market") or {}).get("data_as_of"),
                    "close": mkt.get("close"),
                    "open": mkt.get("open"),
                    "high": mkt.get("high"),
                    "low": mkt.get("low"),
                    "volume": mkt.get("volume"),
                    "open_interest": mkt.get("open_interest"),
                    "quote_unit": mkt.get("quote_unit"),
                },
                "technicals": (enriched.get("technicals") or {}).get("values"),
                "basis": (enriched.get("basis") or {}).get("values"),
                "inventory": (enriched.get("inventory") or {}).get("values"),
                "spread": (enriched.get("spread") or {}).get("values"),
                "margins": enriched.get("margins"),
                "causal_view": enriched.get("causal_view"),
                "chain_canvas": enriched.get("chain_canvas"),
                "roll_guard": enriched.get("roll_guard"),
                "pre_interpretation": (enriched.get("research_brief") or {}).get("pre_interpretation"),
                "notice": "期货研究V2平台核验快照；连续序列非可交易月份合约，具体期现与跨期可配合 futures_basis / futures_spread / futures_inventory 交叉核对。",
            }
            return {
                "ok": True,
                "data": self._safe(compact),
                "refs": [],
                "source_kind": "platform_data",
            }
        if name == "persona_propose_trait":
            from ..preferences import propose_persona_trait

            proposal = await self.lease.manager.run_blocking(
                "auxiliary",
                propose_persona_trait,
                self.context["username"],
                self.context["run_id"],
                self.context["conv_id"],
                arguments["trait_text"],
                arguments.get("category", "focus"),
                arguments.get("reason", ""),
                parent=self.lease,
            )
            return {
                "ok": True,
                "data": self._safe(proposal),
                "refs": [],
                "source_kind": "historical_research",
            }
        if name == "source_read":
            return await self._read_source(arguments, call_id)
        if name == "source_original":
            return await self._read_original(arguments, call_id)
        if name == "source_find":
            return await self._discover(arguments, call_id)
        value = await self.lease.manager.run_blocking(
            "auxiliary",
            _knowledge_call,
            name,
            self.context["username"],
            arguments,
            self.context["run_id"],
            parent=self.lease,
        )
        return {"ok": True, "data": self._safe(value), "refs": [], "source_kind": "historical_research"}

    async def execute(self, name, args, call_id) -> dict:
        try:
            self._authorize()
            self._validate(name, args, call_id)
        except NativeToolError as error:
            return self._error(error.code, details=error.details, kind=error.kind)
        arguments = json.loads(_encoded(args))
        fingerprint = hashlib.sha256(_encoded({"name": name, "arguments": arguments})).hexdigest()
        async with self._lock:
            try:
                self._authorize()
            except NativeToolError as error:
                return self._error(error.code, details=error.details, kind=error.kind)
            cached = self._cache.get(call_id)
            if cached is not None:
                return copy.deepcopy(cached[1]) if cached[0] == fingerprint else self._error("conflict")
            if len(self._cache) >= 100:
                return self._error("rate_limited")
            # Reserve before awaiting work: cancellation cannot prove that an
            # owned blocking call stopped. The same ID must never refetch or
            # start another operation while completion is unknown.
            unknown = self._error("unavailable", source_kind=_CUSTOM.get(name, (None, "unknown"))[1])
            unknown.update({"execution_status": "unknown", "automatic_retry": False})
            self._cache[call_id] = (fingerprint, unknown)
            started = int(time.time())
            try:
                result = await self._perform(name, arguments, call_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                details, kind = (), _failure_kind(error)
                if isinstance(error, NativeToolError):
                    code, details, kind = error.code, error.details, error.kind
                else:
                    code = {
                        400: "invalid_arguments",
                        422: "invalid_arguments",
                        413: "invalid_arguments",
                        401: "unavailable_context",
                        403: "unavailable_context",
                        404: "not_found",
                        409: "conflict",
                        429: "rate_limited",
                    }.get(getattr(error, "status_code", None), "unavailable")
                result = self._error(
                    code,
                    source_kind=_CUSTOM.get(name, (None, "unknown"))[1],
                    details=details,
                    kind=kind,
                )
            if name in _CUSTOM:
                result["call"] = self._custom_call(
                    name, call_id, started, ok=result.get("ok") is True, arguments=arguments
                )
                self._persist_custom_call(result["call"], fingerprint)
            result = self._final(result)
            self._cache[call_id] = (fingerprint, copy.deepcopy(result))
            return result
