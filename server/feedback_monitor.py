"""Read-only monitoring signals; no user text, write identity or model access."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from . import feedback
from .database import check_rate_limit, get_db
from .release import APP_VERSION, _read_deployed_revision

router = APIRouter()
_HEADER = "X-Prism-Monitor-Token"
_ENV = "PRISM_FEEDBACK_MONITOR_TOKEN"
_HEADERS = {"Cache-Control": "no-store", "Vary": _HEADER}
_KINDS = frozenset(
    {
        "js_error",
        "promise_rejection",
        "render_object_artifact",
        "chart_blank",
        "blank_panel",
        "api_network_error",
        "api_http_5xx",
        "server_exception",
        "server_http_5xx",
        "server_native_run_failed",
        "server_native_run_incomplete",
    }
)


def _deny(status: int = 401) -> None:
    raise HTTPException(status, "监测身份无效" if status == 401 else "监测查询无效", headers=_HEADERS)


def _auth(request: Request) -> None:
    host = request.client.host if request.client else "local-unix-socket"
    if not check_rate_limit("feedback-monitor-auth:" + host, 60, 60):
        _deny()
    expected = os.environ.get(_ENV, "")
    supplied = request.headers.getlist(_HEADER)
    worker = os.environ.get("PRISM_FEEDBACK_WORKER_TOKEN", "")
    # A configured write token can never also be the monitoring identity.
    if (
        not 32 <= len(expected) <= 512
        or any(c.isspace() for c in expected)
        or expected == worker
        or len(supplied) != 1
        or not 32 <= len(supplied[0]) <= 512
    ):
        _deny()
    if not secrets.compare_digest(
        hashlib.sha256(expected.encode()).digest(), hashlib.sha256(supplied[0].encode()).digest()
    ):
        _deny()


def _query(request: Request, *, queue: bool) -> tuple[int, int, int, str]:
    allowed = {"since", "after_id", "limit"} | ({"category"} if queue else set())
    pairs = list(request.query_params.multi_items())
    if any(k not in allowed for k, _ in pairs) or len(pairs) != len(dict(pairs)):
        _deny(422)
    params = dict(pairs)
    numbers = []
    for name, default, ceiling in (
        ("since", "0", int(time.time())),
        ("after_id", "0", 2**53 - 1),
        ("limit", "50", 100),
    ):
        raw = params.get(name, default)
        if not re.fullmatch(r"[0-9]{1,16}", raw) or not 0 <= int(raw) <= ceiling:
            _deny(422)
        numbers.append(int(raw))
    if not numbers[2]:
        _deny(422)
    category = params.get("category", "bug")
    if category not in {"bug", "suggestion"}:
        _deny(422)
    return *numbers, category


def _version(value) -> str:
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"v?[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}", value)
        else ""
    )


def _revision(value) -> str:
    return value[:12] if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{12}|[a-f0-9]{40}", value) else ""


def _integer(value) -> int:
    return value if type(value) is int and 0 <= value <= 2**53 - 1 else 0


def _reply(rows, items, *, limit: int, started_at: int, kind: str):
    return JSONResponse(
        {
            "schema": "prism.feedback-monitor.v1",
            "kind": kind,
            "poll_started_at": started_at,
            "next_after_id": items[-1]["id"] if len(rows) > limit else None,
            "items": items,
            "release": {"version": APP_VERSION, "revision": _revision(_read_deployed_revision())},
            "read_only": True,
            "signals_verified": False,
            "user_text_included": False,
        },
        headers=_HEADERS,
    )


@router.get("/api/internal/feedback-monitor/queue")
async def queue_signals(request: Request):
    _auth(request)
    started_at = int(time.time())
    since, after_id, limit, category = _query(request, queue=True)
    # Deliberately avoid selecting titles, descriptions, identity, page paths,
    # triage JSON, reply/verification text or update history.
    with get_db() as db:
        rows = db.execute(
            """SELECT id, category, feature, status, version, revision,
                      revision_number, created_at, updated_at
               FROM feedback_items WHERE category=? AND updated_at>=? AND id>?
               ORDER BY id LIMIT ?""",
            [category, since, after_id, limit + 1],
        ).fetchall()
    items = [
        {
            "id": _integer(row["id"]),
            "category": category,
            "feature": row["feature"] if row["feature"] in feedback._FEATURES else "unknown",
            "status": row["status"] if row["status"] in feedback._STATUSES[category] else "unknown",
            "version": _version(row["version"]),
            "revision": _revision(row["revision"]),
            "revision_number": _integer(row["revision_number"]),
            "created_at": _integer(row["created_at"]),
            "updated_at": _integer(row["updated_at"]),
        }
        for row in rows[:limit]
    ]
    return _reply(rows, items, limit=limit, started_at=started_at, kind="queue")


@router.get("/api/internal/feedback-monitor/diagnostics")
async def diagnostic_signals(request: Request):
    _auth(request)
    started_at = int(time.time())
    since, after_id, limit, _ = _query(request, queue=False)
    kinds = sorted(_KINDS)
    with get_db() as db:
        rows = db.execute(
            """SELECT id,kind,severity,version,substr(message,1,512) AS message,occurrences,first_seen,last_seen
               FROM diagnostic_events WHERE resolved_at IS NULL AND last_seen>=? AND id>?
               AND kind IN ("""
            + ",".join("?" for _ in kinds)
            + ") ORDER BY id LIMIT ?",
            [since, after_id, *kinds, limit + 1],
        ).fetchall()
    items = []
    for row in rows[:limit]:
        version = _version(row["version"])
        # Existing decoder may derive a method identifier from user text. Omit
        # that open vocabulary: monitoring receives only fixed codes/classes.
        code = {
            k: v
            for k, v in feedback._diagnostic_code(row["kind"], row["message"]).items()
            if k in {"error_code", "exception_class", "http_status"}
        }
        items.append(
            {
                "id": _integer(row["id"]),
                "kind": row["kind"],
                "severity": row["severity"]
                if row["severity"] in {"info", "warning", "error", "critical"}
                else "unknown",
                "version": version,
                "version_scope": ("current_version" if version == APP_VERSION else "historical_version")
                if version
                else "unattributed",
                "occurrences": _integer(row["occurrences"]),
                "first_seen": _integer(row["first_seen"]),
                "last_seen": _integer(row["last_seen"]),
                **code,
            }
        )
    return _reply(rows, items, limit=limit, started_at=started_at, kind="diagnostics")
