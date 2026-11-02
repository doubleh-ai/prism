"""Bounded direct Responses HTTP adapter; never a CLI or provider fallback.

Protocol: https://developers.openai.com/api/docs/guides/function-calling
Events: https://developers.openai.com/api/reference/resources/responses/streaming-events
Stateless reasoning: https://developers.openai.com/api/docs/guides/reasoning
Only complete response items may authorize platform tool execution. Private
continuation stays in RAM; only public text, tool calls and actual URLs leave it.
"""

from __future__ import annotations

import asyncio
import copy
import math
import re
import time
from urllib.parse import parse_qsl, quote, urlsplit

import httpx

from . import provider as P

ENDPOINTS = P._OPENAI_ENDPOINTS
_ITEM_FIELDS = {
    "message": {"id", "type", "status", "role", "content", "phase"},
    "function_call": {"id", "type", "status", "call_id", "name", "arguments"},
    "reasoning": {"id", "type", "status", "summary", "encrypted_content"},
    "web_search_call": {"id", "type", "status", "action"},
}


def _identifier(value):
    P._need(type(value) is str and 0 < len(value) <= 256 and all(33 <= ord(c) <= 126 for c in value))
    return value


def _item(value):
    P._need(type(value) is dict and type(value.get("type")) is str and value["type"] in _ITEM_FIELDS)
    _identifier(value.get("id"))
    return {k: copy.deepcopy(v) for k, v in value.items() if k in _ITEM_FIELDS[value["type"]]}


def _url(value):
    if type(value) is not str or len(value) > 4096 or any(ord(c) < 33 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            return None
        if any(
            k.lower() in {"key", "api_key", "apikey", "token", "password", "secret", "signature", "sig"}
            for k, _ in parse_qsl(parsed.query)
        ):
            return None
    except ValueError:
        return None
    return value


def _public_text(part):
    text = part.get("text") if part.get("type") == "output_text" else part.get("refusal")
    P._need(type(text) is str)
    annotations = part.get("annotations", [])
    P._need(type(annotations) is list and len(annotations) <= 256)
    links = []
    seen = set()
    for annotation in annotations:
        if type(annotation) is not dict or annotation.get("type") != "url_citation":
            continue
        url = _url(annotation.get("url"))
        if not url or url in seen:
            continue
        title = annotation.get("title")
        if type(title) is not str or not title.strip():
            title = "来源"
        title = re.sub(r"[\[\]\\\r\n]", " ", title)[:200]
        links.append("[" + title + "](" + quote(url, safe=":/?=&%#@+;,-._~") + ")")
        seen.add(url)
    # Convert vendor-only citation markers into portable, clickable citations.
    # This is attribution, not a claim that the original body was verified.
    if links:
        text = re.sub(r"cite[^]{0,500}", "", text)
        text += "\n\n来源：" + " · ".join(links)
    return text


def _input(messages):
    result = []
    for message in messages:
        role = message["role"]
        content = message.get("content")
        if type(content) is str:
            result.append({"role": role, "content": content})
            continue
        P._need(type(content) is list, "provider_input_error")
        for block in content:
            P._need(type(block) is dict, "provider_input_error")
            kind = block.get("type")
            if kind == "_openai_response_item" and role == "assistant":
                result.append(_item(block.get("item")))
            elif kind == "text" and type(block.get("text")) is str:
                result.append({"role": role, "content": block["text"]})
            elif kind == "tool_use" and role == "assistant":
                P._need(type(block.get("input")) is dict, "provider_input_error")
                result.append(
                    {
                        "type": "function_call",
                        "call_id": _identifier(block.get("id")),
                        "name": _identifier(block.get("name")),
                        "arguments": P._json_bytes(block["input"], "provider_input_error").decode(),
                    }
                )
            elif kind == "tool_result" and role == "user":
                output = block.get("content")
                P._need(type(output) is str, "provider_input_error")
                result.append(
                    {
                        "type": "function_call_output",
                        "call_id": _identifier(block.get("tool_use_id")),
                        "output": output,
                    }
                )
            else:
                raise P.ProviderError("provider_input_error")
    return result


def _tools(tools):
    result = []
    names = set()
    searches = 0
    for tool in tools:
        if tool.get("type") == "web_search_20250305":
            P._need(
                not searches
                and tool.get("name") == "web_search"
                and type(tool.get("max_uses")) is int
                and 1 <= tool["max_uses"] <= 100,
                "provider_input_error",
            )
            searches = tool["max_uses"]
            result.append({"type": "web_search"})
        else:
            P._need(tool.get("type") is None, "provider_input_error")
            name = _identifier(tool.get("name"))
            P._need(name not in names and type(tool.get("input_schema")) is dict, "provider_input_error")
            P._need(type(tool.get("description", "")) is str, "provider_input_error")
            names.add(name)
            # Existing optional tool fields remain optional; closed schemas and
            # permissions are always revalidated by the platform before effects.
            result.append(
                {
                    "type": "function",
                    "name": name,
                    "description": tool.get("description", ""),
                    "parameters": tool["input_schema"],
                    "strict": False,
                }
            )
    return result, names, searches


class _Response:
    def __init__(self, limits, names, searches):
        self.limits, self.names, self.searches = limits, names, searches
        self.started = self.stopped = False
        self.id = None
        self.items = {}
        self.complete = {}
        self.ids = set()
        self.deltas = {}
        self.last_sequence = -1
        self.tool_bytes = 0
        self.final_tool_bytes = 0
        self.search_count = 0
        self.public = []
        self.failure = None

    def _usage(self, response):
        usage = response.get("usage")
        if usage is None:
            return [], False
        P._need(type(usage) is dict)
        event = {"type": "usage"}
        for key in ("input_tokens", "output_tokens"):
            value = usage.get(key)
            if value is not None:
                P._need(type(value) is int and 0 <= value <= 10**12)
                event[key] = value
        return ([event] if len(event) > 1 else []), len(event) == 3

    def feed(self, event):
        P._need(not self.stopped)
        seq = event.get("sequence_number")
        if seq is not None:
            P._need(type(seq) is int and seq > self.last_sequence)
            self.last_sequence = seq
        kind = event["type"]
        if kind == "response.created":
            P._need(not self.started and type(event.get("response")) is dict)
            self.id = _identifier(event["response"].get("id"))
            self.started = True
            return []
        P._need(self.started)
        if kind == "response.output_item.added":
            index = event.get("output_index")
            item = _item(event.get("item"))
            P._need(
                type(index) is int
                and 0 <= index < self.limits.max_blocks
                and index not in self.items
                and item["id"] not in self.ids
            )
            self.items[index] = item
            self.ids.add(item["id"])
            if item["type"] == "web_search_call":
                P._need(self.searches > 0)
                self.search_count += 1
                P._need(self.search_count <= self.searches)
                # Count observed work even if a later stream is truncated.
                return [
                    {
                        "type": "server_tool_use",
                        "block": {
                            "type": "server_tool_use",
                            "id": item["id"],
                            "name": "web_search",
                            "input": {},
                        },
                    }
                ]
            return []
        if kind in {
            "response.output_text.delta",
            "response.refusal.delta",
            "response.function_call_arguments.delta",
        }:
            index = event.get("output_index")
            item = self.items.get(index)
            P._need(
                item is not None
                and index not in self.complete
                and event.get("item_id") == item["id"]
                and type(event.get("delta")) is str
            )
            if kind == "response.function_call_arguments.delta":
                P._need(item["type"] == "function_call")
                part = -1
                self.tool_bytes += len(event["delta"].encode())
                P._need(self.tool_bytes <= self.limits.max_tool_json_bytes, "provider_tool_json_too_large")
            else:
                P._need(item["type"] == "message")
                part = event.get("content_index")
                P._need(type(part) is int and 0 <= part < self.limits.max_blocks)
            self.deltas.setdefault((index, part), []).append(event["delta"])
            return []
        if kind == "response.output_item.done":
            index = event.get("output_index")
            value = _item(event.get("item"))
            old = self.items.get(index)
            P._need(
                old is not None
                and index not in self.complete
                and old["id"] == value["id"]
                and old["type"] == value["type"]
            )
            emitted = []
            if value["type"] == "message":
                P._need(
                    value.get("role") == "assistant"
                    and type(value.get("content")) is list
                    and len(value["content"]) <= self.limits.max_blocks
                )
                for i, part in enumerate(value["content"]):
                    P._need(type(part) is dict and part.get("type") in {"output_text", "refusal"})
                    text = part.get("text") if part["type"] == "output_text" else part.get("refusal")
                    P._need(type(text) is str)
                    if (index, i) in self.deltas:
                        P._need("".join(self.deltas[(index, i)]) == text)
                    public = _public_text(part)
                    if public:
                        self.public.append({"type": "text", "text": public})
                        emitted.append({"type": "text_delta", "text": public})
            elif value["type"] == "function_call":
                P._need(type(value.get("arguments")) is str)
                self.final_tool_bytes += len(value["arguments"].encode())
                if (index, -1) in self.deltas:
                    P._need("".join(self.deltas[(index, -1)]) == value["arguments"])
                P._need(
                    self.final_tool_bytes <= self.limits.max_tool_json_bytes,
                    "provider_tool_json_too_large",
                )
            elif value["type"] == "web_search_call":
                action = value.get("action", {})
                P._need(type(action) is dict)
                sources = action.get("sources", [])
                P._need(type(sources) is list and len(sources) <= 256)
                found = []
                for source in sources:
                    if type(source) is not dict:
                        continue
                    url = _url(source.get("url"))
                    if url:
                        found.append(
                            {
                                "type": "web_search_result",
                                "url": url,
                                "title": source.get("title") if type(source.get("title")) is str else url,
                            }
                        )
                block = {"type": "web_search_tool_result", "tool_use_id": value["id"], "content": found}
                self.public.append(block)
                emitted.append({"type": "server_tool_result", "block": block})
            self.complete[index] = value
            return emitted
        if kind in {"response.completed", "response.incomplete", "response.failed", "response.cancelled"}:
            response = event.get("response")
            P._need(type(response) is dict and response.get("id") == self.id)
            usage, usage_complete = self._usage(response)
            self.stopped = True
            if kind in {"response.failed", "response.cancelled"}:
                P._need(response.get("status") == kind.removeprefix("response."))
                self.failure = "provider_upstream_error"
                return usage
            expected = "completed" if kind == "response.completed" else "incomplete"
            P._need(response.get("status") == expected and type(response.get("output")) is list)
            output = [_item(i) for i in response["output"]]
            P._need(
                len(output) == len(self.complete) == len(self.items)
                and all(self.complete.get(i) == v for i, v in enumerate(output))
            )
            calls = []
            call_ids = set()
            reason = "end_turn"
            if kind == "response.completed":
                for item in output:
                    P._need(item.get("status", "completed") == "completed")
                    if item["type"] == "function_call":
                        name = _identifier(item.get("name"))
                        call = _identifier(item.get("call_id"))
                        P._need(name in self.names and call not in call_ids)
                        arguments = P._loads(item["arguments"])
                        P._need(type(arguments) is dict)
                        call_ids.add(call)
                        calls.append({"type": "tool_call", "id": call, "name": name, "arguments": arguments})
                        self.public.append({"type": "tool_use", "id": call, "name": name, "input": arguments})
                if calls:
                    reason = "tool_use"
                elif any(
                    p.get("type") == "refusal" for i in output if i["type"] == "message" for p in i["content"]
                ):
                    reason = "refusal"
            else:
                details = response.get("incomplete_details")
                P._need(type(details) is dict)
                reason = {"max_output_tokens": "max_tokens", "content_filter": "refusal"}.get(
                    details.get("reason")
                )
                if reason is None:
                    self.failure = "provider_upstream_error"
                    return usage
            private = [
                {"type": "_openai_response_item", "item": v}
                for v in output
                if v.get("status", "completed") == "completed"
            ]
            return [
                *usage,
                *calls,
                {
                    "type": "done",
                    "stop_reason": reason,
                    "content": P.AssistantContent(self.public, private),
                    "usage_complete": usage_complete,
                },
            ]
        if kind == "error":
            self.stopped = True
            self.failure = "provider_upstream_error"
        # Non-mutating lifecycle/progress events carry no executable authority.
        return []


class OpenAIResponsesProvider(P.AnthropicMessagesProvider):
    async def stream(self, messages, *, system, tools, model_cfg, max_tokens, timeout_seconds):
        P._need(type(model_cfg) is dict, "provider_configuration_error")
        base, key, model = (model_cfg.get(k) for k in ("base_url", "api_key", "model"))
        P._need(type(base) is str and base.rstrip("/") in ENDPOINTS, "provider_configuration_error")
        P._need(
            type(key) is str and 0 < len(key) <= 8192 and all(33 <= ord(c) <= 126 for c in key),
            "provider_configuration_error",
        )
        P._need(
            type(model) is str and 0 < len(model) <= 128 and all(32 <= ord(c) < 127 for c in model),
            "provider_configuration_error",
        )
        P._need(
            type(messages) is list
            and 0 < len(messages) <= 2048
            and all(type(m) is dict and m.get("role") in ("user", "assistant") for m in messages)
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
        try:
            definitions, names, searches = _tools(tools)
            inputs = _input(messages)
        except (P.ProviderError, KeyError, IndexError, TypeError, ValueError, RecursionError, UnicodeError):
            raise P.ProviderError("provider_input_error") from None
        payload = {
            "model": model,
            "input": inputs,
            "instructions": system,
            "tools": definitions,
            "stream": True,
            "store": False,
            "max_output_tokens": max_tokens,
            "include": ["reasoning.encrypted_content"],
        }
        if searches:
            payload["max_tool_calls"] = searches
            payload["include"].append("web_search_call.action.sources")
        effort = model_cfg.get("reasoning_effort")
        if effort is not None:
            P._need(
                type(effort) is str and effort in {"none", "minimal", "low", "medium", "high", "xhigh"},
                "provider_configuration_error",
            )
            payload["reasoning"] = {"effort": effort}
        body = P._json_bytes(payload, "provider_input_error")
        P._need(len(body) <= self.limits.max_request_bytes, "provider_input_error")
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
                "https://api.openai.com/v1/responses",
                content=body,
                headers={
                    "authorization": "Bearer " + key,
                    "content-type": "application/json",
                    "accept": "text/event-stream",
                    "accept-encoding": "identity",
                },
            )
            response = await asyncio.wait_for(client.send(request, stream=True), timeout_seconds)
            if response.status_code != 200:
                raise P.ProviderError(
                    {
                        401: "provider_auth_error",
                        403: "provider_auth_error",
                        429: "provider_rate_limited",
                    }.get(response.status_code, "provider_http_error")
                )
            P._need(
                response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                == "text/event-stream"
                and response.headers.get("content-encoding", "identity").lower() in {"", "identity"}
            )
            state = _Response(self.limits, names, searches)
            async for frame in P._frames(response, self.limits, deadline):
                try:
                    events = state.feed(frame)
                except (KeyError, IndexError, TypeError, ValueError, RecursionError, UnicodeError):
                    raise P.ProviderError("provider_protocol_error") from None
                if state.stopped:
                    await P._close_transport(response, client)
                for event in events:
                    yield event
                if state.stopped:
                    if state.failure:
                        raise P.ProviderError(state.failure)
                    return
            raise P.ProviderError("provider_stream_truncated")
        except (TimeoutError, httpx.TimeoutException):
            raise P.ProviderError("provider_timeout") from None
        except httpx.HTTPError:
            raise P.ProviderError("provider_connection_error") from None
        finally:
            await P._close_transport(response, client)
