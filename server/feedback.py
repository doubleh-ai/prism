"""Owned, private feedback and visible review history; feedback is never executed."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import unquote, urlsplit
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .auth import require_admin, require_auth, verify_csrf
from .database import check_rate_limit, get_db
from .release import APP_VERSION, _read_deployed_revision
from .source_urls import redact_source_text

router = APIRouter()
_SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
Feature = Literal[
    "unknown", "chat", "market", "vibe", "analysis", "reports", "industry", "admin", "screener", "indicators"
]
_FEATURES = {
    "unknown",
    "chat",
    "market",
    "vibe",
    "analysis",
    "reports",
    "industry",
    "admin",
    "screener",
    "indicators",
}
_STATUSES = {
    "bug": {"triaging", "in_progress", "needs_info", "fixed", "deferred", "declined"},
    "suggestion": {
        "pending_review",
        "accepted",
        "in_progress",
        "needs_info",
        "fixed",
        "deferred",
        "declined",
    },
}
_SECRET_LABEL = re.compile(
    r"(?i)(?:[\w.-]*(?:api[_-]?key|access[_-]?key|private[_-]?key|token|password|passwd|secret|cookie|authorization)[\w.-]*|pwd|密码|口令|密钥|私钥|令牌|访问凭证)"
    r"\s*[=:：]\s*(?:\"[^\"\r\n]+\"|'[^'\r\n]+'|[^\s,;，；]+)"
)
_SECRET_VALUE = re.compile(
    r"(?i)\b(?:sk-[a-z0-9_-]{8,}|gh[pousr]_[a-z0-9]{12,}|github_pat_[a-z0-9_]{12,}|"
    r"AKIA[A-Z0-9]{16}|bearer\s+[a-z0-9._~+/=-]{8,}|eyJ[a-z0-9_-]{5,}\.[a-z0-9_-]{5,}\.[a-z0-9_-]{5,})"
)
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9 ]*PRIVATE KEY|OPENSSH PRIVATE KEY)-----", re.I)
_BAD_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WORKER_ACTOR = "system:feedback-worker"
_WORKER_HEADER = "X-Prism-Feedback-Token"


class FeedbackCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    category: Literal["bug", "suggestion"]
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=6000)
    page: str = Field(default="/", max_length=1000)
    feature: Feature = "unknown"
    request_id: str | None = Field(default=None, min_length=1, max_length=64)


class FeedbackUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: str = Field(min_length=1, max_length=32)
    public_reply: str = Field(min_length=1, max_length=3000)
    verification: str = Field(default="", max_length=3000)
    target_version: str = Field(default="", max_length=64)
    target_revision: str = Field(default="", max_length=40)
    expected_revision: int | None = Field(default=None, ge=1)


def init_feedback_db() -> None:
    """Additive atomic schema; old applications also purge feedback on deletion."""
    with get_db() as db:
        if db.in_transaction:
            raise RuntimeError("feedback schema requires its own transaction")
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                """CREATE TABLE IF NOT EXISTS feedback_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    category TEXT NOT NULL CHECK(category IN ('bug','suggestion')),
                    title TEXT NOT NULL, description TEXT NOT NULL, page TEXT NOT NULL,
                    feature TEXT NOT NULL DEFAULT 'unknown',
                    status TEXT NOT NULL, version TEXT NOT NULL, revision TEXT NOT NULL,
                    triage_json TEXT NOT NULL, review_batch_date TEXT NOT NULL,
                    public_reply TEXT NOT NULL DEFAULT '', verification TEXT NOT NULL DEFAULT '',
                    target_version TEXT NOT NULL DEFAULT '', target_revision TEXT NOT NULL DEFAULT '',
                    revision_number INTEGER NOT NULL DEFAULT 1,
                    request_id TEXT, request_fingerprint TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS feedback_updates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, feedback_id INTEGER NOT NULL,
                    actor TEXT NOT NULL, old_status TEXT NOT NULL, status TEXT NOT NULL,
                    public_reply TEXT NOT NULL, verification TEXT NOT NULL,
                    target_version TEXT NOT NULL, target_revision TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL
                )"""
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(feedback_items)")}
            if "feature" not in columns:
                db.execute("ALTER TABLE feedback_items ADD COLUMN feature TEXT NOT NULL DEFAULT 'unknown'")
            for table in ("feedback_items", "feedback_updates"):
                if "target_revision" not in {
                    row[1] for row in db.execute("PRAGMA table_info(" + table + ")")
                }:
                    db.execute(
                        "ALTER TABLE " + table + " ADD COLUMN target_revision TEXT NOT NULL DEFAULT ''"
                    )
            db.execute("CREATE INDEX IF NOT EXISTS feedback_owner ON feedback_items(username,id DESC)")
            db.execute(
                "CREATE INDEX IF NOT EXISTS feedback_queue ON feedback_items(category,status,review_batch_date,id)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS feedback_history ON feedback_updates(feedback_id,id)")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS feedback_request ON feedback_items(username,request_id) WHERE request_id IS NOT NULL"
            )
            db.execute(
                """CREATE TRIGGER IF NOT EXISTS feedback_account_delete AFTER DELETE ON users
                BEGIN
                    DELETE FROM feedback_updates WHERE feedback_id IN
                        (SELECT id FROM feedback_items WHERE username=OLD.username);
                    DELETE FROM feedback_items WHERE username=OLD.username;
                    UPDATE feedback_updates SET actor='[removed]' WHERE actor=OLD.username;
                END"""
            )
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise


async def _payload(request: Request, model):
    """Bounded JSON; invalid values are never echoed or logged as validation inputs."""
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 32768:
            raise HTTPException(413, "反馈内容过长")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique)
        return model.model_validate(value)
    except (ValueError, TypeError, RecursionError, ValidationError):
        raise HTTPException(422, "反馈格式无效，请检查类型、长度和必填项")


def _text(value: str) -> str:
    if _PRIVATE_KEY.search(value):
        raise HTTPException(422, "请移除私钥、凭证和环境配置后再提交反馈")
    clean = _BAD_CONTROL.sub("", value).strip()
    clean = _SECRET_LABEL.sub("[凭证已隐藏]", clean)
    clean = _SECRET_VALUE.sub("[凭证已隐藏]", clean)
    return redact_source_text(clean).strip()


def _page(value: str) -> str:
    # Decode before stripping so encoded query/fragment credentials cannot remain.
    for _ in range(2):
        value = unquote(value)
    if _BAD_CONTROL.search(value) or "\\" in value:
        raise HTTPException(422, "页面仅支持站内路径")
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise HTTPException(422, "页面仅支持站内路径")
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith("//"):
        raise HTTPException(422, "页面仅支持站内路径")
    return _text(parsed.path)[:240] or "/"


def _triage(category: str, title: str, description: str) -> dict:
    if category == "suggestion":
        return {"mode": "daily_review", "priority": "normal", "requires_review": True}
    urgent = any(
        word in (title + " " + description).lower()
        for word in ("泄露", "越权", "数据丢失", "无法登录", "500", "503")
    )
    return {
        "mode": "immediate_rule_triage",
        "priority": "urgent" if urgent else "normal",
        "requires_review": True,
        "automatically_fixed": False,
        "next_action": "核对当前版本与复现条件，再验证修复结果",
    }


def _item(row, *, admin: bool = False) -> dict:
    value = dict(row)
    value.pop("request_fingerprint", None)
    if not admin:
        value.pop("username", None)
    value["release"] = {"version": value.pop("version"), "revision": value.pop("revision")}
    value["triage"] = json.loads(value.pop("triage_json"))
    return value


def _owned(db, feedback_id: int, username: str | None):
    row = db.execute(
        "SELECT * FROM feedback_items WHERE id=?" + (" AND username=?" if username else ""),
        [feedback_id, username] if username else [feedback_id],
    ).fetchone()
    if row is None:
        raise HTTPException(404, "反馈不存在")
    return row


def _detail(feedback_id: int, username: str | None = None) -> dict:
    with get_db() as db:
        row = _owned(db, feedback_id, username)
        history = db.execute(
            "SELECT * FROM feedback_updates WHERE feedback_id=? ORDER BY id", [feedback_id]
        ).fetchall()
    value = _item(row, admin=username is None)
    value["updates"] = []
    for event in history:
        public = dict(event)
        public.pop("feedback_id")
        if username is not None:
            public.pop("actor")
        value["updates"].append(public)
    return value


def _queue(*, username=None, category="", status="", feature="", review_batch_date="", before_id=0, limit=50):
    if category and category not in _STATUSES:
        raise HTTPException(422, "反馈类别无效")
    if status and status not in set().union(*_STATUSES.values()):
        raise HTTPException(422, "反馈状态无效")
    if feature and feature not in _FEATURES:
        raise HTTPException(422, "反馈功能无效")
    if review_batch_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", review_batch_date):
        raise HTTPException(422, "评审批次日期无效")
    clauses, args = [], []
    for key, value in (
        ("username", username),
        ("category", category),
        ("status", status),
        ("feature", feature),
        ("review_batch_date", review_batch_date),
    ):
        if value:
            clauses.append(key + "=?")
            args.append(value)
    if before_id:
        clauses.append("id<?")
        args.append(before_id)
    limit = max(1, min(100, limit))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM feedback_items" + where + " ORDER BY id DESC LIMIT ?", [*args, limit + 1]
        ).fetchall()
    items = [_item(row, admin=username is None) for row in rows[:limit]]
    return {"items": items, "next_before_id": items[-1]["id"] if len(rows) > limit else None}


@router.post("/api/feedback")
async def submit_feedback(request: Request):
    username = require_auth(request)
    verify_csrf(request)
    if not check_rate_limit("feedback:" + username, 6, 60):
        raise HTTPException(429, "反馈提交较频繁，请稍后再试")
    payload = await _payload(request, FeedbackCreate)
    title, description, page = _text(payload.title), _text(payload.description), _page(payload.page)
    if not title or not description:
        raise HTTPException(422, "请填写标题和具体说明")
    now = int(time.time())
    version = APP_VERSION[:40]
    revision = _read_deployed_revision()
    revision = revision[:12] if re.fullmatch(r"[a-f0-9]{40}", revision) else ""
    status = "triaging" if payload.category == "bug" else "pending_review"
    triage = _triage(payload.category, title, description)
    batch = datetime.fromtimestamp(now, _SHANGHAI).date().isoformat()
    request_id = None
    if payload.request_id is not None:
        try:
            request_id = str(UUID(payload.request_id))
        except (ValueError, AttributeError):
            raise HTTPException(422, "request_id 必须是 UUID")
    fingerprint = hashlib.sha256(
        json.dumps([payload.category, title, description, page, payload.feature], ensure_ascii=False).encode()
    ).hexdigest()
    reused = False
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if not db.execute(
                "SELECT 1 FROM users WHERE username=? AND disabled_at IS NULL", [username]
            ).fetchone():
                raise HTTPException(401)
            previous = (
                db.execute(
                    "SELECT id,request_fingerprint FROM feedback_items WHERE username=? AND request_id=?",
                    [username, request_id],
                ).fetchone()
                if request_id
                else None
            )
            if previous:
                if previous["request_fingerprint"] != fingerprint:
                    raise HTTPException(409, "该提交编号已用于不同反馈，请刷新核对")
                feedback_id, reused = previous["id"], True
            else:
                cur = db.execute(
                    """INSERT INTO feedback_items
            (username,category,title,description,page,feature,status,version,revision,triage_json,review_batch_date,request_id,request_fingerprint,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [
                        username,
                        payload.category,
                        title,
                        description,
                        page,
                        payload.feature,
                        status,
                        version,
                        revision,
                        json.dumps(triage, ensure_ascii=False),
                        batch,
                        request_id,
                        fingerprint,
                        now,
                        now,
                    ],
                )
                feedback_id = cur.lastrowid
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    return {"ok": True, "reused": reused, "feedback": _detail(feedback_id, username)}


@router.get("/api/feedback")
async def my_feedback(
    request: Request,
    category: str = "",
    status: str = "",
    feature: str = "",
    before_id: int = 0,
    limit: int = 50,
):
    return _queue(
        username=require_auth(request),
        category=category,
        status=status,
        feature=feature,
        before_id=before_id,
        limit=limit,
    )


@router.get("/api/feedback/{feedback_id}")
async def feedback_detail(feedback_id: int, request: Request):
    return {"feedback": _detail(feedback_id, require_auth(request))}


@router.get("/api/admin/feedback")
async def admin_feedback(
    request: Request,
    category: str = "",
    status: str = "",
    feature: str = "",
    review_batch_date: str = "",
    before_id: int = 0,
    limit: int = 50,
):
    require_admin(request)
    return _queue(
        category=category,
        status=status,
        feature=feature,
        review_batch_date=review_batch_date,
        before_id=before_id,
        limit=limit,
    )


@router.get("/api/admin/feedback/{feedback_id}")
async def admin_feedback_detail(feedback_id: int, request: Request):
    require_admin(request)
    return {"feedback": _detail(feedback_id)}


@router.post("/api/admin/feedback/{feedback_id}/update")
async def update_feedback(feedback_id: int, request: Request):
    actor = require_admin(request)
    verify_csrf(request)
    if not check_rate_limit("feedback-admin:" + actor, 60, 60):
        raise HTTPException(429, "反馈处理较频繁，请稍后再试")
    payload = await _payload(request, FeedbackUpdate)
    return {"ok": True, "feedback": _apply_update(feedback_id, payload, actor, recheck_account=True)}


def _apply_update(feedback_id: int, payload: FeedbackUpdate, actor: str, *, recheck_account: bool) -> dict:
    """One atomic state/CAS/history implementation for admin and scoped worker."""
    if not recheck_account and actor != _WORKER_ACTOR:
        raise HTTPException(403, "反馈处理身份无效")
    if payload.target_revision and not re.fullmatch(r"[a-f0-9]{40}", payload.target_revision):
        raise HTTPException(422, "目标修订必须是完整版本标识")
    reply, verification, target = (
        _text(payload.public_reply),
        _text(payload.verification),
        _text(payload.target_version),
    )
    if not reply:
        raise HTTPException(422, "请填写用户可见的处理说明")
    if payload.status == "fixed" and (
        len(verification) < 12 or not re.fullmatch(r"v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", target)
    ):
        raise HTTPException(422, "标记已修复需要具体验证说明（至少12字）和目标版本号")
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if (
                recheck_account
                and not db.execute(
                    "SELECT 1 FROM users WHERE username=? AND disabled_at IS NULL", [actor]
                ).fetchone()
            ):
                raise HTTPException(403, "管理员账户已停用，请重新登录")
            row = _owned(db, feedback_id, None)
            if payload.status not in _STATUSES[row["category"]]:
                raise HTTPException(422, "该类别不支持此状态")
            if payload.expected_revision is not None and payload.expected_revision != row["revision_number"]:
                raise HTTPException(409, "反馈已更新，请刷新后再处理")
            now = int(time.time())
            db.execute(
                """INSERT INTO feedback_updates(feedback_id,actor,old_status,status,public_reply,verification,target_version,target_revision,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                [
                    feedback_id,
                    actor,
                    row["status"],
                    payload.status,
                    reply,
                    verification,
                    target,
                    payload.target_revision,
                    now,
                ],
            )
            db.execute(
                """UPDATE feedback_items SET status=?,public_reply=?,verification=?,target_version=?,target_revision=?,updated_at=?,revision_number=revision_number+1 WHERE id=?""",
                [payload.status, reply, verification, target, payload.target_revision, now, feedback_id],
            )
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    return _detail(feedback_id)


def is_worker_update_path(path: str) -> bool:
    """Only this exact route uses its dedicated token header instead of cookies/CSRF."""
    return re.fullmatch(r"/api/internal/feedback/[0-9]+/update", path) is not None


def _worker_auth(request: Request) -> None:
    # Bound all authentication attempts before comparing a configured secret.
    # Do not trust forwarded headers or create an admin/browser identity.
    host = request.client.host if request.client is not None else "local-unix-socket"
    permitted = check_rate_limit("feedback-worker-auth:" + host, 60, 60)
    if not permitted:
        raise HTTPException(401, "反馈处理身份无效")
    expected = os.environ.get("PRISM_FEEDBACK_WORKER_TOKEN", "")
    monitor = os.environ.get("PRISM_FEEDBACK_MONITOR_TOKEN", "")
    supplied = request.headers.get(_WORKER_HEADER, "")
    enabled = 32 <= len(expected) <= 512 and not any(c.isspace() for c in expected)
    bounded = len(supplied) <= 1024
    if not bounded:
        supplied = ""
    same = secrets.compare_digest(
        hashlib.sha256(expected.encode()).digest(), hashlib.sha256(supplied.encode()).digest()
    )
    if not (permitted and enabled and bounded and same) or (monitor and expected == monitor):
        raise HTTPException(401, "反馈处理身份无效")


def _worker_view(value: dict) -> dict:
    safe = dict(value)
    safe.pop("username", None)
    safe.pop("request_id", None)
    if "updates" in safe:
        safe["updates"] = []
        for update in value["updates"]:
            public = dict(update)
            public["actor"] = _WORKER_ACTOR if public.get("actor") == _WORKER_ACTOR else "administrator"
            safe["updates"].append(public)
    return safe


def _diagnostic_code(kind: str, message: str) -> dict:
    """Finite derived identifiers, never return arbitrary error/request text."""
    fixed = {
        "render_object_artifact": "visible_object_artifact",
        "chart_blank": "chart_canvas_missing",
        "blank_panel": "active_panel_blank",
        "api_network_error": "network_request_error",
    }
    if kind in fixed:
        return {"error_code": fixed[kind]}
    text = message.splitlines()[0][:512] if isinstance(message, str) and message else ""
    if kind in {"api_http_5xx", "server_http_5xx"}:
        found = re.match(r"^HTTP (5[0-9]{2})(?:$| from )", text)
        return {"error_code": "http_server_error", **({"http_status": int(found[1])} if found else {})}
    if kind in {"js_error", "promise_rejection", "server_exception"}:
        method = re.fullmatch(
            r"(?:TypeError:\s*)?this\.([A-Za-z_$][A-Za-z0-9_$]{0,59}) is not a function\.?", text.strip()
        )
        if method:
            return {"error_code": "method_not_callable", "method": method[1]}
        exception = re.match(
            r"^(TypeError|ReferenceError|RangeError|SyntaxError|URIError|EvalError|ValueError|KeyError|RuntimeError|TimeoutError|AttributeError|HTTPException)(?::|$)",
            text,
        )
        if exception:
            return {"error_code": "exception", "exception_class": exception[1]}
    return {"error_code": "needs_review"}


@router.get("/api/internal/feedback")
async def worker_feedback(
    request: Request,
    category: str = "",
    status: str = "",
    feature: str = "",
    review_batch_date: str = "",
    before_id: int = 0,
    limit: int = 50,
):
    _worker_auth(request)
    result = _queue(
        category=category,
        status=status,
        feature=feature,
        review_batch_date=review_batch_date,
        before_id=before_id,
        limit=limit,
    )
    result["items"] = [_worker_view(value) for value in result["items"]]
    return result


@router.get("/api/internal/feedback/diagnostics")
async def worker_diagnostics(request: Request, limit: int = 50):
    _worker_auth(request)
    kinds = {
        "js_error",
        "promise_rejection",
        "render_object_artifact",
        "chart_blank",
        "blank_panel",
        "api_network_error",
        "api_http_5xx",
        "server_exception",
        "server_http_5xx",
    }
    with get_db() as db:
        rows = db.execute(
            "SELECT id,kind,severity,version,message,occurrences,first_seen,last_seen FROM diagnostic_events WHERE resolved_at IS NULL ORDER BY last_seen DESC,id DESC LIMIT ?",
            [max(1, min(100, limit))],
        ).fetchall()
    events = []
    for row in rows:
        version = row["version"] if re.fullmatch(r"v?\d+\.\d+\.\d+", row["version"] or "") else ""
        events.append(
            {
                "id": row["id"],
                "kind": row["kind"] if row["kind"] in kinds else "other",
                "severity": row["severity"]
                if row["severity"] in {"info", "warning", "error", "critical"}
                else "unknown",
                "version": version,
                "version_scope": ("current_version" if version == APP_VERSION else "historical_version")
                if version
                else "unattributed",
                "occurrences": max(1, int(row["occurrences"] or 1)),
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                **_diagnostic_code(row["kind"], row["message"]),
            }
        )
    revision = _read_deployed_revision()
    return {
        "release": {
            "version": APP_VERSION,
            "revision": revision[:12] if re.fullmatch(r"[a-f0-9]{40}", revision) else "",
        },
        "events": events,
        "diagnostic_records_changed": False,
        "privacy": {
            "username_included": False,
            "raw_message_included": False,
            "stack_included": False,
            "context_or_request_data_included": False,
        },
        "scope_note": "Version labels do not prove exact deployed revision; events remain unverified signals.",
    }


@router.get("/api/internal/feedback/{feedback_id}")
async def worker_feedback_detail(feedback_id: int, request: Request):
    _worker_auth(request)
    return {"feedback": _worker_view(_detail(feedback_id))}


@router.post("/api/internal/feedback/{feedback_id}/update")
async def worker_update_feedback(feedback_id: int, request: Request):
    _worker_auth(request)
    payload = await _payload(request, FeedbackUpdate)
    if payload.expected_revision is None:
        raise HTTPException(422, "反馈处理必须提供 expected_revision")
    # An administrator may rotate/disable the token while a slow body arrives.
    # Recheck the dedicated identity immediately before the atomic write.
    _worker_auth(request)
    if payload.status == "fixed":
        current_revision = _read_deployed_revision()
        if not (
            re.fullmatch(r"[a-f0-9]{40}", current_revision)
            and payload.target_revision == current_revision
            and payload.target_version == APP_VERSION
        ):
            raise HTTPException(409, "标记已修复必须绑定当前已部署版本和修订，并完成真实线上验证")
    value = _apply_update(feedback_id, payload, _WORKER_ACTOR, recheck_account=False)
    return {"ok": True, "feedback": _worker_view(value)}
