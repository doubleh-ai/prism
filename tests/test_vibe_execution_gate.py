"""The shipped policy cannot start or resume the shared legacy Vibe runtime."""

import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from server import capabilities as cap
from server import vibe
from server.database import get_db
from server.models import VibePlanRequest


def saved_plan(username, *, status="running", session="legacy-session"):
    plan = vibe.build_vibe_plan(VibePlanRequest(prompt="saved research"))
    plan.update(
        id="legacy-owned",
        status=status,
        engine_session_id=session,
        engine_attempt_id="legacy-attempt",
        report_markdown="# Previously saved report" if status == "complete" else "",
    )
    vibe._insert_vibe_plan(username, plan)
    return plan


def stored_rows():
    with get_db() as db:
        return [tuple(row) for row in db.execute("SELECT * FROM vibe_plans ORDER BY id")]


@pytest.mark.parametrize("payload", ["not-json", "[]", "{}", '{"report_markdown":"saved text"}'])
def test_unknown_legacy_history_is_not_relabelled_complete(payload):
    plan = vibe._row_to_plan({"id": "legacy-unknown", "created_at": 1, "plan_json": payload})
    assert plan["status"] == "unknown"
    if "saved text" in payload:
        assert plan["report_markdown"] == "saved text"


@pytest.mark.parametrize("saved_body", [None, "# Previously saved original body"])
async def test_unknown_history_stays_readable_and_save_uses_only_existing_body(
    client, auth_cookies, test_user, saved_body
):
    plan = saved_plan(test_user["username"])
    payload = (
        json.dumps(
            {
                "report_markdown": saved_body,
                "followups": [{"id": "unknown-turn", "answer_markdown": "not completed"}],
            }
        )
        if saved_body
        else "invalid legacy JSON"
    )
    with get_db() as db:
        db.execute("UPDATE vibe_plans SET plan_json=? WHERE id=?", [payload, plan["id"]])
    before = stored_rows()
    detail = await client.get(f"/api/vibe/plans/{plan['id']}", cookies=auth_cookies)
    listing = await client.get("/api/vibe/plans", cookies=auth_cookies)
    assert detail.status_code == listing.status_code == 200
    assert detail.json()["plan"]["status"] == "unknown"
    assert listing.json()["plans"][0]["status"] == "unknown"
    url = f"/api/vibe/plans/{plan['id']}/save-report"
    response = await client.post(url, json={}, cookies=auth_cookies)
    if saved_body:
        assert response.status_code == 200
        snapshot = response.json()["report"]["report"]
        assert snapshot["markdown"] == saved_body
        assert snapshot["evidence_status"] == "unverified_engine_report"
        assert (
            await client.post(url, json={"turn_id": "unknown-turn"}, cookies=auth_cookies)
        ).status_code == 409
    else:
        assert response.status_code == 409
    assert stored_rows() == before


@pytest.mark.parametrize("agent", [True, False])
async def test_http_create_paused_before_writes_or_sidecar(client, auth_cookies, monkeypatch, agent):
    sidecar = AsyncMock(side_effect=AssertionError("sidecar must not be contacted"))
    monkeypatch.setattr(vibe, "engine_create_session", sidecar)
    # No runtime configuration can attest the unreviewed sidecar as safe.
    monkeypatch.setenv("PRISM_VIBE_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("PRISM_VIBE_ENGINE_URL", "http://127.0.0.1:8899")
    response = await client.post(
        "/api/vibe/plan", cookies=auth_cookies, json={"prompt": "new request", "agent": agent}
    )
    assert response.status_code == 503
    assert "隔离执行环境尚待验收" in response.json()["detail"]
    assert stored_rows() == []
    sidecar.assert_not_awaited()


@pytest.mark.parametrize("status", ["queued", "running", "cancelling", "complete"])
async def test_history_never_resumes_fetches_or_changes_saved_state(
    client, auth_cookies, test_user, monkeypatch, status
):
    plan = saved_plan(test_user["username"], status=status)
    before = stored_rows()
    sidecar = AsyncMock(side_effect=AssertionError("history is local only"))
    for name in ("engine_create_session", "engine_send_message", "engine_get_messages", "engine_events"):
        monkeypatch.setattr(vibe, name, sidecar)
    starts = []
    monkeypatch.setattr(vibe, "_start_task", lambda *a, **kw: starts.append((a, kw)))
    detail = await client.get(f"/api/vibe/plans/{plan['id']}", cookies=auth_cookies)
    listing = await client.get("/api/vibe/plans", cookies=auth_cookies)
    result, _, summary = await cap._execute(
        "vibe_status", cap.IdArgs(id=plan["id"]), {"username": test_user["username"]}
    )
    prior, _, list_summary = await cap._execute(
        "vibe_list", cap.ListArgs(limit=30), {"username": test_user["username"]}
    )
    assert detail.json()["plan"]["status"] == status
    assert listing.json()["plans"][0]["id"] == plan["id"]
    assert result["plan"]["status"] == status
    assert result["history_only"] is True
    assert result["execution"]["execution_available"] is False
    assert "不是旧引擎实时状态" in summary
    assert prior["history_only"] is True
    assert prior["execution"]["execution_available"] is False
    assert [row["id"] for row in prior["plans"]] == [plan["id"]]
    assert "已保存主题研究" in list_summary
    assert stored_rows() == before
    assert starts == []
    sidecar.assert_not_awaited()
    with pytest.raises(HTTPException) as foreign:
        vibe.get_owned_plan("another-account", plan["id"])
    assert foreign.value.status_code == 404


async def test_followup_rejects_without_changing_report_or_clock(client, auth_cookies, test_user):
    plan = saved_plan(test_user["username"], status="complete")
    before = stored_rows()
    response = await client.post(
        f"/api/vibe/plans/{plan['id']}/follow-up",
        cookies=auth_cookies,
        json={"question": "continue research"},
    )
    assert response.status_code == 503
    assert stored_rows() == before
    with pytest.raises(HTTPException) as foreign:
        vibe.start_owned_followup("another-account", plan["id"], "continue")
    assert foreign.value.status_code == 404


async def test_all_internal_launch_and_monitor_paths_are_closed():
    for callback in (
        lambda: vibe._start_task("p", "user"),
        lambda: vibe._ensure_task({"id": "p", "status": "running"}, "user"),
    ):
        with pytest.raises(HTTPException) as blocked:
            callback()
        assert blocked.value.status_code == 503
    for callback in (
        lambda: vibe._run_official_research("p", "user", resume=True),
        lambda: vibe._monitor_attempt("p", "user", "session", "attempt"),
        lambda: vibe._answer_for_attempt("session", "attempt"),
        lambda: cap._execute("vibe_start", cap.VibeArgs(prompt="new"), {"username": "user"}),
        lambda: cap._execute("vibe_followup", cap.FollowupArgs(id="p", question="new"), {"username": "user"}),
    ):
        with pytest.raises(HTTPException) as blocked:
            await callback()
        assert blocked.value.status_code == 503


async def test_cancel_is_explicit_owned_fixed_operation_without_reading_messages(test_user, monkeypatch):
    plan = saved_plan(test_user["username"])
    cancel = AsyncMock(return_value={"cancelled": True})
    messages = AsyncMock(side_effect=AssertionError("no shared history reads"))
    monkeypatch.setattr(vibe, "engine_cancel", cancel)
    monkeypatch.setattr(vibe, "engine_get_messages", messages)
    with pytest.raises(HTTPException) as foreign:
        await vibe.cancel_owned_plan("another-account", plan["id"])
    assert foreign.value.status_code == 404
    cancel.assert_not_awaited()
    result = await vibe.cancel_owned_plan(test_user["username"], plan["id"])
    cancel.assert_awaited_once_with("legacy-session")
    messages.assert_not_awaited()
    assert result["cancelled"] is True


async def test_unknown_legacy_task_is_not_falsely_marked_cancelled(test_user, monkeypatch):
    plan = saved_plan(test_user["username"], session="")
    before = stored_rows()
    cancel = AsyncMock(side_effect=AssertionError("no session to cancel"))
    monkeypatch.setattr(vibe, "engine_cancel", cancel)
    result = await vibe.cancel_owned_plan(test_user["username"], plan["id"])
    assert result["cancelled"] is False
    assert result["cancel_requested"] is False
    assert stored_rows() == before
    cancel.assert_not_awaited()


@pytest.mark.parametrize("payload", [None, [], ["cancelled"], "cancelled"])
async def test_malformed_cancel_response_keeps_unconfirmed_history(test_user, monkeypatch, payload):
    plan = saved_plan(test_user["username"])
    cancel = AsyncMock(return_value=payload)
    monkeypatch.setattr(vibe, "engine_cancel", cancel)
    result = await vibe.cancel_owned_plan(test_user["username"], plan["id"])
    cancel.assert_awaited_once_with("legacy-session")
    assert result["cancelled"] is False
    assert result["cancel_requested"] is True
    assert result["plan"]["status"] == "cancelling"
    assert result["plan"]["engine_attempt_id"] == "legacy-attempt"


async def test_delete_cannot_implicitly_cancel_old_task(client, auth_cookies, test_user, monkeypatch):
    plan = saved_plan(test_user["username"])
    before = stored_rows()
    cancel = AsyncMock(side_effect=AssertionError("delete is not cancellation authorization"))
    monkeypatch.setattr(vibe, "engine_cancel", cancel)
    response = await client.delete(f"/api/vibe/plans/{plan['id']}", cookies=auth_cookies)
    assert response.status_code == 409
    assert stored_rows() == before
    cancel.assert_not_awaited()


async def test_engine_status_reports_unavailable_without_a_health_request(client, auth_cookies):
    response = await client.get("/api/vibe/engine", cookies=auth_cookies)
    assert response.status_code == 200
    assert response.json()["status"] == "execution_unavailable"
    assert response.json()["execution_available"] is False
    assert response.json()["history_available"] is True
    paused = {item["id"]: item for item in cap.capability_catalog() if item.get("available") is False}
    assert set(paused) == {"vibe_start", "vibe_followup"}
    assert all(item["unavailable_reason"] for item in paused.values())
