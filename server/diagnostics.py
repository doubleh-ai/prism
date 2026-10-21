"""Privacy-aware anomaly collection and admin diagnostic bundles."""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .auth import require_admin, require_auth
from .database import check_rate_limit, get_db
from .models import DiagnosticEventRequest, validation_error_message
from .release import release_payload


router = APIRouter()

_ALLOWED_CONTEXT_KEYS = {
    "mode", "route", "method", "status", "selector", "resource",
    "request_id", "source", "line", "column", "viewport",
}
_TOKEN_RE = re.compile(
    r"(?i)(?:bearer\s+)?(?:sk-[A-Za-z0-9_-]{8,}|(?:api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+)"
)
_URL_QUERY_RE = re.compile(r"(https?://[^\s?#]+)[?#][^\s]+", re.I)


def _redact(value: Any, limit: int) -> str:
    text = str(value or "").replace("[object Object]", "[rendered-object]")
    text = _URL_QUERY_RE.sub(r"\1?[redacted]", text)
    text = _TOKEN_RE.sub("[redacted]", text)
    return text[:limit]


def _safe_context(context: dict | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (context or {}).items():
        if key not in _ALLOWED_CONTEXT_KEYS:
            continue
        if isinstance(value, str):
            out[key] = _redact(value, 240)
        elif isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
    return out


def record_diagnostic(
    *, username: str = "", kind: str, severity: str = "error",
    component: str = "", page: str = "", message: str,
    stack: str = "", version: str = "", context: dict | None = None,
) -> int:
    now = int(time.time())
    clean_message = _redact(message, 2000) or "Unknown anomaly"
    clean_stack = _redact(stack, 8000)
    clean_context = _safe_context(context)
    signature = "\n".join([
        kind, component, page, clean_message[:600], clean_stack.splitlines()[0] if clean_stack else "", version,
    ])
    fingerprint = hashlib.sha256(signature.encode("utf-8", errors="replace")).hexdigest()[:24]
    with get_db() as db:
        row = db.execute(
            """SELECT id, occurrences FROM diagnostic_events
               WHERE fingerprint=? AND version=? AND username=? AND resolved_at IS NULL
               ORDER BY last_seen DESC LIMIT 1""",
            [fingerprint, version, username],
        ).fetchone()
        if row:
            db.execute(
                """UPDATE diagnostic_events
                   SET occurrences=?, last_seen=?, severity=?, context_json=?
                   WHERE id=?""",
                [int(row["occurrences"] or 0) + 1, now, severity,
                 json.dumps(clean_context, ensure_ascii=False), row["id"]],
            )
            return int(row["id"])
        cur = db.execute(
            """INSERT INTO diagnostic_events
               (username, kind, severity, component, page, message, stack,
                fingerprint, version, context_json, first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            [username, kind, severity, _redact(component, 120), _redact(page, 240),
             clean_message, clean_stack, fingerprint, _redact(version, 40),
             json.dumps(clean_context, ensure_ascii=False), now, now],
        )
        return int(cur.lastrowid)


def _diagnostic_rows(status: str = "open", limit: int = 100) -> list[dict[str, Any]]:
    where = "resolved_at IS NULL" if status != "resolved" else "resolved_at IS NOT NULL"
    with get_db() as db:
        rows = db.execute(
            f"""SELECT * FROM diagnostic_events WHERE {where}
                ORDER BY last_seen DESC LIMIT ?""",
            [max(1, min(500, limit))],
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        try:
            item["context"] = json.loads(item.pop("context_json") or "{}")
        except (TypeError, ValueError):
            item["context"] = {}
            item.pop("context_json", None)
        out.append(item)
    return out


@router.post("/api/diagnostics/client")
async def capture_client_diagnostic(request: Request):
    username = require_auth(request)
    if not check_rate_limit(f"diagnostics:{username}", 60, 60):
        raise HTTPException(429, "diagnostic rate limit exceeded")
    try:
        payload = DiagnosticEventRequest(**((await request.json()) or {}))
    except Exception as exc:
        raise HTTPException(422, validation_error_message(exc))
    event_id = record_diagnostic(
        username=username,
        kind=payload.kind,
        severity=payload.severity,
        component=payload.component or "",
        page=payload.page or "",
        message=payload.message,
        stack=payload.stack or "",
        version=payload.version or "",
        context=payload.context,
    )
    return {"ok": True, "id": event_id}


@router.get("/api/admin/diagnostics")
async def list_diagnostics(request: Request, status: str = "open", limit: int = 100):
    require_admin(request)
    rows = _diagnostic_rows(status, limit)
    stats = {"critical": 0, "error": 0, "warning": 0, "occurrences": 0}
    for row in rows:
        severity = row.get("severity") or "error"
        if severity in stats:
            stats[severity] += 1
        stats["occurrences"] += int(row.get("occurrences") or 1)
    return {"status": status, "events": rows, "stats": stats}


@router.post("/api/admin/diagnostics/{event_id}/resolve")
async def resolve_diagnostic(event_id: int, request: Request):
    require_admin(request)
    with get_db() as db:
        cur = db.execute(
            "UPDATE diagnostic_events SET resolved_at=? WHERE id=? AND resolved_at IS NULL",
            [int(time.time()), event_id],
        )
    if not cur.rowcount:
        raise HTTPException(404, "diagnostic event not found")
    return {"ok": True}


@router.get("/api/admin/diagnostics/bundle")
async def diagnostic_bundle(request: Request, limit: int = 100):
    require_admin(request)
    with get_db() as db:
        failed = db.execute(
            """SELECT created_at, username, feature, action, target_type, target_id,
                      duration_ms, request_id, meta_json
               FROM activity_events WHERE status='failed'
               ORDER BY created_at DESC LIMIT 80"""
        ).fetchall()
    failed_requests = []
    for row in failed:
        item = dict(row)
        try:
            item["meta"] = json.loads(item.pop("meta_json") or "{}")
        except (TypeError, ValueError):
            item["meta"] = {}
            item.pop("meta_json", None)
        failed_requests.append(item)
    return {
        "schema": "prism-codex-diagnostics-v1",
        "generated_at": int(time.time()),
        "release": release_payload(),
        "privacy": "No prompts, passwords, API keys, cookies, or request bodies are collected.",
        "open_anomalies": _diagnostic_rows("open", limit),
        "recent_failed_requests": failed_requests,
    }
