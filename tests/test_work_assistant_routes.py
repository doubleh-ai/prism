"""Intent is never authority: preview, consent, replay and provider quotas."""

import json
from datetime import UTC, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

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


async def test_native_http_work_plan_keeps_purpose_quota_and_human_confirmation(
    client, auth_cookies, monkeypatch
):
    """Exercise the actual Messages parser and Work consumer, with synthetic HTTP."""
    import httpx
    from test_model_network import frame

    from server import model_network

    monkeypatch.setattr(model_network, "_network", None)
    monkeypatch.setattr(
        model_routes.config,
        "MODELS",
        {
            "synthetic-work": {
                "name": "Synthetic planner",
                "base_url": "https://api.deepseek.com/anthropic",
                "model": "synthetic-work",
                "api_key": "synthetic-work-key",
            }
        },
    )
    monkeypatch.setattr(model_routes.config, "DEFAULT_MODEL", "synthetic-work")
    model_routes.init_model_routes_db()
    route = {
        "purpose": "work_planning",
        "version": 1,
        "configured": True,
        "enabled": True,
        "allow_private_work": True,
        "daily_calls_max": 2,
        "model_id": "synthetic-work",
    }
    with get_db() as db:
        db.execute("INSERT INTO model_routes VALUES(?,?,?)", ["work_planning", 1, json.dumps(route)])
    descriptor, requests = [], []
    plan = {"actions": [{"kind": "create_task", "fields": {"title": "合成模型安排"}}]}
    raw = b"".join(
        [
            frame(
                "message_start", message={"role": "assistant", "content": [], "usage": {"input_tokens": 2}}
            ),
            frame(
                "content_block_start",
                index=0,
                content_block={
                    "type": "tool_use",
                    "id": "synthetic-tool",
                    "name": "propose_work",
                    "input": plan,
                },
            ),
            frame("content_block_stop", index=0),
            frame("message_delta", delta={"stop_reason": "tool_use"}, usage={"output_tokens": 3}),
            frame("message_stop"),
        ]
    )

    class WorkNetwork:
        def async_transport(self, **selected):
            descriptor.append(selected)

            def send(request):
                requests.append(json.loads(request.content))
                return httpx.Response(200, content=raw, headers={"content-type": "text/event-stream"})

            return httpx.MockTransport(send)

    model_network.install(WorkNetwork())
    body = {
        "request_id": str(uuid4()),
        "text": "请把这段合成想法整理成一个待办：先梳理评审重点。",
        "timezone": "Asia/Shanghai",
        "allow_model": True,
    }
    response = await client.post("/api/work/assistant/proposals", json=body, cookies=auth_cookies)
    assert response.status_code == 200, response.text
    proposal = response.json()["proposal"]
    assert proposal["source"] == "model" and proposal["state"] == "ready"
    assert descriptor == [
        {
            "purpose": "work_planning",
            "endpoint": "https://api.deepseek.com/anthropic/v1/messages",
            "model": "synthetic-work",
        }
    ]
    assert requests[0]["tools"][0]["name"] == "propose_work"
    assert "_model_purpose" not in requests[0]
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM work_tasks").fetchone()[0] == 0
        usage = dict(
            db.execute(
                "SELECT purpose,status,input_tokens,output_tokens FROM model_request_reservations"
            ).fetchone()
        )
    assert usage == {"purpose": "work_planning", "status": "complete", "input_tokens": 2, "output_tokens": 3}
    assert (
        await client.post("/api/work/assistant/proposals", json=body, cookies=auth_cookies)
    ).json() == response.json()
    assert len(descriptor) == 1
    path = f"/api/work/assistant/proposals/{proposal['id']}"
    assert (
        await client.post(path + "/confirm", json={"digest": proposal["digest"]}, cookies=auth_cookies)
    ).status_code == 200
    with get_db() as db:
        assert db.execute("SELECT title FROM work_tasks").fetchone()[0] == "合成模型安排"
    assert (
        await client.post(path + "/undo", json={"digest": proposal["digest"]}, cookies=auth_cookies)
    ).status_code == 200


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


@pytest.mark.parametrize("purpose", ["research", "work_planning"])
def test_purpose_quota_unknown_usage_not_reset_by_configuration(purpose):
    model_routes.init_model_routes_db()
    route = {
        "purpose": purpose,
        "version": 1,
        "configured": True,
        "enabled": True,
        "daily_calls_max": 1,
        "allow_private_work": True,
        "model_id": "synthetic-model",
    }
    with get_db() as db:
        db.execute("INSERT INTO model_routes VALUES(?,?,?)", [purpose, 1, json.dumps(route)])
    model_routes.reserve(
        purpose, "synthetic-owner", "request-one", "synthetic-model", expected_route_version=1
    )
    with pytest.raises(HTTPException) as repeated:
        model_routes.reserve(
            purpose, "synthetic-owner", "request-one", "synthetic-model", expected_route_version=1
        )
    assert repeated.value.status_code == 409
    route["version"] = 2
    with get_db() as db:
        db.execute("UPDATE model_routes SET version=2,payload=?", [json.dumps(route)])
    with pytest.raises(HTTPException) as exhausted:
        model_routes.reserve(
            purpose, "synthetic-owner", "request-two", "synthetic-model", expected_route_version=2
        )
    assert exhausted.value.status_code == 429
    with get_db() as db:
        row = db.execute(
            "SELECT status,input_tokens,output_tokens FROM model_request_reservations"
        ).fetchone()
        assert tuple(row) == ("unknown", None, None)
    assert "api_key" not in json.dumps(model_routes.snapshot())


@pytest.fixture
def historical_unused_model_settings():
    """Old enabled settings and both known/unknown usage remain private history."""
    model_routes.init_model_routes_db()
    day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    with get_db() as db:
        for purpose in ("feedback_triage", "suggestion_review", "validation"):
            route = {
                "purpose": purpose,
                "configured": True,
                "version": 7,
                "enabled": True,
                "model_id": model_routes.config.DEFAULT_MODEL,
                "daily_calls_max": 50,
            }
            db.execute("INSERT INTO model_routes VALUES(?,?,?)", [purpose, 7, json.dumps(route)])
            for status in ("unknown", "complete"):
                db.execute(
                    "INSERT INTO model_request_reservations VALUES(?,?,?,?,?,?,?,?,?,?)",
                    [
                        f"historical-{purpose}-{status}",
                        purpose,
                        "synthetic-owner",
                        7,
                        route["model_id"],
                        day,
                        status,
                        12 if status == "complete" else None,
                        8 if status == "complete" else None,
                        100,
                    ],
                )

    def snapshot():
        with get_db() as db:
            return (
                [tuple(row) for row in db.execute("SELECT * FROM model_routes ORDER BY purpose")],
                [
                    tuple(row)
                    for row in db.execute("SELECT * FROM model_request_reservations ORDER BY request_id")
                ],
            )

    return snapshot


async def test_admin_lists_only_live_application_purposes_without_erasing_history(
    client, auth_cookies, monkeypatch, historical_unused_model_settings
):
    from server import auth

    monkeypatch.setattr(auth, "ADMIN_USERS", {"testuser"})
    before = historical_unused_model_settings()
    response = await client.get("/api/admin/model-routes", cookies=auth_cookies)
    assert response.status_code == 200, response.text
    value = response.json()
    assert [route["purpose"] for route in value["routes"]] == ["research", "work_planning"]
    assert all(route["configured"] is False for route in value["routes"])
    assert "api_key" not in json.dumps(value)
    assert historical_unused_model_settings() == before


@pytest.mark.parametrize("purpose", ["feedback_triage", "suggestion_review", "validation"])
async def test_nonconsumer_purposes_cannot_be_saved_resolved_or_reserved(
    purpose, client, auth_cookies, monkeypatch, historical_unused_model_settings
):
    from server import auth

    monkeypatch.setattr(auth, "ADMIN_USERS", {"testuser"})
    before = historical_unused_model_settings()
    response = await client.put(
        "/api/admin/model-routes/" + purpose,
        cookies=auth_cookies,
        json={"expected_version": 7, "model_id": model_routes.config.DEFAULT_MODEL},
    )
    assert response.status_code == 422, response.text
    with pytest.raises(HTTPException) as resolved:
        model_routes.resolve(purpose)
    assert resolved.value.status_code == 422
    with pytest.raises(HTTPException) as reserved:
        model_routes.reserve(
            purpose,
            "synthetic-owner",
            "new-unsupported-request",
            model_routes.config.DEFAULT_MODEL,
            expected_route_version=7,
        )
    assert reserved.value.status_code == 422
    # The direct handler also refuses before parsing a body or touching history.
    with pytest.raises(HTTPException) as direct:
        await model_routes.save_route(
            purpose,
            type(
                "SyntheticRequest",
                (),
                {
                    "method": "PUT",
                    "cookies": auth_cookies,
                    "headers": {"X-Requested-With": "XMLHttpRequest"},
                },
            )(),
        )
    assert direct.value.status_code == 422 and direct.value.detail == "模型用途无效"
    assert historical_unused_model_settings() == before


@pytest.mark.parametrize("purpose", ["research", "work_planning"])
async def test_live_application_routes_still_save_and_drive_resolution(
    purpose, client, auth_cookies, monkeypatch
):
    from server import auth

    monkeypatch.setattr(auth, "ADMIN_USERS", {"testuser"})
    response = await client.put(
        "/api/admin/model-routes/" + purpose,
        cookies=auth_cookies,
        json={
            "expected_version": 0,
            "model_id": model_routes.config.DEFAULT_MODEL,
            "allow_private_work": purpose == "work_planning",
            "daily_calls_max": 1,
        },
    )
    assert response.status_code == 200, response.text
    model_id, _model, route = model_routes.resolve(purpose, private_work=purpose == "work_planning")
    assert model_id == response.json()["route"]["model_id"] and route["version"] == 1
    model_routes.reserve(purpose, "synthetic-owner", "one-live-request", model_id, expected_route_version=1)
    with pytest.raises(HTTPException) as exhausted:
        model_routes.reserve(
            purpose, "synthetic-owner", "two-live-request", model_id, expected_route_version=1
        )
    assert exhausted.value.status_code == 429


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
