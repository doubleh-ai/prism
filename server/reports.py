"""Saved report library endpoints and persistence helpers."""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .auth import require_auth
from .database import get_db
from .models import SavedReportCreateRequest, SavedReportSaveRequest, validation_error_message

router = APIRouter(prefix="/api")


class VibeReportSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="", max_length=160)
    turn_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_\-]{1,128}$")


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
        return None
    if isinstance(value, str):
        return value.replace("[object Object]", "").strip()
    if isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, list):
        out = []
        for item in value:
            cleaned = _sanitize_report_value(item)
            if cleaned in ("", [], {}):
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
            cleaned = _sanitize_report_value(child, force_text=key in _REPORT_TEXT_KEYS)
            if cleaned in ("", [], {}):
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
    """Save one immutable source revision; repeat saves return the same snapshot."""
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
        if (
            payload.get("conv_id")
            and not db.execute(
                "SELECT id FROM conversations WHERE id=? AND username=? AND deleted_at IS NULL",
                [payload["conv_id"], username],
            ).fetchone()
        ):
            raise HTTPException(404, "会话不存在或不可访问")
        existing = None
        if source_id:
            existing = db.execute(
                "SELECT id FROM saved_reports WHERE username=? AND source=? AND source_id=?",
                [username, source, source_id],
            ).fetchone()
        if existing:
            rid = existing["id"]
            # A follow-up must use its new run/attempt ID. Repeated reads and
            # saves cannot rewrite the previous report, timestamps or evidence.
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
    # Only trusted, owner-scoped producers can reserve a server research ID.
    # Otherwise a client could preempt Chat's immutable run snapshot.
    if req.source != "manual":
        raise HTTPException(422, "研究来源由服务端保存；此接口仅接受手动笔记")
    payload = req.model_dump()
    payload["report"] = _manual_report(payload["report"])
    payload["report"]["evidence_status"] = "user_supplied_unverified"
    saved = save_report_snapshot(username, payload)
    return {"ok": True, "report": saved}


def _manual_report(value):
    """User notes cannot mint server evidence IDs, receipts or verification state."""
    reserved = {
        "research",
        "_analysis_meta",
        "research_run_id",
        "research_revision_id",
        "run_id",
        "claim_id",
        "passage_id",
        "document_id",
        "tool_call_id",
        "citation_status",
        "semantic_status",
        "evidence_status",
        "verification_status",
        "source_kind",
        "source_id",
    }
    if isinstance(value, dict):
        return {key: _manual_report(child) for key, child in value.items() if key not in reserved}
    if isinstance(value, list):
        return [_manual_report(child) for child in value]
    return value


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

    saved = save_completed_analysis(username, analysis_id, title=req.title or "", summary=req.summary or "")
    return {"ok": True, "report": saved}


def save_completed_analysis(
    username: str, analysis_id: str, *, title: str = "", summary: str = "", conv_id: str | None = None
) -> dict:
    """Read through the durable owner-scoped service, including after restart."""
    from .analysis_engine import get_analysis_record

    entry = get_analysis_record(analysis_id, username)
    if not entry:
        raise HTTPException(404, "analysis not found")
    if entry.get("status") != "done" or not entry.get("report"):
        raise HTTPException(409, "analysis report is not ready")
    report = entry["report"]
    return save_report_snapshot(
        username,
        {
            "source": "analysis",
            "source_id": analysis_id,
            "conv_id": conv_id,
            "ticker": entry.get("ticker") or "",
            "name": entry.get("name") or "",
            "title": title,
            "signal": entry.get("signal") or report.get("signal") or "",
            "summary": summary,
            "report": report,
        },
    )


def save_vibe_report(
    username: str, plan_id: str, *, title: str = "", turn_id: str | None = None, conv_id: str | None = None
) -> dict:
    from .vibe import get_owned_plan

    plan = get_owned_plan(username, plan_id, recover=False)
    content = plan.get("report_markdown")
    research = plan.get("report_research") or plan.get("research") or {}
    quality = plan.get("report_quality") or plan.get("quality")
    if turn_id:
        turn = next((item for item in plan.get("followups") or [] if item.get("id") == turn_id), None)
        if not turn:
            raise HTTPException(404, "追问不存在或不可访问")
        if turn.get("status") != "complete":
            raise HTTPException(409, "追问尚未产生完整报告")
        content, research, quality = (
            turn.get("answer_markdown"),
            turn.get("research") or {},
            turn.get("quality"),
        )
    if not content:
        raise HTTPException(409, "研究尚未产生报告")
    revision = str(research.get("run_id") or turn_id or "legacy-original")
    return save_report_snapshot(
        username,
        {
            "source": "vibe",
            "source_id": f"{plan_id}:{revision}",
            "conv_id": conv_id,
            "title": title or (str(plan.get("title") or "Vibe研究") + (" · 追问" if turn_id else "")),
            "report": {
                "markdown": content,
                "research": research,
                "quality": quality,
                "plan_id": plan_id,
                "turn_id": turn_id,
                "evidence_status": "unverified_engine_report",
            },
        },
    )


@router.post("/vibe/plans/{plan_id}/save-report")
async def api_save_vibe_report(plan_id: str, body: VibeReportSaveRequest, request: Request):
    saved = save_vibe_report(require_auth(request), plan_id, title=body.title, turn_id=body.turn_id)
    return {"ok": True, "report": saved}
