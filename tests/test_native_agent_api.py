"""Owned execution, HTTP replay, and platform persistence, without any model/network."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from server import config, conversations
from server.agent import api, runtime, store
from server.database import get_db
from server.work_lifecycle import Manager


@pytest.fixture
def native(monkeypatch, test_user):
    runtime.init_runtime_db()
    manager = Manager()
    user = test_user["username"]
    with get_db() as db:
        db.execute("INSERT INTO conversations(id,username) VALUES('native-conv',?)", [user])
        db.execute("INSERT INTO users(username,password_hash) VALUES('other','synthetic')")
    monkeypatch.setattr(api, "require_auth", lambda request: request.user)
    monkeypatch.setattr(conversations, "require_auth", lambda request: request.user)
    monkeypatch.setattr(
        config,
        "MODELS",
        {
            "mock": {
                "api_key": "synthetic-only",
                "model": "mock",
                "base_url": "https://api.deepseek.com/anthropic",
            }
        },
    )
    monkeypatch.setattr(config, "DEFAULT_MODEL", "mock")

    def no_child(*args, **kwargs):
        pytest.fail("the native Agent must never start a CLI")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_child)

    class Request:
        def __init__(self, body=None, username=user):
            self.user = username
            self.body = body or {}
            self.scope = {"app": SimpleNamespace(state=SimpleNamespace(work_manager=manager))}

        async def json(self):
            return self.body

    yield manager, user, Request
    manager._pool.shutdown(wait=True, cancel_futures=False)


def body(**changes):
    return {
        "question": "合成公司的研究问题",
        "conv_id": "native-conv",
        "request_id": "fixed-input",
        **changes,
    }


async def drain(response):
    frames = []
    async for chunk in response.body_iterator:
        for line in chunk.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                frames.append(json.loads(line[6:]))
    return frames


async def wait_empty(manager):
    for _ in range(400):
        if not sum(manager.observe()["leases"].values()):
            return
        await asyncio.sleep(0.005)
    pytest.fail(str(manager.observe()))


async def test_two_step_tools_persist_and_duplicate_http_replays_without_new_model(native, monkeypatch):
    manager, user, Request = native
    calls = []

    async def model(messages, **kwargs):
        calls.append(json.loads(json.dumps(messages)))
        assert "当前服务器时间" in kwargs["system"]
        assert {"indicator_list", "research_read", "source_read", "knowledge_search"} <= {
            t["name"] for t in kwargs["tools"]
        }
        if len(calls) == 1:
            yield {"type": "tool_call", "id": "call_one", "name": "indicator_list", "arguments": {}}
            yield {
                "type": "done",
                "stop_reason": "tool_use",
                "content": [{"type": "tool_use", "id": "call_one", "name": "indicator_list", "input": {}}],
            }
        else:
            result = json.loads(messages[-1]["content"][0]["content"])
            assert result["ok"] and result["call"]["capability_id"] == "indicator_list"
            yield {"type": "text_delta", "text": "已读取平台指标。没有新公告证据，当前结论仍待核验。"}
            yield {
                "type": "done",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "公开合成回答"}],
            }

    monkeypatch.setattr(runtime, "model_stream", model)
    frames = await asyncio.wait_for(drain(await api.chat_stream(Request(body()))), 5)
    await wait_empty(manager)
    run = store.list_runs(user)[0]
    assert run["status"] == "completed" and run["research_status"] == "unreviewed", (run, calls, frames)
    assert len(calls) == 2
    assert [f["agent"]["seq"] for f in frames] == list(range(1, run["last_seq"] + 1))
    assert frames[0]["agent"]["status"] != "completed"
    assert frames[-1]["agent"]["status"] == "completed"
    assert any(f.get("platform_call") for f in frames)
    replay = await drain(await api.chat_stream(Request(body())))
    assert replay == frames and len(calls) == 2
    with pytest.raises(HTTPException) as conflict:
        await api.chat_stream(Request(body(question="changed")))
    assert conflict.value.status_code == 409
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM messages WHERE role='user'").fetchone()[0] == 1
        saved = json.loads(db.execute("SELECT content FROM messages WHERE role='assistant'").fetchone()[0])
        assert saved["native_agent"]["engine"] == "prism-native" and saved["report_files"] == []
        assert (
            db.execute("SELECT COUNT(*) FROM research_revisions WHERE run_id=?", [run["run_id"]]).fetchone()[
                0
            ]
            == 1
        )
        assert db.execute("SELECT COUNT(*) FROM saved_reports WHERE source='native-agent'").fetchone()[0] == 1


async def test_disconnect_only_closes_reader_refresh_shows_progress_and_get_replays(native, monkeypatch):
    manager, user, Request = native
    started, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def model(*args, **kwargs):
        nonlocal count
        count += 1
        yield {"type": "text_delta", "text": "已采集的合成研究进度。" * 60}
        started.set()
        await release.wait()
        yield {"type": "text_delta", "text": "最终回答仍保留证据缺口。"}
        yield {"type": "done", "stop_reason": "end_turn", "content": []}

    monkeypatch.setattr(runtime, "model_stream", model)
    response = await api.chat_stream(Request(body()))
    await asyncio.wait_for(started.wait(), 2)
    frame = await anext(response.body_iterator)
    assert "id: 1" in frame
    await response.body_iterator.aclose()
    run = store.list_runs(user)[0]
    assert run["status"] == "running" and manager.observe()["leases"]["agent"] == 1
    restored = conversations.get_conv("native-conv", Request())
    progress = json.loads(restored["messages"][-1]["content"])
    assert progress["text"] and progress["native_agent"]["status"] == "running"
    with pytest.raises(HTTPException) as conflict:
        await api.chat_stream(Request(body(request_id="other-request")))
    assert conflict.value.status_code == 409
    release.set()
    replay = await asyncio.wait_for(drain(api.stream_run(user, run["run_id"], after_seq=1)), 5)
    assert replay[0]["agent"]["seq"] == 2 and replay[-1]["agent"]["status"] == "completed"
    assert count == 1
    await wait_empty(manager)


async def test_cancel_closes_transport_before_terminal_and_preserves_answer(native, monkeypatch):
    manager, user, Request = native
    started, closed = asyncio.Event(), asyncio.Event()

    async def model(*args, **kwargs):
        try:
            yield {"type": "text_delta", "text": "已经收到的部分回答"}
            started.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(runtime, "model_stream", model)
    response = await api.chat_stream(Request(body()))
    await asyncio.wait_for(started.wait(), 2)
    run = store.list_runs(user)[0]
    ack = await api.cancel(Request({"conv_id": "native-conv", "run_id": run["run_id"]}))
    assert ack["ok"] and ack["cancel_requested"] and not ack["stopped"]
    frames = await asyncio.wait_for(drain(response), 5)
    assert closed.is_set() and frames[-1]["agent"]["status"] == "cancelled"
    assert store.get_run(user, run["run_id"])["summary"] == "已经收到的部分回答"
    await wait_empty(manager)


async def test_deleted_conversation_stops_model_and_permanent_delete_purges_native_records(
    native, monkeypatch
):
    manager, user, Request = native
    started, closed = asyncio.Event(), asyncio.Event()

    async def model(*args, **kwargs):
        try:
            started.set()
            await asyncio.Event().wait()
            yield {}
        finally:
            closed.set()

    monkeypatch.setattr(runtime, "model_stream", model)
    response = await api.chat_stream(Request(body()))
    await asyncio.wait_for(started.wait(), 2)
    run = store.list_runs(user)[0]
    with pytest.raises(HTTPException) as denied:
        api.stream_run("other", run["run_id"])
    assert denied.value.status_code == 404
    conversations.delete_conv("native-conv", Request())
    conversations.permanent_delete_conv("native-conv", Request())
    await asyncio.wait_for(drain(response), 3)
    await wait_empty(manager)
    assert closed.is_set()
    with get_db() as db:
        for table in ("native_agent_runs", "native_agent_events", "native_agent_dialogues", "messages"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


async def test_default_legacy_chat_alias_routes_to_native_and_no_respond_bridge(native, monkeypatch):
    _manager, _user, Request = native
    from server import main

    seen = []

    async def route(request):
        seen.append(await request.json())
        return "native"

    monkeypatch.setattr(main, "native_chat_stream", route)
    assert await main.api_chat_stream(Request(body())) == "native"
    assert seen == [body()]


async def test_actual_http_auth_csrf_catalog_and_stream_use_native_engine(native, monkeypatch):
    manager, user, _Request = native
    from server import auth, main

    monkeypatch.setattr(api, "require_auth", auth.require_auth)
    monkeypatch.setattr(main.app.state, "work_manager", manager, raising=False)
    count = 0

    async def model(*args, **kwargs):
        nonlocal count
        count += 1
        yield {"type": "text_delta", "text": "合成 HTTP 验收回答，资料仍待核验。"}
        yield {"type": "done", "stop_reason": "end_turn", "content": []}

    monkeypatch.setattr(runtime, "model_stream", model)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/agent/workflows")).status_code == 401
        client.cookies.set("token", auth.create_session(user))
        assert {r["workflow"] for r in (await client.get("/api/agent/workflows")).json()} == {
            "research",
            "industry",
            "company",
            "futures",
        }
        catalog = (await client.get("/api/agent/tools")).json()["capabilities"]
        assert "source_read" in {row["id"] for row in catalog}
        assert "analysis_start" not in {row["id"] for row in catalog}
        assert (await client.post("/api/agent/chat/stream", json=body())).status_code == 403
        result = await client.post(
            "/api/agent/chat/stream", json=body(), headers={"X-Requested-With": "XMLHttpRequest"}
        )
        assert (
            result.status_code == 200
            and "[DONE]" in result.text
            and '"engine": "prism-native"' in result.text
        )
        assert count == 1
        run = store.list_runs(user)[0]
        replay = await client.get(f"/api/agent/runs/{run['run_id']}/stream?after_seq={run['last_seq']}")
        assert replay.text == "data: [DONE]\n\n" and count == 1
        assert (await client.get(f"/api/agent/runs/{run['run_id']}/stream?after_seq=9999")).status_code == 422
    await wait_empty(manager)


def test_refresh_pending_history_preserves_tool_navigation_and_errors(native):
    _manager, user, Request = native
    run = store.create_run(user, "native-conv", "合成研究", "mock", "research")
    runtime.ensure_dialogue(user, run)
    call = {"call_id": "synthetic-call", "status": "completed", "target": {"view": "indicators"}}
    store.append_event(user, run["run_id"], {"type": "tool_completed", "ui": {"platform_call": call}})
    store.append_event(user, run["run_id"], {"type": "run_error", "ui": {"error": "合成资料暂不可用"}})
    history = conversations.get_conv("native-conv", Request())
    saved = json.loads(history["messages"][-1]["content"])
    assert {"t": "platform_call", "call": call} in saved["chunks"]
    assert "合成资料暂不可用" in saved["text"]
    assert saved["native_agent"]["run_id"] == run["run_id"]


async def test_real_protocol_adapter_tool_roundtrip_to_saved_answer_offline(native, monkeypatch):
    manager, user, Request = native
    from server.agent.provider import AnthropicMessagesProvider

    requests = []

    def frame(kind, **fields):
        return ("event: " + kind + "\ndata: " + json.dumps({"type": kind, **fields}) + "\n\n").encode()

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.headers["x-api-key"] == "synthetic-only"
        assert b"synthetic-only" not in request.content
        wire = frame(
            "message_start",
            message={"role": "assistant", "content": [], "usage": {"input_tokens": 12, "output_tokens": 0}},
        )
        if len(requests) == 1:
            wire += frame(
                "content_block_start",
                index=0,
                content_block={"type": "tool_use", "id": "read_ledger", "name": "research_read", "input": {}},
            )
            wire += frame(
                "content_block_delta", index=0, delta={"type": "input_json_delta", "partial_json": "{}"}
            )
            reason = "tool_use"
        else:
            tool_reply = json.loads(payload["messages"][-1]["content"][0]["content"])
            assert tool_reply["ok"] and tool_reply["data"]["run"]["conv_id"] == "native-conv"
            wire += frame("content_block_start", index=0, content_block={"type": "text", "text": ""})
            wire += frame(
                "content_block_delta",
                index=0,
                delta={"type": "text_delta", "text": "原生协议联调已读取当前研究，暂无外部证据。"},
            )
            reason = "end_turn"
        wire += frame("content_block_stop", index=0)
        wire += frame("message_delta", delta={"stop_reason": reason}, usage={"output_tokens": 15})
        wire += frame("message_stop")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire)

    adapter = AnthropicMessagesProvider(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(runtime, "model_stream", adapter.stream)
    frames = await asyncio.wait_for(drain(await api.chat_stream(Request(body()))), 5)
    await wait_empty(manager)
    run = store.list_runs(user)[0]
    assert run["status"] == "completed", (run, frames)
    assert len(requests) == 2 and "原生协议联调" in run["summary"]
    completed = [
        event for event in store.list_events(user, run["run_id"]) if event["type"] == "model_completed"
    ]
    assert completed[-1]["usage"]["input_tokens"] == 24
    assert completed[-1]["usage"]["output_tokens"] == 30
    assert completed[-1]["usage"]["usage_complete"] is True


async def test_failed_dialogue_initialization_does_not_strand_admission(native, monkeypatch):
    manager, user, Request = native

    def unavailable(*args):
        raise HTTPException(409, "旧历史暂无法核对")

    monkeypatch.setattr(api, "ensure_dialogue", unavailable)
    with pytest.raises(HTTPException) as error:
        await api.chat_stream(Request(body()))
    assert error.value.status_code == 409
    failed = store.list_runs(user)[0]
    assert failed["status"] == "failed"
    assert store.list_events(user, failed["run_id"])[-1]["type"] == "run_finished"
    retry = store.create_run(
        user, "native-conv", "different input", "mock", "research", exclusive_conversation=True
    )
    assert retry["status"] == "queued"
    await wait_empty(manager)


async def test_failed_initialization_does_not_finish_another_executor(native, monkeypatch):
    manager, user, Request = native

    def claimed_elsewhere(_user, run):
        assert store.claim_run(user, run["run_id"], "other-owned-executor")
        raise HTTPException(409, "synthetic race")

    monkeypatch.setattr(api, "ensure_dialogue", claimed_elsewhere)
    with pytest.raises(HTTPException):
        await api.chat_stream(Request(body()))
    assert store.list_runs(user)[0]["status"] == "running"
    await wait_empty(manager)


async def test_owner_removed_during_initialization_retires_unstarted_run(native, monkeypatch):
    manager, user, Request = native

    def removed_owner(*args):
        with get_db() as db:
            db.execute("UPDATE conversations SET deleted_at=1 WHERE id='native-conv'")
        raise HTTPException(404, "removed")

    monkeypatch.setattr(api, "ensure_dialogue", removed_owner)
    with pytest.raises(HTTPException):
        await api.chat_stream(Request(body()))
    with get_db() as db:
        assert (
            db.execute("SELECT status FROM native_agent_runs WHERE username=?", [user]).fetchone()[0]
            == "cancelled"
        )
    await wait_empty(manager)
