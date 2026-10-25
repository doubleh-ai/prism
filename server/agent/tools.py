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
    "knowledge_search": ("检索我的历史研究", "historical_research"),
    "knowledge_get": ("读取我的历史研究", "historical_research"),
    "source_read": ("读取公开资料节选", "external_web"),
}
_ERRORS = {
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
    return [
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
            "description": "读取获准公开来源的实际页面节选；未取得原文时保留缺口，节选不代表事实已核验。",
            "input_schema": {
                "type": "object",
                "properties": {"url": {"type": "string", "maxLength": 2048, "format": "uri"}},
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


def _fetch_source(url, *, allowed_hosts):
    from .sources import fetch_public_source

    return fetch_public_source(url, allowed_hosts=allowed_hosts)


def _knowledge_call(name, username, arguments, run_id):
    from . import knowledge

    if name == "knowledge_search":
        return knowledge.search(
            username, arguments["query"], limit=arguments.get("limit", 8), exclude_run_id=run_id
        )
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
        elif set(arguments) != {"url"} or not _valid_url(arguments["url"]):
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
            "result_summary": "已读取资料，内容仍待语义核验" if ok else "未形成可用资料，保留研究缺口",
        }

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
        )
        if type(value) is not dict:
            raise NativeToolError("source_unavailable")
        data = self._safe(value)
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
                "semantic_status": "unreviewed",
                "original_obtained": False,
                "original_verified": False,
                "original_preserved": False,
                "content_scope": "server_extracted_excerpt",
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
        if name == "source_read":
            return await self._read_source(arguments, call_id)
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
            result = self._final(result)
            self._cache[call_id] = (fingerprint, copy.deepcopy(result))
            return result
