"""Real tiny CLI/reap ownership and current-turn SSE persistence, entirely offline."""

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from server import capabilities, chat
from server.database import get_db
from server.research_context import build_research_context
from server.services import claude_runner as runner
from server.work_lifecycle import Manager


@pytest.fixture
def manager():
    value = Manager()
    yield value
    value._pool.shutdown(wait=True, cancel_futures=False)


@pytest.fixture
def conversation(test_user):
    with get_db() as db:
        db.execute(
            "INSERT INTO conversations(id,username) VALUES('lifecycle-chat',?)", [test_user["username"]]
        )
    return test_user["username"]


class Request:
    def __init__(self, manager):
        self.scope = {"app": SimpleNamespace(state=SimpleNamespace(work_manager=manager))}

    async def json(self):
        return {"question": "public synthetic current turn", "conv_id": "lifecycle-chat", "skill": ""}


def messages():
    with get_db() as db:
        return db.execute(
            "SELECT content FROM messages WHERE conv_id='lifecycle-chat' AND role='assistant' ORDER BY id"
        ).fetchall()


def patch_chat(monkeypatch, user, engine):
    monkeypatch.setattr(chat, "require_auth", lambda request: user)
    monkeypatch.setattr(chat, "run_claude_stream", engine)
    monkeypatch.setattr(chat, "persist_graph", lambda *args: None)


async def wait_empty(manager):
    for _ in range(200):
        if not sum(manager.observe()["leases"].values()):
            return
        await asyncio.sleep(0.005)
    pytest.fail("actual lifecycle completion did not converge")


async def test_real_cli_stays_registered_through_reap_and_token_revocation(
    manager, conversation, monkeypatch, tmp_path
):
    real_spawn = asyncio.create_subprocess_exec
    waited, release = asyncio.Event(), asyncio.Event()
    launched = []
    code = (
        "import json,sys;print(json.dumps({'type':'assistant','message':{'content':"
        "[{'type':'text','text':'synthetic'}]}}),flush=True);sys.stdin.buffer.read(1)"
    )

    async def spawn(*argv, **kwargs):
        # Fixed tiny interpreter child, with the actual runner pipe/env boundary.
        process = await real_spawn(sys.executable, "-u", "-c", code, **kwargs)
        original_wait = process.wait

        async def delayed_wait():
            result = await original_wait()
            waited.set()
            await release.wait()
            return result

        process.wait = delayed_wait
        launched.append(process)
        return process

    async def proxy():
        return None

    monkeypatch.setattr(runner, "get_proxy_config", proxy)
    monkeypatch.setattr(runner, "_resolve_skill_dir", lambda name: (tmp_path, [tmp_path]))
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", spawn)
    root = manager.admit("request")
    stream = runner.run_claude_stream(
        "public synthetic",
        user=conversation,
        conv_id="lifecycle-chat",
        model_cfg={"model": "synthetic", "api_key": "synthetic"},
        research_context=build_research_context(run_id="tiny-cli-run"),
        parent_lease=root,
    )
    try:
        assert (await anext(stream))["type"] == "assistant"
        root.release()
        assert manager.observe()["pending_processes"] == 1
        assert "tiny-cli-run" in capabilities._run_bindings
        manager.begin_drain(manager.ticket())
        closing = asyncio.create_task(stream.aclose())
        await asyncio.wait_for(waited.wait(), 2)
        assert manager.observe()["pending_processes"] == 1
        assert not await manager.wait_drained(timeout=0.01)
        release.set()
        await asyncio.wait_for(closing, 2)
        assert await manager.wait_drained(timeout=2)
        assert "tiny-cli-run" not in capabilities._run_bindings
        assert "lifecycle-chat" not in runner._active_procs
        manager.close_development()
    finally:
        release.set()
        await stream.aclose()
        for process in launched:
            if process.returncode is None:
                process.kill()
            await process.wait()


async def test_disconnect_saves_partial_assistant_and_one_revision(manager, conversation, monkeypatch):
    closed = []

    async def engine(*args, **kwargs):
        try:
            yield {"type": "assistant", "message": {"content": [{"type": "text", "text": "partial"}]}}
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    patch_chat(monkeypatch, conversation, engine)
    response = await chat.chat_stream(Request(manager))
    stream = response.body_iterator
    await anext(stream)
    await anext(stream)
    assert '"text": "partial"' in await anext(stream)
    await stream.aclose()
    await wait_empty(manager)
    assert closed == [True]
    saved = messages()
    assert len(saved) == 1
    assert json.loads(saved[0]["content"])["text"] == "partial"
    with get_db() as db:
        rows = db.execute("SELECT payload FROM research_revisions").fetchall()
    assert len(rows) == 1 and json.loads(rows[0][0])["narrative"] == "partial"
    assert json.loads(saved[0]["content"])["research"]["status"] == "cancelled"


async def test_final_research_event_already_has_assistant_and_disconnect_cannot_duplicate(
    manager, conversation, monkeypatch
):
    async def engine(*args, **kwargs):
        yield {"type": "assistant", "message": {"content": [{"type": "text", "text": "complete"}]}}
        yield {"type": "result", "is_error": False}

    patch_chat(monkeypatch, conversation, engine)
    response = await chat.chat_stream(Request(manager))
    stream = response.body_iterator
    async for event in stream:
        if '"revision_id"' in event:
            assert len(messages()) == 1  # The old finalize/yield/INSERT gap is gone.
            break
    await stream.aclose()
    await wait_empty(manager)
    assert len(messages()) == 1
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM research_revisions").fetchone()[0] == 1
    assert json.loads(messages()[0]["content"])["text"] == "complete"


async def test_chat_terminal_message_failure_blocks_without_second_insert(manager, conversation, monkeypatch):
    async def engine(*args, **kwargs):
        yield {"type": "assistant", "message": {"content": [{"type": "text", "text": "retained"}]}}
        yield {"type": "result", "is_error": False}

    patch_chat(monkeypatch, conversation, engine)
    with get_db() as db:
        db.execute(
            "CREATE TRIGGER reject_assistant BEFORE INSERT ON messages WHEN NEW.role='assistant' "
            "BEGIN SELECT RAISE(FAIL,'synthetic persistence failure'); END"
        )
    response = await chat.chat_stream(Request(manager))
    with pytest.raises(Exception):
        async for _ in response.body_iterator:
            pass
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert manager.observe()["persist_blocked"] == manager.observe()["blocked"] == 1
    assert not messages()


async def test_chat_done_and_history_do_not_wait_for_real_background_thread(
    manager, conversation, monkeypatch
):
    import threading

    started, release = threading.Event(), threading.Event()
    child_task = []

    def blocking():
        started.set()
        release.wait(timeout=5)

    async def engine(*args, parent_lease, **kwargs):
        child_task.append(
            asyncio.create_task(manager.run_blocking("auxiliary", blocking, parent=parent_lease))
        )
        while not started.is_set():
            await asyncio.sleep(0.005)
        yield {"type": "assistant", "message": {"content": [{"type": "text", "text": "accepted"}]}}
        yield {"type": "result", "is_error": False}

    patch_chat(monkeypatch, conversation, engine)
    try:
        response = await chat.chat_stream(Request(manager))
        events = [event async for event in response.body_iterator]
        assert events[-1] == "data: [DONE]\n\n"
        assert len(messages()) == 1
        manager.begin_drain(manager.ticket())
        assert manager.observe()["pending_cf"] == 1 and manager.observe()["persist_pending"] == 1
        assert not await manager.wait_drained(timeout=0.01)
        release.set()
        await asyncio.gather(*child_task)
        assert await manager.wait_drained(timeout=2)
        assert len(messages()) == 1
        manager.close_development()
    finally:
        release.set()
        await asyncio.gather(*child_task, return_exceptions=True)


async def test_history_read_cancellation_retains_actual_file_thread(manager, monkeypatch, tmp_path):
    import threading
    from pathlib import Path

    started, release = threading.Event(), threading.Event()
    path = tmp_path / "session.jsonl"
    path.write_text(json.dumps({"type": "user", "message": {"content": "synthetic"}}) + "\n")
    original_open = Path.open

    class Reader:
        def __init__(self, source):
            self.source = source

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.source.close()

        def __iter__(self):
            started.set()
            release.wait(timeout=5)
            yield from self.source

    def controlled_open(candidate, *args, **kwargs):
        source = original_open(candidate, *args, **kwargs)
        return Reader(source) if candidate == path else source

    monkeypatch.setattr(chat, "CLAUDE_PROJECTS", tmp_path)
    monkeypatch.setattr(Path, "open", controlled_open)
    root = manager.admit("request")
    with root:
        task = asyncio.create_task(chat._read_jsonl_messages("session", "."))
        await asyncio.sleep(0)
    try:
        for _ in range(200):
            if started.is_set():
                break
            await asyncio.sleep(0.005)
        assert started.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        manager.begin_drain(manager.ticket())
        assert manager.observe()["pending_cf"] == 1
        assert not await manager.wait_drained(timeout=0.01)
        release.set()
        assert await manager.wait_drained(timeout=2)
        manager.close_development()
    finally:
        release.set()
