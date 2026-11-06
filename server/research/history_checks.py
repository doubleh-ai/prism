"""Live owner-scoped numeric differences, not independent semantic verification.

Filter eligible historical differences before the bounded result window so
unrelated or repeated agreeing claims cannot hide an older conflicting value.
No copied text index, report mutation, model call or shared result cache.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from datetime import UTC, datetime

from fastapi import HTTPException

from ..productivity.store import _active
from .quantities import comparison_input

SCAN_SECONDS = 0.75
MAX_PEERS = 80


def _claim(raw):
    if not isinstance(raw, str) or len(raw) > 65536:
        return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (ValueError, RecursionError):
        return None


def _eligible(row, now):
    if row["state"] in {"disputed", "retracted"} or row["superseded_by"]:
        return False
    if row["valid_until"]:
        try:
            until = datetime.fromisoformat(row["valid_until"])
            return until.tzinfo is not None and until > now
        except (TypeError, ValueError):
            return False
    return True


def cross_run_checks(db, owner, run_id, *, deadline=None):
    """Caller initializes the research/review schema outside any transaction."""
    from .knowledge import _SELECT

    _active(db, owner)
    visible_run = db.execute(
        "SELECT 1 FROM research_runs r WHERE r.run_id=? AND r.username=? "
        "AND (r.conv_id IS NULL OR EXISTS(SELECT 1 FROM conversations conv "
        "WHERE conv.id=r.conv_id AND conv.username=r.username AND conv.deleted_at IS NULL))",
        [run_id, owner],
    ).fetchone()
    if not visible_run:
        raise HTTPException(404, "研究不存在或不可访问")
    current = db.execute(_SELECT + " AND c.run_id=? ORDER BY c.rowid LIMIT 61", [owner, run_id]).fetchall()
    now = datetime.now(UTC)
    groups, unassessed = defaultdict(list), []
    for row in current[:60]:
        claim = _claim(row["payload"])
        if claim is None or not _eligible(row, now):
            unassessed.append({"claim_id": row["claim_id"], "reasons": ["claim_unavailable_or_ineligible"]})
            continue
        if claim.get("value") is None:
            continue
        key, value, reasons = comparison_input(claim)
        if reasons:
            unassessed.append({"claim_id": row["claim_id"], "reasons": reasons})
        else:
            groups[key].append({"claim_id": row["claim_id"], "run_id": run_id, "normalized_value": value})
    result = {
        "schema": "prism.history-checks.v1",
        "scope": "owned_live_explicit_numeric_signatures",
        "semantic_verification": False,
        "independent_sources_verified": False,
        "evaluated_at": now.isoformat().replace("+00:00", "Z"),
        "status": "no_comparable_inputs",
        "conflicts": [],
        "current_comparable_claims": sum(len(rows) for rows in groups.values()),
        "not_comparable": unassessed[:20],
        "not_comparable_count": len(unassessed),
        "details_truncated": len(unassessed) > 20,
        "coverage_incomplete": len(current) > 60,
        "historical_results_truncated": False,
        "historical_candidates_scanned": 0,
        "historical_unreadable_claims": 0,
        "notice": "仅比较本账号仍可用的明确数值口径；差异须回查原文，不自动判定谁对谁错。相同数值或未发现差异不代表独立来源或事实核验。",
    }
    if not groups:
        return result
    current_values = {key: {item["normalized_value"] for item in rows} for key, rows in groups.items()}
    deadline = (
        min(deadline, time.monotonic() + SCAN_SECONDS)
        if deadline is not None
        else time.monotonic() + SCAN_SECONDS
    )
    expired = False

    def differs(raw):
        nonlocal expired
        if time.monotonic() >= deadline:
            expired = True
            raise ValueError("bounded history scan expired")
        result["historical_candidates_scanned"] += 1
        claim = _claim(raw)
        if claim is None:
            result["historical_unreadable_claims"] += 1
            return 0
        key, value, reasons = comparison_input(claim)
        return int(not reasons and key in groups and current_values[key] != {value})

    db.create_function("prism_history_differs", 1, differs)
    try:
        # CROSS JOIN keeps the owner-indexed runs outermost. The function sees
        # claims from these runs only; it cannot inspect another account's data.
        peers = db.execute(
            "SELECT c.rowid AS sequence,c.claim_id,c.run_id,c.payload "
            "FROM research_runs r INDEXED BY idx_research_runs_owner "
            "CROSS JOIN research_claims c INDEXED BY idx_research_claims_run "
            "LEFT JOIN knowledge_reviews v ON v.claim_id=c.claim_id AND v.owner=r.username "
            "WHERE r.username=? AND c.run_id=r.run_id AND c.run_id<>? "
            "AND (r.conv_id IS NULL OR EXISTS(SELECT 1 FROM conversations conv WHERE conv.id=r.conv_id "
            "AND conv.username=r.username AND conv.deleted_at IS NULL)) "
            "AND COALESCE(v.state,'candidate') NOT IN ('disputed','retracted') "
            "AND v.superseded_by IS NULL AND (v.valid_until IS NULL OR julianday(v.valid_until)>julianday(?)) "
            "AND prism_history_differs(c.payload)=1 ORDER BY c.rowid DESC LIMIT ?",
            [owner, run_id, now.isoformat(), MAX_PEERS + 1],
        ).fetchall()
    except sqlite3.OperationalError:
        if not expired:
            raise
        return {**result, "status": "incomplete", "coverage_incomplete": True, "reason": "scan_time_limit"}
    finally:
        db.create_function("prism_history_differs", 1, None)
    historical = defaultdict(list)
    for row in peers[:MAX_PEERS]:
        key, value, _ = comparison_input(_claim(row["payload"]))
        historical[key].append(
            {"claim_id": row["claim_id"], "run_id": row["run_id"], "normalized_value": value}
        )
    conflicts = []
    for key, values in historical.items():
        entity, metric, basis, start, end, dimension, currency = key
        combined = groups[key] + values
        conflicts.append(
            {
                "status": "potential_historical_value_conflict",
                "entity": entity,
                "metric": metric,
                "accounting_basis": basis,
                "period": {"start": start, "end": end},
                "dimension": dimension,
                "currency": currency,
                "current_claim_ids": [v["claim_id"] for v in groups[key]],
                "claim_ids": [v["claim_id"] for v in combined],
                "values": combined,
                "targets": [
                    {"view": "reports", "run_id": rid} for rid in dict.fromkeys(v["run_id"] for v in combined)
                ],
            }
        )
    result.update(
        status="conflicts_require_review" if conflicts else "no_numeric_conflict_detected",
        conflicts=conflicts[:20],
        conflict_groups_count=len(conflicts),
        historical_results_truncated=len(peers) > MAX_PEERS,
        details_truncated=result["details_truncated"] or len(conflicts) > 20,
        coverage_incomplete=result["coverage_incomplete"]
        or bool(result["historical_unreadable_claims"])
        or len(peers) > MAX_PEERS,
    )
    return result
