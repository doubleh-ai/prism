"""Durable terminal-to-diagnostic projection, with no model or network access.

Admission captures release provenance in the same transaction as the run.
Terminal status remains the source of truth; projection and delivery receipt
commit together, so a crash cannot increment an incident twice. No question,
answer, tool payload or provider error text is selected or copied here.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from ..database import get_db
from ..diagnostics import native_outcome_fields, record_diagnostic
from ..work_lifecycle import LifecycleError

log = logging.getLogger("prism.native.diagnostic-delivery")
MAX_BATCH = 50
INTERVAL = 30
_PENDING = """
    FROM native_agent_diagnostic_delivery d
    JOIN native_agent_runs r ON r.run_id=d.run_id
    WHERE d.delivered_at IS NULL
      AND r.status IN ('completed','cancelled','failed','partial')
      AND r.finished_at IS NOT NULL
      AND EXISTS (SELECT 1 FROM users u WHERE u.username=r.username AND u.disabled_at IS NULL)
      AND (r.conv_id IS NULL OR EXISTS (SELECT 1 FROM conversations c
           WHERE c.id=r.conv_id AND c.username=r.username AND c.deleted_at IS NULL))
"""


def deliver_pending(*, run_id=None, username=None, limit=MAX_BATCH):
    if type(limit) is not int or not 1 <= limit <= MAX_BATCH:
        raise ValueError("native_diagnostic_batch_invalid")
    where, params = "", []
    if (run_id is None) != (username is None):
        raise ValueError("native_diagnostic_owner_required")
    if run_id is not None:
        if not isinstance(run_id, str) or not run_id or not isinstance(username, str) or not username:
            raise ValueError("native_diagnostic_owner_required")
        where = " AND r.run_id=? AND r.username=?"
        params = [run_id, username]
    with get_db() as db:
        if db.in_transaction:
            raise RuntimeError("native_diagnostic_transaction_required")
        db.execute("BEGIN IMMEDIATE")
        try:
            rows = db.execute(
                "SELECT r.run_id,r.username,r.status,r.error_code,r.finished_at,d.version "
                + _PENDING
                + where
                + " ORDER BY r.finished_at,r.run_id LIMIT ?",
                [*params, limit],
            ).fetchall()
            for row in rows:
                fields = native_outcome_fields(row["status"], row["error_code"])
                event_id = None
                if fields is not None:
                    event_id = record_diagnostic(
                        username=row["username"],
                        version=row["version"],
                        _connection=db,
                        _occurred_at=row["finished_at"],
                        **fields,
                    )
                db.execute(
                    "UPDATE native_agent_diagnostic_delivery SET delivered_at=?,event_id=? "
                    "WHERE run_id=? AND delivered_at IS NULL",
                    [time.time(), event_id, row["run_id"]],
                )
            db.execute("COMMIT")
            return len(rows)
        except BaseException:
            db.execute("ROLLBACK")
            raise


def pending_count():
    with get_db() as db:
        return db.execute("SELECT COUNT(*) " + _PENDING).fetchone()[0]


class DiagnosticDelivery:
    """Idle time owns no work; each bounded SQL batch obeys maintenance drain."""

    def __init__(self, manager):
        self.manager = manager
        self.task = None
        self.last_success = None
        self.last_failure = None
        self.status = "not_started"

    def tick(self):
        try:
            with self.manager.admit("diagnostic", persist_required=False):
                count = deliver_pending()
            self.last_success = int(time.time())
            self.last_failure = None
            self.status = "ready"
            return count
        except LifecycleError:
            self.status = "maintenance"
            return 0
        except Exception:
            self.last_failure = int(time.time())
            self.status = "unavailable"
            log.warning("native_diagnostic_delivery_unavailable")
            return 0

    async def _run(self):
        while True:
            self.tick()
            await asyncio.sleep(INTERVAL)

    def start(self):
        from .store import init_agent_db

        init_agent_db()
        self.task = asyncio.create_task(self._run())

    async def close(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        self.status = "stopped"

    def health(self):
        try:
            pending = pending_count()
            status = self.status
        except Exception:
            pending, status = None, "unavailable"
        return {
            "status": status,
            "last_success": self.last_success,
            "last_failure": self.last_failure,
            "pending": pending,
            "scope": "native_terminal_diagnostics",
            "model_calls": False,
        }
