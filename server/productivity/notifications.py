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


def _has_projects(db):
    return (
        db.execute("SELECT 1 FROM sqlite_master WHERE name='work_project_approvals'").fetchone() is not None
    )


def _project_due(db, lower, upper, now):
    if not _has_projects(db):
        return []
    approvals = db.execute(
        "SELECT a.id,a.reviewer AS owner,a.version,'approval' AS kind FROM work_project_approvals a "
        "JOIN work_projects p ON p.id=a.project_id JOIN users u ON u.username=p.owner JOIN users r ON r.username=a.reviewer "
        "LEFT JOIN work_project_members m ON m.project_id=p.id AND m.username=a.reviewer "
        "WHERE a.status='pending' AND p.status='active' AND u.disabled_at IS NULL AND r.disabled_at IS NULL "
        "AND (p.owner=a.reviewer OR m.role='editor') "
        "AND NOT EXISTS(SELECT 1 FROM work_notifications n WHERE n.owner=a.reviewer AND n.kind='approval' AND n.target_id=a.id AND n.target_version=a.version) ORDER BY a.sequence LIMIT 100"
    ).fetchall()
    results = db.execute(
        "SELECT a.id,a.requester AS owner,a.version,'approval_result' AS kind FROM work_project_approvals a "
        "JOIN work_projects p ON p.id=a.project_id JOIN users u ON u.username=p.owner JOIN users r ON r.username=a.requester "
        "LEFT JOIN work_project_members m ON m.project_id=p.id AND m.username=a.requester "
        "WHERE a.status<>'pending' AND a.decided_at>=? AND u.disabled_at IS NULL AND r.disabled_at IS NULL "
        "AND (p.owner=a.requester OR m.role IN ('editor','viewer')) "
        "AND NOT EXISTS(SELECT 1 FROM work_notifications n WHERE n.owner=a.requester AND n.kind='approval_result' AND n.target_id=a.id AND n.target_version=a.version) ORDER BY a.sequence LIMIT 100",
        [now - 86400],
    ).fetchall()
    tasks = db.execute(
        "SELECT t.id||':'||t.version AS id,e.assignee AS owner,e.version,'project_task' AS kind FROM work_project_task_plans e "
        "JOIN work_project_tasks l ON l.task_id=e.task_id AND l.project_id=e.project_id JOIN work_projects p ON p.id=e.project_id "
        "JOIN work_tasks t ON t.id=e.task_id JOIN work_spaces s ON s.id=t.workspace_id AND s.owner=p.owner "
        "JOIN users u ON u.username=p.owner JOIN users r ON r.username=e.assignee "
        "JOIN work_project_members m ON m.project_id=p.id AND m.username=e.assignee AND m.role='editor' "
        "WHERE p.status='active' AND l.owner=p.owner AND u.disabled_at IS NULL AND r.disabled_at IS NULL AND s.deleted_at IS NULL "
        "AND t.status IN ('todo','doing') AND t.due_at>? AND t.due_at<=? AND e.assignee<>p.owner "
        "AND NOT EXISTS(SELECT 1 FROM work_notifications n WHERE n.owner=e.assignee AND n.kind='project_task' AND n.target_id=t.id||':'||t.version AND n.target_version=e.version) ORDER BY t.due_at,t.id LIMIT 100",
        [lower, upper],
    ).fetchall()
    return [*approvals, *results, *tasks]


def _project_notices(db, actor):
    if not _has_projects(db):
        return []
    return db.execute(
        "SELECT n.id,n.kind,n.created_at,n.read_at,a.title,NULL AS due_at,'/projects' AS href "
        "FROM work_notifications n JOIN work_project_approvals a ON a.id=n.target_id JOIN work_projects p ON p.id=a.project_id "
        "JOIN users u ON u.username=p.owner LEFT JOIN work_project_members m ON m.project_id=p.id AND m.username=n.owner "
        "WHERE n.owner=? AND n.kind='approval' AND a.reviewer=n.owner AND a.status='pending' AND n.target_version=a.version "
        "AND u.disabled_at IS NULL AND p.status='active' AND (p.owner=n.owner OR m.role='editor') "
        "UNION ALL SELECT n.id,n.kind,n.created_at,n.read_at,a.title||' · '||CASE a.status WHEN 'approved' THEN '已通过' WHEN 'rejected' THEN '未通过' ELSE '已撤回' END,NULL,'/projects' "
        "FROM work_notifications n JOIN work_project_approvals a ON a.id=n.target_id JOIN work_projects p ON p.id=a.project_id "
        "JOIN users u ON u.username=p.owner LEFT JOIN work_project_members m ON m.project_id=p.id AND m.username=n.owner "
        "WHERE n.owner=? AND n.kind='approval_result' AND a.requester=n.owner AND a.status<>'pending' AND n.target_version=a.version "
        "AND u.disabled_at IS NULL AND (p.owner=n.owner OR m.role IN ('editor','viewer')) "
        "UNION ALL SELECT n.id,n.kind,n.created_at,n.read_at,t.title,t.due_at,'/projects' "
        "FROM work_notifications n JOIN work_tasks t ON n.target_id=t.id||':'||t.version "
        "JOIN work_project_task_plans e ON e.task_id=t.id AND e.assignee=n.owner AND e.version=n.target_version "
        "JOIN work_project_tasks l ON l.task_id=t.id AND l.project_id=e.project_id JOIN work_projects p ON p.id=e.project_id "
        "JOIN work_spaces s ON s.id=t.workspace_id AND s.owner=p.owner JOIN users u ON u.username=p.owner "
        "JOIN work_project_members m ON m.project_id=p.id AND m.username=n.owner AND m.role='editor' "
        "WHERE n.owner=? AND n.kind='project_task' AND t.status IN ('todo','doing') AND s.deleted_at IS NULL "
        "AND l.owner=p.owner AND p.status='active' AND u.disabled_at IS NULL ORDER BY 3 DESC,1 LIMIT 100",
        [actor, actor, actor],
    ).fetchall()


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
                "SELECT i.id,i.owner,i.version FROM work_calendar_instances i "
                "JOIN work_calendar c ON c.id=i.series_id JOIN users u ON u.username=i.owner "
                "WHERE u.disabled_at IS NULL AND c.cancelled=0 AND i.cancelled=0 "
                "AND i.start_at<=? AND i.end_at>=? "
                "AND NOT EXISTS(SELECT 1 FROM work_notifications n WHERE n.owner=i.owner "
                "AND n.kind='calendar' AND n.target_id=i.id AND n.target_version=i.version) "
                "ORDER BY i.start_at,i.id LIMIT 100",
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
            project_rows = _project_due(db, lower, upper, int(instant.timestamp()))
            for row in project_rows:
                db.execute(
                    "INSERT OR IGNORE INTO work_notifications VALUES(?,?,?,?,?,?,NULL)",
                    [
                        str(uuid4()),
                        row["owner"],
                        row["kind"],
                        row["id"],
                        row["version"],
                        int(instant.timestamp()),
                    ],
                )
            db.execute("COMMIT")
            return len(events) + len(tasks) + len(project_rows)
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
            "SELECT n.id,n.kind,n.created_at,n.read_at,c.title,i.start_at AS due_at,'/calendar' AS href "
            "FROM work_notifications n JOIN work_calendar_instances i ON i.id=n.target_id AND i.owner=n.owner "
            "JOIN work_calendar c ON c.id=i.series_id "
            "WHERE n.owner=? AND n.kind='calendar' AND n.target_version=i.version AND i.cancelled=0 AND c.cancelled=0 "
            "UNION ALL SELECT n.id,n.kind,n.created_at,n.read_at,t.title,t.due_at,'/work' AS href "
            "FROM work_notifications n JOIN work_tasks t ON t.id=n.target_id "
            "JOIN work_spaces s ON s.id=t.workspace_id AND s.owner=n.owner "
            "WHERE n.owner=? AND n.kind='task' AND n.target_version=t.version "
            "AND t.status IN ('todo','doing') AND s.deleted_at IS NULL ORDER BY 3 DESC,1 LIMIT 100",
            [owner, owner],
        ).fetchall()
        merged = sorted(
            [dict(row) for row in rows] + [dict(row) for row in _project_notices(db, owner)],
            key=lambda r: (-r["created_at"], r["id"]),
        )
        return {
            "notifications": merged[:100],
            "truncated": len(merged) > 100,
            "channel": "in_app",
            "reminder_minutes": 15,
        }


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
