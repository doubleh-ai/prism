"""Local project reads and explicit approval proposals do not grant model authority."""

from uuid import uuid4

from test_work_assistant_projects import apply, propose
from test_work_attachments_approvals import decision, upload
from test_work_scheduling import scheduled as scheduled

from server.database import get_db
from server.productivity import assistant, store
from server.productivity.models import CreateTask


async def test_read_query_runs_without_confirmation_or_provider_and_is_not_undoable(
    client, scheduled, monkeypatch
):
    d = scheduled

    async def forbidden(*args):
        raise AssertionError("read does not call a model")

    monkeypatch.setattr(assistant, "_model_plan", forbidden)
    store.create_task("plan-owner", d["space"], CreateTask(request_id=str(uuid4()), title="PRIVATE_UNLINKED"))
    p = await propose(client, d, "查看项目「合成排程」的任务", actor="plan-viewer", model=True)
    assert p["state"] == "completed" and p["can_undo"] is False and p["source"] == "routine"
    result = p["results"][0]
    assert result["kind"] == "read_project" and len(result["result"]["tasks"]) == 3
    assert "PRIVATE_UNLINKED" not in str(p)
    assert (await apply(client, d, p, actor="plan-viewer", method="undo")).status_code == 409
    assert (await apply(client, d, p, actor="plan-viewer")).json()["proposal"] == p
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM work_project_approvals").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM work_proposal_steps").fetchone()[0] == 0


async def test_read_proposal_history_does_not_reopen_revoked_project(client, scheduled):
    d = scheduled
    p = await propose(client, d, "查看项目「合成排程」", actor="plan-viewer")
    assert p["state"] == "completed"
    removed = await client.put(
        d["base"] + "/members/plan-viewer",
        cookies=d["cookies"]["plan-owner"],
        json={"request_id": str(uuid4()), "expected_version": 1, "role": "removed"},
    )
    assert removed.status_code == 200
    r = await client.get(f"/api/work/assistant/proposals/{p['id']}", cookies=d["cookies"]["plan-viewer"])
    assert r.status_code == 404 and "设计" not in r.text


async def test_agent_only_proposes_and_creates_pending_request_then_can_withdraw(client, scheduled):
    d = scheduled
    p = await propose(client, d, "在项目「合成排程」中发起审批「发布材料」交给「plan-owner」")
    assert p["state"] == "ready" and p["actions"][0]["kind"] == "create_project_approval"
    assert p["actions"][0]["target"]["approval_snapshot"] == {"task": None, "attachments": []}
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM work_project_approvals").fetchone()[0] == 0
    first = await apply(client, d, p)
    assert first.status_code == 200, first.text
    pending = first.json()["proposal"]["results"][0]["result"]["approval"]
    assert pending["status"] == "pending" and pending["reviewer"] == "plan-owner"
    assert first.json()["proposal"]["can_undo"] is True
    assert (await apply(client, d, p, method="undo")).status_code == 200
    listed = (await client.get(d["base"] + "/approvals", cookies=d["cookies"]["plan-editor"])).json()["items"]
    assert listed[0]["status"] == "cancelled"


async def test_human_decision_cannot_be_overwritten_by_agent_undo(client, scheduled):
    d = scheduled
    p = await propose(client, d, "在项目「合成排程」中发起审批「发布材料」交给「plan-owner」")
    first = await apply(client, d, p)
    row = first.json()["proposal"]["results"][0]["result"]["approval"]
    assert (await decision(client, d, row)).status_code == 200
    assert (await apply(client, d, p, method="undo")).status_code == 409
    with get_db() as db:
        assert (
            db.execute("SELECT status FROM work_project_approvals WHERE id=?", [row["id"]]).fetchone()[0]
            == "approved"
        )


async def test_model_proposal_uses_local_names_and_pins_task_and_file(client, scheduled, monkeypatch):
    d = scheduled
    f = (await upload(client, d))[0].json()["attachment"]

    async def model(*args):
        return assistant.ModelPlan(
            actions=[
                assistant.Action(
                    kind="create_project_approval",
                    fields={
                        "project_name": "合成排程",
                        "title": "评审设计",
                        "reviewer": "plan-owner",
                        "task_title": "设计",
                        "attachment_names": [f["filename"]],
                    },
                )
            ]
        )

    monkeypatch.setattr(assistant, "_model_plan", model)
    p = await propose(client, d, "合成关联材料评审", model=True)
    assert p["state"] == "ready"
    pinned = p["actions"][0]["target"]["approval_snapshot"]
    assert pinned["task"]["title"] == "设计" and pinned["attachments"][0]["sha256"] == f["sha256"]
    removed = await client.post(
        d["base"] + f"/attachments/{f['id']}/remove",
        cookies=d["cookies"]["plan-owner"],
        json={"request_id": str(uuid4()), "expected_version": 1},
    )
    assert removed.status_code == 200
    assert (await apply(client, d, p)).status_code == 409
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM work_project_approvals").fetchone()[0] == 0


async def test_ambiguous_file_and_self_approval_clarify_without_fallback(client, scheduled, monkeypatch):
    d = scheduled
    for _ in range(2):
        assert (await upload(client, d))[0].status_code == 200

    async def model(*args):
        return assistant.ModelPlan(
            actions=[
                assistant.Action(
                    kind="create_project_approval",
                    fields={
                        "project_name": "合成排程",
                        "title": "评审设计",
                        "reviewer": "plan-owner",
                        "attachment_names": ["合成资料.txt"],
                    },
                )
            ]
        )

    monkeypatch.setattr(assistant, "_model_plan", model)
    p = await propose(client, d, "合成含歧义附件", model=True)
    assert p["state"] == "clarification" and "同名" in p["clarification"]
    p = await propose(client, d, "在项目「合成排程」中发起审批「自批」交给「plan-editor」")
    assert p["state"] == "clarification" and p["can_use_model"] is False
