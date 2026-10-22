"""Real dispatch with isolated data and mocked external/long-running engines."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from server import capabilities as cap
from server.database import get_db
from server.work_lifecycle import Manager


@pytest.fixture
def scoped_run(test_user):
    user = test_user["username"]
    with get_db() as db:
        db.execute("INSERT INTO conversations(id,username) VALUES('owned-conv',?)", [user])
        db.execute("INSERT INTO users(username,password_hash) VALUES('other','unused-test-hash')")
        db.execute("INSERT INTO conversations(id,username) VALUES('other-conv','other')")
    manager = Manager()
    parent = manager.admit("cli")
    token = cap.issue_run_token(user, "owned-conv", "test-run", parent_lease=parent)
    yield {
        "username": user,
        "conv_id": "owned-conv",
        "run_id": "test-run",
        "token": token,
        "parent_lease": parent,
    }
    cap.revoke_run_token("test-run")
    cap.revoke_run_token("next-run")
    parent.release()
    manager._pool.shutdown(wait=True, cancel_futures=False)


@pytest.fixture
async def bridge_client(scoped_run):
    app = FastAPI()
    app.include_router(cap.router)
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 4321)),
        base_url="http://127.0.0.1:8088",
        headers={"Authorization": "Bearer " + scoped_run["token"], "X-Requested-With": "XMLHttpRequest"},
    ) as client:
        yield client


async def call(context, name, arguments=None, request_id="request-1"):
    return await cap.dispatch_call(
        context, cap.CallRequest(name=name, arguments=arguments or {}, request_id=request_id)
    )


async def test_bridge_auth_checks_loopback_origin_forwarding_expiry_and_owner(scoped_run, bridge_client):
    path = "/api/internal/capabilities/tools"
    declared = await bridge_client.get(path)
    assert declared.status_code == 200
    names = {tool["name"] for tool in declared.json()["tools"]}
    assert not names & {"vibe_start", "vibe_followup"}
    assert {"vibe_list", "vibe_status", "vibe_cancel"} <= names
    for headers in (
        {"Origin": "http://127.0.0.1:8088"},
        {"X-Forwarded-For": "127.0.0.1"},
        {"Forwarded": "for=127.0.0.1"},
        {"Authorization": "Bearer wrong"},
    ):
        assert (await bridge_client.get(path, headers=headers)).status_code in {401, 403}
    app = FastAPI()
    app.include_router(cap.router)
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("192.0.2.1", 1)), base_url="http://127.0.0.1:8088"
    ) as remote:
        assert (
            await remote.get(path, headers={"Authorization": "Bearer " + scoped_run["token"]})
        ).status_code == 403
    with pytest.raises(HTTPException) as denied:
        cap.issue_run_token("other", "owned-conv", "wrong-user-run")
    assert denied.value.status_code == 404
    with get_db() as db:
        db.execute("UPDATE users SET disabled_at=1 WHERE username=?", [scoped_run["username"]])
    assert (await bridge_client.get(path)).status_code == 404
    with get_db() as db:
        db.execute("UPDATE users SET disabled_at=NULL WHERE username=?", [scoped_run["username"]])
        db.execute("UPDATE capability_runs SET expires_at=1 WHERE run_id='test-run'")
    assert (await bridge_client.get(path)).status_code == 401


async def test_bridge_revoke_delete_and_schema_deny(scoped_run, bridge_client):
    endpoint = "/api/internal/capabilities/call"
    for name, arguments, status in (
        ("shell", {"command": "ignored"}, 404),
        ("market_quote", {"ticker": "600519", "username": "other"}, 422),
        ("market_quote", {"ticker": "http://outside.invalid"}, 422),
    ):
        response = await bridge_client.post(
            endpoint, json={"name": name, "arguments": arguments, "request_id": "test-arg"}
        )
        assert response.status_code == status
    cap.revoke_run_token(scoped_run["run_id"])
    assert (await bridge_client.get("/api/internal/capabilities/tools")).status_code == 401
    token = cap.issue_run_token(
        scoped_run["username"], "owned-conv", "next-run", parent_lease=scoped_run["parent_lease"]
    )
    with get_db() as db:
        db.execute("UPDATE conversations SET deleted_at=1 WHERE id='owned-conv'")
    assert (
        await bridge_client.get(
            "/api/internal/capabilities/tools", headers={"Authorization": "Bearer " + token}
        )
    ).status_code == 404


async def test_idempotency_audit_targets_and_no_raw_prompt_or_tokens(scoped_run, monkeypatch):
    execute = AsyncMock(
        return_value=(
            {"id": "task-one", "api_key": "fake-sensitive-value", "note": "password=placeholder-credential"},
            {"view": "indicators", "id": "task-one"},
            "研究已发起",
        )
    )
    monkeypatch.setattr(cap, "_execute", execute)
    arguments = {"input": "private test research prompt"}
    first = await call(scoped_run, "indicator_translate", arguments)
    assert await call(scoped_run, "indicator_translate", arguments) == first
    assert execute.await_count == 1
    assert first["call"]["target"] == {"view": "indicators", "id": "task-one"}
    assert "api_key" not in first["data"]
    assert "placeholder-credential" not in json.dumps(first)
    records = cap.list_calls(scoped_run["username"], view="indicators")
    assert len(records) == 1 and records[0]["conv_id"] == "owned-conv"
    assert cap.list_calls("other") == []
    with get_db() as db:
        serialized = json.dumps([dict(row) for row in db.execute("SELECT * FROM capability_calls")])
        run = dict(db.execute("SELECT * FROM capability_runs WHERE run_id='test-run'").fetchone())
    assert arguments["input"] not in serialized
    assert scoped_run["token"] not in json.dumps(run)
    with pytest.raises(HTTPException) as conflict:
        await call(scoped_run, "indicator_translate", {"input": "different prompt"})
    assert conflict.value.status_code == 409


async def test_http_errors_do_not_echo_engine_secrets(scoped_run, monkeypatch):
    monkeypatch.setattr(cap, "_execute", AsyncMock(side_effect=RuntimeError("private upstream credential")))
    result = await call(scoped_run, "market_quote", {"ticker": "600519"})
    assert result["error"]["code"] == "unavailable"
    assert "private upstream credential" not in json.dumps(result)
    assert result["call"]["status"] == "failed"


async def test_cancelled_call_has_replayable_failure(scoped_run, monkeypatch):
    monkeypatch.setattr(cap, "_execute", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await call(scoped_run, "market_quote", {"ticker": "600519"})
    replay = await call(scoped_run, "market_quote", {"ticker": "600519"})
    assert replay["ok"] is False
    assert replay["call"]["status"] == "failed"


async def test_report_ownership_and_call_history_api(scoped_run, bridge_client, auth_cookies):
    from server.reports import save_report_snapshot

    # Use the same persisted report helper as the UI, scoped to the other user.
    report = save_report_snapshot(
        "other",
        {
            "source": "chat",
            "source_id": "other-source",
            "title": "Other research",
            "report": {"final_decision": "private research"},
        },
    )
    response = await call(scoped_run, "report_get", {"id": report["id"]})
    assert response["error"]["code"] == "not_found"
    assert "private research" not in json.dumps(response)
    bridge_client.cookies.update(auth_cookies)
    assert (await bridge_client.get("/api/convs/other-conv/capability-calls")).status_code == 404
    history = await bridge_client.get("/api/capability-calls?view=reports")
    assert history.status_code == 200
    assert len(history.json()["calls"]) == 1
    assert history.json()["calls"][0]["source_kind"] == "prior_research"


async def test_indicator_executes_real_dsl_and_missing_fund_is_failure(scoped_run, monkeypatch):
    import pandas as pd

    from server.indicators import router as indicators

    library = await call(scoped_run, "indicator_list")
    assert library["data"]["indicators"]
    indicator = {"id": "unit-indicator", "dsl": {"price": "close"}, "params": {}}
    monkeypatch.setattr(indicators, "_load_owned", lambda _id, user: indicator)
    monkeypatch.setattr(
        indicators,
        "_build_df",
        AsyncMock(
            return_value=(
                pd.DataFrame(
                    {
                        "t": [1, 2],
                        "close": [10.0, 12.0],
                        "high": [11.0, 13.0],
                        "low": [9.0, 11.0],
                        "volume": [100, 200],
                    }
                ),
                {},
            )
        ),
    )
    result = await call(
        scoped_run, "indicator_eval", {"indicator_id": indicator["id"], "ticker": "600519"}, "compute"
    )
    assert result["ok"] is True
    assert result["data"]["points"][-1] == {"t": 2, "value": 12.0}
    indicator["dsl"] = {"fund": "pe"}
    result = await call(
        scoped_run, "indicator_eval", {"indicator_id": indicator["id"], "ticker": "600519"}, "missing-fund"
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "data_insufficient"


async def test_scan_background_creation_and_owner_status(scoped_run, monkeypatch):
    from server.indicators import router as indicators
    from server.indicators import scanner

    indicator = {"id": "unit-indicator", "name": "price", "dsl": {"price": "close"}, "params": {}}
    monkeypatch.setattr(indicators, "_load_owned", lambda _id, user: indicator)
    starts = []
    monkeypatch.setattr(scanner, "start_job", lambda job, ind, builder: starts.append(job["id"]))
    result = await call(scoped_run, "scan_start", {"indicator_id": indicator["id"], "scope": "market"})
    assert result["ok"] is True
    job_id = result["data"]["id"]
    assert starts == [job_id]
    assert result["call"]["target"] == {"view": "screener", "id": job_id}
    cap.revoke_run_token(scoped_run["run_id"])
    assert scanner.get_job(job_id, scoped_run["username"])
    assert scanner.get_job(job_id, "other") is None


async def test_analysis_uses_owned_persistent_record(scoped_run, monkeypatch):
    from server import analysis_engine

    async def start(**kwargs):
        assert kwargs["username"] == scoped_run["username"]
        with get_db() as db:
            db.execute(
                "INSERT INTO analysis_runs(id,username,ticker,mode,trade_date,status,started_at,updated_at) VALUES('analysis-one',?,'600519','deep',?,'running',1,1)",
                [kwargs["username"], kwargs["date"]],
            )
        return "analysis-one"

    monkeypatch.setattr(analysis_engine, "start_analysis", start)
    result = await call(scoped_run, "analysis_start", {"ticker": "600519"})
    assert result["ok"] is True
    assert result["call"]["target"]["id"] == "analysis-one"
    # No memory process remains: status is honest and the record is still readable.
    status = await call(scoped_run, "analysis_status", {"id": "analysis-one"}, "read-status")
    assert status["data"]["status"] == "interrupted"
    assert analysis_engine.get_analysis_record("analysis-one", "other") is None


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("vibe_start", {"prompt": "芯片产业研究"}),
        ("vibe_followup", {"id": "existing", "question": "继续核验"}),
    ],
)
async def test_paused_vibe_bridge_rejects_before_any_task_or_audit_write(
    scoped_run, monkeypatch, name, arguments
):
    execute = AsyncMock(side_effect=AssertionError("must not dispatch paused execution"))
    monkeypatch.setattr(cap, "_execute", execute)
    with pytest.raises(HTTPException) as blocked:
        await call(scoped_run, name, arguments)
    assert blocked.value.status_code == 503
    execute.assert_not_awaited()
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM vibe_plans").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM capability_calls").fetchone()[0] == 0


async def test_industry_exposes_background_provenance(scoped_run):
    response = await call(scoped_run, "industry_overview")
    assert response["ok"] is True
    assert response["call"]["source_kind"] == "curated_unverified"
    assert "未经" in response["data"]["evidence_notice"]
    catalog = cap.capability_catalog()
    assert all(item["target_view"] == item["view"] for item in catalog)
    assert {item["id"] for item in catalog if not item["read_only"]} == {
        "scan_start",
        "analysis_start",
        "vibe_start",
        "watchlist_update",
        "indicator_create",
        "scan_cancel",
        "vibe_followup",
        "vibe_cancel",
        "report_save",
        "research_record_claims",
        "research_record_relations",
        "research_scope",
    }


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "accessToken",
        "REFRESH_TOKEN",
        "api_key",
        "password",
        "cookie",
    ],
)
def test_secret_fields_removed_from_nested_dicts_and_serialized_json(key):
    marker = "synthetic-private-marker"
    assert marker not in json.dumps(cap.safe_result({"nested": {key: marker}}))
    text = json.dumps({key: marker})
    assert marker not in cap.safe_result(text)
    assert marker not in cap.safe_result(f"https://example.invalid/?{key}={marker}")
    assert cap.safe_result({"output_tokens": 123}) == {"output_tokens": 123}


async def test_unavailable_analysis_keeps_failed_call_target(scoped_run, monkeypatch):
    from server import analysis_engine as engine

    def unavailable():
        raise ImportError("synthetic missing engine")

    monkeypatch.setattr(engine, "_ensure_astock", unavailable)
    monkeypatch.setattr(engine, "ASTOCK_CACHE_TTL_SECONDS", 0)
    result = await call(scoped_run, "analysis_start", {"ticker": "600519"})
    aid = result["call"]["target"]["id"]
    try:
        assert result["ok"] is False
        assert result["call"]["status"] == "failed"
        assert result["data"]["status"] == "error"
        assert engine.get_analysis_record(aid, scoped_run["username"])["status"] == "error"
        assert engine.get_analysis_record(aid, "other") is None
    finally:
        engine._store.pop(aid, None)
