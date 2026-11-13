"""Real Work/HTTP/SQLite flow with a synthetic Responses HTTP provider only."""

import json
from uuid import uuid4

import httpx
import pytest


async def test_work_responses_plan_preview_confirm_undo_and_local_configuration(
    client, auth_cookies, test_user, monkeypatch
):
    from server import config, health, model_routes
    from server.agent import provider_openai
    from server.database import get_db
    from server.productivity import calendar, notifications, projects, store
    from server.productivity.models import CreateSpace, CreateTask

    for init in (
        store.init_productivity_db,
        projects.init_projects_db,
        calendar.init_calendar_db,
        notifications.init_notifications_db,
    ):
        init()
    cfg = {
        "protocol": "openai_responses",
        "name": "合成 GPT",
        "model": "synthetic-model",
        "base_url": "https://api.openai.com/v1",
        "api_key": "synthetic-isolated-responses",
    }
    models = {"openai": cfg, "unconfigured": {**cfg, "model": ""}}
    monkeypatch.setattr(config, "MODELS", models)
    monkeypatch.setattr(config, "DEFAULT_MODEL", "openai")
    model_routes.init_model_routes_db()
    space = store.create_space(
        test_user["username"], CreateSpace(request_id=str(uuid4()), name="合成工作区")
    )["workspace"]
    store.create_task(
        test_user["username"],
        space["id"],
        CreateTask(request_id=str(uuid4()), title="private-existing-record-fixture"),
    )
    with get_db() as db:
        route = {
            "purpose": "work_planning",
            "version": 1,
            "configured": True,
            "enabled": True,
            "allow_private_work": True,
            "model_id": "openai",
            "daily_calls_max": 1,
        }
        db.execute("INSERT INTO model_routes VALUES(?,?,?)", ["work_planning", 1, json.dumps(route)])
    requests = []

    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        assert body["input"] == [{"role": "user", "content": "整理一个新的合成项目"}]
        assert "private-existing-record-fixture" not in request.content.decode()
        assert body["tools"][0]["name"] == "propose_work" and body["tools"][0]["strict"] is False
        item = {
            "id": "fc_fixture",
            "type": "function_call",
            "call_id": "call_fixture",
            "name": "propose_work",
            "arguments": json.dumps(
                {"actions": [{"kind": "create_project", "fields": {"name": "合成新项目"}}]}
            ),
            "status": "completed",
        }
        events = [
            ("response.created", {"response": {"id": "resp_fixture"}}),
            (
                "response.output_item.added",
                {"output_index": 0, "item": {**item, "arguments": "", "status": "in_progress"}},
            ),
            ("response.output_item.done", {"output_index": 0, "item": item}),
            (
                "response.completed",
                {
                    "response": {
                        "id": "resp_fixture",
                        "status": "completed",
                        "output": [item],
                        "usage": {"input_tokens": 17, "output_tokens": 23},
                    }
                },
            ),
        ]
        wire = "".join("data: " + json.dumps({"type": kind, **fields}) + "\n\n" for kind, fields in events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire.encode())

    original = provider_openai.OpenAIResponsesProvider
    monkeypatch.setattr(
        provider_openai, "OpenAIResponsesProvider", lambda: original(transport=httpx.MockTransport(handler))
    )
    body = {"request_id": str(uuid4()), "text": "整理一个新的合成项目", "allow_model": True}
    result = await client.post("/api/work/assistant/proposals", json=body, cookies=auth_cookies)
    assert result.status_code == 200, result.text
    proposal = result.json()["proposal"]
    assert proposal["state"] == "ready" and proposal["source"] == "model" and len(requests) == 1
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM work_projects").fetchone()[0] == 0
        usage = db.execute(
            "SELECT status,input_tokens,output_tokens FROM model_request_reservations"
        ).fetchone()
        assert tuple(usage) == ("complete", 17, 23)
    path = f"/api/work/assistant/proposals/{proposal['id']}"
    confirmed = await client.post(
        path + "/confirm", json={"digest": proposal["digest"]}, cookies=auth_cookies
    )
    assert confirmed.status_code == 200, confirmed.text
    assert (
        await client.post(path + "/undo", json={"digest": proposal["digest"]}, cookies=auth_cookies)
    ).status_code == 200
    assert (
        await client.post("/api/work/assistant/proposals", json=body, cookies=auth_cookies)
    ).status_code == 200
    assert len(requests) == 1
    snapshot = model_routes.snapshot()
    assert snapshot["models"][0]["endpoint_host"] == "openai"
    assert snapshot["models"][0]["protocol"] == "openai_responses"
    public_models = (await client.get("/api/models")).json()
    assert [m["id"] for m in public_models if m["default"]] == ["openai"]
    assert {m["id"]: m["configured"] for m in public_models} == {"openai": True, "unconfigured": False}
    assert {m["id"]: m["configured"] for m in health._model_checks()} == {
        "openai": True,
        "unconfigured": False,
    }
    assert cfg["api_key"] not in json.dumps([snapshot, public_models])
    with pytest.raises(Exception) as missing:
        model_routes.resolve("work_planning", explicit_model="unconfigured", private_work=True)
    assert getattr(missing.value, "status_code", None) == 503
