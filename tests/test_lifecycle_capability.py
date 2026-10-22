"""Existing-run bridge ownership, fence behavior and actual durable call rows."""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from server import capabilities as cap
from server.database import get_db
from server.work_lifecycle import LifecycleError, Manager


@pytest.fixture
def run(test_user):
    user = test_user["username"]
    with get_db() as db:
        db.execute("INSERT INTO conversations(id,username) VALUES('bound-conv',?)", [user])
    manager = Manager()
    parent = manager.admit("cli")
    token = cap.issue_run_token(user, "bound-conv", "bound-run", parent_lease=parent)
    context = {"run_id": "bound-run", "username": user, "conv_id": "bound-conv"}
    yield SimpleNamespace(manager=manager, parent=parent, token=token, context=context)
    cap.revoke_run_token("bound-run")
    parent.release()
    manager._pool.shutdown(wait=True, cancel_futures=False)


async def drain_finalizers():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_fence_allows_existing_authenticated_run_only_and_no_db_grant_after_memory_loss(run):
    manager = run.manager
    manager.begin_drain(manager.ticket())
    with pytest.raises(LifecycleError):
        manager.admit("cli")
    app = FastAPI()
    app.include_router(cap.router)
    headers = {"Authorization": "Bearer " + run.token}
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 4321)), base_url="http://127.0.0.1:8088"
    ) as client:
        response = await client.post(
            "/api/internal/capabilities/call",
            headers=headers,
            json={"name": "indicator_list", "request_id": "existing"},
        )
        assert response.status_code == 200 and response.json()["ok"]
        assert manager.observe()["state"] == "draining"
        # A still-unrevoked, unexpired hashed database token is insufficient after
        # the process-local owner capability is lost (e.g. application restart).
        cap._run_bindings.pop("bound-run")
        rejected = await client.post(
            "/api/internal/capabilities/call",
            headers=headers,
            json={"name": "indicator_list", "request_id": "after-restart"},
        )
        assert rejected.status_code == 401
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM capability_calls").fetchone()[0] == 1


def test_fake_parent_and_cross_owner_context_cannot_mint_or_reuse_grant(run):
    with pytest.raises(HTTPException):
        cap.issue_run_token(run.context["username"], "bound-conv", "fake", parent_lease={"allowed": True})
    with pytest.raises(HTTPException):
        cap._run_parent({**run.context, "username": "foreign"})
    with pytest.raises(HTTPException):
        cap._run_parent({**run.context, "conv_id": "foreign"})


async def test_business_error_and_cancel_persist_terminal_without_permanent_block(run, monkeypatch):
    async def error(*args):
        raise ValueError("synthetic business argument")

    monkeypatch.setattr(cap, "_execute", error)
    result = await cap.dispatch_call(run.context, cap.CallRequest(name="indicator_list", request_id="failed"))
    assert result["call"]["status"] == "failed"
    assert run.manager.observe()["blocked"] == 0

    async def cancel(*args):
        raise asyncio.CancelledError()

    monkeypatch.setattr(cap, "_execute", cancel)
    with pytest.raises(asyncio.CancelledError):
        await cap.dispatch_call(run.context, cap.CallRequest(name="indicator_list", request_id="cancelled"))
    await drain_finalizers()
    assert run.manager.observe()["blocked"] == run.manager.observe()["persist_pending"] == 0
    with get_db() as db:
        rows = db.execute("SELECT status,response_json FROM capability_calls").fetchall()
    assert len(rows) == 2 and all(row["status"] == "failed" for row in rows)
    assert all(json.loads(row["response_json"])["ok"] is False for row in rows)


async def test_terminal_writer_failure_stays_persist_blocked(run, monkeypatch):
    async def execute(*args):
        return {"items": []}, {"view": "indicators"}, "synthetic completed"

    monkeypatch.setattr(cap, "_execute", execute)
    with get_db() as db:
        db.execute(
            "CREATE TRIGGER reject_response BEFORE UPDATE OF response_json ON capability_calls "
            "BEGIN SELECT RAISE(FAIL,'synthetic persistence failure'); END"
        )
    with pytest.raises(Exception):
        await cap.dispatch_call(run.context, cap.CallRequest(name="indicator_list", request_id="write-fail"))
    await drain_finalizers()
    assert run.manager.observe()["persist_blocked"] == run.manager.observe()["blocked"] == 1
    with get_db() as db:
        assert db.execute("SELECT status FROM capability_calls").fetchone()[0] == "running"
    run.manager.begin_drain(run.manager.ticket())
    assert not await run.manager.wait_drained(timeout=0.01)


async def test_accepted_call_returns_while_real_child_and_persistence_verifier_remain_owned(run, monkeypatch):
    started, release = threading.Event(), threading.Event()
    task_box = []

    def blocking():
        started.set()
        release.wait(timeout=5)

    async def execute(*args):
        task_box.append(asyncio.create_task(run.manager.run_blocking("auxiliary", blocking)))
        while not started.is_set():
            await asyncio.sleep(0.005)
        return {"status": "accepted"}, {"view": "analysis"}, "synthetic accepted"

    monkeypatch.setattr(cap, "_execute", execute)
    try:
        result = await cap.dispatch_call(
            run.context, cap.CallRequest(name="indicator_list", request_id="child")
        )
        assert result["ok"] and result["data"]["status"] == "accepted"
        run.manager.begin_drain(run.manager.ticket())
        assert run.manager.observe()["pending_cf"] == run.manager.observe()["persist_pending"] == 1
        assert not await run.manager.wait_drained(timeout=0.01)
        release.set()
        await asyncio.gather(*task_box)
        for _ in range(100):
            if run.manager.observe()["persist_pending"] == 0:
                break
            await asyncio.sleep(0.005)
        assert run.manager.observe()["persist_pending"] == run.manager.observe()["pending_cf"] == 0
    finally:
        release.set()
        await asyncio.gather(*task_box, return_exceptions=True)


def test_revoke_and_expiry_remove_memory_binding_without_touching_parent_work(run, monkeypatch):
    original_now = cap.time.time()
    monkeypatch.setattr(cap.time, "time", lambda: original_now + 10_000_000)
    with pytest.raises(HTTPException):
        cap._run_parent(run.context)
    assert "bound-run" not in cap._run_bindings
    assert run.manager.observe()["leases"]["cli"] == 1
    cap.revoke_run_token("bound-run")
    with get_db() as db:
        assert db.execute("SELECT revoked FROM capability_runs WHERE run_id='bound-run'").fetchone()[0] == 1
