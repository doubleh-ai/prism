"""Bounded public-answer projection for legacy history and saved research.

Provider/private blocks are never flattened into prose. This cannot recover the
provenance of legacy prose that was already flattened before it was stored.
"""

from __future__ import annotations

import json
import re

from ..source_urls import redact_source_text

MAX_PUBLIC_CHARACTERS = 100000
MAX_INPUT_CHARACTERS = 400000
MAX_WORK_BYTES = 1200000
MAX_NODES = 2048
MAX_DEPTH = 10
MAX_JSON_LAYERS = 2
_ASSIGNMENT = re.compile(
    r"(?i)\b([\w-]*(?:api[_-]?key|password|passwd|secret|access[_-]?token|refresh[_-]?token|"
    r"id[_-]?token|authorization|cookie))"
    r"[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{20,})\b")
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)
_THINK_TAG = re.compile(r"(?is)<(think|thinking|reasoning|analysis)\b[^>]*>.*?(?:</\1\s*>|\Z)")
_BUSINESS_FIELDS = ("question", "summary", "thesis", "narrative", "final_decision", "answer")


def _decode(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("ambiguous public answer")
            result[key] = item
        return result

    def invalid(_value):
        raise ValueError("non-json public answer")

    return json.loads(value, object_pairs_hook=pairs, parse_constant=invalid)


def _json_shaped(value):
    value = value.lstrip()
    if re.match(r"^\[[^\[\]\r\n{}\"]+\]\(", value):
        return False  # An ordinary leading Markdown link is public prose.
    return value.startswith(("{", '"', "["))


def _clean(value):
    value = _THINK_TAG.sub("", value)
    value = _PEM.sub("[已隐藏敏感信息]", value)
    value = _ASSIGNMENT.sub(lambda match: match.group(1) + "=[已隐藏敏感信息]", value)
    value = _BEARER.sub("Bearer [已隐藏敏感信息]", value)
    value = _TOKEN.sub("[已隐藏敏感信息]", value)
    return redact_source_text(value)


def public_answer_text(value) -> str:
    """Extract known public text, including at most two encoded JSON layers.

    Unknown typed blocks and unknown fields have no public projection. Normal
    Markdown remains prose; JSON-shaped malformed, over-encoded, ambiguous or
    oversized strings fail closed. Limits apply across the entire projection.
    """
    nodes = work = emitted = 0

    def visit(item, depth=0, json_layers=0):
        nonlocal nodes, work, emitted
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH or emitted >= MAX_PUBLIC_CHARACTERS:
            return ""
        if isinstance(item, str):
            if len(item) > MAX_INPUT_CHARACTERS:
                return ""
            try:
                work += len(item.encode("utf-8"))
            except UnicodeError:
                return ""
            if work > MAX_WORK_BYTES:
                return ""
            if _json_shaped(item):
                if json_layers >= MAX_JSON_LAYERS:
                    return ""
                try:
                    decoded = _decode(item)
                except (TypeError, ValueError, RecursionError):
                    return ""
                return visit(decoded, depth + 1, json_layers + 1)
            text = _clean(item).strip()[: MAX_PUBLIC_CHARACTERS - emitted]
            emitted += len(text)
            return text
        if type(item) is list:
            parts = []
            for child in item:
                if nodes >= MAX_NODES or emitted >= MAX_PUBLIC_CHARACTERS or work > MAX_WORK_BYTES:
                    break
                text = visit(child, depth + 1, json_layers)
                if text:
                    parts.append(text)
            return "\n".join(parts)
        if type(item) is not dict:
            return ""
        if len(item) > 100:
            return ""
        # A private/unknown typed wrapper must not become public by containing a
        # seemingly innocuous `text`, `content`, `summary` or nested `message`.
        kinds = [
            child for key, child in item.items() if isinstance(key, str) and key.casefold() in {"type", "t"}
        ]
        if kinds:
            if any(not isinstance(kind, str) for kind in kinds):
                return ""
            normalized = {kind.casefold() for kind in kinds}
            if len(normalized) != 1:
                return ""
            kind = normalized.pop()
            if kind in {"text", "output_text", "text_delta"}:
                return visit(item.get("text", item.get("c", "")), depth + 1, json_layers)
            if kind != "message":
                return ""
        if any(
            child not in (None, "user", "assistant")
            for key, child in item.items()
            if isinstance(key, str) and key.casefold() == "role"
        ):
            return ""
        # Public chat text is authoritative; do not also flatten its duplicated
        # chunks, tool records or hidden fields into the same answer.
        for field in ("text", "content", "message", "chunks", "blocks"):
            if field in item:
                result = visit(item[field], depth + 1, json_layers)
                if result:
                    return result
        parts = []
        for field in _BUSINESS_FIELDS:
            if field in item and nodes < MAX_NODES and emitted < MAX_PUBLIC_CHARACTERS:
                text = visit(item[field], depth + 1, json_layers)
                if text:
                    parts.append(text)
        return "\n".join(parts)

    return visit(value)[:MAX_PUBLIC_CHARACTERS]
