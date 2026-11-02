"""Account-owned personal projects, reusing Work's mutation and task contracts."""

from __future__ import annotations

import time
from typing import Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field, field_validator

from ..database import get_db
from . import collaboration, store
from .models import ClosedModel, CreateSpace, CreateTask, UpdateTask, clean_text
from .routes import _body, _id, _owner, _query_int

router = APIRouter(prefix="/api/work/projects", tags=["work"])


class ProjectCreate(ClosedModel):
    request_id: str = Field(min_length=36, max_length=36)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=4000)

    _uuid = field_validator("request_id")(CreateSpace.request_uuid.__func__)

    @field_validator("name", "description")
    @classmethod
    def text(cls, value, info):
        return clean_text(value, empty=info.field_name == "description")


class ProjectUpdate(ProjectCreate):
    expected_version: int = Field(ge=1)
    status: Literal["active", "completed", "archived"] = "active"


def init_projects_db():
    if not store.enabled():
        return
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS work_projects (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
            description TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
            version INTEGER NOT NULL DEFAULT 1, updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS projects_owner_updated ON work_projects(owner,updated_at,id);
        CREATE TABLE IF NOT EXISTS work_project_tasks (
            task_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, owner TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS projects_account_delete AFTER DELETE ON users BEGIN
            DELETE FROM work_project_tasks WHERE owner=OLD.username;
            DELETE FROM work_projects WHERE owner=OLD.username;
        END;
        """)
    collaboration.init_collaboration_db()
    from .scheduling import init_scheduling_db

    init_scheduling_db()
    from .approvals import init_approvals_db
    from .attachments import init_attachments_db

    init_attachments_db()
    init_approvals_db()


def _project(db, owner, project_id):
    row = db.execute("SELECT * FROM work_projects WHERE id=? AND owner=?", [project_id, owner]).fetchone()
    if row is None:
        raise HTTPException(404, "项目不存在")
    return {key: value for key, value in dict(row).items() if key != "owner"}


def save_project(owner, body, project_id=None, *, transaction=None):
    if transaction is None:
        init_projects_db()
    kind = "project_update" if project_id else "project_create"
    fingerprint = store._fingerprint(kind, project_id or "", body)
    with store._transaction(owner, transaction) as db:
        if project_id:
            _project(db, owner, project_id)
        replay = store._replay(db, owner, body.request_id, fingerprint)
        if replay is not None:
            return {**replay, "replayed": True}
        now = int(time.time())
        if project_id:
            if _project(db, owner, project_id)["version"] != body.expected_version:
                raise HTTPException(409, "项目已在另一设备更新，请刷新后修改")
            db.execute(
                "UPDATE work_projects SET name=?,description=?,status=?,version=version+1,updated_at=? WHERE id=? AND owner=?",
                [body.name, body.description, body.status, now, project_id, owner],
            )
        else:
            if db.execute("SELECT COUNT(*) FROM work_projects WHERE owner=?", [owner]).fetchone()[0] >= 200:
                raise HTTPException(409, "个人项目数量已达上限")
            project_id = str(uuid4())
            db.execute(
                "INSERT INTO work_projects VALUES(?,?,?,?, 'active',1,?)",
                [project_id, owner, body.name, body.description, now],
            )
        response = {"project": _project(db, owner, project_id)}
        collaboration.audit(
            db, project_id, owner, "project_updated" if kind == "project_update" else "project_created"
        )
        store._record_request(db, owner, body, kind, "", project_id, fingerprint, response)
        return {**response, "replayed": False}


@router.get("")
async def list_projects(request: Request):
    owner = _owner(request)
    init_projects_db()
    with get_db() as db:
        store._active(db, owner)
        rows = db.execute(
            "SELECT DISTINCT p.id FROM work_projects p JOIN users u ON u.username=p.owner "
            "LEFT JOIN work_project_members m ON m.project_id=p.id AND m.username=? "
            "WHERE u.disabled_at IS NULL AND (p.owner=? OR m.role IN ('viewer','editor')) ORDER BY p.updated_at DESC,p.id LIMIT 200",
            [owner, owner],
        ).fetchall()
        return {"projects": [collaboration.view(*collaboration.access(db, owner, row[0])) for row in rows]}


@router.post("")
async def create_project(request: Request):
    owner = _owner(request)
    body = await _body(request, ProjectCreate)
    if _owner(request) != owner:
        raise HTTPException(401, "请重新登录")
    return save_project(owner, body)


@router.patch("/{project_id}")
async def update_project(project_id: str, request: Request):
    owner = _owner(request)
    body = await _body(request, ProjectUpdate)
    if _owner(request) != owner:
        raise HTTPException(401, "请重新登录")
    return save_project(owner, body, _id(project_id))


@router.get("/{project_id}/tasks")
async def project_tasks(project_id: str, request: Request):
    owner = _owner(request)
    init_projects_db()
    limit = _query_int(request.query_params.get("limit"), 200)
    before = _query_int(request.query_params.get("before"))
    if not 1 <= limit <= 200 or (before is not None and before < 1):
        raise HTTPException(422, "项目任务分页参数无效")
    with get_db() as db:
        store._active(db, owner)
        parent, role = collaboration.access(db, owner, _id(project_id))
        project = collaboration.view(parent, role)
        tasks = db.execute(
            "SELECT t.* FROM work_tasks t JOIN work_project_tasks p ON p.task_id=t.id "
            "JOIN work_spaces s ON s.id=t.workspace_id WHERE p.project_id=? AND p.owner=? "
            "AND s.owner=? AND s.deleted_at IS NULL AND (? IS NULL OR t.sequence<?) ORDER BY t.sequence DESC LIMIT ?",
            [project_id, parent["owner"], parent["owner"], before, before, limit + 1],
        ).fetchall()
        return {
            "project": project,
            "tasks": [store._task(row) for row in tasks[:limit]],
            "truncated": len(tasks) > limit,
            "next_cursor": tasks[limit - 1]["sequence"] if len(tasks) > limit else None,
        }


@router.post("/{project_id}/tasks")
async def create_project_task(project_id: str, request: Request):
    owner = _owner(request)
    body = await _body(request, CreateTask)
    if _owner(request) != owner:
        raise HTTPException(401, "请重新登录")
    project_id = _id(project_id)
    return save_project_task(owner, project_id, body)


def save_project_task(owner, project_id, body, *, transaction=None):
    if transaction is None:
        init_projects_db()
    fingerprint = store._fingerprint("project_task", project_id, body)
    with store._transaction(owner, transaction) as db:
        parent, role = collaboration.access(db, owner, project_id, write=True)
        project = collaboration.view(parent, role)
        replay = store._replay(db, owner, body.request_id, fingerprint)
        if replay is not None:
            return {**replay, "replayed": True}
        if project["status"] != "active":
            raise HTTPException(409, "项目已结束或归档，请重新激活后添加任务")
        space = db.execute(
            "SELECT id FROM work_spaces WHERE owner=? AND deleted_at IS NULL", [parent["owner"]]
        ).fetchone()
        if space is None:
            created = store.create_space(
                parent["owner"],
                CreateSpace(
                    request_id=str(
                        uuid5(NAMESPACE_URL, "prism-project-space:" + owner + ":" + body.request_id)
                    ),
                    name="我的工作台",
                    timezone=body.timezone,
                ),
                transaction=db,
            )
            space = (created["workspace"]["id"],)
        task_id, now = str(uuid4()), int(time.time())
        db.execute(
            "INSERT INTO work_tasks(id,workspace_id,title,description,status,priority,due_at,timezone,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            [
                task_id,
                space[0],
                body.title,
                body.description,
                body.status,
                body.priority,
                body.due_at,
                body.timezone,
                now,
                now,
            ],
        )
        db.execute("INSERT INTO work_project_tasks VALUES(?,?,?)", [task_id, project_id, parent["owner"]])
        store._event(db, space[0], task_id, "task_created", 1, ["title", "project"], now)
        response = {
            "project": project,
            "task": store._task(store._owned_task(db, parent["owner"], space[0], task_id)),
        }
        collaboration.audit(db, project_id, owner, "task_created", task_id)
        from .scheduling import bump

        bump(db, project_id)
        store._record_request(db, owner, body, "project_task", space[0], task_id, fingerprint, response)
        return {**response, "replayed": False}


@router.get("/{project_id}")
async def get_project(project_id: str, request: Request):
    actor = _owner(request)
    with get_db() as db:
        return {"project": collaboration.view(*collaboration.access(db, actor, _id(project_id)))}


def project_task(db, actor, project_id, task_id, *, write=False):
    parent, role = collaboration.access(db, actor, project_id, write=write)
    row = db.execute(
        "SELECT t.* FROM work_tasks t JOIN work_project_tasks p ON p.task_id=t.id "
        "JOIN work_spaces s ON s.id=t.workspace_id WHERE p.project_id=? AND p.task_id=? AND p.owner=? "
        "AND s.owner=? AND s.deleted_at IS NULL",
        [project_id, task_id, parent["owner"], parent["owner"]],
    ).fetchone()
    if row is None:
        raise HTTPException(404, "该任务不属于可访问的项目")
    return parent, role, row


def save_task(actor, project_id, task_id, body, *, transaction=None):
    with store._transaction(actor, transaction) as db:
        parent, _, row = project_task(db, actor, project_id, task_id, write=True)
        if parent["status"] != "active":
            raise HTTPException(409, "请先激活项目再修改任务")
        result = store._update_task_record(db, row, body)
        collaboration.audit(db, project_id, actor, "task_updated", task_id)
        return result


@router.patch("/{project_id}/tasks/{task_id}")
async def edit_project_task(project_id: str, task_id: str, request: Request):
    actor = _owner(request)
    body = await _body(request, UpdateTask)
    if _owner(request) != actor:
        raise HTTPException(401)
    return save_task(actor, _id(project_id), _id(task_id), body)


@router.get("/{project_id}/tasks/{task_id}")
async def read_project_task(project_id: str, task_id: str, request: Request):
    actor = _owner(request)
    with get_db() as db:
        parent, role, row = project_task(db, actor, _id(project_id), _id(task_id))
        return {"project": collaboration.view(parent, role), "task": store._task(row)}
