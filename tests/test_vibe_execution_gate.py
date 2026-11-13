"""The shipped policy cannot start or resume the shared legacy Vibe runtime."""

import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from vibe_history import historical_plan, insert_historical_plan

from server import capabilities as cap
from server import vibe
from server.database import get_db


def saved_plan(username, *, status="running", session="legacy-session"):
    plan = historical_plan("saved research")
    plan.update(
        id="legacy-owned",
        status=status,
        engine_session_id=session,
        engine_attempt_id="legacy-attempt",
        report_markdown="# Previously saved report" if status == "complete" else "",
    )
    insert_historical_plan(username, plan)
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


@pytest.mark.parametrize("status", ["queued", "running", "cancelling", "complete"])
async def test_history_never_resumes_fetches_or_changes_saved_state(
    client, auth_cookies, test_user, monkeypatch, status
):
    plan = saved_plan(test_user["username"], status=status)
    before = stored_rows()
    sidecar = AsyncMock(side_effect=AssertionError("history is local only"))
    for name in ("engine_create_session", "engine_send_message", "engine_get_messages", "engine_events"):
        monkeypatch.setattr(vibe, name, sidecar, raising=False)
    starts = []
    monkeypatch.setattr(vibe, "_start_task", lambda *a, **kw: starts.append((a, kw)), raising=False)
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


async def test_cancel_keeps_owned_history_without_contacting_retired_engine(test_user, monkeypatch):
    plan = saved_plan(test_user["username"])
    cancel = AsyncMock(return_value={"cancelled": True})
    messages = AsyncMock(side_effect=AssertionError("no shared history reads"))
    monkeypatch.setattr(vibe, "engine_cancel", cancel, raising=False)
    monkeypatch.setattr(vibe, "engine_get_messages", messages, raising=False)
    with pytest.raises(HTTPException) as foreign:
        await vibe.cancel_owned_plan("another-account", plan["id"])
    assert foreign.value.status_code == 404
    cancel.assert_not_awaited()
    result = await vibe.cancel_owned_plan(test_user["username"], plan["id"])
    cancel.assert_not_awaited()
    messages.assert_not_awaited()
    assert result["cancelled"] is False
    assert result["cancel_requested"] is False
    assert result["plan"]["status"] == "running"


async def test_unknown_legacy_task_is_not_falsely_marked_cancelled(test_user, monkeypatch):
    plan = saved_plan(test_user["username"], session="")
    before = stored_rows()
    cancel = AsyncMock(side_effect=AssertionError("no session to cancel"))
    monkeypatch.setattr(vibe, "engine_cancel", cancel, raising=False)
    result = await vibe.cancel_owned_plan(test_user["username"], plan["id"])
    assert result["cancelled"] is False
    assert result["cancel_requested"] is False
    assert stored_rows() == before
    cancel.assert_not_awaited()


async def test_delete_cannot_implicitly_cancel_old_task(client, auth_cookies, test_user, monkeypatch):
    plan = saved_plan(test_user["username"])
    before = stored_rows()
    cancel = AsyncMock(side_effect=AssertionError("delete is not cancellation authorization"))
    monkeypatch.setattr(vibe, "engine_cancel", cancel, raising=False)
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
    assert set(paused) == {"analysis_start", "vibe_start", "vibe_followup"}
    assert all(item["unavailable_reason"] for item in paused.values())
