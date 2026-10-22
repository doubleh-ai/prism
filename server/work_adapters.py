"""Application adapters for the finite work producers used by Prism."""

from __future__ import annotations

import functools

from fastapi import HTTPException
from starlette.responses import JSONResponse

from .work_lifecycle import LifecycleError, bind_context, get_manager


def async_work(kind, *, block_on_error=False):
    """Keep resource ownership through the original await, including callers outside HTTP."""

    def decorate(function):
        @functools.wraps(function)
        async def wrapped(*args, **kwargs):
            manager = get_manager()
            try:
                lease = manager.admit(kind, persist_required=False)
            except LifecycleError:
                raise HTTPException(503, "服务维护中，请稍后重试；已有任务仍保留") from None
            error, result = None, None
            with lease:
                try:
                    result = await function(*args, **kwargs)
                except BaseException as exc:
                    error = exc
                    if block_on_error:
                        lease.block("owner_failed")
            if error is not None:
                raise error
            return result

        return wrapped

    return decorate


def produces_work(method, path):
    """History and existing question/cancel responses stay usable during the first fence."""
    if not path.startswith("/api/"):
        return False
    if path.startswith("/api/internal/capabilities/"):
        # The call route authenticates and attaches to its existing run lease.
        return False
    if method not in {"GET", "HEAD", "OPTIONS"}:
        return path not in {"/api/login", "/api/logout", "/api/chat/respond", "/api/chat/cancel"}
    return path.startswith(("/api/quote/", "/api/fundamentals/", "/api/indicators/universe/")) or path in {
        "/api/quotes",
        "/api/search",
        "/api/data-providers",
        "/api/indicators/universe",
    }


class WorkAdmissionMiddleware:
    """Pure ASGI scope covers response streaming without BaseHTTPMiddleware task shortcuts."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        manager = get_manager(scope)
        try:
            observation = manager.track_request()
        except LifecycleError:
            return await self.held(scope, receive, send)
        try:
            with bind_context(manager):
                if not produces_work(scope["method"], scope["path"]):
                    return await self.app(scope, receive, send)
                try:
                    lease = manager.admit("request", persist_required=False)
                except LifecycleError:
                    return await self.held(scope, receive, send)
                with lease:
                    return await self.app(scope, receive, send)
        finally:
            observation.release()

    @staticmethod
    async def held(scope, receive, send):
        # Stay outside Activity/auth/readiness code once sealed: even a nominal
        # read can otherwise write an activity event or open a SQLite connection.
        response = JSONResponse(
            {"error": "maintenance_hold", "detail": "服务维护中，请稍后重试；已有任务仍保留"},
            503,
            headers={
                "Cache-Control": "no-store",
                "Retry-After": "60",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
        )
        return await response(scope, receive, send)
