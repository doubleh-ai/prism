"""Read-only release observations. A green workflow is never proof of deployment."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sqlite3
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
from .database import get_db
from .release import release_payload
from .research.quality_bar import SCENARIOS, classify_scenario, quality_gate, scenario_threshold

router = APIRouter(prefix="/api/admin")
REPOSITORY = os.environ.get("PRISM_GITHUB_REPOSITORY", "doubleh-ai/prism").strip() or "doubleh-ai/prism"
WEB = f"https://github.com/{REPOSITORY}"
API = f"https://api.github.com/repos/{REPOSITORY}"
CACHE_SECONDS = 180  # Two public GETs per refresh; stays below the anonymous quota.
RETRY_SECONDS = 180
RELEASE_STATUS = Path("/var/lib/prism-operations/status.json")
RELEASE_STATUS_OWNER = 0
BACKUP_STATE = Path("/var/backups/prism/state")
BACKUP_RECEIPT_SCHEMA = "prism.backup.receipt.v1"
RESTORE_RECEIPT_SCHEMA = "prism.backup.restore.v1"
OPERATOR_RECORD = os.environ.get("PRISM_OPERATOR_RECORD_URL", f"{WEB}/releases").strip() or f"{WEB}/releases"
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
# Read-only research quality telemetry: one bounded window, no N+1 queries.
RESEARCH_WINDOW_DAYS = 30
_RESEARCH_STATUSES = ("completed", "partial", "failed", "cancelled")
_RESEARCH_BUDGET_REASONS = frozenset(
    {
        "context_characters",
        "model_calls",
        "tool_calls",
        "search_calls",
        "wall_time",
        "output_characters",
    }
)
# Claim verification lives in the claim payload JSON (no schema column); claims that
# predate the field or carry an unknown value count conservatively as unverified.
_RESEARCH_VERIFICATION_LEVELS = frozenset({"verified_source", "secondhand", "inferred", "unverified"})


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


def _backup_fields(data: dict) -> dict | None:
    """Fields shared by last-backup.json; None means the receipt is invalid."""
    name, digest, size = data.get("file"), data.get("sha256"), data.get("bytes")
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(digest, str)
        or not digest
        or type(size) is not int
        or size < 0
    ):
        return None
    receipt = {"file": _text(name, 200), "sha256": _text(digest, 128), "bytes": size}
    for key in ("created_at", "finished_at"):
        stamp = data.get(key)
        if stamp is None:
            continue
        if type(stamp) is not int:
            return None
        receipt[key] = stamp
    cos = data.get("cos")
    if isinstance(cos, dict) and type(cos.get("uploaded")) is bool:
        receipt["cos"] = {"uploaded": cos["uploaded"]}
    return receipt


def _restore_fields(data: dict) -> dict | None:
    """Map the private restore receipt onto the same four safe fields."""
    name = data.get("backup_file")
    digest = data.get("backup_sha256")
    size = data.get("backup_bytes")
    created = data.get("at")
    manifest_verified = data.get("manifest_verified")
    sqlite_integrity = data.get("sqlite_integrity")
    restored_to = data.get("restored_to")
    production_data_restored = data.get("production_data_restored")
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(digest, str)
        or not digest
        or type(size) is not int
        or size < 0
        or type(created) is not int
        or type(manifest_verified) is not bool
        or not isinstance(sqlite_integrity, str)
        or not sqlite_integrity
        or not isinstance(restored_to, str)
        or not restored_to
        or type(production_data_restored) is not bool
    ):
        return None
    return {
        "file": _text(name, 200),
        "sha256": _text(digest, 128),
        "bytes": size,
        "created_at": created,
        "manifest_verified": manifest_verified,
        "sqlite_integrity": _text(sqlite_integrity, 64),
        "restored_to": _text(restored_to, 100),
        "production_data_restored": production_data_restored,
    }


def _backup_receipt(path: Path, kind: str) -> dict:
    """Read one bounded root-owned receipt. Never raises, never trusts a partial file.

    A backup receipt and the restore receipt written by the private executor use
    different schemas; both map to the same bounded fields, tagged by ``kind``.
    """
    missing = {"status": "missing", "path": str(path), "kind": kind}
    try:
        with path.open("rb") as source:
            data = json.loads(source.read(128_001))
    except PermissionError:
        return {"status": "unreadable", "path": str(path), "kind": kind}
    except (OSError, ValueError, TypeError, RecursionError):
        return missing
    if not isinstance(data, dict):
        return missing
    schema = data.get("schema")
    if schema == BACKUP_RECEIPT_SCHEMA:
        fields = _backup_fields(data)
    elif schema == RESTORE_RECEIPT_SCHEMA:
        fields = _restore_fields(data)
    else:
        fields = None
    if fields is None:
        return missing
    return {"status": "ok", "kind": kind, **fields}


def backup_observation() -> dict:
    """Observe the private executor's backup/restore receipts, never modify them."""
    last_backup = _backup_receipt(BACKUP_STATE / "last-backup.json", "backup")
    last_restore = _backup_receipt(BACKUP_STATE / "last-restore.json", "restore")
    statuses = (last_backup["status"], last_restore["status"])
    if statuses == ("ok", "ok"):
        status = "ok"
    elif "unreadable" in statuses:
        status = "unreadable"
    else:
        status = "missing"
    return {"status": status, "last_backup": last_backup, "last_restore": last_restore}


def _research_window_start(now: float | None = None) -> float:
    """Unix cutoff for the admin telemetry window; tests inject a fixed clock."""
    return (time.time() if now is None else now) - RESEARCH_WINDOW_DAYS * 86400


def _research_percentile(samples: list[float], fraction: float) -> float | None:
    if not samples:
        return None
    ordered = sorted(samples)
    return round(ordered[max(0, math.ceil(fraction * len(ordered)) - 1)], 1)


def _research_run_metrics(db, window_start: float) -> tuple[dict, float | None, dict]:
    """One bounded read of run status, completion and latency for the window."""
    rows = db.execute(
        "SELECT status,created_at,finished_at FROM native_agent_runs WHERE created_at>=?",
        [window_start],
    ).fetchall()
    counts = dict.fromkeys(_RESEARCH_STATUSES, 0)
    latencies: list[float] = []
    for row in rows:
        if row["status"] in counts:
            counts[row["status"]] += 1
        created, finished = row["created_at"], row["finished_at"]
        if (
            type(created) in (int, float)
            and type(finished) in (int, float)
            and created >= window_start
            and finished >= created
        ):
            latencies.append(float(finished - created))
    total = len(rows)
    completion_rate = round(counts["completed"] / total, 4) if total else None
    latency = {
        "p50": _research_percentile(latencies, 0.50),
        "p95": _research_percentile(latencies, 0.95),
        "max": round(max(latencies), 1) if latencies else None,
    }
    return {"total": total, **counts}, completion_rate, latency


def _research_partial_reasons(db, window_start: float) -> dict:
    """Budget stops are named on the terminal event; read one row per finished run."""
    rows = db.execute(
        "SELECT e.payload FROM native_agent_events e JOIN native_agent_runs r "
        "ON r.run_id=e.run_id AND e.seq=r.last_seq "
        "WHERE r.created_at>=? AND r.finished_at IS NOT NULL",
        [window_start],
    ).fetchall()
    reasons: dict[str, int] = {}
    for row in rows:
        try:
            event = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict) or event.get("type") != "run_finished":
            continue
        reason = event.get("budget")
        if reason in _RESEARCH_BUDGET_REASONS:
            reasons[reason] = reasons.get(reason, 0) + 1
    return reasons


def _research_run_scenarios(db, cutoff: str, stored: dict[str, str], run_ids: list[str]) -> dict:
    """Scenario per revision run: stored for new runs, replayed from facts for history.

    Revisions written before the scenario field existed are classified from the same
    ledger facts as new runs, so the window stays comparable and a historical run is
    described by the current rule. Both queries are window-bounded, never per run.
    """
    scenarios = dict(stored)
    missing = [run_id for run_id in run_ids if run_id not in scenarios]
    if not missing:
        return scenarios
    wanted = set(missing)
    questions = {
        row[0]: row[1]
        for row in db.execute(
            "SELECT run_id, json_extract(payload,'$.question') FROM research_runs "
            "WHERE datetime(created_at)>=datetime(?)",
            [cutoff],
        )
        if row[0] in wanted
    }
    documents: dict[str, list] = {}
    for row in db.execute(
        "SELECT d.run_id, json_extract(d.payload,'$.source_kind'),"
        "json_extract(d.payload,'$.request.symbol'),"
        "json_extract(d.payload,'$.access_method') "
        "FROM research_documents d JOIN research_runs r ON r.run_id=d.run_id "
        "WHERE datetime(r.created_at)>=datetime(?) ORDER BY d.rowid",
        [cutoff],
    ):
        if row[0] not in wanted:
            continue
        symbol = row[2]
        documents.setdefault(row[0], []).append(
            {
                "source_kind": row[1],
                "access_method": row[3],
                "request": {"symbol": symbol} if isinstance(symbol, str) else {},
            }
        )
    for run_id in missing:
        scenarios[run_id] = classify_scenario(questions.get(run_id), documents.get(run_id, []))["scenario"]
    return scenarios


def _research_report_quality(db, window_start: float) -> dict:
    """Read-only report-number coverage over the 30-day window.

    Newer releases store the server-side reconciliation in the immutable revision
    payload JSON, so the run's report numbers are the supported unit. Runs without a
    usable reconciliation (older revisions, or a degraded check) fall back to the
    per-claim verification levels so the window stays comparable; both paths read
    JSON, never a new column, keeping the three-positional-column claim INSERT and an
    application rollback intact. Legacy or unknown claim levels count as
    ``unverified``. Each run is also placed in its scenario: the scenario persisted
    with the revision when present, otherwise replayed from the same ledger facts.
    The window ratio is the mean of per-run ratios; the scenario breakdown exposes
    each bar, so a failing synthesis is not hidden by a passing single-subject run.
    If the SQLite build has no JSON1, ``json_extract`` raises and the caller degrades
    this item instead of the whole observation.
    """
    cutoff = datetime.fromtimestamp(window_start, UTC).isoformat(timespec="seconds")
    revision_rows = db.execute(
        "SELECT rv.run_id, json_extract(rv.payload,'$.claim_reconciliation.status'),"
        "json_extract(rv.payload,'$.claim_reconciliation.verified_claim_ratio'),"
        "json_extract(rv.payload,'$.claim_reconciliation.unsupported_claims'),"
        "json_extract(rv.payload,'$.scenario') "
        "FROM research_revisions rv JOIN research_runs r ON r.run_id=rv.run_id "
        "WHERE datetime(r.created_at)>=datetime(?) ORDER BY rv.rowid",
        [cutoff],
    ).fetchall()
    reconciled = set()
    ratio_by_run: dict[str, float] = {}
    run_order: list[str] = []
    stored_scenarios: dict[str, str] = {}
    unsupported = 0
    for row in revision_rows:
        run_id = row[0]
        run_order.append(run_id)
        if row[4] in SCENARIOS:
            stored_scenarios[run_id] = row[4]
        if row[1] != "ok":
            continue
        reconciled.add(run_id)
        ratio = row[2]
        if isinstance(ratio, (int, float)) and not isinstance(ratio, bool) and math.isfinite(ratio):
            ratio_by_run[run_id] = float(ratio)
        count = row[3]
        if isinstance(count, int) and not isinstance(count, bool):
            unsupported += count
    claim_rows = db.execute(
        "SELECT c.run_id, COALESCE(json_extract(c.payload,'$.verification'),'unverified'),"
        "COUNT(*) FROM research_claims c JOIN research_runs r ON r.run_id=c.run_id "
        "WHERE datetime(r.created_at)>=datetime(?) GROUP BY c.run_id, 2",
        [cutoff],
    ).fetchall()
    fallback: dict = {}
    for row in claim_rows:
        run_id, verification, count = row[0], row[1], row[2]
        counts = fallback.setdefault(run_id, dict.fromkeys(_RESEARCH_VERIFICATION_LEVELS, 0))
        if verification not in counts:
            verification = "unverified"
        counts[verification] += count
    for run_id, counts in fallback.items():
        if run_id in reconciled:
            continue
        denominator = sum(counts.values())
        if denominator:
            ratio_by_run[run_id] = counts["verified_source"] / denominator
        unsupported += counts["unverified"]
    ratios = list(ratio_by_run.values())
    ratio = round(sum(ratios) / len(ratios), 4) if ratios else None
    scenarios = _research_run_scenarios(db, cutoff, stored_scenarios, run_order)
    breakdown = {}
    for scenario in SCENARIOS:
        members = [run_id for run_id in run_order if scenarios.get(run_id) == scenario]
        values = [ratio_by_run[run_id] for run_id in members if run_id in ratio_by_run]
        average = round(sum(values) / len(values), 4) if values else None
        threshold = scenario_threshold(scenario)
        breakdown[scenario] = {
            "runs": len(members),
            "evaluated_runs": len(values),
            "verified_claim_ratio": average,
            "threshold": threshold,
            "passed": None if average is None else average >= threshold,
        }
    latest_gate = None
    for run_id in reversed(run_order):
        run_scenario = scenarios.get(run_id)
        if run_scenario is None:
            continue
        gate = quality_gate(run_scenario, ratio_by_run.get(run_id))
        latest_gate = gate
        if gate["status"] == "ok":
            break
    return {
        "verified_claim_ratio": ratio,
        "unsupported_claims": unsupported,
        "scenario": latest_gate["scenario"] if latest_gate else None,
        "quality_gate": latest_gate,
        "scenarios": breakdown,
    }


def research_observation(now: float | None = None) -> dict:
    """Read-only 30-day research quality telemetry; every metric degrades alone.

    ``model_calls`` is intentionally omitted: native_agent_runs has no such column and
    the runtime keeps usage counters only inside append-only native_agent_events, so
    the metric is skipped instead of being approximated from unrelated counters.
    Claim verification is read from the claim payload JSON, never from a new column.
    ``verified_claim_ratio`` and ``unsupported_claims`` prefer the report-level
    reconciliation stored in the revision payload and fall back to claim levels.
    ``scenario`` and ``quality_gate`` describe the latest evaluated run; the
    ``scenarios`` breakdown exposes each scenario's own bar over the window.
    """
    window_start = _research_window_start(now)
    degraded = False
    runs: dict = {"status": "unavailable"}
    completion_rate: float | None = None
    latency: dict = {"p50": None, "p95": None, "max": None}
    partial_reasons: dict = {}
    verified_claim_ratio: float | None = None
    unsupported_claims: int | None = None
    claim_verification: dict = {"status": "unavailable"}
    scenario: str | None = None
    latest_gate: dict | None = None
    scenarios: dict = {}
    try:
        with get_db() as db:
            runs, completion_rate, latency = _research_run_metrics(db, window_start)
    except (sqlite3.Error, ValueError, TypeError, OverflowError):
        degraded = True
    try:
        with get_db() as db:
            partial_reasons = _research_partial_reasons(db, window_start)
    except (sqlite3.Error, ValueError, TypeError):
        degraded = True
    try:
        with get_db() as db:
            values = _research_report_quality(db, window_start)
    except (sqlite3.Error, ValueError, TypeError, OverflowError):
        degraded = True
    else:
        verified_claim_ratio = values["verified_claim_ratio"]
        unsupported_claims = values["unsupported_claims"]
        scenario = values["scenario"]
        latest_gate = values["quality_gate"]
        scenarios = values["scenarios"]
        claim_verification = {"status": "ok"}
    return {
        "status": "degraded" if degraded else "ok",
        "window_days": RESEARCH_WINDOW_DAYS,
        "runs": runs,
        "completion_rate": completion_rate,
        "partial_reasons": partial_reasons,
        "latency_seconds": latency,
        "verified_claim_ratio": verified_claim_ratio,
        "unsupported_claims": unsupported_claims,
        "claim_verification": claim_verification,
        "scenario": scenario,
        "quality_gate": latest_gate,
        "scenarios": scenarios,
    }


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
            "reason": "生产变更由外部受控发布流程执行。此接口仅提供只读观察，不具备部署或回滚权限。"
            if connected
            else "尚未读取到可信的生产执行记录；检查与候选结果不代表上线。请核对独立发布记录。",
        },
        "production": production,
        "backup": backup_observation(),
        "research": research_observation(),
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
