"""Prism — FastAPI 主入口"""

import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from lru import LRU
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import StreamingResponse

from .activity import ActivityMiddleware
from .analysis_engine import _sse as _analysis_sse
from .analysis_engine import _store, get_analysis_record, start_analysis, stream_events
from .analysis_graph import router as graph_router
from .auth import hash_password, is_admin_user, require_admin, require_auth, verify_csrf
from .auth import login as auth_login
from .auth import logout as auth_logout
from .capabilities import router as capabilities_router
from .chat import chat_cancel, chat_respond, chat_stream
from .config import (
    CORS_ALLOW_CREDENTIALS,
    CORS_ORIGINS,
    DEFAULT_MODEL,
    LOG_FILE,
    MODELS,
    RATE_EXPLAIN_PER_MIN,
    SKILL_MAP,
    STATIC_DIR,
    TEMPLATE_DIR,
)
from .conversations import (
    batch_delete_convs,
    create_conv,
    delete_conv,
    get_conv,
    list_convs,
    list_trash,
    permanent_delete_conv,
    restore_conv,
    update_skill,
)
from .database import (
    admin_activity_events,
    admin_activity_insights,
    admin_activity_overview,
    admin_activity_users,
    admin_usage_stats,
    check_rate_limit,
    create_user,
    delete_user_account,
    init_db,
    is_username_retired,
    list_users,
    record_activity,
    set_user_disabled,
    set_user_password,
    user_exists,
)
from .diagnostics import router as diagnostics_router
from .health import admin_health_payload, readiness_payload
from .indicators.router import router as indicators_router
from .industry_router import router as industry_router
from .market.router import router as market_router
from .middleware import configure_logging, install_error_handlers, install_request_id_middleware
from .operations import router as operations_router
from .release import release_payload
from .report_files import REPORT_CSP, list_report_files, read_report_file
from .reports import list_saved_reports_for_conv
from .reports import router as reports_router
from .research import init_research_db, recover_interrupted_runs
from .research import router as research_router
from .vibe import router as vibe_router
from .watchlist import router as watchlist_router

# ── Logging ──
LOG_PATH = Path(LOG_FILE)
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
# Log level can be overridden via env, e.g. PRISM_LOG_LEVEL=DEBUG
log_level = os.environ.get("PRISM_LOG_LEVEL", "INFO")

log = configure_logging(str(LOG_PATH), level=log_level)

# ── Init ──
init_db()
init_research_db()
if os.environ.get("PRISM_SINGLE_WORKER") == "1":
    from .capabilities import recover_interrupted_calls

    recover_interrupted_calls()
    recover_interrupted_runs()


def ensure_configured_admin_user() -> None:
    """Create the configured admin user on first boot when credentials exist."""
    admin_user = os.environ.get("ADMIN_USER", "admin").strip()
    admin_pass = os.environ.get("ADMIN_PASS", "")
    if not admin_user or not admin_pass or user_exists(admin_user):
        return
    if is_username_retired(admin_user):
        log.warning("Configured admin account is retired; automatic recreation is disabled")
        return
    try:
        create_user(admin_user, hash_password(admin_pass))
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        log.warning("Configured admin creation was restricted; no existing account was replaced")
        return
    log.info("Configured admin user created: %s", admin_user)


ensure_configured_admin_user()

# ── FastAPI app ──
app = FastAPI(title="Prism")

# Install request-ID + global error handlers early so all routes benefit
install_request_id_middleware(app)
install_error_handlers(app)

# ── CORS (only when explicitly configured) ──
if CORS_ORIGINS:
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=CORS_ALLOW_CREDENTIALS,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-Requested-With", "X-CSRF-Token", "X-Request-ID"],
        max_age=600,
    )

# ── Security headers middleware ──
# Defense-in-depth headers. The frontend's inline viewer iframe needs
# 'unsafe-inline' for its own assets; for non-HTML responses we still send
# the header — the browser only enforces CSP that matches the document type.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "  # app.js + inline onclick handlers
    "style-src 'self' 'unsafe-inline'; "  # inline <style> + dynamic styles
    "img-src 'self' data: https:; "  # data: URIs for SVG icons
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-src 'self'; "  # report viewer iframe
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "object-src 'none';"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach defensive HTTP headers to every response."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/api/reports/") and path.endswith((".html", ".htm")):
            # A response-level sandbox protects new windows and direct access too.
            response.headers["Content-Security-Policy"] = REPORT_CSP
            response.headers["Cache-Control"] = "no-store"
        else:
            response.headers.setdefault("Content-Security-Policy", _CSP)
            response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        # HSTS — only meaningful when served over HTTPS. Harmless on HTTP.
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response


class StaticCacheControlMiddleware(BaseHTTPMiddleware):
    """Attach Cache-Control: immutable to /static/* responses.

    Static assets are versioned via ``?v=N`` query strings, so the URL
    is the cache key. max-age=31536000 + immutable lets the browser
    skip revalidation for a year. Dynamic routes (/api/*) are unaffected
    so auth-gated responses don't get immortal-cached.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/static/sw.js":
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        elif request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


class CSRFMiddleware(BaseHTTPMiddleware):
    """Enforce CSRF defense for all state-changing requests.

    Defense layers (any one is sufficient):
    1. ``X-Requested-With: XMLHttpRequest`` — browsers won't add custom
       headers to cross-site form submissions.
    2. ``ALLOWED_ORIGINS`` env-var — strict Origin pinning (optional).
    3. ``SameSite=strict`` cookie attribute — browser won't send the
       session cookie on cross-site requests.
    """

    # Endpoints that must remain accessible without CSRF (e.g. login).
    # These have no session cookie yet, so the requirement doesn't apply.
    _EXEMPT_PATHS = frozenset(["/api/login"])

    async def dispatch(self, request: Request, call_next):
        if (
            request.method.upper() in ("POST", "PUT", "DELETE", "PATCH")
            and request.url.path not in self._EXEMPT_PATHS
        ):
            try:
                verify_csrf(request)
            except HTTPException as e:
                from fastapi.responses import JSONResponse

                return JSONResponse({"detail": e.detail}, status_code=e.status_code)
        return await call_next(request)


app.add_middleware(GZipMiddleware, minimum_size=512)
app.add_middleware(StaticCacheControlMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(CSRFMiddleware)
app.add_middleware(ActivityMiddleware)

# Static files (proper caching via StaticFiles)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Template helpers ──
def render_template(name: str) -> str:
    """Read and return a template file."""
    path = TEMPLATE_DIR / name
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


# ── Pages ──
@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return HTMLResponse(render_template("login.html"), headers={"Cache-Control": "no-store"})


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    u = require_auth(request, optional=True)
    if not u:
        return RedirectResponse("/login")
    return HTMLResponse(render_template("app.html"), headers={"Cache-Control": "no-store"})


# ── Auth ──
@app.get("/health/ready")
async def api_readiness():
    payload = readiness_payload()
    return JSONResponse(
        payload,
        status_code=200 if payload["status"] == "ready" else 503,
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/login")
async def api_login(request: Request):
    return await auth_login(request)


@app.post("/api/logout")
async def api_logout(request: Request):
    return auth_logout(request)


@app.get("/api/me")
async def api_me(request: Request):
    u = require_auth(request, optional=True)
    if not u:
        raise HTTPException(401)
    return {"user": u, "is_admin": is_admin_user(u)}


@app.get("/api/releases")
async def api_releases(request: Request):
    require_auth(request)
    return release_payload()


_UI_ACTIVITY_RULES = {
    "chat": {"open"},
    "market": {"open", "watchlist_add", "watchlist_remove", "screener_start"},
    "analysis": {"open"},
    "vibe": {"open"},
    "report": {"open"},
    "industry": {"open"},
    "admin": {"open"},
}


@app.post("/api/activity")
async def api_track_activity(request: Request):
    """Record allowlisted UI-only events without storing prompts or page contents."""
    username = require_auth(request)
    from .models import ActivityTrackRequest, validation_error_message

    try:
        payload = ActivityTrackRequest(**((await request.json()) or {}))
    except Exception as exc:
        raise HTTPException(422, validation_error_message(exc))
    allowed = _UI_ACTIVITY_RULES.get(payload.feature, set())
    if payload.action not in allowed:
        raise HTTPException(422, detail="activity event is not allowed")
    target_type = payload.target_type or ""
    target_id = payload.target_id or ""
    target_label = payload.target_label or ""
    if payload.action == "open":
        target_type = target_id = target_label = ""
    record_activity(
        username=username,
        feature=payload.feature,
        action=payload.action,
        target_type=target_type,
        target_id=target_id,
        target_label=target_label,
        meta={"source": "ui"},
        request=request,
    )
    return {"ok": True}


# ── Admin: account management ──
@app.get("/api/admin/stats")
async def api_admin_stats(request: Request):
    require_admin(request)
    return admin_usage_stats()


@app.get("/api/admin/health")
async def api_admin_health(request: Request):
    require_admin(request)
    return admin_health_payload()


@app.get("/api/admin/analytics/overview")
async def api_admin_analytics_overview(request: Request, range: str = "7d"):
    require_admin(request)
    return admin_activity_overview(range)


@app.get("/api/admin/analytics/insights")
async def api_admin_analytics_insights(request: Request, range: str = "7d"):
    require_admin(request)
    return admin_activity_insights(range)


@app.get("/api/admin/analytics/events")
async def api_admin_analytics_events(
    request: Request,
    range: str = "7d",
    username: str = "",
    feature: str = "",
    action: str = "",
    status: str = "",
    query: str = "",
    limit: int = 80,
    offset: int = 0,
):
    require_admin(request)
    return admin_activity_events(
        range_key=range,
        username=username.strip(),
        feature=feature.strip(),
        action=action.strip(),
        status=status.strip(),
        query=query.strip(),
        limit=limit,
        offset=offset,
    )


@app.get("/api/admin/analytics/users")
async def api_admin_analytics_users(request: Request, range: str = "7d"):
    require_admin(request)
    return admin_activity_users(range)


@app.get("/api/admin/users")
async def api_admin_list_users(request: Request):
    current = require_admin(request)
    users = []
    for row in list_users():
        users.append(
            {
                "username": row["username"],
                "created_at": row["created_at"],
                "disabled_at": row["disabled_at"],
                "active_sessions": row["active_sessions"],
                "conversation_count": row["conversation_count"],
                "is_admin": is_admin_user(row["username"]),
                "is_current": row["username"] == current,
            }
        )
    return {"users": users, "current_user": current}


@app.post("/api/admin/users")
async def api_admin_create_user(request: Request):
    require_admin(request)
    from .models import AdminUserCreateRequest, validation_error_message

    body = await request.json()
    try:
        req = AdminUserCreateRequest(**(body or {}))
    except Exception as e:
        raise HTTPException(422, validation_error_message(e))
    username = req.username.strip()
    if user_exists(username):
        raise HTTPException(status_code=409, detail="user already exists")
    create_user(username, hash_password(req.password))
    return {"ok": True, "user": username}


@app.put("/api/admin/users/{username}/password")
async def api_admin_reset_password(username: str, request: Request):
    require_admin(request)
    from .models import AdminPasswordResetRequest, validation_error_message

    body = await request.json()
    try:
        req = AdminPasswordResetRequest(**(body or {}))
    except Exception as e:
        raise HTTPException(422, validation_error_message(e))
    if not set_user_password(username, hash_password(req.password)):
        raise HTTPException(status_code=404, detail="user not found")
    return {"ok": True}


@app.put("/api/admin/users/{username}/status")
async def api_admin_set_user_status(username: str, request: Request):
    current = require_admin(request)
    from .models import AdminUserStatusRequest, validation_error_message

    body = await request.json()
    try:
        req = AdminUserStatusRequest(**(body or {}))
    except Exception as e:
        raise HTTPException(422, validation_error_message(e))
    if username == current and req.disabled:
        raise HTTPException(status_code=400, detail="cannot disable current user")
    if not set_user_disabled(username, req.disabled):
        raise HTTPException(status_code=404, detail="user not found")
    return {"ok": True}


@app.delete("/api/admin/users/{username}")
async def api_admin_delete_user(username: str, request: Request):
    current = require_admin(request)
    if username == current:
        raise HTTPException(status_code=400, detail="cannot delete current user")
    if not delete_user_account(username):
        raise HTTPException(status_code=404, detail="user not found")
    return {"ok": True}


# ── Conversations ──


@app.get("/api/convs")
async def api_list_convs(request: Request):
    return list_convs(request)


@app.post("/api/convs")
async def api_create_conv(request: Request):
    return await create_conv(request)


@app.get("/api/convs/{cid}")
async def api_get_conv(cid: str, request: Request):
    return get_conv(cid, request)


@app.delete("/api/convs/{cid}")
async def api_delete_conv(cid: str, request: Request):
    return delete_conv(cid, request)


@app.post("/api/convs/{cid}/restore")
async def api_restore_conv(cid: str, request: Request):
    return restore_conv(cid, request)


@app.delete("/api/convs/{cid}/permanent")
async def api_permanent_delete_conv(cid: str, request: Request):
    return permanent_delete_conv(cid, request)


@app.put("/api/convs/{cid}/skill")
async def api_update_skill(cid: str, request: Request):
    return await update_skill(cid, request)


@app.get("/api/trash")
async def api_list_trash(request: Request):
    return list_trash(request)


@app.post("/api/convs/batch-delete")
async def api_batch_delete_convs(request: Request):
    return await batch_delete_convs(request)


# ── Chat ──


@app.get("/api/models")
async def api_models():
    return [
        {
            "id": key,
            "name": model["name"],
            "configured": bool(model.get("api_key")),
            "default": key == DEFAULT_MODEL,
        }
        for key, model in MODELS.items()
    ]


@app.get("/api/skills")
async def api_skills():
    return [{"id": k, "name": v["name"], "shortcut": "/" + k[:2]} for k, v in SKILL_MAP.items()]


@app.post("/api/chat/stream")
async def api_chat_stream(request: Request):
    return await chat_stream(request)


@app.post("/api/chat/respond")
async def api_chat_respond(request: Request):
    return await chat_respond(request)


@app.post("/api/chat/cancel")
async def api_chat_cancel(request: Request):
    return await chat_cancel(request)


# ── Market data (v0.6) ──

app.include_router(market_router)
app.include_router(capabilities_router)
app.include_router(graph_router)
app.include_router(industry_router)
app.include_router(indicators_router)
app.include_router(reports_router)
app.include_router(vibe_router)
app.include_router(watchlist_router)
app.include_router(research_router)
app.include_router(diagnostics_router)
app.include_router(operations_router)


# ── Report viewer ──
_REPORT_KIND = {".html": "html", ".htm": "html", ".pdf": "pdf", ".csv": "csv", ".md": "md", ".json": "json"}


@app.get("/api/convs/{conv_id}/reports")
async def api_list_reports(conv_id: str, request: Request):
    """List only artifacts explicitly assigned to this user and conversation."""
    u = require_auth(request)
    try:
        out = list_report_files(u, conv_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        return {"reports": []}
    for artifact in out:
        artifact["kind"] = _REPORT_KIND.get(Path(artifact["filename"]).suffix.lower(), "file")

    for rep in list_saved_reports_for_conv(u, conv_id, limit=50):
        out.append(
            {
                "filename": rep.get("title") or "Saved report",
                "title": rep.get("title") or "Saved report",
                "size": 0,
                "mtime": rep.get("updated_at") or rep.get("created_at") or 0,
                "kind": "saved",
                "report_id": rep["id"],
                "summary": rep.get("summary") or "",
            }
        )
    return {"reports": out}


# ── Term explanation cache ──

# 256-entry LRU cap so long-running processes don't grow unbounded.
# lru-dict is thread-safe; O(1) get/set.

_term_cache: LRU = LRU(256)


@app.post("/api/explain-term")
async def api_explain_term(request: Request):
    """Generate a brief AI explanation for a financial/professional term."""
    from .models import ExplainTermRequest, validation_error_message

    u = require_auth(request)
    # Rate limit: 10 req/min per user to protect the 15s subprocess timeout.
    if not check_rate_limit(f"explain:{u}", RATE_EXPLAIN_PER_MIN, 60):
        return JSONResponse(
            {"error": "请求太频繁，请稍后再试"}, status_code=429, headers={"Retry-After": "60"}
        )
    body = await request.json()
    try:
        req = ExplainTermRequest(**(body or {}))
    except Exception as e:
        raise HTTPException(422, validation_error_message(e))
    term = (req.term or "").strip()
    context = (req.context or "").strip()[:200]
    if not term or len(term) > 80:
        raise HTTPException(400, "无效术语")

    # Context can be private. A shared term must never return another account's
    # context-derived answer, and different contexts are different requests.
    cache_key = (u, term, context)
    if cache_key in _term_cache:
        return {"term": term, "explanation": _term_cache[cache_key], "cached": True}

    prompt = f'请用1-2句中文简练解释金融/商业术语"{term}"。'
    if context:
        prompt += f"上下文：{context}。请结合上下文解释。"
    prompt += "只返回解释内容，不加任何前缀。"

    try:
        from .services.auxiliary_model import run_text_async

        explanation = await run_text_async(prompt, timeout=15)
        # Filter out common prefixes Claude adds
        for prefix in ["好的", "以下是", "这是", "解释：", "答案："]:
            if explanation.startswith(prefix):
                explanation = explanation[len(prefix) :].strip()
        if explanation:
            _term_cache[cache_key] = explanation
            return {"term": term, "explanation": explanation, "cached": False}
    except TimeoutError:
        pass
    except Exception:
        pass

    # Fallback: return a generic explanation hint
    return {"term": term, "explanation": f"暂无可用的AI解释，建议搜索「{term}」了解更多。", "cached": False}


# ── Astock Analysis Engine ──


@app.post("/api/analysis/start")
async def api_analysis_start(request: Request):
    """Launch an astock analysis for the selected ticker."""
    u = require_auth(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    ticker = (body.get("ticker") or "").strip()
    name = (body.get("name") or "").strip()
    mode = (body.get("mode") or "fast").strip().lower()
    force_refresh = bool(body.get("force_refresh"))
    if not ticker:
        return JSONResponse({"error": "请提供股票代码"}, status_code=400)
    try:
        aid = await start_analysis(ticker, name, username=u, mode=mode, force_refresh=force_refresh)
        entry = _store.get(aid, {})
        return {
            "analysis_id": aid,
            "status": "cached" if entry.get("cached") else "started",
            "mode": entry.get("mode", mode),
            "cached": bool(entry.get("cached")),
            "selected_analysts": entry.get("selected_analysts") or [],
        }
    except Exception as e:
        logging.getLogger("prism").error("Analysis start failed (%s)", type(e).__name__)
        return JSONResponse(
            {"error": "研究启动失败，请联系管理员核查引擎配置", "code": "engine_unavailable"}, status_code=500
        )


@app.get("/api/analysis/{analysis_id}/stream")
async def api_analysis_stream(analysis_id: str, request: Request):
    """SSE stream of analysis progress events."""
    u = require_auth(request)
    entry = _store.get(analysis_id)
    record = get_analysis_record(analysis_id, u)
    if not record:
        return StreamingResponse(
            iter([_analysis_sse({"type": "error", "message": "分析不存在或无权访问"})]),
            media_type="text/event-stream",
        )

    async def _generate():
        if not entry or record["status"] in {"done", "error"}:
            if record["status"] == "done":
                yield _analysis_sse({"type": "done", **record})
            else:
                yield _analysis_sse({"type": "error", "message": record.get("error") or "研究已结束"})
            return
        async for event in stream_events(analysis_id):
            yield event

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/analysis/{analysis_id}/report")
async def api_analysis_report(analysis_id: str, request: Request):
    """Return the final structured analysis report."""
    u = require_auth(request)
    report = get_analysis_record(analysis_id, u)
    if not report:
        return JSONResponse({"error": "分析不存在或无权访问"}, status_code=404)
    if report["status"] in {"starting", "running"}:
        return JSONResponse(report, status_code=202)
    return report


@app.get("/api/reports/{conv_id}/{filename:path}")
async def api_view_report(conv_id: str, filename: str, request: Request):
    """Serve immutable, explicitly owned artifacts; shared skills grant no access."""
    u = require_auth(request, optional=True)
    if not u:
        return RedirectResponse("/login")
    metadata, content = read_report_file(u, conv_id, filename)
    ext = Path(filename).suffix.lower()
    mime = {
        ".html": "text/html",
        ".htm": "text/html",
        ".pdf": "application/pdf",
        ".csv": "text/csv",
        ".md": "text/markdown",
        ".json": "application/json",
    }.get(ext, "application/octet-stream")
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if ext in {".html", ".htm"}:
        headers["Content-Security-Policy"] = REPORT_CSP
    else:
        # Never let browsers interpret markdown/CSV/JSON as application HTML.
        headers["Content-Disposition"] = f'attachment; filename="{metadata["filename"]}"'
    return Response(content, media_type=mime, headers=headers)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server.main:app", host="127.0.0.1", port=8088, reload=True)
