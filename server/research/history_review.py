"""Finite, live-owner historical excerpts for the existing model review.

Historical claims are comparison candidates, never independent source evidence.
No questions, conversations, user review notes or copied search index are read.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .quantities import normalized, period_span, quantity

SCAN_SECONDS = 0.5
MAX_CANDIDATES = 80
MAX_PER_CLAIM = 2
MAX_TOTAL = 6
FIELDS = (
    "claim_id",
    "kind",
    "statement",
    "entities",
    "metric",
    "period",
    "accounting_basis",
    "value",
    "unit",
    "currency",
    "limitations",
)
_SELECT = (
    "SELECT c.claim_id,c.run_id,c.payload,"
    "CASE WHEN json_valid(r.payload) THEN json_extract(r.payload,'$.as_of') END AS research_as_of,"
    "v.version,v.state,v.valid_until,v.superseded_by "
    "FROM research_runs r INDEXED BY idx_research_runs_owner "
    "CROSS JOIN research_claims c INDEXED BY idx_research_claims_run "
    "LEFT JOIN knowledge_reviews v ON v.claim_id=c.claim_id AND v.owner=r.username "
    "WHERE r.username=? AND c.run_id=r.run_id "
    "AND (r.conv_id IS NULL OR EXISTS(SELECT 1 FROM conversations conv "
    "WHERE conv.id=r.conv_id AND conv.username=r.username AND conv.deleted_at IS NULL)) "
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _decode(raw):
    if not isinstance(raw, str) or len(raw) > 65536:
        return {}
    try:
        value = json.loads(raw)
        return value if type(value) is dict else {}
    except (ValueError, RecursionError):
        return {}


def _scope(claim):
    entities = claim.get("entities")
    metric, basis = normalized(claim.get("metric"), 80), normalized(claim.get("accounting_basis"), 300)
    period = period_span(claim.get("period"))
    if type(entities) is not list or len(entities) != 1 or not metric or not basis or not period:
        return None
    entity = normalized(entities[0], 120)
    measurement = ("qualitative", None, None)
    if claim.get("value") is not None:
        numeric, issue = quantity(claim["value"], claim.get("unit"), claim.get("currency"))
        if issue:
            return None
        measurement = ("numeric", numeric["dimension"], numeric["currency"])
    return (entity, metric, basis, period, measurement) if entity else None


def _available_at(old, current):
    try:
        observed = datetime.fromisoformat(old)
        if observed.tzinfo is None:
            return False
        cutoff = current.get("information_cutoff") or current["as_of"]
        if len(cutoff) == 10:
            return observed.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat() <= cutoff
        boundary = datetime.fromisoformat(cutoff)
        return boundary.tzinfo is not None and observed <= boundary
    except (KeyError, TypeError, ValueError):
        return False


def _eligible(row, run):
    claim = _decode(row["payload"])
    if (
        claim.get("citation_status") != "valid"
        or claim.get("kind") not in {"fact", "inference", "hypothesis"}
        or row["state"] in {"disputed", "retracted"}
        or row["superseded_by"]
        or not _available_at(row["research_as_of"], run)
        or not _available_at(claim.get("created_at"), run)
    ):
        return False
    if row["valid_until"]:
        try:
            until = datetime.fromisoformat(row["valid_until"])
            if until.tzinfo is None or until <= datetime.now(until.tzinfo):
                return False
        except (TypeError, ValueError):
            return False
    return True


def _sources(db, run_id, claim):
    result = []
    for role in ("supports", "opposes"):
        for pid in claim.get(role, [])[:20]:
            row = db.execute(
                "SELECT payload FROM research_passages WHERE run_id=? AND passage_id=?", [run_id, pid]
            ).fetchone()
            passage = _decode(row[0]) if row else {}
            row = db.execute(
                "SELECT payload FROM research_documents WHERE run_id=? AND document_id=?",
                [run_id, passage.get("document_id")],
            ).fetchone()
            document = _decode(row[0]) if row else {}
            excerpt, digest = passage.get("excerpt"), document.get("retrieved_body_sha256")
            if (
                passage.get("content_scope") != "tool_returned_text"
                or not isinstance(excerpt, str)
                or not 0 < len(excerpt) <= 600
                or hashlib.sha256(excerpt.encode()).hexdigest() != passage.get("excerpt_sha256")
                or not isinstance(digest, str)
                or not re.fullmatch(r"[a-f0-9]{64}", digest)
            ):
                continue
            result.append(
                {
                    "passage_id": pid,
                    "role": role,
                    "excerpt": excerpt,
                    "excerpt_sha256": passage["excerpt_sha256"],
                    "body_sha256": digest,
                    "url": document.get("url"),
                    "published_at": document.get("published_at"),
                    "retrieved_at": document.get("retrieved_at"),
                    "gaps": document.get("gaps", []),
                    "content_scope": "server_fetched_excerpt_only",
                    "original_verified": False,
                }
            )
            break  # One supporting and one opposing excerpt; omission stays explicit.
    return result


def _peer(db, row, current, run):
    old = _decode(row["payload"])
    a, b = _scope(current), _scope(old)
    statement = old.get("statement")
    if (
        not _eligible(row, run)
        or not a
        or not b
        or a[:2] != b[:2]
        or not isinstance(statement, str)
        or not 0 < len(statement) <= 1200
        or normalized(statement, 3000) == normalized(current.get("statement"), 3000)
    ):
        return None
    sources = _sources(db, row["run_id"], old)
    if not any(s["role"] == "supports" for s in sources):
        return None
    value = {k: old.get(k) for k in FIELDS}
    value.update(
        run_id=row["run_id"],
        research_as_of=row["research_as_of"],
        claim_recorded_at=old.get("created_at"),
        review_version=row["version"] or 0,
        review_state=row["state"] or "candidate",
        context_matches=a == b,
        sources=sources,
        references_omitted=sum(len(old.get(role, [])) for role in ("supports", "opposes", "background"))
        - len(sources),
        shared_body=bool(
            {s["body_sha256"] for s in sources} & {s["body_sha256"] for s in current["sources"]}
        ),
        independent_sources_verified=False,
    )
    value["binding_sha256"] = hashlib.sha256(_json(value).encode()).hexdigest()
    return value


def candidates(db, owner, run, selected):
    """Filter related claims before the finite result window; no latest-N index."""
    scopes = {_scope(c)[:2] for c in selected if _scope(c)}
    coverage = {"scope": "owned_explicit_entity_and_metric", "complete": True, "reason": None}
    result = {c["claim_id"]: [] for c in selected}
    if not scopes:
        return result, {**coverage, "complete": False, "reason": "explicit_scope_missing"}
    deadline = time.monotonic() + SCAN_SECONDS
    expired = False

    def matches(raw, as_of):
        nonlocal expired
        if time.monotonic() >= deadline:
            expired = True
            raise ValueError("history scan expired")
        old = _decode(raw)
        scope = _scope(old)
        if (
            not scope
            or scope[:2] not in scopes
            or old.get("citation_status") != "valid"
            or not _available_at(as_of, run)
            or not _available_at(old.get("created_at"), run)
        ):
            return 0
        peers = [c for c in selected if _scope(c) and _scope(c)[:2] == scope[:2]]
        if all(normalized(old.get("statement"), 3000) == normalized(c.get("statement"), 3000) for c in peers):
            return 0
        return 2 if any(_scope(c) == scope for c in peers) else 1

    db.create_function("prism_review_related", 2, matches)
    try:
        rows = db.execute(
            _SELECT + " AND c.run_id<>? AND COALESCE(v.state,'candidate') NOT IN ('disputed','retracted') "
            "AND v.superseded_by IS NULL AND (v.valid_until IS NULL OR julianday(v.valid_until)>julianday('now')) "
            "AND prism_review_related(c.payload,research_as_of)>0 ORDER BY prism_review_related(c.payload,research_as_of) DESC,c.rowid DESC LIMIT ?",
            [owner, run["run_id"], MAX_CANDIDATES + 1],
        ).fetchall()
    except sqlite3.OperationalError:
        if not expired:
            raise
        return result, {**coverage, "complete": False, "reason": "scan_time_limit"}
    finally:
        db.create_function("prism_review_related", 2, None)
    coverage["complete"] = len(rows) <= MAX_CANDIDATES and all(_scope(c) for c in selected)
    for claim in selected:
        seen = set()
        for row in rows[:MAX_CANDIDATES]:
            if time.monotonic() >= deadline:
                coverage.update(complete=False, reason="scan_time_limit")
                break
            peer = _peer(db, row, claim, run)
            if peer is None:
                continue
            key = (peer["statement"], tuple(s["body_sha256"] for s in peer["sources"]))
            if key in seen:
                continue
            seen.add(key)
            result[claim["claim_id"]].append(peer)
        # Same-period/basis differences precede changes of period or definition.
        result[claim["claim_id"]].sort(key=lambda p: not p["context_matches"])
        if any(p["context_matches"] for p in result[claim["claim_id"]]):
            different = [p for p in result[claim["claim_id"]] if not p["context_matches"]]
            if different:
                coverage["complete"] = False
            result[claim["claim_id"]] = [p for p in result[claim["claim_id"]] if p["context_matches"]]
        if len(result[claim["claim_id"]]) > MAX_PER_CLAIM:
            coverage["complete"] = False
        result[claim["claim_id"]] = result[claim["claim_id"]][:MAX_PER_CLAIM]
    return result, coverage


def current(db, owner, run, selected):
    """Re-read exact owner/source/review bindings before use or committing advice."""
    for claim in selected:
        for old in claim.get("historical_context", []):
            row = db.execute(
                _SELECT + " AND c.claim_id=? AND c.run_id<>?", [owner, old["claim_id"], run["run_id"]]
            ).fetchone()
            fresh = _peer(db, row, claim, run) if row else None
            if fresh is None or fresh["binding_sha256"] != old.get("binding_sha256"):
                return False
    return True
