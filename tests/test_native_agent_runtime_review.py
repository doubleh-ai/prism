"""Exact native runtime race/budget regressions on the isolated test database."""

import asyncio
import copy
import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from server.agent import runtime, store
from server.database import get_db
from server.work_lifecycle import Manager


@pytest.fixture
def engine(monkeypatch):
    user, conv = "runtime-review-owner", "runtime-review-conv"
    with get_db() as db:
        db.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", [user, "synthetic-only"])
        db.execute("INSERT INTO conversations(id,username) VALUES(?,?)", [conv, user])
    runtime.init_runtime_db()
    run = store.create_run(user, conv, "合成研究问题", "synthetic-model", "research")
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


def events(engine):
    return store.list_events(engine.user, engine.run["run_id"], limit=200)


def last_usage(engine):
    return [e["usage"] for e in events(engine) if e["type"] == "model_usage"][-1]


def done(reason="end_turn", *, complete=True, content=None):
    return {
        "type": "done",
        "stop_reason": reason,
        "usage_complete": complete,
        "content": content if content is not None else [{"type": "text", "text": "合成答案"}],
    }


def test_history_spends_budget_on_latest_turns(engine):
    with get_db() as db:
        for prefix in ("old", "middle", "latest"):
            for role in ("user", "assistant"):
                text = prefix + "-" + role + ":" + ("x" * 16000 if prefix != "latest" else "最新内容")
                db.execute(
                    "INSERT INTO messages(conv_id,role,content) VALUES(?,?,?)", [engine.conv, role, text]
                )
        before = db.execute("SELECT MAX(id) FROM messages").fetchone()[0]
    history = runtime._history(engine.user, engine.conv, before)
    assert history[0]["role"] == "user"
    assert history[-2]["content"].startswith("latest-user:")
    assert history[-1]["content"].startswith("latest-assistant:")
    assert not any(row["content"].startswith("old-") for row in history)
    assert sum(len(row["content"]) for row in history) <= 40000


@pytest.mark.parametrize(
    "payload",
    [
        [{"type": "thinking", "thinking": "PRIVATE_THINKING_MARKER"}, {"type": "text", "text": "公开答案"}],
        {
            "content": [
                {"type": "reasoning", "text": "PRIVATE_THINKING_MARKER"},
                {
                    "type": "text",
                    "text": {
                        "content": [
                            {"type": "thinking", "thinking": "PRIVATE_THINKING_MARKER"},
                            {"type": "text", "text": "公开答案"},
                        ]
                    },
                },
            ]
        },
    ],
)
def test_assistant_encoded_blocks_never_reenter_private_history(engine, payload):
    with get_db() as db:
        db.execute(
            "INSERT INTO messages(conv_id,role,content) VALUES(?,'assistant',?)",
            [engine.conv, json.dumps(payload)],
        )
        before = db.execute("SELECT MAX(id) FROM messages").fetchone()[0]
    history = runtime._history(engine.user, engine.conv, before)
    assert "PRIVATE_THINKING_MARKER" not in json.dumps(history)
    assert "公开答案" in json.dumps(history, ensure_ascii=False)


async def test_cancel_winning_immediately_before_claim_finishes_queue(engine, monkeypatch):
    original = store.claim_run

    def cancel_then_claim(user, run_id, executor):
        store.request_cancel(user, run_id)
        return original(user, run_id, executor)

    monkeypatch.setattr(store, "claim_run", cancel_then_claim)
    outcome = await execute(engine)
    run = store.get_run(engine.user, engine.run["run_id"])
    assert outcome.status == run["status"] == "cancelled"
    assert not outcome.observer_only
    assert events(engine)[-1]["type"] == "run_finished"
    assert not any(e["type"] == "model_started" for e in events(engine))


async def test_lost_claim_to_other_executor_is_not_cancelled(engine):
    assert store.claim_run(engine.user, engine.run["run_id"], "another-executor")
    outcome = await execute(engine)
    assert outcome.observer_only
    assert store.get_run(engine.user, engine.run["run_id"])["status"] == "running"
    assert events(engine)[-1]["type"] == "run_started"


async def test_truncated_model_retains_observed_usage_as_unknown(engine, monkeypatch):
    async def script(*args, **kwargs):
        yield {"type": "usage", "input_tokens": 11}
        yield {"type": "text_delta", "text": "已收到的合成文本"}
        raise runtime.provider.ProviderError("provider_stream_truncated")

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)
    usage = last_usage(engine)
    assert outcome.status == "partial"
    assert usage["model_calls"] == 1 and usage["input_tokens"] == 11
    assert usage["output_tokens"] == 0 and usage["usage_complete"] is False
    assert usage["server_search_usage_complete"] is False
    assert "已收到" in store.get_run(engine.user, engine.run["run_id"])["summary"]


@pytest.mark.parametrize("report_usage", [False, True])
async def test_completed_call_requires_real_usage_fields_and_terminal_flag(engine, monkeypatch, report_usage):
    async def script(*args, **kwargs):
        if report_usage:
            yield {"type": "usage", "input_tokens": 5}
            yield {"type": "usage", "input_tokens": 8, "output_tokens": 3}
            yield {"type": "usage", "input_tokens": 8, "output_tokens": 4}
        yield {"type": "text_delta", "text": "合成公开答案"}
        yield done(complete=report_usage)

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)
    usage = last_usage(engine)
    assert outcome.status == "completed"
    assert usage["usage_complete"] is report_usage
    assert (usage["input_tokens"], usage["output_tokens"]) == ((8, 4) if report_usage else (0, 0))


async def test_search_pause_uses_ram_continuation_without_retry(engine, monkeypatch):
    calls = []

    async def script(messages, **kwargs):
        calls.append(copy.deepcopy(messages))
        yield {"type": "usage", "input_tokens": 3, "output_tokens": 2}
        if len(calls) == 1:
            yield {
                "type": "server_tool_use",
                "block": {
                    "type": "server_tool_use",
                    "id": "search-one",
                    "name": "web_search",
                    "input": {"query": "合成问题"},
                },
            }
            yield done(
                "pause_turn",
                complete=False,
                content=[
                    {
                        "type": "server_tool_use",
                        "id": "search-one",
                        "name": "web_search",
                        "input": {"query": "合成问题"},
                    }
                ],
            )
        else:
            yield {"type": "text_delta", "text": "搜索续轮后的公开答案"}
            yield done()

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)
    assert outcome.status == "completed" and len(calls) == 2
    assert calls[1][-1]["role"] == "assistant"
    assert calls[1][-1]["content"][0]["id"] == "search-one"
    assert last_usage(engine)["server_search_calls"] == 1
    assert (
        last_usage(engine)["usage_complete"] is False
    )  # Later complete metadata cannot certify the earlier turn.
    assert last_usage(engine)["input_tokens"] == 6 and last_usage(engine)["output_tokens"] == 4


async def test_search_retrieval_uses_receive_time_across_midnight(engine, monkeypatch):
    original_clock = runtime.build_research_context
    shanghai = ZoneInfo("Asia/Shanghai")
    started = datetime(2026, 10, 25, 23, 59, 58, tzinfo=shanghai)
    requested = datetime(2026, 10, 25, 23, 59, 59, tzinfo=shanghai)
    received = datetime(2026, 10, 26, 0, 0, 2, tzinfo=shanghai)
    later = iter((requested, received))
    retrieved, systems = [], []

    def clock(**kwargs):
        return original_clock(now=started if kwargs.get("run_id") else next(later), **kwargs)

    def record_result(self, block, *, retrieved_at):
        retrieved.append(retrieved_at)
        return None

    async def script(*args, **kwargs):
        systems.append(kwargs["system"])
        yield {
            "type": "server_tool_use",
            "block": {
                "type": "server_tool_use",
                "id": "midnight-search",
                "name": "web_search",
                "input": {"query": "合成问题"},
            },
        }
        yield {
            "type": "server_tool_result",
            "block": {"type": "web_search_tool_result", "tool_use_id": "midnight-search", "content": []},
        }
        yield {"type": "text_delta", "text": "合成公开答案"}
        yield done()

    monkeypatch.setattr(runtime, "build_research_context", clock)
    monkeypatch.setattr(runtime.EvidenceTracker, "record_server_result", record_result)
    monkeypatch.setattr(runtime, "model_stream", script)
    assert (await execute(engine)).status == "completed"
    assert retrieved == [received.isoformat(timespec="seconds")]
    assert requested.isoformat(timespec="seconds") in systems[0]
    research_event = next(e for e in events(engine) if e["type"] == "research_started")
    assert research_event["ui"]["research"]["as_of"] == started.isoformat(timespec="seconds")


async def test_pause_call_budget_exhaustion_is_partial(engine, monkeypatch):
    engine.service.limits = runtime.Limits(model_calls=2)
    calls = []

    async def script(*args, **kwargs):
        calls.append(1)
        yield {
            "type": "server_tool_use",
            "block": {
                "type": "server_tool_use",
                "id": "search-" + str(len(calls)),
                "name": "web_search",
                "input": {"query": "合成"},
            },
        }
        yield done("pause_turn")

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)
    assert outcome.status == "partial" and outcome.error_code == "budget_exceeded"
    assert len(calls) == 2 and last_usage(engine)["server_search_calls"] == 2


async def test_search_allowance_is_bound_before_each_request(engine, monkeypatch):
    allowances, call_number = [], 0

    async def script(*args, **kwargs):
        nonlocal call_number
        call_number += 1
        search = [t for t in kwargs["tools"] if t.get("name") == "web_search"]
        allowances.append(search[0]["max_uses"] if search else 0)
        for index in range((1, 2, 1, 0)[call_number - 1]):
            yield {
                "type": "server_tool_use",
                "block": {
                    "type": "server_tool_use",
                    "id": f"search-{call_number}-{index}",
                    "name": "web_search",
                    "input": {"query": "合成"},
                },
            }
        if call_number < 4:
            yield done("pause_turn")
        else:
            yield {"type": "text_delta", "text": "额度内的公开结果"}
            yield done()

    monkeypatch.setattr(runtime, "model_stream", script)
    assert (await execute(engine)).status == "completed"
    assert allowances == [2, 2, 1, 0]
    assert last_usage(engine)["server_search_calls"] == 4


async def test_unexpected_search_overrun_preserves_observed_attempt(engine, monkeypatch):
    engine.service.limits = runtime.Limits(server_search_calls=1)

    async def script(*args, **kwargs):
        assert [t["max_uses"] for t in kwargs["tools"] if t.get("name") == "web_search"] == [1]
        for index in range(2):
            yield {
                "type": "server_tool_use",
                "block": {
                    "type": "server_tool_use",
                    "id": "overrun-" + str(index),
                    "name": "web_search",
                    "input": {"query": "合成"},
                },
            }
        yield done()

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)
    assert outcome.status == "partial" and outcome.error_code == "budget_exceeded"
    assert last_usage(engine)["server_search_calls"] == 2
    assert last_usage(engine)["server_search_usage_complete"] is False


def claimed_outcome(engine):
    assert store.claim_run(engine.user, engine.run["run_id"], "review-executor")
    return runtime.Outcome(engine.user, engine.run["run_id"], "review-executor", "completed", "合成答案")


@pytest.mark.parametrize("removal", ["disable", "soft_delete", "delete_user"])
def test_owner_disabling_at_final_writer_retires_without_new_content(engine, monkeypatch, removal):
    outcome = claimed_outcome(engine)
    original = store.finish_run
    before = len(events(engine))

    def disable_then_finish(*args):
        with get_db() as db:
            if removal == "disable":
                db.execute("UPDATE users SET disabled_at=1 WHERE username=?", [engine.user])
            elif removal == "soft_delete":
                db.execute("UPDATE conversations SET deleted_at=1 WHERE id=?", [engine.conv])
            else:
                db.execute("DELETE FROM users WHERE username=?", [engine.user])
        return original(*args)

    monkeypatch.setattr(store, "finish_run", disable_then_finish)
    engine.service._persist(outcome)
    with get_db() as db:
        row = db.execute(
            "SELECT status,error_code FROM native_agent_runs WHERE run_id=?", [engine.run["run_id"]]
        ).fetchone()
        assert tuple(row) == ("cancelled", "owner_unavailable")
        assert (
            db.execute(
                "SELECT COUNT(*) FROM native_agent_events WHERE run_id=?", [engine.run["run_id"]]
            ).fetchone()[0]
            == before
        )
        assert db.execute("SELECT COUNT(*) FROM messages WHERE role='assistant'").fetchone()[0] == 0
    assert outcome.abandoned


def test_arbitrary_404_is_not_an_owner_retirement_grant(engine, monkeypatch):
    outcome = claimed_outcome(engine)

    def reject(*args):
        raise HTTPException(404)

    monkeypatch.setattr(store, "finish_run", reject)
    with pytest.raises(RuntimeError, match="native_owner_retirement_unconfirmed"):
        engine.service._persist(outcome)
    assert store.get_run(engine.user, engine.run["run_id"])["status"] == "running"


def test_assistant_writer_rechecks_enabled_owner(engine):
    with get_db() as db:
        db.execute("UPDATE users SET disabled_at=1 WHERE username=?", [engine.user])
    with pytest.raises(HTTPException) as error:
        runtime._save_assistant_report(engine.user, engine.run, "{}", {})
    assert error.value.status_code == 404
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM messages WHERE role='assistant'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM saved_reports").fetchone()[0] == 0


def test_assistant_and_report_failure_roll_back_together(engine, monkeypatch):
    def fail(*args):
        raise RuntimeError("synthetic snapshot failure")

    monkeypatch.setattr("server.reports.save_report_snapshot", fail)
    with pytest.raises(RuntimeError, match="synthetic snapshot failure"):
        runtime._save_assistant_report(engine.user, engine.run, "{}", {})
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM messages WHERE role='assistant'").fetchone()[0] == 0
        assert (
            db.execute(
                "SELECT assistant_message_id FROM native_agent_dialogues WHERE run_id=?",
                [engine.run["run_id"]],
            ).fetchone()[0]
            is None
        )


@pytest.mark.parametrize("code", ["provider_auth_error", "provider_timeout", "provider_connection_error"])
def test_native_failed_execution_reaches_diagnostics(engine, monkeypatch, code):
    async def failed(*args, **kwargs):
        raise runtime.provider.ProviderError(code)
        yield  # pragma: no cover

    monkeypatch.setattr(runtime, "model_stream", failed)
    asyncio.run(execute(engine))
    with get_db() as db:
        row = dict(
            db.execute("SELECT * FROM diagnostic_events WHERE kind='server_native_run_failed'").fetchone()
        )
    assert row["message"] == "Native outcome: " + code
    assert row["occurrences"] == 1
    assert row["severity"] == "error"
    assert "合成研究问题" not in json.dumps(row, ensure_ascii=False)
    assert "synthetic-isolated-test" not in json.dumps(row)


@pytest.mark.parametrize(
    "status,code,expected",
    [
        ("completed", None, None),
        ("cancelled", "cancelled_by_user", None),
        ("cancelled", "execution_interrupted_unknown", "server_native_run_incomplete"),
        ("partial", "output_incomplete", "server_native_run_incomplete"),
        ("failed", "budget_exceeded", "server_native_run_incomplete"),
        ("failed", "private_marker", "server_native_run_failed"),
    ],
)
def test_terminal_diagnostics_classify_and_do_not_duplicate(engine, status, code, expected):
    outcome = runtime.Outcome(
        engine.user, engine.run["run_id"], "test", status, text="PRIVATE_ANSWER_MARKER", error_code=code
    )
    engine.service._persist(outcome)
    engine.service._persist(outcome)
    with get_db() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM diagnostic_events")]
    if expected is None:
        assert not rows
    else:
        assert len(rows) == 1 and rows[0]["kind"] == expected and rows[0]["occurrences"] == 1
        assert "PRIVATE_ANSWER_MARKER" not in json.dumps(rows)
        assert "private_marker" not in json.dumps(rows)


def test_diagnostic_failure_does_not_undo_saved_research(engine, monkeypatch):
    from server import diagnostics

    def unavailable(*args):
        raise RuntimeError("synthetic diagnostic sink unavailable")

    monkeypatch.setattr(diagnostics, "record_native_outcome", unavailable)
    outcome = runtime.Outcome(
        engine.user, engine.run["run_id"], "test", "failed", error_code="provider_timeout"
    )
    engine.service._persist(outcome)
    assert store.get_run(engine.user, engine.run["run_id"])["status"] == "failed"


async def test_model_receives_frozen_profile_not_latest_settings(engine, monkeypatch):
    from server.preferences import init_preferences_db

    init_preferences_db()
    frozen = {
        "configured": True,
        "version": 1,
        "style": "conservative",
        "horizon": "long",
        "max_position_pct": 12,
        "max_drawdown_pct": 7,
        "allow_leverage": False,
    }
    with get_db() as db:
        db.execute("DELETE FROM native_agent_runs WHERE run_id=?", [engine.run["run_id"]])
        db.execute("INSERT INTO research_profiles VALUES(?,?,?,?)", [engine.user, 1, json.dumps(frozen), 1])
    engine.run = store.create_run(engine.user, engine.conv, "合成偏好验证", "synthetic-model", "research")
    with get_db() as db:
        db.execute(
            "UPDATE research_profiles SET version=2,profile_json=? WHERE username=?",
            [json.dumps({**frozen, "version": 2, "style": "aggressive"}), engine.user],
        )
    systems = []

    async def script(*args, **kwargs):
        systems.append(kwargs["system"])
        yield {"type": "text_delta", "text": "合成答案"}
        yield {"type": "usage", "input_tokens": 5, "output_tokens": 4}
        yield done()

    monkeypatch.setattr(runtime, "model_stream", script)
    await execute(engine)
    assert systems and all('"style": "conservative"' in s and '"version": 1' in s for s in systems)
    assert all('"style": "aggressive"' not in s for s in systems)
    assert all("未评估" in s for s in systems)
    assert store.get_run(engine.user, engine.run["run_id"])["profile"] == frozen
