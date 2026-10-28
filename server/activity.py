"""User activity tracking for admin analytics.

The tracker records coarse product events only. It intentionally avoids
request bodies so prompts, passwords, API keys, and report contents never land
in the activity table.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from .database import record_activity, username_from_request_token

log = logging.getLogger("prism.activity")


@dataclass
class ActivityEventSpec:
    feature: str
    action: str
    target_type: str = ""
    target_id: str = ""
    target_label: str = ""
    meta: dict = field(default_factory=dict)


_SKIP_PREFIXES = (
    "/static/",
    "/api/admin/analytics",
    "/api/admin/diagnostics",
    "/api/diagnostics/",
)

_SKIP_PATHS = {
    "/api/activity",
    "/api/me",
    "/api/models",
    "/api/skills",
    "/api/admin/stats",
    "/api/admin/health",
}


def _part(path: str, idx: int) -> str:
    parts = [p for p in path.split("/") if p]
    return parts[idx] if idx < len(parts) else ""


def classify_activity(request: Request) -> ActivityEventSpec | None:
    """Map an HTTP request to a product-level activity event."""
    path = request.url.path
    method = request.method.upper()
    if not path.startswith("/api/"):
        return None
    if path in _SKIP_PATHS or any(path.startswith(p) for p in _SKIP_PREFIXES):
        return None
    if path == "/api/login":
        return None  # login success/failure is recorded inside auth.login()

    q = request.query_params

    if path == "/api/logout":
        return None  # logout is recorded inside auth.logout() before the session is deleted
    if path == "/api/releases":
        return ActivityEventSpec("release", "open_changelog")

    if path.startswith("/api/admin/users"):
        username = _part(path, 3)
        if path == "/api/admin/users" and method == "GET":
            return ActivityEventSpec("admin", "open_users")
        if path == "/api/admin/users" and method == "POST":
            return ActivityEventSpec("admin", "create_user")
        if path.endswith("/password"):
            return ActivityEventSpec("admin", "reset_password", "user", username)
        if path.endswith("/status"):
            return ActivityEventSpec("admin", "set_user_status", "user", username)
        if method == "DELETE":
            return ActivityEventSpec("admin", "delete_user", "user", username)
        return ActivityEventSpec("admin", method.lower())

    if path == "/api/convs":
        return ActivityEventSpec("conversation", "create" if method == "POST" else "list")
    if path == "/api/trash":
        return ActivityEventSpec("conversation", "open_trash")
    if path == "/api/convs/batch-delete":
        return ActivityEventSpec("conversation", "batch_delete")
    if path.startswith("/api/convs/"):
        conv_id = _part(path, 2)
        if path.endswith("/restore"):
            return ActivityEventSpec("conversation", "restore", "conversation", conv_id)
        if path.endswith("/permanent"):
            return ActivityEventSpec("conversation", "delete_permanent", "conversation", conv_id)
        if path.endswith("/skill"):
            return ActivityEventSpec("conversation", "set_skill", "conversation", conv_id)
        if path.endswith("/reports"):
            return ActivityEventSpec("report", "list_conversation_reports", "conversation", conv_id)
        if path.endswith("/graph"):
            return ActivityEventSpec("graph", "open_conversation_graph", "conversation", conv_id)
        if method == "DELETE":
            return ActivityEventSpec("conversation", "delete", "conversation", conv_id)
        return ActivityEventSpec("conversation", "open", "conversation", conv_id)

    if path.startswith("/api/chat/"):
        return ActivityEventSpec("chat", _part(path, 2) or method.lower())
    if path == "/api/explain-term":
        return ActivityEventSpec("chat", "explain_term")

    if path == "/api/search":
        term = (q.get("q") or "")[:64]
        return ActivityEventSpec("market", "search", "query", term, term)
    if path == "/api/quotes":
        tickers = (q.get("tickers") or "")[:128]
        return ActivityEventSpec("market", "batch_quote", "ticker", tickers)
    if path.startswith("/api/quote/"):
        ticker = _part(path, 2)
        if path.endswith("/kline"):
            return ActivityEventSpec(
                "market", "kline", "ticker", ticker, meta={"period": q.get("period") or "1d"}
            )
        return ActivityEventSpec("market", "quote", "ticker", ticker)
    if path.startswith("/api/fundamentals/"):
        ticker = _part(path, 2)
        return ActivityEventSpec("market", "fundamentals", "ticker", ticker)
    if path == "/api/data-providers":
        return ActivityEventSpec("market", "provider_health")

    if path.startswith("/api/indicators"):
        parts = [p for p in path.split("/") if p]
        ind_id = parts[2] if len(parts) > 2 else ""
        if path.endswith("/translate"):
            return ActivityEventSpec("indicator", "translate")
        if path.endswith("/eval"):
            return ActivityEventSpec("indicator", "eval", "indicator", ind_id)
        if path.endswith("/scan"):
            return ActivityEventSpec("indicator", "scan", "indicator", ind_id)
        if method == "POST":
            return ActivityEventSpec("indicator", "create")
        if method == "PUT":
            return ActivityEventSpec("indicator", "update", "indicator", ind_id)
        if method == "DELETE":
            return ActivityEventSpec("indicator", "delete", "indicator", ind_id)
        return ActivityEventSpec("indicator", "list")

    if path == "/api/analysis/start":
        return ActivityEventSpec("analysis", "start")
    if path.startswith("/api/analysis/"):
        analysis_id = _part(path, 2)
        if path.endswith("/stream"):
            return ActivityEventSpec("analysis", "stream", "analysis", analysis_id)
        if path.endswith("/report"):
            return ActivityEventSpec("analysis", "open_report", "analysis", analysis_id)
        if path.endswith("/save-report"):
            return ActivityEventSpec("report", "save_analysis_report", "analysis", analysis_id)
        return ActivityEventSpec("analysis", "open", "analysis", analysis_id)

    if path == "/api/reports/library":
        return ActivityEventSpec("report", "create" if method == "POST" else "list_library")
    if path.startswith("/api/reports/library/"):
        report_id = _part(path, 3)
        return ActivityEventSpec(
            "report", "delete" if method == "DELETE" else "open_saved", "report", report_id
        )
    if path.startswith("/api/reports/"):
        return ActivityEventSpec("report", "open_file")

    if path == "/api/vibe/plan":
        return ActivityEventSpec("vibe", "create_plan")
    if path == "/api/vibe/plans":
        return ActivityEventSpec("vibe", "list_plans")
    if path.startswith("/api/vibe/plans/"):
        plan_id = _part(path, 3)
        return ActivityEventSpec(
            "vibe", "delete_plan" if method == "DELETE" else "open_plan", "vibe_plan", plan_id
        )

    if path.startswith("/api/industry/"):
        action = _part(path, 2) or "open"
        return ActivityEventSpec("industry", action)

    return ActivityEventSpec("api", method.lower(), "path", path)


class ActivityMiddleware(BaseHTTPMiddleware):
    """Persist coarse authenticated API usage for the admin console."""

    async def dispatch(self, request: Request, call_next):
        spec = classify_activity(request)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            try:
                from .diagnostics import record_diagnostic

                record_diagnostic(
                    username=username_from_request_token(request.cookies.get("token") or "") or "",
                    kind="server_exception",
                    severity="critical",
                    component="api",
                    page=request.url.path,
                    message=f"{exc.__class__.__name__}: {exc}",
                    context={
                        "route": request.url.path,
                        "method": request.method.upper(),
                        "request_id": getattr(request.state, "request_id", ""),
                    },
                )
            except Exception:
                log.debug("server exception diagnostic failed", exc_info=True)
            raise
        expected_policy_rejection = (
            response.status_code == 503
            and response.headers.get("X-Prism-Execution-Policy") == "native-only-analysis-disabled"
            and request.url.path in {"/api/analysis/start", "/api/internal/capabilities/call"}
        )
        if response.status_code >= 500 and not expected_policy_rejection:
            try:
                from .diagnostics import record_diagnostic

                record_diagnostic(
                    username=username_from_request_token(request.cookies.get("token") or "") or "",
                    kind="server_http_5xx",
                    severity="error",
                    component="api",
                    page=request.url.path,
                    message=f"HTTP {response.status_code} from {request.method.upper()} {request.url.path}",
                    context={
                        "route": request.url.path,
                        "method": request.method.upper(),
                        "status": response.status_code,
                        "request_id": response.headers.get("X-Request-ID", ""),
                    },
                )
            except Exception:
                log.debug("HTTP diagnostic record failed", exc_info=True)
        if not spec:
            return response
        try:
            username = username_from_request_token(request.cookies.get("token") or "")
            status = "success" if response.status_code < 400 else "failed"
            record_activity(
                username=username or "",
                feature=spec.feature,
                action=spec.action,
                target_type=spec.target_type,
                target_id=spec.target_id,
                target_label=spec.target_label,
                status=status,
                duration_ms=int((time.perf_counter() - started) * 1000),
                meta={**spec.meta, "method": request.method.upper(), "status_code": response.status_code},
                request=request,
            )
        except Exception:
            log.debug("activity record failed for %s", request.url.path, exc_info=True)
        return response
