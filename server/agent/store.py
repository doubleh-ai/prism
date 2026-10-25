"""Owned, append-only native Agent execution records; never a model continuation.

Only public output projections belong here. Provider reasoning, authentication
frames, environment contents and private file payloads must never reach this API.
Persisting an attempt does not authorize replaying it after an uncertain exit.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import time
from contextlib import contextmanager

from fastapi import HTTPException

from ..database import get_db
from ..source_urls import redact_source_text, source_display_payload

TERMINAL = frozenset({"completed", "partial", "failed", "cancelled"})
ACTIVE = frozenset({"queued", "running", "awaiting_user"})
MAX_EVENT_BYTES = 65536
MAX_PAGE = 200
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")
_EVENT_TYPE = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,79}\Z")
_PRIVATE_KEYS = frozenset(
    {
        "apikey",
        "authorization",
        "authentication",
        "password",
        "passwd",
        "secret",
        "clientsecret",
        "privatekey",
        "credential",
        "credentials",
        "cookie",
        "cookies",
        "token",
        "accesstoken",
        "refreshtoken",
        "headers",
        "env",
        "environment",
        "raw",
        "rawrequest",
        "rawresponse",
        "rawframe",
        "rawframes",
        "thinking",
        "reasoning",
        "reasoningcontent",
        "chainofthought",
        "signature",
        "encryptedcontent",
        "redactedthinking",
        "privatefile",
        "privatefiles",
        "filecontents",
        "filecontent",
        "stdout",
        "stderr",
        "path",
        "paths",
    }
)
_PRIVATE_FRAME = re.compile(r"thinking|reasoning|signature|encrypted", re.I)
_KEY_ASSIGNMENT = re.compile(
    r"(?i)\b([\w-]*(?:api[_-]?key|password|passwd|secret|access[_-]?token|refresh[_-]?token))"
    r"[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
_SK = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)
_PRIVATE_PATH = re.compile(r"(?<![\w:])(?:/Users/|/home/|/root/|/etc/|/private/|~/)[^\s<>\"')\]]+")
_OWNED = (
    "r.run_id=? AND r.username=? "
    "AND EXISTS (SELECT 1 FROM users u WHERE u.username=r.username AND u.disabled_at IS NULL) "
    "AND (r.conv_id IS NULL OR EXISTS (SELECT 1 FROM conversations c "
    "WHERE c.id=r.conv_id AND c.username=r.username AND c.deleted_at IS NULL))"
)


def _reject(detail="无效的 Agent 记录参数"):
    raise HTTPException(422, detail)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _safe_text(value, maximum):
    if not isinstance(value, str) or len(value) > maximum:
        _reject()
    try:
        value.encode("utf-8")
    except UnicodeError:
        _reject()
    value = redact_source_text(value)
    value = _PEM.sub("[已隐藏敏感信息]", value)
    value = _KEY_ASSIGNMENT.sub(lambda m: m.group(1) + "=[已隐藏敏感信息]", value)
    value = _BEARER.sub("Bearer [已隐藏敏感信息]", value)
    value = _SK.sub("[已隐藏敏感信息]", value)
    return _PRIVATE_PATH.sub("[私有路径已隐藏]", value)


def _event_payload(event):
    if type(event) is not dict or not _EVENT_TYPE.fullmatch(str(event.get("type", ""))):
        _reject("事件须包含有效 type")
    if _PRIVATE_FRAME.search(event["type"]):
        _reject("不能保存模型内部推理或认证帧")
    if set(event) & {"run_id", "seq", "created_at"}:
        _reject("事件序号与时间由服务端生成")
    count = 0

    def clean(value, depth=0):
        nonlocal count
        count += 1
        if count > 2048 or depth > 8:
            _reject("事件结构过大")
        if value is None or type(value) in (bool, int):
            return value
        if type(value) is float:
            if not math.isfinite(value):
                _reject()
            return value
        if isinstance(value, str):
            return _safe_text(value, MAX_EVENT_BYTES)
        if type(value) is list:
            return [clean(item, depth + 1) for item in value]
        if type(value) is dict:
            if isinstance(value.get("type"), str) and _PRIVATE_FRAME.search(value["type"]):
                _reject("不能保存模型内部推理或认证帧")
            result = {}
            for key, item in value.items():
                if not isinstance(key, str) or len(key) > 100:
                    _reject()
                normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
                if normalized not in _PRIVATE_KEYS and not normalized.endswith(
                    ("apikey", "password", "passwd", "secret", "privatekey", "accesstoken", "refreshtoken")
                ):
                    result[_safe_text(key, 100)] = clean(item, depth + 1)
            return result
        _reject()

    try:
        payload = source_display_payload(clean(event))
        if len(_json(payload).encode("utf-8")) > MAX_EVENT_BYTES:
            _reject("事件内容过大")
    except (TypeError, ValueError, OverflowError, RecursionError):
        _reject()
    return payload


def init_agent_db():
    """Additive and idempotent; no import-time migration or startup replay."""
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS native_agent_runs (
                run_id TEXT PRIMARY KEY, username TEXT NOT NULL, conv_id TEXT,
                question TEXT NOT NULL, model_id TEXT NOT NULL, workflow TEXT NOT NULL,
                request_id TEXT, input_sha256 TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                research_status TEXT NOT NULL DEFAULT 'unreviewed',
                claimed_by TEXT, claimed_at REAL,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                summary TEXT NOT NULL DEFAULT '', error_code TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL, finished_at REAL,
                last_seq INTEGER NOT NULL DEFAULT 0
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_native_agent_request
                ON native_agent_runs(username, COALESCE(conv_id,''), request_id)
                WHERE request_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_native_agent_owner
                ON native_agent_runs(username, created_at DESC, run_id);
            CREATE TABLE IF NOT EXISTS native_agent_events (
                run_id TEXT NOT NULL, seq INTEGER NOT NULL, created_at REAL NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY (run_id, seq)
            );
        """)


@contextmanager
def _transaction():
    init_agent_db()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise


def _owned(db, user, run_id):
    row = db.execute("SELECT r.* FROM native_agent_runs r WHERE " + _OWNED, [run_id, user]).fetchone()
    if row is None:
        raise HTTPException(404, "Agent 记录不存在或不可访问")
    return row


def _public_run(row):
    result = dict(row)
    result.pop("input_sha256", None)
    result.pop("claimed_by", None)
    result["id"] = result["run_id"]
    result["cancel_requested"] = bool(result["cancel_requested"])
    return result


def _insert_event(db, row, payload):
    seq, now = row["last_seq"] + 1, time.time()
    db.execute(
        "INSERT INTO native_agent_events(run_id,seq,created_at,payload) VALUES(?,?,?,?)",
        [row["run_id"], seq, now, _json(payload)],
    )
    db.execute(
        "UPDATE native_agent_runs SET last_seq=?,updated_at=? WHERE run_id=?", [seq, now, row["run_id"]]
    )
    return {**payload, "run_id": row["run_id"], "seq": seq, "created_at": now}


def create_run(user, conv_id, question, model_id, workflow, request_id=None, *, exclusive_conversation=False):
    if type(exclusive_conversation) is not bool:
        _reject()
    if not isinstance(user, str) or not user or len(user) > 128:
        _reject()
    if conv_id is not None and (not isinstance(conv_id, str) or not conv_id or len(conv_id) > 128):
        _reject()
    if not isinstance(question, str) or not question.strip() or len(question) > 20000:
        _reject()
    for identifier in (model_id, workflow):
        if not isinstance(identifier, str) or not _IDENTIFIER.fullmatch(identifier):
            _reject()
    if request_id is not None and (not isinstance(request_id, str) or not _IDENTIFIER.fullmatch(request_id)):
        _reject()
    fingerprint = hashlib.sha256(_json([question, model_id, workflow]).encode("utf-8")).hexdigest()
    safe_question = _safe_text(question, 20000)
    with _transaction() as db:
        if not db.execute("SELECT 1 FROM users WHERE username=? AND disabled_at IS NULL", [user]).fetchone():
            raise HTTPException(404, "用户不存在或不可访问")
        if (
            conv_id is not None
            and not db.execute(
                "SELECT 1 FROM conversations WHERE id=? AND username=? AND deleted_at IS NULL",
                [conv_id, user],
            ).fetchone()
        ):
            raise HTTPException(404, "会话不存在或不可访问")
        if request_id is not None:
            existing = db.execute(
                "SELECT * FROM native_agent_runs WHERE username=? AND conv_id IS ? AND request_id=?",
                [user, conv_id, request_id],
            ).fetchone()
            if existing:
                if existing["input_sha256"] != fingerprint:
                    raise HTTPException(409, "请求编号已用于不同输入")
                return {**_public_run(existing), "created": False, "reused": True}
        if exclusive_conversation:
            if db.execute(
                "SELECT 1 FROM native_agent_runs WHERE username=? AND conv_id IS ? "
                "AND status IN ('queued','running','awaiting_user')",
                [user, conv_id],
            ).fetchone():
                raise HTTPException(409, "这个对话已有研究正在进行，请先查看或停止当前研究")
            if (
                db.execute(
                    "SELECT COUNT(*) FROM native_agent_runs WHERE username=? "
                    "AND status IN ('queued','running','awaiting_user')",
                    [user],
                ).fetchone()[0]
                >= 3
            ):
                raise HTTPException(429, "同时进行的研究已达上限，请先查看已有研究")
        run_id, now = secrets.token_hex(16), time.time()
        db.execute(
            "INSERT INTO native_agent_runs "
            "(run_id,username,conv_id,question,model_id,workflow,request_id,input_sha256,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            [run_id, user, conv_id, safe_question, model_id, workflow, request_id, fingerprint, now, now],
        )
        row = _owned(db, user, run_id)
        _insert_event(db, row, {"type": "run_created", "status": "queued"})
        return {**_public_run(_owned(db, user, run_id)), "created": True, "reused": False}


def get_run(user, run_id):
    init_agent_db()
    with get_db() as db:
        return _public_run(_owned(db, user, run_id))


def append_event(user, run_id, event):
    payload = _event_payload(event)
    with _transaction() as db:
        row = _owned(db, user, run_id)
        if row["status"] in TERMINAL:
            raise HTTPException(409, "执行已结束，不能追加事件")
        return _insert_event(db, row, payload)


def list_events(user, run_id, after_seq=0, limit=200):
    if type(after_seq) is not int or after_seq < 0 or type(limit) is not int or limit < 1:
        _reject()
    init_agent_db()
    with get_db() as db:
        _owned(db, user, run_id)
        rows = db.execute(
            "SELECT e.seq,e.created_at,e.payload FROM native_agent_events e "
            "JOIN native_agent_runs r ON r.run_id=e.run_id WHERE "
            + _OWNED
            + " AND e.seq>? ORDER BY e.seq LIMIT ?",
            [run_id, user, after_seq, min(limit, MAX_PAGE)],
        ).fetchall()
        return [
            {
                **json.loads(row["payload"]),
                "run_id": run_id,
                "seq": row["seq"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]


def list_runs(user, conv_id=None, limit=50):
    if type(limit) is not int or limit < 1:
        _reject()
    init_agent_db()
    with get_db() as db:
        query = (
            "SELECT r.* FROM native_agent_runs r WHERE r.username=? "
            "AND EXISTS(SELECT 1 FROM users u WHERE u.username=r.username AND u.disabled_at IS NULL) "
            "AND (r.conv_id IS NULL OR EXISTS(SELECT 1 FROM conversations c "
            "WHERE c.id=r.conv_id AND c.username=r.username AND c.deleted_at IS NULL))"
        )
        params = [user]
        if conv_id is not None:
            query += " AND r.conv_id=?"
            params.append(conv_id)
        params.append(min(limit, MAX_PAGE))
        rows = db.execute(query + " ORDER BY r.created_at DESC,r.run_id DESC LIMIT ?", params).fetchall()
        return [_public_run(row) for row in rows]


def claim_run(user, run_id, executor_id):
    """Claim a queued run once; even the same executor cannot replay a claim."""
    if not isinstance(executor_id, str) or not _IDENTIFIER.fullmatch(executor_id):
        _reject()
    with _transaction() as db:
        row = _owned(db, user, run_id)
        if row["status"] != "queued" or row["claimed_by"] is not None or row["cancel_requested"]:
            return False
        now = time.time()
        db.execute(
            "UPDATE native_agent_runs SET claimed_by=?,claimed_at=?,status='running',updated_at=? WHERE run_id=?",
            [executor_id, now, now, run_id],
        )
        _insert_event(db, row, {"type": "run_started", "status": "running"})
        return True


def set_run_status(user, run_id, status):
    """Pause/resume only the existing executor; this never claims or executes work."""
    if not isinstance(status, str) or status not in {"running", "awaiting_user"}:
        _reject()
    with _transaction() as db:
        row = _owned(db, user, run_id)
        if row["status"] not in {"running", "awaiting_user"} or row["claimed_by"] is None:
            raise HTTPException(409, "当前执行不能改变状态")
        if row["status"] != status:
            db.execute("UPDATE native_agent_runs SET status=? WHERE run_id=?", [status, run_id])
            _insert_event(db, row, {"type": "run_status", "status": status})
        return _public_run(_owned(db, user, run_id))


def finish_run(user, run_id, status, summary, error_code=None):
    if (
        not isinstance(status, str)
        or status not in TERMINAL
        or (
            error_code is not None
            and (not isinstance(error_code, str) or not _ERROR_CODE.fullmatch(error_code))
        )
    ):
        _reject()
    summary = _safe_text(summary, 200000)
    with _transaction() as db:
        row = _owned(db, user, run_id)
        if row["status"] in TERMINAL:
            if (row["status"], row["summary"], row["error_code"]) != (status, summary, error_code):
                raise HTTPException(409, "结束记录已固定，不能覆盖")
            return _public_run(row)
        now = time.time()
        db.execute(
            "UPDATE native_agent_runs SET status=?,summary=?,error_code=?,finished_at=?,updated_at=? WHERE run_id=?",
            [status, summary, error_code, now, now, run_id],
        )
        _insert_event(db, row, {"type": "run_finished", "status": status, "error_code": error_code})
        return _public_run(_owned(db, user, run_id))


def request_cancel(user, run_id):
    """Persist intent; this does not claim the external model/tool was stopped."""
    with _transaction() as db:
        row = _owned(db, user, run_id)
        if row["status"] not in TERMINAL and not row["cancel_requested"]:
            db.execute("UPDATE native_agent_runs SET cancel_requested=1 WHERE run_id=?", [run_id])
            _insert_event(db, row, {"type": "cancel_requested", "external_stop_confirmed": False})
        return _public_run(_owned(db, user, run_id))


def is_cancelled(user, run_id):
    row = get_run(user, run_id)
    return row["cancel_requested"] or row["status"] == "cancelled"


def recover_interrupted_runs(*, exclusive_startup=False):
    """Explicit exclusive-worker startup only; retain uncertainty, never retry.

    The caller must actually establish exclusive startup before passing True.
    Schema initialization, GET and ordinary requests must never invoke this.
    """
    if exclusive_startup is not True:
        raise RuntimeError("exclusive native Agent startup ownership required")
    with _transaction() as db:
        rows = db.execute(
            "SELECT r.* FROM native_agent_runs r WHERE r.status IN ('queued','running','awaiting_user') "
            "AND EXISTS(SELECT 1 FROM users u WHERE u.username=r.username AND u.disabled_at IS NULL) "
            "AND (r.conv_id IS NULL OR EXISTS(SELECT 1 FROM conversations c "
            "WHERE c.id=r.conv_id AND c.username=r.username AND c.deleted_at IS NULL))"
        ).fetchall()
        now = time.time()
        for row in rows:
            uncertain = row["claimed_by"] is not None
            status = "partial" if uncertain else "cancelled" if row["cancel_requested"] else "failed"
            code = "interrupted_execution_unknown" if uncertain else "interrupted_before_execution"
            summary = (
                "执行被中断；外部调用完成情况未知，未自动重试。"
                if uncertain
                else "执行开始前已中断，未自动重试。"
            )
            db.execute(
                "UPDATE native_agent_runs SET status=?,summary=?,error_code=?,finished_at=?,updated_at=? "
                "WHERE run_id=?",
                [status, summary, code, now, now, row["run_id"]],
            )
            _insert_event(
                db,
                row,
                {
                    "type": "run_interrupted",
                    "status": status,
                    "external_completion_unknown": uncertain,
                    "automatic_retry": False,
                },
            )
        return len(rows)
