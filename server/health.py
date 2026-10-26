"""Operational health checks for the admin console."""

from __future__ import annotations

import re
import sys
import time
from typing import Any

from . import config
from .database import get_db
from .release import release_payload


def _release_identity() -> dict[str, str]:
    try:
        release = release_payload()
    except (OSError, UnicodeError):
        release = {}
    revision = release.get("deployed_revision")
    revision = revision.strip() if isinstance(revision, str) else ""
    version = release.get("version")
    version = version.strip() if isinstance(version, str) else ""
    return {
        "version": version if re.fullmatch(r"v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version) else "unknown",
        "deployed_revision": revision.lower() if re.fullmatch(r"[a-fA-F0-9]{40}", revision) else "unknown",
    }


def readiness_payload() -> dict[str, Any]:
    """Public basic readiness evidence; this is not deployment acceptance.

    No CLI, model or provider is invoked. A usable DB can coexist with an
    unknown deployment identity; release validation must separately match the
    observed revision against its immutable candidate.
    """
    return {
        "status": "ready" if _check_db().get("status") == "ok" else "unready",
        **_release_identity(),
        "checked_at": int(time.time()),
        "tz": "Asia/Shanghai",
    }


def _ok(status: str) -> bool:
    return status == "ok"


def _check_db() -> dict[str, Any]:
    started = time.perf_counter()
    try:
        with get_db() as db:
            row = db.execute("SELECT 1 AS ok").fetchone()
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        return {"status": "ok" if row and row["ok"] == 1 else "error", "latency_ms": elapsed_ms}
    except Exception as exc:
        return {"status": "error", "error": exc.__class__.__name__}


def _check_native_core() -> dict[str, Any]:
    """Check local entry points without creating a run, lease or provider transport."""
    try:
        from .agent import provider, runtime

        available = all(
            callable(value)
            for value in (
                runtime.NativeAgent.start,
                runtime.NativeAgent.cancel,
                runtime.get_service,
                provider.stream,
            )
        )
        return {"status": "ok" if available else "error", "engine": "prism-native"}
    except Exception:
        return {"status": "error", "engine": "prism-native", "error": "native_core_unavailable"}


def _check_tool_catalog() -> dict[str, Any]:
    """Inspect the same in-process definitions exposed to the Native runtime."""
    try:
        from .agent.tools import PLATFORM_TOOLS, native_catalog

        rows = native_catalog()
        required = {*PLATFORM_TOOLS, "knowledge_search", "knowledge_get", "source_read"}
        identifiers = [row["id"] for row in rows]
        valid = (
            set(identifiers) == required
            and len(identifiers) == len(required)
            and all(isinstance(row.get("input_schema"), dict) for row in rows)
        )
        return {"status": "ok" if valid else "error", "count": len(rows)}
    except Exception:
        return {"status": "error", "count": 0, "error": "native_tools_unavailable"}


def _model_checks() -> list[dict[str, Any]]:
    try:
        from .agent.provider import _ENDPOINTS
    except ImportError:
        _ENDPOINTS = {}
    models = []
    for model_id, cfg in config.MODELS.items():
        base, key, model = (cfg.get(field) for field in ("base_url", "api_key", "model"))
        # Match the local provider's input contract. This checks configuration,
        # never authentication, connectivity, model quality or provider uptime.
        endpoint_supported = isinstance(base, str) and base.rstrip("/") in _ENDPOINTS
        credential_configured = (
            type(key) is str and 0 < len(key) <= 8192 and all(33 <= ord(c) <= 126 for c in key)
        )
        model_configured = (
            type(model) is str and 0 < len(model) <= 128 and all(32 <= ord(c) < 127 for c in model)
        )
        models.append(
            {
                "id": model_id,
                "name": cfg.get("name", model_id),
                "configured": endpoint_supported and credential_configured and model_configured,
                "credential_configured": credential_configured,
                "endpoint_supported": endpoint_supported,
                "model_configured": model_configured,
            }
        )
    return models


def admin_health_payload() -> dict[str, Any]:
    """Local Native configuration evidence; live integrations need separate checks."""
    db = _check_db()
    core = _check_native_core()
    tools = _check_tool_catalog()
    models = _model_checks()
    configured_count = sum(model["configured"] for model in models)
    providers = {
        "status": "ok" if configured_count else "missing",
        "configured_models": configured_count,
        "model_count": len(models),
        "live_checked": False,
    }
    critical = [
        _ok(db.get("status", "")),
        _ok(core.get("status", "")),
        _ok(tools.get("status", "")),
        _ok(providers["status"]),
    ]
    return {
        "ok": all(critical),
        "checked_at": int(time.time()),
        **_release_identity(),
        "scope": "local_configuration",
        "runtime": {
            "python": ".".join(str(part) for part in sys.version_info[:3]),
            "engine": "prism-native",
        },
        "checks": {
            "database": db,
            "native_core": core,
            "tool_catalog": tools,
            "provider_configuration": providers,
        },
        "models": models,
    }
