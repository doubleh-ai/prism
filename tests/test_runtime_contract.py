"""Owner isolation, immutable results and lifecycle behaviour across real APIs."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from server import capabilities as cap
from server.database import get_db


@pytest.fixture
def owners(test_user):
    from server.auth import create_session

    with get_db() as db:
        db.execute("INSERT INTO users(username,password_hash) VALUES('other','unused-test-hash')")
        for cid, username in (
            ("owned-a", test_user["username"]),
            ("owned-b", test_user["username"]),
            ("foreign", "other"),
        ):
            db.execute(
                "INSERT INTO conversations(id,username,skill) VALUES(?,?,'bottleneck-hunter')",
                [cid, username],
            )
    from server.research import create_research_run
    from server.research_context import build_research_context

    for run_id, user, conv in (
        ("run-one", test_user["username"], "owned-a"),
        ("run-two", test_user["username"], "owned-a"),
        ("run-sibling", test_user["username"], "owned-b"),
        ("run-foreign", "other", "foreign"),
    ):
        create_research_run(user, conv, "A report ownership test", build_research_context(run_id=run_id))
    return {"user": test_user["username"], "other_cookie": {"token": create_session("other")}}


@pytest.mark.parametrize("run_id", ["not-a-real-run", "run-foreign", "run-sibling"])
def test_report_registration_requires_matching_real_run(owners, tmp_path, run_id):
    from server.report_files import register_report_file

    source = tmp_path / "report.html"
    source.write_text("<p>Report</p>")
    with pytest.raises(HTTPException) as denied:
        register_report_file(owners["user"], "owned-a", run_id, source)
    assert denied.value.status_code == 404


async def test_files_are_bound_to_user_conversation_and_immutable_copy(
    client, auth_cookies, owners, tmp_path
):
    from server.report_files import register_report_file

    legacy = tmp_path / "shared-skill-report.html"
    legacy.write_text("<h1>Original</h1><script>fetch('/api/me')</script>")
    artifact = register_report_file(owners["user"], "owned-a", "run-one", legacy)
    legacy.write_text("<h1>Changed producer file</h1>")
    listed = await client.get("/api/convs/owned-a/reports", cookies=auth_cookies)
    assert [r["filename"] for r in listed.json()["reports"]] == [artifact["filename"]]
    assert (await client.get("/api/convs/owned-b/reports", cookies=auth_cookies)).json()["reports"] == []
    assert (await client.get("/api/convs/foreign/reports", cookies=owners["other_cookie"])).json()[
        "reports"
    ] == []
    for conv, cookie in (
        ("owned-b", auth_cookies),
        ("foreign", owners["other_cookie"]),
        ("owned-a", owners["other_cookie"]),
    ):
        assert (
            await client.get(f"/api/reports/{conv}/{artifact['filename']}", cookies=cookie)
        ).status_code == 404
    viewed = await client.get(f"/api/reports/owned-a/{artifact['filename']}", cookies=auth_cookies)
    assert viewed.status_code == 200 and "Original" in viewed.text and "Changed producer" not in viewed.text
    policy = viewed.headers["content-security-policy"]
    assert "sandbox;" in policy and "allow-same-origin" not in policy
    assert "script-src 'none'" in policy and "connect-src 'none'" in policy
    assert "frame-ancestors 'self'" in policy
    assert viewed.headers["cache-control"] == "no-store"
    # Existing files without an ownership receipt are preserved but not claimable.
    assert legacy.exists()
    assert (await client.get(f"/api/reports/owned-a/{legacy.name}", cookies=auth_cookies)).status_code == 404


async def test_report_hash_change_symlink_and_deleted_conversation_deny(
    client, auth_cookies, owners, tmp_path
):
    from server import config
    from server.report_files import register_report_file

    original = tmp_path / "report.html"
    original.write_text("<p>Owned content</p>")
    artifact = register_report_file(owners["user"], "owned-a", "run-two", original)
    stored = config.DATA_DIR / "report_files" / artifact["filename"]
    stored.write_text("<p>Different content</p>")
    assert (
        await client.get(f"/api/reports/owned-a/{artifact['filename']}", cookies=auth_cookies)
    ).status_code == 409
    stored.unlink()
    stored.symlink_to(original)
    assert (
        await client.get(f"/api/reports/owned-a/{artifact['filename']}", cookies=auth_cookies)
    ).status_code == 404
    with get_db() as db:
        db.execute("UPDATE conversations SET deleted_at=1 WHERE id='owned-a'")
    assert (
        await client.get(f"/api/reports/owned-a/{artifact['filename']}", cookies=auth_cookies)
    ).status_code == 404


async def test_missing_report_filename_is_not_reflected_as_html(client, auth_cookies, owners):
    response = await client.get(
        "/api/reports/owned-a/%3Cscript%3Eevil%3C%2Fscript%3E.html", cookies=auth_cookies
    )
    assert response.status_code in {400, 404}
    assert "<script>" not in response.text
    assert (
        await client.get("/api/reports/owned-a/%2E%2E%2Foutside.html", cookies=auth_cookies)
    ).status_code in {400, 404}


def test_report_versions_preserve_ids_unknown_values_and_prior_content(owners):
    from server.reports import save_report_snapshot

    first = save_report_snapshot(
        owners["user"],
        {
            "source": "chat",
            "source_id": "run-a",
            "conv_id": "owned-a",
            "report": {
                "id": "claim-one",
                "type": "inference",
                "confidence": None,
                "markdown": "First version",
            },
        },
    )
    repeated = save_report_snapshot(
        owners["user"], {"source": "chat", "source_id": "run-a", "report": {"markdown": "Must not overwrite"}}
    )
    second = save_report_snapshot(
        owners["user"],
        {
            "source": "chat",
            "source_id": "run-b",
            "conv_id": "owned-a",
            "report": {"markdown": "Follow-up version"},
        },
    )
    assert repeated == first
    assert second["id"] != first["id"]
    assert first["report"]["id"] == "claim-one" and first["report"]["type"] == "inference"
    assert first["report"]["confidence"] is None
    assert first["report"]["markdown"] == "First version"


async def test_analysis_can_save_after_memory_loss_and_remains_owner_scoped(client, auth_cookies, owners):
    from server import analysis_engine

    report = {
        "markdown": "Durable result",
        "_analysis_meta": {"research": {"as_of": "2026-10-21T12:00:00+08:00"}},
    }
    with get_db() as db:
        db.execute(
            "INSERT INTO analysis_runs(id,username,ticker,mode,trade_date,status,report_json,started_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            [
                "durable-analysis",
                owners["user"],
                "600519",
                "deep",
                "2026-10-21",
                "done",
                json.dumps(report),
                1.0,
                2,
            ],
        )
    analysis_engine._store.pop("durable-analysis", None)
    saved = await client.post("/api/analysis/durable-analysis/save-report", json={}, cookies=auth_cookies)
    assert saved.status_code == 200
    assert saved.json()["report"]["report"]["markdown"] == "Durable result"
    assert (
        await client.post(
            "/api/analysis/durable-analysis/save-report", json={}, cookies=owners["other_cookie"]
        )
    ).status_code == 404


async def test_snapshot_replays_exact_kline_without_new_network_call(
    client, auth_cookies, owners, monkeypatch, request
):
    from server.market import factory
    from server.market.models import KLine

    provider = SimpleNamespace(
        get_kline=AsyncMock(
            return_value=([KLine(timestamp=1792512000, open=1, high=2, low=1, close=2, volume=10)], False)
        )
    )
    monkeypatch.setattr(factory, "get_chain", lambda: provider)
    context = {"username": owners["user"], "conv_id": "owned-a", "run_id": "snapshot-run"}
    parent = cap.get_manager().admit("cli")
    request.addfinalizer(parent.release)
    request.addfinalizer(lambda: cap.revoke_run_token("snapshot-run"))
    cap.issue_run_token(owners["user"], "owned-a", "snapshot-run", parent_lease=parent)
    output = await cap.dispatch_call(
        context,
        cap.CallRequest(
            name="market_kline",
            arguments={"ticker": "600519", "period": "1w", "limit": 60},
            request_id="snapshot-call",
        ),
    )
    call_id = output["call"]["call_id"]
    path = f"/api/capabilities/calls/{call_id}"
    result = (await client.get(path, cookies=auth_cookies)).json()
    assert result["params"] == {"ticker": "600519", "period": "1w", "limit": 60}
    assert result["result"]["bars"][0]["close"] == 2
    assert result["target"]["period"] == "1w"
    assert provider.get_kline.await_count == 1
    assert (await client.get(path, cookies=owners["other_cookie"])).status_code == 404


async def test_account_watchlist_shared_with_agent_and_replace_failure_is_atomic(
    client, auth_cookies, owners
):
    from server.watchlist import get_watchlist, set_watchlist

    assert (
        await client.put(
            "/api/watchlist", json={"items": [{"ticker": "600519", "name": "Stock"}]}, cookies=auth_cookies
        )
    ).status_code == 200
    assert (await client.get("/api/watchlist", cookies=owners["other_cookie"])).json()["items"] == []
    context = {"username": owners["user"], "conv_id": "owned-a", "run_id": "watch-run"}
    result, _, _ = await cap._execute("watchlist_get", cap.EmptyArgs(), context)
    assert result["items"] == [{"ticker": "600519", "name": "Stock"}]
    with get_db() as db:
        db.execute(
            "CREATE TEMP TRIGGER reject_bad BEFORE INSERT ON user_watchlist WHEN NEW.ticker='BAD' BEGIN SELECT RAISE(ABORT,'test constraint'); END"
        )
    with pytest.raises(Exception):
        set_watchlist(owners["user"], [{"ticker": "BAD", "name": ""}])
    assert get_watchlist(owners["user"]) == result["items"]


async def test_scan_tool_uses_account_watchlist_and_all_real_conditions(owners, monkeypatch):
    from server.indicators import router, scanner
    from server.watchlist import set_watchlist

    set_watchlist(owners["user"], [{"ticker": "688001", "name": "Test"}])
    indicator = {"id": "owned-indicator", "name": "close", "dsl": {"price": "close"}, "params": {}}
    monkeypatch.setattr(router, "_load_owned", lambda _id, _user: indicator)
    monkeypatch.setattr(scanner, "start_job", lambda *args: None)
    args = cap.ScanArgs(
        indicator_id=indicator["id"],
        markets=["SH"],
        boards=["star"],
        min_turnover=12345,
        exclude_st=True,
        sort="recent_signal",
        direction="asc",
    )
    output, _, _ = await cap._execute(
        "scan_start", args, {"username": owners["user"], "conv_id": "owned-a", "run_id": "scan-run"}
    )
    assert output["config"]["tickers"] == ["688001"]
    for field in ("markets", "boards", "min_turnover", "exclude_st", "sort", "direction"):
        assert output["config"][field] == getattr(args, field)


async def test_vibe_page_and_agent_read_only_history_without_resuming_or_rewriting(owners, monkeypatch):
    from server import vibe
    from server.models import VibePlanRequest

    starts = []
    monkeypatch.setattr(vibe, "_start_task", lambda *a, **kw: starts.append((a, kw)))
    plan = vibe.build_vibe_plan(VibePlanRequest(prompt="Research a company", agent=True))
    plan.update(
        id="resumed-vibe", status="running", engine_session_id="session-one", engine_attempt_id="attempt-one"
    )
    vibe._insert_vibe_plan(owners["user"], plan)
    vibe.get_owned_plan(owners["user"], plan["id"])
    result, _, _ = await cap._execute("vibe_status", cap.IdArgs(id=plan["id"]), {"username": owners["user"]})
    assert result["plan"]["id"] == plan["id"] and starts == []
    assert result["plan"]["status"] == "running"
    plan.update(id="uncertain-vibe", engine_attempt_id="")
    vibe._insert_vibe_plan(owners["user"], plan)
    assert vibe.get_owned_plan(owners["user"], plan["id"])["status"] == "running"
    assert starts == []


async def test_vibe_cancel_requires_confirmation_and_does_not_overwrite_completion(owners, monkeypatch):
    from server import vibe
    from server.models import VibePlanRequest
    from server.vibe_engine import VibeEngineError

    plan = vibe.build_vibe_plan(VibePlanRequest(prompt="A cancellation test", agent=True))
    plan.update(
        id="cancel-vibe", status="running", engine_session_id="session-one", engine_attempt_id="attempt-one"
    )
    vibe._insert_vibe_plan(owners["user"], plan)
    monkeypatch.setattr(vibe, "engine_cancel", AsyncMock(side_effect=VibeEngineError("offline")))
    result = await vibe.cancel_owned_plan(owners["user"], plan["id"])
    assert result["cancelled"] is False and result["plan"]["status"] == "cancelling"

    async def completed_while_cancelling(_session):
        current = vibe._load_vibe_plan(plan["id"], owners["user"])
        vibe._finish_with_report(current, "# Completed report", "attempt-one")
        vibe._update_vibe_plan(owners["user"], plan["id"], current)
        with pytest.raises(HTTPException) as blocked:
            vibe.start_owned_followup(owners["user"], plan["id"], "New follow up")
        assert blocked.value.status_code == 503
        await asyncio.sleep(0)
        return {"status": "cancelled"}

    monkeypatch.setattr(vibe, "engine_cancel", completed_while_cancelling)
    result = await vibe.cancel_owned_plan(owners["user"], plan["id"])
    assert result["cancelled"] is False and result["plan"]["status"] == "complete"
    assert result["plan"]["report_markdown"] == "# Completed report"


def test_catalog_explicitly_excludes_management_and_arbitrary_access():
    names = set(cap.REGISTRY)
    assert {
        "watchlist_get",
        "indicator_create",
        "scan_cancel",
        "vibe_followup",
        "report_save",
        "research_record_claims",
        "research_record_relations",
    } <= names
    assert not any(
        word in name for name in names for word in ("admin", "deploy", "shell", "sql", "file", "fetch_url")
    )


@pytest.mark.parametrize("source", ["chat", "analysis", "vibe", "research"])
async def test_browser_cannot_preempt_server_provenance(client, auth_cookies, owners, source):
    response = await client.post(
        "/api/reports/library",
        cookies=auth_cookies,
        json={
            "source": source,
            "source_id": "run-one",
            "conv_id": "owned-a",
            "report": {"markdown": "Forged server result", "research_run_id": "run-one"},
        },
    )
    assert response.status_code == 422
    with get_db() as db:
        assert db.execute("SELECT count(*) FROM saved_reports").fetchone()[0] == 0


async def test_manual_note_cannot_mint_evidence_or_reference_foreign_conversation(
    client, auth_cookies, owners
):
    response = await client.post(
        "/api/reports/library",
        cookies=auth_cookies,
        json={
            "source": "manual",
            "conv_id": "owned-a",
            "report": {
                "markdown": "User's own note",
                "research_run_id": "run-one",
                "research_revision_id": "invented-revision",
                "research": {"as_of": "invented"},
                "claim": {"claim_id": "invented", "semantic_status": "verified"},
            },
        },
    )
    report = response.json()["report"]["report"]
    assert response.status_code == 200 and report["evidence_status"] == "user_supplied_unverified"
    assert "research" not in report and "research_run_id" not in report
    assert "invented" not in json.dumps(report)
    assert (
        await client.post(
            "/api/reports/library",
            cookies=auth_cookies,
            json={"conv_id": "foreign", "report": {"markdown": "A note"}},
        )
    ).status_code == 404


async def test_deleted_username_cannot_inherit_preserved_data(client, auth_cookies, owners, monkeypatch):
    from server import database, main

    assert database.delete_user_account(owners["user"]) is True
    assert database.user_exists(owners["user"]) is False
    assert database.is_username_retired(owners["user"]) is True
    assert (await client.get("/api/me", cookies=auth_cookies)).status_code == 401
    with pytest.raises(HTTPException) as denied:
        database.create_user(owners["user"], "different-test-hash")
    assert denied.value.status_code == 409
    with get_db() as db:
        assert (
            db.execute("SELECT count(*) FROM conversations WHERE username=?", [owners["user"]]).fetchone()[0]
            == 2
        )
    monkeypatch.setenv("ADMIN_USER", owners["user"])
    monkeypatch.setenv("ADMIN_PASS", "test-only-password-value")
    main.ensure_configured_admin_user()
    assert database.user_exists(owners["user"]) is False


def test_legacy_orphaned_username_is_reserved_without_deleting_history(owners):
    from server import database

    with get_db() as db:
        db.execute("DELETE FROM users WHERE username=?", [owners["user"]])
    database.init_db()
    assert database.is_username_retired(owners["user"])
    with pytest.raises(HTTPException):
        database.create_user(owners["user"], "test-only-new-hash")
    with get_db() as db:
        assert (
            db.execute("SELECT count(*) FROM conversations WHERE username=?", [owners["user"]]).fetchone()[0]
            == 2
        )


def test_database_guards_legacy_delete_and_recreate_paths(owners, request):
    import sqlite3

    from server import database

    parent = cap.get_manager().admit("cli")
    request.addfinalizer(parent.release)
    request.addfinalizer(lambda: cap.revoke_run_token("legacy-bridge-run"))
    cap.issue_run_token(owners["user"], "owned-a", "legacy-bridge-run", parent_lease=parent)
    with get_db() as db:
        # This is the old program's SQL path, without the new business helper.
        db.execute("DELETE FROM users WHERE username=?", [owners["user"]])
        assert (
            db.execute("SELECT revoked FROM capability_runs WHERE run_id='legacy-bridge-run'").fetchone()[0]
            == 1
        )
        for statement in (
            "INSERT INTO users(username,password_hash) VALUES(?,?)",
            "INSERT OR REPLACE INTO users(username,password_hash) VALUES(?,?)",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="retired_username"):
                db.execute(statement, [owners["user"], "test-only-hash"])
        assert (
            db.execute("SELECT count(*) FROM conversations WHERE username=?", [owners["user"]]).fetchone()[0]
            == 2
        )
    assert database.is_username_retired(owners["user"])


def test_restart_revokes_old_bridge_grants_and_preserves_completed_calls(owners, request):
    parent = cap.get_manager().admit("cli")
    request.addfinalizer(parent.release)
    request.addfinalizer(lambda: cap.revoke_run_token("old-process-run"))
    cap.issue_run_token(owners["user"], "owned-a", "old-process-run", parent_lease=parent)
    with get_db() as db:
        for call_id, status in (("inflight", "running"), ("finished", "completed")):
            db.execute(
                "INSERT INTO capability_calls(call_id,run_id,username,conv_id,request_id,capability_id,arguments_hash,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    call_id,
                    "old-process-run",
                    owners["user"],
                    "owned-a",
                    call_id,
                    "market_quote",
                    "test-hash",
                    status,
                    1,
                ],
            )
    cap.recover_interrupted_calls()
    cap.recover_interrupted_calls()
    with get_db() as db:
        assert (
            db.execute("SELECT revoked FROM capability_runs WHERE run_id='old-process-run'").fetchone()[0]
            == 1
        )
        assert (
            db.execute("SELECT status FROM capability_calls WHERE call_id='inflight'").fetchone()[0]
            == "interrupted"
        )
        assert (
            db.execute("SELECT status FROM capability_calls WHERE call_id='finished'").fetchone()[0]
            == "completed"
        )


async def test_vibe_original_and_followup_save_distinct_immutable_versions(client, auth_cookies, owners):
    from server import vibe
    from server.models import VibePlanRequest

    plan = vibe.build_vibe_plan(VibePlanRequest(prompt="Version preservation", agent=False))
    plan.update(
        id="version-vibe",
        status="complete",
        report_markdown="# Original report",
        report_research={"run_id": "original-vibe-run", "as_of": "2026-10-20T12:00:00+08:00"},
        followups=[
            {
                "id": "turn-one",
                "status": "complete",
                "answer_markdown": "# Follow-up report",
                "research": {"run_id": "followup-vibe-run", "as_of": "2026-10-21T12:00:00+08:00"},
            }
        ],
    )
    vibe._insert_vibe_plan(owners["user"], plan)
    url = "/api/vibe/plans/version-vibe/save-report"
    original = (await client.post(url, json={}, cookies=auth_cookies)).json()["report"]
    followup = (await client.post(url, json={"turn_id": "turn-one"}, cookies=auth_cookies)).json()["report"]
    assert original["id"] != followup["id"]
    assert original["report"]["markdown"] == "# Original report"
    assert followup["report"]["markdown"] == "# Follow-up report"
    assert (await client.post(url, json={"turn_id": "turn-one"}, cookies=auth_cookies)).json()[
        "report"
    ] == followup
    assert (
        await client.post(url, json={"turn_id": "turn-one"}, cookies=owners["other_cookie"])
    ).status_code == 404
    assert (
        await client.post(url, json={"markdown": "Forged browser report"}, cookies=auth_cookies)
    ).status_code == 422
