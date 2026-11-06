"""Real owner-scoped edits, atomic arrangements and concrete calendar exceptions."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException

from server.database import create_session, get_db
from server.productivity import assistant, assistant_changes, calendar, notifications, projects, store
from server.productivity.models import CreateSpace, CreateTask, UpdateTask

NOW = datetime(2026, 11, 3, 1, tzinfo=UTC)


@pytest.fixture
def accounts(monkeypatch):
    store.init_productivity_db()
    calendar.init_calendar_db()
    projects.init_projects_db()
    notifications.init_notifications_db()
    owners = {}
    for name in ("edit-alice", "edit-bob"):
        with get_db() as db:
            db.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", [name, "synthetic-only"])
        space = store.create_space(name, CreateSpace(request_id=str(uuid4()), name="合成工作区"))["workspace"]
        owners[name] = {"cookies": {"token": create_session(name)}, "space": space["id"]}
    parse = assistant_changes.parse
    monkeypatch.setattr(assistant_changes, "parse", lambda text, zone: parse(text, zone, now=NOW))
    return owners


def task(accounts, owner="edit-alice", **extra):
    return store.create_task(
        owner, accounts[owner]["space"], CreateTask(request_id=str(uuid4()), title="整理材料", **extra)
    )["task"]


def event(owner="edit-alice", repeat=True, title="周会"):
    return calendar.save_event(
        owner,
        calendar.EventCreate(
            request_id=str(uuid4()),
            title=title,
            start_at="2026-11-04T02:00:00Z",
            end_at="2026-11-04T03:00:00Z",
            timezone="Asia/Shanghai",
            recurrence={"frequency": "daily", "count": 3} if repeat else None,
        ),
    )["event"]


async def propose(client, cookies, text, *, allow_model=False):
    response = await client.post(
        "/api/work/assistant/proposals",
        cookies=cookies,
        json={
            "request_id": str(uuid4()),
            "text": text,
            "timezone": "Asia/Shanghai",
            "allow_model": allow_model,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["proposal"]


async def apply(client, cookies, p, method="confirm"):
    return await client.post(
        f"/api/work/assistant/proposals/{p['id']}/{method}", cookies=cookies, json={"digest": p["digest"]}
    )


async def test_task_local_match_is_owner_scoped_preview_only_and_undoable(client, accounts):
    alice, bob = accounts.values()
    a = task(accounts)
    b = task(accounts, "edit-bob")
    p = await propose(client, alice["cookies"], "把任务「整理材料」标为已完成")
    assert p["state"] == "ready" and p["source"] == "routine"
    action = p["actions"][0]
    assert action["kind"] == "update_task" and action["target"]["id"] == a["id"]
    assert action["before"]["status"] == "todo" and action["fields"]["status"] == "done"
    assert store.task_detail("edit-alice", alice["space"], a["id"])["task"]["status"] == "todo"
    assert (await apply(client, bob["cookies"], p)).status_code == 404
    first = await apply(client, alice["cookies"], p)
    assert first.status_code == 200, first.text
    assert (await apply(client, alice["cookies"], p)).json() == first.json()
    assert store.task_detail("edit-bob", bob["space"], b["id"])["task"]["version"] == 1
    assert (await apply(client, alice["cookies"], p, "undo")).status_code == 200
    restored = store.task_detail("edit-alice", alice["space"], a["id"])["task"]
    assert restored["status"] == "todo" and restored["version"] == 3
    assert (await apply(client, alice["cookies"], p, "undo")).json()["proposal"]["state"] == "undone"


async def test_duplicate_or_missing_targets_clarify_without_false_fallback(client, accounts, monkeypatch):
    alice = accounts["edit-alice"]
    task(accounts)
    task(accounts)

    async def forbidden(*a, **k):
        raise AssertionError("no implicit provider request")

    monkeypatch.setattr(assistant, "_model_plan", forbidden)
    p = await propose(client, alice["cookies"], "把任务「整理材料」设为高优先级")
    assert p["state"] == "clarification" and "多个" in p["clarification"] and not p["actions"]
    task(accounts, "edit-bob", description="not visible")
    p = await propose(client, alice["cookies"], "把任务「只有别人的任务」标为已完成")
    assert p["state"] == "clarification"
    with get_db() as db:
        assert not db.execute("SELECT 1 FROM work_tasks WHERE status='done'").fetchone()


async def test_confirmation_and_undo_refuse_concurrent_edit(client, accounts):
    a = accounts["edit-alice"]
    t = task(accounts)
    p = await propose(client, a["cookies"], "把任务「整理材料」标为已完成")
    store.update_task("edit-alice", a["space"], t["id"], UpdateTask(expected_version=1, priority="high"))
    assert (await apply(client, a["cookies"], p)).status_code == 409
    assert store.task_detail("edit-alice", a["space"], t["id"])["task"]["status"] == "todo"
    p = await propose(client, a["cookies"], "把任务「整理材料」标为已完成")
    assert (await apply(client, a["cookies"], p)).status_code == 200
    store.update_task(
        "edit-alice", a["space"], t["id"], UpdateTask(expected_version=3, description="另一设备修改")
    )
    assert (await apply(client, a["cookies"], p, "undo")).status_code == 409


async def test_project_changes_and_inverse_use_real_versioned_contract(client, accounts):
    a = accounts["edit-alice"]
    original = projects.save_project(
        "edit-alice", projects.ProjectCreate(request_id=str(uuid4()), name="发布")
    )["project"]
    p = await propose(client, a["cookies"], "把项目「发布」标为归档")
    assert p["actions"][0]["kind"] == "update_project"
    r = await apply(client, a["cookies"], p)
    assert (
        r.status_code == 200
        and r.json()["proposal"]["results"][0]["result"]["project"]["status"] == "archived"
    )
    assert (await apply(client, a["cookies"], p, "undo")).status_code == 200
    with get_db() as db:
        assert projects._project(db, "edit-alice", original["id"])["status"] == "active"


async def test_move_one_occurrence_keeps_others_and_exports_actual_duration(client, accounts):
    a = accounts["edit-alice"]
    e = event()
    before = calendar.agenda("edit-alice", "2026-11-03T00:00:00Z", "2026-11-08T00:00:00Z")["occurrences"]
    p = await propose(client, a["cookies"], "把明天的周会改到下午3点")
    action = p["actions"][0]
    assert (
        action["kind"] == "update_occurrence"
        and action["fields"]["start_at"] == "2026-11-04T07:00:00.000000Z"
    )
    assert action["fields"]["end_at"] == "2026-11-04T08:00:00.000000Z"
    assert (await apply(client, a["cookies"], p)).status_code == 200
    after = calendar.agenda("edit-alice", "2026-11-03T00:00:00Z", "2026-11-08T00:00:00Z")["occurrences"]
    assert after[0]["start_at"] == action["fields"]["start_at"] and after[1:] == before[1:]
    export = await client.get(f"/api/work/calendar/{e['id']}.ics", cookies=a["cookies"])
    assert export.status_code == 200 and export.text.count("BEGIN:VEVENT") == 2
    assert "DTSTART:20261104T070000Z" in export.text and "DTEND:20261104T080000Z" in export.text
    assert "RECURRENCE-ID:20261104T020000Z" in export.text
    assert export.text.count("UID:" + e["id"] + "@prism-work") == 2
    assert (await apply(client, a["cookies"], p, "undo")).status_code == 200
    restored = calendar.agenda("edit-alice", "2026-11-03T00:00:00Z", "2026-11-08T00:00:00Z")["occurrences"]
    assert [r["start_at"] for r in restored] == [r["start_at"] for r in before]
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM work_calendar_instance_edits").fetchone()[0] == 0


async def test_direct_occurrence_end_time_conflicts_and_reminder_versions(client, accounts):
    a = accounts["edit-alice"]
    e = event()
    other = calendar.save_event(
        "edit-alice",
        calendar.EventCreate(
            request_id=str(uuid4()),
            title="冲突安排",
            start_at="2026-11-04T07:30:00Z",
            end_at="2026-11-04T08:30:00Z",
            timezone="Asia/Shanghai",
        ),
    )["event"]
    original = calendar.agenda("edit-alice", "2026-11-04T00:00:00Z", "2026-11-05T00:00:00Z")["occurrences"][0]
    notifications.collect_due(now=datetime(2026, 11, 4, 1, 50, tzinfo=UTC).timestamp())
    path = f"/api/work/calendar/{e['id']}/occurrences/{original['occurrence_id']}"
    body = {
        "request_id": str(uuid4()),
        "expected_version": 1,
        "expected_series_version": 1,
        "start_at": "2026-11-04T07:00:00Z",
        "end_at": "2026-11-04T09:00:00Z",
    }
    first = await client.put(path, json=body, cookies=a["cookies"])
    assert first.status_code == 200, first.text
    assert first.json()["conflicts"][0]["id"] == other["id"]
    assert (await client.put(path, json=body, cookies=a["cookies"])).json()["replayed"]
    old = await client.get("/api/work/notifications", cookies=a["cookies"])
    assert all(n["due_at"] != "2026-11-04T02:00:00.000000Z" for n in old.json()["notifications"])
    notifications.collect_due(now=datetime(2026, 11, 4, 6, 50, tzinfo=UTC).timestamp())
    fresh = await client.get("/api/work/notifications", cookies=a["cookies"])
    assert any(n["due_at"] == "2026-11-04T07:00:00.000000Z" for n in fresh.json()["notifications"])


async def test_multi_action_confirmation_rolls_back_earlier_creates_on_stale_target(
    client, accounts, monkeypatch
):
    a = accounts["edit-alice"]
    t = task(accounts)

    async def plan(*args):
        return assistant.ModelPlan(
            actions=[
                assistant.Action(kind="create_project", fields={"name": "不应留下"}),
                assistant.Action(
                    kind="update_task", fields={"match_title": "整理材料", "changes": {"status": "done"}}
                ),
            ]
        )

    monkeypatch.setattr(assistant, "_model_plan", plan)
    p = await propose(client, a["cookies"], "把这一组想法安排好", allow_model=True)
    store.update_task("edit-alice", a["space"], t["id"], UpdateTask(expected_version=1, priority="high"))
    assert (await apply(client, a["cookies"], p)).status_code == 409
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM work_projects").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM work_proposal_steps").fetchone()[0] == 0
        assert db.execute("SELECT state FROM work_proposals WHERE id=?", [p["id"]]).fetchone()[0] == "ready"


async def test_multi_action_undo_is_all_or_nothing(client, accounts, monkeypatch):
    a = accounts["edit-alice"]
    t = task(accounts)

    async def plan(*args):
        return assistant.ModelPlan(
            actions=[
                assistant.Action(
                    kind="update_task", fields={"match_title": "整理材料", "changes": {"status": "done"}}
                ),
                assistant.Action(kind="create_project", fields={"name": "一起撤销"}),
            ]
        )

    monkeypatch.setattr(assistant, "_model_plan", plan)
    p = await propose(client, a["cookies"], "完成这一组安排", allow_model=True)
    done = await apply(client, a["cookies"], p)
    assert done.status_code == 200, done.text
    project = done.json()["proposal"]["results"][1]["result"]["project"]
    store.update_task("edit-alice", a["space"], t["id"], UpdateTask(expected_version=2, priority="high"))
    assert (await apply(client, a["cookies"], p, "undo")).status_code == 409
    with get_db() as db:
        assert projects._project(db, "edit-alice", project["id"])["status"] == "active"
        assert db.execute("SELECT COUNT(*) FROM work_proposal_steps WHERE step>=1000").fetchone()[0] == 0
        assert (
            db.execute("SELECT state FROM work_proposals WHERE id=?", [p["id"]]).fetchone()[0] == "completed"
        )


async def test_series_edit_preserves_single_exceptions_and_refuses_silent_deletion(client, accounts):
    e = event()
    original = calendar.agenda("edit-alice", "2026-11-04T00:00:00Z", "2026-11-08T00:00:00Z")["occurrences"][0]
    calendar.save_occurrence(
        "edit-alice",
        e["id"],
        original["occurrence_id"],
        calendar.OccurrenceUpdate(
            request_id=str(uuid4()),
            expected_version=1,
            start_at="2026-11-04T07:00:00Z",
            end_at="2026-11-04T09:00:00Z",
        ),
    )
    base = {k: e[k] for k in ("title", "description", "start_at", "end_at", "timezone", "recurrence")}
    saved = calendar.save_event(
        "edit-alice",
        calendar.EventUpdate(
            **{**base, "description": "整组备注"}, request_id=str(uuid4()), expected_version=1
        ),
        e["id"],
    )
    assert saved["event"]["version"] == 2
    with get_db() as db:
        assert (
            db.execute(
                "SELECT start_at FROM work_calendar_instances WHERE id=?", [original["occurrence_id"]]
            ).fetchone()[0]
            == "2026-11-04T07:00:00.000000Z"
        )
    shift = {**base, "start_at": "2026-11-05T02:00:00Z", "end_at": "2026-11-05T03:00:00Z"}
    with pytest.raises(HTTPException) as err:
        calendar.save_event(
            "edit-alice", calendar.EventUpdate(**shift, request_id=str(uuid4()), expected_version=2), e["id"]
        )
    assert err.value.status_code == 409
    with get_db() as db:
        assert calendar._owned(db, "edit-alice", e["id"])["version"] == 2
    reset = calendar.save_event(
        "edit-alice",
        calendar.EventUpdate(**shift, request_id=str(uuid4()), expected_version=2, reset_exceptions=True),
        e["id"],
    )
    assert reset["event"]["version"] == 3


async def test_instance_change_blocks_whole_series_undo(client, accounts, monkeypatch):
    a = accounts["edit-alice"]
    e = event()

    async def plan(*args):
        return assistant.ModelPlan(
            actions=[
                assistant.Action(
                    kind="update_event",
                    fields={"match_title": "周会", "scope": "series", "changes": {"description": "整组更新"}},
                )
            ]
        )

    monkeypatch.setattr(assistant, "_model_plan", plan)
    p = await propose(client, a["cookies"], "给整组周会补充说明", allow_model=True)
    assert (await apply(client, a["cookies"], p)).status_code == 200
    first = calendar.agenda("edit-alice", "2026-11-04T00:00:00Z", "2026-11-08T00:00:00Z")["occurrences"][0]
    calendar.save_occurrence(
        "edit-alice",
        e["id"],
        first["occurrence_id"],
        calendar.OccurrenceUpdate(request_id=str(uuid4()), expected_version=first["version"], cancelled=True),
    )
    assert (await apply(client, a["cookies"], p, "undo")).status_code == 409


async def test_deleted_account_cannot_rebuild_a_prepared_edit(client, accounts):
    a = accounts["edit-alice"]
    task(accounts)
    p = await propose(client, a["cookies"], "把任务「整理材料」标为已完成")
    with get_db() as db:
        db.execute("DELETE FROM users WHERE username='edit-alice'")
    assert (await apply(client, a["cookies"], p)).status_code == 401
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM work_proposals WHERE owner='edit-alice'").fetchone()[0] == 0


@pytest.mark.parametrize(
    "fields",
    [
        {"match_title": "整理材料", "id": "foreign", "changes": {"status": "done"}},
        {"match_title": "整理材料", "changes": {"owner": "edit-bob"}},
        {"match_title": "整理材料", "changes": {"expected_version": 99}},
        {"match_title": "整理材料", "changes": {"request_id": str(uuid4())}},
    ],
)
async def test_model_fields_cannot_select_owner_ids_versions_or_request_identity(
    client, accounts, monkeypatch, fields
):
    a = accounts["edit-alice"]
    task(accounts)

    async def plan(*args):
        return assistant.ModelPlan(actions=[assistant.Action(kind="update_task", fields=fields)])

    monkeypatch.setattr(assistant, "_model_plan", plan)
    response = await client.post(
        "/api/work/assistant/proposals",
        cookies=a["cookies"],
        json={"request_id": str(uuid4()), "text": "synthetic untrusted input", "allow_model": True},
    )
    assert response.status_code == 502
    with get_db() as db:
        assert db.execute("SELECT status FROM work_tasks").fetchone()[0] == "todo"


def test_relative_change_parser_rejects_negations_and_dst_ambiguity():
    assert assistant_changes.parse("不要把任务材料标为已完成", "Asia/Shanghai", now=NOW) is None
    now = datetime(2026, 10, 31, 12, tzinfo=UTC)
    assert assistant_changes.parse("把明天的周会改到上午1点30分", "America/New_York", now=now)[
        "clarification"
    ]
    assert assistant_changes.parse("把明天的周会改到下午99点", "Asia/Shanghai", now=NOW) is None
