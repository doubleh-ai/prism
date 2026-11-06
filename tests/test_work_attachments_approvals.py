"""Real permission checks and pinned human decisions over synthetic content only."""

import json
from urllib.parse import quote
from uuid import uuid4

import pytest
from test_work_scheduling import scheduled as scheduled

from server.database import get_db
from server.productivity import attachments


async def upload(
    client,
    d,
    actor="plan-owner",
    *,
    filename="合成资料.txt",
    data=b"synthetic content",
    request_id=None,
    task_id=None,
):
    meta = {"request_id": request_id or str(uuid4()), "filename": filename, "task_id": task_id}
    result = await client.post(
        d["base"] + "/attachments",
        cookies=d["cookies"][actor],
        headers={"Content-Type": "application/octet-stream", "X-Prism-Upload": quote(json.dumps(meta))},
        content=data,
    )
    return result, meta


async def approval(client, d, actor="plan-editor", **extra):
    body = {
        "request_id": str(uuid4()),
        "title": "合成发布评审",
        "description": "核对合成内容",
        "reviewer": "plan-owner",
        "task_id": None,
        "attachment_ids": [],
        **extra,
    }
    response = await client.post(d["base"] + "/approvals", cookies=d["cookies"][actor], json=body)
    return response, body


async def decision(client, d, row, actor="plan-owner", **extra):
    return await client.post(
        d["base"] + f"/approvals/{row['id']}/decision",
        cookies=d["cookies"][actor],
        json={
            "request_id": str(uuid4()),
            "expected_version": row["version"],
            "decision": "approved",
            "comment": "",
            **extra,
        },
    )


async def test_attachment_exact_bytes_private_download_and_idempotency(client, scheduled):
    d = scheduled
    data = "合成资料，不是真实用户数据。".encode()
    response, meta = await upload(client, d, data=data, task_id=d["tasks"][0]["id"])
    assert response.status_code == 200, response.text
    row = response.json()["attachment"]
    again, _ = await upload(client, d, data=data, request_id=meta["request_id"], task_id=meta["task_id"])
    assert again.status_code == 200 and again.json()["replayed"]
    conflict, _ = await upload(
        client, d, data=b"different", request_id=meta["request_id"], task_id=meta["task_id"]
    )
    assert conflict.status_code == 409
    path = d["base"] + f"/attachments/{row['id']}/download"
    result = await client.get(path, cookies=d["cookies"]["plan-viewer"])
    assert result.status_code == 200 and result.content == data
    assert result.headers["Content-Type"] == "application/octet-stream"
    assert result.headers["Cache-Control"] == "private, no-store"
    assert result.headers["Content-Disposition"].startswith("attachment;")
    assert result.headers["X-Content-Type-Options"] == "nosniff"
    assert (await client.get(path, cookies=d["cookies"]["plan-stranger"])).status_code == 404
    listed = await client.get(d["base"] + "/attachments", cookies=d["cookies"]["plan-viewer"])
    assert listed.json()["can_edit"] is False and "data" not in listed.json()["items"][0]


@pytest.mark.parametrize(
    "filename,data",
    [
        (".env", b"x"),
        ("../x.txt", b"x"),
        ("x.html", b"x"),
        ("x.svg", b"x"),
        ("x.pdf", b"not PDF"),
        ("x.png", b"not PNG"),
        ("x.txt", b"\x00binary"),
        ("bad\r\n.txt", b"x"),
    ],
)
async def test_attachment_unsafe_names_types_and_content_never_persist(client, scheduled, filename, data):
    response, _ = await upload(client, scheduled, filename=filename, data=data)
    assert response.status_code == 422, response.text
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM work_project_attachments").fetchone()[0] == 0


async def test_upload_limits_and_shared_viewer_cannot_upload(client, scheduled, monkeypatch):
    d = scheduled
    assert (await upload(client, d, "plan-viewer"))[0].status_code == 403
    monkeypatch.setattr(attachments, "MAX_FILE", 64)
    assert (await upload(client, d, data=b"x" * 65))[0].status_code == 413
    monkeypatch.setattr(attachments, "MAX_OWNER_BYTES", 8)
    assert (await upload(client, d, data=b"synthetic"))[0].status_code == 409
    assert (await upload(client, d, data=b"small"))[0].status_code == 200
    assert (await upload(client, d, data=b"small"))[0].status_code == 409


async def test_removal_checks_uploader_role_and_replay_does_not_resurrect(client, scheduled):
    d = scheduled
    first, meta = await upload(client, d)
    row = first.json()["attachment"]
    path = d["base"] + f"/attachments/{row['id']}/remove"
    body = {"request_id": str(uuid4()), "expected_version": 1}
    assert (await client.post(path, cookies=d["cookies"]["plan-editor"], json=body)).status_code == 403
    result = await client.post(path, cookies=d["cookies"]["plan-owner"], json=body)
    assert result.status_code == 200 and result.json()["attachment"]["state"] == "removed"
    replay, _ = await upload(client, d, request_id=meta["request_id"])
    assert replay.status_code == 200 and replay.json()["attachment"]["state"] == "removed"
    assert (
        await client.get(d["base"] + f"/attachments/{row['id']}/download", cookies=d["cookies"]["plan-owner"])
    ).status_code == 404
    with get_db() as db:
        assert (
            db.execute("SELECT data FROM work_project_attachments WHERE id=?", [row["id"]]).fetchone()[0]
            is None
        )


async def test_approval_freezes_task_and_selected_attachment_and_exact_reviewer_decides(client, scheduled):
    d = scheduled
    uploaded, _ = await upload(client, d)
    file = uploaded.json()["attachment"]
    created, body = await approval(client, d, task_id=d["tasks"][0]["id"], attachment_ids=[file["id"]])
    assert created.status_code == 200, created.text
    row = created.json()["approval"]
    assert row["task_snapshot"]["title"] == "设计" and row["attachments"][0]["sha256"] == file["sha256"]
    assert (await decision(client, d, row, "plan-editor")).status_code == 403
    assert (await decision(client, d, row, "plan-viewer")).status_code == 403
    response = await decision(client, d, row, comment="已核对合成内容")
    assert response.status_code == 200, response.text
    assert response.json()["approval"]["status"] == "approved"
    assert (await decision(client, d, row)).status_code == 409
    repeated = await client.post(d["base"] + "/approvals", cookies=d["cookies"]["plan-editor"], json=body)
    assert repeated.json()["approval"]["status"] == "approved" and repeated.json()["replayed"]
    assert (
        await client.get(d["base"] + "/approvals", cookies=d["cookies"]["plan-stranger"])
    ).status_code == 404


@pytest.mark.parametrize("change", ["task", "attachment", "membership"])
async def test_changed_approval_context_cannot_be_approved(client, scheduled, change):
    d = scheduled
    f = (await upload(client, d))[0].json()["attachment"]
    response, _ = await approval(client, d, task_id=d["tasks"][0]["id"], attachment_ids=[f["id"]])
    row = response.json()["approval"]
    if change == "task":
        edited = await client.patch(
            d["base"] + f"/tasks/{d['tasks'][0]['id']}",
            cookies=d["cookies"]["plan-owner"],
            json={"expected_version": 1, "title": "设计新内容"},
        )
    elif change == "attachment":
        edited = await client.post(
            d["base"] + f"/attachments/{f['id']}/remove",
            cookies=d["cookies"]["plan-owner"],
            json={"request_id": str(uuid4()), "expected_version": 1},
        )
    else:
        edited = await client.put(
            d["base"] + "/members/plan-editor",
            cookies=d["cookies"]["plan-owner"],
            json={"request_id": str(uuid4()), "expected_version": 1, "role": "viewer"},
        )
    assert edited.status_code == 200, edited.text
    assert (await decision(client, d, row)).status_code == 409
    cancelled = await client.post(
        d["base"] + f"/approvals/{row['id']}/cancel",
        cookies=d["cookies"]["plan-owner"],
        json={"request_id": str(uuid4()), "expected_version": 1},
    )
    assert cancelled.status_code == 200 and cancelled.json()["approval"]["status"] == "cancelled"


async def test_self_approval_wrong_reviewer_and_rejection_without_reason_are_refused(client, scheduled):
    d = scheduled
    assert (await approval(client, d, reviewer="plan-editor"))[0].status_code == 422
    assert (await approval(client, d, reviewer="plan-viewer"))[0].status_code == 422
    response, _ = await approval(client, d)
    row = response.json()["approval"]
    assert (await decision(client, d, row, decision="rejected")).status_code == 422
    result = await decision(client, d, row, decision="rejected", comment="缺少评审材料")
    assert result.status_code == 200 and result.json()["approval"]["comment"] == "缺少评审材料"


async def test_approval_pagination_never_hides_older_pending_for_me(client, scheduled):
    d = scheduled
    old, _ = await approval(client, d, title="较早待处理")
    newer, _ = await approval(client, d, title="较新申请")
    row = newer.json()["approval"]
    assert (await decision(client, d, row)).status_code == 200
    result = (await client.get(d["base"] + "/approvals?limit=1", cookies=d["cookies"]["plan-owner"])).json()
    assert len(result["items"]) == 1 and result["next_cursor"]
    assert result["pending_for_me"][0]["id"] == old.json()["approval"]["id"]
    next_page = (
        await client.get(
            d["base"] + f"/approvals?before={result['next_cursor']}&limit=1",
            cookies=d["cookies"]["plan-owner"],
        )
    ).json()
    assert next_page["items"][0]["title"] == "较早待处理" and next_page["next_cursor"] is None


async def test_project_and_account_cleanup_removes_private_file_bytes_and_requests(client, scheduled):
    d = scheduled
    uploaded, _ = await upload(client, d, "plan-editor")
    file = uploaded.json()["attachment"]
    response, _ = await approval(client, d)
    assert response.status_code == 200
    with get_db() as db:
        db.execute("DELETE FROM users WHERE username='plan-editor'")
        row = db.execute(
            "SELECT data,state,uploader FROM work_project_attachments WHERE id=?", [file["id"]]
        ).fetchone()
        assert row["data"] is None and row["state"] == "removed" and row["uploader"] == "[removed]"
        assert db.execute("SELECT COUNT(*) FROM work_project_approvals").fetchone()[0] == 0
        db.execute("DELETE FROM work_projects WHERE id=?", [d["project"]["id"]])
        assert db.execute("SELECT COUNT(*) FROM work_project_attachments").fetchone()[0] == 0


async def test_sqlite_backup_restores_exact_attachment_and_pending_approval(
    client, scheduled, tmp_path, monkeypatch
):
    import sqlite3

    from server import database

    d = scheduled
    raw = b"isolated restore fixture; no real user data"
    uploaded, _ = await upload(client, d, data=raw)
    file = uploaded.json()["attachment"]
    created, _ = await approval(client, d, task_id=d["tasks"][0]["id"], attachment_ids=[file["id"]])
    assert created.status_code == 200
    clone = tmp_path / "restored-synthetic.db"
    with get_db() as source, sqlite3.connect(clone) as target:
        source.backup(target)
    database.reset_connection()
    monkeypatch.setattr(database, "DB_PATH", clone)
    response = await client.get(
        d["base"] + f"/attachments/{file['id']}/download", cookies=d["cookies"]["plan-owner"]
    )
    assert response.status_code == 200 and response.content == raw
    row = (await client.get(d["base"] + "/approvals", cookies=d["cookies"]["plan-owner"])).json()["items"][0]
    assert row["attachments"][0]["sha256"] == file["sha256"] and row["status"] == "pending"
    assert (await decision(client, d, row)).status_code == 200
