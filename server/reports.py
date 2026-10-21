"""Saved report library endpoints and persistence helpers."""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .auth import require_auth
from .database import get_db
from .models import SavedReportCreateRequest, SavedReportSaveRequest, validation_error_message

router = APIRouter(prefix="/api")


def _text_from_value(value: Any) -> str:
    """Extract readable text from Claude/content blocks without stringifying objects."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.replace("[object Object]", "").strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(x for x in (_text_from_value(item) for item in value) if x).strip()
    if isinstance(value, dict):
        if value.get("type") == "tool_use":
            return ""
        if value.get("type") == "text" and "text" in value:
            return _text_from_value(value.get("text"))
        if value.get("type") == "thinking" and "thinking" in value:
            return _text_from_value(value.get("thinking"))
        if value.get("type") == "tool_result" and "content" in value:
            return _text_from_value(value.get("content"))
        message = value.get("message")
        if isinstance(message, dict) and "content" in message:
            return _text_from_value(message.get("content"))
        for key in (
            "markdown",
            "text",
            "content",
            "summary",
            "thesis",
            "verdict_summary",
            "final_decision",
            "rationale",
            "decision",
            "plan",
            "output",
            "description",
        ):
            if key in value:
                text = _text_from_value(value.get(key))
                if text:
                    return text
        parts: list[str] = []
        for key, child in value.items():
            if key in {"id", "type", "role", "name", "source_id", "conv_id", "created_at", "updated_at"}:
                continue
            text = _text_from_value(child)
            if text:
                parts.append(text)
        return "\n".join(parts).strip()
    return str(value).replace("[object Object]", "").strip()


_REPORT_TEXT_KEYS = {
    "markdown",
    "text",
    "content",
    "summary",
    "thesis",
    "verdict_summary",
    "final_decision",
    "final_trade_decision",
    "trader_plan",
    "trade_plan",
    "decision",
    "rationale",
    "reason",
    "plan",
    "output",
    "description",
    "question",
}


def _sanitize_report_value(value: Any, *, force_text: bool = False) -> Any:
    """Clean report JSON before storing/returning it.

    Older chat snapshots can contain Claude content blocks or browser-stringified
    object artifacts. Report details should never render raw objects, but we also
    keep structured metric dictionaries intact when they are useful data.
    """
    if force_text:
        return _text_from_value(value)
    if value is None:
        return ""
    if isinstance(value, str):
        return value.replace("[object Object]", "").strip()
    if isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, list):
        out = []
        for item in value:
            cleaned = _sanitize_report_value(item)
            if cleaned in ("", None, [], {}):
                continue
            out.append(cleaned)
        return out
    if isinstance(value, dict):
        typ = value.get("type")
        if typ == "tool_use":
            return ""
        if typ in {"text", "thinking", "tool_result"}:
            return _text_from_value(value)
        out = {}
        for key, child in value.items():
            if key in {"id", "type", "role", "source_id", "conv_id"}:
                continue
            cleaned = _sanitize_report_value(child, force_text=key in _REPORT_TEXT_KEYS)
            if cleaned in ("", None, [], {}):
                continue
            out[str(key)] = cleaned
        return out
    return str(value).replace("[object Object]", "").strip()


def _sanitize_report_payload(report: Any) -> dict:
    if not isinstance(report, dict):
        text = _text_from_value(report)
        return {"final_decision": text} if text else {}
    cleaned = _sanitize_report_value(report)
    return cleaned if isinstance(cleaned, dict) else {}


def _compact_summary(report: Any) -> str:
    if not isinstance(report, dict):
        return ""
    structured = report.get("structured") if isinstance(report.get("structured"), dict) else {}
    for value in (
        structured.get("thesis"),
        structured.get("verdict_summary"),
        report.get("summary"),
        report.get("question"),
        report.get("final_decision"),
        report.get("trader_plan"),
    ):
        text = _text_from_value(value)
        if text:
            return " ".join(text.split())[:240]
    return ""


def _signal_from(report: Any, fallback: str = "") -> str:
    if isinstance(report, dict):
        structured = report.get("structured") if isinstance(report.get("structured"), dict) else {}
        for value in (structured.get("signal"), report.get("signal"), fallback):
            text = _text_from_value(value)
            if text:
                return text.splitlines()[0].strip()[:32]
    return _text_from_value(fallback)[:32]


def save_report_snapshot(username: str, payload: dict) -> dict:
    """Insert or update a user's saved report snapshot."""
    report = _sanitize_report_payload(payload.get("report") or {})
    now = int(time.time())
    source = (payload.get("source") or "manual").strip()[:32]
    source_id = (payload.get("source_id") or "").strip()[:128] or None
    ticker = (payload.get("ticker") or "").strip()[:32]
    name = (payload.get("name") or "").strip()[:80]
    signal = _signal_from(report, payload.get("signal") or "")
    summary = (_text_from_value(payload.get("summary")) or _compact_summary(report)).strip()[:500]
    title = _text_from_value(payload.get("title") or "").strip()
    if not title:
        title = " ".join(x for x in [ticker, name, signal] if x) or "Untitled Report"
    title = title[:160]
    report_json = json.dumps(report, ensure_ascii=False, separators=(",", ":"))

    with get_db() as db:
        existing = None
        if source_id:
            existing = db.execute(
                "SELECT id FROM saved_reports WHERE username=? AND source=? AND source_id=?",
                [username, source, source_id],
            ).fetchone()
        if existing:
            rid = existing["id"]
            db.execute(
                """
                UPDATE saved_reports
                SET title=?, ticker=?, name=?, signal=?, summary=?, report_json=?,
                    conv_id=?, updated_at=?
                WHERE id=? AND username=?
                """,
                [
                    title,
                    ticker,
                    name,
                    signal,
                    summary,
                    report_json,
                    payload.get("conv_id"),
                    now,
                    rid,
                    username,
                ],
            )
        else:
            rid = secrets.token_hex(12)
            db.execute(
                """
                INSERT INTO saved_reports
                    (id, username, source, source_id, conv_id, ticker, name, title,
                     signal, summary, report_json, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    rid,
                    username,
                    source,
                    source_id,
                    payload.get("conv_id"),
                    ticker,
                    name,
                    title,
                    signal,
                    summary,
                    report_json,
                    now,
                    now,
                ],
            )
    saved = get_saved_report(username, rid)
    if not saved:
        raise HTTPException(500, "saved report missing after write")
    return saved


def list_saved_reports(username: str, limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit or 50), 200))
    with get_db() as db:
        rows = db.execute(
            """
            SELECT id, source, source_id, conv_id, ticker, name, title, signal,
                   summary, created_at, updated_at
            FROM saved_reports
            WHERE username=?
            ORDER BY updated_at DESC, created_at DESC
            LIMIT ?
            """,
            [username, limit],
        ).fetchall()
    return [dict(row) for row in rows]


def list_saved_reports_for_conv(username: str, conv_id: str, limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit or 50), 200))
    with get_db() as db:
        rows = db.execute(
            """
            SELECT id, source, source_id, conv_id, ticker, name, title, signal,
                   summary, created_at, updated_at
            FROM saved_reports
            WHERE username=? AND conv_id=?
            ORDER BY updated_at DESC, created_at DESC
            LIMIT ?
            """,
            [username, conv_id, limit],
        ).fetchall()
    return [dict(row) for row in rows]


def get_saved_report(username: str, report_id: str) -> dict | None:
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM saved_reports WHERE id=? AND username=?",
            [report_id, username],
        ).fetchone()
    if not row:
        return None
    out = dict(row)
    try:
        out["report"] = _sanitize_report_payload(json.loads(out.pop("report_json") or "{}"))
    except json.JSONDecodeError:
        out["report"] = {}
    return out


def delete_saved_report(username: str, report_id: str) -> bool:
    with get_db() as db:
        cur = db.execute(
            "DELETE FROM saved_reports WHERE id=? AND username=?",
            [report_id, username],
        )
    return cur.rowcount > 0


@router.get("/reports/library")
async def api_list_saved_reports(request: Request, limit: int = 50):
    username = require_auth(request)
    return {"reports": list_saved_reports(username, limit=limit)}


@router.post("/reports/library")
async def api_create_saved_report(request: Request):
    username = require_auth(request)
    try:
        body = await request.json()
        req = SavedReportCreateRequest(**(body or {}))
    except Exception as exc:
        raise HTTPException(422, validation_error_message(exc))
    saved = save_report_snapshot(username, req.model_dump())
    return {"ok": True, "report": saved}


@router.get("/reports/library/{report_id}")
async def api_get_saved_report(report_id: str, request: Request):
    username = require_auth(request)
    saved = get_saved_report(username, report_id)
    if not saved:
        raise HTTPException(404, "report not found")
    return saved


@router.delete("/reports/library/{report_id}")
async def api_delete_saved_report(report_id: str, request: Request):
    username = require_auth(request)
    if not delete_saved_report(username, report_id):
        raise HTTPException(404, "report not found")
    return {"ok": True}


@router.post("/analysis/{analysis_id}/save-report")
async def api_save_analysis_report(analysis_id: str, request: Request):
    username = require_auth(request)
    try:
        body = await request.json()
        req = SavedReportSaveRequest(**(body or {}))
    except Exception as exc:
        raise HTTPException(422, validation_error_message(exc))

    from .analysis_engine import _store

    entry = _store.get(analysis_id)
    if not entry or entry.get("username") != username:
        raise HTTPException(404, "analysis not found")
    if entry.get("status") != "done" or not entry.get("report"):
        raise HTTPException(409, "analysis report is not ready")

    report = entry["report"]
    payload = {
        "source": "analysis",
        "source_id": analysis_id,
        "ticker": entry.get("ticker") or "",
        "name": entry.get("name") or "",
        "title": req.title or "",
        "signal": entry.get("signal") or report.get("signal") or "",
        "summary": req.summary or "",
        "report": report,
    }
    saved = save_report_snapshot(username, payload)
    return {"ok": True, "report": saved}
