"""Bounded direct Google Gemini REST SSE adapter; never an SDK or provider fallback.

Protocol: https://ai.google.dev/api/generate-content#method:-models.streamgeneratecontent
Authentication uses the official `x-goog-api-key` header, supporting both Google AI Studio
(`AIza...`) and Google AI Ultra / Vertex Express (`AQ....`) keys without URL query leakage.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import time
from collections.abc import AsyncIterator

import httpx

from . import provider as P

ENDPOINTS = P._GEMINI_ENDPOINTS
_SCHEMA_STRIP_KEYS = frozenset(
    {
        "$schema",
        "$id",
        "$defs",
        "additionalProperties",
        "default",
        "examples",
        "title",
        "exclusiveMinimum",
        "exclusiveMaximum",
    }
)


def _clean_schema(node):
    """Strip JSON Schema keywords rejected by Gemini functionDeclarations."""
    if isinstance(node, list):
        return [_clean_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for key, val in node.items():
        if key in _SCHEMA_STRIP_KEYS:
            continue
        if key == "type" and isinstance(val, list):
            non_null = [t for t in val if t != "null"]
            out[key] = non_null[0] if non_null else "string"
            continue
        out[key] = _clean_schema(val)
    if out.get("type") == "object" and "properties" not in out:
        out["properties"] = {}
    return out


def _contents(messages: list[dict]) -> list[dict]:
    """Translate Prism Anthropic-shaped messages into Gemini `contents`."""
    tool_names: dict[str, str] = {}
    for msg in messages:
        content = msg.get("content")
        if msg.get("role") == "assistant" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tid = block.get("id")
                    name = block.get("name")
                    if isinstance(tid, str) and isinstance(name, str):
                        tool_names[tid] = name

    out: list[dict] = []
    for msg in messages:
        role = "model" if msg.get("role") == "assistant" else "user"
        content = msg.get("content")
        parts: list[dict] = []
        if isinstance(content, str):
            if content:
                parts.append({"text": content})
        elif isinstance(content, list):
            for block in content:
                P._need(type(block) is dict, "provider_input_error")
                kind = block.get("type")
                if kind == "_gemini_part" and role == "model":
                    raw_part = block.get("part")
                    P._need(type(raw_part) is dict, "provider_input_error")
                    parts.append(copy.deepcopy(raw_part))
                elif kind == "text" and isinstance(block.get("text"), str):
                    if block["text"]:
                        parts.append({"text": block["text"]})
                elif kind == "tool_use" and role == "model":
                    P._need(isinstance(block.get("input"), dict), "provider_input_error")
                    part: dict = {
                        "functionCall": {
                            "name": str(block.get("name") or ""),
                            "args": copy.deepcopy(block["input"]),
                        }
                    }
                    sig = block.get("_thought_signature")
                    if isinstance(sig, str) and sig:
                        part["thoughtSignature"] = sig
                    parts.append(part)
                elif kind == "tool_result" and role == "user":
                    tid = str(block.get("tool_use_id") or "")
                    output = block.get("content")
                    P._need(isinstance(output, str), "provider_input_error")
                    fn_name = tool_names.get(tid, "tool_output")
                    parts.append(
                        {
                            "functionResponse": {
                                "name": fn_name,
                                "response": {"output": output},
                            }
                        }
                    )
                elif kind in {"thinking", "redacted_thinking"}:
                    continue
                else:
                    raise P.ProviderError("provider_input_error")
        else:
            raise P.ProviderError("provider_input_error")

        if not parts:
            parts.append({"text": " "})
        if out and out[-1]["role"] == role:
            out[-1]["parts"].extend(parts)
        else:
            out.append({"role": role, "parts": parts})
    return out


def _tools(tools: list[dict]) -> list[dict]:
    decls = []
    has_search = False
    for tool in tools:
        if tool.get("type") == "web_search_20250305":
            has_search = True
            continue
        name = tool.get("name")
        P._need(isinstance(name, str) and 0 < len(name) <= 128, "provider_input_error")
        schema = _clean_schema(tool.get("input_schema") or {"type": "object", "properties": {}})
        decl: dict = {
            "name": name,
            "description": str(tool.get("description") or "")[:1024],
        }
        if isinstance(schema, dict) and schema.get("properties"):
            decl["parameters"] = schema
        decls.append(decl)

    result: list[dict] = []
    if decls:
        result.append({"functionDeclarations": decls})
    elif has_search:
        result.append({"google_search": {}})
    return result


async def _gemini_sse_chunks(
    response: httpx.Response,
    limits: P.ProviderLimits,
    deadline: float,
) -> AsyncIterator[dict]:
    """Read bounded SSE `data:` JSON objects from Gemini streamGenerateContent."""
    total_bytes = 0
    event_count = 0
    buf = b""
    data_lines: list[bytes] = []

    async for raw_chunk in response.aiter_bytes():
        if time.monotonic() > deadline:
            raise P.ProviderError("provider_timeout")
        total_bytes += len(raw_chunk)
        P._need(total_bytes <= limits.max_response_bytes, "provider_response_too_large")
        buf += raw_chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if not line:
                if data_lines:
                    payload = b"\n".join(data_lines).strip()
                    data_lines = []
                    if not payload or payload == b"[DONE]":
                        continue
                    P._need(len(payload) <= limits.max_event_bytes, "provider_event_too_large")
                    event_count += 1
                    P._need(event_count <= limits.max_events, "provider_response_too_large")
                    try:
                        obj = json.loads(payload.decode("utf-8"))
                    except (ValueError, UnicodeError):
                        raise P.ProviderError("provider_protocol_error") from None
                    if isinstance(obj, dict):
                        yield obj
                continue
            if line.startswith(b"data:"):
                data_lines.append(line[5:].lstrip(b" "))
    if data_lines:
        payload = b"\n".join(data_lines).strip()
        if payload and payload != b"[DONE]":
            try:
                obj = json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeError):
                raise P.ProviderError("provider_protocol_error") from None
            if isinstance(obj, dict):
                yield obj


class GeminiGenerateContentProvider:
    """Direct bounded Gemini SSE streaming provider."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        limits: P.ProviderLimits | None = None,
    ):
        self.transport = transport
        self.limits = limits or P.ProviderLimits()

    async def stream(
        self,
        messages: list[dict],
        *,
        system: str,
        tools: list[dict],
        model_cfg: dict,
        max_tokens: int,
        timeout_seconds: float,
    ) -> AsyncIterator[dict]:
        P._need(type(model_cfg) is dict, "provider_configuration_error")
        base, key, model = (model_cfg.get(k) for k in ("base_url", "api_key", "model"))
        P._need(type(base) is str and base.rstrip("/") in ENDPOINTS, "provider_configuration_error")
        logical_endpoint = base.rstrip("/")
        P._need(
            type(key) is str and 0 < len(key) <= 8192 and all(33 <= ord(c) <= 126 for c in key),
            "provider_configuration_error",
        )
        P._need(
            type(model) is str and 0 < len(model) <= 128 and all(c.isalnum() or c in "._-" for c in model),
            "provider_configuration_error",
        )
        P._need(
            P._valid_messages(messages)
            and type(system) is str
            and type(tools) is list
            and len(tools) <= 256
            and all(type(t) is dict for t in tools)
            and type(max_tokens) is int
            and 1 <= max_tokens <= 65536
            and type(timeout_seconds) in {int, float}
            and math.isfinite(timeout_seconds)
            and 0 < timeout_seconds <= 600,
            "provider_input_error",
        )

        payload: dict = {
            "contents": _contents(messages),
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        if system.strip():
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        gem_tools = _tools(tools)
        if gem_tools:
            payload["tools"] = gem_tools

        body = P._json_bytes(payload, "provider_input_error")
        P._need(len(body) <= self.limits.max_request_bytes, "provider_input_error")
        url = f"{logical_endpoint}/models/{model}:streamGenerateContent?alt=sse"

        client = httpx.AsyncClient(
            transport=P._transport(model_cfg, logical_endpoint, self.transport),
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_seconds),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
        )
        response = None
        deadline = time.monotonic() + timeout_seconds
        try:
            request = client.build_request(
                "POST",
                url,
                content=body,
                headers={
                    "x-goog-api-key": key,
                    "content-type": "application/json",
                    "accept": "text/event-stream",
                    "accept-encoding": "identity",
                },
            )
            response = await asyncio.wait_for(client.send(request, stream=True), timeout_seconds)
            if response.status_code != 200:
                code = {
                    401: "provider_auth_error",
                    403: "provider_auth_error",
                    429: "provider_rate_limited",
                }.get(response.status_code, "provider_http_error")
                raise P.ProviderError(code, http_status=response.status_code)

            text_parts: list[str] = []
            public_blocks: list[dict] = []
            raw_blocks: list[dict] = []
            pending_calls: list[dict] = []
            finish_reason = ""
            input_tokens = 0
            output_tokens = 0
            usage_seen = False
            call_seq = 0

            async for chunk in _gemini_sse_chunks(response, self.limits, deadline):
                um = chunk.get("usageMetadata")
                if isinstance(um, dict):
                    pt = um.get("promptTokenCount")
                    ct = um.get("candidatesTokenCount")
                    if isinstance(pt, int) and pt >= 0:
                        input_tokens = pt
                        usage_seen = True
                    if isinstance(ct, int) and ct >= 0:
                        output_tokens = ct
                        usage_seen = True

                candidates = chunk.get("candidates") or []
                if not candidates or not isinstance(candidates[0], dict):
                    continue
                cand = candidates[0]
                if isinstance(cand.get("finishReason"), str) and cand["finishReason"]:
                    finish_reason = cand["finishReason"]
                content_obj = cand.get("content") or {}
                for part in content_obj.get("parts") or []:
                    if not isinstance(part, dict):
                        continue
                    if part.get("thought") is True:
                        raw_blocks.append({"type": "_gemini_part", "part": copy.deepcopy(part)})
                        continue
                    if isinstance(part.get("text"), str) and part["text"]:
                        t = part["text"]
                        text_parts.append(t)
                        raw_blocks.append({"type": "_gemini_part", "part": copy.deepcopy(part)})
                        yield {"type": "text_delta", "text": t}
                    fc = part.get("functionCall")
                    if isinstance(fc, dict) and isinstance(fc.get("name"), str) and fc["name"]:
                        call_seq += 1
                        cid = f"gemini_call_{call_seq}"
                        args = fc.get("args") if isinstance(fc.get("args"), dict) else {}
                        tu_block: dict = {
                            "type": "tool_use",
                            "id": cid,
                            "name": fc["name"],
                            "input": copy.deepcopy(args),
                        }
                        if isinstance(part.get("thoughtSignature"), str):
                            tu_block["_thought_signature"] = part["thoughtSignature"]
                        public_blocks.append(
                            {"type": "tool_use", "id": cid, "name": fc["name"], "input": copy.deepcopy(args)}
                        )
                        raw_blocks.append(tu_block)
                        pending_calls.append(
                            {
                                "type": "tool_call",
                                "id": cid,
                                "name": fc["name"],
                                "arguments": copy.deepcopy(args),
                            }
                        )

            await P._close_transport(response, client)
            response = None

            if usage_seen:
                yield {"type": "usage", "input_tokens": input_tokens, "output_tokens": output_tokens}
            if text_parts:
                public_blocks.insert(0, {"type": "text", "text": "".join(text_parts)})
            for call in pending_calls:
                yield call
            stop_reason = (
                "tool_use" if pending_calls else "max_tokens" if finish_reason == "MAX_TOKENS" else "end_turn"
            )
            yield {
                "type": "done",
                "stop_reason": stop_reason,
                "content": P.AssistantContent(public_blocks, raw_blocks or public_blocks),
                "usage_complete": usage_seen and input_tokens > 0 and output_tokens > 0,
            }
        except (TimeoutError, httpx.TimeoutException):
            raise P.ProviderError("provider_timeout") from None
        except httpx.HTTPError:
            raise P.ProviderError("provider_connection_error") from None
        finally:
            await P._close_transport(response, client)
