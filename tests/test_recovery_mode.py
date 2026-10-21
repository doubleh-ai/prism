"""Exercise the exact fixed recovery ASGI entry, never models or production data."""

import json
import os
import subprocess
import sys

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient


def test_fresh_recovery_startup_cannot_import_task_runners(tmp_path):
    code = """
import importlib.abc, sys
class RejectRunners(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'server.main', 'server.services.claude_runner', 'server.vibe', 'server.analysis_graph'} or fullname.startswith('tradingagents'):
            raise AssertionError('recovery imported task runner: ' + fullname)
sys.meta_path.insert(0, RejectRunners())
from server.recovery import app, readiness_payload
assert readiness_payload()['status'] == 'ready'
assert app.title == 'Prism 有限恢复服务'
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "STOCK_DATA_DIR": str(tmp_path / "data"),
            "STOCK_LOG_FILE": str(tmp_path / "log"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "CLAUDE_BIN": "/nonexistent/recovery-cli",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
async def recovery_client(test_user):
    from server.recovery import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://recovery.test") as client:
        yield client


@pytest.fixture
def stored_history(test_user):
    from server.auth import hash_password
    from server.database import create_user, get_db

    create_user("other-owner", hash_password("other-password"))
    with get_db() as db:
        for cid, username in [("mine", test_user["username"]), ("theirs", "other-owner")]:
            db.execute(
                "INSERT INTO conversations(id,username,title,skill,session_id,project_dir) VALUES(?,?,?,'prism-research','external-session','shared-external-project')",
                [cid, username, cid],
            )
            db.execute(
                "INSERT INTO messages(conv_id,role,content) VALUES(?,'assistant',?)",
                [cid, json.dumps({"text": cid + " saved research"})],
            )
            db.execute(
                "INSERT INTO saved_reports(id,username,conv_id,title,report_json,created_at,updated_at) VALUES(?,?,?,?,?,1,1)",
                [cid, username, cid, cid, json.dumps({"summary": cid})],
            )
            db.execute(
                "INSERT INTO vibe_plans(id,username,prompt,plan_json,created_at) VALUES(?,?,'fixture',?,1)",
                [cid, username, json.dumps({"id": cid, "status": "running", "report_markdown": cid})],
            )
            db.execute(
                "INSERT INTO analysis_runs(id,username,ticker,mode,trade_date,status,report_json,started_at,updated_at) VALUES(?,?,'600000','deep','2026-10-20','done',?,1,1)",
                [cid, username, json.dumps({"summary": cid})],
            )
            db.execute(
                "INSERT INTO user_watchlist(username,ticker,name,position,updated_at) VALUES(?,?,'fixture',0,1)",
                [username, "600000" if cid == "mine" else "000001"],
            )
            db.execute(
                "INSERT INTO capability_calls(call_id,run_id,username,conv_id,request_id,capability_id,arguments_hash,status,target_json,created_at) VALUES(?,?,?,?,?,'market_quote','fixture','completed',?,1)",
                [cid, cid, username, cid, cid, json.dumps({"view": "market", "ticker": "600000"})],
            )
    return test_user["username"]


async def test_recovery_login_logout_metadata_and_banner(recovery_client, test_user, monkeypatch, tmp_path):
    import server.release as release

    marker = tmp_path / "revision"
    marker.write_text("a" * 40)
    monkeypatch.setattr(release, "DEPLOY_REVISION_PATH", marker)
    ready = await recovery_client.get("/health/ready")
    assert ready.json()["deployed_revision"] == "a" * 40
    assert ready.json()["version"] == "v0.19.8-recovery"
    assert ready.json()["new_research_enabled"] is False
    assert (await recovery_client.get("/api/me")).status_code == 401
    response = await recovery_client.post("/api/login", json=test_user)
    assert response.status_code == 200
    assert response.headers["x-prism-recovery-mode"] == "limited_read_only_recovery"
    assert (await recovery_client.get("/api/me")).json()["user"] == test_user["username"]
    for path in ["/", "/login"]:
        response = await recovery_client.get(path)
        assert "Prism 有限恢复模式" in response.text
        assert "recovery.css?v=recovery-v0198" in response.text
        assert response.headers["cache-control"] == "no-store"
    assert (await recovery_client.post("/api/logout")).status_code == 403
    assert (
        await recovery_client.post("/api/logout", headers={"X-Requested-With": "XMLHttpRequest"})
    ).status_code == 200
    assert (await recovery_client.get("/api/me")).status_code == 401


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def test_every_api_mutation_except_auth_is_blocked(recovery_client, auth_cookies, method):
    for path in [
        "/api/chat/stream",
        "/api/convs",
        "/api/reports/library",
        "/api/analysis/start",
        "/api/vibe/plan",
        "/api/indicators/x/scan",
        "/api/internal/capabilities/call",
        "/api/admin/users",
        "/api/unknown",
    ]:
        response = await recovery_client.request(method, path, cookies=auth_cookies, json={})
        assert response.status_code == 503
        assert response.json()["detail"] == "恢复模式，暂不支持新研究"
        assert response.headers["retry-after"] == "60"


async def test_owner_database_history_only_no_files_or_task_restart(
    recovery_client, auth_cookies, stored_history, monkeypatch, tmp_path
):
    import server.conversations as conversations
    import server.vibe as vibe
    from server.database import get_db

    def forbidden(*args, **kwargs):
        pytest.fail("Recovery must never read external CLI files or start a task")

    monkeypatch.setattr(conversations, "_read_jsonl_messages", forbidden)
    monkeypatch.setattr(conversations, "_read_jsonl_user_content", forbidden)
    monkeypatch.setattr(vibe, "_ensure_task", forbidden)
    monkeypatch.setattr(vibe, "_start_task", forbidden)
    assert [row["id"] for row in (await recovery_client.get("/api/convs", cookies=auth_cookies)).json()] == [
        "mine"
    ]
    mine = await recovery_client.get("/api/convs/mine", cookies=auth_cookies)
    assert "mine saved research" in mine.text and "theirs" not in mine.text
    for prefix in ["/api/convs/", "/api/reports/library/", "/api/vibe/plans/"]:
        assert (await recovery_client.get(prefix + "theirs", cookies=auth_cookies)).status_code == 404
    assert (await recovery_client.get("/api/analysis/theirs/report", cookies=auth_cookies)).status_code == 404
    assert (await recovery_client.get("/api/analysis/mine/report", cookies=auth_cookies)).json()["report"][
        "summary"
    ] == "mine"
    assert (await recovery_client.get("/api/reports/library/mine", cookies=auth_cookies)).json()["report"][
        "summary"
    ] == "mine"
    reports = (await recovery_client.get("/api/convs/mine/reports", cookies=auth_cookies)).json()["reports"]
    assert len(reports) == 1 and reports[0]["kind"] == "saved"
    assert (await recovery_client.get("/api/watchlist", cookies=auth_cookies)).json()["items"] == [
        {"ticker": "600000", "name": "fixture"}
    ]
    for path in ["/api/capability-calls?view=market", "/api/convs/mine/capability-calls"]:
        calls = (await recovery_client.get(path, cookies=auth_cookies)).json()["calls"]
        assert [call["call_id"] for call in calls] == ["mine"]
        assert calls[0]["target"] == {"view": "market", "ticker": "600000"}
    assert (
        await recovery_client.get("/api/convs/theirs/capability-calls", cookies=auth_cookies)
    ).status_code == 404
    for path in [
        "/api/reports/mine/shared.html",
        "/api/reports/mine/shared.pdf",
        "/api/analysis/mine/stream",
        "/api/chat/stream",
        "/api/internal/capabilities/tools",
    ]:
        assert (await recovery_client.get(path, cookies=auth_cookies)).status_code == 404
    assert (await recovery_client.get("/api/vibe/plans", cookies=auth_cookies)).json()["plans"][0][
        "status"
    ] == "interrupted"
    assert (await recovery_client.get("/api/vibe/plans/mine", cookies=auth_cookies)).json()["plan"][
        "status"
    ] == "interrupted"
    with get_db() as db:
        assert (
            json.loads(db.execute("SELECT plan_json FROM vibe_plans WHERE id='mine'").fetchone()[0])["status"]
            == "running"
        )
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 2


async def test_market_reads_still_require_auth_and_do_not_call_models(
    recovery_client, auth_cookies, monkeypatch
):
    from types import SimpleNamespace

    import server.market.router as router

    async def fake_quote(ticker):
        return SimpleNamespace(model_dump=lambda: {"ticker": ticker, "price": 12}), False

    monkeypatch.setattr(router, "get_chain", lambda: SimpleNamespace(get_quote=fake_quote))
    assert (await recovery_client.get("/api/quote/600000")).status_code == 401
    response = await recovery_client.get("/api/quote/600000", cookies=auth_cookies)
    assert response.status_code == 200 and response.json()["price"] == 12


async def test_historical_provider_failures_are_redacted_without_database_rewrite(
    recovery_client, auth_cookies, stored_history
):
    from server.database import get_db

    opaque = "OPAQUE_PROVIDER_DIAGNOSTIC"
    key = "sk-" + "fixture" * 8
    prose = "正常研究正文：该材料需要进一步取得原文核验。"
    plan = {
        "status": "failed",
        "stage_label": opaque,
        "report_markdown": prose,
        "api_key": key,
        "activity": [{"status": "error", "summary": opaque, "detail": opaque}],
        "followups": [{"status": "failed", "error": opaque, "answer_markdown": prose}],
    }
    plan_json = json.dumps(plan, ensure_ascii=False)
    with get_db() as db:
        db.execute("UPDATE vibe_plans SET plan_json=? WHERE id='mine'", [plan_json])
        db.execute("UPDATE analysis_runs SET error=? WHERE id='mine'", [opaque])
        db.execute(
            "UPDATE saved_reports SET report_json=? WHERE id='mine'",
            [json.dumps({"summary": prose, "diagnostic": {"error": opaque, "api_key": key}})],
        )
        db.execute(
            "UPDATE messages SET content=? WHERE conv_id='mine'",
            [json.dumps({"text": prose, "error": opaque, "authorization": "Bearer " + key})],
        )
    for path in [
        "/api/vibe/plans/mine",
        "/api/vibe/plans",
        "/api/analysis/mine/report",
        "/api/reports/library/mine",
        "/api/convs/mine",
    ]:
        response = await recovery_client.get(path, cookies=auth_cookies)
        assert response.status_code == 200
        assert opaque not in response.text and key not in response.text
    viewed = (await recovery_client.get("/api/vibe/plans/mine", cookies=auth_cookies)).json()["plan"]
    assert viewed["report_markdown"] == prose
    assert viewed["followups"][0]["answer_markdown"] == prose
    with get_db() as db:
        assert db.execute("SELECT plan_json FROM vibe_plans WHERE id='mine'").fetchone()[0] == plan_json


def test_additive_new_schema_and_retired_names_survive_recovery_init(monkeypatch):
    import server.database as db
    import server.main as main

    with db.get_db() as connection:
        connection.execute(
            "CREATE TABLE research_runs(run_id TEXT PRIMARY KEY,username TEXT,conv_id TEXT,payload_json TEXT)"
        )
        connection.execute(
            "INSERT INTO research_runs VALUES('ledger-orphan','retired-admin','preserved','{\"source\":\"preserved\"}')"
        )
        connection.execute(
            "INSERT INTO conversations(id,username,title) VALUES('orphan','retired-admin','do not reassign')"
        )
    db.init_db()
    assert db.is_username_retired("retired-admin")
    with pytest.raises(HTTPException) as error:
        db.create_user("retired-admin", "test-hash")
    assert error.value.status_code == 409
    monkeypatch.setenv("ADMIN_USER", "retired-admin")
    monkeypatch.setenv("ADMIN_PASS", "not-a-real-password")
    main.ensure_configured_admin_user()
    assert not db.user_exists("retired-admin")
    with db.get_db() as connection:
        assert (
            connection.execute("SELECT title FROM conversations WHERE id='orphan'").fetchone()[0]
            == "do not reassign"
        )
        assert (
            connection.execute("SELECT payload_json FROM research_runs").fetchone()[0]
            == '{"source":"preserved"}'
        )
        with pytest.raises(Exception, match="retired_username"):
            connection.execute(
                "INSERT INTO users(username,password_hash) VALUES('retired-admin','test-hash')"
            )
        assert {row[1] for row in connection.execute("PRAGMA table_info(indicator_scan_jobs)")} >= {
            "evaluated",
            "insufficient",
            "excluded",
        }
