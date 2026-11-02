"""Explicit per-project invitations and roles; no research or other Work access."""

from __future__ import annotations

import time
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field, field_validator

from ..database import check_rate_limit, get_db
from . import store
from .models import ClosedModel, CreateSpace, clean_text
from .routes import _body, _id, _owner

router = APIRouter(prefix="/api/work", tags=["work"])


class VersionRequest(ClosedModel):
    request_id: str = Field(min_length=36, max_length=36)
    expected_version: int = Field(ge=1)
    _uuid = field_validator("request_id")(CreateSpace.request_uuid.__func__)


class Invitation(ClosedModel):
    request_id: str = Field(min_length=36, max_length=36)
    username: str = Field(min_length=1, max_length=100)
    role: Literal["viewer", "editor"] = "viewer"
    _uuid = field_validator("request_id")(CreateSpace.request_uuid.__func__)

    @field_validator("username")
    @classmethod
    def name(cls, v):
        return clean_text(v)


class InvitationResponse(VersionRequest):
    decision: Literal["accept", "decline"]


class MemberChange(VersionRequest):
    role: Literal["viewer", "editor", "removed"]


def init_collaboration_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS work_project_members (
            project_id TEXT NOT NULL,username TEXT NOT NULL,role TEXT NOT NULL,
            version INTEGER NOT NULL,updated_at INTEGER NOT NULL,
            PRIMARY KEY(project_id,username));
        CREATE INDEX IF NOT EXISTS work_project_members_user ON work_project_members(username,role);
        CREATE TABLE IF NOT EXISTS work_project_invitations (
            id TEXT PRIMARY KEY,project_id TEXT NOT NULL,inviter TEXT NOT NULL,
            invitee TEXT NOT NULL,role TEXT NOT NULL,state TEXT NOT NULL,version INTEGER NOT NULL,
            created_at INTEGER NOT NULL,expires_at INTEGER NOT NULL,UNIQUE(project_id,invitee));
        CREATE TABLE IF NOT EXISTS work_project_audit (
            id INTEGER PRIMARY KEY,project_id TEXT NOT NULL,actor TEXT NOT NULL,
            action TEXT NOT NULL,target TEXT NOT NULL,created_at INTEGER NOT NULL);
        CREATE TRIGGER IF NOT EXISTS work_project_collaboration_delete AFTER DELETE ON work_projects BEGIN
            DELETE FROM work_project_members WHERE project_id=OLD.id;
            DELETE FROM work_project_invitations WHERE project_id=OLD.id;
            DELETE FROM work_project_audit WHERE project_id=OLD.id;
        END;
        CREATE TRIGGER IF NOT EXISTS work_project_collaboration_account_delete AFTER DELETE ON users BEGIN
            DELETE FROM work_project_members WHERE username=OLD.username;
            DELETE FROM work_project_invitations WHERE invitee=OLD.username OR inviter=OLD.username;
            UPDATE work_project_audit SET actor='[removed]' WHERE actor=OLD.username;
            UPDATE work_project_audit SET target='[removed]' WHERE target=OLD.username;
        END;
        """)


def access(db, actor, project_id, *, write=False, manage=False):
    store.require_enabled()
    store._active(db, actor)
    row = db.execute(
        "SELECT p.*,m.role AS member_role,m.version AS member_version FROM work_projects p "
        "JOIN users u ON u.username=p.owner LEFT JOIN work_project_members m ON m.project_id=p.id AND m.username=? "
        "WHERE p.id=? AND u.disabled_at IS NULL AND (p.owner=? OR m.role IN ('viewer','editor'))",
        [actor, project_id, actor],
    ).fetchone()
    if row is None:
        raise HTTPException(404, "项目不存在或不可访问")
    role = "owner" if row["owner"] == actor else row["member_role"]
    if manage and role != "owner":
        raise HTTPException(403, "仅项目负责人可以管理成员和项目设置")
    if write and role not in {"owner", "editor"}:
        raise HTTPException(403, "你在该项目中只有查看权限")
    return row, role


def view(row, role):
    return {
        **{k: row[k] for k in ("id", "name", "description", "status", "version", "updated_at")},
        "role": role,
        "owner_name": row["owner"],
        "membership_version": row["member_version"] if role != "owner" else None,
    }


def audit(db, project_id, actor, action, target=""):
    db.execute(
        "INSERT INTO work_project_audit(project_id,actor,action,target,created_at) VALUES(?,?,?,?,?)",
        [project_id, actor, action, target, int(time.time())],
    )
    if action in {
        "invited",
        "accepted",
        "declined",
        "invitation_revoked",
        "member_viewer",
        "member_editor",
        "member_removed",
    }:
        db.execute(
            "UPDATE work_projects SET version=version+1,updated_at=? WHERE id=?",
            [int(time.time()), project_id],
        )


def _record(db, actor, body, kind, target, response):
    store._record_request(db, actor, body, kind, "", target, store._fingerprint(kind, target, body), response)
    return {**response, "replayed": False}


def _drop_assignments(db, project_id, username):
    # The scheduling module is optional in older isolated Work readers.
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='work_project_task_plans'").fetchone():
        changed = db.execute(
            "UPDATE work_project_task_plans SET assignee=NULL,version=version+1 WHERE project_id=? AND assignee=?",
            [project_id, username],
        ).rowcount
        if changed:
            db.execute(
                "UPDATE work_project_schedule_state SET version=version+1 WHERE project_id=?", [project_id]
            )


@router.get("/projects/{project_id}/members")
async def members(project_id: str, request: Request):
    actor = _owner(request)
    project_id = _id(project_id)
    with get_db() as db:
        row, role = access(db, actor, project_id)
        people = [{"username": row["owner"], "role": "owner", "version": None}]
        people.extend(
            dict(r)
            for r in db.execute(
                "SELECT m.username,m.role,m.version FROM work_project_members m JOIN users u ON u.username=m.username WHERE m.project_id=? AND m.role IN ('viewer','editor') AND u.disabled_at IS NULL ORDER BY m.username LIMIT 50",
                [project_id],
            )
        )
        invitations = (
            [
                dict(r)
                for r in db.execute(
                    "SELECT id,invitee AS username,role,state,version,expires_at FROM work_project_invitations WHERE project_id=? AND state='pending' AND expires_at>? ORDER BY created_at LIMIT 50",
                    [project_id, int(time.time())],
                )
            ]
            if role == "owner"
            else []
        )
        return {
            "members": people,
            "invitations": invitations,
            "can_manage": role == "owner",
            "role": role,
            "sharing_scope": "仅本项目说明、任务、附件、审批与成员操作记录，不包含其他工作、日历或投资研究。",
        }


@router.post("/projects/{project_id}/invitations")
async def invite(project_id: str, request: Request):
    actor = _owner(request)
    body = await _body(request, Invitation)
    project_id = _id(project_id)
    if _owner(request) != actor:
        raise HTTPException(401)
    if not check_rate_limit("project-invite:" + actor, 10, 60):
        raise HTTPException(429, "邀请较多，请稍后再试")
    with store._transaction(actor) as db:
        parent, _ = access(db, actor, project_id, manage=True)
        replay = store._replay(
            db, actor, body.request_id, store._fingerprint("project_invite", project_id, body)
        )
        if replay:
            return {**replay, "replayed": True}
        if parent["status"] != "active":
            raise HTTPException(409, "请先激活项目再邀请成员")
        if (
            body.username == actor
            or not db.execute(
                "SELECT 1 FROM users WHERE username=? AND disabled_at IS NULL", [body.username]
            ).fetchone()
        ):
            raise HTTPException(422, "无法邀请该账号，请核对账号与状态")
        member = db.execute(
            "SELECT role FROM work_project_members WHERE project_id=? AND username=?",
            [project_id, body.username],
        ).fetchone()
        if member and member["role"] != "removed":
            raise HTTPException(409, "该账号已是成员，请直接调整权限")
        old = db.execute(
            "SELECT * FROM work_project_invitations WHERE project_id=? AND invitee=?",
            [project_id, body.username],
        ).fetchone()
        now = int(time.time())
        if old and old["state"] == "pending" and old["expires_at"] > now:
            raise HTTPException(409, "已有待接受邀请，请先核对或撤销")
        total = (
            db.execute(
                "SELECT COUNT(*) FROM work_project_members WHERE project_id=? AND role<>'removed'",
                [project_id],
            ).fetchone()[0]
            + db.execute(
                "SELECT COUNT(*) FROM work_project_invitations WHERE project_id=? AND state='pending' AND expires_at>?",
                [project_id, now],
            ).fetchone()[0]
        )
        if total >= 49:
            raise HTTPException(409, "项目成员和待接受邀请已达50人范围")
        invitation_id = old["id"] if old else str(uuid4())
        version = old["version"] + 1 if old else 1
        db.execute(
            "INSERT INTO work_project_invitations VALUES(?,?,?,?,?,'pending',?,?,?) ON CONFLICT(project_id,invitee) DO UPDATE SET inviter=excluded.inviter,role=excluded.role,state='pending',version=excluded.version,created_at=excluded.created_at,expires_at=excluded.expires_at",
            [invitation_id, project_id, actor, body.username, body.role, version, now, now + 7 * 86400],
        )
        audit(db, project_id, actor, "invited", body.username)
        return _record(
            db,
            actor,
            body,
            "project_invite",
            project_id,
            {
                "invitation": {
                    "id": invitation_id,
                    "state": "pending",
                    "role": body.role,
                    "version": version,
                    "expires_at": now + 7 * 86400,
                }
            },
        )


@router.get("/invitations")
async def incoming(request: Request):
    actor = _owner(request)
    with get_db() as db:
        store._active(db, actor)
        rows = db.execute(
            "SELECT i.id,i.project_id,p.name AS project_name,i.inviter,i.role,i.version,i.expires_at FROM work_project_invitations i JOIN work_projects p ON p.id=i.project_id JOIN users u ON u.username=p.owner WHERE i.invitee=? AND i.state='pending' AND i.expires_at>? AND p.status='active' AND u.disabled_at IS NULL ORDER BY i.created_at DESC LIMIT 100",
            [actor, int(time.time())],
        ).fetchall()
        return {"invitations": [dict(r) for r in rows]}


@router.post("/invitations/{invitation_id}/respond")
async def respond(invitation_id: str, request: Request):
    actor = _owner(request)
    body = await _body(request, InvitationResponse)
    invitation_id = _id(invitation_id)
    if _owner(request) != actor:
        raise HTTPException(401)
    with store._transaction(actor) as db:
        row = db.execute(
            "SELECT i.* FROM work_project_invitations i JOIN work_projects p ON p.id=i.project_id JOIN users u ON u.username=p.owner WHERE i.id=? AND i.invitee=? AND u.disabled_at IS NULL AND p.status='active'",
            [invitation_id, actor],
        ).fetchone()
        if row is None:
            raise HTTPException(404, "邀请不存在或不可接受")
        replay = store._replay(
            db, actor, body.request_id, store._fingerprint("project_invitation_response", invitation_id, body)
        )
        if replay:
            return {**replay, "replayed": True}
        if (
            row["state"] != "pending"
            or row["version"] != body.expected_version
            or row["expires_at"] <= time.time()
        ):
            raise HTTPException(409, "邀请已变化或过期，请刷新核对")
        state = "accepted" if body.decision == "accept" else "declined"
        db.execute(
            "UPDATE work_project_invitations SET state=?,version=version+1 WHERE id=?", [state, invitation_id]
        )
        if state == "accepted":
            db.execute(
                "INSERT INTO work_project_members VALUES(?,?,?,1,?) ON CONFLICT(project_id,username) DO UPDATE SET role=excluded.role,version=work_project_members.version+1,updated_at=excluded.updated_at",
                [row["project_id"], actor, row["role"], int(time.time())],
            )
        audit(db, row["project_id"], actor, state, actor)
        return _record(
            db,
            actor,
            body,
            "project_invitation_response",
            invitation_id,
            {
                "invitation": {
                    "id": invitation_id,
                    "project_id": row["project_id"],
                    "state": state,
                    "version": row["version"] + 1,
                }
            },
        )


@router.post("/projects/{project_id}/invitations/{invitation_id}/revoke")
async def revoke(project_id: str, invitation_id: str, request: Request):
    actor = _owner(request)
    body = await _body(request, VersionRequest)
    project_id, invitation_id = _id(project_id), _id(invitation_id)
    if _owner(request) != actor:
        raise HTTPException(401)
    with store._transaction(actor) as db:
        access(db, actor, project_id, manage=True)
        replay = store._replay(
            db, actor, body.request_id, store._fingerprint("project_invitation_revoke", invitation_id, body)
        )
        if replay:
            return {**replay, "replayed": True}
        row = db.execute(
            "SELECT * FROM work_project_invitations WHERE id=? AND project_id=?", [invitation_id, project_id]
        ).fetchone()
        if row is None:
            raise HTTPException(404, "邀请不存在")
        if row["version"] != body.expected_version or row["state"] != "pending":
            raise HTTPException(409, "邀请已有变化；已加入成员需从成员列表移除")
        db.execute(
            "UPDATE work_project_invitations SET state='revoked',version=version+1 WHERE id=?",
            [invitation_id],
        )
        audit(db, project_id, actor, "invitation_revoked", row["invitee"])
        return _record(
            db,
            actor,
            body,
            "project_invitation_revoke",
            invitation_id,
            {"invitation": {"id": invitation_id, "state": "revoked", "version": row["version"] + 1}},
        )


def change_member(db, actor, project_id, username, body, *, leaving=False):
    if leaving:
        replay = store._replay(
            db, actor, body.request_id, store._fingerprint("project_leave", project_id + ":" + actor, body)
        )
        if replay:
            return {**replay, "replayed": True}
    parent, role = access(db, actor, project_id, manage=not leaving)
    if username == parent["owner"]:
        raise HTTPException(409, "项目负责人不能被移除或自行退出")
    if leaving and (username != actor or role == "owner"):
        raise HTTPException(403)
    kind = "project_leave" if leaving else "project_member"
    fingerprint = store._fingerprint(kind, project_id + ":" + username, body)
    replay = store._replay(db, actor, body.request_id, fingerprint)
    if replay:
        return {**replay, "replayed": True}
    row = db.execute(
        "SELECT * FROM work_project_members WHERE project_id=? AND username=?", [project_id, username]
    ).fetchone()
    if row is None:
        raise HTTPException(404, "成员不存在")
    if row["version"] != body.expected_version:
        raise HTTPException(409, "成员权限已有变化，请刷新")
    role = "removed" if leaving else body.role
    db.execute(
        "UPDATE work_project_members SET role=?,version=version+1,updated_at=? WHERE project_id=? AND username=?",
        [role, int(time.time()), project_id, username],
    )
    if role != "editor":
        _drop_assignments(db, project_id, username)
    audit(db, project_id, actor, "member_" + role, username)
    response = {"member": {"username": username, "role": role, "version": row["version"] + 1}}
    store._record_request(db, actor, body, kind, "", project_id, fingerprint, response)
    return {**response, "replayed": False}


@router.put("/projects/{project_id}/members/{username}")
async def update_member(project_id: str, username: str, request: Request):
    actor = _owner(request)
    body = await _body(request, MemberChange)
    if _owner(request) != actor:
        raise HTTPException(401)
    with store._transaction(actor) as db:
        return change_member(db, actor, _id(project_id), username, body)


@router.post("/projects/{project_id}/leave")
async def leave(project_id: str, request: Request):
    actor = _owner(request)
    body = await _body(request, VersionRequest)
    if _owner(request) != actor:
        raise HTTPException(401)
    with store._transaction(actor) as db:
        return change_member(db, actor, _id(project_id), actor, body, leaving=True)


@router.get("/projects/{project_id}/activity")
async def activity(project_id: str, request: Request):
    actor = _owner(request)
    project_id = _id(project_id)
    with get_db() as db:
        access(db, actor, project_id)
        return {
            "items": [
                dict(r)
                for r in db.execute(
                    "SELECT id,actor,action,target,created_at FROM work_project_audit WHERE project_id=? ORDER BY id DESC LIMIT 100",
                    [project_id],
                )
            ]
        }
