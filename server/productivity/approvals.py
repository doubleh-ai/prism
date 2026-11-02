"""Explicit human project decisions over a frozen, versioned request."""

from __future__ import annotations

import json
import time
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field, field_validator, model_validator

from ..database import get_db
from . import attachments, collaboration, projects, store
from .models import ClosedModel, CreateSpace, clean_text
from .routes import _body, _id, _owner, _query_int

router = APIRouter(prefix="/api/work/projects", tags=["work"])


class Create(ClosedModel):
    request_id: str = Field(min_length=36, max_length=36)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    reviewer: str = Field(min_length=1, max_length=100)
    task_id: str | None = None
    attachment_ids: list[str] = Field(default_factory=list, max_length=10)
    _uuid = field_validator("request_id")(CreateSpace.request_uuid.__func__)

    @field_validator("title", "reviewer", "description")
    @classmethod
    def text(cls, value, info):
        return clean_text(value, empty=info.field_name == "description")

    @model_validator(mode="after")
    def identifiers(self):
        for value in self.attachment_ids + ([self.task_id] if self.task_id is not None else []):
            CreateSpace.request_uuid(value)
        if len(set(self.attachment_ids)) != len(self.attachment_ids):
            raise ValueError("duplicate attachments")
        return self


class Decision(collaboration.VersionRequest):
    decision: Literal["approved", "rejected"]
    comment: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def reason(self):
        self.comment = clean_text(self.comment, empty=self.decision != "rejected")
        return self


def init_approvals_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS work_project_approvals (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,
            project_id TEXT NOT NULL,requester TEXT NOT NULL,reviewer TEXT NOT NULL,
            title TEXT NOT NULL,description TEXT NOT NULL,task_id TEXT,
            snapshot_json TEXT NOT NULL,context_json TEXT NOT NULL,
            status TEXT NOT NULL,version INTEGER NOT NULL,created_at INTEGER NOT NULL,
            decided_at INTEGER,comment TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS work_approvals_project ON work_project_approvals(project_id,sequence);
        CREATE INDEX IF NOT EXISTS work_approvals_reviewer ON work_project_approvals(reviewer,status,sequence);
        CREATE TRIGGER IF NOT EXISTS work_approvals_project_delete AFTER DELETE ON work_projects BEGIN
            DELETE FROM work_project_approvals WHERE project_id=OLD.id;
        END;
        CREATE TRIGGER IF NOT EXISTS work_approvals_account_delete AFTER DELETE ON users BEGIN
            DELETE FROM work_project_approvals WHERE requester=OLD.username OR reviewer=OLD.username;
        END;
        """)


def item(row):
    snapshot = json.loads(row["snapshot_json"])
    return {
        **{
            k: row[k]
            for k in (
                "id",
                "title",
                "description",
                "requester",
                "reviewer",
                "status",
                "version",
                "task_id",
                "created_at",
                "decided_at",
                "comment",
            )
        },
        "attachments": snapshot["attachments"],
        "task_snapshot": snapshot["task"],
    }


def owned(db, actor, project_id, approval_id):
    parent, role = collaboration.access(db, actor, project_id)
    row = db.execute(
        "SELECT * FROM work_project_approvals WHERE project_id=? AND id=?", [project_id, approval_id]
    ).fetchone()
    if row is None:
        raise HTTPException(404, "审批不存在或不可访问")
    return parent, role, row


def reviewers(db, parent):
    return [
        parent["owner"],
        *(
            r[0]
            for r in db.execute(
                "SELECT m.username FROM work_project_members m JOIN users u ON u.username=m.username WHERE m.project_id=? AND m.role='editor' AND u.disabled_at IS NULL ORDER BY m.username LIMIT 50",
                [parent["id"]],
            )
        ),
    ]


def listing(db, actor, project_id, *, before=None, limit=50):
    parent, role = collaboration.access(db, actor, project_id)
    rows = db.execute(
        "SELECT * FROM work_project_approvals WHERE project_id=? AND (? IS NULL OR sequence<?) ORDER BY sequence DESC LIMIT ?",
        [project_id, before, before, limit + 1],
    ).fetchall()
    pending = db.execute(
        "SELECT * FROM work_project_approvals WHERE project_id=? AND reviewer=? AND status='pending' ORDER BY sequence LIMIT 101",
        [project_id, actor],
    ).fetchall()
    return {
        "items": [item(r) for r in rows[:limit]],
        "next_cursor": rows[limit - 1]["sequence"] if len(rows) > limit else None,
        "pending_for_me": [item(r) for r in pending[:100]],
        "pending_for_me_truncated": len(pending) > 100,
        "reviewers": [u for u in reviewers(db, parent) if u != actor],
        "can_submit": role in {"owner", "editor"} and parent["status"] == "active",
    }


def snapshot(db, actor, parent, task_id, attachment_ids):
    task = None
    if task_id:
        _, _, row = projects.project_task(db, actor, parent["id"], task_id)
        task = {
            k: row[k]
            for k in ("id", "title", "description", "status", "priority", "due_at", "timezone", "version")
        }
    files = []
    for key in attachment_ids:
        row = attachments.owned(db, actor, parent["id"], key)[2]
        if row["state"] != "active" or row["data"] is None:
            raise HTTPException(409, "所选附件已移除，请重新核对")
        files.append({k: row[k] for k in ("id", "filename", "size", "sha256", "version")})
    return {"task": task, "attachments": files}


def context(db, actor, reviewer, project_id):
    _, role = collaboration.access(db, actor, project_id, write=True)
    parent, reviewer_role = collaboration.access(db, reviewer, project_id, write=True)
    requester_row = collaboration.access(db, actor, project_id)[0]
    return {
        "requester_role": role,
        "requester_version": requester_row["member_version"],
        "reviewer_role": reviewer_role,
        "reviewer_version": parent["member_version"],
    }


def create(actor, project_id, body, *, transaction=None, expected_snapshot=None, expected_context=None):
    with store._transaction(actor, transaction) as db:
        parent, _ = collaboration.access(db, actor, project_id, write=True)
        if parent["status"] != "active":
            raise HTTPException(409, "请先激活项目再发起审批")
        fingerprint = store._fingerprint("project_approval_create", project_id, body)
        replay = store._replay(db, actor, body.request_id, fingerprint)
        if replay:
            current = owned(db, actor, project_id, replay["approval"]["id"])[2]
            return {"approval": item(current), "replayed": True}
        if actor == body.reviewer or body.reviewer not in reviewers(db, parent):
            raise HTTPException(422, "请选择另一位项目负责人或已接受邀请的编辑成员审批")
        pinned = snapshot(db, actor, parent, body.task_id, body.attachment_ids)
        identities = context(db, actor, body.reviewer, project_id)
        if expected_snapshot is not None and pinned != expected_snapshot:
            raise HTTPException(409, "审批内容在预览后已有变化，请重新整理")
        if expected_context is not None and identities != expected_context:
            raise HTTPException(409, "审批人员权限已有变化，请重新整理")
        if (
            db.execute(
                "SELECT COUNT(*) FROM work_project_approvals WHERE project_id=? AND status='pending'",
                [project_id],
            ).fetchone()[0]
            >= 200
        ):
            raise HTTPException(409, "待处理审批较多，请先处理已有申请")
        key = str(uuid4())
        db.execute(
            "INSERT INTO work_project_approvals(id,project_id,requester,reviewer,title,description,task_id,snapshot_json,context_json,status,version,created_at) VALUES(?,?,?,?,?,?,?,?,?,'pending',1,?)",
            [
                key,
                project_id,
                actor,
                body.reviewer,
                body.title,
                body.description,
                body.task_id,
                json.dumps(pinned, ensure_ascii=False),
                json.dumps(identities),
                int(time.time()),
            ],
        )
        response = {"approval": item(owned(db, actor, project_id, key)[2])}
        store._record_request(
            db, actor, body, "project_approval_create", "", project_id, fingerprint, response
        )
        collaboration.audit(db, project_id, actor, "approval_requested", key)
        return {**response, "replayed": False}


def decide(actor, project_id, approval_id, body, *, cancelling=False, transaction=None):
    with store._transaction(actor, transaction) as db:
        parent, role, row = owned(db, actor, project_id, approval_id)
        if role not in {"owner", "editor"}:
            raise HTTPException(403, "查看成员不能处理审批")
        if cancelling:
            if actor != row["requester"] and role != "owner":
                raise HTTPException(403, "只有申请人或负责人可以撤回")
        elif actor != row["reviewer"]:
            raise HTTPException(403, "只有本次指定审批人可以作出决定")
        kind = "project_approval_cancel" if cancelling else "project_approval_decision"
        fingerprint = store._fingerprint(kind, approval_id, body)
        replay = store._replay(db, actor, body.request_id, fingerprint)
        if replay:
            return {"approval": item(row), "replayed": True}
        if row["version"] != body.expected_version or row["status"] != "pending":
            raise HTTPException(409, "审批已处理或已有变化，请刷新核对")
        if not cancelling:
            if parent["status"] != "active":
                raise HTTPException(409, "项目已结束或归档，请核对审批是否仍需处理")
            try:
                identities = context(db, row["requester"], row["reviewer"], project_id)
                actual = snapshot(
                    db,
                    actor,
                    parent,
                    row["task_id"],
                    [f["id"] for f in json.loads(row["snapshot_json"])["attachments"]],
                )
            except HTTPException:
                raise HTTPException(409, "审批人员或关联内容已失效，请撤回后重新发起") from None
            if identities != json.loads(row["context_json"]) or actual != json.loads(row["snapshot_json"]):
                raise HTTPException(409, "任务、附件或成员权限已变化，请撤回后重新发起")
        status = "cancelled" if cancelling else body.decision
        comment = "" if cancelling else body.comment
        db.execute(
            "UPDATE work_project_approvals SET status=?,version=version+1,decided_at=?,comment=? WHERE id=?",
            [status, int(time.time()), comment, approval_id],
        )
        response = {"approval": item(owned(db, actor, project_id, approval_id)[2])}
        store._record_request(db, actor, body, kind, "", approval_id, fingerprint, response)
        collaboration.audit(db, project_id, actor, "approval_" + status, approval_id)
        return {**response, "replayed": False}


@router.get("/{project_id}/approvals")
async def list_approvals(project_id: str, request: Request):
    actor = _owner(request)
    limit, before = (
        _query_int(request.query_params.get("limit"), 50),
        _query_int(request.query_params.get("before")),
    )
    if not 1 <= limit <= 100 or (before is not None and before < 1):
        raise HTTPException(422, "审批分页参数无效")
    with get_db() as db:
        return listing(db, actor, _id(project_id), limit=limit, before=before)


@router.post("/{project_id}/approvals")
async def post_approval(project_id: str, request: Request):
    actor = _owner(request)
    body = await _body(request, Create)
    if _owner(request) != actor:
        raise HTTPException(401)
    return create(actor, _id(project_id), body)


@router.post("/{project_id}/approvals/{approval_id}/decision")
async def post_decision(project_id: str, approval_id: str, request: Request):
    actor = _owner(request)
    body = await _body(request, Decision)
    if _owner(request) != actor:
        raise HTTPException(401)
    return decide(actor, _id(project_id), _id(approval_id), body)


@router.post("/{project_id}/approvals/{approval_id}/cancel")
async def cancel_approval(project_id: str, approval_id: str, request: Request):
    actor = _owner(request)
    body = await _body(request, collaboration.VersionRequest)
    if _owner(request) != actor:
        raise HTTPException(401)
    return decide(actor, _id(project_id), _id(approval_id), body, cancelling=True)
