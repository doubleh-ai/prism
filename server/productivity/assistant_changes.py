"""Resolve submitted selectors locally; providers never receive an account's records.

The server pins owner, IDs, versions and before-images in the preview. No model
supplies authority, operation identifiers or versions. Confirm/undo are one Work
transaction, so partial changes cannot be mistaken for a completed arrangement.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import Field, model_validator

from ..database import get_db
from . import calendar, projects, store
from .models import ClosedModel, UpdateTask, clean_text


class Clarification(ValueError):
    pass


class TaskFields(ClosedModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=6000)
    status: Literal["todo", "doing", "done", "cancelled"] | None = None
    priority: Literal["low", "normal", "high"] | None = None
    due_at: str | None = Field(default=None, max_length=40)
    timezone: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def valid(self):
        UpdateTask(expected_version=1, **self.model_dump(exclude_unset=True))
        return self


class EventFields(ClosedModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    start_at: str | None = Field(default=None, max_length=40)
    end_at: str | None = Field(default=None, max_length=40)
    timezone: str | None = Field(default=None, max_length=100)
    recurrence: calendar.Recurrence | None = None
    cancelled: bool | None = None

    @model_validator(mode="after")
    def valid(self):
        if not self.model_fields_set or any(
            getattr(self, k) is None for k in self.model_fields_set - {"recurrence"}
        ):
            raise ValueError("nonempty explicit event changes required")
        return self


class ProjectFields(ClosedModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=4000)
    status: Literal["active", "completed", "archived"] | None = None

    @model_validator(mode="after")
    def valid(self):
        if not self.model_fields_set or any(getattr(self, k) is None for k in self.model_fields_set):
            raise ValueError("explicit project changes required")
        return self


class TaskChange(ClosedModel):
    match_title: str = Field(min_length=1, max_length=200)
    changes: TaskFields


class ProjectChange(ClosedModel):
    match_title: str = Field(min_length=1, max_length=100)
    changes: ProjectFields


class EventChange(ClosedModel):
    match_title: str = Field(min_length=1, max_length=200)
    on_date: str | None = Field(default=None, max_length=10)
    scope: Literal["occurrence", "series"] = "occurrence"
    changes: EventFields

    @model_validator(mode="after")
    def valid(self):
        if self.on_date is not None and date.fromisoformat(self.on_date).isoformat() != self.on_date:
            raise ValueError("canonical date required")
        return self


SCHEMAS = {"update_task": TaskChange, "update_event": EventChange, "update_project": ProjectChange}


def _day(word, zone, now):
    return (
        now.astimezone(ZoneInfo(zone)).date() + timedelta(days={"今天": 0, "明天": 1, "后天": 2}[word])
    ).isoformat()


def parse(text, zone, *, now=None):
    """Common whole commands only. Quoting the target disambiguates its boundary."""
    now = now or datetime.now(UTC)
    if len(text) > 300 or any(w in text for w in ("不要", "别", "如果", "例如", "示例", "然后", "；", "\n")):
        return None
    match = re.fullmatch(
        r'(?:帮我)?把任务[「“"]?(.+?)[」”"]?(?:标为|设为)(已完成|进行中|待办|取消|高优先级|普通优先级|低优先级)',
        text,
    )
    if match:
        value = {
            "已完成": ("status", "done"),
            "进行中": ("status", "doing"),
            "待办": ("status", "todo"),
            "取消": ("status", "cancelled"),
            "高优先级": ("priority", "high"),
            "普通优先级": ("priority", "normal"),
            "低优先级": ("priority", "low"),
        }[match[2]]
        return {
            "actions": [
                {
                    "kind": "update_task",
                    "fields": {"match_title": match[1].strip(), "changes": {value[0]: value[1]}},
                }
            ]
        }
    match = re.fullmatch(r'(?:帮我)?把项目[「“"]?(.+?)[」”"]?(?:标为|设为)(已完成|进行中|归档)', text)
    if match:
        return {
            "actions": [
                {
                    "kind": "update_project",
                    "fields": {
                        "match_title": match[1].strip(),
                        "changes": {
                            "status": {"已完成": "completed", "进行中": "active", "归档": "archived"}[
                                match[2]
                            ]
                        },
                    },
                }
            ]
        }
    match = re.fullmatch(
        r'(?:帮我)?把(?P<day>今天|明天|后天)的(?:日程)?[「“"]?(?P<title>.+?)[」”"]?改到(?P<part>上午|下午|晚上|早上|中午)?(?P<hour>\d{1,2})[点:：](?P<minute>\d{1,2}分?)?',
        text,
    )
    if match:
        hour = int(match["hour"])
        minute = int((match["minute"] or "0").rstrip("分"))
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            return None
        if match["part"] in {"下午", "晚上"} and 1 <= hour < 12:
            hour += 12
        day = _day(match["day"], zone, now)
        wall = datetime.fromisoformat(day).replace(hour=hour, minute=minute)
        z = ZoneInfo(zone)
        times = {
            wall.replace(tzinfo=z, fold=f).astimezone(UTC)
            for f in (0, 1)
            if wall.replace(tzinfo=z, fold=f).astimezone(UTC).astimezone(z).replace(tzinfo=None) == wall
        }
        if len(times) != 1:
            return {"clarification": "这个本地时间不存在或有两次，请明确选择时间。"}
        start = times.pop()
        if start <= now:
            return {"clarification": "调整后的时间已经过去，请确认新的日期。"}
        return {
            "actions": [
                {
                    "kind": "update_event",
                    "fields": {
                        "match_title": match["title"].strip(),
                        "on_date": day,
                        "scope": "occurrence",
                        "changes": {"start_at": start.isoformat()},
                    },
                }
            ]
        }
    return None


def instance_signature(db, event_id):
    rows = [
        tuple(r)
        for r in db.execute(
            "SELECT id,start_at,end_at,version,cancelled FROM work_calendar_instances WHERE series_id=? ORDER BY id",
            [event_id],
        )
    ]
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def _one(rows, kind):
    if not rows:
        raise Clarification(f"没有找到本人可操作的同名{kind}，请核对完整名称。")
    if len(rows) != 1:
        raise Clarification(f"找到多个同名{kind}，请补充日期或先在原页面区分名称。")
    return rows[0]


def resolve(owner, kind, fields, zone):
    body = SCHEMAS[kind].model_validate(fields)
    name = clean_text(body.match_title)
    changes = body.changes.model_dump(exclude_unset=True)
    operation_id = str(uuid4())
    with get_db() as db:
        store._active(db, owner)
        if kind == "update_task":
            row = _one(
                db.execute(
                    "SELECT t.* FROM work_tasks t JOIN work_spaces s ON s.id=t.workspace_id WHERE s.owner=? AND s.deleted_at IS NULL AND t.title=? LIMIT 2",
                    [owner, name],
                ).fetchall(),
                "任务",
            )
            before = store._task(row)
            target = {"id": row["id"], "workspace_id": row["workspace_id"], "title": row["title"]}
            patch = UpdateTask(expected_version=row["version"], **changes)
            if patch.due_at is not None:
                patch.due_at = calendar.canonical_instant(patch.due_at, patch.timezone or row["timezone"])
            validated = patch.model_dump(exclude_unset=True)
        elif kind == "update_project":
            row = _one(
                db.execute(
                    "SELECT * FROM work_projects WHERE owner=? AND name=? LIMIT 2", [owner, name]
                ).fetchall(),
                "项目",
            )
            before = projects._project(db, owner, row["id"])
            target = {"id": row["id"], "title": row["name"]}
            validated = projects.ProjectUpdate(
                request_id=operation_id,
                expected_version=row["version"],
                **{k: before[k] for k in ("name", "description", "status") if k not in changes},
                **changes,
            ).model_dump()
        else:
            rows = db.execute(
                "SELECT c.id FROM work_calendar c WHERE c.owner=? AND c.title=? AND c.cancelled=0 LIMIT 2",
                [owner, name],
            ).fetchall()
            # A date can disambiguate two different series with the same name.
            if body.scope == "occurrence" and body.on_date:
                day = date.fromisoformat(body.on_date)
                z = ZoneInfo(zone)
                start = (
                    datetime.combine(day, datetime.min.time(), z)
                    .astimezone(UTC)
                    .isoformat(timespec="microseconds")
                    .replace("+00:00", "Z")
                )
                end = (
                    datetime.combine(day + timedelta(days=1), datetime.min.time(), z)
                    .astimezone(UTC)
                    .isoformat(timespec="microseconds")
                    .replace("+00:00", "Z")
                )
                rows = db.execute(
                    "SELECT i.*,c.version AS series_version,c.title,c.timezone FROM work_calendar_instances i JOIN work_calendar c ON c.id=i.series_id WHERE i.owner=? AND c.owner=? AND c.title=? AND c.cancelled=0 AND i.start_at>=? AND i.start_at<? LIMIT 2",
                    [owner, owner, name, start, end],
                ).fetchall()
                row = _one(rows, "日程")
            else:
                row = _one(rows, "日程")
                parent = calendar._event(calendar._owned(db, owner, row["id"]))
                if body.scope == "occurrence":
                    if parent["recurrence"]:
                        raise Clarification("这是重复日程，请说明哪一天，或明确要调整整组。")
                    row = db.execute(
                        "SELECT i.*,c.version AS series_version,c.title,c.timezone FROM work_calendar_instances i JOIN work_calendar c ON c.id=i.series_id WHERE i.id=? AND i.owner=?",
                        [parent["id"], owner],
                    ).fetchone()
            if body.scope == "occurrence":
                parent = calendar._event(calendar._owned(db, owner, row["series_id"]))
                if parent["recurrence"] is None:
                    body.scope = "series"
                    row = {"id": parent["id"]}
            if body.scope == "occurrence":
                if set(changes) - {"start_at", "end_at", "cancelled"}:
                    raise Clarification("单次日程可调整时间或取消；修改名称等内容请明确调整整组。")
                kind = "update_occurrence"
                before = {k: row[k] for k in ("start_at", "end_at", "cancelled", "version", "series_version")}
                target = {
                    "id": row["id"],
                    "series_id": row["series_id"],
                    "title": row["title"],
                    "timezone": row["timezone"],
                }
                if "start_at" in changes and "end_at" not in changes:
                    changes["end_at"] = (
                        datetime.fromisoformat(changes["start_at"])
                        + (datetime.fromisoformat(row["end_at"]) - datetime.fromisoformat(row["start_at"]))
                    ).isoformat()
                if "end_at" in changes and "start_at" not in changes:
                    changes["start_at"] = row["start_at"]
                validated = calendar.OccurrenceUpdate(
                    request_id=operation_id,
                    expected_version=row["version"],
                    expected_series_version=row["series_version"],
                    **changes,
                ).model_dump(exclude_none=True)
                # Validate wall-time and duration before the user sees a preview.
                if "start_at" in changes:
                    interval = calendar.EventCreate(
                        request_id=operation_id,
                        title=row["title"],
                        start_at=validated["start_at"],
                        end_at=validated["end_at"],
                        timezone=row["timezone"],
                    )
                    validated.update(start_at=interval.start_at, end_at=interval.end_at)
            else:
                before = calendar._event(calendar._owned(db, owner, row["id"]))
                target = {
                    "id": row["id"],
                    "title": before["title"],
                    "instance_signature": instance_signature(db, row["id"]),
                }
                if "start_at" in changes and "end_at" not in changes:
                    changes["end_at"] = (
                        datetime.fromisoformat(changes["start_at"])
                        + (
                            datetime.fromisoformat(before["end_at"])
                            - datetime.fromisoformat(before["start_at"])
                        )
                    ).isoformat()
                base = {
                    k: before[k]
                    for k in (
                        "title",
                        "description",
                        "start_at",
                        "end_at",
                        "timezone",
                        "recurrence",
                        "cancelled",
                    )
                }
                validated = calendar.EventUpdate(
                    request_id=operation_id, expected_version=before["version"], **{**base, **changes}
                ).model_dump()
        meaningful = {
            k: v
            for k, v in validated.items()
            if k not in {"request_id", "expected_version", "expected_series_version", "reset_exceptions"}
        }
        if all(v == before.get(k) for k, v in meaningful.items()):
            raise Clarification("当前安排已经符合要求，不需要再次修改。")
        return {
            "kind": kind,
            "operation_id": operation_id,
            "target": target,
            "before": before,
            "fields": validated,
        }


def apply(owner, action, db):
    kind, target, fields = action["kind"], action["target"], action["fields"]
    if kind == "update_task":
        return store.update_task(
            owner, target["workspace_id"], target["id"], UpdateTask.model_validate(fields), transaction=db
        )
    if kind == "update_project":
        return projects.save_project(
            owner, projects.ProjectUpdate.model_validate(fields), target["id"], transaction=db
        )
    if kind == "update_occurrence":
        return calendar.save_occurrence(
            owner,
            target["series_id"],
            target["id"],
            calendar.OccurrenceUpdate.model_validate(fields),
            transaction=db,
        )
    if instance_signature(db, target["id"]) != target["instance_signature"]:
        raise HTTPException(409, "单次日程已有变化，请重新预览")
    result = calendar.save_event(
        owner, calendar.EventUpdate.model_validate(fields), target["id"], transaction=db
    )
    return {**result, "instance_signature": instance_signature(db, target["id"])}


def undo(owner, action, result, request_id, db):
    kind, target, before = action["kind"], action["target"], action["before"]
    if kind == "update_task":
        return store.update_task(
            owner,
            target["workspace_id"],
            target["id"],
            UpdateTask(
                expected_version=result["task"]["version"],
                **{
                    k: before[k] for k in ("title", "description", "status", "priority", "due_at", "timezone")
                },
            ),
            transaction=db,
        )
    if kind == "update_project":
        return projects.save_project(
            owner,
            projects.ProjectUpdate(
                request_id=request_id,
                expected_version=result["project"]["version"],
                **{k: before[k] for k in ("name", "description", "status")},
            ),
            target["id"],
            transaction=db,
        )
    if kind == "update_occurrence":
        return calendar.save_occurrence(
            owner,
            target["series_id"],
            target["id"],
            calendar.OccurrenceUpdate(
                request_id=request_id,
                expected_version=result["version"],
                expected_series_version=result["series_version"],
                start_at=before["start_at"],
                end_at=before["end_at"],
                cancelled=bool(before["cancelled"]),
            ),
            transaction=db,
        )
    if instance_signature(db, target["id"]) != result["instance_signature"]:
        raise HTTPException(409, "单次日程已有后续变化，不能覆盖撤销")
    return calendar.save_event(
        owner,
        calendar.EventUpdate(
            request_id=request_id,
            expected_version=result["event"]["version"],
            **{
                k: before[k]
                for k in ("title", "description", "start_at", "end_at", "timezone", "recurrence", "cancelled")
            },
        ),
        target["id"],
        transaction=db,
    )
