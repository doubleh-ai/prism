"""Deterministic server-side Work reminders. No model or external delivery."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request

from ..auth import require_admin
from ..database import get_db
from ..work_lifecycle import LifecycleError
from . import store
from .routes import _id, _owner

router = APIRouter(prefix="/api/work/notifications", tags=["work"])
log = logging.getLogger(__name__)


def init_notifications_db():
    if not store.enabled():
        return
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS work_notifications (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL,
            target_id TEXT NOT NULL, target_version INTEGER NOT NULL,
            created_at INTEGER NOT NULL, read_at INTEGER,
            UNIQUE(owner,kind,target_id,target_version)
        );
        CREATE INDEX IF NOT EXISTS notifications_owner ON work_notifications(owner,created_at,id);
        CREATE TRIGGER IF NOT EXISTS notifications_account_delete AFTER DELETE ON users BEGIN
            DELETE FROM work_notifications WHERE owner=OLD.username;
        END;
        """)


def collect_due(*, now=None):
    """Atomic bounded insertion; repeated ticks and multiple workers deduplicate."""
    if not store.enabled():
        return 0
    instant = datetime.fromtimestamp(now if now is not None else time.time(), UTC)
    upper = (instant + timedelta(minutes=15)).isoformat(timespec="microseconds").replace("+00:00", "Z")
    lower = (instant - timedelta(days=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")
    current = instant.isoformat(timespec="microseconds").replace("+00:00", "Z")
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            events = db.execute(
                "SELECT c.id,c.owner,c.version FROM work_calendar c JOIN users u ON u.username=c.owner "
                "WHERE u.disabled_at IS NULL AND c.cancelled=0 AND c.start_at<=? AND c.end_at>=? "
                "AND NOT EXISTS(SELECT 1 FROM work_notifications n WHERE n.owner=c.owner "
                "AND n.kind='calendar' AND n.target_id=c.id AND n.target_version=c.version) "
                "ORDER BY c.start_at,c.id LIMIT 100",
                [upper, current],
            ).fetchall()
            tasks = db.execute(
                "SELECT t.id,s.owner,t.version FROM work_tasks t JOIN work_spaces s ON s.id=t.workspace_id "
                "JOIN users u ON u.username=s.owner WHERE u.disabled_at IS NULL AND s.deleted_at IS NULL "
                "AND t.status IN ('todo','doing') AND t.due_at>? AND t.due_at<=? "
                "AND NOT EXISTS(SELECT 1 FROM work_notifications n WHERE n.owner=s.owner "
                "AND n.kind='task' AND n.target_id=t.id AND n.target_version=t.version) "
                "ORDER BY t.due_at,t.id LIMIT 100",
                [lower, upper],
            ).fetchall()
            for kind, rows in (("calendar", events), ("task", tasks)):
                for row in rows:
                    db.execute(
                        "INSERT OR IGNORE INTO work_notifications VALUES(?,?,?,?,?,?,NULL)",
                        [
                            str(uuid4()),
                            row["owner"],
                            kind,
                            row["id"],
                            row["version"],
                            int(instant.timestamp()),
                        ],
                    )
            db.execute("COMMIT")
            return len(events) + len(tasks)
        except BaseException:
            db.execute("ROLLBACK")
            raise


@router.get("")
async def list_notifications(request: Request):
    owner = _owner(request)
    init_notifications_db()
    with get_db() as db:
        store._active(db, owner)
        rows = db.execute(
            "SELECT n.id,n.kind,n.created_at,n.read_at,c.title,c.start_at AS due_at,'/calendar' AS href "
            "FROM work_notifications n JOIN work_calendar c ON c.id=n.target_id AND c.owner=n.owner "
            "WHERE n.owner=? AND n.kind='calendar' AND n.target_version=c.version AND c.cancelled=0 "
            "UNION ALL SELECT n.id,n.kind,n.created_at,n.read_at,t.title,t.due_at,'/work' AS href "
            "FROM work_notifications n JOIN work_tasks t ON t.id=n.target_id "
            "JOIN work_spaces s ON s.id=t.workspace_id AND s.owner=n.owner "
            "WHERE n.owner=? AND n.kind='task' AND n.target_version=t.version "
            "AND t.status IN ('todo','doing') AND s.deleted_at IS NULL ORDER BY 3 DESC,1 LIMIT 100",
            [owner, owner],
        ).fetchall()
        return {"notifications": [dict(row) for row in rows], "channel": "in_app", "reminder_minutes": 15}


@router.get("/health")
async def reminder_health(request: Request):
    require_admin(request)
    scheduler = getattr(request.app.state, "work_reminders", None)
    return {
        "enabled": store.enabled(),
        "running": bool(scheduler and scheduler.task and not scheduler.task.done()),
        "last_success": scheduler.last_success if scheduler else None,
        "last_failure": scheduler.last_failure if scheduler else None,
        "channel": "in_app",
    }


@router.post("/{notification_id}/read")
async def read_notification(notification_id: str, request: Request):
    owner = _owner(request)
    init_notifications_db()
    with store._transaction(owner) as db:
        row = db.execute(
            "SELECT id FROM work_notifications WHERE owner=? AND id=?", [owner, _id(notification_id)]
        ).fetchone()
        if row is None:
            raise HTTPException(404, "提醒不存在")
        db.execute(
            "UPDATE work_notifications SET read_at=COALESCE(read_at,?) WHERE owner=? AND id=?",
            [int(time.time()), owner, notification_id],
        )
    return {"ok": True}


class ReminderScheduler:
    """Idle waits own no lease; each committed batch participates in drain."""

    def __init__(self, manager):
        self.manager = manager
        self.task = None
        self.last_success = None
        self.last_failure = None

    def tick(self):
        try:
            with self.manager.admit("capability", persist_required=False):
                count = collect_due()
            self.last_success = int(time.time())
            self.last_failure = None
            return count
        except LifecycleError:
            # Maintenance owns admission. A skipped tick never changes it.
            return 0
        except Exception:
            self.last_failure = int(time.time())
            log.warning("Work reminder batch failed; details withheld")
            return 0

    async def _run(self):
        while True:
            self.tick()
            await asyncio.sleep(30)

    def start(self):
        if store.enabled():
            init_notifications_db()
            self.task = asyncio.create_task(self._run())

    async def close(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
