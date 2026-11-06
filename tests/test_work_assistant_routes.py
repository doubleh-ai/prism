"""Intent is never authority: preview, consent, replay and provider quotas."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException

from server import model_routes
from server.database import get_db
from server.productivity.assistant import parse_routine


@pytest.fixture(autouse=True)
def work_schema():
    from server.productivity import calendar, notifications, projects, store

    store.init_productivity_db()
    calendar.init_calendar_db()
    projects.init_projects_db()
    notifications.init_notifications_db()


def test_routine_intents_preserve_wall_time_and_reject_ambiguous_writes():
    now = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    plan = parse_routine("每周一上午10点提醒我开项目会，1小时", "Asia/Shanghai", now=now)
    assert plan.actions[0].fields["start_at"].startswith("2026-11-02T02:00")
    assert plan.actions[0].fields["recurrence"]["count"] == 12
    assert parse_routine("不要创建任务买东西", "Asia/Shanghai", now=now) is None
    assert parse_routine("明天讨论一下", "Asia/Shanghai", now=now) is None
    assert parse_routine("今天上午7点提醒我开项目会", "Asia/Shanghai", now=now).clarification


async def test_preview_changes_nothing_confirmation_replays_one_object(client, auth_cookies):
    body = {"request_id": str(uuid4()), "text": "创建任务 整理合成评审材料", "timezone": "Asia/Shanghai"}
    response = await client.post("/api/work/assistant/proposals", json=body, cookies=auth_cookies)
    assert response.status_code == 200, response.text
    proposal = response.json()["proposal"]
    assert proposal["state"] == "ready" and proposal["source"] == "routine"
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM work_tasks").fetchone()[0] == 0
    replay = await client.post("/api/work/assistant/proposals", json=body, cookies=auth_cookies)
    assert replay.json() == response.json()
    path = f"/api/work/assistant/proposals/{proposal['id']}/confirm"
    assert (await client.post(path, json={"digest": "a" * 64}, cookies=auth_cookies)).status_code == 409
    confirmed = await client.post(path, json={"digest": proposal["digest"]}, cookies=auth_cookies)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["proposal"]["state"] == "completed"
    again = await client.post(path, json={"digest": proposal["digest"]}, cookies=auth_cookies)
    assert again.json() == confirmed.json()
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM work_tasks").fetchone()[0] == 1
    by_request = await client.get(
        "/api/work/assistant/proposals/by-request/" + body["request_id"], cookies=auth_cookies
    )
    assert by_request.json()["proposal"]["id"] == proposal["id"]
    undo_path = f"/api/work/assistant/proposals/{proposal['id']}/undo"
    undone = await client.post(undo_path, json={"digest": proposal["digest"]}, cookies=auth_cookies)
    assert undone.status_code == 200 and undone.json()["proposal"]["state"] == "undone"
    assert (
        await client.post(undo_path, json={"digest": proposal["digest"]}, cookies=auth_cookies)
    ).json() == undone.json()
    with get_db() as db:
        assert db.execute("SELECT status FROM work_tasks").fetchone()[0] == "cancelled"
    assert (await client.get(f"/api/work/assistant/proposals/{proposal['id']}")).status_code == 401


def test_purpose_quota_unknown_usage_not_reset_by_configuration(monkeypatch):
    model_routes.init_model_routes_db()
    route = {
        "purpose": "work_planning",
        "version": 1,
        "configured": True,
        "enabled": True,
        "daily_calls_max": 1,
        "allow_private_work": True,
        "model_id": "synthetic-model",
    }
    with get_db() as db:
        db.execute("INSERT INTO model_routes VALUES(?,?,?)", ["work_planning", 1, json.dumps(route)])
    model_routes.reserve(
        "work_planning", "synthetic-owner", "request-one", "synthetic-model", expected_route_version=1
    )
    with pytest.raises(HTTPException) as repeated:
        model_routes.reserve(
            "work_planning", "synthetic-owner", "request-one", "synthetic-model", expected_route_version=1
        )
    assert repeated.value.status_code == 409
    route["version"] = 2
    with get_db() as db:
        db.execute("UPDATE model_routes SET version=2,payload=?", [json.dumps(route)])
    with pytest.raises(HTTPException) as exhausted:
        model_routes.reserve(
            "work_planning", "synthetic-owner", "request-two", "synthetic-model", expected_route_version=2
        )
    assert exhausted.value.status_code == 429
    with get_db() as db:
        row = db.execute(
            "SELECT status,input_tokens,output_tokens FROM model_request_reservations"
        ).fetchone()
        assert tuple(row) == ("unknown", None, None)
    assert "api_key" not in json.dumps(model_routes.snapshot())


async def test_model_planner_only_proposes_then_requires_confirmation(client, auth_cookies, monkeypatch):
    from server.productivity import assistant, projects

    projects.init_projects_db()

    called = []

    async def fake_plan(owner, proposal_id, body):
        called.append(body.text)
        return assistant.ModelPlan(
            actions=[assistant.Action(kind="create_project", fields={"name": "合成排期项目"})]
        )

    monkeypatch.setattr(assistant, "_model_plan", fake_plan)
    body = {"request_id": str(uuid4()), "text": "整理这个想法成为一个项目", "allow_model": False}
    local = await client.post("/api/work/assistant/proposals", json=body, cookies=auth_cookies)
    assert local.status_code == 200 and local.json()["proposal"]["state"] == "clarification"
    assert not called
    body.update(request_id=str(uuid4()), allow_model=True)
    response = await client.post("/api/work/assistant/proposals", json=body, cookies=auth_cookies)
    assert response.status_code == 200 and response.json()["proposal"]["state"] == "ready"
    assert called == [body["text"]]
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM work_projects").fetchone()[0] == 0
    proposal = response.json()["proposal"]
    assert (
        await client.post(
            f"/api/work/assistant/proposals/{proposal['id']}/confirm",
            json={"digest": proposal["digest"]},
            cookies=auth_cookies,
        )
    ).status_code == 200
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM work_projects").fetchone()[0] == 1
