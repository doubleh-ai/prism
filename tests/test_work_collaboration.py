"""No implicit sharing: accepted roles grant exactly one project's records."""

import sqlite3
from uuid import uuid4

import pytest

from server.database import create_session, get_db
from server.productivity import projects, store
from server.productivity.models import CreateSpace, CreateTask


@pytest.fixture
def team():
    store.init_productivity_db()
    projects.init_projects_db()
    cookies = {}
    spaces = {}
    for actor in ("team-owner", "team-editor", "team-viewer", "team-outsider"):
        with get_db() as db:
            db.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", [actor, "synthetic-only"])
        cookies[actor] = {"token": create_session(actor)}
        spaces[actor] = store.create_space(
            actor, CreateSpace(request_id=str(uuid4()), name="合成个人工作区")
        )["workspace"]["id"]
    parent = projects.save_project(
        "team-owner",
        projects.ProjectCreate(request_id=str(uuid4()), name="合成协作项目", description="仅项目成员可见"),
    )["project"]
    unrelated = store.create_task(
        "team-owner", spaces["team-owner"], CreateTask(request_id=str(uuid4()), title="PRIVATE_UNLINKED_TASK")
    )["task"]
    return {"cookies": cookies, "spaces": spaces, "project": parent, "private": unrelated}


async def invite(client, team, actor, role="editor"):
    pid = team["project"]["id"]
    body = {"request_id": str(uuid4()), "username": actor, "role": role}
    r = await client.post(
        f"/api/work/projects/{pid}/invitations", cookies=team["cookies"]["team-owner"], json=body
    )
    assert r.status_code == 200, r.text
    return r.json()["invitation"], body


async def join(client, team, actor, role="editor"):
    invitation, _ = await invite(client, team, actor, role)
    r = await client.post(
        "/api/work/invitations/" + invitation["id"] + "/respond",
        cookies=team["cookies"][actor],
        json={"request_id": str(uuid4()), "expected_version": invitation["version"], "decision": "accept"},
    )
    assert r.status_code == 200, r.text
    return r.json()


async def test_invitation_is_not_access_and_only_recipient_can_accept(client, team):
    pid = team["project"]["id"]
    editor = team["cookies"]["team-editor"]
    outsider = team["cookies"]["team-outsider"]
    invitation, body = await invite(client, team, "team-editor")
    assert (await client.get(f"/api/work/projects/{pid}/tasks", cookies=editor)).status_code == 404
    incoming = await client.get("/api/work/invitations", cookies=editor)
    assert incoming.status_code == 200 and incoming.json()["invitations"][0]["project_name"] == "合成协作项目"
    assert "仅项目成员可见" not in incoming.text and "PRIVATE_UNLINKED_TASK" not in incoming.text
    replay = await client.post(
        f"/api/work/projects/{pid}/invitations", cookies=team["cookies"]["team-owner"], json=body
    )
    assert replay.status_code == 200 and replay.json()["replayed"]
    accept = {"request_id": str(uuid4()), "expected_version": invitation["version"], "decision": "accept"}
    path = "/api/work/invitations/" + invitation["id"] + "/respond"
    assert (await client.post(path, cookies=outsider, json=accept)).status_code == 404
    first = await client.post(path, cookies=editor, json=accept)
    assert first.status_code == 200
    assert (await client.post(path, cookies=editor, json=accept)).json()["replayed"]
    visible = await client.get("/api/work/projects", cookies=editor)
    assert visible.json()["projects"][0]["role"] == "editor"
    assert (await client.get("/api/work/projects", cookies=outsider)).json()["projects"] == []


async def test_editor_can_edit_project_tasks_but_not_personal_records_or_team(client, team):
    pid = team["project"]["id"]
    c = team["cookies"]
    await join(client, team, "team-editor")
    created = await client.post(
        f"/api/work/projects/{pid}/tasks",
        cookies=c["team-editor"],
        json={"request_id": str(uuid4()), "title": "协作任务"},
    )
    assert created.status_code == 200, created.text
    task = created.json()["task"]
    assert task["workspace_id"] == team["spaces"]["team-owner"]
    changed = await client.patch(
        f"/api/work/projects/{pid}/tasks/{task['id']}",
        cookies=c["team-editor"],
        json={"expected_version": 1, "status": "doing"},
    )
    assert changed.status_code == 200, changed.text
    assert (
        await client.patch(
            f"/api/work/projects/{pid}/tasks/{task['id']}",
            cookies=c["team-editor"],
            json={"expected_version": 1, "status": "done"},
        )
    ).status_code == 409
    assert (
        await client.get(
            f"/api/work/spaces/{task['workspace_id']}/tasks/{task['id']}", cookies=c["team-editor"]
        )
    ).status_code == 404
    assert (
        await client.patch(
            f"/api/work/projects/{pid}/tasks/{team['private']['id']}",
            cookies=c["team-editor"],
            json={"expected_version": 1, "status": "done"},
        )
    ).status_code == 404
    assert (
        "PRIVATE_UNLINKED_TASK"
        not in (await client.get(f"/api/work/projects/{pid}/tasks", cookies=c["team-editor"])).text
    )
    assert (
        await client.post(
            f"/api/work/projects/{pid}/invitations",
            cookies=c["team-editor"],
            json={"request_id": str(uuid4()), "username": "team-outsider"},
        )
    ).status_code == 403
    assert (
        await client.patch(
            f"/api/work/projects/{pid}",
            cookies=c["team-editor"],
            json={"request_id": str(uuid4()), "expected_version": 1, "name": "非法接管"},
        )
    ).status_code == 404
    activity = (await client.get(f"/api/work/projects/{pid}/activity", cookies=c["team-owner"])).json()[
        "items"
    ]
    assert any(r["actor"] == "team-editor" and r["action"] == "task_updated" for r in activity)


async def test_viewer_can_read_only_and_revocation_immediately_removes_server_access(client, team):
    pid = team["project"]["id"]
    c = team["cookies"]
    await join(client, team, "team-viewer", "viewer")
    assert (await client.get(f"/api/work/projects/{pid}/tasks", cookies=c["team-viewer"])).status_code == 200
    assert (
        await client.post(
            f"/api/work/projects/{pid}/tasks",
            cookies=c["team-viewer"],
            json={"request_id": str(uuid4()), "title": "不应创建"},
        )
    ).status_code == 403
    members = (await client.get(f"/api/work/projects/{pid}/members", cookies=c["team-owner"])).json()[
        "members"
    ]
    member = next(m for m in members if m["username"] == "team-viewer")
    body = {"request_id": str(uuid4()), "expected_version": member["version"], "role": "removed"}
    assert (
        await client.put(f"/api/work/projects/{pid}/members/team-viewer", cookies=c["team-owner"], json=body)
    ).status_code == 200
    for path in (
        f"/api/work/projects/{pid}/tasks",
        f"/api/work/projects/{pid}/members",
        f"/api/work/projects/{pid}/activity",
    ):
        assert (await client.get(path, cookies=c["team-viewer"])).status_code == 404


async def test_rejoin_uses_monotonic_membership_version_and_old_accept_cannot_regrant(client, team):
    pid = team["project"]["id"]
    c = team["cookies"]
    invitation, _ = await invite(client, team, "team-editor")
    accepted = {"request_id": str(uuid4()), "expected_version": 1, "decision": "accept"}
    path = "/api/work/invitations/" + invitation["id"] + "/respond"
    assert (await client.post(path, cookies=c["team-editor"], json=accepted)).status_code == 200
    remove = {"request_id": str(uuid4()), "expected_version": 1, "role": "removed"}
    assert (
        await client.put(
            f"/api/work/projects/{pid}/members/team-editor", cookies=c["team-owner"], json=remove
        )
    ).status_code == 200
    assert (await client.post(path, cookies=c["team-editor"], json=accepted)).json()["replayed"]
    assert (await client.get(f"/api/work/projects/{pid}", cookies=c["team-editor"])).status_code == 404
    await join(client, team, "team-editor")
    stale = await client.put(
        f"/api/work/projects/{pid}/members/team-editor",
        cookies=c["team-owner"],
        json={**remove, "request_id": str(uuid4())},
    )
    assert stale.status_code == 409


async def test_member_leave_reconciles_without_reopening_and_owner_cannot_leave(client, team):
    pid = team["project"]["id"]
    c = team["cookies"]
    await join(client, team, "team-editor")
    body = {"request_id": str(uuid4()), "expected_version": 1}
    first = await client.post(f"/api/work/projects/{pid}/leave", cookies=c["team-editor"], json=body)
    assert first.status_code == 200, first.text
    assert (await client.post(f"/api/work/projects/{pid}/leave", cookies=c["team-editor"], json=body)).json()[
        "replayed"
    ]
    assert (
        await client.post(
            f"/api/work/projects/{pid}/leave",
            cookies=c["team-owner"],
            json={"request_id": str(uuid4()), "expected_version": 1},
        )
    ).status_code == 409


async def test_revoked_pending_invitation_cannot_be_accepted(client, team):
    pid = team["project"]["id"]
    c = team["cookies"]
    invitation, _ = await invite(client, team, "team-editor")
    r = await client.post(
        f"/api/work/projects/{pid}/invitations/{invitation['id']}/revoke",
        cookies=c["team-owner"],
        json={"request_id": str(uuid4()), "expected_version": 1},
    )
    assert r.status_code == 200
    assert (
        await client.post(
            "/api/work/invitations/" + invitation["id"] + "/respond",
            cookies=c["team-editor"],
            json={"request_id": str(uuid4()), "expected_version": 1, "decision": "accept"},
        )
    ).status_code == 409


async def test_disabled_owner_and_deleted_member_do_not_expose_old_data_after_recreation(client, team):
    pid = team["project"]["id"]
    c = team["cookies"]
    await join(client, team, "team-editor")
    with get_db() as db:
        db.execute("UPDATE users SET disabled_at=1 WHERE username='team-owner'")
    assert (await client.get(f"/api/work/projects/{pid}/tasks", cookies=c["team-editor"])).status_code == 404
    with get_db() as db:
        db.execute("UPDATE users SET disabled_at=NULL WHERE username='team-owner'")
        db.execute("DELETE FROM users WHERE username='team-editor'")
        with pytest.raises(sqlite3.IntegrityError, match="retired_username"):
            db.execute("INSERT INTO users(username,password_hash) VALUES('team-editor','synthetic-only')")
    assert (await client.get(f"/api/work/projects/{pid}/tasks", cookies=c["team-editor"])).status_code == 401
    with get_db() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM work_project_members WHERE username='team-editor'").fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM work_project_invitations WHERE invitee='team-editor'"
            ).fetchone()[0]
            == 0
        )


async def test_owner_deletion_removes_project_roles_invites_and_audit(client, team):
    await join(client, team, "team-editor")
    with get_db() as db:
        db.execute("DELETE FROM users WHERE username='team-owner'")
        for table in (
            "work_projects",
            "work_project_members",
            "work_project_invitations",
            "work_project_audit",
        ):
            assert db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 0
