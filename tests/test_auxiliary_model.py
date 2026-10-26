"""Synthetic Messages streams only; never a CLI, live model or production DB."""

import asyncio
import json
import os
import subprocess

import httpx
import pytest

from server.agent import provider
from server.work_lifecycle import Manager


@pytest.fixture
def helper(temp_data_dir):
    # The standard fixture binds config/SQLite to a newly created test directory
    # before auxiliary_model imports config. No inherited runtime data is read.
    from server.services import auxiliary_model

    return auxiliary_model


@pytest.fixture(autouse=True)
def no_processes(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Auxiliary tasks must never start a process")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)


@pytest.fixture
def configured(helper, monkeypatch):
    monkeypatch.setattr(helper.config, "DEFAULT_MODEL", "deepseek")
    monkeypatch.setattr(
        helper.config,
        "MODELS",
        {
            "deepseek": {
                "model": "synthetic-model",
                "base_url": "https://api.deepseek.com/anthropic",
                "api_key": "synthetic-provider-key",
            },
            "minimax": {
                "model": "synthetic-other",
                "base_url": "https://api.minimax.io/anthropic/v1/",
                "api_key": "synthetic-other-key",
            },
            "missing-key": {
                "model": "synthetic",
                "base_url": "https://api.deepseek.com/anthropic",
                "api_key": "",
            },
        },
    )
    for name in ("HTTPS_PROXY", "ANTHROPIC_AUTH_TOKEN", "PRISM_CAPABILITY_TOKEN", "NODE_OPTIONS"):
        monkeypatch.setenv(name, "synthetic-must-not-inherit")
    manager = Manager(max_workers=1)
    monkeypatch.setattr(helper, "get_manager", lambda: manager)
    # Each model test must install its explicit MockTransport before HTTP.
    monkeypatch.setattr(provider.httpx, "AsyncClient", lambda **kw: pytest.fail("Unexpected HTTP transport"))
    yield helper, manager
    manager._pool.shutdown(wait=True, cancel_futures=False)


# Capture before individual fixtures replace it; this is still the real HTTP
# client, always constructed with a local MockTransport in these tests.
HTTP_CLIENT = httpx.AsyncClient


class Wire(httpx.AsyncByteStream):
    def __init__(self, chunks, *, wait=False, close_gate=None):
        self.chunks = chunks
        self.wait = wait
        self.closed = False
        self.waiting = asyncio.Event()
        self.closing = asyncio.Event()
        self.close_gate = close_gate

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.wait:
            self.waiting.set()
            await asyncio.Event().wait()

    async def aclose(self):
        self.closing.set()
        if self.close_gate is not None:
            await self.close_gate.wait()
        self.closed = True


def frame(kind, **fields):
    return (
        f"event: {kind}\ndata: " + json.dumps({"type": kind, **fields}, ensure_ascii=False) + "\n\n"
    ).encode()


def start():
    return frame("message_start", message={"role": "assistant", "content": [], "usage": {"input_tokens": 1}})


def text(text, *, initial="", terminal=True, reason="end_turn"):
    data = start() + frame("content_block_start", index=0, content_block={"type": "text", "text": initial})
    data += frame("content_block_delta", index=0, delta={"type": "text_delta", "text": text})
    if terminal:
        data += frame("content_block_stop", index=0)
        data += frame("message_delta", delta={"stop_reason": reason}, usage={"output_tokens": 1})
        data += frame("message_stop")
    return data


def connect(monkeypatch, wire, *, status=200):
    requests, options = [], []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, headers={"content-type": "text/event-stream"}, stream=wire)

    def client(**kwargs):
        options.append(dict(kwargs))
        kwargs["transport"] = httpx.MockTransport(handle)
        return HTTP_CLIENT(**kwargs)

    monkeypatch.setattr(provider.httpx, "AsyncClient", client)
    return requests, options


@pytest.mark.parametrize(
    "model_id,endpoint",
    [
        (None, "https://api.deepseek.com/anthropic/v1/messages"),
        ("minimax", "https://api.minimax.io/anthropic/v1/messages"),
    ],
)
async def test_direct_provider_final_text_no_tools_no_inherited_credentials(
    configured, monkeypatch, model_id, endpoint
):
    helper, manager = configured
    before = dict(os.environ)
    wire = Wire([text("中文", initial=" final ")])
    requests, options = connect(monkeypatch, wire)
    result = await helper.run_text_async("synthetic private input", model_id=model_id)
    assert result == "final 中文" and wire.closed and len(requests) == 1
    assert str(requests[0].url) == endpoint
    payload = json.loads(requests[0].content)
    assert payload["tools"] == [] and payload["messages"] == [
        {"role": "user", "content": "synthetic private input"}
    ]
    assert payload["stream"] is True
    assert "synthetic-provider-key" not in requests[0].content.decode()
    assert options[0]["trust_env"] is False and options[0]["follow_redirects"] is False
    assert os.environ == before and sum(manager.observe()["leases"].values()) == 0


def test_sync_worker_uses_same_direct_stream(configured, monkeypatch):
    helper, manager = configured
    wire = Wire([text('{"kind":"plot","dsl":{"price":"close"}}')])
    requests, _ = connect(monkeypatch, wire)
    assert json.loads(helper.run_text("synthetic", output_format="json"))["kind"] == "plot"
    assert len(requests) == 1 and wire.closed and sum(manager.observe()["leases"].values()) == 0


async def test_sync_refuses_running_loop_before_transport_or_admission(configured):
    helper, manager = configured
    with pytest.raises(helper.AuxiliaryModelUnavailable, match="事件循环"):
        helper.run_text("synthetic")
    assert sum(manager.observe()["leases"].values()) == 0


@pytest.mark.parametrize(
    "body", ["```json\n{}\n```", "{} trailing", '{"x":NaN}', '{"x":1e999}', '{"x":1,"x":2}', "[]", '"scalar"']
)
async def test_json_requires_exact_final_object(configured, monkeypatch, body):
    helper, manager = configured
    wire = Wire([text(body)])
    requests, _ = connect(monkeypatch, wire)
    with pytest.raises(helper.AuxiliaryModelUnavailable):
        await helper.run_text_async("synthetic", output_format="json")
    assert wire.closed and len(requests) == 1 and sum(manager.observe()["leases"].values()) == 0


@pytest.mark.parametrize(
    "body", ["{}", '{"result":"plain JSON without CLI unwrapping"}', '{"中文":[1,true,null]}']
)
async def test_json_returns_only_valid_final_text(configured, monkeypatch, body):
    helper, _ = configured
    wire = Wire([text(body)])
    connect(monkeypatch, wire)
    assert await helper.run_text_async("synthetic", output_format="json") == body


@pytest.mark.parametrize("model_id", ["missing-key", "unknown", 4])
async def test_unknown_or_unconfigured_model_fails_before_http(configured, model_id):
    helper, manager = configured
    with pytest.raises(helper.AuxiliaryModelUnavailable):
        await helper.run_text_async("synthetic", model_id=model_id)
    assert sum(manager.observe()["leases"].values()) == 0


@pytest.mark.parametrize(
    "field,value",
    [("base_url", "https://unknown.invalid/anthropic"), ("api_key", "bad key"), ("model", "bad\nmodel")],
)
async def test_provider_configuration_fails_without_network_or_secret_echo(configured, field, value):
    helper, manager = configured
    helper.config.MODELS["deepseek"][field] = value
    with pytest.raises(helper.AuxiliaryModelUnavailable) as error:
        await helper.run_text_async("synthetic")
    assert value not in str(error.value) and sum(manager.observe()["leases"].values()) == 0


@pytest.mark.parametrize("status", [401, 429, 500])
async def test_http_error_safe_one_request_no_retry(configured, monkeypatch, status):
    helper, _ = configured
    wire = Wire([b"synthetic-provider-key and private upstream detail"])
    requests, _ = connect(monkeypatch, wire, status=status)
    with pytest.raises(helper.AuxiliaryModelUnavailable) as error:
        await helper.run_text_async("synthetic")
    assert len(requests) == 1 and wire.closed and "private" not in str(error.value)


@pytest.mark.parametrize(
    "body", ["", "synthetic-provider-key", "中" * 85_334], ids=["empty", "credential", "overflow"]
)
async def test_empty_credential_or_byte_overflow_never_returns_partial(configured, monkeypatch, body):
    helper, _ = configured
    wire = Wire([text(body)])
    connect(monkeypatch, wire)
    with pytest.raises(helper.AuxiliaryModelUnavailable):
        await helper.run_text_async("synthetic")
    assert wire.closed


async def test_bound_counts_streaming_utf8_bytes_before_final(configured, monkeypatch):
    helper, _ = configured
    data = start() + frame("content_block_start", index=0, content_block={"type": "text", "text": ""})
    chunks = [data] + [
        frame("content_block_delta", index=0, delta={"type": "text_delta", "text": "中" * 50_000})
    ] * 2
    wire = Wire(chunks, wait=True)
    connect(monkeypatch, wire)
    with pytest.raises(helper.AuxiliaryModelUnavailable):
        await helper.run_text_async("synthetic")
    assert wire.closed and not wire.waiting.is_set()


@pytest.mark.parametrize("kind", ["tool_use", "server_tool_use"])
async def test_tools_rejected_even_if_upstream_ignores_empty_tools(configured, monkeypatch, kind):
    helper, _ = configured
    data = start() + frame(
        "content_block_start",
        index=0,
        content_block={"type": kind, "id": "synthetic-call", "name": "forbidden", "input": {}},
    )
    data += frame("content_block_stop", index=0)
    data += frame("message_delta", delta={"stop_reason": "tool_use"}, usage={"output_tokens": 1}) + frame(
        "message_stop"
    )
    wire = Wire([data])
    requests, _ = connect(monkeypatch, wire)
    with pytest.raises(helper.AuxiliaryModelUnavailable):
        await helper.run_text_async("synthetic")
    assert wire.closed and len(requests) == 1


@pytest.mark.parametrize("reason", ["max_tokens", "stop_sequence"])
async def test_nonfinal_terminal_does_not_return_partial_text(configured, monkeypatch, reason):
    helper, _ = configured
    wire = Wire([text("synthetic partial", reason=reason)])
    connect(monkeypatch, wire)
    with pytest.raises(helper.AuxiliaryModelUnavailable):
        await helper.run_text_async("synthetic")
    assert wire.closed


async def test_total_timeout_closes_transport_then_releases_lease(configured, monkeypatch):
    helper, manager = configured
    wire = Wire([text("partial", terminal=False)], wait=True)
    requests, _ = connect(monkeypatch, wire)
    with pytest.raises(helper.AuxiliaryModelUnavailable):
        await helper.run_text_async("synthetic", timeout=0.02)
    assert wire.closed and len(requests) == 1 and sum(manager.observe()["leases"].values()) == 0


async def test_repeated_cancel_cannot_release_fenced_lease_before_actual_close(configured, monkeypatch):
    helper, manager = configured
    release_close = asyncio.Event()
    wire = Wire([text("partial", terminal=False)], wait=True, close_gate=release_close)
    requests, _ = connect(monkeypatch, wire)
    task = asyncio.create_task(helper.run_text_async("synthetic"))
    await asyncio.wait_for(wire.waiting.wait(), 1)
    assert manager.observe()["leases"]["auxiliary"] == 1
    manager.begin_drain(manager.ticket())
    task.cancel()
    await asyncio.wait_for(wire.closing.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not wire.closed and not task.done() and manager.observe()["leases"]["auxiliary"] == 1
    assert not await manager.wait_drained(timeout=0.01)
    release_close.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert wire.closed and len(requests) == 1 and sum(manager.observe()["leases"].values()) == 0


async def test_fence_prevents_http_admission(configured):
    helper, manager = configured
    manager.begin_drain(manager.ticket())
    with pytest.raises(helper.AuxiliaryModelUnavailable):
        await helper.run_text_async("synthetic")
    assert sum(manager.observe()["leases"].values()) == 0


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan"), 601])
async def test_invalid_deadline_fails_before_http(configured, timeout):
    helper, _ = configured
    with pytest.raises(helper.AuxiliaryModelUnavailable):
        await helper.run_text_async("synthetic", timeout=timeout)


def test_indicator_complex_translation_calls_direct_provider(configured, monkeypatch):
    helper, _ = configured
    from server.indicators import translator

    wire = Wire([text(json.dumps({"kind": "plot", "name": "Close", "dsl": {"price": "close"}}))])
    requests, _ = connect(monkeypatch, wire)
    result = translator.translate("unrecognized custom indicator request")
    assert result["source"] == "llm" and result["dsl"] == {"price": "close"}
    assert wire.closed and len(requests) == 1


def test_indicator_rule_fastpath_never_starts_model(helper, monkeypatch):
    from server.indicators import translator

    def forbidden(*args, **kwargs):
        pytest.fail("Known rules must remain local")

    monkeypatch.setattr(helper, "run_text", forbidden)
    assert translator.translate("MACD金叉")["source"] == "rule"


async def test_explanation_cache_is_owner_and_context_scoped(client, auth_cookies, monkeypatch, helper):
    from server import main
    from server.auth import create_session, hash_password
    from server.database import create_user

    main._term_cache.clear()
    calls = []

    async def fake(prompt, **kwargs):
        calls.append(prompt)
        return "private explanation " + str(len(calls))

    monkeypatch.setattr(helper, "run_text_async", fake)
    create_user("other-owner", hash_password("test-password"))
    other = {"token": create_session("other-owner")}
    body = {"term": "same-term", "context": "private context A"}
    first = await client.post("/api/explain-term", json=body, cookies=auth_cookies)
    same = await client.post("/api/explain-term", json=body, cookies=auth_cookies)
    foreign = await client.post("/api/explain-term", json=body, cookies=other)
    changed = await client.post(
        "/api/explain-term", json={**body, "context": "private context B"}, cookies=auth_cookies
    )
    assert first.json()["explanation"] == same.json()["explanation"]
    assert same.json()["cached"] is True
    assert foreign.json()["explanation"] != first.json()["explanation"]
    assert changed.json()["explanation"] != first.json()["explanation"] and len(calls) == 3
    main._term_cache.clear()


async def test_unconfirmed_analysis_start_never_echoes_provider_exception(
    client, auth_cookies, monkeypatch, caplog
):
    from server import main

    secret = "synthetic-provider-key-do-not-return"

    async def fail(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(main, "start_analysis", fail)
    response = await client.post("/api/analysis/start", cookies=auth_cookies, json={"ticker": "600519"})
    assert response.status_code == 500 and response.json()["code"] == "engine_unavailable"
    assert secret not in response.text and secret not in caplog.text


async def test_split_credential_never_returns_final_text(configured, monkeypatch):
    helper, _ = configured
    wire = Wire([text("-provider-key", initial="synthetic")])
    connect(monkeypatch, wire)
    with pytest.raises(helper.AuxiliaryModelUnavailable):
        await helper.run_text_async("synthetic")
    assert wire.closed


async def test_escaped_json_credential_is_not_returned(configured, monkeypatch):
    helper, _ = configured
    wire = Wire([text(r'{"value":"\u0073ynthetic-provider-key"}')])
    connect(monkeypatch, wire)
    with pytest.raises(helper.AuxiliaryModelUnavailable):
        await helper.run_text_async("synthetic", output_format="json")
    assert wire.closed


async def test_exact_utf8_output_byte_limit(configured, monkeypatch):
    helper, _ = configured
    data = start() + frame("content_block_start", index=0, content_block={"type": "text", "text": ""})
    parts = ["x" * 128_000, "x" * 128_000]
    for part in parts:
        data += frame("content_block_delta", index=0, delta={"type": "text_delta", "text": part})
    data += frame("content_block_stop", index=0)
    data += frame("message_delta", delta={"stop_reason": "end_turn"}, usage={"output_tokens": 1}) + frame(
        "message_stop"
    )
    wire = Wire([data])
    connect(monkeypatch, wire)
    assert len((await helper.run_text_async("synthetic")).encode()) == 256_000
    assert wire.closed


async def test_truncated_stream_never_returns_partial(configured, monkeypatch):
    helper, manager = configured
    wire = Wire([text("partial", terminal=False)])
    requests, _ = connect(monkeypatch, wire)
    with pytest.raises(helper.AuxiliaryModelUnavailable):
        await helper.run_text_async("synthetic")
    assert wire.closed and len(requests) == 1 and sum(manager.observe()["leases"].values()) == 0
