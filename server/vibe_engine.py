"""HTTP adapter for the official HKUDS/Vibe-Trading session runtime."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .config import VIBE_ENGINE_TIMEOUT_SECONDS, VIBE_ENGINE_URL


class VibeEngineError(RuntimeError):
    """Raised when the sidecar cannot satisfy an engine request."""


_SECRET_FIELDS = re.compile(
    r"(?i)(?:^|[_-])(?:password|secret|api[ _-]?key|(?:access|refresh|id|auth)[_-]?token|token|cookie|authorization)$"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)([\"']?(?:api[ _-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|token|password|secret|authorization|cookie)[\"']?\s*[=:]\s*[\"']?)[^\s&;\"'<>]+"
)
_SECRET_TEXT = re.compile(
    r"(?:sk-(?:ant-|proj-)?[\w-]{16,}|gh[pousr]_[\w]{20,}|(?i:Bearer|Basic)\s+[^\s\"'<>]+)"
)
_URL_AUTH = re.compile(r"(?i)(https?://)[^\s/@]+:[^\s/@]+@")
_PRIVATE_KEY = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|$)")


def redact_engine_text(value: str) -> str:
    """Remove credential-shaped values without rewriting research prose."""
    value = _PRIVATE_KEY.sub("[redacted]", value)
    value = _URL_AUTH.sub(r"\1[redacted]@", value)
    value = _SECRET_TEXT.sub("[redacted]", value)
    return _SECRET_ASSIGNMENT.sub(r"\1[redacted]", value)


def safe_engine_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "研究引擎返回错误"
                if key in {"error", "exception", "traceback", "stack_trace"} and item
                else safe_engine_payload(item)
            )
            for key, item in value.items()
            if not _SECRET_FIELDS.search(str(key))
        }
    if isinstance(value, list):
        return [safe_engine_payload(item) for item in value]
    return redact_engine_text(value) if isinstance(value, str) else value


def safe_engine_event(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Diagnostics are classifications, never raw provider response bodies."""
    safe = safe_engine_payload(payload)
    if event_type in {"attempt.failed", "attempt.cancelled", "error"}:
        safe = {key: safe[key] for key in ("attempt_id", "linked_attempt_id", "timestamp") if key in safe}
        safe["error"] = "研究任务已取消" if event_type == "attempt.cancelled" else "研究引擎未能完成任务"
    elif event_type == "tool_progress":
        safe["message"] = "研究工具正在处理请求"
        safe.pop("stage", None)
    return safe


def _timeout(read: float | None = None) -> httpx.Timeout:
    return httpx.Timeout(
        connect=10.0,
        read=read if read is not None else 30.0,
        write=30.0,
        pool=30.0,
    )


def _raise_engine_error(exc: Exception) -> VibeEngineError:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        status = status if 100 <= status <= 599 else 502
        return VibeEngineError(f"研究引擎请求失败（HTTP {status}）")
    if isinstance(exc, httpx.TimeoutException):
        return VibeEngineError("研究引擎请求超时")
    if isinstance(exc, httpx.RequestError):
        return VibeEngineError("研究引擎连接失败")
    if isinstance(exc, (ValueError, TypeError)):
        return VibeEngineError("研究引擎返回无效数据")
    return VibeEngineError("研究引擎暂时不可用")


async def engine_health() -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(base_url=VIBE_ENGINE_URL, timeout=_timeout()) as client:
            response = await client.get("/health")
            response.raise_for_status()
            data = safe_engine_payload(response.json())
            return data if isinstance(data, dict) else {"status": "unknown"}
    except Exception as exc:  # pragma: no cover - normalized for callers
        raise _raise_engine_error(exc) from None


async def engine_create_session(title: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(base_url=VIBE_ENGINE_URL, timeout=_timeout()) as client:
            response = await client.post("/sessions", json={"title": title[:160], "config": config or {}})
            response.raise_for_status()
            return safe_engine_payload(response.json())
    except Exception as exc:
        raise _raise_engine_error(exc) from None


async def engine_send_message(session_id: str, content: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(base_url=VIBE_ENGINE_URL, timeout=_timeout()) as client:
            response = await client.post(f"/sessions/{session_id}/messages", json={"content": content})
            response.raise_for_status()
            return safe_engine_payload(response.json())
    except Exception as exc:
        raise _raise_engine_error(exc) from None


async def engine_get_messages(session_id: str, limit: int = 100) -> list[dict[str, Any]]:
    try:
        async with httpx.AsyncClient(base_url=VIBE_ENGINE_URL, timeout=_timeout()) as client:
            response = await client.get(f"/sessions/{session_id}/messages", params={"limit": limit})
            response.raise_for_status()
            data = safe_engine_payload(response.json())
            return data if isinstance(data, list) else []
    except Exception as exc:
        raise _raise_engine_error(exc) from None


async def engine_cancel(session_id: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(base_url=VIBE_ENGINE_URL, timeout=_timeout()) as client:
            response = await client.post(f"/sessions/{session_id}/cancel")
            response.raise_for_status()
            return safe_engine_payload(response.json())
    except Exception as exc:
        raise _raise_engine_error(exc) from None


async def engine_delete_session(session_id: str) -> None:
    try:
        async with httpx.AsyncClient(base_url=VIBE_ENGINE_URL, timeout=_timeout()) as client:
            response = await client.delete(f"/sessions/{session_id}")
            if response.status_code not in {200, 404}:
                response.raise_for_status()
    except Exception as exc:
        raise _raise_engine_error(exc) from None


async def engine_events(session_id: str) -> AsyncIterator[tuple[str, dict[str, Any], str]]:
    """Yield normalized SSE event tuples from one active official session."""
    try:
        timeout = _timeout(float(VIBE_ENGINE_TIMEOUT_SECONDS))
        async with (
            httpx.AsyncClient(base_url=VIBE_ENGINE_URL, timeout=timeout) as client,
            client.stream(
                "GET",
                f"/sessions/{session_id}/events",
                params={"replay": "active"},
            ) as response,
        ):
            response.raise_for_status()
            event_type = "message"
            event_id = ""
            async for line in response.aiter_lines():
                if line.startswith("id:"):
                    event_id = line.split(":", 1)[1].strip()
                elif line.startswith("event:"):
                    event_type = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    raw = line.split(":", 1)[1].strip()
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        payload = {"error": "研究引擎返回无效事件"}
                    if not isinstance(payload, dict):
                        payload = {"value": payload}
                    yield event_type, safe_engine_event(event_type, payload), event_id
                    event_type = "message"
                    event_id = ""
    except Exception as exc:
        raise _raise_engine_error(exc) from None
