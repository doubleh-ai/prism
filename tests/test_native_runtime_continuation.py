"""Native runtime: max_tokens is a truncated turn, not a failed run."""

import asyncio
import copy
from types import SimpleNamespace

import pytest

from server.agent import runtime, store
from server.database import get_db
from server.work_lifecycle import Manager


@pytest.fixture
def engine(monkeypatch):
    user, conv = "runtime-continuation-owner", "runtime-continuation-conv"
    with get_db() as db:
        db.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", [user, "synthetic-only"])
        db.execute("INSERT INTO conversations(id,username) VALUES(?,?)", [conv, user])
    runtime.init_runtime_db()
    run = store.create_run(user, conv, "合成续写研究问题", "synthetic-model", "research")
    runtime.ensure_dialogue(user, run)
    manager = Manager()
    value = SimpleNamespace(
        user=user,
        conv=conv,
        run=run,
        manager=manager,
        service=runtime.NativeAgent(manager),
        model={
            "api_key": "synthetic-isolated-test",
            "base_url": "https://api.deepseek.com/anthropic",
            "model": "synthetic",
        },
    )

    async def forbidden(*args, **kwargs):
        raise AssertionError("A test must explicitly supply a finite model script")
        yield  # pragma: no cover -- this is an asynchronous iterator, never a real provider.

    monkeypatch.setattr(runtime, "model_stream", forbidden)
    yield value
    manager._pool.shutdown(wait=True, cancel_futures=False)


async def execute(engine):
    parent = engine.manager.admit("request")
    with parent:
        engine.service.start(engine.user, engine.run, engine.model, parent=parent)
    handle = engine.service.active[engine.run["run_id"]].handle
    outcome = await asyncio.wait_for(handle.wait(), 3)
    assert engine.manager.observe()["blocked"] == 0
    return outcome


def done(reason="end_turn", *, content=None):
    return {
        "type": "done",
        "stop_reason": reason,
        "usage_complete": True,
        "content": content if content is not None else [{"type": "text", "text": "合成答案"}],
    }


def events(engine):
    return store.list_events(engine.user, engine.run["run_id"], limit=200)


async def test_max_tokens_continues_same_run_then_completes(engine, monkeypatch):
    calls = []

    async def script(messages, **kwargs):
        calls.append(copy.deepcopy(messages))
        if len(calls) == 1:
            yield {"type": "text_delta", "text": "被截断的前半段"}
            yield done("max_tokens", content=[{"type": "text", "text": "被截断的前半段"}])
        else:
            yield {"type": "text_delta", "text": "，续写完成。"}
            yield done()

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)

    assert outcome.status == "completed" and outcome.error_code is None
    assert len(calls) == 2
    # The partial assistant turn is replayed to the model exactly once, not
    # duplicated and not dropped.
    assert calls[1][-1]["role"] == "assistant"
    assert calls[1][-1]["content"] == [{"type": "text", "text": "被截断的前半段"}]
    continuations = [e for e in events(engine) if e["type"] == "model_continuation"]
    assert len(continuations) == 1 and continuations[0]["ui"]["attempt"] == 1
    assert continuations[0]["status"] == "running"


async def test_repeated_max_tokens_is_bounded_and_then_output_incomplete(engine, monkeypatch):
    calls = []

    async def script(messages, **kwargs):
        calls.append(1)
        yield {"type": "text_delta", "text": "截断" + str(len(calls))}
        yield done("max_tokens", content=[{"type": "text", "text": "截断" + str(len(calls))}])

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)

    # The default budget allows 8 calls, but every call is again truncated at
    # max_tokens: the original call plus exactly 4 permitted continuations are
    # used, then the run stops as output_incomplete. Raising model_calls does
    # not lift the per-run continuation cap, so the run cannot loop forever.
    assert len(calls) == 5
    assert outcome.error_code == "output_incomplete"
    assert outcome.status != "completed"
    continuation_events = [e for e in events(engine) if e["type"] == "model_continuation"]
    assert [e["ui"]["attempt"] for e in continuation_events] == [1, 2, 3, 4]
    assert len([e for e in events(engine) if e["type"] == "model_started"]) == 5


def test_default_limits_fit_one_complete_report():
    # Production evidence on 2ab32d4: a real report used 6 calls x 2048 output
    # tokens and was still truncated on the 6th call (output_incomplete). The
    # defaults must fit a full report plus the bounded review/continuation work.
    limits = runtime.Limits()
    assert limits.model_calls == 8
    assert limits.output_tokens_per_call == 4096
    assert limits.token_continuations == 4
    assert limits.wall_seconds == 300


async def test_continuation_never_extends_the_model_call_budget(engine, monkeypatch):
    engine.service.limits = runtime.Limits(model_calls=2)
    calls = []

    async def script(messages, **kwargs):
        calls.append(1)
        yield {"type": "text_delta", "text": "截断" + str(len(calls))}
        yield done("max_tokens", content=[{"type": "text", "text": "截断" + str(len(calls))}])

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)

    assert len(calls) == 2
    assert outcome.error_code == "budget_exceeded" and outcome.limit_reason == "model_calls"
    # Both permitted calls were truncated turns; neither one bought an extra call.
    assert [e["ui"]["attempt"] for e in events(engine) if e["type"] == "model_continuation"] == [1, 2]
