"""认证 — bcrypt 密码 + cookie 会话"""
import bcrypt
from fastapi import Request, HTTPException, Response
from fastapi.responses import JSONResponse
from .config import RATE_LOGIN_PER_MIN
from .database import (get_db, check_rate_limit, create_session,
                       get_session, delete_session)


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


async def login(request: Request):
    from .logger import Event
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")
    if not check_rate_limit(f"login:{request.client.host}", RATE_LOGIN_PER_MIN, 60):
        Event.auth(username, "rate_limited").warn()
        raise HTTPException(status_code=429, detail="请求过于频繁")
    with get_db() as db:
        row = db.execute("SELECT password_hash FROM users WHERE username=?", [username]).fetchone()
    if not row or not verify_password(password, row["password_hash"]):
        Event.auth(username, "failed").warn()
        raise HTTPException(status_code=403)
    Event.auth(username, "ok").info()
    token = create_session(username)
    resp = JSONResponse({"user": username})
    resp.set_cookie("token", token, max_age=30*86400, path="/", httponly=True, samesite="lax")
    return resp


def logout(request: Request):
    token = request.cookies.get("token")
    if token:
        delete_session(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("token", path="/")
    return resp
