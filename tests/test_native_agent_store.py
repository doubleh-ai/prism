"""Durable execution tests on synthetic users and the autouse temporary database."""

import json
import re
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

from server.agent import store
from server.database import get_db, reset_connection


@pytest.fixture
def owners():
    with get_db() as db:
        for user in ("alice", "bob"):
            db.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", [user, "synthetic-only"])
            db.execute("INSERT INTO conversations(id,username) VALUES(?,?)", [user + "-conv", user])
    return "alice", "bob"


def create(user="alice", conv="alice-conv", request_id=None, **changes):
    args = {"question": "合成公司的产业研究", "model_id": "synthetic-model", "workflow": "research"}
    return store.create_run(user, conv, **(args | changes), request_id=request_id)


def assert_http(code, call):
    with pytest.raises(HTTPException) as exc:
        call()
    assert exc.value.status_code == code


def test_create_and_reopen_persists_execution_and_distinguishes_research_review(owners):
    run = create()
    assert re.fullmatch("[0-9a-f]{32}", run["id"])
    assert run["run_id"] == run["id"] and run["created"] and not run["reused"]
    assert run["status"] == "queued" and run["research_status"] == "unreviewed"
    assert "input_sha256" not in run and "claimed_by" not in run
    assert store.claim_run("alice", run["id"], "worker-one")
    store.append_event("alice", run["id"], {"type": "assistant_delta", "text": "公开回答"})
    done = store.finish_run("alice", run["id"], "completed", "公开回答")
    reset_connection()
    assert store.get_run("alice", run["id"]) == done
    assert done["status"] == "completed" and done["research_status"] == "unreviewed"
    assert [e["type"] for e in store.list_events("alice", run["id"])] == [
        "run_created",
        "run_started",
        "assistant_delta",
        "run_finished",
    ]


def test_request_key_scopes_and_changed_input_conflict(owners):
    first = create(request_id="request-one")
    again = create(request_id="request-one")
    assert first["id"] == again["id"] and not again["created"] and again["reused"]
    for changed in ({"question": "不同问题"}, {"model_id": "other"}, {"workflow": "industry"}):
        assert_http(409, lambda: create(request_id="request-one", **changed))
    other = create("bob", "bob-conv", request_id="request-one")
    unbound = create(conv=None, request_id="request-one")
    assert len({first["id"], other["id"], unbound["id"]}) == 3
    assert create(conv=None, request_id="request-one")["id"] == unbound["id"]


def test_simultaneous_duplicate_requests_create_one_run(owners):
    def worker(_):
        try:
            return create(request_id="concurrent-id")
        finally:
            reset_connection()

    with ThreadPoolExecutor(max_workers=6) as pool:
        runs = list(pool.map(worker, range(12)))
    assert len({r["id"] for r in runs}) == 1
    assert sum(r["created"] for r in runs) == 1
    assert len(store.list_runs("alice")) == 1


def test_all_owned_operations_hide_other_users_and_deleted_conversations(owners):
    run = create()
    calls = [
        lambda user: store.get_run(user, run["id"]),
        lambda user: store.list_events(user, run["id"]),
        lambda user: store.append_event(user, run["id"], {"type": "status", "text": "x"}),
        lambda user: store.finish_run(user, run["id"], "failed", "x"),
        lambda user: store.claim_run(user, run["id"], "worker"),
        lambda user: store.request_cancel(user, run["id"]),
        lambda user: store.is_cancelled(user, run["id"]),
        lambda user: store.set_run_status(user, run["id"], "running"),
    ]
    for call in calls:
        assert_http(404, lambda call=call: call("bob"))
    assert_http(404, lambda: create("bob", "alice-conv"))
    with get_db() as db:
        db.execute("UPDATE conversations SET deleted_at=1 WHERE id='alice-conv'")
    for call in calls:
        assert_http(404, lambda call=call: call("alice"))
    assert store.list_runs("alice") == []
    assert_http(404, lambda: create(request_id="deleted-conv"))


def test_unbound_runs_require_existing_enabled_owner(owners):
    run = create(conv=None)
    assert_http(404, lambda: create("unknown", None))
    with get_db() as db:
        db.execute("UPDATE users SET disabled_at=1 WHERE username='alice'")
    assert_http(404, lambda: store.get_run("alice", run["id"]))
    assert store.list_runs("alice") == []
    with get_db() as db:
        db.execute("DELETE FROM users WHERE username='alice'")
    assert_http(404, lambda: store.list_events("alice", run["id"]))


def test_concurrent_event_writers_have_gapless_ordered_durable_sequences(owners):
    run = create()

    def worker(index):
        try:
            return store.append_event("alice", run["id"], {"type": "tool_result", "index": index})
        finally:
            reset_connection()

    with ThreadPoolExecutor(max_workers=6) as pool:
        events = list(pool.map(worker, range(30)))
    assert sorted(e["seq"] for e in events) == list(range(2, 32))
    cursor, recovered = 0, []
    while page := store.list_events("alice", run["id"], after_seq=cursor, limit=7):
        recovered.extend(page)
        cursor = page[-1]["seq"]
    assert [e["seq"] for e in recovered] == list(range(1, 32))
    assert sorted(e["index"] for e in recovered if e["type"] == "tool_result") == list(range(30))
    assert store.get_run("alice", run["id"])["last_seq"] == 31


def test_failed_append_rolls_back_event_and_sequence(owners, monkeypatch):
    run = create()
    original = store._insert_event

    def failing(db, row, event):
        original(db, row, event)
        raise RuntimeError("synthetic write interruption")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_insert_event", failing)
        with pytest.raises(RuntimeError):
            store.append_event("alice", run["id"], {"type": "assistant_delta", "text": "lost"})
    event = store.append_event("alice", run["id"], {"type": "assistant_delta", "text": "retained"})
    assert event["seq"] == 2
    assert [e.get("text") for e in store.list_events("alice", run["id"])] == [None, "retained"]


def test_claim_is_once_even_for_same_executor_and_does_not_replay_after_restart(owners):
    run = create()
    assert store.claim_run("alice", run["id"], "worker-one")
    assert not store.claim_run("alice", run["id"], "worker-one")
    assert not store.claim_run("alice", run["id"], "worker-two")
    reset_connection()
    assert not store.claim_run("alice", run["id"], "worker-three")
    assert store.set_run_status("alice", run["id"], "awaiting_user")["status"] == "awaiting_user"
    assert not store.claim_run("alice", run["id"], "worker-four")
    assert store.set_run_status("alice", run["id"], "running")["status"] == "running"


def test_concurrent_executors_cannot_both_claim_the_same_run(owners):
    run = create()

    def claim(index):
        try:
            return store.claim_run("alice", run["id"], f"worker-{index}")
        finally:
            reset_connection()

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(claim, range(12))) == 1


def test_cancel_intent_is_idempotent_and_does_not_assert_external_stop(owners):
    run = create()
    first = store.request_cancel("alice", run["id"])
    assert first["cancel_requested"] and first["status"] == "queued"
    assert store.request_cancel("alice", run["id"]) == first
    assert store.is_cancelled("alice", run["id"])
    assert not store.claim_run("alice", run["id"], "worker")
    assert store.list_events("alice", run["id"])[-1]["external_stop_confirmed"] is False
    done = store.finish_run("alice", run["id"], "cancelled", "已取消")
    assert store.request_cancel("alice", run["id"]) == done


def test_terminal_is_immutable_and_exact_finish_retry_is_read_only(owners):
    run = create()
    done = store.finish_run("alice", run["id"], "failed", "公开错误", "synthetic_error")
    assert store.finish_run("alice", run["id"], "failed", "公开错误", "synthetic_error") == done
    assert_http(409, lambda: store.finish_run("alice", run["id"], "completed", "改写"))
    assert_http(409, lambda: store.append_event("alice", run["id"], {"type": "assistant_delta", "text": "x"}))
    assert_http(409, lambda: store.set_run_status("alice", run["id"], "running"))


def test_recovery_is_explicit_terminal_and_never_retries_uncertain_calls(owners):
    queued, running, complete = create(), create(), create()
    store.claim_run("alice", running["id"], "old-worker")
    store.append_event("alice", running["id"], {"type": "model_started", "attempt_id": "attempt-one"})
    store.finish_run("alice", complete["id"], "completed", "已有结果")
    with pytest.raises(RuntimeError):
        store.recover_interrupted_runs()
    assert store.recover_interrupted_runs(exclusive_startup=True) == 2
    assert store.get_run("alice", queued["id"])["status"] == "failed"
    interrupted = store.get_run("alice", running["id"])
    assert interrupted["status"] == "partial" and interrupted["error_code"] == "interrupted_execution_unknown"
    event = store.list_events("alice", running["id"])[-1]
    assert event["external_completion_unknown"] is True and event["automatic_retry"] is False
    assert not store.claim_run("alice", running["id"], "new-worker")
    assert store.recover_interrupted_runs(exclusive_startup=True) == 0
    assert store.get_run("alice", complete["id"])["summary"] == "已有结果"


def test_public_projection_redacts_secrets_and_drops_internal_fields_before_storage(owners):
    run = create(question="API_KEY=synthetic-secret /Users/example/private.txt")
    store.append_event(
        "alice",
        run["id"],
        {
            "type": "tool_result",
            "api_key": "synthetic-never-store",
            "DEEPSEEK_API_KEY": "synthetic-prefixed-key",
            "environment": {"SECRET": "private"},
            "result": {
                "text": "Bearer synthetic-token https://example.com/?token=synthetic-link",
                "path": "/home/x",
            },
            "reasoning": "internal chain",
            "raw": {"private_key": "synthetic-key"},
            "public_text": '{"api_key":"synthetic-json-key"}',
            "input_tokens": 12,
            "citation": "https://example.com/disclosure",
        },
    )
    store.finish_run("alice", run["id"], "completed", "password=synthetic-password")
    with get_db() as db:
        stored = " ".join(str(row[0]) for row in db.execute("SELECT payload FROM native_agent_events"))
        stored += " ".join(
            str(v)
            for v in db.execute(
                "SELECT question,summary FROM native_agent_runs WHERE run_id=?", [run["id"]]
            ).fetchone()
        )
    for secret in (
        "synthetic-secret",
        "synthetic-never-store",
        "synthetic-token",
        "synthetic-link",
        "internal chain",
        "synthetic-key",
        "synthetic-prefixed-key",
        "synthetic-json-key",
        "synthetic-password",
        "/Users/example",
        "/home/x",
    ):
        assert secret not in stored
    event = store.list_events("alice", run["id"])[1]
    assert event["input_tokens"] == 12 and event["citation"] == "https://example.com/disclosure"


@pytest.mark.parametrize(
    "event",
    [
        {"type": "thinking_delta", "thinking": "private"},
        {"type": "provider", "delta": {"type": "reasoning", "text": "private"}},
        {"type": "assistant_delta", "seq": 999},
        {"type": "assistant_delta", "text": "a" * 65537},
        {"type": "tool_result", "value": float("nan")},
        {"type": "assistant_delta", "text": "中" * 30000},
        {"type": "assistant_delta", "text": "\ud800"},
    ],
)
def test_invalid_or_private_frames_never_append(event, owners):
    run = create()
    assert_http(422, lambda: store.append_event("alice", run["id"], event))
    assert store.get_run("alice", run["id"])["last_seq"] == 1


def test_replay_bounds_and_schema_initialization_are_safe(owners):
    run = create()
    store.init_agent_db()
    store.init_agent_db()
    for _ in range(210):
        store.append_event("alice", run["id"], {"type": "status", "text": "公开进度"})
    assert len(store.list_events("alice", run["id"], limit=10000)) == 200
    assert len(store.list_events("alice", run["id"], after_seq=200)) == 11
    for kwargs in ({"after_seq": -1}, {"after_seq": True}, {"limit": 0}, {"limit": True}):
        assert_http(422, lambda kwargs=kwargs: store.list_events("alice", run["id"], **kwargs))
    assert store.get_run("alice", run["id"])["status"] == "queued"
    assert not json.loads(json.dumps(store.get_run("alice", run["id"])))["cancel_requested"]
