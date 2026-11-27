"""Actual SQLite admission races on temporary data; no background execution."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

from server.agent import store
from server.database import get_db, reset_connection


@pytest.fixture
def owners():
    store.init_agent_db()
    with get_db() as db:
        for user in ("alice", "bob"):
            db.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", [user, "synthetic-only"])
            for index in range(10):
                db.execute("INSERT INTO conversations(id,username) VALUES(?,?)", [f"{user}-{index}", user])


def create(index=0, *, user="alice", request_id=None, exclusive=True, question="同一份合成问题"):
    return store.create_run(
        user,
        f"{user}-{index}",
        question,
        "synthetic-model",
        "research",
        request_id=request_id,
        exclusive_conversation=exclusive,
    )


def assert_http(status, function):
    with pytest.raises(HTTPException) as caught:
        function()
    assert caught.value.status_code == status


@pytest.mark.parametrize("status", ["queued", "running", "awaiting_user"])
def test_every_active_state_blocks_another_request_in_the_same_conversation(owners, status):
    run = create(request_id="first")
    if status != "queued":
        assert store.claim_run("alice", run["id"], "synthetic-executor")
    if status == "awaiting_user":
        store.set_run_status("alice", run["id"], "awaiting_user")
    assert_http(409, lambda: create(request_id="second"))
    assert len(store.list_runs("alice")) == 1
    store.finish_run("alice", run["id"], "completed", "合成结果")
    assert create(request_id="after-terminal")["created"] is True


def race(function, count=8):
    barrier = threading.Barrier(count)

    def worker(index):
        try:
            barrier.wait(timeout=5)
            try:
                return "ok", function(index)
            except HTTPException as error:
                return "error", error.status_code
        finally:
            reset_connection()

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(worker, range(count)))


def test_actual_concurrent_same_conversation_admits_one(owners):
    outcomes = race(lambda index: create(request_id=f"distinct-{index}"))
    assert sum(kind == "ok" for kind, _ in outcomes) == 1
    assert [value for kind, value in outcomes if kind == "error"] == [409] * 7
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM native_agent_runs").fetchone()[0] == 1


def test_actual_concurrent_distinct_conversations_share_exact_user_three_limit(owners):
    outcomes = race(lambda index: create(index, request_id=f"distinct-{index}"))
    assert sum(kind == "ok" for kind, _ in outcomes) == 3
    assert [value for kind, value in outcomes if kind == "error"] == [429] * 5
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM native_agent_runs WHERE status='queued'").fetchone()[0] == 3
    assert create(0, user="bob", request_id="independent-owner")["created"] is True


def test_simultaneous_identical_request_reuses_before_conversation_rejection(owners):
    outcomes = race(lambda _index: create(request_id="same-request"))
    assert all(kind == "ok" for kind, _ in outcomes)
    rows = [value for _, value in outcomes]
    assert len({row["id"] for row in rows}) == 1 and sum(row["created"] for row in rows) == 1
    assert sum(row["reused"] for row in rows) == 7
    assert len(store.list_events("alice", rows[0]["id"])) == 1


def test_idempotent_reuse_precedes_full_user_quota_and_changed_input_still_conflicts(owners):
    first = create(0, request_id="repeat")
    create(1, request_id="second")
    create(2, request_id="third")
    assert_http(429, lambda: create(3, request_id="over-quota"))
    repeated = create(0, request_id="repeat")
    assert repeated["id"] == first["id"] and repeated["reused"] and not repeated["created"]
    assert_http(409, lambda: create(0, request_id="repeat", question="不同输入不得冒充重试"))
    assert len(store.list_runs("alice")) == 3


def test_terminal_request_reuse_is_not_a_new_execution_even_when_three_others_active(owners):
    old = create(0, request_id="completed-request")
    store.finish_run("alice", old["id"], "completed", "已有结果")
    for index in range(1, 4):
        create(index, request_id=f"active-{index}")
    repeated = create(0, request_id="completed-request")
    assert repeated["id"] == old["id"] and repeated["status"] == "completed" and repeated["reused"]
    assert_http(429, lambda: create(4, request_id="new-fourth-active"))
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM native_agent_runs").fetchone()[0] == 4


def test_pending_cancel_does_not_free_admission_until_real_terminal_record(owners):
    run = create(request_id="cancel-me")
    store.request_cancel("alice", run["id"])
    assert_http(409, lambda: create(request_id="not-yet-free"))
    store.finish_run("alice", run["id"], "cancelled", "已取消")
    assert create(request_id="now-free")["created"] is True


def test_request_identity_is_scoped_to_owner_and_conversation(owners):
    rows = [
        create(0, request_id="shared"),
        create(1, request_id="shared"),
        create(0, user="bob", request_id="shared"),
    ]
    assert len({row["id"] for row in rows}) == 3
    assert_http(
        404,
        lambda: store.create_run(
            "bob",
            "alice-0",
            "合成问题",
            "synthetic-model",
            "research",
            request_id="foreign",
            exclusive_conversation=True,
        ),
    )


@pytest.mark.parametrize("value", [1, "true", None])
def test_exclusive_flag_is_an_actual_boolean(owners, value):
    assert_http(422, lambda: create(request_id="wrong-flag", exclusive=value))


def test_legacy_nonexclusive_storage_entry_remains_compatible(owners):
    # Only explicit v1 admission opts into the new guard. Historic store callers
    # still work; storing a record is not permission to start an execution.
    rows = [create(request_id=f"legacy-{index}", exclusive=False) for index in range(4)]
    assert len({row["id"] for row in rows}) == 4
