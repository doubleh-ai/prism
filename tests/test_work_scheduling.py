"""Real Work routes: shared scheduling, stale writes and dependency invariants."""

from uuid import uuid4

import pytest

from server.database import create_session, get_db
from server.productivity import projects, store
from server.productivity.models import CreateSpace

CONFIG = {
    "start_at": "2026-11-02T01:00:00Z",
    "timezone": "Asia/Shanghai",
    "work_start": 540,
    "work_end": 1080,
    "weekdays": [0, 1, 2, 3, 4],
    "holidays": [],
}


@pytest.fixture
async def scheduled(client):
    store.init_productivity_db()
    projects.init_projects_db()
    cookies = {}
    for name in ("plan-owner", "plan-editor", "plan-viewer", "plan-stranger"):
        with get_db() as db:
            db.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", [name, "synthetic-only"])
        cookies[name] = {"token": create_session(name)}
    space = store.create_space("plan-owner", CreateSpace(request_id=str(uuid4()), name="合成工作区"))[
        "workspace"
    ]
    project = projects.save_project(
        "plan-owner", projects.ProjectCreate(request_id=str(uuid4()), name="合成排程")
    )["project"]
    base = f"/api/work/projects/{project['id']}"
    for actor, role in (("plan-editor", "editor"), ("plan-viewer", "viewer")):
        invited = await client.post(
            base + "/invitations",
            cookies=cookies["plan-owner"],
            json={"request_id": str(uuid4()), "username": actor, "role": role},
        )
        assert invited.status_code == 200, invited.text
        value = invited.json()["invitation"]
        joined = await client.post(
            f"/api/work/invitations/{value['id']}/respond",
            cookies=cookies[actor],
            json={"request_id": str(uuid4()), "expected_version": value["version"], "decision": "accept"},
        )
        assert joined.status_code == 200, joined.text
    tasks = []
    for title in ("设计", "开发", "验收"):
        created = await client.post(
            base + "/tasks", cookies=cookies["plan-owner"], json={"request_id": str(uuid4()), "title": title}
        )
        assert created.status_code == 200, created.text
        tasks.append(created.json()["task"])
    return {"base": base, "project": project, "cookies": cookies, "tasks": tasks, "space": space["id"]}


async def state(client, data, actor="plan-owner"):
    r = await client.get(data["base"] + "/schedule", cookies=data["cookies"][actor])
    assert r.status_code == 200, r.text
    return r.json()


async def put(client, data, path, body, actor="plan-owner"):
    return await client.put(data["base"] + path, cookies=data["cookies"][actor], json=body)


async def dependency(client, data, links):
    current = await state(client, data)
    return await put(
        client,
        data,
        "/dependencies",
        {"request_id": str(uuid4()), "expected_version": current["version"], "links": links},
    )


def edge(data, a=0, b=1):
    return {
        "predecessor_id": data["tasks"][a]["id"],
        "successor_id": data["tasks"][b]["id"],
        "lag_minutes": 0,
    }


async def test_explicit_calendar_is_versioned_and_does_not_misreport_defaults(client, scheduled):
    d = scheduled
    current = await state(client, d)
    assert current["configured"] is False and current["version"] == 3 and current["computed"] is None
    body = {**CONFIG, "request_id": str(uuid4()), "expected_version": current["version"]}
    first = await put(client, d, "/schedule", body, "plan-editor")
    assert first.status_code == 200, first.text
    assert (await put(client, d, "/schedule", body, "plan-editor")).json()["replayed"]
    stale = await put(client, d, "/schedule", {**body, "request_id": str(uuid4())})
    assert stale.status_code == 409
    current = await state(client, d)
    assert current["computed"]["status"] == "needs_estimates"
    assert current["computed"]["counts"]["default_estimate"] == 3
    assert not current["computed"]["assumptions"]["observed_actual_dates"]


async def test_viewer_reads_stranger_cannot_read_and_all_writes_check_role(client, scheduled):
    d = scheduled
    assert (await state(client, d, "plan-viewer"))["can_edit"] is False
    assert (
        await client.get(d["base"] + "/schedule", cookies=d["cookies"]["plan-stranger"])
    ).status_code == 404
    task_id = d["tasks"][0]["id"]
    for path, body in (
        ("/schedule", {**CONFIG, "expected_version": 3}),
        ("/dependencies", {"expected_version": 3, "links": []}),
        (f"/tasks/{task_id}/plan", {"expected_version": 0, "duration_minutes": 30}),
    ):
        response = await put(client, d, path, {**body, "request_id": str(uuid4())}, "plan-viewer")
        assert response.status_code == 403, response.text


async def test_cycles_duplicate_edges_and_cross_project_targets_are_rejected_atomically(client, scheduled):
    d = scheduled
    assert (await dependency(client, d, [edge(d)])).status_code == 200
    current = await state(client, d)
    invalid = [[edge(d), edge(d, 1, 0)], [edge(d), edge(d)], [{**edge(d), "successor_id": str(uuid4())}]]
    for links in invalid:
        assert (await dependency(client, d, links)).status_code == 422
        assert (await state(client, d))["links"] == current["links"]
        assert (await state(client, d))["version"] == current["version"]


async def test_dependencies_guard_both_shared_and_personal_task_status_routes(client, scheduled):
    d = scheduled
    assert (await dependency(client, d, [edge(d)])).status_code == 200
    a, b = d["tasks"][:2]
    shared = d["base"] + f"/tasks/{b['id']}"
    personal = f"/api/work/spaces/{d['space']}/tasks/{b['id']}"
    for path, actor in ((shared, "plan-editor"), (personal, "plan-owner")):
        r = await client.patch(
            path, cookies=d["cookies"][actor], json={"expected_version": 1, "status": "done"}
        )
        assert r.status_code == 409, r.text
    first = await client.patch(
        d["base"] + f"/tasks/{a['id']}",
        cookies=d["cookies"]["plan-editor"],
        json={"expected_version": 1, "status": "done"},
    )
    assert first.status_code == 200, first.text
    assert (
        await client.patch(
            shared, cookies=d["cookies"]["plan-editor"], json={"expected_version": 1, "status": "doing"}
        )
    ).status_code == 200
    reopened = await client.patch(
        d["base"] + f"/tasks/{a['id']}",
        cookies=d["cookies"]["plan-owner"],
        json={"expected_version": 2, "status": "todo"},
    )
    assert reopened.status_code == 409
    assert (await dependency(client, d, [edge(d), edge(d, 2, 1)])).status_code == 409


async def test_plan_estimate_assignment_milestone_and_compare_and_swap(client, scheduled):
    d = scheduled
    path = f"/tasks/{d['tasks'][0]['id']}/plan"
    body = {
        "request_id": str(uuid4()),
        "expected_version": 0,
        "duration_minutes": 120,
        "assignee": "plan-editor",
    }
    first = await put(client, d, path, body)
    assert first.status_code == 200, first.text
    assert first.json()["plan"]["duration_source"] == "user"
    assert (await put(client, d, path, body)).json()["replayed"]
    assert (await put(client, d, path, {**body, "request_id": str(uuid4())})).status_code == 409
    for changes in (
        {"duration_minutes": 0},
        {"assignee": "plan-viewer"},
        {"assignee": "plan-stranger"},
        {"not_before": "2026-11-03T09:00:00"},
    ):
        response = await put(client, d, path, {"request_id": str(uuid4()), "expected_version": 1, **changes})
        assert response.status_code == 422, response.text
    milestone = await put(
        client, d, path, {"request_id": str(uuid4()), "expected_version": 1, "milestone": True}
    )
    assert milestone.status_code == 200 and milestone.json()["plan"]["duration_minutes"] == 0


async def test_member_removal_unassigns_task_and_invalidates_old_plan(client, scheduled):
    d = scheduled
    path = f"/tasks/{d['tasks'][0]['id']}/plan"
    body = {"request_id": str(uuid4()), "expected_version": 0, "assignee": "plan-editor"}
    assert (await put(client, d, path, body)).status_code == 200
    before = await state(client, d)
    removed = await put(
        client,
        d,
        "/members/plan-editor",
        {"request_id": str(uuid4()), "expected_version": 1, "role": "removed"},
    )
    assert removed.status_code == 200, removed.text
    after = await state(client, d)
    assert after["version"] > before["version"]
    assert after["tasks"][0]["assignee"] is None and after["tasks"][0]["plan_version"] == 2
    assert (
        await put(
            client, d, path, {"request_id": str(uuid4()), "expected_version": 1, "duration_minutes": 30}
        )
    ).status_code == 409
    assert (await client.get(d["base"] + "/schedule", cookies=d["cookies"]["plan-editor"])).status_code == 404


async def test_disabled_assignee_blocks_computed_plan_instead_of_freeing_capacity(client, scheduled):
    d = scheduled
    assert (
        await put(client, d, "/schedule", {**CONFIG, "request_id": str(uuid4()), "expected_version": 3})
    ).status_code == 200
    assert (
        await put(
            client,
            d,
            f"/tasks/{d['tasks'][0]['id']}/plan",
            {"request_id": str(uuid4()), "expected_version": 0, "assignee": "plan-editor"},
        )
    ).status_code == 200
    with get_db() as db:
        db.execute("UPDATE users SET disabled_at=1 WHERE username='plan-editor'")
    result = (await state(client, d))["computed"]
    assert result["status"] == "blocked" and result["project_finish_at"] is None
    assert result["tasks"][0]["blocked_reason"] == "assignee_unavailable"


async def test_delete_cleanup_does_not_leave_links_plans_or_memberships(client, scheduled):
    d = scheduled
    assert (await dependency(client, d, [edge(d)])).status_code == 200
    tid = d["tasks"][0]["id"]
    assert (
        await put(
            client,
            d,
            f"/tasks/{tid}/plan",
            {"request_id": str(uuid4()), "expected_version": 0, "duration_minutes": 30},
        )
    ).status_code == 200
    with get_db() as db:
        db.execute("DELETE FROM work_tasks WHERE id=?", [tid])
        assert not db.execute(
            "SELECT 1 FROM work_project_dependencies WHERE predecessor_id=? OR successor_id=?", [tid, tid]
        ).fetchone()
        assert not db.execute("SELECT 1 FROM work_project_task_plans WHERE task_id=?", [tid]).fetchone()
        db.execute("DELETE FROM work_projects WHERE id=?", [d["project"]["id"]])
        for table in (
            "work_project_dependencies",
            "work_project_schedule_state",
            "work_project_task_plans",
            "work_project_members",
            "work_project_audit",
        ):
            assert not db.execute(
                f"SELECT 1 FROM {table} WHERE project_id=?", [d["project"]["id"]]
            ).fetchone()
