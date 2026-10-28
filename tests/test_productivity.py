"""Real isolated SQLite/ASGI checks for the optional personal Work domain."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from server.productivity import init_productivity_db, router, store
from server.productivity.models import CreateSpace, CreateTask, UpdateTask


@pytest.fixture
def work_db(monkeypatch):
    from server.database import create_session, get_db

    monkeypatch.setenv("PRISM_WORK_ENABLED", "1")
    init_productivity_db()
    with get_db() as db:
        for name in ("work-alice", "work-bob"):
            db.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", [name, "unused-test-hash"])
    return {name: {"token": create_session("work-" + name)} for name in ("alice", "bob")}


@pytest.fixture
async def work_client(work_db):
    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Requested-With": "XMLHttpRequest"},
    ) as client:
        yield client


def space_payload(**changes):
    return {"request_id": str(uuid4()), "name": "我的工作", "timezone": "Asia/Shanghai", **changes}


def task_payload(**changes):
    return {"request_id": str(uuid4()), "title": "准备方案评审", **changes}


async def space(client, cookies, **changes):
    response = await client.post("/api/work/spaces", json=space_payload(**changes), cookies=cookies)
    assert response.status_code == 200, response.text
    return response.json()["workspace"]


async def task(client, cookies, workspace, **changes):
    response = await client.post(
        f"/api/work/spaces/{workspace['id']}/tasks", json=task_payload(**changes), cookies=cookies
    )
    assert response.status_code == 200, response.text
    return response.json()["task"]


def counts():
    from server.database import get_db

    with get_db() as db:
        return {
            name: db.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
            for name in ("work_spaces", "work_tasks", "work_mutations", "work_events")
        }


def test_flag_off_creates_no_schema_or_rows(monkeypatch):
    from server.database import get_db

    monkeypatch.delenv("PRISM_WORK_ENABLED", raising=False)
    init_productivity_db()
    with get_db() as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'work_%'").fetchall()


async def test_flag_off_rejects_all_routes_without_parsing_private_payload(work_client, work_db, monkeypatch):
    monkeypatch.setenv("PRISM_WORK_ENABLED", "0")
    sid, tid = str(uuid4()), str(uuid4())
    for method, path in (
        ("GET", "/api/work/overview"),
        ("POST", "/api/work/spaces"),
        ("GET", f"/api/work/spaces/{sid}/tasks"),
        ("POST", f"/api/work/spaces/{sid}/tasks"),
        ("GET", f"/api/work/spaces/{sid}/tasks/{tid}"),
        ("PATCH", f"/api/work/spaces/{sid}/tasks/{tid}"),
    ):
        response = await work_client.request(
            method, path, content="private-not-json", cookies=work_db["alice"]
        )
        assert response.status_code == 404
        assert "private-not-json" not in response.text
    assert counts() == dict.fromkeys(counts(), 0)


async def test_overview_get_is_read_only_and_declares_unimplemented_capabilities(work_client, work_db):
    before = counts()
    for _ in range(2):
        response = await work_client.get("/api/work/overview", cookies=work_db["alice"])
        assert response.status_code == 200
        assert response.json() == {
            "workspace": None,
            "tasks": [],
            "next_cursor": None,
            "capabilities": {"tasks": True, "calendar": False, "reminders": False, "agent_actions": False},
        }
    assert counts() == before


@pytest.mark.parametrize("limit", ["0", "101", "true", "-1", "1e2"])
async def test_overview_validates_paging_even_before_space_exists(work_client, work_db, limit):
    response = await work_client.get("/api/work/overview", params={"limit": limit}, cookies=work_db["alice"])
    assert response.status_code == 422 and counts()["work_spaces"] == 0


async def test_full_task_journey_has_finite_history_and_no_user_field(work_client, work_db):
    ws = await space(work_client, work_db["alice"])
    item = await task(
        work_client,
        work_db["alice"],
        ws,
        description="先检查依赖，再同步进度",
        priority="high",
        due_at="2026-10-30T18:00:00+08:00",
        timezone="Asia/Shanghai",
    )
    assert item["due_at"] == "2026-10-30T10:00:00.000000Z" and item["version"] == 1
    path = f"/api/work/spaces/{ws['id']}/tasks/{item['id']}"
    for expected, status in ((1, "doing"), (2, "done")):
        response = await work_client.patch(
            path, json={"expected_version": expected, "status": status}, cookies=work_db["alice"]
        )
        assert response.status_code == 200
        assert response.json()["task"]["version"] == expected + 1
    detail = (await work_client.get(path, cookies=work_db["alice"])).json()
    assert detail["task"]["status"] == "done"
    assert [entry["action"] for entry in detail["history"]] == [
        "task_created",
        "task_updated",
        "task_updated",
    ]
    assert [entry["version"] for entry in detail["history"]] == [1, 2, 3]
    assert detail["history"][-1]["changed_fields"] == ["status"]
    assert not any(key in json.dumps(detail) for key in ("password_hash", "work-alice", "fingerprint"))


async def test_create_space_and_task_are_idempotent_without_duplicate_audit(work_client, work_db):
    body = space_payload()
    first = await work_client.post("/api/work/spaces", json=body, cookies=work_db["alice"])
    second = await work_client.post("/api/work/spaces", json=body, cookies=work_db["alice"])
    assert second.json() == {**first.json(), "replayed": True}
    ws = first.json()["workspace"]
    payload = task_payload()
    path = f"/api/work/spaces/{ws['id']}/tasks"
    one = await work_client.post(path, json=payload, cookies=work_db["alice"])
    two = await work_client.post(path, json=payload, cookies=work_db["alice"])
    assert two.json() == {**one.json(), "replayed": True}
    assert counts() == {"work_spaces": 1, "work_tasks": 1, "work_mutations": 2, "work_events": 2}
    conflict = await work_client.post(path, json={**payload, "title": "不同内容"}, cookies=work_db["alice"])
    assert conflict.status_code == 409
    cross_kind = await work_client.post(
        path, json=task_payload(request_id=body["request_id"]), cookies=work_db["alice"]
    )
    assert cross_kind.status_code == 409
    second_space = await work_client.post("/api/work/spaces", json=space_payload(), cookies=work_db["alice"])
    assert second_space.status_code == 409
    assert counts()["work_events"] == 2


async def test_cross_owner_paths_and_cross_space_task_reference_are_rejected(work_client, work_db):
    alice = await space(work_client, work_db["alice"])
    bob = await space(work_client, work_db["bob"])
    item = await task(work_client, work_db["alice"], alice)
    for owner, sid in (("bob", alice["id"]), ("bob", bob["id"]), ("alice", bob["id"])):
        path = f"/api/work/spaces/{sid}/tasks/{item['id']}"
        assert (await work_client.get(path, cookies=work_db[owner])).status_code == 404
        assert (
            await work_client.patch(
                path, json={"expected_version": 1, "status": "done"}, cookies=work_db[owner]
            )
        ).status_code == 404
    path = f"/api/work/spaces/{alice['id']}/tasks"
    assert (await work_client.get(path, cookies=work_db["bob"])).status_code == 404
    assert (await work_client.post(path, json=task_payload(), cookies=work_db["bob"])).status_code == 404
    assert (await work_client.get("/api/work/overview", cookies=work_db["bob"])).json()["tasks"] == []
    assert counts()["work_tasks"] == 1


async def test_auth_and_csrf_enforced_before_creation(work_client, work_db):
    assert (await work_client.get("/api/work/overview")).status_code == 401
    assert (await work_client.post("/api/work/spaces", json=space_payload())).status_code == 401
    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as unmarked:
        response = await unmarked.post("/api/work/spaces", json=space_payload(), cookies=work_db["alice"])
        assert response.status_code == 403
    assert counts()["work_spaces"] == 0


async def test_session_revoked_while_body_stream_is_read_does_not_authorize_write(
    work_client, work_db, monkeypatch
):
    from server.database import delete_session
    from server.productivity import routes

    original = routes._body

    async def revoked_body(request, model):
        body = await original(request, model)
        delete_session(work_db["alice"]["token"])
        return body

    monkeypatch.setattr(routes, "_body", revoked_body)
    response = await work_client.post("/api/work/spaces", json=space_payload(), cookies=work_db["alice"])
    assert response.status_code == 401 and counts()["work_spaces"] == 0


async def test_disabled_deleted_and_retired_owners_cannot_recreate_work(work_client, work_db):
    from server.database import delete_user_account, get_db, set_user_disabled

    ws = await space(work_client, work_db["alice"])
    item = await task(work_client, work_db["alice"], ws)
    assert set_user_disabled("work-alice", True)
    assert (await work_client.get("/api/work/overview", cookies=work_db["alice"])).status_code == 401
    with pytest.raises(HTTPException) as denied:
        store.update_task("work-alice", ws["id"], item["id"], UpdateTask(expected_version=1, status="done"))
    assert denied.value.status_code == 401
    assert delete_user_account("work-alice")
    assert counts() == dict.fromkeys(counts(), 0)
    with pytest.raises(HTTPException) as denied:
        store.create_space("work-alice", CreateSpace(**space_payload()))
    assert denied.value.status_code == 401
    with get_db() as db:
        assert db.execute("SELECT 1 FROM retired_usernames WHERE username='work-alice'").fetchone()


async def test_deleted_workspace_rejects_reads_new_writes_and_old_request_replay(work_client, work_db):
    from server.database import get_db

    body = space_payload()
    ws = (await work_client.post("/api/work/spaces", json=body, cookies=work_db["alice"])).json()["workspace"]
    payload = task_payload()
    path = f"/api/work/spaces/{ws['id']}/tasks"
    item = (await work_client.post(path, json=payload, cookies=work_db["alice"])).json()["task"]
    with get_db() as db:
        db.execute("UPDATE work_spaces SET deleted_at=1 WHERE id=?", [ws["id"]])
    for method, route, value in (
        ("GET", path, None),
        ("GET", path + "/" + item["id"], None),
        ("POST", path, payload),
        ("POST", "/api/work/spaces", body),
        ("PATCH", path + "/" + item["id"], {"expected_version": 1, "status": "done"}),
    ):
        assert (
            await work_client.request(method, route, json=value, cookies=work_db["alice"])
        ).status_code == 404
    assert counts()["work_tasks"] == 1


async def test_stale_patch_conflict_and_noop_do_not_append_events(work_client, work_db):
    ws = await space(work_client, work_db["alice"])
    item = await task(work_client, work_db["alice"], ws)
    path = f"/api/work/spaces/{ws['id']}/tasks/{item['id']}"
    noop = await work_client.patch(
        path, json={"expected_version": 1, "status": "todo"}, cookies=work_db["alice"]
    )
    assert noop.json()["task"]["version"] == 1
    assert counts()["work_events"] == 2
    assert (
        await work_client.patch(
            path, json={"expected_version": 1, "status": "doing"}, cookies=work_db["alice"]
        )
    ).status_code == 200
    assert (
        await work_client.patch(
            path, json={"expected_version": 1, "title": "过期编辑"}, cookies=work_db["alice"]
        )
    ).status_code == 409
    assert counts()["work_events"] == 3


def test_real_concurrent_creation_claims_only_once(work_db):
    from server.database import reset_connection

    ws = store.create_space("work-alice", CreateSpace(**space_payload()))["workspace"]
    body = CreateTask(**task_payload())

    def create(_):
        try:
            return store.create_task("work-alice", ws["id"], body)
        finally:
            reset_connection()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    assert sorted(value["replayed"] for value in results) == [False, True]
    assert results[0]["task"]["id"] == results[1]["task"]["id"]
    assert counts()["work_tasks"] == 1 and counts()["work_events"] == 2


def test_real_concurrent_versions_have_one_winner(work_db):
    from server.database import reset_connection

    ws = store.create_space("work-alice", CreateSpace(**space_payload()))["workspace"]
    item = store.create_task("work-alice", ws["id"], CreateTask(**task_payload()))["task"]

    def update(status):
        try:
            store.update_task(
                "work-alice", ws["id"], item["id"], UpdateTask(expected_version=1, status=status)
            )
            return 200
        except HTTPException as error:
            return error.status_code
        finally:
            reset_connection()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ["doing", "done"]))
    assert sorted(results) == [200, 409]
    assert store.task_detail("work-alice", ws["id"], item["id"])["task"]["version"] == 2
    assert counts()["work_events"] == 3


@pytest.mark.parametrize("action", ["task_created", "task_updated"])
def test_audit_failure_rolls_back_task_request_and_version(work_db, action):
    from server.database import get_db

    ws = store.create_space("work-alice", CreateSpace(**space_payload()))["workspace"]
    item = (
        store.create_task("work-alice", ws["id"], CreateTask(**task_payload()))["task"]
        if action == "task_updated"
        else None
    )
    before = counts()
    with get_db() as db:
        db.execute(
            "CREATE TRIGGER fail_work_audit BEFORE INSERT ON work_events WHEN NEW.action='"
            + action
            + "' BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic audit failure"):
        if item:
            store.update_task(
                "work-alice", ws["id"], item["id"], UpdateTask(expected_version=1, status="done")
            )
        else:
            store.create_task("work-alice", ws["id"], CreateTask(**task_payload()))
    assert counts() == before
    if item:
        assert store.task_detail("work-alice", ws["id"], item["id"])["task"] == item


async def test_paginated_lists_are_bounded_stable_and_filterable(work_client, work_db):
    ws = await space(work_client, work_db["alice"])
    items = [
        await task(work_client, work_db["alice"], ws, title=f"任务{i}", status="done" if i == 1 else "todo")
        for i in range(4)
    ]
    path = f"/api/work/spaces/{ws['id']}/tasks"
    first = (await work_client.get(path, params={"limit": "2"}, cookies=work_db["alice"])).json()
    second = (
        await work_client.get(
            path, params={"limit": "2", "before": str(first["next_cursor"])}, cookies=work_db["alice"]
        )
    ).json()
    assert [x["id"] for x in first["tasks"] + second["tasks"]] == [x["id"] for x in reversed(items)]
    assert second["next_cursor"] is None
    only_done = (await work_client.get(path, params={"status": "done"}, cookies=work_db["alice"])).json()
    assert [x["id"] for x in only_done["tasks"]] == [items[1]["id"]]
    for params in (
        {"limit": "0"},
        {"limit": "101"},
        {"limit": "true"},
        {"before": "0"},
        {"status": "unknown"},
    ):
        assert (await work_client.get(path, params=params, cookies=work_db["alice"])).status_code == 422


@pytest.mark.parametrize(
    "changes",
    [
        {"title": " "},
        {"title": 123},
        {"title": "x" * 201},
        {"title": "a\x00b"},
        {"title": "unpaired\ud800"},
        {"description": "x" * 6001},
        {"owner": "work-bob"},
        {"workspace_id": "forged"},
        {"status": "complete"},
        {"priority": "urgent"},
        {"request_id": "not-a-uuid"},
        {"timezone": "not/a/timezone"},
        {"timezone": "../../etc/passwd"},
        {"due_at": "2026-10-27"},
        {"due_at": "2026-10-27T12:00:00"},
        {"due_at": "2026-03-08T02:30:00-05:00", "timezone": "America/New_York"},
        {"due_at": "2026-07-01T12:00:00-05:00", "timezone": "America/New_York"},
        {"due_at": "2026-10-27T12:00:00+25:00"},
        {"due_at": True},
    ],
)
async def test_invalid_task_payloads_rejected_without_echo_or_mutation(work_client, work_db, changes):
    ws = await space(work_client, work_db["alice"])
    response = await work_client.post(
        f"/api/work/spaces/{ws['id']}/tasks", json=task_payload(**changes), cookies=work_db["alice"]
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "工作内容格式无效，请检查字段、长度、时间与时区"}
    assert counts()["work_tasks"] == 0 and counts()["work_events"] == 1


@pytest.mark.parametrize(
    "due_at,zone,expected",
    [
        ("2026-10-27T12:00:00Z", "America/New_York", "2026-10-27T12:00:00.000000Z"),
        ("2026-11-01T01:30:00-04:00", "America/New_York", "2026-11-01T05:30:00.000000Z"),
        ("2026-11-01T01:30:00-05:00", "America/New_York", "2026-11-01T06:30:00.000000Z"),
        ("2026-03-08T03:30:00-04:00", "America/New_York", "2026-03-08T07:30:00.000000Z"),
    ],
)
def test_explicit_utc_and_disambiguated_dst_instants(due_at, zone, expected):
    assert CreateTask(**task_payload(due_at=due_at, timezone=zone)).due_at == expected


@pytest.mark.parametrize("operation", ["create", "patch"])
@pytest.mark.parametrize(
    "due_at,zone",
    [
        ("2026-10-30T18:00:00+08:60", "Asia/Tokyo"),
        ("2026-07-01T12:00:00+00:60", "Europe/London"),
        ("2026-12-01T12:00:00-04:60", "America/New_York"),
        ("2026-12-01T12:00:00-00:60", "Atlantic/Azores"),
        ("2026-10-30T18:00:00+24:00", "UTC"),
        ("2026-10-30T18:00:00-24:00", "UTC"),
        ("2026-10-30T18:00:00+00:99", "UTC"),
    ],
)
async def test_offset_component_ranges_are_not_normalized_by_datetime(
    work_client, work_db, operation, due_at, zone
):
    ws = await space(work_client, work_db["alice"])
    path = f"/api/work/spaces/{ws['id']}/tasks"
    original = None
    if operation == "patch":
        original = await task(work_client, work_db["alice"], ws, timezone=zone)
        path += "/" + original["id"]
    before = counts()
    if original:
        response = await work_client.patch(
            path,
            json={"expected_version": 1, "due_at": due_at},
            cookies=work_db["alice"],
        )
    else:
        response = await work_client.post(
            path, json=task_payload(due_at=due_at, timezone=zone), cookies=work_db["alice"]
        )
    assert response.status_code == 422
    assert due_at not in response.text
    assert counts() == before
    if original:
        detail = (await work_client.get(path, cookies=work_db["alice"])).json()
        assert detail["task"] == original
        assert len(detail["history"]) == 1


@pytest.mark.parametrize(
    "patch",
    [
        {"expected_version": 1},
        {"expected_version": True, "status": "done"},
        {"expected_version": 1, "title": None},
        {"expected_version": 1, "title": " "},
        {"expected_version": 1, "status": None},
        {"expected_version": 1, "timezone": "bad/zone"},
        {"expected_version": 1, "owner": "work-bob"},
    ],
)
async def test_invalid_patches_do_not_modify_tasks(work_client, work_db, patch):
    ws = await space(work_client, work_db["alice"])
    item = await task(work_client, work_db["alice"], ws)
    path = f"/api/work/spaces/{ws['id']}/tasks/{item['id']}"
    assert (await work_client.patch(path, json=patch, cookies=work_db["alice"])).status_code == 422
    assert (await work_client.get(path, cookies=work_db["alice"])).json()["task"] == item


async def test_patch_due_uses_existing_zone_and_supports_explicit_clear(work_client, work_db):
    ws = await space(work_client, work_db["alice"])
    item = await task(work_client, work_db["alice"], ws, timezone="America/New_York")
    path = f"/api/work/spaces/{ws['id']}/tasks/{item['id']}"
    invalid = await work_client.patch(
        path, json={"expected_version": 1, "due_at": "2026-03-08T02:30:00-05:00"}, cookies=work_db["alice"]
    )
    assert invalid.status_code == 422
    good = await work_client.patch(
        path, json={"expected_version": 1, "due_at": "2026-11-01T01:30:00-05:00"}, cookies=work_db["alice"]
    )
    assert good.json()["task"]["due_at"] == "2026-11-01T06:30:00.000000Z"
    cleared = await work_client.patch(
        path, json={"expected_version": 2, "due_at": None}, cookies=work_db["alice"]
    )
    assert cleared.json()["task"]["due_at"] is None and cleared.json()["task"]["version"] == 3


async def test_duplicate_json_excessive_body_and_invalid_ids_are_finite_errors(work_client, work_db):
    private = "private-work-content-never-echo"
    for raw, expected in (
        ('{"title":"a","title":"' + private + '"}', 422),
        ('{"title":"' + private * 2000 + '"}', 413),
        ("[1,2,3]", 422),
        ("{" + private, 422),
    ):
        response = await work_client.post("/api/work/spaces", content=raw, cookies=work_db["alice"])
        assert response.status_code == expected and private not in response.text
    assert (
        await work_client.get("/api/work/spaces/not-an-id/tasks", cookies=work_db["alice"])
    ).status_code == 404
    assert counts()["work_spaces"] == 0


def test_schema_repeat_preserves_existing_rows_and_audit(work_db):
    ws = store.create_space("work-alice", CreateSpace(**space_payload()))["workspace"]
    item = store.create_task("work-alice", ws["id"], CreateTask(**task_payload()))["task"]
    before = counts()
    init_productivity_db()
    assert counts() == before
    assert store.task_detail("work-alice", ws["id"], item["id"])["task"] == item
