"""Opt-in administrator recovery with a privately configured, fixed recipient."""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from . import config
from .auth import hash_password, is_admin_user, require_admin
from .database import check_rate_limit, get_db
from .recovery_mail import DeliveryUnavailable, send_recovery_code

router = APIRouter()
log = logging.getLogger("prism.auth.recovery")
TTL = 600
MAX_ATTEMPTS = 5
INVALID = "验证码无效或已过期，请重新申请"
UNAVAILABLE = "管理员邮箱找回尚未启用，请联系平台维护者"
_EMAIL = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}\Z"
)


@dataclass(frozen=True, repr=False)
class Settings:
    username: str
    recipient: str
    sender: str
    template_id: int
    region: str
    secret_id: str = field(repr=False)
    secret_key: str = field(repr=False)


def settings():
    """No automatic mailbox discovery, user-controlled recipient, or insecure default."""
    if os.environ.get("PRISM_ADMIN_RECOVERY_ENABLED") != "1":
        return None
    values = {
        key: os.environ.get("PRISM_ADMIN_RECOVERY_" + key, "").strip()
        for key in ("USER", "EMAIL", "SENDER", "TEMPLATE_ID", "REGION", "SECRET_ID", "SECRET_KEY")
    }
    if not all(values.values()) or not is_admin_user(values["USER"]):
        return None
    if any(not _EMAIL.fullmatch(values[key]) or len(values[key]) > 254 for key in ("EMAIL", "SENDER")):
        return None
    if values["REGION"] not in {"ap-guangzhou", "ap-hongkong"}:
        return None
    if not re.fullmatch(r"[0-9]{1,12}", values["TEMPLATE_ID"]) or int(values["TEMPLATE_ID"]) < 1:
        return None
    return Settings(
        values["USER"],
        values["EMAIL"],
        values["SENDER"],
        int(values["TEMPLATE_ID"]),
        values["REGION"],
        values["SECRET_ID"],
        values["SECRET_KEY"],
    )


def digest(*parts):
    return hmac.new(config.SECRET_KEY.encode(), json.dumps(parts).encode(), hashlib.sha256).hexdigest()


def binding(cfg, user):
    # A password change or private recipient change invalidates outstanding codes.
    return digest("recovery-binding", cfg.username, cfg.recipient, user["password_hash"])


def active_user(db, cfg):
    return db.execute(
        "SELECT password_hash FROM users WHERE username=? AND disabled_at IS NULL", [cfg.username]
    ).fetchone()


def rate_limit(key, count, window):
    if not check_rate_limit("admin-recovery:" + key, count, window):
        raise HTTPException(429, "请求过于频繁，请稍后重试")


def request_code():
    cfg = settings()
    if cfg is None:
        raise HTTPException(503, UNAVAILABLE)
    # Account-wide buckets prevent distributed mailbox flooding. No new account is created.
    rate_limit("send-minute", 1, 60)
    rate_limit("send-hour", 6, 3600)
    rate_limit("send-day", 12, 86400)
    now, challenge, code = int(time.time()), secrets.token_hex(32), f"{secrets.randbelow(100_000_000):08d}"
    with get_db() as db:
        user = active_user(db, cfg)
        if user is None:
            raise HTTPException(503, UNAVAILABLE)
        db.execute("DELETE FROM admin_recovery_challenges WHERE expires_at<=?", [now])
        db.execute(
            """INSERT INTO admin_recovery_challenges
               (id, username, binding, code_hash, expires_at, attempts, state)
               VALUES (?,?,?,?,?,0,'pending')""",
            [
                challenge,
                cfg.username,
                binding(cfg, user),
                digest("recovery-code", challenge, code),
                now + TTL,
            ],
        )
    try:
        send_recovery_code(cfg, code)
    except DeliveryUnavailable:
        with get_db() as db:
            db.execute("UPDATE admin_recovery_challenges SET state='delivery_failed' WHERE id=?", [challenge])
        log.warning("recovery_delivery_unconfirmed")
        raise HTTPException(502, "邮件发送未确认，本次验证码已作废；请稍后手动重试") from None
    with get_db() as db:
        db.execute("UPDATE admin_recovery_challenges SET state='sent' WHERE id=?", [challenge])
    log.info("recovery_delivery_accepted")
    return {
        "challenge_id": challenge,
        "expires_in": TTL,
        "detail": "邮件服务已接受发送请求，请查收预先绑定的管理员邮箱（含垃圾邮件）",
    }


def reset_password(body):
    cfg = settings()
    if cfg is None:
        raise HTTPException(503, UNAVAILABLE)
    if (
        not isinstance(body, dict)
        or set(body) != {"challenge_id", "code", "password", "confirm_password"}
        or not all(isinstance(value, str) for value in body.values())
    ):
        raise HTTPException(422, "请填写验证码并重复输入新密码")
    challenge, code, password = body["challenge_id"], body["code"], body["password"]
    if not re.fullmatch(r"[0-9a-f]{64}", challenge) or not re.fullmatch(r"[0-9]{8}", code):
        raise HTTPException(400, INVALID)
    try:
        byte_length = len(password.encode())
    except UnicodeError:
        byte_length = 73
    if len(password) < 12 or byte_length > 72 or password != body["confirm_password"]:
        raise HTTPException(422, "新密码需至少 12 个字符、不超过 72 字节，且两次输入一致")
    # Invalid guesses cannot trigger expensive bcrypt work. Failed attempts commit too.
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT * FROM admin_recovery_challenges WHERE id=?", [challenge]).fetchone()
            user = active_user(db, cfg)
            valid = bool(
                row
                and user
                and row["state"] == "sent"
                and row["username"] == cfg.username
                and row["expires_at"] > time.time()
                and row["attempts"] < MAX_ATTEMPTS
                and hmac.compare_digest(row["binding"], binding(cfg, user))
            )
            if valid:
                db.execute("UPDATE admin_recovery_challenges SET attempts=attempts+1 WHERE id=?", [challenge])
                valid = hmac.compare_digest(row["code_hash"], digest("recovery-code", challenge, code))
            if valid:
                db.execute(
                    "UPDATE users SET password_hash=? WHERE username=? AND disabled_at IS NULL",
                    [hash_password(password), cfg.username],
                )
                db.execute("DELETE FROM sessions WHERE username=?", [cfg.username])
                db.execute(
                    "UPDATE admin_recovery_challenges SET state='consumed' WHERE username=?", [cfg.username]
                )
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    if not valid:
        raise HTTPException(400, INVALID)
    log.info("recovery_password_reset_sessions_revoked")
    return {"detail": "密码已重置，旧登录已失效。请使用新密码重新登录"}


async def bounded_json(request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 2048:
            raise HTTPException(413, "请求内容过长")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise HTTPException(422, "请求格式不正确") from None


@router.get("/recover", response_class=HTMLResponse)
def recovery_page():
    return HTMLResponse(
        (config.TEMPLATE_DIR / "recover.html").read_text(),
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.get("/api/auth/recovery/status")
def recovery_status():
    # Configuration availability only, never claim inbox delivery is verified.
    return JSONResponse({"configured": settings() is not None}, headers={"Cache-Control": "no-store"})


@router.get("/api/admin/recovery")
def admin_recovery_status(request: Request):
    require_admin(request)
    cfg = settings()
    return JSONResponse(
        {
            "configured": cfg is not None,
            "passwords_readable": False,
            "recipient": (cfg.recipient[0] + "***@" + cfg.recipient.split("@", 1)[1]) if cfg else None,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/auth/recovery/request")
async def recovery_request(request: Request):
    # No recipient or account can be selected by an unauthenticated caller.
    if await bounded_json(request) != {}:
        raise HTTPException(422, "此入口仅向预先绑定的管理员邮箱发送验证码")
    result = await run_in_threadpool(request_code)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/api/auth/recovery/reset")
async def recovery_reset(request: Request):
    # A fixed bucket has bounded storage, even behind a reverse proxy or botnet.
    await run_in_threadpool(rate_limit, "reset-minute", 30, 60)
    result = await run_in_threadpool(reset_password, await bounded_json(request))
    return JSONResponse(result, headers={"Cache-Control": "no-store"})
