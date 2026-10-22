"""Read-only release observations. A green workflow is never proof of deployment."""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .auth import require_admin
from .config import OPERATIONS_ENVIRONMENT
from .release import release_payload

router = APIRouter(prefix="/api/admin")
REPOSITORY = "doubleh-ai/prism"
WEB = f"https://github.com/{REPOSITORY}"
API = f"https://api.github.com/repos/{REPOSITORY}"
CACHE_SECONDS = 180  # Two public GETs per refresh; stays below the anonymous quota.
RETRY_SECONDS = 180
RELEASE_STATUS = Path("/var/lib/prism-operations/status.json")
RELEASE_STATUS_OWNER = 0
OPERATOR_RECORD = f"{WEB}/issues/9"
_SHA = re.compile(r"^[a-fA-F0-9]{40}$")
_WORKFLOW_KINDS = {
    ".github/workflows/ci.yml": "ci",
    ".github/workflows/release-candidate.yml": "candidate",
}
_STATUSES = {"queued", "in_progress", "completed", "waiting", "pending", "requested"}
_CONCLUSIONS = {
    "success",
    "failure",
    "neutral",
    "cancelled",
    "skipped",
    "timed_out",
    "action_required",
    "stale",
    "startup_failure",
}
_RELEASE_STAGES = {
    "inspect",
    "prepare",
    "stage",
    "finish_stage",
    "diagnose_build",
    "quarantine_failed_prepare",
    "integration_astock",
    "integration_cli",
    "integration_live",
    "integration_market",
    "validate_legacy_retirement",
    "validate_legacy_stop",
    "legacy_drain",
    "switch_stopped",
    "preflight",
    "quiesce",
    "switch",
    "verify",
    "verified",
    "cutover_config_written",
    "rollback",
    "resume",
}
_RELEASE_STATUSES = {
    "running",
    "prepared",
    "staged",
    "switched",
    "success",
    "failed",
    "cancelled",
    "rolled_back",
    "pending",
    "stopped",
    "verified",
    "complete",
    "completed",
    "in_progress",
    "started",
}


def _record_time(value) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 0 < value <= time.time() + 300:
            return datetime.fromtimestamp(value, UTC).isoformat(timespec="seconds")
        return None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None and parsed.timestamp() <= time.time() + 300:
            return parsed.isoformat(timespec="seconds")
    except (ValueError, OverflowError):
        pass
    return None


def release_observation() -> dict:
    """Read only the executor's bounded root-owned public projection, never receipts."""
    unknown = {"status": "unavailable", "record": None, "url": OPERATOR_RECORD}
    try:
        fd = os.open(RELEASE_STATUS, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != RELEASE_STATUS_OWNER
                or info.st_mode & 0o022
                or info.st_size > 128_000
            ):
                return unknown
            data = json.loads(source.read(128_001))
        if not isinstance(data, dict) or _sha(data.get("commit")) == "unknown":
            return unknown
        stage, status = data.get("stage"), data.get("status")
        if stage not in _RELEASE_STAGES or status not in _RELEASE_STATUSES:
            return unknown
        updated = _record_time(data.get("updated_at"))
        if updated is None:
            return unknown
        steps = []
        for item in data.get("steps", [])[:100]:
            if (
                isinstance(item, dict)
                and item.get("stage") in _RELEASE_STAGES
                and item.get("status") in _RELEASE_STATUSES
            ):
                steps.append(
                    {"stage": item["stage"], "status": item["status"], "at": _record_time(item.get("at"))}
                )
        record = {
            "release_id": _text(data.get("release_id"), 100),
            "commit": _sha(data.get("commit")),
            "archive_sha256": data.get("archive_sha256")
            if re.fullmatch(r"[a-f0-9]{64}", str(data.get("archive_sha256")))
            else "unknown",
            "previous_commit": _sha(data.get("previous_commit")),
            "recovery_commit": _sha(data.get("recovery_commit")),
            "stage": stage,
            "status": status,
            "outcome": data.get("outcome")
            if data.get("outcome")
            in {"success", "failure", "completed", "failed", "cancelled", "rolled_back", "pending"}
            else "unknown",
            "started_at": _record_time(data.get("started_at")),
            "updated_at": updated,
            "operator": "Mac 受控发布执行器",
            "steps": steps,
            "last_completed_stage": data.get("last_completed_stage")
            if data.get("last_completed_stage") in _RELEASE_STAGES
            else None,
            "cutover_occurred": data.get("cutover_occurred") is True,
            "production_verified": data.get("production_verified") is True
            and stage == "resume"
            and status == "completed",
        }
        age = time.time() - datetime.fromisoformat(updated).timestamp()
        active = status in {"running", "in_progress", "pending"}
        return {"status": "stale" if active and age > 600 else "ok", "record": record, "url": OPERATOR_RECORD}
    except (OSError, ValueError, TypeError, AttributeError):
        return unknown


@dataclass
class Observation:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    checked_at: int | None = None
    next_poll: float = 0
    status: str = "unavailable"
    runs: list[dict] = field(default_factory=list)
    pull_requests: list[dict] = field(default_factory=list)


_observation = Observation()


def _text(value: Any, limit: int = 200) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\x00-\x1f\x7f]", " ", value)[:limit]


def _sha(value: Any) -> str:
    return value.lower() if isinstance(value, str) and _SHA.fullmatch(value) else "unknown"


def _positive_id(value: Any) -> int | None:
    return value if type(value) is int and 0 < value < 10**18 else None


def _normalise_runs(payload: Any) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("workflow_runs"), list):
        raise ValueError("Invalid workflow observation")
    result = []
    for value in payload["workflow_runs"][:30]:
        if not isinstance(value, dict):
            continue
        kind = _WORKFLOW_KINDS.get(str(value.get("path", "")).split("@")[0])
        run_id = _positive_id(value.get("id"))
        if not kind or not run_id:
            continue
        result.append(
            {
                "id": run_id,
                "name": _text(value.get("name"), 100),
                "kind": kind,
                "status": value.get("status") if value.get("status") in _STATUSES else "unknown",
                "conclusion": value.get("conclusion") if value.get("conclusion") in _CONCLUSIONS else None,
                "head_sha": _sha(value.get("head_sha")),
                "branch": _text(value.get("head_branch"), 128),
                "event": _text(value.get("event"), 40),
                "created_at": _text(value.get("created_at"), 40),
                "updated_at": _text(value.get("updated_at"), 40),
                "url": f"{WEB}/actions/runs/{run_id}",
            }
        )
    return result


def _normalise_pulls(payload: Any) -> list[dict]:
    if not isinstance(payload, list):
        raise ValueError("Invalid pull-request observation")
    result = []
    for value in payload[:20]:
        if not isinstance(value, dict) or not (number := _positive_id(value.get("number"))):
            continue
        head = value.get("head") if isinstance(value.get("head"), dict) else {}
        base = value.get("base") if isinstance(value.get("base"), dict) else {}
        result.append(
            {
                "number": number,
                "title": _text(value.get("title"), 200),
                "draft": value.get("draft") is True,
                "state": "open" if value.get("state") == "open" else "unknown",
                "head_sha": _sha(head.get("sha")),
                "head": _text(head.get("ref"), 128),
                "base": _text(base.get("ref"), 128),
                "url": f"{WEB}/pull/{number}",
            }
        )
    return result


async def _read_github() -> tuple[list[dict], list[dict]]:
    # Fixed public endpoints, no inherited proxy, redirects, credential or arbitrary URL.
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(6, connect=3),
        follow_redirects=False,
        trust_env=False,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "Prism-release-observer",
        },
    ) as client:
        runs, pulls = await asyncio.gather(
            client.get(f"{API}/actions/runs", params={"per_page": 30}),
            client.get(f"{API}/pulls", params={"state": "open", "per_page": 20}),
        )
        for response in (runs, pulls):
            response.raise_for_status()
            if len(response.content) > 2_000_000:
                raise ValueError("Observation too large")
        return _normalise_runs(runs.json()), _normalise_pulls(pulls.json())


async def operations_payload() -> dict:
    state = _observation
    if time.monotonic() >= state.next_poll:
        async with state.lock:
            if time.monotonic() >= state.next_poll:
                try:
                    runs, pulls = await _read_github()
                except Exception:
                    # Never convert an API/rate-limit failure to an empty successful snapshot.
                    state.status = "unavailable"
                    state.next_poll = time.monotonic() + RETRY_SECONDS
                else:
                    state.runs, state.pull_requests = runs, pulls
                    state.checked_at = int(time.time())
                    state.status = "ok"
                    state.next_poll = time.monotonic() + CACHE_SECONDS
    release = release_payload()
    production = release_observation()
    record = production["record"]
    connected = production["status"] == "ok" and record is not None
    verified = bool(
        connected
        and record["production_verified"]
        and OPERATIONS_ENVIRONMENT == "production"
        and record["commit"] == _sha(release.get("deployed_revision"))
    )
    return {
        "observed_at": int(time.time()),
        "repository": REPOSITORY,
        "github": {
            "status": state.status,
            "checked_at": state.checked_at,
            "stale": state.status != "ok" and state.checked_at is not None,
            "error": None
            if state.status == "ok"
            else "GitHub 数据暂时不可用，无法确认最新流程状态；请打开 GitHub 核查。",
            "refresh_interval_seconds": CACHE_SECONDS,
            "runs": list(state.runs),
            "pull_requests": list(state.pull_requests),
        },
        "runtime": {
            "version": release.get("version") or "unknown",
            "deployed_revision": _sha(release.get("deployed_revision")),
            "environment": OPERATIONS_ENVIRONMENT,
            "production_verified": verified,
        },
        "delivery": {
            "mode": "controlled_external" if connected else "candidate_only",
            "production_connected": connected,
            "can_deploy": False,
            "can_rollback": False,
            "reason": "生产变更由 Mac 上的受控执行器执行，独立记录在 GitHub。此网页只读观察；关闭独立记录可停止尚未开始的步骤，不能撤销已发生的切换。"
            if connected
            else "尚未读取到可信的生产执行记录；检查与候选结果不代表上线。请打开独立记录核对。",
        },
        "production": production,
        "links": {
            "actions": f"{WEB}/actions",
            "repository": WEB,
            "pull_requests": f"{WEB}/pulls",
            "operator_record": OPERATOR_RECORD,
        },
    }


@router.get("/operations")
async def api_operations(request: Request):
    require_admin(request)
    return JSONResponse(await operations_payload(), headers={"Cache-Control": "no-store"})
