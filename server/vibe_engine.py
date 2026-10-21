"""HTTP adapter for the official HKUDS/Vibe-Trading session runtime."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .config import VIBE_ENGINE_TIMEOUT_SECONDS, VIBE_ENGINE_URL


class VibeEngineError(RuntimeError):
    """Raised when the sidecar cannot satisfy an engine request."""


def _timeout(read: float | None = None) -> httpx.Timeout:
    return httpx.Timeout(
        connect=10.0,
        read=read if read is not None else 30.0,
        write=30.0,
        pool=30.0,
    )


def _raise_engine_error(exc: Exception) -> VibeEngineError:
    if isinstance(exc, httpx.HTTPStatusError):
        detail = exc.response.text.strip().replace("\n", " ")[:300]
        return VibeEngineError(f"engine HTTP {exc.response.status_code}: {detail}")
    return VibeEngineError(str(exc)[:300] or exc.__class__.__name__)


async def engine_health() -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(base_url=VIBE_ENGINE_URL, timeout=_timeout()) as client:
            response = await client.get("/health")
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {"status": "unknown"}
    except Exception as exc:  # pragma: no cover - normalized for callers
        raise _raise_engine_error(exc) from exc


async def engine_create_session(title: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(base_url=VIBE_ENGINE_URL, timeout=_timeout()) as client:
            response = await client.post("/sessions", json={"title": title[:160], "config": config or {}})
            response.raise_for_status()
            return response.json()
    except Exception as exc:
        raise _raise_engine_error(exc) from exc


async def engine_send_message(session_id: str, content: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(base_url=VIBE_ENGINE_URL, timeout=_timeout()) as client:
            response = await client.post(f"/sessions/{session_id}/messages", json={"content": content})
            response.raise_for_status()
            return response.json()
    except Exception as exc:
        raise _raise_engine_error(exc) from exc


async def engine_get_messages(session_id: str, limit: int = 100) -> list[dict[str, Any]]:
    try:
        async with httpx.AsyncClient(base_url=VIBE_ENGINE_URL, timeout=_timeout()) as client:
            response = await client.get(f"/sessions/{session_id}/messages", params={"limit": limit})
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, list) else []
    except Exception as exc:
        raise _raise_engine_error(exc) from exc


async def engine_cancel(session_id: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(base_url=VIBE_ENGINE_URL, timeout=_timeout()) as client:
            response = await client.post(f"/sessions/{session_id}/cancel")
            response.raise_for_status()
            return response.json()
    except Exception as exc:
        raise _raise_engine_error(exc) from exc


async def engine_delete_session(session_id: str) -> None:
    try:
        async with httpx.AsyncClient(base_url=VIBE_ENGINE_URL, timeout=_timeout()) as client:
            response = await client.delete(f"/sessions/{session_id}")
            if response.status_code not in {200, 404}:
                response.raise_for_status()
    except Exception as exc:
        raise _raise_engine_error(exc) from exc


async def engine_events(session_id: str) -> AsyncIterator[tuple[str, dict[str, Any], str]]:
    """Yield normalized SSE event tuples from one active official session."""
    try:
        timeout = _timeout(float(VIBE_ENGINE_TIMEOUT_SECONDS))
        async with httpx.AsyncClient(base_url=VIBE_ENGINE_URL, timeout=timeout) as client:
            async with client.stream(
                "GET",
                f"/sessions/{session_id}/events",
                params={"replay": "active"},
            ) as response:
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
                            payload = {"preview": raw[:300]}
                        if not isinstance(payload, dict):
                            payload = {"value": payload}
                        yield event_type, payload, event_id
                        event_type = "message"
                        event_id = ""
    except Exception as exc:
        raise _raise_engine_error(exc) from exc
