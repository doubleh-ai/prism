"""Research quality telemetry: admin payload section and claim verification in payload JSON."""

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from server import auth
from server import operations as ops
from server.database import get_db
from server.research import create_research_run, init_research_db, read_research_run, record_claims
from server.research_context import build_research_context


@pytest.fixture(autouse=True)
def isolated_observer(monkeypatch):
    monkeypatch.setattr(ops, "_observation", ops.Observation())
    monkeypatch.setattr(ops, "release_payload", lambda: {"version": "v0.26.0", "deployed_revision": ""})


def seed_user(username="alice"):
    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO users(username,password_hash) VALUES(?,?)",
            [username, "synthetic-telemetry-user"],
        )


def native_run(run_id, status, created, finished, budget=None):
    """Insert one native run; a bounded stop also keeps its terminal event at last_seq."""
    with get_db() as db:
        db.execute(
            "INSERT INTO native_agent_runs(run_id,username,question,model_id,workflow,"
            "input_sha256,status,created_at,updated_at,finished_at,last_seq) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [
                run_id,
                "alice",
                "研究问题",
                "mock-model",
                "chat",
                "0" * 64,
                status,
                created,
                created,
                finished,
                1 if budget else 0,
            ],
        )
        if budget is not None:
            event = {"type": "run_finished", "status": status, "error_code": "budget_exceeded"}
            event["budget"] = budget
            db.execute(
                "INSERT INTO native_agent_events(run_id,seq,created_at,payload) VALUES(?,?,?,?)",
                [run_id, 1, created, json.dumps(event, separators=(",", ":"))],
            )


def research_round(run_id, age_days, verifications):
    """Create a research run plus inference claims carrying the given verification levels."""
    seed_user()
    created_at = datetime.now(UTC) - timedelta(days=age_days)
    context = build_research_context(run_id=run_id, now=created_at)
    create_research_run("alice", "", "研究问题", context)
    claims = [
        {
            "kind": "inference",
            "statement": f"合成判断 {run_id}-{index}",
            "limitations": ["合成失效条件"],
            **({"verification": level} if level else {}),
        }
        for index, level in enumerate(verifications)
    ]
    record_claims("alice", run_id, claims)
    # The ledger stamps created_at with the wall clock; move the row into/out of the window.
    with get_db() as db:
        db.execute(
            "UPDATE research_runs SET created_at=? WHERE run_id=?",
            [created_at.isoformat(timespec="seconds"), run_id],
        )


def test_research_section_reports_window_metrics():
    seed_user()
    init_research_db()
    now = time.time()
    native_run("run-completed", "completed", now - 1000, now - 900)
    native_run("run-partial-wall", "partial", now - 2000, now - 1700, "wall_time")
    native_run("run-partial-model", "partial", now - 3000, now - 2400, "model_calls")
    native_run("run-failed", "failed", now - 500, now - 300)
    native_run("run-cancelled", "cancelled", now - 4000, now - 3950)
    native_run("run-old", "completed", now - 40 * 86400, now - 40 * 86400 + 100)
    research_round("round-in", 1, ["verified_source", "verified_source", "secondhand", "unverified"])
    research_round("round-out", 40, ["verified_source", "unverified"])

    section = ops.research_observation(now=now)

    assert section["status"] == "ok"
    assert section["window_days"] == 30
    assert section["runs"] == {"total": 5, "completed": 1, "partial": 2, "failed": 1, "cancelled": 1}
    assert section["completion_rate"] == 0.2
    assert section["partial_reasons"] == {"wall_time": 1, "model_calls": 1}
    assert section["latency_seconds"] == {"p50": 200.0, "p95": 600.0, "max": 600.0}
    assert section["verified_claim_ratio"] == 0.5
    assert section["unsupported_claims"] == 1
    assert section["claim_verification"] == {"status": "ok"}
    # No persisted per-run model-call column exists, so the metric is omitted on purpose.
    assert "model_calls" not in section


def test_research_section_handles_empty_data_and_zero_denominator():
    init_research_db()

    section = ops.research_observation()

    assert section["status"] == "ok"
    assert section["runs"] == {"total": 0, "completed": 0, "partial": 0, "failed": 0, "cancelled": 0}
    assert section["completion_rate"] is None
    assert section["partial_reasons"] == {}
    assert section["latency_seconds"] == {"p50": None, "p95": None, "max": None}
    assert section["verified_claim_ratio"] is None
    assert section["unsupported_claims"] == 0
    assert section["claim_verification"] == {"status": "ok"}


def test_missing_native_tables_degrade_runs_without_raising():
    init_research_db()
    with get_db() as db:
        db.execute("DROP TABLE IF EXISTS native_agent_events")
        db.execute("DROP TABLE IF EXISTS native_agent_runs")

    section = ops.research_observation()

    assert section["status"] == "degraded"
    assert section["runs"] == {"status": "unavailable"}
    assert section["completion_rate"] is None
    assert section["partial_reasons"] == {}
    assert section["latency_seconds"] == {"p50": None, "p95": None, "max": None}
    assert section["claim_verification"] == {"status": "ok"}


def test_missing_research_tables_degrade_claims_without_raising():
    with get_db() as db:
        db.execute("DROP TABLE IF EXISTS research_claims")
        db.execute("DROP TABLE IF EXISTS research_runs")
    now = time.time()
    native_run("run-completed", "completed", now - 100, now - 50)

    section = ops.research_observation(now=now)

    assert section["status"] == "degraded"
    assert section["runs"]["total"] == 1
    assert section["claim_verification"] == {"status": "unavailable"}
    assert section["verified_claim_ratio"] is None
    assert section["unsupported_claims"] is None


def test_missing_json1_degrades_claim_metrics_without_raising(monkeypatch):
    """Old SQLite builds without JSON1 make json_extract raise; only this item degrades."""
    now = time.time()
    init_research_db()
    native_run("run-completed", "completed", now - 100, now - 50)
    real_get_db = ops.get_db

    @contextmanager
    def legacy_get_db():
        with real_get_db() as db:

            class NoJson1:
                def execute(self, sql, params=()):
                    if "json_extract" in sql:
                        raise sqlite3.OperationalError("no such function: json_extract")
                    return db.execute(sql, params)

            yield NoJson1()

    monkeypatch.setattr(ops, "get_db", legacy_get_db)

    section = ops.research_observation(now=now)

    assert section["status"] == "degraded"
    assert section["runs"]["total"] == 1
    assert section["claim_verification"] == {"status": "unavailable"}
    assert section["verified_claim_ratio"] is None
    assert section["unsupported_claims"] is None


async def test_operations_route_includes_research_section(client, test_user, auth_cookies, monkeypatch):
    init_research_db()
    monkeypatch.setattr(auth, "ADMIN_USERS", {"testuser"})
    monkeypatch.setattr(ops, "_read_github", AsyncMock(return_value=([], [])))

    response = await client.get("/api/admin/operations", cookies=auth_cookies)

    assert response.status_code == 200
    body = response.json()
    assert {
        "observed_at",
        "repository",
        "github",
        "runtime",
        "delivery",
        "production",
        "backup",
        "research",
        "links",
    } <= set(body)
    assert body["research"]["status"] == "ok"
    assert body["research"]["window_days"] == 30
    assert body["research"]["claim_verification"] == {"status": "ok"}


def test_legacy_claims_without_verification_count_as_unverified():
    seed_user()
    init_research_db()
    context = build_research_context(run_id="claims-legacy")
    create_research_run("alice", "", "研究问题", context)
    record_claims(
        "alice",
        "claims-legacy",
        [
            {
                "kind": "inference",
                "statement": f"已核验结论 {index}",
                "limitations": ["失效条件"],
                "verification": "verified_source",
            }
            for index in range(2)
        ],
    )
    # Legacy three-value inserts must keep working: verification is only in the payload.
    with get_db() as db:
        db.execute(
            "INSERT INTO research_claims VALUES(?,?,?)",
            [
                "legacy-no-key",
                "claims-legacy",
                json.dumps({"claim_id": "legacy-no-key", "kind": "inference", "statement": "旧结论"}),
            ],
        )
        db.execute(
            "INSERT INTO research_claims VALUES(?,?,?)",
            [
                "legacy-unknown",
                "claims-legacy",
                json.dumps(
                    {
                        "claim_id": "legacy-unknown",
                        "kind": "inference",
                        "statement": "旧结论",
                        "verification": "trusted",
                    }
                ),
            ],
        )

    section = ops.research_observation()

    assert section["claim_verification"] == {"status": "ok"}
    assert section["verified_claim_ratio"] == 0.5
    assert section["unsupported_claims"] == 2


def test_claim_verification_defaults_to_unverified():
    seed_user()
    context = build_research_context(run_id="claims-default")
    create_research_run("alice", "", "研究问题", context)

    claim = record_claims(
        "alice",
        "claims-default",
        [{"kind": "inference", "statement": "默认核验状态", "limitations": ["失效条件"]}],
    )["claims"][0]

    with get_db() as db:
        stored = json.loads(
            db.execute(
                "SELECT payload FROM research_claims WHERE claim_id=?", [claim["claim_id"]]
            ).fetchone()[0]
        )
    assert claim["verification"] == "unverified"
    assert stored["verification"] == "unverified"
    assert read_research_run("alice", "claims-default")["claims"][0]["verification"] == "unverified"


@pytest.mark.parametrize("level", ["verified_source", "secondhand", "inferred"])
def test_claim_verification_persists_explicit_level(level):
    run_id = f"claims-{level}"
    seed_user()
    context = build_research_context(run_id=run_id)
    create_research_run("alice", "", "研究问题", context)

    claim = record_claims(
        "alice",
        run_id,
        [
            {
                "kind": "inference",
                "statement": "显式核验状态",
                "limitations": ["失效条件"],
                "verification": level,
            }
        ],
    )["claims"][0]

    with get_db() as db:
        stored = json.loads(
            db.execute(
                "SELECT payload FROM research_claims WHERE claim_id=?", [claim["claim_id"]]
            ).fetchone()[0]
        )
    assert claim["verification"] == level
    assert stored["verification"] == level


def test_claim_verification_rejects_unknown_level():
    seed_user()
    context = build_research_context(run_id="claims-invalid")
    create_research_run("alice", "", "研究问题", context)

    with pytest.raises(HTTPException) as error:
        record_claims(
            "alice",
            "claims-invalid",
            [
                {
                    "kind": "inference",
                    "statement": "非法核验状态",
                    "limitations": ["失效条件"],
                    "verification": "trusted",
                }
            ],
        )

    assert error.value.status_code == 422
    with get_db() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM research_claims WHERE run_id='claims-invalid'").fetchone()[0]
            == 0
        )


def test_claim_capability_exposes_verification_levels():
    from server.capabilities import ResearchClaim

    assert ResearchClaim(kind="fact", statement="合成事实").verification == "unverified"
    assert ResearchClaim(kind="fact", statement="合成事实", verification="secondhand").verification == (
        "secondhand"
    )
    with pytest.raises(ValueError):
        ResearchClaim(kind="fact", statement="合成事实", verification="trusted")
