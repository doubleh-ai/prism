"""Personal project ownership, concurrent writes and deterministic reminders."""

import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.database import create_session, get_db
from server.productivity import calendar, notifications, projects, store
from server.productivity.calendar import EventCreate, EventUpdate
from server.productivity.models import CreateSpace
from server.work_lifecycle import Manager


@pytest.fixture
async def workspace(monkeypatch):
    monkeypatch.setenv("PRISM_WORK_ENABLED", "1")
    store.init_productivity_db()
    calendar.init_calendar_db()
    projects.init_projects_db()
    notifications.init_notifications_db()
    sessions = {}
    for owner in ("project-alice", "project-bob"):
        with get_db() as db:
            db.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", [owner, "synthetic-only"])
        sessions[owner] = {"token": create_session(owner)}
        store.create_space(owner, CreateSpace(request_id=str(uuid4()), name="合成工作区"))
    app = FastAPI()
    app.include_router(projects.router)
    app.include_router(notifications.router)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Requested-With": "XMLHttpRequest", "X-CSRF-Token": "synthetic-test-csrf"},
        cookies={"csrf_token": "synthetic-test-csrf"},
    ) as client:
        yield client, sessions


async def test_project_task_idempotency_and_owner_isolation(workspace):
    client, sessions = workspace
    alice, bob = sessions.values()
    body = {"request_id": str(uuid4()), "name": "方案评审"}
    first = await client.post("/api/work/projects", json=body, cookies=alice)
    assert first.status_code == 200
    project = first.json()["project"]
    again = await client.post("/api/work/projects", json=body, cookies=alice)
    assert again.json()["replayed"] and again.json()["project"] == project
    assert (await client.get("/api/work/projects", cookies=bob)).json()["projects"] == []
    path = f"/api/work/projects/{project['id']}/tasks"
    assert (await client.get(path, cookies=bob)).status_code == 404
    task = {"request_id": str(uuid4()), "title": "合成里程碑", "due_at": None}
    response = await client.post(path, json=task, cookies=alice)
    assert response.status_code == 200, response.text
    replay = await client.post(path, json=task, cookies=alice)
    assert replay.json()["replayed"]
    rows = (await client.get(path, cookies=alice)).json()["tasks"]
    assert len(rows) == 1 and rows[0]["id"] == replay.json()["task"]["id"]
    assert (
        await client.post(path, json={**task, "request_id": str(uuid4())}, cookies=bob)
    ).status_code == 404


async def test_project_conflicts_archive_and_account_binding(workspace):
    client, sessions = workspace
    alice = sessions["project-alice"]
    response = await client.post(
        "/api/work/projects", json={"request_id": str(uuid4()), "name": "合成项目"}, cookies=alice
    )
    project = response.json()["project"]
    path = f"/api/work/projects/{project['id']}"
    update = {"request_id": str(uuid4()), "expected_version": 1, "name": "已归档项目", "status": "archived"}
    assert (await client.patch(path, json=update, cookies=alice)).status_code == 200
    assert (await client.patch(path, json=update, cookies=alice)).json()["replayed"]
    assert (
        await client.patch(path, json={**update, "request_id": str(uuid4())}, cookies=alice)
    ).status_code == 409
    assert (
        await client.post(
            path + "/tasks", json={"request_id": str(uuid4()), "title": "不应写入"}, cookies=alice
        )
    ).status_code == 409
    assert (
        await client.get("/api/work/projects", cookies=alice, headers={"X-Prism-Account": "project-bob"})
    ).status_code == 409
    assert (await client.get("/api/work/projects")).status_code == 401


async def test_reminder_ticks_deduplicate_and_revoke_stale_event(workspace):
    client, sessions = workspace
    now = int(time.time())
    start = datetime.fromtimestamp(now, UTC) + timedelta(minutes=10)
    payload = {
        "request_id": str(uuid4()),
        "title": "合成会议",
        "start_at": start.isoformat(),
        "end_at": (start + timedelta(hours=1)).isoformat(),
        "timezone": "UTC",
    }
    first = calendar.save_event("project-alice", EventCreate(**payload))["event"]
    assert notifications.collect_due(now=now) == 1
    assert notifications.collect_due(now=now) == 0
    items = (await client.get("/api/work/notifications", cookies=sessions["project-alice"])).json()[
        "notifications"
    ]
    assert len(items) == 1 and items[0]["title"] == "合成会议"
    path = f"/api/work/notifications/{items[0]['id']}/read"
    assert (await client.post(path, cookies=sessions["project-bob"])).status_code == 404
    assert (await client.post(path, cookies=sessions["project-alice"])).status_code == 200
    assert (await client.get("/api/work/notifications", cookies=sessions["project-bob"])).json()[
        "notifications"
    ] == []
    calendar.save_event(
        "project-alice",
        EventUpdate(**{**payload, "request_id": str(uuid4()), "expected_version": 1, "cancelled": True}),
        first["id"],
    )
    assert (await client.get("/api/work/notifications", cookies=sessions["project-alice"])).json()[
        "notifications"
    ] == []
    assert notifications.collect_due(now=now) == 0


async def test_disabled_work_rejects_new_routes(workspace, monkeypatch):
    client, sessions = workspace
    monkeypatch.setenv("PRISM_WORK_ENABLED", "0")
    for path in ("/api/work/projects", "/api/work/notifications"):
        assert (await client.get(path, cookies=sessions["project-alice"])).status_code == 404
    assert notifications.collect_due() == 0


async def test_scheduler_respects_drain_and_releases_idle_work(workspace, monkeypatch):
    manager = Manager()
    calls = []
    monkeypatch.setattr(notifications, "collect_due", lambda: calls.append(1) or 1)
    scheduler = notifications.ReminderScheduler(manager)
    assert scheduler.tick() == 1
    assert manager.observe()["blocked"] == 0
    manager.begin_drain(manager.ticket())
    assert scheduler.tick() == 0 and calls == [1]
    await manager.wait_drained(timeout=1)
    manager.close_development()
