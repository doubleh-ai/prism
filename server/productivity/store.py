"""Transactional, owner-scoped personal workspaces, tasks and finite audit events."""

from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from uuid import uuid4

from fastapi import HTTPException

from ..database import get_db
from .models import CreateSpace, CreateTask, UpdateTask, canonical_instant


def enabled() -> bool:
    return os.environ.get("PRISM_WORK_ENABLED", "1") == "1"


def require_enabled() -> None:
    if not enabled():
        raise HTTPException(404, "工作台尚未启用")


def init_productivity_db() -> None:
    """Explicit additive schema, with no side effects while the feature is off."""
    if not enabled():
        return
    with get_db() as db:
        if db.in_transaction:
            raise RuntimeError("Work schema requires its own transaction")
        db.execute("BEGIN IMMEDIATE")
        try:
            for statement in (
                """CREATE TABLE IF NOT EXISTS work_spaces (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL, timezone TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                    deleted_at INTEGER, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
                )""",
                """CREATE TABLE IF NOT EXISTS work_tasks (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                    workspace_id TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('todo','doing','done','cancelled')),
                    priority TEXT NOT NULL CHECK(priority IN ('low','normal','high')),
                    due_at TEXT, timezone TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
                )""",
                """CREATE INDEX IF NOT EXISTS work_tasks_space_sequence
                    ON work_tasks(workspace_id,sequence DESC)""",
                """CREATE TABLE IF NOT EXISTS work_mutations (
                    owner TEXT NOT NULL, request_id TEXT NOT NULL, kind TEXT NOT NULL,
                    workspace_id TEXT NOT NULL, target_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, response_json TEXT NOT NULL,
                    PRIMARY KEY(owner,request_id)
                )""",
                """CREATE TABLE IF NOT EXISTS work_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id TEXT NOT NULL,
                    task_id TEXT, action TEXT NOT NULL, version INTEGER NOT NULL,
                    changed_fields TEXT NOT NULL, created_at INTEGER NOT NULL
                )""",
                """CREATE INDEX IF NOT EXISTS work_events_task
                    ON work_events(workspace_id,task_id,sequence)""",
                """CREATE TRIGGER IF NOT EXISTS work_account_delete AFTER DELETE ON users BEGIN
                    DELETE FROM work_events WHERE workspace_id IN
                        (SELECT id FROM work_spaces WHERE owner=OLD.username);
                    DELETE FROM work_tasks WHERE workspace_id IN
                        (SELECT id FROM work_spaces WHERE owner=OLD.username);
                    DELETE FROM work_mutations WHERE owner=OLD.username;
                    DELETE FROM work_spaces WHERE owner=OLD.username;
                END""",
            ):
                db.execute(statement)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise


def _active(db, owner):
    if not db.execute("SELECT 1 FROM users WHERE username=? AND disabled_at IS NULL", [owner]).fetchone():
        raise HTTPException(401, "请重新登录")


@contextmanager
def _transaction(owner):
    require_enabled()
    with get_db() as db:
        if db.in_transaction:
            raise RuntimeError("Work mutation requires its own transaction")
        db.execute("BEGIN IMMEDIATE")
        try:
            _active(db, owner)
            yield db
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise


def _workspace(db, owner, workspace_id):
    row = db.execute(
        """SELECT s.* FROM work_spaces s JOIN users u ON u.username=s.owner
           WHERE s.id=? AND s.owner=? AND s.deleted_at IS NULL AND u.disabled_at IS NULL""",
        [workspace_id, owner],
    ).fetchone()
    if row is None:
        raise HTTPException(404, "工作区不存在")
    return row


def _space(row):
    return {key: row[key] for key in ("id", "name", "timezone", "version", "created_at", "updated_at")}


def _task(row):
    return {
        key: row[key]
        for key in (
            "id",
            "workspace_id",
            "title",
            "description",
            "status",
            "priority",
            "due_at",
            "timezone",
            "version",
            "created_at",
            "updated_at",
        )
    }


def _owned_task(db, owner, workspace_id, task_id):
    _workspace(db, owner, workspace_id)
    row = db.execute(
        "SELECT * FROM work_tasks WHERE id=? AND workspace_id=?", [task_id, workspace_id]
    ).fetchone()
    if row is None:
        raise HTTPException(404, "任务不存在")
    return row


def _fingerprint(kind, workspace_id, body):
    value = {"kind": kind, "workspace_id": workspace_id, "body": body.model_dump(exclude={"request_id"})}
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _replay(db, owner, request_id, fingerprint):
    row = db.execute(
        "SELECT fingerprint,response_json FROM work_mutations WHERE owner=? AND request_id=?",
        [owner, request_id],
    ).fetchone()
    if row is None:
        return None
    if row["fingerprint"] != fingerprint:
        raise HTTPException(409, "请求编号已用于其他内容，请刷新核对")
    return json.loads(row["response_json"])


def _record_request(db, owner, body, kind, workspace_id, target_id, fingerprint, response):
    db.execute(
        "INSERT INTO work_mutations VALUES(?,?,?,?,?,?,?)",
        [
            owner,
            body.request_id,
            kind,
            workspace_id,
            target_id,
            fingerprint,
            json.dumps(response, ensure_ascii=False),
        ],
    )


def _event(db, workspace_id, task_id, action, version, fields, now):
    db.execute(
        "INSERT INTO work_events(workspace_id,task_id,action,version,changed_fields,created_at) VALUES(?,?,?,?,?,?)",
        [workspace_id, task_id, action, version, json.dumps(sorted(fields)), now],
    )


def create_space(owner: str, body: CreateSpace) -> dict:
    fingerprint = _fingerprint("space", "", body)
    with _transaction(owner) as db:
        replay = _replay(db, owner, body.request_id, fingerprint)
        if replay is not None:
            _workspace(db, owner, replay["workspace"]["id"])
            return {**replay, "replayed": True}
        if db.execute("SELECT 1 FROM work_spaces WHERE owner=?", [owner]).fetchone():
            raise HTTPException(409, "个人工作区已存在，请刷新核对")
        workspace_id, now = str(uuid4()), int(time.time())
        db.execute(
            "INSERT INTO work_spaces(id,owner,name,timezone,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            [workspace_id, owner, body.name, body.timezone, now, now],
        )
        _event(db, workspace_id, None, "workspace_created", 1, ["name", "timezone"], now)
        response = {"workspace": _space(_workspace(db, owner, workspace_id))}
        _record_request(db, owner, body, "space", workspace_id, workspace_id, fingerprint, response)
        return {**response, "replayed": False}


def create_task(owner: str, workspace_id: str, body: CreateTask) -> dict:
    fingerprint = _fingerprint("task", workspace_id, body)
    with _transaction(owner) as db:
        _workspace(db, owner, workspace_id)
        replay = _replay(db, owner, body.request_id, fingerprint)
        if replay is not None:
            return {**replay, "replayed": True}
        task_id, now = str(uuid4()), int(time.time())
        db.execute(
            """INSERT INTO work_tasks(id,workspace_id,title,description,status,priority,due_at,timezone,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            [
                task_id,
                workspace_id,
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
        _event(
            db, workspace_id, task_id, "task_created", 1, type(body).model_fields.keys() - {"request_id"}, now
        )
        response = {"task": _task(_owned_task(db, owner, workspace_id, task_id))}
        _record_request(db, owner, body, "task", workspace_id, task_id, fingerprint, response)
        return {**response, "replayed": False}


def update_task(owner: str, workspace_id: str, task_id: str, body: UpdateTask) -> dict:
    with _transaction(owner) as db:
        row = _owned_task(db, owner, workspace_id, task_id)
        if row["version"] != body.expected_version:
            raise HTTPException(409, "任务已在其他设备更新，请刷新后再修改")
        changes = body.model_dump(exclude_unset=True, exclude={"expected_version"})
        zone = changes.get("timezone", row["timezone"])
        if "due_at" in changes:
            try:
                changes["due_at"] = canonical_instant(changes["due_at"], zone)
            except (ValueError, OverflowError):
                raise HTTPException(422, "截止时间或时区无效") from None
        changed = {key: value for key, value in changes.items() if value != row[key]}
        if not changed:
            return {"task": _task(row)}
        now = int(time.time())
        # Keys can only originate in the closed UpdateTask model above.
        columns = ",".join(key + "=?" for key in changed)
        result = db.execute(
            "UPDATE work_tasks SET "
            + columns
            + ",version=version+1,updated_at=? WHERE id=? AND workspace_id=? AND version=?",
            [*changed.values(), now, task_id, workspace_id, body.expected_version],
        )
        if result.rowcount != 1:
            raise HTTPException(409, "任务已在其他设备更新，请刷新后再修改")
        _event(db, workspace_id, task_id, "task_updated", row["version"] + 1, changed, now)
        return {"task": _task(_owned_task(db, owner, workspace_id, task_id))}


def _validate_page(limit, before=None, status=None):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise HTTPException(422, "每页数量须为1到100")
    if before is not None and (type(before) is not int or before < 1):
        raise HTTPException(422, "分页位置无效")
    if status is not None and status not in {"todo", "doing", "done", "cancelled"}:
        raise HTTPException(422, "任务状态无效")


def _tasks(db, workspace_id, *, limit, before=None, status=None):
    _validate_page(limit, before, status)
    clauses, arguments = ["workspace_id=?"], [workspace_id]
    if before is not None:
        clauses.append("sequence<?")
        arguments.append(before)
    if status is not None:
        clauses.append("status=?")
        arguments.append(status)
    rows = db.execute(
        "SELECT * FROM work_tasks WHERE " + " AND ".join(clauses) + " ORDER BY sequence DESC LIMIT ?",
        [*arguments, limit + 1],
    ).fetchall()
    return {
        "tasks": [_task(row) for row in rows[:limit]],
        "next_cursor": rows[limit - 1]["sequence"] if len(rows) > limit else None,
    }


def overview(owner: str, *, limit=50):
    require_enabled()
    _validate_page(limit)
    with get_db() as db:
        _active(db, owner)
        row = db.execute("SELECT * FROM work_spaces WHERE owner=? AND deleted_at IS NULL", [owner]).fetchone()
        result = {"workspace": _space(row) if row else None, "tasks": [], "next_cursor": None}
        if row:
            result.update(_tasks(db, row["id"], limit=limit))
    result["capabilities"] = {"tasks": True, "calendar": True, "reminders": False, "agent_actions": False}
    return result


def list_tasks(owner: str, workspace_id: str, *, limit=50, before=None, status=None):
    require_enabled()
    with get_db() as db:
        _active(db, owner)
        _workspace(db, owner, workspace_id)
        return _tasks(db, workspace_id, limit=limit, before=before, status=status)


def task_detail(owner: str, workspace_id: str, task_id: str):
    require_enabled()
    with get_db() as db:
        _active(db, owner)
        row = _owned_task(db, owner, workspace_id, task_id)
        # Bounded recent history, returned chronologically.
        events = db.execute(
            """SELECT action,version,changed_fields,created_at FROM work_events
               WHERE workspace_id=? AND task_id=? ORDER BY sequence DESC LIMIT 100""",
            [workspace_id, task_id],
        ).fetchall()
        return {
            "task": _task(row),
            "history": [
                {**dict(event), "changed_fields": json.loads(event["changed_fields"])}
                for event in reversed(events)
            ],
        }
