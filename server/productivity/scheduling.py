"""Versioned project estimates and dependencies with no model or external calls."""

from __future__ import annotations

import json
from datetime import date

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field, field_validator, model_validator

from ..database import get_db
from . import collaboration, planning, projects, store
from .models import ClosedModel, CreateSpace, canonical_instant, zone_name
from .routes import _body, _id, _owner

router = APIRouter(prefix="/api/work/projects", tags=["work"])
DEFAULT = {
    "start_at": None,
    "timezone": "Asia/Shanghai",
    "work_start": 540,
    "work_end": 1080,
    "weekdays": [0, 1, 2, 3, 4],
    "holidays": [],
}


class Config(ClosedModel):
    start_at: str = Field(max_length=40)
    timezone: str = Field(default="Asia/Shanghai", max_length=100)
    work_start: int = Field(default=540, ge=0, le=1439)
    work_end: int = Field(default=1080, ge=1, le=1440)
    weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4], min_length=1, max_length=7)
    holidays: list[str] = Field(default_factory=list, max_length=366)

    @model_validator(mode="after")
    def valid(self):
        self.timezone = zone_name(self.timezone)
        self.start_at = canonical_instant(self.start_at, self.timezone)
        if self.work_end <= self.work_start:
            raise ValueError("working hours must be within one local day")
        if any(type(d) is not int or not 0 <= d <= 6 for d in self.weekdays) or len(
            set(self.weekdays)
        ) != len(self.weekdays):
            raise ValueError("invalid weekdays")
        if any(date.fromisoformat(v).isoformat() != v for v in self.holidays) or len(
            set(self.holidays)
        ) != len(self.holidays):
            raise ValueError("invalid holidays")
        self.weekdays.sort()
        self.holidays.sort()
        return self


class ConfigUpdate(Config):
    request_id: str = Field(min_length=36, max_length=36)
    expected_version: int = Field(ge=0)
    _uuid = field_validator("request_id")(CreateSpace.request_uuid.__func__)


class Edge(ClosedModel):
    predecessor_id: str
    successor_id: str
    lag_minutes: int = Field(default=0, ge=0, le=43200)
    _uuid = field_validator("predecessor_id", "successor_id")(CreateSpace.request_uuid.__func__)


class Dependencies(ClosedModel):
    request_id: str = Field(min_length=36, max_length=36)
    expected_version: int = Field(ge=0)
    links: list[Edge] = Field(default_factory=list, max_length=4000)
    _uuid = field_validator("request_id")(CreateSpace.request_uuid.__func__)


class TaskPlan(ClosedModel):
    request_id: str = Field(min_length=36, max_length=36)
    expected_version: int = Field(ge=0)
    duration_minutes: int | None = Field(default=None, ge=0, le=100000)
    assignee: str | None = Field(default=None, min_length=1, max_length=100)
    not_before: str | None = Field(default=None, max_length=40)
    milestone: bool | None = None
    _uuid = field_validator("request_id")(CreateSpace.request_uuid.__func__)

    @model_validator(mode="after")
    def valid(self):
        if not self.model_fields_set - {"request_id", "expected_version"}:
            raise ValueError("empty plan change")
        if "milestone" in self.model_fields_set and self.milestone is None:
            raise ValueError("milestone is boolean")
        return self


def init_scheduling_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS work_project_schedule_state (
            project_id TEXT PRIMARY KEY,version INTEGER NOT NULL,config_json TEXT NOT NULL,configured INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS work_project_task_plans (
            task_id TEXT PRIMARY KEY,project_id TEXT NOT NULL,duration_minutes INTEGER,
            assignee TEXT,not_before TEXT,milestone INTEGER NOT NULL DEFAULT 0,version INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS work_project_plans_project ON work_project_task_plans(project_id);
        CREATE TABLE IF NOT EXISTS work_project_dependencies (
            project_id TEXT NOT NULL,predecessor_id TEXT NOT NULL,successor_id TEXT NOT NULL,
            lag_minutes INTEGER NOT NULL,PRIMARY KEY(project_id,predecessor_id,successor_id));
        CREATE TRIGGER IF NOT EXISTS work_schedule_project_delete AFTER DELETE ON work_projects BEGIN
            DELETE FROM work_project_dependencies WHERE project_id=OLD.id;
            DELETE FROM work_project_task_plans WHERE project_id=OLD.id;
            DELETE FROM work_project_schedule_state WHERE project_id=OLD.id;
        END;
        CREATE TRIGGER IF NOT EXISTS work_schedule_task_delete AFTER DELETE ON work_tasks BEGIN
            UPDATE work_project_schedule_state SET version=version+1 WHERE project_id IN
                (SELECT project_id FROM work_project_tasks WHERE task_id=OLD.id);
            DELETE FROM work_project_dependencies WHERE predecessor_id=OLD.id OR successor_id=OLD.id;
            DELETE FROM work_project_task_plans WHERE task_id=OLD.id;
        END;
        CREATE TRIGGER IF NOT EXISTS work_schedule_account_delete AFTER DELETE ON users BEGIN
            UPDATE work_project_schedule_state SET version=version+1 WHERE project_id IN
                (SELECT project_id FROM work_project_task_plans WHERE assignee=OLD.username);
            UPDATE work_project_task_plans SET assignee=NULL,version=version+1 WHERE assignee=OLD.username;
        END;
        """)


def state(db, project_id):
    row = db.execute("SELECT * FROM work_project_schedule_state WHERE project_id=?", [project_id]).fetchone()
    return (
        {
            "version": row["version"],
            "configured": bool(row["configured"]),
            "config": json.loads(row["config_json"]),
        }
        if row
        else {"version": 0, "configured": False, "config": dict(DEFAULT)}
    )


def bump(db, project_id):
    db.execute(
        "INSERT INTO work_project_schedule_state VALUES(?,1,?,0) ON CONFLICT(project_id) DO UPDATE SET version=version+1",
        [project_id, json.dumps(DEFAULT)],
    )
    return state(db, project_id)["version"]


def _available(db, parent, username):
    return (
        username == parent["owner"]
        or db.execute(
            "SELECT 1 FROM work_project_members m JOIN users u ON u.username=m.username WHERE m.project_id=? AND m.username=? AND m.role='editor' AND u.disabled_at IS NULL",
            [parent["id"], username],
        ).fetchone()
        is not None
    )


def tasks(db, parent):
    rows = db.execute(
        "SELECT t.id,t.title,t.status,t.priority,t.due_at,t.version AS task_version,"
        "p.duration_minutes,p.assignee,p.not_before,p.milestone,p.version AS plan_version "
        "FROM work_tasks t JOIN work_project_tasks link ON link.task_id=t.id JOIN work_spaces s ON s.id=t.workspace_id "
        "LEFT JOIN work_project_task_plans p ON p.task_id=t.id WHERE link.project_id=? AND link.owner=? "
        "AND s.owner=? AND s.deleted_at IS NULL ORDER BY t.sequence LIMIT 1001",
        [parent["id"], parent["owner"], parent["owner"]],
    ).fetchall()
    if len(rows) > 1000:
        raise HTTPException(409, "排程最多处理1000项任务，请拆分项目后再安排")
    return [
        {
            **{
                k: r[k]
                for k in (
                    "id",
                    "title",
                    "status",
                    "priority",
                    "due_at",
                    "task_version",
                    "assignee",
                    "not_before",
                )
            },
            "duration_minutes": r["duration_minutes"] if r["duration_minutes"] is not None else 60,
            "duration_source": "user" if r["duration_minutes"] is not None else "default",
            "milestone": bool(r["milestone"]),
            "plan_version": r["plan_version"] or 0,
            "assignee_unavailable": bool(r["assignee"] and not _available(db, parent, r["assignee"])),
        }
        for r in rows
    ]


def links(db, project_id):
    rows = db.execute(
        "SELECT predecessor_id,successor_id,lag_minutes FROM work_project_dependencies WHERE project_id=? ORDER BY predecessor_id,successor_id LIMIT 4001",
        [project_id],
    ).fetchall()
    if len(rows) > 4000:
        raise HTTPException(409, "排程依赖超过4000条，请拆分项目")
    return [dict(r) for r in rows]


def read(db, actor, project_id):
    parent, role = collaboration.access(db, actor, project_id)
    result = state(db, project_id)
    items = tasks(db, parent)
    edges = links(db, project_id)
    computed = None
    if result["configured"]:
        try:
            computed = planning.calculate(result["config"], items, edges)
        except (planning.PlanError, ValueError, OverflowError):
            raise HTTPException(422, "排程无法计算，请核对工作日历、任务范围和依赖") from None
    return {
        **result,
        "tasks": items,
        "links": edges,
        "computed": computed,
        "can_edit": role in {"owner", "editor"} and parent["status"] == "active",
    }


def _parent(db, actor, project_id):
    row, _ = collaboration.access(db, actor, project_id, write=True)
    if row["status"] != "active":
        raise HTTPException(409, "请先激活项目再调整排程")
    return row


def _record(db, actor, body, kind, target, result):
    store._record_request(db, actor, body, kind, "", target, store._fingerprint(kind, target, body), result)
    return {**result, "replayed": False}


def save_config(actor, project_id, body, *, transaction=None):
    with store._transaction(actor, transaction) as db:
        _parent(db, actor, project_id)
        replay = store._replay(
            db, actor, body.request_id, store._fingerprint("project_calendar", project_id, body)
        )
        if replay:
            return {**replay, "replayed": True}
        current = state(db, project_id)
        if current["version"] != body.expected_version:
            raise HTTPException(409, "项目排程已有变化，请刷新")
        config = body.model_dump(exclude={"request_id", "expected_version"})
        try:
            planning.WorkingCalendar(config)
        except (planning.PlanError, ValueError, OverflowError):
            raise HTTPException(422, "工作日历没有可用时间或日期无效") from None
        db.execute(
            "INSERT INTO work_project_schedule_state VALUES(?,?,?,1) ON CONFLICT(project_id) DO UPDATE SET version=excluded.version,config_json=excluded.config_json,configured=1",
            [project_id, current["version"] + 1, json.dumps(config)],
        )
        collaboration.audit(db, project_id, actor, "schedule_configured")
        return _record(
            db,
            actor,
            body,
            "project_calendar",
            project_id,
            {"version": current["version"] + 1, "configured": True, "config": config},
        )


def save_links(actor, project_id, body, *, transaction=None):
    with store._transaction(actor, transaction) as db:
        parent = _parent(db, actor, project_id)
        replay = store._replay(
            db, actor, body.request_id, store._fingerprint("project_dependencies", project_id, body)
        )
        if replay:
            return {**replay, "replayed": True}
        if state(db, project_id)["version"] != body.expected_version:
            raise HTTPException(409, "项目依赖或任务已有变化，请刷新")
        items = {t["id"]: t for t in tasks(db, parent)}
        edges = [e.model_dump() for e in body.links]
        if len({(e["predecessor_id"], e["successor_id"]) for e in edges}) != len(edges):
            raise HTTPException(422, "依赖关系重复")
        try:
            planning.graph(items, edges)
        except planning.PlanError:
            raise HTTPException(422, "依赖存在循环、引用自身或超出本项目") from None
        if any(
            items[e["successor_id"]]["status"] in {"doing", "done"}
            and items[e["predecessor_id"]]["status"] != "done"
            for e in edges
        ):
            raise HTTPException(409, "进行中或已完成任务不能新增未完成的前置任务")
        db.execute("DELETE FROM work_project_dependencies WHERE project_id=?", [project_id])
        db.executemany(
            "INSERT INTO work_project_dependencies VALUES(?,?,?,?)",
            [(project_id, e["predecessor_id"], e["successor_id"], e["lag_minutes"]) for e in edges],
        )
        version = bump(db, project_id)
        collaboration.audit(db, project_id, actor, "dependencies_updated")
        return _record(
            db, actor, body, "project_dependencies", project_id, {"version": version, "links": edges}
        )


def save_plan(actor, project_id, task_id, body, *, transaction=None):
    with store._transaction(actor, transaction) as db:
        parent = _parent(db, actor, project_id)
        projects.project_task(db, actor, project_id, task_id, write=True)
        replay = store._replay(
            db, actor, body.request_id, store._fingerprint("project_task_plan", task_id, body)
        )
        if replay:
            return {**replay, "replayed": True}
        old = db.execute(
            "SELECT * FROM work_project_task_plans WHERE task_id=? AND project_id=?", [task_id, project_id]
        ).fetchone()
        version = old["version"] if old else 0
        if version != body.expected_version:
            raise HTTPException(409, "该任务估算已有变化，请刷新")
        values = {
            k: old[k] if old else (0 if k == "milestone" else None)
            for k in ("duration_minutes", "assignee", "not_before", "milestone")
        }
        patch = body.model_dump(exclude_unset=True, exclude={"request_id", "expected_version"})
        values.update(patch)
        if values["milestone"] and "duration_minutes" not in patch:
            values["duration_minutes"] = 0
        if (values["milestone"] and values["duration_minutes"] != 0) or (
            not values["milestone"] and values["duration_minutes"] == 0
        ):
            raise HTTPException(422, "里程碑工时为0，普通任务需正工时或使用默认估算")
        if values["assignee"] and not _available(db, parent, values["assignee"]):
            raise HTTPException(422, "只能分配给本项目已接受的编辑成员或负责人")
        try:
            values["not_before"] = canonical_instant(
                values["not_before"], state(db, project_id)["config"]["timezone"]
            )
        except (ValueError, OverflowError):
            raise HTTPException(422, "最早开始时间需要有效时区") from None
        db.execute(
            "INSERT INTO work_project_task_plans VALUES(?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET duration_minutes=excluded.duration_minutes,assignee=excluded.assignee,not_before=excluded.not_before,milestone=excluded.milestone,version=excluded.version",
            [
                task_id,
                project_id,
                values["duration_minutes"],
                values["assignee"],
                values["not_before"],
                int(values["milestone"]),
                version + 1,
            ],
        )
        updated = bump(db, project_id)
        collaboration.audit(db, project_id, actor, "task_plan_updated", task_id)
        selected = next(t for t in tasks(db, parent) if t["id"] == task_id)
        return _record(
            db, actor, body, "project_task_plan", task_id, {"plan": selected, "schedule_version": updated}
        )


def guard_status(db, task_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='work_project_dependencies'").fetchone():
        return
    if db.execute(
        "SELECT 1 FROM work_project_dependencies d JOIN work_tasks p ON p.id=d.predecessor_id WHERE d.successor_id=? AND p.status<>'done' LIMIT 1",
        [task_id],
    ).fetchone():
        raise HTTPException(409, "前置任务尚未完成，请先完成或调整依赖")


def guard_reopen(db, task_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='work_project_dependencies'").fetchone():
        return
    if db.execute(
        "SELECT 1 FROM work_project_dependencies d JOIN work_tasks t ON t.id=d.successor_id WHERE d.predecessor_id=? AND t.status IN ('doing','done') LIMIT 1",
        [task_id],
    ).fetchone():
        raise HTTPException(409, "后续任务已开始，请先调整后续任务或依赖再重新打开前置任务")


def task_changed(db, task_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='work_project_schedule_state'").fetchone():
        return
    for row in db.execute("SELECT project_id FROM work_project_tasks WHERE task_id=?", [task_id]):
        bump(db, row[0])


@router.get("/{project_id}/schedule")
async def schedule(project_id: str, request: Request):
    actor = _owner(request)
    with get_db() as db:
        return read(db, actor, _id(project_id))


@router.put("/{project_id}/schedule")
async def update_config(project_id: str, request: Request):
    actor = _owner(request)
    body = await _body(request, ConfigUpdate)
    if _owner(request) != actor:
        raise HTTPException(401)
    return save_config(actor, _id(project_id), body)


@router.put("/{project_id}/dependencies")
async def update_links(project_id: str, request: Request):
    actor = _owner(request)
    body = await _body(request, Dependencies, maximum=524288)
    if _owner(request) != actor:
        raise HTTPException(401)
    return save_links(actor, _id(project_id), body)


@router.put("/{project_id}/tasks/{task_id}/plan")
async def update_plan(project_id: str, task_id: str, request: Request):
    actor = _owner(request)
    body = await _body(request, TaskPlan)
    if _owner(request) != actor:
        raise HTTPException(401)
    return save_plan(actor, _id(project_id), _id(task_id), body)
