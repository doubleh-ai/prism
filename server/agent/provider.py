"""Direct, bounded Anthropic Messages streaming for Prism's configured providers.

No SDK, environment/config import, credential discovery or retries. Only the
caller-supplied key is used, in an authentication header to a fixed HTTPS URL.
Protocol reference: https://platform.claude.com/docs/en/build-with-claude/streaming
DeepSeek: https://api-docs.deepseek.com/guides/anthropic_api/
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass

import httpx

_ENDPOINTS = {
    "https://api.deepseek.com/anthropic": "https://api.deepseek.com/anthropic/v1/messages",
    "https://api.deepseek.com/anthropic/v1": "https://api.deepseek.com/anthropic/v1/messages",
    "https://api.minimax.io/anthropic/v1": "https://api.minimax.io/anthropic/v1/messages",
}
_ERROR_CODES = frozenset(
    {
        "provider_configuration_error",
        "provider_input_error",
        "provider_http_error",
        "provider_auth_error",
        "provider_rate_limited",
        "provider_timeout",
        "provider_connection_error",
        "provider_protocol_error",
        "provider_response_too_large",
        "provider_event_too_large",
        "provider_tool_json_too_large",
        "provider_stream_truncated",
        "provider_upstream_error",
    }
)
_STOP_REASONS = frozenset(
    {
        "end_turn",
        "max_tokens",
        "stop_sequence",
        "tool_use",
        "pause_turn",
        "refusal",
        "model_context_window_exceeded",
    }
)
_SERVER_RESULTS = frozenset(
    {
        "web_search_tool_result",
        "web_fetch_tool_result",
        "code_execution_tool_result",
        "bash_code_execution_tool_result",
        "text_editor_code_execution_tool_result",
        "tool_search_tool_result",
    }
)


class ProviderError(RuntimeError):
    """A closed error code, never an upstream body, URL, key or exception string."""

    def __init__(self, code: str):
        self.code = code if code in _ERROR_CODES else "provider_protocol_error"
        super().__init__(self.code)


@dataclass(frozen=True)
class ProviderLimits:
    max_event_bytes: int = 256 * 1024
    max_response_bytes: int = 8 * 1024 * 1024
    max_tool_json_bytes: int = 256 * 1024
    max_request_bytes: int = 2 * 1024 * 1024
    max_blocks: int = 256
    max_events: int = 20000


class AssistantContent(list):
    """JSON-safe public blocks with an explicitly requested, RAM-only continuation.

    Never persist continuation() or expose it in UI/logs. Normal list iteration,
    repr and JSON encoding include only public operational/text blocks.
    """

    __slots__ = ("_private_blocks",)

    def __init__(self, public_blocks: list[dict], private_blocks: list[dict]):
        super().__init__(copy.deepcopy(public_blocks))
        self._private_blocks = copy.deepcopy(private_blocks)

    def continuation(self) -> list[dict]:
        return copy.deepcopy(self._private_blocks)


def _need(value: bool, code: str = "provider_protocol_error") -> None:
    if not value:
        raise ProviderError(code)


def _json_bytes(value, code: str) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise ProviderError(code) from None


def _loads(raw: bytes | str):
    def pairs(items):
        result = {}
        for key, value in items:
            _need(key not in result)
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ProviderError("provider_protocol_error")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ProviderError("provider_protocol_error") from None


async def _frames(response: httpx.Response, limits: ProviderLimits, deadline: float) -> AsyncIterator[dict]:
    """Parse SSE at the byte boundary, including split UTF-8/CRLF and multiline data."""
    pending = bytearray()
    data = []
    event_name = None
    total = event_bytes = count = 0
    first_line = True
    iterator = response.aiter_bytes().__aiter__()
    while True:
        remaining = deadline - time.monotonic()
        _need(remaining > 0, "provider_timeout")
        try:
            chunk = await asyncio.wait_for(iterator.__anext__(), remaining)
        except StopAsyncIteration:
            # SSE dispatch requires a blank line; EOF is not a terminal event.
            if pending or data:
                raise ProviderError("provider_stream_truncated") from None
            return
        total += len(chunk)
        _need(total <= limits.max_response_bytes, "provider_response_too_large")
        pending.extend(chunk)
        while pending:
            _need(time.monotonic() < deadline, "provider_timeout")
            positions = [p for p in (pending.find(b"\n"), pending.find(b"\r")) if p >= 0]
            if not positions:
                _need(event_bytes + len(pending) <= limits.max_event_bytes, "provider_event_too_large")
                break
            end = min(positions)
            if pending[end] == 13 and end + 1 == len(pending):
                _need(event_bytes + len(pending) <= limits.max_event_bytes, "provider_event_too_large")
                break
            width = 2 if pending[end : end + 2] == b"\r\n" else 1
            line = bytes(pending[:end])
            del pending[: end + width]
            event_bytes += len(line) + width
            _need(event_bytes <= limits.max_event_bytes, "provider_event_too_large")
            if first_line:
                line = line.removeprefix(b"\xef\xbb\xbf")
                first_line = False
            if not line:
                count += 1
                _need(count <= limits.max_events, "provider_response_too_large")
                if data:
                    try:
                        payload = b"\n".join(data).decode("utf-8")
                    except UnicodeError:
                        raise ProviderError("provider_protocol_error") from None
                    value = _loads(payload)
                    _need(type(value) is dict and type(value.get("type")) is str)
                    _need(event_name is None or event_name == value["type"])
                    yield value
                data, event_name, event_bytes = [], None, 0
            elif not line.startswith(b":"):
                field, separator, value = line.partition(b":")
                if separator and value.startswith(b" "):
                    value = value[1:]
                if field == b"data":
                    data.append(value)
                elif field == b"event":
                    try:
                        event_name = value.decode("ascii")
                    except UnicodeError:
                        raise ProviderError("provider_protocol_error") from None


def _public_block(block: dict) -> dict:
    # Vendor/private extension fields (including reasoning) stay solely in the
    # original RAM continuation, even when attached to a public block type.
    if block["type"] == "text":
        keys = {"type", "text", "citations"}
    elif block["type"] in {"tool_use", "server_tool_use"}:
        keys = {"type", "id", "name", "input"}
    else:
        keys = {"type", "tool_use_id", "content"}
    return {key: copy.deepcopy(value) for key, value in block.items() if key in keys}


class _Message:
    def __init__(self, limits: ProviderLimits):
        self.limits = limits
        self.started = self.stopped = False
        self.blocks: list[dict] = []
        self.open_index: int | None = None
        self.pieces: dict[str, list[str]] = {}
        self.tool_json_bytes = 0
        self.tool_ids: set[str] = set()
        self.pending_calls: list[dict] = []
        self.stop_reason = None
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.usage_seen: set[str] = set()
        self.final_output_usage = False

    def _usage(self, usage) -> list[dict]:
        _need(type(usage) is dict)
        changed = False
        for key in self.usage:
            if key in usage:
                value = usage[key]
                _need(type(value) is int and self.usage[key] <= value <= 10**12)
                self.usage[key] = value
                self.usage_seen.add(key)
                changed = True
        return [{"type": "usage", **{key: self.usage[key] for key in self.usage_seen}}] if changed else []

    def _index(self, event) -> int:
        value = event.get("index")
        _need(type(value) is int and value == self.open_index)
        return value

    def feed(self, event: dict) -> list[dict]:
        kind = event["type"]
        _need(not self.stopped)
        if kind == "ping":
            return []
        if kind == "error":
            raise ProviderError("provider_upstream_error")
        if kind == "message_start":
            _need(not self.started)
            message = event.get("message")
            _need(
                type(message) is dict and message.get("role") == "assistant" and message.get("content") == []
            )
            self.started = True
            return self._usage(message.get("usage", {}))
        _need(self.started)
        if kind == "content_block_start":
            _need(
                self.open_index is None
                and event.get("index") == len(self.blocks)
                and type(event.get("index")) is int
                and len(self.blocks) < self.limits.max_blocks
            )
            block = event.get("content_block")
            _need(type(block) is dict and type(block.get("type")) is str)
            block = copy.deepcopy(block)
            block_type = block["type"]
            self.blocks.append(block)
            self.open_index = len(self.blocks) - 1
            self.pieces = {}
            if block_type in {"tool_use", "server_tool_use"}:
                _need(
                    type(block.get("id")) is str
                    and 0 < len(block["id"]) <= 512
                    and block["id"] not in self.tool_ids
                    and type(block.get("name")) is str
                    and 0 < len(block["name"]) <= 128
                    and type(block.get("input")) is dict
                )
                self.tool_ids.add(block["id"])
                self.tool_json_bytes += len(_json_bytes(block["input"], "provider_protocol_error"))
                _need(self.tool_json_bytes <= self.limits.max_tool_json_bytes, "provider_tool_json_too_large")
            if block_type == "text":
                _need(type(block.get("text")) is str)
                return [{"type": "text_delta", "text": block["text"]}] if block["text"] else []
            return []
        if kind == "content_block_delta":
            block = self.blocks[self._index(event)]
            delta = event.get("delta")
            _need(type(delta) is dict and type(delta.get("type")) is str)
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                _need(block["type"] == "text" and type(delta.get("text")) is str)
                self.pieces.setdefault("text", []).append(delta["text"])
                return [{"type": "text_delta", "text": delta["text"]}]
            if delta_type == "input_json_delta":
                _need(
                    block["type"] in {"tool_use", "server_tool_use"}
                    and type(delta.get("partial_json")) is str
                    and block["input"] == {}
                )
                self.tool_json_bytes += len(delta["partial_json"].encode("utf-8"))
                _need(self.tool_json_bytes <= self.limits.max_tool_json_bytes, "provider_tool_json_too_large")
                self.pieces.setdefault("input", []).append(delta["partial_json"])
            elif delta_type in {"thinking_delta", "signature_delta"}:
                field = "thinking" if delta_type == "thinking_delta" else "signature"
                _need(
                    block["type"] == "thinking"
                    and type(delta.get(field)) is str
                    and type(block.get(field, "")) is str
                )
                self.pieces.setdefault(field, []).append(delta[field])
            elif delta_type == "citations_delta":
                _need(block["type"] == "text" and type(delta.get("citation")) is dict)
                citations = block.setdefault("citations", [])
                _need(type(citations) is list)
                citations.append(copy.deepcopy(delta["citation"]))
            else:
                # Unknown complete opaque blocks can be retained. Unknown
                # mutations cannot be silently assembled into a valid follow-up.
                raise ProviderError("provider_protocol_error")
            return []
        if kind == "content_block_stop":
            block = self.blocks[self._index(event)]
            for field, pieces in self.pieces.items():
                if field == "input":
                    joined = "".join(pieces)
                    value = _loads(joined) if joined else block["input"]
                    _need(type(value) is dict)
                    block["input"] = value
                else:
                    block[field] = block.get(field, "") + "".join(pieces)
            self.open_index = None
            self.pieces = {}
            if block["type"] == "tool_use":
                self.pending_calls.append(
                    {
                        "type": "tool_call",
                        "id": block["id"],
                        "name": block["name"],
                        "arguments": copy.deepcopy(block["input"]),
                    }
                )
            elif block["type"] == "server_tool_use":
                return [{"type": "server_tool_use", "block": _public_block(block)}]
            elif block["type"] in _SERVER_RESULTS:
                return [{"type": "server_tool_result", "block": _public_block(block)}]
            return []
        if kind == "message_delta":
            _need(self.open_index is None and type(event.get("delta")) is dict)
            reason = event["delta"].get("stop_reason")
            if reason is not None:
                _need(type(reason) is str and reason in _STOP_REASONS)
                self.stop_reason = reason
            usage = event.get("usage", {})
            result = self._usage(usage)
            if "output_tokens" in usage:
                self.final_output_usage = True
            return result
        if kind == "message_stop":
            _need(self.open_index is None and self.stop_reason is not None)
            _need(not self.pending_calls or self.stop_reason == "tool_use")
            self.stopped = True
            public = [
                _public_block(b)
                for b in self.blocks
                if b["type"] in {"text", "tool_use", "server_tool_use"} | _SERVER_RESULTS
            ]
            return [
                *self.pending_calls,
                {
                    "type": "done",
                    "stop_reason": self.stop_reason,
                    "content": AssistantContent(public, self.blocks),
                    "usage_complete": self.usage_seen == {"input_tokens", "output_tokens"}
                    and self.final_output_usage,
                },
            ]
        # Forward-compatible extra top-level SSE events carry no public content.
        return []


class AnthropicMessagesProvider:
    """A transport per request; inject only MockTransport in offline tests."""

    def __init__(
        self, *, transport: httpx.AsyncBaseTransport | None = None, limits: ProviderLimits | None = None
    ):
        self.transport = transport
        self.limits = limits or ProviderLimits()
        _need(
            isinstance(self.limits, ProviderLimits)
            and (transport is None or isinstance(transport, httpx.AsyncBaseTransport)),
            "provider_configuration_error",
        )
        ceilings = ProviderLimits()
        for name in ProviderLimits.__dataclass_fields__:
            value = getattr(self.limits, name)
            _need(type(value) is int and 0 < value <= getattr(ceilings, name), "provider_configuration_error")

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
        _need(type(model_cfg) is dict, "provider_configuration_error")
        base, key, model = (model_cfg.get(k) for k in ("base_url", "api_key", "model"))
        _need(type(base) is str and base.rstrip("/") in _ENDPOINTS, "provider_configuration_error")
        endpoint = _ENDPOINTS[base.rstrip("/")]
        _need(
            type(key) is str and 0 < len(key) <= 8192 and all(33 <= ord(c) <= 126 for c in key),
            "provider_configuration_error",
        )
        _need(
            type(model) is str and 0 < len(model) <= 128 and all(32 <= ord(c) < 127 for c in model),
            "provider_configuration_error",
        )
        _need(
            type(messages) is list
            and 0 < len(messages) <= 2048
            and all(
                type(m) is dict and type(m.get("role")) is str and m["role"] in {"user", "assistant"}
                for m in messages
            )
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
        payload = {
            "model": model,
            "stream": True,
            "max_tokens": max_tokens,
            "messages": messages,
            "system": system,
            "tools": tools,
        }
        body = _json_bytes(payload, "provider_input_error")
        _need(len(body) <= self.limits.max_request_bytes, "provider_input_error")
        client = httpx.AsyncClient(
            transport=self.transport,
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
                endpoint,
                content=body,
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
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
                raise ProviderError(code)
            _need(
                response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                == "text/event-stream"
            )
            _need(response.headers.get("content-encoding", "identity").lower() in {"", "identity"})
            message = _Message(self.limits)
            async for frame in _frames(response, self.limits, deadline):
                try:
                    events = message.feed(frame)
                except (KeyError, IndexError, TypeError, RecursionError, UnicodeError):
                    raise ProviderError("provider_protocol_error") from None
                if message.stopped:
                    # Close before exposing executable tool calls or done, even
                    # if a consumer stops iterating immediately upon that event.
                    await _close_transport(response, client)
                for event in events:
                    yield event
                if message.stopped:
                    return
            raise ProviderError("provider_stream_truncated")
        except (TimeoutError, httpx.TimeoutException):
            raise ProviderError("provider_timeout") from None
        except httpx.HTTPError:
            raise ProviderError("provider_connection_error") from None
        finally:
            await _close_transport(response, client)


async def _close_transport(response: httpx.Response | None, client: httpx.AsyncClient) -> None:
    failed = False
    try:
        if response is not None:
            try:
                await response.aclose()
            except Exception:
                failed = True
    finally:
        try:
            await client.aclose()
        except Exception:
            failed = True
    if failed:
        raise ProviderError("provider_connection_error") from None


async def stream(
    messages: list[dict],
    *,
    system: str,
    tools: list[dict],
    model_cfg: dict,
    max_tokens: int,
    timeout_seconds: float,
) -> AsyncIterator[dict]:
    """Default production entry; callers close the iterator when stopping early."""
    iterator = AnthropicMessagesProvider().stream(
        messages,
        system=system,
        tools=tools,
        model_cfg=model_cfg,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
    )
    async with aclosing(iterator):
        async for event in iterator:
            yield event
