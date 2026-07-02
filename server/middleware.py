"""Cross-cutting middleware: request ID, error sanitization, logging."""
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from pathlib import Path
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

# ── Per-request context ──
# Available everywhere via ``get_request_id()``. Set by ``RequestIDMiddleware``
# at the start of every request, cleared on completion.
_request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


_SAFE_HEADER_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
)


def _is_safe_header_value(value: str) -> bool:
    """Return True if *value* contains only header-safe characters."""
    return bool(value) and all(c in _SAFE_HEADER_CHARS for c in value)


def get_request_id() -> str:
    """Return the current request's correlation ID, or '-' if none."""
    return _request_id_var.get()


class RequestIDFilter(logging.Filter):
    """Inject the current request ID into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_var.get()
        return True


def configure_logging(log_file: str, level: str = "INFO") -> logging.Logger:
    """Set up logging with rotation, request-ID enrichment, and stdout mirror.

    - File: rotating, 10 MB per file, 5 backups kept.
    - Stdout: line-buffered for container log capture.
    - Each log record carries the active request_id (or '-' if none).
    """
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    fmt = "%(asctime)s [%(levelname)s] [%(request_id)s] %(name)s: %(message)s"
    formatter = logging.Formatter(fmt, datefmt="%H:%M:%S")

    # File handler — rotated. 10 MB × 5 = 50 MB total.
    file_handler = RotatingFileHandler(
        str(log_path), maxBytes=10 * 1024 * 1024, backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()  # Drop any default config
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Install the request-ID filter on every handler
    rid_filter = RequestIDFilter()
    file_handler.addFilter(rid_filter)
    stream_handler.addFilter(rid_filter)

    return logging.getLogger("prism")


def install_request_id_middleware(app: FastAPI) -> None:
    """Attach a middleware that assigns a correlation ID to every request.

    The ID comes from ``X-Request-ID`` if the client supplied one (useful
    for tracing through a reverse proxy), otherwise a fresh UUID4.
    It is echoed back in the response header and added to the
    ``request.state.request_id`` for handler access.
    """

    @app.middleware("http")
    async def _request_id_middleware(request: Request, call_next):
        rid_raw = request.headers.get("X-Request-ID")
        if rid_raw and _is_safe_header_value(rid_raw):
            rid = rid_raw
        else:
            rid = uuid.uuid4().hex[:16]
        token = _request_id_var.set(rid)
        request.state.request_id = rid
        try:
            response = await call_next(request)
        finally:
            _request_id_var.reset(token)
        response.headers["X-Request-ID"] = rid
        return response


def install_error_handlers(app: FastAPI) -> None:
    """Install global exception handlers that sanitize error output.

    Avoid leaking stack traces or internal error messages to clients.
    Log the full error with its request ID; respond with a sanitized
    500/422 payload.
    """
    log = logging.getLogger("prism")

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        log.warning(f"Validation error: {exc.errors()[:3]}")
        # Don't echo the raw Pydantic errors (they may include field values
        # the user sent). Return a stable, minimal shape.
        return JSONResponse(
            status_code=422,
            content={"detail": "Request payload validation failed",
                     "errors": [{"loc": list(e.get("loc", [])), "msg": e.get("msg", "")}
                                for e in exc.errors()[:10]]},
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception(request: Request, exc: Exception):
        rid = getattr(request.state, "request_id", "-")
        log.exception(f"Unhandled exception (request_id={rid}): {exc}")
        # Never echo the exception's repr to the client. Log internally,
        # return a generic message externally.
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error",
                     "request_id": rid},
        )
