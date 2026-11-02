"""Owned single-occurrence calendar, overlap detection and portable ICS export."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import Field, model_validator

from ..database import get_db
from . import store
from .models import ClosedModel, CreateSpace, canonical_instant, clean_text, zone_name
from .recurrence import Recurrence, expand
from .routes import _body, _id, _owner

router = APIRouter(prefix="/api/work/calendar", tags=["work"])


class EventCreate(ClosedModel):
    request_id: str = Field(min_length=36, max_length=36)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    start_at: str = Field(max_length=40)
    end_at: str = Field(max_length=40)
    timezone: str = Field(default="Asia/Shanghai", max_length=100)
    recurrence: Recurrence | None = None

    @model_validator(mode="after")
    def validate_event(self):
        CreateSpace.request_uuid(self.request_id)
        self.title = clean_text(self.title)
        self.description = clean_text(self.description, empty=True)
        self.timezone = zone_name(self.timezone)
        self.start_at = canonical_instant(self.start_at, self.timezone)
        self.end_at = canonical_instant(self.end_at, self.timezone)
        duration = datetime.fromisoformat(self.end_at) - datetime.fromisoformat(self.start_at)
        if not timedelta(0) < duration <= timedelta(days=31):
            raise ValueError("invalid duration")
        expand("validation", self.start_at, self.end_at, self.timezone, self.recurrence)
        return self


class EventUpdate(EventCreate):
    expected_version: int = Field(ge=1)
    cancelled: bool = False


def init_calendar_db():
    if not store.enabled():
        return
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS work_calendar (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL,
            description TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL,
            timezone TEXT NOT NULL, version INTEGER NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS calendar_owner_start ON work_calendar(owner,start_at);
        CREATE TABLE IF NOT EXISTS work_calendar_rules (
            series_id TEXT PRIMARY KEY, rule_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS work_calendar_instances (
            id TEXT PRIMARY KEY, series_id TEXT NOT NULL, owner TEXT NOT NULL,
            start_at TEXT NOT NULL, end_at TEXT NOT NULL, version INTEGER NOT NULL,
            cancelled INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS calendar_instances_owner_start
            ON work_calendar_instances(owner,start_at);
        INSERT OR IGNORE INTO work_calendar_instances
            SELECT id,id,owner,start_at,end_at,version,0,updated_at FROM work_calendar c
            WHERE NOT EXISTS(SELECT 1 FROM work_calendar_rules r WHERE r.series_id=c.id);
        CREATE TRIGGER IF NOT EXISTS calendar_instances_account_delete AFTER DELETE ON users BEGIN
            DELETE FROM work_calendar_rules WHERE series_id IN
                (SELECT series_id FROM work_calendar_instances WHERE owner=OLD.username);
            DELETE FROM work_calendar_instances WHERE owner=OLD.username;
        END;
        CREATE TRIGGER IF NOT EXISTS calendar_account_delete AFTER DELETE ON users BEGIN
            DELETE FROM work_calendar WHERE owner=OLD.username;
        END;
        """)


def _event(row):
    value = dict(row)
    return {
        **{key: row[key] for key in value if key not in {"owner", "rule_json"}},
        "cancelled": bool(row["cancelled"]),
        "recurrence": json.loads(value["rule_json"]) if value.get("rule_json") else None,
    }


def _owned(db, owner, event_id):
    row = db.execute(
        "SELECT c.*,r.rule_json FROM work_calendar c LEFT JOIN work_calendar_rules r ON r.series_id=c.id "
        "WHERE c.owner=? AND c.id=?",
        [owner, event_id],
    ).fetchone()
    if row is None:
        raise HTTPException(404, "日程不存在")
    return row


def _conflicts(db, owner, body, excluding=""):
    rows = db.execute(
        "SELECT DISTINCT i.id,c.title,i.start_at,i.end_at FROM work_calendar_instances i "
        "JOIN work_calendar c ON c.id=i.series_id WHERE i.owner=? AND i.cancelled=0 AND c.cancelled=0 "
        "AND i.series_id<>? AND EXISTS(SELECT 1 FROM work_calendar_instances mine "
        "WHERE mine.series_id=? AND mine.cancelled=0 AND mine.start_at<i.end_at "
        "AND mine.end_at>i.start_at) ORDER BY i.start_at LIMIT 21",
        [owner, excluding, excluding],
    ).fetchall()
    return {"conflicts": [dict(row) for row in rows[:20]], "conflicts_truncated": len(rows) > 20}


def save_event(owner, body, event_id=None):
    init_calendar_db()
    kind = "calendar_update" if event_id else "calendar_create"
    fingerprint = store._fingerprint(kind, event_id or "", body)
    with store._transaction(owner) as db:
        if event_id:
            _owned(db, owner, event_id)
        replay = store._replay(db, owner, body.request_id, fingerprint)
        if replay is not None:
            return {**replay, "replayed": True}
        if event_id:
            current = _owned(db, owner, event_id)
            if current["version"] != body.expected_version:
                raise HTTPException(409, "日程已在另一设备更新，请刷新后修改")
            db.execute(
                "UPDATE work_calendar SET title=?,description=?,start_at=?,end_at=?,timezone=?,"
                "version=version+1,cancelled=?,updated_at=? WHERE id=? AND owner=?",
                [
                    body.title,
                    body.description,
                    body.start_at,
                    body.end_at,
                    body.timezone,
                    int(body.cancelled),
                    int(time.time()),
                    event_id,
                    owner,
                ],
            )
        else:
            if db.execute("SELECT COUNT(*) FROM work_calendar WHERE owner=?", [owner]).fetchone()[0] >= 500:
                raise HTTPException(409, "日程数量已达上限，请先整理已有日程")
            event_id = str(uuid4())
            db.execute(
                "INSERT INTO work_calendar VALUES(?,?,?,?,?,?,?,1,0,?)",
                [
                    event_id,
                    owner,
                    body.title,
                    body.description,
                    body.start_at,
                    body.end_at,
                    body.timezone,
                    int(time.time()),
                ],
            )
        previous = {
            row["id"]: dict(row)
            for row in db.execute("SELECT * FROM work_calendar_instances WHERE series_id=?", [event_id])
        }
        instances = expand(event_id, body.start_at, body.end_at, body.timezone, body.recurrence)
        total = db.execute(
            "SELECT COUNT(*) FROM work_calendar_instances WHERE owner=? AND series_id<>?", [owner, event_id]
        ).fetchone()[0]
        if total + len(instances) > 5000:
            raise HTTPException(409, "日程实例已达上限，请缩短重复范围")
        db.execute("DELETE FROM work_calendar_instances WHERE series_id=?", [event_id])
        db.execute("DELETE FROM work_calendar_rules WHERE series_id=?", [event_id])
        if body.recurrence is not None:
            db.execute(
                "INSERT INTO work_calendar_rules VALUES(?,?)", [event_id, body.recurrence.model_dump_json()]
            )
        for instance in instances:
            old = previous.get(instance["id"])
            db.execute(
                "INSERT INTO work_calendar_instances VALUES(?,?,?,?,?,?,?,?)",
                [
                    instance["id"],
                    event_id,
                    owner,
                    instance["start_at"],
                    instance["end_at"],
                    old["version"] + 1 if old else 1,
                    int(bool(old and old["cancelled"])),
                    int(time.time()),
                ],
            )
        value = {"event": _event(_owned(db, owner, event_id)), **_conflicts(db, owner, body, event_id)}
        store._record_request(db, owner, body, kind, "", event_id, fingerprint, value)
        return {**value, "replayed": False}


@router.post("")
async def create_event(request: Request):
    owner = _owner(request)
    body = await _body(request, EventCreate)
    if _owner(request) != owner:
        raise HTTPException(401, "请重新登录")
    return save_event(owner, body)


@router.put("/{event_id}")
async def update_event(event_id: str, request: Request):
    owner = _owner(request)
    body = await _body(request, EventUpdate)
    if _owner(request) != owner:
        raise HTTPException(401, "请重新登录")
    return save_event(owner, body, _id(event_id))


class OccurrenceUpdate(ClosedModel):
    request_id: str = Field(min_length=36, max_length=36)
    expected_version: int = Field(ge=1)
    cancelled: bool

    @model_validator(mode="after")
    def valid_request(self):
        CreateSpace.request_uuid(self.request_id)
        return self


@router.put("/{event_id}/occurrences/{occurrence_id}")
async def update_occurrence(event_id: str, occurrence_id: str, request: Request):
    owner = _owner(request)
    event_id, occurrence_id = _id(event_id), _id(occurrence_id)
    body = await _body(request, OccurrenceUpdate)
    if _owner(request) != owner:
        raise HTTPException(401, "请重新登录")
    init_calendar_db()
    fingerprint = store._fingerprint("calendar_occurrence", occurrence_id, body)
    with store._transaction(owner) as db:
        _owned(db, owner, event_id)
        row = db.execute(
            "SELECT * FROM work_calendar_instances WHERE id=? AND series_id=? AND owner=?",
            [occurrence_id, event_id, owner],
        ).fetchone()
        if row is None:
            raise HTTPException(404, "日程实例不存在")
        replay = store._replay(db, owner, body.request_id, fingerprint)
        if replay is not None:
            return {**replay, "replayed": True}
        if row["version"] != body.expected_version:
            raise HTTPException(409, "该次日程已更新，请刷新后修改")
        db.execute(
            "UPDATE work_calendar_instances SET cancelled=?,version=version+1,updated_at=? WHERE id=?",
            [int(body.cancelled), int(time.time()), occurrence_id],
        )
        value = {"occurrence_id": occurrence_id, "cancelled": body.cancelled, "version": row["version"] + 1}
        store._record_request(db, owner, body, "calendar_occurrence", "", occurrence_id, fingerprint, value)
        return {**value, "replayed": False}


def agenda(owner, start, end):
    try:
        start = canonical_instant(start, "UTC")
        end = canonical_instant(end, "UTC")
        if (
            start is None
            or end is None
            or not timedelta(0)
            < (datetime.fromisoformat(end) - datetime.fromisoformat(start))
            <= timedelta(days=31)
        ):
            raise ValueError()
    except (ValueError, TypeError, OverflowError):
        raise HTTPException(422, "日程查询需要明确时区的起止时间，范围最多31天") from None
    init_calendar_db()
    with get_db() as db:
        store._active(db, owner)
        instances = db.execute(
            "SELECT i.id AS occurrence_id,i.series_id,i.start_at,i.end_at,i.version,c.version AS series_version,"
            "c.title,c.description,c.timezone FROM work_calendar_instances i JOIN work_calendar c ON c.id=i.series_id "
            "WHERE i.owner=? AND i.cancelled=0 AND c.cancelled=0 AND i.start_at<? "
            "AND i.end_at>? ORDER BY i.start_at,i.id LIMIT 201",
            [owner, end, start],
        ).fetchall()
        series_ids = list(dict.fromkeys(row["series_id"] for row in instances[:200]))
        events = [_event(_owned(db, owner, series_id)) for series_id in series_ids]
        tasks = db.execute(
            "SELECT t.* FROM work_tasks t JOIN work_spaces s ON s.id=t.workspace_id "
            "WHERE s.owner=? AND s.deleted_at IS NULL AND t.status IN ('todo','doing') "
            "AND t.due_at>=? AND t.due_at<? ORDER BY t.due_at,t.id LIMIT 201",
            [owner, start, end],
        ).fetchall()
    return {
        "events": events,
        "occurrences": [dict(row) for row in instances[:200]],
        "tasks": [store._task(row) for row in tasks[:200]],
        "truncated": len(instances) > 200 or len(tasks) > 200,
        "start_at": start,
        "end_at": end,
        "reminders": "in_app",
        "recurrence": True,
    }


@router.get("")
async def read_agenda(request: Request):
    owner = _owner(request)
    return agenda(owner, request.query_params.get("start"), request.query_params.get("end"))


def ics(event, reminder_minutes=0):
    def escape(value):
        return (
            value.replace("\\", "\\\\")
            .replace("\n", "\\n")
            .replace("\r", "")
            .replace(";", "\\;")
            .replace(",", "\\,")
        )

    def stamp(value):
        return datetime.fromisoformat(value).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Prism//Work Calendar//ZH",
        "BEGIN:VEVENT",
        "UID:" + event["id"] + "@prism-work",
        "SEQUENCE:" + str(event["version"]),
        "DTSTAMP:" + datetime.fromtimestamp(event["updated_at"], UTC).strftime("%Y%m%dT%H%M%SZ"),
        "DTSTART:" + stamp(event["start_at"]),
        "DTEND:" + stamp(event["end_at"]),
        "SUMMARY:" + escape(event["title"]),
        "DESCRIPTION:" + escape(event["description"]),
        "STATUS:" + ("CANCELLED" if event["cancelled"] else "CONFIRMED"),
    ]
    for row in event.get("export_instances", []):
        if row["start_at"] != event["start_at"] and not row["cancelled"]:
            lines.append("RDATE:" + stamp(row["start_at"]))
        elif row["cancelled"]:
            lines.append("EXDATE:" + stamp(row["start_at"]))
    if reminder_minutes and not event["cancelled"]:
        lines.extend(
            [
                "BEGIN:VALARM",
                f"TRIGGER:-PT{reminder_minutes}M",
                "ACTION:DISPLAY",
                "DESCRIPTION:" + escape(event["title"]),
                "END:VALARM",
            ]
        )
    lines.extend(["END:VEVENT", "END:VCALENDAR"])
    folded = []
    for line in lines:
        chunk = ""
        for character in line:
            if len((chunk + character).encode()) > 75:
                folded.append(chunk)
                chunk = " "
            chunk += character
        folded.append(chunk)
    return "\r\n".join(folded) + "\r\n"


@router.get("/{event_id}.ics")
async def export_event(
    event_id: str, request: Request, reminder_minutes: int = Query(default=0, ge=0, le=1440)
):
    owner = _owner(request)
    init_calendar_db()
    with get_db() as db:
        store._active(db, owner)
        event = _event(_owned(db, owner, _id(event_id)))
        event["export_instances"] = [
            dict(row)
            for row in db.execute(
                "SELECT start_at,cancelled FROM work_calendar_instances WHERE series_id=? ORDER BY start_at",
                [event_id],
            )
        ]
    return Response(
        ics(event, reminder_minutes),
        media_type="text/calendar",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'attachment; filename="prism-event.ics"',
        },
    )


@router.get("/{event_id}")
async def read_event(event_id: str, request: Request):
    owner = _owner(request)
    init_calendar_db()
    with get_db() as db:
        store._active(db, owner)
        return {"event": _event(_owned(db, owner, _id(event_id)))}
