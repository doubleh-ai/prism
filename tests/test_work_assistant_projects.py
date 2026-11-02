"""The same Agent proposal applies shared task/plan changes, never model authority."""

from uuid import uuid4

from test_work_scheduling import CONFIG, state
from test_work_scheduling import scheduled as scheduled

from server.database import get_db
from server.productivity import assistant


async def propose(client, d, text, actor="plan-editor", model=False):
    r = await client.post(
        "/api/work/assistant/proposals",
        cookies=d["cookies"][actor],
        json={"request_id": str(uuid4()), "text": text, "timezone": "Asia/Shanghai", "allow_model": model},
    )
    assert r.status_code == 200, r.text
    return r.json()["proposal"]


async def apply(client, d, p, actor="plan-editor", method="confirm"):
    return await client.post(
        f"/api/work/assistant/proposals/{p['id']}/{method}",
        cookies=d["cookies"][actor],
        json={"digest": p["digest"]},
    )


async def test_shared_task_edit_uses_one_preview_confirm_and_inverse(client, scheduled):
    d = scheduled
    p = await propose(client, d, "把项目「合成排程」的任务「设计」标为已完成")
    assert p["state"] == "ready" and p["source"] == "routine"
    action = p["actions"][0]
    assert action["kind"] == "update_project_task"
    assert action["target"]["project_id"] == d["project"]["id"]
    assert (await state(client, d))["tasks"][0]["status"] == "todo"
    result = await apply(client, d, p)
    assert result.status_code == 200, result.text
    assert (await apply(client, d, p)).json() == result.json()
    assert (await state(client, d))["tasks"][0]["status"] == "done"
    undone = await apply(client, d, p, method="undo")
    assert undone.status_code == 200, undone.text
    assert (await state(client, d))["tasks"][0]["status"] == "todo"


async def test_project_task_creation_is_agent_reachable_without_manual_workspace_setup(client, scheduled):
    d = scheduled
    with get_db() as db:
        db.execute("DELETE FROM work_tasks")
        db.execute("DELETE FROM work_project_tasks")
        db.execute("DELETE FROM work_spaces")
    p = await propose(client, d, "在项目「合成排程」中添加任务「发布检查」")
    assert p["state"] == "ready" and p["actions"][0]["kind"] == "create_project_task"
    first = await apply(client, d, p)
    assert first.status_code == 200, first.text
    current = await state(client, d)
    assert len(current["tasks"]) == 1 and current["tasks"][0]["title"] == "发布检查"
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM work_spaces WHERE owner='plan-owner'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM work_spaces WHERE owner='plan-editor'").fetchone()[0] == 0
    assert (await apply(client, d, p)).json() == first.json()
    assert (await apply(client, d, p, method="undo")).status_code == 200
    assert (await state(client, d))["tasks"][0]["status"] == "cancelled"


async def test_plan_fields_and_dependencies_are_agent_reachable_and_undoable(client, scheduled):
    d = scheduled
    for text, key, value in (
        ("把项目「合成排程」的任务「设计」工时设为120分钟", "duration_minutes", 120),
        ("把项目「合成排程」的任务「设计」负责人设为plan-editor", "assignee", "plan-editor"),
    ):
        p = await propose(client, d, text)
        assert p["state"] == "ready" and p["actions"][0]["kind"] == "plan_project_task"
        response = await apply(client, d, p)
        assert response.status_code == 200, response.text
        assert (await state(client, d))["tasks"][0][key] == value
        assert (await apply(client, d, p, method="undo")).status_code == 200
    p = await propose(client, d, "在项目「合成排程」中让任务「设计」完成后开始任务「开发」")
    assert p["actions"][0]["target"]["dependency_changes"][0]["before_title"] == "设计"
    assert (await apply(client, d, p)).status_code == 200
    assert len((await state(client, d))["links"]) == 1
    assert (await apply(client, d, p, method="undo")).status_code == 200
    assert (await state(client, d))["links"] == []


async def test_batched_project_actions_share_a_transaction_and_undo_in_reverse(
    client, scheduled, monkeypatch
):
    d = scheduled

    async def model(*args):
        return assistant.ModelPlan(
            actions=[
                assistant.Action(
                    kind="set_project_calendar", fields={"project_name": "合成排程", "config": CONFIG}
                ),
                assistant.Action(
                    kind="plan_project_task",
                    fields={
                        "project_name": "合成排程",
                        "match_title": "设计",
                        "changes": {"duration_minutes": 120, "assignee": "plan-editor"},
                    },
                ),
                assistant.Action(
                    kind="set_project_dependencies",
                    fields={
                        "project_name": "合成排程",
                        "links": [{"before_title": "设计", "after_title": "开发"}],
                    },
                ),
            ]
        )

    monkeypatch.setattr(assistant, "_model_plan", model)
    p = await propose(client, d, "合成多项安排", model=True)
    assert p["state"] == "ready" and len(p["actions"]) == 3
    first = await apply(client, d, p)
    assert first.status_code == 200, first.text
    current = await state(client, d)
    assert current["configured"] and len(current["links"]) == 1
    assert current["tasks"][0]["assignee"] == "plan-editor"
    undone = await apply(client, d, p, method="undo")
    assert undone.status_code == 200, undone.text
    current = await state(client, d)
    assert current["configured"] is False and current["links"] == []
    assert current["tasks"][0]["duration_source"] == "default" and current["tasks"][0]["assignee"] is None


async def test_stale_plan_never_partially_applies_other_sibling_actions(client, scheduled, monkeypatch):
    d = scheduled

    async def model(*args):
        return assistant.ModelPlan(
            actions=[
                assistant.Action(kind="create_project", fields={"name": "不能残留"}),
                assistant.Action(
                    kind="plan_project_task",
                    fields={
                        "project_name": "合成排程",
                        "match_title": "设计",
                        "changes": {"duration_minutes": 120},
                    },
                ),
            ]
        )

    monkeypatch.setattr(assistant, "_model_plan", model)
    p = await propose(client, d, "合成冲突安排", model=True)
    changed = await client.patch(
        d["base"] + f"/tasks/{d['tasks'][0]['id']}",
        cookies=d["cookies"]["plan-owner"],
        json={"expected_version": 1, "priority": "high"},
    )
    assert changed.status_code == 200
    assert (await apply(client, d, p)).status_code == 409
    with get_db() as db:
        assert not db.execute("SELECT 1 FROM work_projects WHERE name='不能残留'").fetchone()
        assert not db.execute("SELECT 1 FROM work_proposal_steps WHERE proposal_id=?", [p["id"]]).fetchone()


async def test_revoked_project_access_also_closes_saved_previews_and_replay(client, scheduled):
    d = scheduled
    p = await propose(client, d, "把项目「合成排程」的任务「设计」工时设为120分钟")
    assert (await apply(client, d, p)).status_code == 200
    removed = await client.put(
        d["base"] + "/members/plan-editor",
        cookies=d["cookies"]["plan-owner"],
        json={"request_id": str(uuid4()), "expected_version": 1, "role": "removed"},
    )
    assert removed.status_code == 200
    for path in (f"/api/work/assistant/proposals/{p['id']}",):
        r = await client.get(path, cookies=d["cookies"]["plan-editor"])
        assert r.status_code == 404 and "设计" not in r.text
    assert (await apply(client, d, p)).status_code == 404
    assert (await apply(client, d, p, method="undo")).status_code == 404


async def test_ambiguous_inaccessible_or_noop_project_input_never_invokes_model(
    client, scheduled, monkeypatch
):
    d = scheduled

    async def forbidden(*args):
        raise AssertionError("must not send existing project content to a provider")

    monkeypatch.setattr(assistant, "_model_plan", forbidden)
    for actor, text in (
        ("plan-viewer", "把项目「合成排程」的任务「设计」标为已完成"),
        ("plan-editor", "把项目「合成排程」的任务「设计」标为待办"),
        ("plan-editor", "把项目「合成排程」的任务「设计」负责人设为plan-stranger"),
    ):
        p = await propose(client, d, text, actor, model=True)
        assert p["state"] == "clarification" and p["can_use_model"] is False
        assert p["actions"] == []


async def test_dependency_changes_require_unique_targets_and_cannot_cycle(client, scheduled):
    d = scheduled
    p = await propose(client, d, "在项目「合成排程」中让任务「设计」完成后开始任务「开发」")
    assert (await apply(client, d, p)).status_code == 200
    p = await propose(client, d, "在项目「合成排程」中让任务「开发」完成后开始任务「设计」")
    assert p["state"] == "clarification" and "循环" in p["clarification"]
    r = await client.post(
        d["base"] + "/tasks",
        cookies=d["cookies"]["plan-owner"],
        json={"request_id": str(uuid4()), "title": "设计"},
    )
    assert r.status_code == 200
    p = await propose(client, d, "把项目「合成排程」的任务「设计」工时设为120分钟")
    assert p["state"] == "clarification" and "同名" in p["clarification"]


async def test_undo_does_not_overwrite_newer_project_changes(client, scheduled):
    d = scheduled
    p = await propose(client, d, "把项目「合成排程」的任务「设计」工时设为120分钟")
    assert (await apply(client, d, p)).status_code == 200
    edited = await client.patch(
        d["base"] + f"/tasks/{d['tasks'][1]['id']}",
        cookies=d["cookies"]["plan-owner"],
        json={"expected_version": 1, "title": "开发新安排"},
    )
    assert edited.status_code == 200
    assert (await apply(client, d, p, method="undo")).status_code == 409
    current = await state(client, d)
    assert current["tasks"][0]["duration_minutes"] == 120 and current["tasks"][1]["title"] == "开发新安排"
