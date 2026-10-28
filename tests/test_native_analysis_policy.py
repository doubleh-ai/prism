"""Native-only admission, saved-history and removed-executor boundaries with synthetic data."""

import ast
import asyncio
import builtins
import json
import runpy
from pathlib import Path

import pytest
from fastapi import HTTPException

from server import analysis_engine as engine
from server import capabilities as cap
from server.database import get_db
from server.research_context import build_research_context
from server.work_lifecycle import Manager, bind_context


@pytest.fixture
def forbidden_engines(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append("external execution")
        raise AssertionError("Native-only policy must precede external execution")

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.startswith(("tradingagents", "langchain", "langgraph", "claude_runner")):
            return forbidden()
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setenv("PRISM_ASTOCK_ENABLED", "1")
    monkeypatch.setenv("PRISM_LEGACY_CHAT_ENABLED", "1")
    yield calls
    assert calls == []


@pytest.mark.parametrize("force_refresh", [False, True])
async def test_authenticated_post_cannot_start_even_with_refresh_or_cached_executor(
    client, auth_cookies, forbidden_engines, force_refresh
):
    response = await client.post(
        "/api/analysis/start",
        cookies=auth_cookies,
        json={"ticker": "600519", "mode": "fast", "force_refresh": force_refresh},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == engine.ANALYSIS_EXECUTION_NOTICE
    assert response.headers["X-Prism-Execution-Policy"] == "native-only-analysis-disabled"
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM diagnostic_events").fetchone()[0] == 0


async def test_other_503_responses_still_produce_error_diagnostics(forbidden_engines):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from httpx import ASGITransport, AsyncClient

    from server.activity import ActivityMiddleware

    app = FastAPI()
    app.add_middleware(ActivityMiddleware)

    @app.post("/api/analysis/start")
    async def unavailable():
        return JSONResponse({"error": "synthetic-unexpected-unavailability"}, status_code=503)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/api/analysis/start")).status_code == 503
    with get_db() as db:
        row = db.execute("SELECT kind,severity FROM diagnostic_events").fetchone()
        assert tuple(row) == ("server_http_5xx", "error")


async def test_retired_post_still_authenticates_before_policy(client, forbidden_engines):
    response = await client.post("/api/analysis/start", json={"ticker": "600519"})
    assert response.status_code == 401


@pytest.mark.parametrize("force_refresh", [False, True])
async def test_internal_start_rejects_before_hot_cache_records_and_engine(
    test_user, monkeypatch, forbidden_engines, force_refresh
):
    user = test_user["username"]
    date = build_research_context()["date"]
    with get_db() as db:
        db.execute(
            "INSERT INTO analysis_cache(id,username,ticker,trade_date,mode,report_json) VALUES(?,?,?,?,?,?)",
            ["saved-cache", user, "600519", date, "fast", '{"final_decision":"合成缓存"}'],
        )
    manager = Manager()
    try:

        def forbidden(*args, **kwargs):
            pytest.fail("retired admission must precede database/cache access and work creation")

        with monkeypatch.context() as guards:
            guards.setattr(engine, "get_db", forbidden)
            guards.setattr(manager, "start", forbidden)
            guards.setattr(manager, "run_blocking", forbidden)
            with bind_context(manager), pytest.raises(HTTPException) as blocked:
                await engine.start_analysis("600519", username=user, mode="fast", force_refresh=force_refresh)
        assert blocked.value.status_code == 503
        observed = manager.observe()
        assert observed["blocked"] == 0 and sum(observed["leases"].values()) == 0
        with get_db() as db:
            assert db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM analysis_cache").fetchone()[0] == 1
    finally:
        manager._pool.shutdown(wait=True)


@pytest.mark.parametrize("saved_success", [False, True])
async def test_registered_dispatch_rejects_before_audit_or_old_success_replay(
    test_user, monkeypatch, forbidden_engines, saved_success
):
    user = test_user["username"]
    manager = Manager()
    parent = manager.admit("agent", persist_required=False)
    context = {"username": user, "conv_id": "policy-conv", "run_id": "policy-run"}
    with get_db() as db:
        db.execute("INSERT INTO conversations(id,username) VALUES('policy-conv',?)", [user])
        if saved_success:
            db.execute(
                "INSERT INTO capability_calls(call_id,run_id,username,conv_id,request_id,capability_id,"
                "arguments_hash,status,response_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,1)",
                [
                    "old-call",
                    "policy-run",
                    user,
                    "policy-conv",
                    "same-request",
                    "analysis_start",
                    "old-hash",
                    "completed",
                    '{"ok":true,"data":{"status":"started"}}',
                ],
            )
    cap.issue_run_token(user, context["conv_id"], context["run_id"], parent_lease=parent)
    try:
        request = cap.CallRequest(
            name="analysis_start", arguments={"ticker": "600519"}, request_id="same-request"
        )
        with pytest.raises(HTTPException) as blocked:
            await cap.dispatch_call(context, request)
        assert blocked.value.status_code == 503
        with pytest.raises(HTTPException) as direct:
            await cap._execute("analysis_start", cap.AnalysisArgs(ticker="600519"), context)
        assert direct.value.status_code == 503
        with get_db() as db:
            rows = db.execute("SELECT * FROM capability_calls").fetchall()
            assert len(rows) == int(saved_success)
            assert db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 0
            if saved_success:
                assert rows[0]["response_json"] == '{"ok":true,"data":{"status":"started"}}'
        catalog = {row["id"]: row for row in cap.capability_catalog()}
        assert catalog["analysis_start"]["available"] is False
        assert catalog["analysis_start"]["unavailable_reason"] == engine.ANALYSIS_EXECUTION_NOTICE
        assert "available" not in catalog["analysis_status"]
    finally:
        cap.revoke_run_token(context["run_id"])
        parent.release()
        manager._pool.shutdown(wait=True)


def test_no_legacy_executor_cache_or_model_extraction_entry_points_remain():
    from server import config

    removed = {
        "_run_analysis_worker",
        "_ensure_astock",
        "_build_config",
        "_astock_backend_url",
        "_extract_structured",
        "_save_cached_analysis",
        "_load_cached_analysis",
        "_store",
    }
    assert not removed.intersection(vars(engine))
    assert not any(name.startswith("ASTOCK_") for name in vars(config))
    tree = ast.parse(Path(engine.__file__).read_text())
    imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert all(
        (node.module in {"fastapi", "database", "typing"})
        if isinstance(node, ast.ImportFrom)
        else all(item.name == "json" for item in node.names)
        for node in imports
    )


async def test_owned_history_remains_readable_without_execution(
    client, auth_cookies, test_user, forbidden_engines
):
    report = {"final_decision": "已保存的合成研究"}
    with get_db() as db:
        db.execute(
            "INSERT INTO analysis_runs(id,username,ticker,mode,trade_date,status,report_json,started_at,updated_at) "
            "VALUES('saved-analysis',?,'600519','fast','2026-10-28','done',?,1,1)",
            [test_user["username"], json.dumps(report)],
        )
    response = await client.get("/api/analysis/saved-analysis/report", cookies=auth_cookies)
    assert response.status_code == 200 and response.json()["report"] == report
    assert (await client.get("/api/analysis/unknown/report", cookies=auth_cookies)).status_code == 404


def test_public_operational_entry_point_is_only_a_retirement_notice():
    path = Path(__file__).resolve().parents[1] / "scripts/new_runtime_stop.py"
    source = path.read_bytes()
    assert len(source) <= 1024
    tree = ast.parse(source)
    assert not any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(tree))
    assert {node.name for node in tree.body if isinstance(node, ast.FunctionDef)} == {"main"}
    calls = [node.func for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert all(isinstance(node, ast.Name) and node.id in {"main", "SystemExit"} for node in calls)
    namespace = runpy.run_path(str(path))
    with pytest.raises(SystemExit, match="reviewed private operations workflow"):
        namespace["main"]()
