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
from urllib.parse import urlsplit

from ..research_context import EvidenceTracker
from ..source_urls import public_source_url
from ..work_lifecycle import Lease

PLATFORM_TOOLS = (
    "futures_snapshot",
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
    "vibe_status",
    "watchlist_get",
    "research_read",
    "research_record_claims",
    "research_record_relations",
    "research_scope",
)
MAX_ARGUMENT_BYTES = 32768
MAX_OUTPUT_BYTES = 120000
_CUSTOM = {
    "portfolio_risk": ("核对我的持仓风险", "portfolio_snapshot"),
    "knowledge_search": ("检索我的历史研究", "historical_research"),
    "knowledge_get": ("读取我的历史研究", "historical_research"),
    "source_read": ("读取公开资料节选", "external_web"),
    "source_find": ("查找公开资料", "external_web"),
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
    "unavailable": "本次工具未能完成，请保留研究缺口",
}


class NativeToolError(RuntimeError):
    def __init__(self, code="unavailable"):
        self.code = code if code in _ERRORS else "unavailable"
        super().__init__(self.code)


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
            "name": "source_find",
            "description": "用短的公开关键词或沪深六位证券代码查找公告与新闻线索，不需猜测URL或提供搜索密钥。覆盖上交所和深交所公告、新浪滚动索引及财联社最新电报首页；新浪和财联社仅在有界列表内匹配，非全文历史搜索。每次最多两个来源，保留日期、分页、来源和缺口，随后用source_read读原文。不要发送用户持仓、个人信息或凭证。",
            "input_schema": copy.deepcopy(SCHEMA),
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
            "description": "读取获准公开来源的实际节选。PDF 默认前10页；可先查目录，再指定物理页 first_page 和 page_count 读取相关章节，勿要求用户提供页码。节选不代表全文已读或事实已核验。",
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
        if (
            type(call_id) is not str
            or re.fullmatch(r"[A-Za-z0-9_-]{1,200}", call_id) is None
            or type(arguments) is not dict
            or not all(type(k) is str for k in arguments)
            or len(arguments) > 30
            or len(_encoded(arguments)) > MAX_ARGUMENT_BYTES
        ):
            raise NativeToolError("invalid_arguments")
        if name in PLATFORM_TOOLS:
            spec = _platform().REGISTRY.get(name)
            if spec is None:
                raise NativeToolError("unknown_tool")
            try:
                spec.schema.model_validate(arguments)
            except Exception:
                raise NativeToolError("invalid_arguments") from None
        elif name == "portfolio_risk":
            if arguments:
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
        elif (
            not {"url"} <= set(arguments) <= {"url", "first_page", "page_count"}
            or not _valid_url(arguments["url"])
            or any(
                key in arguments and (type(arguments[key]) is not int or not 1 <= arguments[key] <= maximum)
                for key, maximum in (("first_page", 2000), ("page_count", 20))
            )
        ):
            raise NativeToolError("invalid_arguments")

    def _error(self, code, *, source_kind="unknown", call=None):
        code = code if code in _ERRORS else "unavailable"
        result = {
            "ok": False,
            "data": {},
            "refs": [],
            "source_kind": source_kind,
            "error": {"code": code, "message": _ERRORS[code]},
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

    def _custom_call(self, name, call_id, started, *, ok):
        label, kind = _CUSTOM[name]
        return {
            "call_id": hashlib.sha256((self.context["run_id"] + "\0" + call_id).encode()).hexdigest(),
            "run_id": self.context["run_id"],
            "conv_id": self.context["conv_id"],
            "capability_id": name,
            "label": label,
            "source_kind": kind,
            "status": "completed" if ok else "failed",
            "target": {"view": "reports", "run_id": self.context["run_id"]},
            "created_at": started,
            "completed_at": int(time.time()),
            "input_summary": label,
            "result_summary": (
                "已返回风险估算摘要，缺项仍保留；不是实际账户认证"
                if name == "portfolio_risk"
                else "已读取资料，内容仍待语义核验"
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
        if name == "source_read":
            return await self._read_source(arguments, call_id)
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
            return self._error(error.code)
        arguments = json.loads(_encoded(args))
        fingerprint = hashlib.sha256(_encoded({"name": name, "arguments": arguments})).hexdigest()
        async with self._lock:
            try:
                self._authorize()
            except NativeToolError as error:
                return self._error(error.code)
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
                if isinstance(error, NativeToolError):
                    code = error.code
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
                result = self._error(code, source_kind=_CUSTOM.get(name, (None, "unknown"))[1])
            if name in _CUSTOM:
                result["call"] = self._custom_call(name, call_id, started, ok=result.get("ok") is True)
                self._persist_custom_call(result["call"], fingerprint)
            result = self._final(result)
            self._cache[call_id] = (fingerprint, copy.deepcopy(result))
            return result
