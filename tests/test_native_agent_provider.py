"""Only MockTransport and synthetic SSE; no app/config import, DB or model calls."""

import asyncio
import json

import httpx
import pytest

from server.agent import provider as P


@pytest.fixture(autouse=True)
def temp_data_dir(tmp_path):
    # Override conftest's app/database initializer for this transport-only module.
    return tmp_path


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, wait_after=None):
        self.chunks = chunks
        self.wait_after = wait_after
        self.closed = False
        self.reads = 0
        self.waiting = asyncio.Event()

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk
        if self.wait_after is not None:
            self.waiting.set()
            await self.wait_after.wait()

    async def aclose(self):
        self.closed = True


def frame(kind, **fields):
    return (
        f"event: {kind}\ndata: " + json.dumps({"type": kind, **fields}, ensure_ascii=False) + "\n\n"
    ).encode()


def start():
    return frame(
        "message_start",
        message={"role": "assistant", "content": [], "usage": {"input_tokens": 3, "output_tokens": 0}},
    )


def stop(reason="end_turn"):
    return frame("message_delta", delta={"stop_reason": reason}, usage={"output_tokens": 7}) + frame(
        "message_stop"
    )


def text_stream(text="hello"):
    return (
        start()
        + frame("content_block_start", index=0, content_block={"type": "text", "text": ""})
        + frame("content_block_delta", index=0, delta={"type": "text_delta", "text": text})
        + frame("content_block_stop", index=0)
        + stop()
    )


def config(base="https://api.deepseek.com/anthropic"):
    return {"base_url": base, "model": "synthetic-model", "api_key": "synthetic-key-not-from-environment"}


def source(chunks, *, status=200, limits=None, headers=None):
    wire = chunks if isinstance(chunks, Chunks) else Chunks(chunks)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers=headers or {"content-type": "text/event-stream"}, stream=wire)

    return P.AnthropicMessagesProvider(transport=httpx.MockTransport(handler), limits=limits), wire, requests


async def collect(provider, **kw):
    args = {
        "system": "synthetic system",
        "tools": [],
        "model_cfg": config(),
        "max_tokens": 100,
        "timeout_seconds": 5,
    }
    args.update(kw)
    return [event async for event in provider.stream([{"role": "user", "content": "synthetic"}], **args)]


@pytest.mark.parametrize(
    "base,expected",
    [
        ("https://api.deepseek.com/anthropic", "https://api.deepseek.com/anthropic/v1/messages"),
        ("https://api.deepseek.com/anthropic/v1/", "https://api.deepseek.com/anthropic/v1/messages"),
        ("https://api.minimax.io/anthropic/v1/", "https://api.minimax.io/anthropic/v1/messages"),
    ],
)
async def test_fragmented_utf8_frames_exact_endpoint_and_request_boundary(base, expected, monkeypatch):
    wire_bytes = text_stream("中文界面")
    provider, wire, requests = source([wire_bytes[i : i + 1] for i in range(len(wire_bytes))])
    options = []
    original = httpx.AsyncClient

    def client(**kwargs):
        options.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(P.httpx, "AsyncClient", client)
    events = await collect(provider, model_cfg=config(base))
    assert [e["text"] for e in events if e["type"] == "text_delta"] == ["中文界面"]
    assert events[-1]["content"] == [{"type": "text", "text": "中文界面"}]
    assert events[-2] == {"type": "usage", "input_tokens": 3, "output_tokens": 7}
    assert events[-1]["usage_complete"] is True
    assert wire.closed and len(requests) == 1 and str(requests[0].url) == expected
    assert requests[0].headers["x-api-key"] == config()["api_key"]
    assert config()["api_key"].encode() not in requests[0].content
    assert requests[0].headers["accept-encoding"] == "identity"
    assert options[0]["trust_env"] is False and options[0]["follow_redirects"] is False


@pytest.mark.parametrize(
    "initial,final", [({}, {}), ({"input_tokens": 5}, {}), ({"input_tokens": 5, "output_tokens": 0}, {})]
)
async def test_absent_or_unfinished_usage_is_unknown_not_synthesized_zero(initial, final):
    wire = frame("message_start", message={"role": "assistant", "content": [], "usage": initial})
    wire += frame("message_delta", delta={"stop_reason": "end_turn"}, usage=final)
    wire += frame("message_stop")
    transport, _, _ = source([wire])
    events = await collect(transport)
    assert events[-1]["usage_complete"] is False
    for event in events:
        if event["type"] == "usage":
            assert {key for key in event if key != "type"} == set(initial)


async def test_text_is_not_buffered_until_eof_and_early_close_releases_transport():
    first = (
        start()
        + frame("content_block_start", index=0, content_block={"type": "text", "text": ""})
        + frame("content_block_delta", index=0, delta={"type": "text_delta", "text": "first"})
    )
    provider, wire, _ = source(Chunks([first], wait_after=asyncio.Event()))
    iterator = provider.stream(
        [{"role": "user", "content": "hi"}],
        system="",
        tools=[],
        model_cfg=config(),
        max_tokens=1,
        timeout_seconds=1,
    )
    assert (await anext(iterator))["type"] == "usage"
    assert await anext(iterator) == {"type": "text_delta", "text": "first"}
    assert not wire.waiting.is_set()
    await iterator.aclose()
    assert wire.closed


def tool_stream(terminal=True, arguments='{"company":"中文","limit":2}'):
    data = start() + frame(
        "content_block_start",
        index=0,
        content_block={"type": "tool_use", "id": "call1", "name": "research_read", "input": {}},
    )
    for piece in (arguments[:8], arguments[8:]):
        data += frame(
            "content_block_delta", index=0, delta={"type": "input_json_delta", "partial_json": piece}
        )
    data += frame("content_block_stop", index=0)
    return data + stop("tool_use") if terminal else data


async def test_fragmented_tool_json_emitted_only_after_closed_terminal():
    provider, wire, _ = source([tool_stream()])
    events = []
    async for event in provider.stream(
        [{"role": "user", "content": "hi"}],
        system="",
        tools=[],
        model_cfg=config(),
        max_tokens=20,
        timeout_seconds=5,
    ):
        if event["type"] in {"tool_call", "done"}:
            assert wire.closed
        events.append(event)
    assert [e for e in events if e["type"] == "tool_call"] == [
        {
            "type": "tool_call",
            "id": "call1",
            "name": "research_read",
            "arguments": {"company": "中文", "limit": 2},
        }
    ]
    assert events[-1]["content"].continuation()[0]["input"]["limit"] == 2


async def test_truncated_stream_never_exposes_executable_tool_call():
    provider, wire, _ = source([tool_stream(terminal=False)])
    events = []
    with pytest.raises(P.ProviderError, match="^provider_stream_truncated$"):
        async for event in provider.stream(
            [{"role": "user", "content": "hi"}],
            system="",
            tools=[],
            model_cfg=config(),
            max_tokens=20,
            timeout_seconds=5,
        ):
            events.append(event)
    assert not any(e["type"] in {"tool_call", "done"} for e in events)
    assert wire.closed


async def test_server_web_search_blocks_preserve_actual_urls_and_tool_configuration():
    use = {
        "type": "server_tool_use",
        "id": "server1",
        "name": "web_search",
        "input": {"query": "site:sse.com.cn"},
    }
    result = {
        "type": "web_search_tool_result",
        "tool_use_id": "server1",
        "content": [
            {"type": "web_search_result", "url": "https://www.sse.com.cn/disclosure/example", "title": "线索"}
        ],
    }
    data = start()
    for index, block in enumerate((use, result)):
        data += frame("content_block_start", index=index, content_block=block)
        data += frame("content_block_stop", index=index)
    data += stop()
    provider, wire, requests = source([data])
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
    events = await collect(provider, tools=tools)
    assert [e["block"] for e in events if e["type"].startswith("server_tool")] == [use, result]
    assert json.loads(requests[0].content)["tools"] == tools and wire.closed


async def test_reasoning_and_opaque_only_in_ram_not_serialized_or_emitted():
    data = start()
    blocks = [
        {"type": "thinking", "thinking": "", "signature": ""},
        {"type": "future_private_block", "data": "opaque-private-fixture"},
        {"type": "text", "text": "公开结论"},
    ]
    for index, block in enumerate(blocks):
        data += frame("content_block_start", index=index, content_block=block)
        if index == 0:
            data += frame(
                "content_block_delta",
                index=0,
                delta={"type": "thinking_delta", "thinking": "private-reasoning-fixture"},
            )
            data += frame(
                "content_block_delta",
                index=0,
                delta={"type": "signature_delta", "signature": "opaque-signature"},
            )
        data += frame("content_block_stop", index=index)
    provider, _, _ = source([data + stop()])
    events = await collect(provider)
    serialized = json.dumps(events)
    assert "private-reasoning-fixture" not in serialized and "opaque-private-fixture" not in serialized
    assert "private-reasoning-fixture" not in repr(events)
    full = events[-1]["content"].continuation()
    assert full[0] == {
        "type": "thinking",
        "thinking": "private-reasoning-fixture",
        "signature": "opaque-signature",
    }
    assert full[1] == blocks[1]
    full[0]["thinking"] = "mutated"
    assert events[-1]["content"].continuation()[0]["thinking"] == "private-reasoning-fixture"


async def test_private_vendor_fields_on_public_blocks_are_not_serialized():
    block = {"type": "text", "text": "answer", "reasoning_content": "vendor-private-reasoning"}
    data = start() + frame("content_block_start", index=0, content_block=block)
    data += frame("content_block_stop", index=0) + stop()
    provider, _, _ = source([data])
    events = await collect(provider)
    assert "vendor-private-reasoning" not in json.dumps(events)
    assert events[-1]["content"].continuation() == [block]


async def test_module_entry_closes_inner_transport_on_explicit_close(monkeypatch):
    provider, wire, _ = source(Chunks([start()], wait_after=asyncio.Event()))
    monkeypatch.setattr(P, "AnthropicMessagesProvider", lambda: provider)
    iterator = P.stream(
        [{"role": "user", "content": "hi"}],
        system="",
        tools=[],
        model_cfg=config(),
        max_tokens=1,
        timeout_seconds=1,
    )
    assert (await anext(iterator))["type"] == "usage"
    await iterator.aclose()
    assert wire.closed


@pytest.mark.parametrize(
    "url",
    [
        "http://api.deepseek.com/anthropic",
        "https://api.deepseek.com.evil/anthropic",
        "https://user:secret@api.deepseek.com/anthropic",
        "https://api.deepseek.com:443/anthropic",
        "https://api.deepseek.com/anthropic?key=secret",
        "https://api.deepseek.com/anthropic#x",
        "https://api.minimax.io/anthropic",
        "https://127.0.0.1/anthropic",
        "https://example.org/messages",
    ],
)
async def test_nonconfigured_origins_or_paths_rejected_before_transport(url):
    provider, _, requests = source([text_stream()])
    with pytest.raises(P.ProviderError, match="^provider_configuration_error$"):
        await collect(provider, model_cfg=config(url))
    assert requests == []


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "provider_auth_error"),
        (403, "provider_auth_error"),
        (429, "provider_rate_limited"),
        (400, "provider_http_error"),
        (500, "provider_http_error"),
        (302, "provider_http_error"),
    ],
)
async def test_http_error_body_never_read_echoed_or_retried(status, code):
    provider, wire, requests = source(
        [b"secret-fixture-error-body-key"],
        status=status,
        headers={"location": "https://attacker.invalid/secret"},
    )
    with pytest.raises(P.ProviderError) as caught:
        await collect(provider)
    assert str(caught.value) == code and "secret" not in repr(caught.value)
    assert wire.reads == 0 and wire.closed and len(requests) == 1


@pytest.mark.parametrize(
    "data,code",
    [
        (start() + frame("error", error={"message": "secret-in-upstream-error"}), "provider_upstream_error"),
        (start() + b"data: not-json-secret\n\n", "provider_protocol_error"),
        (start() + frame("message_stop"), "provider_protocol_error"),
        (start() + b'event: wrong\ndata: {"type":"message_stop"}\n\n', "provider_protocol_error"),
        (start() + b'data: {"type":"ping","type":"error"}\n\n', "provider_protocol_error"),
        (start() + b"data: " + '{"type":"ping"}'.encode("utf-16") + b"\n\n", "provider_protocol_error"),
        (start(), "provider_stream_truncated"),
        (tool_stream(arguments='{"x":1,"x":2}'), "provider_protocol_error"),
        (tool_stream(arguments='{"incomplete":'), "provider_protocol_error"),
    ],
)
async def test_malformed_or_upstream_errors_have_only_sanitized_codes(data, code):
    provider, wire, _ = source([data])
    with pytest.raises(P.ProviderError) as caught:
        await collect(provider)
    assert str(caught.value) == code and "secret" not in str(caught.value)
    assert wire.closed


@pytest.mark.parametrize(
    "limits,data,code",
    [
        (P.ProviderLimits(max_event_bytes=40), text_stream(), "provider_event_too_large"),
        (P.ProviderLimits(max_response_bytes=50), text_stream(), "provider_response_too_large"),
        (P.ProviderLimits(max_tool_json_bytes=8), tool_stream(), "provider_tool_json_too_large"),
        (P.ProviderLimits(max_events=1), text_stream(), "provider_response_too_large"),
        (P.ProviderLimits(max_request_bytes=10), text_stream(), "provider_input_error"),
    ],
)
async def test_byte_and_event_bounds(limits, data, code):
    provider, wire, requests = source([data], limits=limits)
    with pytest.raises(P.ProviderError, match="^" + code + "$"):
        await collect(provider)
    assert wire.closed if requests else not requests


async def test_timeout_and_task_cancellation_close_stream_without_retry():
    provider, wire, requests = source(Chunks([], wait_after=asyncio.Event()))
    with pytest.raises(P.ProviderError, match="^provider_timeout$"):
        await collect(provider, timeout_seconds=0.01)
    assert wire.closed and len(requests) == 1
    provider, wire, requests = source(Chunks([], wait_after=asyncio.Event()))
    task = asyncio.create_task(collect(provider))
    await wire.waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert wire.closed and len(requests) == 1


async def test_multiline_data_crlf_bom_ping_and_unknown_top_level_events():
    data = b"\xef\xbb\xbf: keepalive\r\n\r\n" + start()
    data += frame("new_provider_metadata", metadata={"do_not_emit": "private"})
    data += b'event: ping\r\ndata: {"type":\r\ndata: "ping"}\r\n\r\n'
    data += frame("content_block_start", index=0, content_block={"type": "text", "text": "ok"})
    data += frame("content_block_stop", index=0) + stop()
    provider, _, _ = source([data[i : i + 3] for i in range(0, len(data), 3)])
    events = await collect(provider)
    assert events[-1]["content"] == [{"type": "text", "text": "ok"}]
    assert "do_not_emit" not in json.dumps(events)
