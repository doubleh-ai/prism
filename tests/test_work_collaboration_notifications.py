"""The existing server reminder loop handles team work without separate inboxes."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from test_work_attachments_approvals import approval, decision
from test_work_scheduling import scheduled as scheduled

from server.productivity import calendar, notifications


@pytest.fixture(autouse=True)
def reminder_tables(scheduled):
    calendar.init_calendar_db()
    notifications.init_notifications_db()


async def listed(client, d, actor):
    response = await client.get("/api/work/notifications", cookies=d["cookies"][actor])
    assert response.status_code == 200, response.text
    return response.json()["notifications"]


async def test_pending_approval_and_result_share_one_deduplicated_in_app_channel(client, scheduled):
    d = scheduled
    notifications.init_notifications_db()
    response, _ = await approval(client, d)
    row = response.json()["approval"]
    assert notifications.collect_due() == 1
    assert notifications.collect_due() == 0
    owner = await listed(client, d, "plan-owner")
    assert (
        len(owner) == 1
        and owner[0]["kind"] == "approval"
        and owner[0]["due_at"] is None
        and owner[0]["href"] == "/projects"
    )
    assert await listed(client, d, "plan-viewer") == []
    assert (await decision(client, d, row)).status_code == 200
    assert notifications.collect_due() == 1
    assert await listed(client, d, "plan-owner") == []
    requester = await listed(client, d, "plan-editor")
    assert (
        len(requester) == 1
        and requester[0]["kind"] == "approval_result"
        and "已通过" in requester[0]["title"]
    )
    assert notifications.collect_due() == 0


async def test_assigned_task_notice_pins_both_task_and_estimate_versions(client, scheduled):
    d = scheduled
    notifications.init_notifications_db()
    task = d["tasks"][0]
    path = d["base"] + f"/tasks/{task['id']}"
    now = datetime.now(UTC)
    due = (now + timedelta(minutes=10)).isoformat()
    assert (
        await client.patch(
            path, cookies=d["cookies"]["plan-owner"], json={"expected_version": 1, "due_at": due}
        )
    ).status_code == 200
    r = await client.put(
        path + "/plan",
        cookies=d["cookies"]["plan-owner"],
        json={"request_id": str(uuid4()), "expected_version": 0, "assignee": "plan-editor"},
    )
    assert r.status_code == 200, r.text
    assert notifications.collect_due() == 2
    rows = await listed(client, d, "plan-editor")
    assert len(rows) == 1 and rows[0]["kind"] == "project_task"
    old_id = rows[0]["id"]
    assert (
        await client.patch(
            path, cookies=d["cookies"]["plan-owner"], json={"expected_version": 2, "title": "设计新标题"}
        )
    ).status_code == 200
    assert await listed(client, d, "plan-editor") == []
    assert notifications.collect_due() == 2
    assert (await listed(client, d, "plan-editor"))[0]["id"] != old_id
    r = await client.put(
        d["base"] + "/members/plan-editor",
        cookies=d["cookies"]["plan-owner"],
        json={"request_id": str(uuid4()), "expected_version": 1, "role": "removed"},
    )
    assert r.status_code == 200
    assert await listed(client, d, "plan-editor") == []


async def test_revoked_project_access_hides_old_approval_notifications(client, scheduled):
    d = scheduled
    notifications.init_notifications_db()
    response, _ = await approval(client, d, "plan-owner", reviewer="plan-editor")
    assert response.status_code == 200
    assert notifications.collect_due() == 1
    assert (await listed(client, d, "plan-editor"))[0]["kind"] == "approval"
    r = await client.put(
        d["base"] + "/members/plan-editor",
        cookies=d["cookies"]["plan-owner"],
        json={"request_id": str(uuid4()), "expected_version": 1, "role": "viewer"},
    )
    assert r.status_code == 200
    assert await listed(client, d, "plan-editor") == []
