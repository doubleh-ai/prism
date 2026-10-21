"""Fixed limited-recovery entrypoint; cannot be enabled/disabled by environment.

Only this app is accepted for the audited recovery candidate. Legacy main stays
available for baseline regression tests, never as the recovery service target.
No model, bridge, scanner or Vibe task runner is registered here.
"""

from __future__ import annotations

import json
import re
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from .auth import is_admin_user, require_admin, require_auth, verify_csrf
from .auth import login as auth_login
from .auth import logout as auth_logout
from .config import SKILL_MAP, STATIC_DIR, TEMPLATE_DIR
from .conversations import _sanitize_stored_content
from .database import get_db, init_db
from .market.router import router as market_router
from .release import APP_VERSION, release_payload
from .reports import get_saved_report, list_saved_reports, list_saved_reports_for_conv

MESSAGE = "恢复模式，暂不支持新研究"
MODE = "limited_read_only_recovery"
ASSET_VERSION = "recovery-v0198"
_SECRET_FIELDS = re.compile(
    r"(?i)(?:^|[_-])(?:password|secret|api[ _-]?key|(?:access|refresh|id|auth)[_-]?token|token|cookie|authorization)$"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)([\"']?(?:api[ _-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|token|password|secret|authorization|cookie)[\"']?\s*[=:]\s*[\"']?)[^\s&;\"'<>]+"
)
_SECRET_TEXT = re.compile(
    r"(?:sk-(?:ant-|proj-)?[\w-]{16,}|gh[pousr]_[\w]{20,}|(?i:Bearer|Basic)\s+[^\s\"'<>]+)"
)


def safe_history(value):
    """Read-time protection for historical provider failures; never rewrite DB."""
    if isinstance(value, dict):
        return {
            key: (
                "历史诊断详情不公开"
                if str(key).lower() in {"error", "exception", "traceback", "stack_trace"} and item
                else safe_history(item)
            )
            for key, item in value.items()
            if not _SECRET_FIELDS.search(str(key))
        }
    if isinstance(value, list):
        return [safe_history(item) for item in value]
    if isinstance(value, str):
        value = re.sub(
            r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|$)",
            "[redacted]",
            value,
        )
        value = re.sub(r"(?i)(https?://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", value)
        return _SECRET_ASSIGNMENT.sub(r"\1[redacted]", _SECRET_TEXT.sub("[redacted]", value))
    return value


def stored_message(content):
    cleaned = _sanitize_stored_content(content)
    try:
        return json.dumps(safe_history(json.loads(cleaned)), ensure_ascii=False)
    except (ValueError, TypeError):
        return safe_history(cleaned)


init_db()
app = FastAPI(title="Prism 有限恢复服务", docs_url=None, redoc_url=None, openapi_url=None)


class RecoveryBoundary(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path, method = request.url.path, request.method.upper()
        # The write boundary applies even to unknown routes and admins. Login and
        # logout are the only API mutation exceptions; method overrides are ignored.
        if (
            path.startswith("/api/")
            and method not in {"GET", "HEAD"}
            and (path, method)
            not in {
                ("/api/login", "POST"),
                ("/api/logout", "POST"),
            }
        ):
            response = JSONResponse({"error": MESSAGE, "detail": MESSAGE, "recovery_mode": MODE}, 503)
        elif (
            path.startswith("/api/")
            and path not in {"/api/login", "/api/recovery"}
            and not require_auth(request, optional=True)
        ):
            response = JSONResponse({"detail": "请先登录"}, 401)
        else:
            response = await call_next(request)
        response.headers["X-Prism-Recovery-Mode"] = MODE
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; font-src 'self' data:; connect-src 'self'; "
            "object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        response.headers["X-Frame-Options"] = "DENY"
        if response.status_code == 503:
            response.headers["Retry-After"] = "60"
        return response


app.add_middleware(RecoveryBoundary)


def readiness_payload():
    # Keep recovery startup independent of health.py's CLI inspection imports.
    # This proves database readiness and reports identity, never model readiness.
    try:
        with get_db() as db:
            ready = db.execute("SELECT 1").fetchone()[0] == 1
    except Exception:
        ready = False
    try:
        revision = release_payload().get("deployed_revision", "")
    except (OSError, UnicodeError):
        revision = ""
    return {
        "status": "ready" if ready else "unready",
        "version": APP_VERSION,
        "deployed_revision": revision.lower() if re.fullmatch(r"[0-9a-fA-F]{40}", revision) else "unknown",
        "checked_at": int(time.time()),
        "tz": "Asia/Shanghai",
    }


def metadata():
    ready = readiness_payload()
    return {
        "mode": MODE,
        "message": MESSAGE,
        "version": APP_VERSION,
        "deployed_revision": ready["deployed_revision"],
        "new_research_enabled": False,
        "history_scope": "仅已归属当前账号的数据库历史；外部共享文件与 CLI 档案保全但不开放",
    }


def page(name):
    text = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
    text = re.sub(r"\?v=\d+", "?v=" + ASSET_VERSION, text)
    text = text.replace(
        "</head>", '<link rel="stylesheet" href="/static/css/recovery.css?v=' + ASSET_VERSION + '"></head>'
    )
    banner = '<aside class="recovery-banner" role="status"><strong>Prism 有限恢复模式</strong><span>可登录、查看本人已保存历史与行情。新研究、修改和共享文件下载暂不可用。</span></aside>'
    text = re.sub(r"(<body[^>]*>)", r"\1" + banner, text, count=1)
    if name == "app.html":
        text = text.replace(
            "</body>", '<script src="/static/js/app.recovery.js?v=' + ASSET_VERSION + '"></script></body>'
        )
    return HTMLResponse(text)


@app.get("/static/sw.js")
async def recovery_service_worker():
    # Replace any prior cache worker and stop caching this deliberately limited UI.
    return Response(
        "self.addEventListener('install',()=>self.skipWaiting());"
        "self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(keys=>"
        "Promise.all(keys.filter(k=>k.startsWith('prism-')).map(k=>caches.delete(k))))"
        ".then(()=>self.clients.claim()).then(()=>self.registration.unregister())));",
        media_type="application/javascript",
    )


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/login")
async def login_page():
    return page("login.html")


@app.get("/")
async def index(request: Request):
    if not require_auth(request, optional=True):
        return RedirectResponse("/login")
    return page("app.html")


@app.get("/health/ready")
async def ready():
    value = readiness_payload()
    value.update(recovery_mode=MODE, new_research_enabled=False)
    return JSONResponse(value, 200 if value["status"] == "ready" else 503)


@app.get("/api/recovery")
async def recovery_metadata():
    return metadata()


@app.post("/api/login")
async def login(request: Request):
    return await auth_login(request)


@app.post("/api/logout")
async def logout(request: Request):
    verify_csrf(request)
    return auth_logout(request)


@app.get("/api/me")
async def me(request: Request):
    username = require_auth(request)
    return {"user": username, "is_admin": is_admin_user(username), "recovery": metadata()}


@app.get("/api/releases")
async def releases(request: Request):
    require_auth(request)
    return {**release_payload(), "recovery": metadata()}


@app.get("/api/models")
async def models():
    return []


@app.get("/api/skills")
async def skills():
    return [{"id": key, "name": value["name"]} for key, value in SKILL_MAP.items()]


@app.get("/api/watchlist")
async def watchlist(request: Request):
    username = require_auth(request)
    with get_db() as db:
        rows = db.execute(
            "SELECT ticker,name FROM user_watchlist WHERE username=? ORDER BY position,ticker", [username]
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


@app.get("/api/capabilities")
async def capabilities(request: Request):
    require_auth(request)
    return {"capabilities": [], "recovery": metadata()}


@app.get("/api/convs")
async def conversations(request: Request):
    username = require_auth(request)
    with get_db() as db:
        rows = db.execute(
            "SELECT id,title,skill FROM conversations WHERE username=? AND deleted_at IS NULL ORDER BY created_at DESC",
            [username],
        ).fetchall()
    return safe_history([dict(row) for row in rows])


def owned_conversation(conv_id, username):
    with get_db() as db:
        row = db.execute(
            "SELECT id,title,skill FROM conversations WHERE id=? AND username=? AND deleted_at IS NULL",
            [conv_id, username],
        ).fetchone()
    if not row:
        raise HTTPException(404, "对话不存在或无权访问")
    return dict(row)


@app.get("/api/convs/{conv_id}")
async def conversation(conv_id: str, request: Request):
    result = owned_conversation(conv_id, require_auth(request))
    with get_db() as db:
        rows = db.execute(
            "SELECT role,content FROM messages WHERE conv_id=? ORDER BY id", [conv_id]
        ).fetchall()
    result["messages"] = [{"role": row["role"], "content": stored_message(row["content"])} for row in rows]
    result["recovery"] = metadata()
    return safe_history(result)


@app.get("/api/convs/{conv_id}/reports")
async def conversation_reports(conv_id: str, request: Request):
    username = require_auth(request)
    owned_conversation(conv_id, username)
    return safe_history(
        {
            "reports": [
                {
                    "kind": "saved",
                    "report_id": row["id"],
                    "title": row["title"],
                    "filename": row["title"],
                    "mtime": row["created_at"],
                    "summary": row["summary"],
                }
                for row in list_saved_reports_for_conv(username, conv_id)
            ]
        }
    )


@app.get("/api/reports/library")
async def reports(request: Request, limit: int = 50):
    return {"reports": safe_history(list_saved_reports(require_auth(request), limit))}


@app.get("/api/reports/library/{report_id}")
async def report(report_id: str, request: Request):
    result = get_saved_report(require_auth(request), report_id)
    if result is None:
        raise HTTPException(404, "报告不存在或无权访问")
    return safe_history(result)


@app.get("/api/capability-calls")
async def calls(request: Request, view: str = "", limit: int = 20):
    username = require_auth(request)
    with get_db() as db:
        rows = db.execute(
            "SELECT call_id,run_id,conv_id,capability_id,status,created_at,completed_at,input_summary,result_summary,target_json FROM capability_calls WHERE username=? ORDER BY created_at DESC LIMIT ?",
            [username, min(100, max(1, limit))],
        ).fetchall()
    result = []
    for row in rows:
        call = dict(row)
        try:
            call["target"] = json.loads(call.pop("target_json") or "{}")
        except ValueError:
            call["target"] = {}
        if not isinstance(call["target"], dict):
            call["target"] = {}
        call["label"] = call["capability_id"]
        call["source_kind"] = "platform_data"
        if call["status"] == "failed":
            call["result_summary"] = "历史调用未完成；诊断详情不公开"
        elif call["status"] == "running":
            call.update(status="interrupted", result_summary="恢复模式保留历史调用，任务未重新执行")
        if not view or call["target"].get("view") == view:
            result.append(call)
    return {"calls": safe_history(result)}


@app.get("/api/convs/{conv_id}/capability-calls")
async def conversation_calls(conv_id: str, request: Request):
    owned_conversation(conv_id, require_auth(request))
    result = await calls(request, limit=100)
    return {"calls": [call for call in result["calls"] if call["conv_id"] == conv_id]}


@app.get("/api/vibe/engine")
async def vibe_engine(request: Request):
    require_auth(request)
    return {"status": "recovery_disabled", "detail": MESSAGE, "engine": {"configured": False}}


def stored_vibe(row):
    try:
        value = json.loads(row["plan_json"])
    except ValueError:
        value = {}
    if not isinstance(value, dict):
        value = {}
    value = safe_history(value)
    if value.get("status") in {"failed", "error"}:
        value["stage_label"] = "历史研究未完成；诊断详情不公开"
    for item in value.get("activity") or []:
        if isinstance(item, dict) and item.get("status") in {"error", "failed", "warning"}:
            item.update(summary="历史工具调用未完成", detail="诊断详情不公开")
    value.pop("_username", None)
    value.update(id=row["id"], created_at=row["created_at"])
    if value.get("status") in {"queued", "running", "cancelling"}:
        value.update(status="interrupted", error=MESSAGE + "；历史内容保留，任务没有重新执行")
    quality = value.get("quality") if isinstance(value.get("quality"), dict) else {}
    value["quality"] = {**quality, "grade": "历史来源待核验", "verified_sources": 0}
    return value


@app.get("/api/vibe/plans")
async def vibe_plans(request: Request, limit: int = 30):
    username = require_auth(request)
    with get_db() as db:
        rows = db.execute(
            "SELECT id,plan_json,created_at FROM vibe_plans WHERE username=? ORDER BY created_at DESC LIMIT ?",
            [username, min(100, max(1, limit))],
        ).fetchall()
    return {"plans": [stored_vibe(row) for row in rows]}


@app.get("/api/vibe/plans/{plan_id}")
async def vibe_plan(plan_id: str, request: Request):
    username = require_auth(request)
    with get_db() as db:
        row = db.execute(
            "SELECT id,plan_json,created_at FROM vibe_plans WHERE id=? AND username=?", [plan_id, username]
        ).fetchone()
    if not row:
        raise HTTPException(404, "研究不存在或无权访问")
    return {"plan": stored_vibe(row), "recovery": metadata()}


@app.get("/api/analysis/{analysis_id}/report")
async def analysis_report(analysis_id: str, request: Request):
    from .analysis_engine import get_analysis_record

    result = get_analysis_record(analysis_id, require_auth(request))
    if result is None:
        raise HTTPException(404, "分析不存在或无权访问")
    return {**safe_history(result), "recovery": metadata()}


@app.get("/api/admin/health")
async def admin_health(request: Request):
    require_admin(request)
    return {"ok": readiness_payload()["status"] == "ready", "recovery": metadata()}


app.include_router(market_router)
