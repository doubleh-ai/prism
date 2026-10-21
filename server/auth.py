"""认证 — bcrypt 密码 + cookie 会话 + CSRF 防护"""
import secrets
import bcrypt
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from .config import (RATE_LOGIN_PER_MIN, COOKIE_SECURE, COOKIE_SAMESITE,
                     CSRF_REQUIRED_HEADER, CSRF_HEADER_VALUE, ALLOWED_ORIGINS, ADMIN_USERS)
from .database import (get_db, check_rate_limit, create_session,
                       get_session, delete_session, record_activity)



def is_admin_user(username: str | None) -> bool:
    """Return whether the user can manage Prism accounts."""
    if not username:
        return False
    return username in ADMIN_USERS


def require_admin(request: Request) -> str:
    username = require_auth(request)
    if not is_admin_user(username):
        raise HTTPException(status_code=403, detail="admin required")
    return username

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(password: str, hashed: str) -> bool:
    # 兼容旧 SHA-256 格式 (64 hex chars)
    if len(hashed) == 64 and all(c in "0123456789abcdef" for c in hashed):
        import hashlib
        from .config import SECRET_KEY
        if hashed == hashlib.sha256((password + SECRET_KEY).encode()).hexdigest():
            # 迁移到 bcrypt
            new_hash = hash_password(password)
            with get_db() as db:
                db.execute("UPDATE users SET password_hash=? WHERE password_hash=?",
                           [new_hash, hashed])
            return True
        return False
    return bcrypt.checkpw(password.encode(), hashed.encode())


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
    body = await request.json()
    try:
        req = LoginRequest(**(body or {}))
    except Exception as e:
        raise HTTPException(422, validation_error_message(e))
    username = req.username.strip()
    password = req.password
    if not check_rate_limit(f"login:{request.client.host}", RATE_LOGIN_PER_MIN, 60):
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
    with get_db() as db:
        row = db.execute("SELECT password_hash, disabled_at FROM users WHERE username=?", [username]).fetchone()
    if not row or row["disabled_at"] is not None or not verify_password(password, row["password_hash"]):
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
    token = create_session(username)
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
    # Generate a per-session CSRF token. The frontend mirrors this in the
    # ``X-CSRF-Token`` header. This is the double-submit cookie pattern.
    csrf_token = secrets.token_urlsafe(32)
    resp = JSONResponse({"user": username, "csrf_token": csrf_token})
    resp.set_cookie(
        "token", token, max_age=30*86400, path="/",
        httponly=True, samesite=COOKIE_SAMESITE, secure=COOKIE_SECURE,
    )
    # CSRF token is NOT HttpOnly — JS must be able to read it. It's also
    # bound to the session via the same samesite/secure attributes.
    resp.set_cookie(
        "csrf_token", csrf_token, max_age=30*86400, path="/",
        httponly=False, samesite=COOKIE_SAMESITE, secure=COOKIE_SECURE,
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
    resp.delete_cookie("csrf_token", path="/")
    return resp
