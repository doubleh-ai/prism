"""Operational health checks for the admin console."""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import CLAUDE_BIN, DATA_DIR, MODELS, SKILL_FALLBACK_ROOTS, SKILL_MAP, SKILLS_HOME
from .database import get_db
from .release import release_payload
from .services.claude_runner import _resolve_skill_dir


def readiness_payload() -> dict[str, Any]:
    """Public basic readiness evidence; this is not deployment acceptance.

    No CLI, model or provider is invoked. A usable DB can coexist with an
    unknown deployment identity; release validation must separately match the
    observed revision against its immutable candidate.
    """
    try:
        release = release_payload()
    except (OSError, UnicodeError):
        # Unreadable metadata is unknown identity, not evidence of a dead DB.
        release = {}
    revision = release.get("deployed_revision")
    revision = revision.strip() if isinstance(revision, str) else ""
    version = release.get("version")
    version = version.strip() if isinstance(version, str) else ""
    return {
        "status": "ready" if _check_db().get("status") == "ok" else "unready",
        "version": version if re.fullmatch(r"v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version) else "unknown",
        "deployed_revision": revision.lower() if re.fullmatch(r"[a-fA-F0-9]{40}", revision) else "unknown",
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


def _check_claude_cli() -> dict[str, Any]:
    resolved = shutil.which(CLAUDE_BIN) or (CLAUDE_BIN if Path(CLAUDE_BIN).exists() else "")
    result: dict[str, Any] = {"status": "missing", "path": CLAUDE_BIN, "resolved_path": resolved}
    if not resolved:
        return result
    try:
        proc = subprocess.run(
            [resolved, "--version"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        output = (proc.stdout or proc.stderr or "").strip().splitlines()
        result.update(
            {
                "status": "ok" if proc.returncode == 0 else "error",
                "returncode": proc.returncode,
                "version": output[0][:120] if output else "",
            }
        )
    except subprocess.TimeoutExpired:
        result.update({"status": "timeout"})
    except Exception as exc:
        result.update({"status": "error", "error": exc.__class__.__name__})
    return result


def _skill_checks() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for skill_id, info in SKILL_MAP.items():
        dir_name = info["dir"]
        path, checked = _resolve_skill_dir(dir_name)
        claude_md = path / "CLAUDE.md" if path else None
        out.append(
            {
                "id": skill_id,
                "name": info.get("name", skill_id),
                "dir": dir_name,
                "status": "ok" if path and path.exists() else "missing",
                "path": str(path) if path else "",
                "has_claude_md": bool(claude_md and claude_md.exists()),
                "checked_paths": [str(p) for p in checked],
            }
        )
    return out


def _model_checks() -> list[dict[str, Any]]:
    return [
        {"id": model_id, "name": cfg.get("name", model_id), "configured": bool(cfg.get("api_key"))}
        for model_id, cfg in MODELS.items()
    ]


def admin_health_payload() -> dict[str, Any]:
    release = release_payload()
    db = _check_db()
    claude = _check_claude_cli()
    skills = _skill_checks()
    models = _model_checks()
    critical = [
        _ok(db.get("status", "")),
        any(s["id"] == "prism-research" and s["status"] == "ok" and s["has_claude_md"] for s in skills),
        _ok(claude.get("status", "")),
    ]
    return {
        "ok": all(critical),
        "checked_at": int(time.time()),
        "version": release.get("version"),
        "deployed_revision": release.get("deployed_revision", ""),
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "data_dir": str(DATA_DIR),
        },
        "paths": {
            "skills_home": str(SKILLS_HOME),
            "skill_fallback_roots": [str(p) for p in SKILL_FALLBACK_ROOTS],
        },
        "checks": {"database": db, "claude_cli": claude},
        "skills": skills,
        "models": models,
    }
