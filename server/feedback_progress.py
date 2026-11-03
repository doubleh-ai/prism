"""Bug-only machine progress: fixed public messages, CAS and immutable receipts.

No report text, user identity, diagnostics, model access or fixed/deployed
decision is exposed. A separate verification identity must close real fixes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import feedback
from .database import check_rate_limit, get_db
from .release import APP_VERSION, _read_deployed_revision

router = APIRouter(prefix="/api/internal/feedback-progress", tags=["internal-progress"])
HEADER = "X-Prism-Progress-Token"
ENV = "PRISM_FEEDBACK_PROGRESS_TOKEN"
HEADERS = {"Cache-Control": "no-store", "Vary": HEADER}
ACTOR = "system:bug-progress"
STAGES = {
    "triaging": ("triaging", "已收到问题，正在核对运行版本与复现条件。", 0),
    "reproducing": ("in_progress", "正在隔离环境尝试复现，尚未确认修复。", 10),
    "candidate_validated": ("in_progress", "修复候选已完成所选检查，尚待审阅和上线验证。", 20),
    "validating": ("in_progress", "修复草稿已提交，等待自动检查结果；尚未上线。", 30),
    "awaiting_review": ("in_progress", "自动检查已完成，等待代码审阅；尚未上线。", 40),
    "awaiting_release": ("in_progress", "修复方案待发布，尚未完成线上验证。", 50),
    "needs_info": ("needs_info", "当前证据不足，问题和已有结果已保留，待补充核对。", None),
    "not_reproduced": ("needs_info", "原版本未复现该问题，正在核对具体触发条件。", None),
    "blocked": ("in_progress", "部分操作结果尚未确认，已保留现场并等待核对；尚未确认修复。", None),
    "validation_failed": ("in_progress", "修复检查未通过，正在进一步排查，尚未确认修复。", None),
}


def init_progress_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS feedback_progress_receipts (
                request_id TEXT PRIMARY KEY,feedback_id INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,receipt_json TEXT NOT NULL,created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS feedback_progress_state (
                feedback_id INTEGER PRIMARY KEY,stage TEXT NOT NULL,high_water INTEGER NOT NULL,
                revision_number INTEGER NOT NULL,runtime_revision TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS feedback_progress_delete AFTER DELETE ON feedback_items BEGIN
                DELETE FROM feedback_progress_receipts WHERE feedback_id=OLD.id;
                DELETE FROM feedback_progress_state WHERE feedback_id=OLD.id;
            END;
        """)


def _deny(status, message):
    raise HTTPException(status, message, headers=HEADERS)


def _auth(request):
    peer = request.client.host if request.client else "unix"
    if not check_rate_limit("feedback-progress:" + peer, 60, 60):
        _deny(429, "进度更新较频繁")
    expected = os.environ.get(ENV, "")
    supplied = request.headers.getlist(HEADER)
    others = [
        os.environ.get(k, "")
        for k in (
            "PRISM_FEEDBACK_MONITOR_TOKEN",
            "PRISM_FEEDBACK_REVIEW_TOKEN",
            "PRISM_FEEDBACK_WORKER_TOKEN",
        )
    ]
    if not (
        32 <= len(expected) <= 512
        and all(33 <= ord(c) <= 126 for c in expected)
        and expected not in others
        and len(supplied) == 1
        and 32 <= len(supplied[0]) <= 512
        and not request.headers.get("cookie")
    ):
        _deny(401, "进度更新身份无效")
    if not secrets.compare_digest(
        hashlib.sha256(expected.encode()).digest(), hashlib.sha256(supplied[0].encode()).digest()
    ):
        _deny(401, "进度更新身份无效")
    revision = request.headers.getlist("X-Prism-Expected-Revision")
    current = _read_deployed_revision()
    if len(revision) != 1 or not re.fullmatch(r"[a-f0-9]{40}", revision[0]) or revision[0] != current:
        _deny(409, "进度更新运行版本已变化")
    if request.query_params:
        _deny(422, "进度更新参数无效")
    return current


class Progress(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=36, max_length=36)
    expected_revision: int = Field(ge=1, le=2**53 - 1)
    reported_revision: str = Field(pattern=r"^[a-f0-9]{12}$")
    stage: Literal[
        "triaging",
        "reproducing",
        "candidate_validated",
        "validating",
        "awaiting_review",
        "awaiting_release",
        "needs_info",
        "not_reproduced",
        "blocked",
        "validation_failed",
    ]


def _request_id(value):
    try:
        if str(UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        _deny(422, "进度更新请求编号无效")
    return value


@router.get("/receipts/{request_id}")
async def receipt(request_id: str, request: Request):
    _auth(request)
    _request_id(request_id)
    init_progress_db()
    with get_db() as db:
        row = db.execute(
            "SELECT r.receipt_json FROM feedback_progress_receipts r JOIN feedback_items f ON f.id=r.feedback_id JOIN users u ON u.username=f.username WHERE r.request_id=? AND f.category='bug' AND u.disabled_at IS NULL",
            [request_id],
        ).fetchone()
    if row is None:
        _deny(404, "进度回执未找到；这不能证明写入从未发生")
    return JSONResponse(json.loads(row[0]), headers=HEADERS)


@router.post("/{feedback_id}/update")
async def update(feedback_id: int, request: Request):
    revision = _auth(request)
    body = await feedback._payload(request, Progress)
    if _auth(request) != revision:
        _deny(409, "进度更新运行版本已变化")
    _request_id(body.request_id)
    init_progress_db()
    fingerprint = hashlib.sha256(
        json.dumps(body.model_dump(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            # Select metadata only. This identity never obtains a report body.
            row = db.execute(
                "SELECT f.id,f.category,f.status,f.version,f.revision,f.revision_number FROM feedback_items f JOIN users u ON u.username=f.username WHERE f.id=? AND f.category='bug' AND u.disabled_at IS NULL",
                [feedback_id],
            ).fetchone()
            if row is None:
                _deny(404, "问题不存在或不可处理")
            old = db.execute(
                "SELECT feedback_id,fingerprint,receipt_json FROM feedback_progress_receipts WHERE request_id=?",
                [body.request_id],
            ).fetchone()
            if old:
                if old["feedback_id"] != feedback_id or old["fingerprint"] != fingerprint:
                    _deny(409, "进度请求编号已使用")
                result = json.loads(old["receipt_json"])
            else:
                if (
                    row["status"] not in {"triaging", "in_progress"}
                    or row["revision_number"] != body.expected_revision
                ):
                    _deny(409, "问题已更新或暂停，不能覆盖当前处理状态")
                if body.stage == "triaging" and row["status"] != "triaging":
                    _deny(409, "不能将正在处理的问题退回初始排查")
                if (
                    row["version"] != APP_VERSION
                    or row["revision"] != body.reported_revision
                    or body.reported_revision != revision[:12]
                ):
                    _deny(409, "问题来源不属于当前运行版本")
                previous = db.execute(
                    "SELECT stage,high_water,revision_number,runtime_revision FROM feedback_progress_state WHERE feedback_id=?",
                    [feedback_id],
                ).fetchone()
                status, reply, order = STAGES[body.stage]
                high = previous["high_water"] if previous else 0
                if (
                    previous
                    and previous["runtime_revision"] == revision
                    and order is not None
                    and order < high
                ):
                    _deny(409, "进度不能覆盖较新的处理阶段")
                if order is not None:
                    high = max(high, order)
                stamp = int(time.time())
                changed = not (
                    previous
                    and previous["stage"] == body.stage
                    and previous["revision_number"] == row["revision_number"]
                    and previous["runtime_revision"] == revision
                )
                next_revision = body.expected_revision + int(changed)
                if changed:
                    db.execute(
                        "INSERT INTO feedback_updates(feedback_id,actor,old_status,status,public_reply,verification,target_version,target_revision,created_at) VALUES(?,?,?,?,?,'','','',?)",
                        [feedback_id, ACTOR, row["status"], status, reply, stamp],
                    )
                    db.execute(
                        "UPDATE feedback_items SET status=?,public_reply=?,updated_at=?,revision_number=revision_number+1 WHERE id=?",
                        [status, reply, stamp, feedback_id],
                    )
                    db.execute(
                        "INSERT INTO feedback_progress_state VALUES(?,?,?,?,?) ON CONFLICT(feedback_id) DO UPDATE SET stage=excluded.stage,high_water=excluded.high_water,revision_number=excluded.revision_number,runtime_revision=excluded.runtime_revision",
                        [feedback_id, body.stage, high, next_revision, revision],
                    )
                result = {
                    "schema": "prism.bug-progress.receipt.v1",
                    "request_id": body.request_id,
                    "feedback_id": feedback_id,
                    "fingerprint": fingerprint,
                    "stage": body.stage,
                    "status": status,
                    "public_reply": reply,
                    "revision_number": next_revision,
                    "applied_revision": revision,
                    "changed": bool(changed),
                    "fixed": False,
                    "deployed": False,
                }
                db.execute(
                    "INSERT INTO feedback_progress_receipts VALUES(?,?,?,?,?)",
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
