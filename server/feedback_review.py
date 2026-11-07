"""Suggestion-only machine review with durable idempotent decision receipts.

This identity cannot read bugs, user accounts/history or mark anything fixed.
Adoption enters planning; it grants no code, spending, sharing or release right.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import feedback
from .database import check_rate_limit, get_db
from .internal_tokens import load_tokens
from .release import APP_VERSION, _read_deployed_revision

router = APIRouter(prefix="/api/internal/feedback-review", tags=["internal-review"])
HEADERS = {"Cache-Control": "no-store", "Vary": "X-Prism-Review-Token"}
ACTOR = "system:suggestion-review"


def init_review_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS feedback_review_receipts (
            request_id TEXT PRIMARY KEY, feedback_id INTEGER NOT NULL,
            fingerprint TEXT NOT NULL, receipt_json TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS feedback_review_delete AFTER DELETE ON feedback_items BEGIN
            DELETE FROM feedback_review_receipts WHERE feedback_id=OLD.id;
        END;
        """)


def _deny(status, text):
    raise HTTPException(status, text, headers=HEADERS)


def _auth(request):
    peer = request.client.host if request.client else "unix"
    if not check_rate_limit("feedback-review:" + peer, 60, 60):
        _deny(429, "建议评审较频繁")
    tokens = load_tokens()
    value = tokens["review"]
    supplied = request.headers.getlist("X-Prism-Review-Token")
    others = [tokens[k] for k in ("monitor", "worker", "progress")]
    if (
        not 32 <= len(value) <= 512
        or any(not 33 <= ord(c) <= 126 for c in value)
        or value in others
        or len(supplied) != 1
        or not 32 <= len(supplied[0]) <= 512
        or request.headers.get("cookie")
    ):
        _deny(401, "建议评审身份无效")
    if not secrets.compare_digest(
        hashlib.sha256(value.encode()).digest(), hashlib.sha256(supplied[0].encode()).digest()
    ):
        _deny(401, "建议评审身份无效")
    expected = request.headers.getlist("X-Prism-Expected-Revision")
    if (
        len(expected) != 1
        or not re.fullmatch(r"[a-f0-9]{40}", expected[0])
        or expected[0] != _read_deployed_revision()
    ):
        _deny(409, "建议评审运行版本已变化")
    return expected[0]


def _day():
    return datetime.fromtimestamp(time.time(), feedback._SHANGHAI)


def _redact(text):
    value = feedback._text(text)
    value = re.sub(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[联系信息已隐藏]", value)
    return re.sub(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)", "[联系信息已隐藏]", value)


@router.get("/queue")
async def queue(
    request: Request, batch_date: str, after_id: int = Query(0, ge=0), limit: int = Query(10, ge=1, le=10)
):
    revision = _auth(request)
    pairs = list(request.query_params.multi_items())
    if len(pairs) != len(dict(pairs)) or any(
        k not in {"batch_date", "after_id", "limit", "acceptance_id"} for k, _ in pairs
    ):
        _deny(422, "建议评审参数无效")
    acceptance_id = feedback._acceptance_id(request)
    selection = " AND f.id=? AND f.username=?" if acceptance_id is not None else ""
    selected = [acceptance_id, "prism-acceptance"] if acceptance_id is not None else []
    now = _day()
    if batch_date != now.date().isoformat():
        _deny(409, "建议评审批次日期已变化")
    due = (now.hour, now.minute) >= (9, 30)
    with get_db() as db:
        rows = (
            db.execute(
                "SELECT f.id,f.title,f.description,f.feature,f.revision_number,f.review_batch_date "
                "FROM feedback_items f JOIN users u ON u.username=f.username "
                "WHERE u.disabled_at IS NULL AND f.category='suggestion' AND f.status='pending_review' "
                "AND f.review_batch_date<=? AND f.id>?" + selection + " ORDER BY f.id LIMIT ?",
                [batch_date, after_id, *selected, limit + 1],
            ).fetchall()
            if due
            else []
        )
    items = [
        {
            "id": r["id"],
            "title": _redact(r["title"]),
            "description": _redact(r["description"]),
            "feature": r["feature"] if r["feature"] in feedback._FEATURES else "unknown",
            "revision_number": r["revision_number"],
            "review_batch_date": r["review_batch_date"],
        }
        for r in rows[:limit]
    ]
    return JSONResponse(
        {
            "schema": "prism.suggestion-review.queue.v1",
            "batch_date": batch_date,
            "due": due,
            "items": items,
            "next_after_id": items[-1]["id"] if len(rows) > limit else None,
            "release": {"version": APP_VERSION, "revision": revision},
            "user_identity_included": False,
        },
        headers=HEADERS,
    )


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=36, max_length=36)
    expected_revision: int = Field(ge=1)
    batch_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    status: Literal["accepted", "deferred", "declined", "needs_info"]
    reason: str = Field(min_length=4, max_length=1200)
    priority: Literal["low", "normal", "high"] = "normal"
    dependencies: list[str] = Field(default_factory=list, max_length=8)
    risk: Literal["ordinary", "owner_decision"] = "ordinary"
    topic: Literal[
        "research", "work", "experience", "operations", "email_sms", "cross_product_content", "other"
    ] = "other"


def _request_id(value):
    try:
        if str(UUID(value)) != value:
            raise ValueError()
    except (TypeError, ValueError):
        _deny(422, "建议评审请求编号无效")
    return value


@router.get("/receipts/{request_id}")
async def receipt(request_id: str, request: Request):
    _auth(request)
    init_review_db()
    _request_id(request_id)
    with get_db() as db:
        row = db.execute(
            "SELECT receipt_json FROM feedback_review_receipts WHERE request_id=?", [request_id]
        ).fetchone()
    if row is None:
        _deny(404, "建议评审回执未找到；这不能证明写入从未发生")
    return JSONResponse(json.loads(row[0]), headers=HEADERS)


@router.post("/{feedback_id}/decision")
async def decide(feedback_id: int, request: Request):
    revision = _auth(request)
    body = await feedback._payload(request, Decision)
    _auth(request)
    init_review_db()
    _request_id(body.request_id)
    reason = _redact(body.reason)
    if len(reason) < 4 or any(not x.strip() or len(x) > 160 for x in body.dependencies):
        _deny(422, "请提供具体理由及简短依赖")
    if body.status == "accepted" and (
        body.risk != "ordinary" or body.topic in {"email_sms", "cross_product_content"}
    ):
        _deny(422, "该建议需要所有者决定，不能自动采纳")
    fingerprint = hashlib.sha256(
        json.dumps(body.model_dump(), sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            old = db.execute(
                "SELECT feedback_id,fingerprint,receipt_json FROM feedback_review_receipts WHERE request_id=?",
                [body.request_id],
            ).fetchone()
            if old:
                if old["feedback_id"] != feedback_id or old["fingerprint"] != fingerprint:
                    _deny(409, "评审请求编号已使用")
                result = json.loads(old["receipt_json"])
            else:
                now = _day()
                if body.batch_date != now.date().isoformat() or (now.hour, now.minute) < (9, 30):
                    _deny(409, "建议评审批次不在执行时间")
                row = db.execute(
                    "SELECT f.* FROM feedback_items f JOIN users u ON u.username=f.username "
                    "WHERE f.id=? AND u.disabled_at IS NULL AND f.category='suggestion'",
                    [feedback_id],
                ).fetchone()
                if row is None:
                    _deny(404, "建议不存在或不可处理")
                if row["status"] != "pending_review" or row["revision_number"] != body.expected_revision:
                    _deny(409, "建议已经变化，请重新核对")
                if row["review_batch_date"] > body.batch_date:
                    _deny(409, "建议尚未到评审批次")
                stamp = int(time.time())
                if (
                    db.execute(
                        "SELECT COUNT(*) FROM feedback_review_receipts WHERE created_at>=?",
                        [int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())],
                    ).fetchone()[0]
                    >= 200
                ):
                    _deny(429, "已达每日自动评审上限")
                note = {
                    "priority": body.priority,
                    "dependencies": [_redact(x) for x in body.dependencies],
                    "risk": body.risk,
                    "topic": body.topic,
                    "actor": ACTOR,
                }
                triage = json.loads(row["triage_json"])
                triage["review"] = note
                reply = reason + ("\n采纳表示进入计划，尚未上线。" if body.status == "accepted" else "")
                db.execute(
                    "INSERT INTO feedback_updates(feedback_id,actor,old_status,status,public_reply,verification,target_version,target_revision,created_at) VALUES(?,?,?,?,?,'','','',?)",
                    [feedback_id, ACTOR, row["status"], body.status, reply, stamp],
                )
                db.execute(
                    "UPDATE feedback_items SET status=?,public_reply=?,triage_json=?,updated_at=?,revision_number=revision_number+1 WHERE id=?",
                    [body.status, reply, json.dumps(triage, ensure_ascii=False), stamp, feedback_id],
                )
                result = {
                    "schema": "prism.suggestion-review.receipt.v1",
                    "request_id": body.request_id,
                    "feedback_id": feedback_id,
                    "status": body.status,
                    "public_reply": reply,
                    "priority": body.priority,
                    "dependencies": note["dependencies"],
                    "revision_number": body.expected_revision + 1,
                    "batch_date": body.batch_date,
                    "applied_revision": revision,
                    "deployed": False,
                }
                db.execute(
                    "INSERT INTO feedback_review_receipts VALUES(?,?,?,?,?)",
                    [
                        body.request_id,
                        feedback_id,
                        fingerprint,
                        json.dumps(result, ensure_ascii=False),
                        stamp,
                    ],
                )
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    return JSONResponse(result, headers=HEADERS)
