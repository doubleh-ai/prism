"""认证 — bcrypt 密码 + cookie 会话 + 受控回环运维网关 + CSRF 防护"""

import contextlib
import os
import re
import secrets
from pathlib import Path

import bcrypt
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .config import (
    ADMIN_USERS,
    ALLOWED_ORIGINS,
    COOKIE_SAMESITE,
    COOKIE_SECURE,
    CSRF_HEADER_VALUE,
    CSRF_REQUIRED_HEADER,
    DATA_DIR,
    RATE_LOGIN_PER_MIN,
)
from .credential_permissions import is_private_file
from .database import check_rate_limit, create_session, delete_session, get_db, get_session, record_activity

OPS_GATEWAY_ENV = "PRISM_OPS_GATEWAY_TOKEN_FILE"
_OPS_PRINCIPAL_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})


def _read_private_gateway_token() -> str:
    raw_path = os.environ.get(OPS_GATEWAY_ENV) or str(DATA_DIR / "ops_gateway_token")
    path = Path(raw_path)
    if not path.is_absolute() or ".." in path.parts:
        return ""
    try:
        if path.resolve(strict=True) != path:
            return ""
        with contextlib.ExitStack() as stack:
            parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            stack.callback(os.close, parent)
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
            stack.callback(os.close, fd)
            if not is_private_file(fd, parent):
                return ""
            raw = os.read(fd, 514)
            if raw.endswith(b"\n"):
                raw = raw[:-1]
            token = raw.decode("ascii")
            if not (32 <= len(token) <= 512 and all(33 <= ord(c) <= 126 for c in token)):
                return ""
            return token
    except (OSError, ValueError, UnicodeError, RuntimeError):
        return ""


def ops_gateway_identity(request: Request) -> dict | None:
    """Validate an authenticated loopback request from the external ops gateway."""
    client = request.client
    if client is None or client.host not in _LOOPBACK_HOSTS:
        return None
    presented = request.headers.get("X-Prism-Ops-Token", "")
    principal = request.headers.get("X-Prism-Ops-Principal", "").strip()
    if not presented or not _OPS_PRINCIPAL_RE.fullmatch(principal):
        return None
    expected = _read_private_gateway_token()
    if not expected or not secrets.compare_digest(presented, expected):
        return None
    candidates = [principal]
    candidates.extend(u for u in sorted(ADMIN_USERS) if u not in candidates)
    placeholders = ",".join("?" for _ in candidates)
    with get_db() as db:
        rows = db.execute(
            f"SELECT username FROM users WHERE username IN ({placeholders}) AND disabled_at IS NULL",
            candidates,
        ).fetchall()
    active = {row["username"] for row in rows}
    resolved = next((u for u in candidates if u in active), principal)
    return {"user": resolved, "principal": principal, "via": "ops_gateway"}


def is_admin_user(username: str | None) -> bool:
    """Prism user accounts carry no built-in admin privilege; ops uses the 2FA gateway."""
    _ = username
    return False


def require_admin(request: Request) -> str:
    gw = ops_gateway_identity(request)
    if gw is not None:
        return gw["user"]
    if request.cookies.get("token"):
        require_auth(request)
        raise HTTPException(status_code=403, detail="admin required")
    raise HTTPException(status_code=401)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str, *, migrate: bool = True) -> bool:
    # 兼容旧 SHA-256 格式 (64 hex chars)
    if len(hashed) == 64 and all(c in "0123456789abcdef" for c in hashed):
        import hashlib

        from .config import SECRET_KEY

        if hashed == hashlib.sha256((password + SECRET_KEY).encode()).hexdigest():
            if not migrate:
                return True
            # 迁移到 bcrypt
            new_hash = hash_password(password)
            with get_db() as db:
                db.execute("UPDATE users SET password_hash=? WHERE password_hash=?", [new_hash, hashed])
            return True
        return False
    return bcrypt.checkpw(password.encode(), hashed.encode())


def authenticated_session(username: str, password: str) -> str | None:
    """Recheck the verified hash atomically with session issuance after slow hashing."""
    with get_db() as db:
        row = db.execute(
            "SELECT password_hash, disabled_at FROM users WHERE username=?", [username]
        ).fetchone()
    if not row or row["disabled_at"] is not None:
        return None
    old_hash = row["password_hash"]
    if not verify_password(password, old_hash, migrate=False):
        return None
    new_hash = hash_password(password) if len(old_hash) == 64 else None
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            current = db.execute(
                "SELECT password_hash FROM users WHERE username=? AND disabled_at IS NULL", [username]
            ).fetchone()
            valid = current and secrets.compare_digest(current["password_hash"], old_hash)
            if valid and new_hash:
                db.execute("UPDATE users SET password_hash=? WHERE username=?", [new_hash, username])
            token = create_session(username) if valid else None
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    return token


def require_auth(request: Request, optional: bool = False) -> str | None:
    """Return the authenticated username, or None when ``optional=True``.

    Raises HTTP 401 when authentication is required but no valid session
    cookie is present (or the session has expired).
    """
    token = request.cookies.get("token")
    if not token:
        if optional:
            return None
        raise HTTPException(status_code=401)
    user = get_session(token)
    if not user:
        if optional:
            return None
        raise HTTPException(status_code=401)
    return user


def require_active_account(db, owner: str) -> None:
    """Recheck account access on the connection owning the current operation."""
    if not db.execute("SELECT 1 FROM users WHERE username=? AND disabled_at IS NULL", [owner]).fetchone():
        raise HTTPException(401, "请重新登录")


def require_account_context(request: Request, authenticated_user: str) -> None:
    """Fence a browser write to the account that prepared it, when supplied.

    The caller must authenticate first. Resource ownership remains mandatory. Older clients may
    omit this optional context; it is never an alternative login credential.
    """
    if not authenticated_user:
        raise HTTPException(401)
    if request.headers.get("X-Prism-Account", authenticated_user) != authenticated_user:
        raise HTTPException(409, "账号已经切换，请刷新后再提交")


def verify_csrf(request: Request) -> None:
    """Verify the request carries the CSRF defense header.

    For state-changing methods we require ``X-Requested-With: XMLHttpRequest``.
    Browsers will not add custom headers to cross-site form submissions
    without a CORS preflight, so the absence of this header is a strong
    CSRF signal. Same-origin fetch() calls can set it freely.

    Additionally, if ``ALLOWED_ORIGINS`` is configured we enforce that the
    request's ``Origin`` header matches one of the allowed origins.
    """
    method = request.method.upper()
    if method in ("GET", "HEAD", "OPTIONS"):
        return

    # Always require the X-Requested-With marker.
    if request.headers.get(CSRF_REQUIRED_HEADER) != CSRF_HEADER_VALUE:
        raise HTTPException(
            status_code=403,
            detail=f"Missing {CSRF_REQUIRED_HEADER}: {CSRF_HEADER_VALUE} header",
        )

    # Optional Origin pinning (configure ALLOWED_ORIGINS in production).
    if ALLOWED_ORIGINS:
        origin = request.headers.get("origin", "").rstrip("/")
        if origin and origin not in [o.rstrip("/") for o in ALLOWED_ORIGINS]:
            raise HTTPException(status_code=403, detail="Origin not allowed")


async def login(request: Request):
    from .logger import Event
    from .models import LoginRequest, validation_error_message

    try:
        body = await request.json()
        req = LoginRequest(**(body or {}))
    except Exception as e:
        raise HTTPException(422, validation_error_message(e))
    username = req.username.strip()
    password = req.password
    # Unix-domain ASGI transports have no remote address. Keep one shared,
    # bounded bucket for that transport; never trust a forwarded address header.
    client_host = request.client.host if request.client is not None else "local-unix-socket"
    if not check_rate_limit(f"login:{client_host}", RATE_LOGIN_PER_MIN, 60):
        Event.auth(username, "rate_limited").warn()
        record_activity(
            username=username,
            feature="auth",
            action="login_rate_limited",
            target_type="user",
            target_id=username,
            target_label=username,
            status="failed",
            request=request,
        )
        raise HTTPException(status_code=429, detail="请求过于频繁")
    token = authenticated_session(username, password)
    if token is None:
        Event.auth(username, "failed").warn()
        record_activity(
            username=username,
            feature="auth",
            action="login",
            target_type="user",
            target_id=username,
            target_label=username,
            status="failed",
            request=request,
        )
        raise HTTPException(status_code=403)
    Event.auth(username, "ok").info()
    record_activity(
        username=username,
        feature="auth",
        action="login",
        target_type="user",
        target_id=username,
        target_label=username,
        status="success",
        request=request,
    )
    resp = JSONResponse({"user": username})
    resp.set_cookie(
        "token",
        token,
        max_age=30 * 86400,
        path="/",
        httponly=True,
        samesite=COOKIE_SAMESITE,
        secure=COOKIE_SECURE,
    )
    return resp


def logout(request: Request):
    token = request.cookies.get("token")
    username = get_session(token) if token else ""
    if token:
        delete_session(token)
    if username:
        record_activity(
            username=username,
            feature="auth",
            action="logout",
            target_type="user",
            target_id=username,
            target_label=username,
            status="success",
            request=request,
        )
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("token", path="/")
    return resp
