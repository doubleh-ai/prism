"""Synthetic Responses HTTP contracts; no credentials, app imports or network."""

import asyncio
import copy
import json

import httpx
import pytest

from server.agent import provider as P
from server.agent import provider_openai as O


@pytest.fixture(autouse=True)
def temp_data_dir(tmp_path):
    return tmp_path


CFG = {
    "base_url": "https://api.openai.com/v1",
    "model": "synthetic-model",
    "api_key": "synthetic-not-an-environment-key",
    "protocol": "openai_responses",
}
TOOL = {
    "name": "indicator_list",
    "description": "合成指标",
    "input_schema": {
        "type": "object",
        "properties": {"limit": {"type": "integer"}},
        "additionalProperties": False,
    },
}


def text_item(text="合成回答", **kwargs):
    return {
        "type": "message",
        "id": "msg_fixture",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
        **kwargs,
    }


def tool_item(**kwargs):
    return {
        "type": "function_call",
        "id": "fc_fixture",
        "call_id": "call_fixture",
        "name": "indicator_list",
        "arguments": "{}",
        "status": "completed",
        **kwargs,
    }


def frame(kind, **fields):
    return (
        f"event: {kind}\ndata: " + json.dumps({"type": kind, **fields}, ensure_ascii=False) + "\n\n"
    ).encode()


def frames(items=None, *, status="completed", usage=None, reason=None):
    items = [text_item()] if items is None else items
    result = [frame("response.created", response={"id": "resp_fixture", "status": "in_progress"})]
    for index, item in enumerate(items):
        initial = {**copy.deepcopy(item), "status": "in_progress"}
        if item["type"] == "message":
            initial["content"] = []
        elif item["type"] == "function_call":
            initial["arguments"] = ""
        elif item["type"] == "reasoning":
            initial["encrypted_content"] = "incomplete-private-fixture"
        result += [frame("response.output_item.added", output_index=index, item=initial)]
        if item["type"] == "message":
            for ci, part in enumerate(item["content"]):
                key = "refusal" if part["type"] == "refusal" else "text"
                kind = "response.refusal.delta" if key == "refusal" else "response.output_text.delta"
                for delta in (part[key][:1], part[key][1:]):
                    result.append(
                        frame(kind, item_id=item["id"], output_index=index, content_index=ci, delta=delta)
                    )
        elif item["type"] == "function_call":
            result.append(
                frame(
                    "response.function_call_arguments.delta",
                    item_id=item["id"],
                    output_index=index,
                    delta=item["arguments"],
                )
            )
        result.append(frame("response.output_item.done", output_index=index, item=item))
    terminal = {
        "id": "resp_fixture",
        "status": status,
        "output": items,
        "usage": {"input_tokens": 11, "output_tokens": 7} if usage is None else usage,
    }
    if reason:
        terminal["incomplete_details"] = {"reason": reason}
    result.append(frame("response." + status, response=terminal))
    return result


class Wire(httpx.AsyncByteStream):
    def __init__(self, chunks, wait=False):
        self.chunks, self.wait = chunks, wait
        self.closed = False
        self.waiting = asyncio.Event()

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.wait:
            self.waiting.set()
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


def source(chunks=None, *, wire=None, status=200, headers=None, limits=None):
    wire = wire or Wire(frames() if chunks is None else chunks)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers=headers or {"content-type": "text/event-stream"}, stream=wire)

    return O.OpenAIResponsesProvider(transport=httpx.MockTransport(handler), limits=limits), wire, requests


async def collect(provider, messages=None, **kw):
    args = {"system": "合成公开说明", "tools": [], "model_cfg": CFG, "max_tokens": 128, "timeout_seconds": 2}
    args.update(kw)
    return [e async for e in provider.stream(messages or [{"role": "user", "content": "合成问题"}], **args)]


@pytest.mark.parametrize("base", ["https://api.openai.com", "https://api.openai.com/v1/"])
async def test_explicit_direct_endpoint_fragmented_utf8_and_no_storage_proxy_or_retry(base, monkeypatch):
    wire = b"".join(frames())
    provider, closed, requests = source([wire[i : i + 1] for i in range(len(wire))])
    options = []
    original = httpx.AsyncClient

    def client(**kwargs):
        options.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(O.httpx, "AsyncClient", client)
    events = await collect(provider, model_cfg={**CFG, "base_url": base})
    assert closed.closed and len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://api.openai.com/v1/responses"
    assert request.headers["authorization"] == "Bearer " + CFG["api_key"]
    assert CFG["api_key"].encode() not in request.content
    body = json.loads(request.content)
    assert body["store"] is False and body["stream"] is True
    assert "previous_response_id" not in body and "conversation" not in body
    assert body["model"] == CFG["model"] and body["max_output_tokens"] == 128
    assert options[0]["trust_env"] is False and options[0]["follow_redirects"] is False
    assert request.headers["accept-encoding"] == "identity"
    assert [e["text"] for e in events if e["type"] == "text_delta"] == ["合成回答"]
    assert events[-1]["usage_complete"] is True and events[-1]["stop_reason"] == "end_turn"


async def test_item_text_available_before_terminal_early_close_releases_transport():
    provider, wire, _ = source(wire=Wire(frames()[:-1], wait=True))
    stream = provider.stream(
        [{"role": "user", "content": "合成"}],
        system="",
        tools=[],
        model_cfg=CFG,
        max_tokens=128,
        timeout_seconds=2,
    )
    event = await anext(stream)
    assert event == {"type": "text_delta", "text": "合成回答"} and not wire.closed
    await stream.aclose()
    assert wire.closed


async def test_only_response_terminal_releases_executable_calls_and_transport_is_already_closed():
    provider, wire, _ = source(frames([tool_item()]))
    stream = provider.stream(
        [{"role": "user", "content": "合成"}],
        system="",
        tools=[TOOL],
        model_cfg=CFG,
        max_tokens=128,
        timeout_seconds=2,
    )
    events = []
    async for event in stream:
        events.append(event)
        assert wire.closed
        if event["type"] == "tool_call":
            break
    await stream.aclose()
    assert events[-1] == {
        "type": "tool_call",
        "id": "call_fixture",
        "name": "indicator_list",
        "arguments": {},
    }
    truncated, truncated_wire, requests = source(frames([tool_item()])[:-1])
    observed = []
    with pytest.raises(P.ProviderError, match="provider_stream_truncated"):
        async for event in truncated.stream(
            [{"role": "user", "content": "合成"}],
            system="",
            tools=[TOOL],
            model_cfg=CFG,
            max_tokens=128,
            timeout_seconds=2,
        ):
            observed.append(event)
    assert not observed and truncated_wire.closed and len(requests) == 1


async def test_private_reasoning_continuation_kept_only_in_ram_and_call_results_roundtrip():
    reasoning = {
        "id": "rs_fixture",
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": "private-summary-fixture"}],
        "encrypted_content": "complete-private-fixture",
    }
    message = text_item("公开", phase="commentary")
    adapter, _, _ = source(frames([reasoning, message, tool_item()]))
    result = (await collect(adapter, tools=[TOOL]))[-1]
    assert result["stop_reason"] == "tool_use"
    assert "private-" not in json.dumps(result, ensure_ascii=False)
    assert "private-" not in repr(result)
    continuation = result["content"].continuation()
    second, _, requests = source()
    await collect(
        second,
        [
            {"role": "user", "content": "合成问题"},
            {"role": "assistant", "content": continuation},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "call_fixture", "content": '{"count":2}'}],
            },
        ],
    )
    inputs = json.loads(requests[0].content)["input"]
    assert inputs[1] == reasoning and inputs[2]["phase"] == "commentary"
    assert inputs[-1] == {"type": "function_call_output", "call_id": "call_fixture", "output": '{"count":2}'}
    assert b"incomplete-private-fixture" not in requests[0].content
    continuation[0]["item"]["summary"] = []
    assert result["content"].continuation()[0]["item"]["summary"] == reasoning["summary"]


async def test_search_is_bounded_with_actual_urls_and_portable_visible_citations():
    search = {
        "id": "ws_fixture",
        "type": "web_search_call",
        "status": "completed",
        "action": {
            "type": "search",
            "queries": ["site:sse.com.cn 合成"],
            "sources": [
                {"type": "url", "url": "https://www.sse.com.cn/fixture", "title": "合成公告"},
                {"type": "url", "url": "https://example.com/?api_key=synthetic"},
            ],
        },
    }
    message = text_item("线索待核验citeturn0search0")
    message["content"][0]["annotations"] = [
        {
            "type": "url_citation",
            "url": "https://www.sse.com.cn/fixture",
            "title": "合成公告",
            "start_index": 0,
            "end_index": 5,
        }
    ]
    adapter, _, requests = source(frames([search, message]))
    events = await collect(
        adapter, tools=[TOOL, {"type": "web_search_20250305", "name": "web_search", "max_uses": 2}]
    )
    body = json.loads(requests[0].content)
    assert body["max_tool_calls"] == 2 and body["tools"][-1] == {"type": "web_search"}
    assert body["tools"][0]["strict"] is False and body["tools"][0]["parameters"] == TOOL["input_schema"]
    assert "web_search_call.action.sources" in body["include"]
    assert [e["type"] for e in events][:2] == ["server_tool_use", "server_tool_result"]
    assert events[1]["block"]["content"] == [
        {"type": "web_search_result", "url": "https://www.sse.com.cn/fixture", "title": "合成公告"}
    ]
    text = next(e["text"] for e in events if e["type"] == "text_delta")
    assert "" not in text and "[合成公告](https://www.sse.com.cn/fixture)" in text


@pytest.mark.parametrize(
    "usage,keys",
    [
        ({}, set()),
        ({"input_tokens": 3}, {"input_tokens"}),
        ({"input_tokens": 0, "output_tokens": 0}, {"input_tokens", "output_tokens"}),
    ],
)
async def test_usage_unknown_is_not_zero(usage, keys):
    adapter, _, _ = source(frames(usage=usage))
    events = await collect(adapter)
    assert events[-1]["usage_complete"] == (len(keys) == 2)
    assert set().union(*(set(e) - {"type"} for e in events if e["type"] == "usage")) == keys


@pytest.mark.parametrize(
    "reason,expected", [("max_output_tokens", "max_tokens"), ("content_filter", "refusal")]
)
async def test_incomplete_response_never_authorizes_tool_execution(reason, expected):
    adapter, _, _ = source(frames([tool_item()], status="incomplete", reason=reason))
    events = await collect(adapter, tools=[TOOL])
    assert events[-1]["stop_reason"] == expected
    assert not any(e["type"] == "tool_call" for e in events)


@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_failed_terminal_preserves_observed_usage_without_exposing_upstream_error(status):
    wire = [
        frame("response.created", response={"id": "resp_fixture"}),
        frame(
            "response." + status,
            response={
                "id": "resp_fixture",
                "status": status,
                "usage": {"input_tokens": 9},
                "error": {"message": "private-upstream-fixture"},
            },
        ),
    ]
    adapter, closed, requests = source(wire)
    events = []
    with pytest.raises(P.ProviderError) as err:
        async for event in adapter.stream(
            [{"role": "user", "content": "合成"}],
            system="",
            tools=[],
            model_cfg=CFG,
            max_tokens=128,
            timeout_seconds=2,
        ):
            events.append(event)
    assert str(err.value) == "provider_upstream_error" and closed.closed and len(requests) == 1
    assert events == [{"type": "usage", "input_tokens": 9}]


@pytest.mark.parametrize("arguments", ['{"x":1,"x":2}', "[]", "null", '{"x":NaN}', "{"])
async def test_invalid_or_ambiguous_arguments_are_never_executed(arguments):
    adapter, closed, _ = source(frames([tool_item(arguments=arguments)]))
    with pytest.raises(P.ProviderError, match="provider_protocol_error"):
        await collect(adapter, tools=[TOOL])
    assert closed.closed


@pytest.mark.parametrize(
    "case",
    [
        "missing_start",
        "mismatched_terminal",
        "duplicate_id",
        "unknown_tool",
        "changed_arguments",
        "regressed_sequence",
        "unsupported_item",
    ],
)
async def test_stream_state_and_terminal_integrity(case):
    wire = frames([tool_item()])
    if case == "missing_start":
        wire = wire[1:]
    if case == "mismatched_terminal":
        wire[-1] = wire[-1].replace(b"resp_fixture", b"resp_other")
    if case == "duplicate_id":
        wire = frames([tool_item(), tool_item(call_id="call_other")])
    if case == "unknown_tool":
        wire = frames([tool_item(name="unregistered")])
    if case == "changed_arguments":
        wire[2] = wire[2].replace(b'"delta": "{}"', b'"delta": "[]"')
    if case == "regressed_sequence":
        wire[0] = frame("response.created", sequence_number=2, response={"id": "resp_fixture"})
        wire[1] = frame("response.in_progress", sequence_number=1)
    if case == "unsupported_item":
        wire = frames([{"id": "computer_fixture", "type": "computer_call"}])
    adapter, closed, _ = source(wire)
    with pytest.raises(P.ProviderError, match="provider_protocol_error"):
        await collect(adapter, tools=[TOOL])
    assert closed.closed


@pytest.mark.parametrize(
    "cfg",
    [
        {**CFG, "base_url": "https://api.openai.com.evil/v1"},
        {**CFG, "base_url": "http://api.openai.com/v1"},
        {**CFG, "api_key": ""},
        {**CFG, "model": ""},
        {**CFG, "api_key": "bad\nkey"},
    ],
)
async def test_invalid_configuration_is_rejected_before_any_request(cfg):
    adapter, _, requests = source()
    with pytest.raises(P.ProviderError, match="provider_configuration_error"):
        await collect(adapter, model_cfg=cfg)
    assert not requests


@pytest.mark.parametrize(
    "status,code",
    [
        (302, "provider_http_error"),
        (401, "provider_auth_error"),
        (403, "provider_auth_error"),
        (429, "provider_rate_limited"),
        (500, "provider_http_error"),
    ],
)
async def test_http_failure_is_closed_sanitized_and_never_retried(status, code):
    adapter, closed, requests = source(
        status=status, headers={"content-type": "text/plain", "location": "https://example.com"}
    )
    with pytest.raises(P.ProviderError, match=code):
        await collect(adapter)
    assert closed.closed and len(requests) == 1


async def test_deadline_and_consumer_cancellation_close_real_async_transport():
    adapter, wire, requests = source(wire=Wire(frames()[:1], wait=True))
    with pytest.raises(P.ProviderError, match="provider_timeout"):
        await collect(adapter, timeout_seconds=0.03)
    assert wire.closed and len(requests) == 1
    adapter, wire, _ = source(wire=Wire(frames()[:1], wait=True))
    task = asyncio.create_task(collect(adapter))
    await asyncio.wait_for(wire.waiting.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert wire.closed


async def test_explicit_protocol_dispatch_and_no_automatic_model_change(monkeypatch):
    adapter, wire, requests = source()
    monkeypatch.setattr(O, "OpenAIResponsesProvider", lambda: adapter)
    result = [
        e
        async for e in P.stream(
            [{"role": "user", "content": "合成"}],
            system="",
            tools=[],
            model_cfg=CFG,
            max_tokens=128,
            timeout_seconds=2,
        )
    ]
    assert wire.closed and len(requests) == 1 and result[-1]["stop_reason"] == "end_turn"
    with pytest.raises(P.ProviderError, match="provider_configuration_error"):
        _ = [
            e
            async for e in P.stream(
                [{"role": "user", "content": "合成"}],
                system="",
                tools=[],
                model_cfg={**CFG, "protocol": "automatic"},
                max_tokens=128,
                timeout_seconds=2,
            )
        ]
    assert len(requests) == 1


def test_protocol_configuration_is_accurate_and_search_requires_explicit_support():
    assert P.configuration_status(CFG)["endpoint_host"] == "openai"
    assert P.configuration_status(CFG)["protocol"] == "openai_responses"
    assert P.configuration_status({**CFG, "model": ""})["configured"] is False
    assert P.configuration_status({**CFG, "protocol": "anthropic_messages"})["configured"] is False
    assert P.supports_search(CFG) is True
    assert P.supports_search({**CFG, "model": ""}) is False
    assert (
        P.supports_search(
            {**CFG, "base_url": "https://api.minimax.io/anthropic/v1", "protocol": "anthropic_messages"}
        )
        is False
    )
    assert CFG["api_key"] not in json.dumps(P.configuration_status(CFG))
