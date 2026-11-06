"""Real SQLite, explicit numeric scope, history visibility and immutable reports."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from server.database import get_db
from server.research import knowledge, ledger
from server.research.history_checks import cross_run_checks


@pytest.fixture
def history(test_user):
    ledger.init_research_db()
    knowledge.init_knowledge_db()
    owner = test_user["username"]
    with get_db() as db:
        db.execute("INSERT INTO users(username,password_hash) VALUES('other-history','synthetic')")
        for cid, who in [("own-history", owner), ("other-history", "other-history")]:
            db.execute("INSERT INTO conversations(id,username) VALUES(?,?)", [cid, who])

    def add(run, value="120", unit="万吨", *, who=owner, **updates):
        now = datetime.now(UTC).isoformat()
        payload = {"run_id": run, "as_of": now, "gaps": [], "status": "complete"}
        claim = {
            "claim_id": "c-" + run,
            "kind": "fact",
            "entities": ["合成材料公司"],
            "statement": "合成年度披露中的设计产能",
            "metric": "设计年产能",
            "accounting_basis": "设计产能",
            "period": "2025",
            "value": value,
            "unit": unit,
            "currency": None,
            "citation_status": "valid",
            "supports": ["p-" + run],
            "quotes": {"p-" + run: "合成产能原文"},
            "limitations": [],
            "gaps": [],
            **updates,
        }
        with get_db() as db:
            db.execute(
                "INSERT INTO research_runs VALUES(?,?,?,?,?)",
                [run, who, "own-history" if who == owner else who, now, json.dumps(payload)],
            )
            db.execute(
                "INSERT INTO research_claims VALUES(?,?,?)", [claim["claim_id"], run, json.dumps(claim)]
            )
        return claim["claim_id"]

    def check(run="current"):
        with get_db() as db:
            return cross_run_checks(db, owner, run)

    return owner, add, check


def test_unit_normalization_and_older_differing_value_before_result_window(history):
    owner, add, check = history
    add("old-different", "130")
    for index in range(230):
        add(f"recent-{index}", "1200000000", "kg")
    add("current", "120")
    result = check()
    assert result["status"] == "conflicts_require_review"
    assert result["historical_results_truncated"] is False
    assert result["historical_candidates_scanned"] >= 231
    group = result["conflicts"][0]
    assert set(group["claim_ids"]) == {"c-current", "c-old-different"}
    assert {v["normalized_value"] for v in group["values"]} == {"1200000", "1300000"}
    assert group["dimension"] == "mass_t" and group["period"]["start"] == "2025-01-01"
    assert result["semantic_verification"] is False and result["independent_sources_verified"] is False
    assert ledger.read_research_run(owner, "current")["history_checks"]["conflicts"] == result["conflicts"]


@pytest.mark.parametrize(
    "changes",
    [
        {"period": "2026"},
        {"metric": "实际产量"},
        {"accounting_basis": "有效产能"},
        {"entities": ["另一合成公司"]},
        {"entities": ["合成材料公司", "下游客户"]},
        {"unit": "万吨/年"},
        {"kind": "hypothesis"},
        {"citation_status": "gaps"},
        {"quotes": {}},
        {"period": "最近"},
    ],
)
def test_different_or_missing_scopes_never_become_numeric_conflicts(history, changes):
    _, add, check = history
    add("old", "130", **changes)
    add("current")
    assert check()["conflicts"] == []


def test_equal_values_are_not_independent_verification(history):
    _, add, check = history
    add("old", "1200000000", "kg")
    add("current")
    value = check()
    assert value["status"] == "no_numeric_conflict_detected" and value["conflicts"] == []
    assert value["semantic_verification"] is False and value["independent_sources_verified"] is False


@pytest.mark.parametrize("state", ["disputed", "retracted", "expired", "superseded"])
def test_ineligible_historical_claims_are_not_reused(history, state):
    owner, add, check = history
    cid = add("old", "130")
    add("current")
    with get_db() as db:
        db.execute(
            "INSERT INTO knowledge_reviews VALUES(?,?,?,?,?,?,?,?)",
            [
                cid,
                owner,
                1,
                "reviewed" if state in {"expired", "superseded"} else state,
                "synthetic",
                (datetime.now(UTC) - timedelta(days=1)).isoformat() if state == "expired" else None,
                "c-current" if state == "superseded" else None,
                1,
            ],
        )
    assert check()["conflicts"] == []


def test_account_deleted_conversation_and_inactive_owner_boundaries(history, monkeypatch):
    owner, add, check = history
    add("other", "999", who="other-history")
    add("current")
    import server.research.history_checks as module

    original = module.comparison_input
    seen = []

    def compare(claim):
        seen.append(claim["claim_id"])
        return original(claim)

    monkeypatch.setattr(module, "comparison_input", compare)
    assert check()["conflicts"] == [] and "c-other" not in seen
    with get_db() as db:
        with pytest.raises(HTTPException) as other:
            cross_run_checks(db, "other-history", "current")
        assert other.value.status_code == 404
        db.execute("UPDATE conversations SET deleted_at=1 WHERE id='own-history'")
    with pytest.raises(HTTPException) as deleted:
        check()
    assert deleted.value.status_code == 404
    with get_db() as db:
        db.execute("UPDATE conversations SET deleted_at=NULL WHERE id='own-history'")
        db.execute("UPDATE users SET disabled_at=1 WHERE username=?", [owner])
    with pytest.raises(HTTPException):
        check()


def test_more_than_eighty_conflicting_peers_reports_truncation(history):
    _, add, check = history
    for index in range(82):
        add(f"old-{index}", "130")
    add("current")
    result = check()
    assert result["conflicts"] and result["historical_results_truncated"]
    assert result["coverage_incomplete"]
    assert len(result["conflicts"][0]["values"]) == 81


def test_timeout_is_incomplete_and_does_not_rewrite_immutable_reports(history, monkeypatch):
    owner, add, check = history
    add("old", "130")
    add("current")
    with get_db() as db:
        revision = json.dumps({"run_id": "current", "text": "原历史报告，不可改写"})
        db.execute("INSERT INTO research_revisions VALUES(?,?,?)", ["revision", "current", revision])
    import server.research.history_checks as module

    monkeypatch.setattr(module, "SCAN_SECONDS", 0)
    result = check()
    assert result["status"] == "incomplete" and result["coverage_incomplete"]
    assert result["reason"] == "scan_time_limit" and result["conflicts"] == []
    with get_db() as db:
        assert (
            db.execute("SELECT payload FROM research_revisions WHERE run_id='current'").fetchone()[0]
            == revision
        )
        assert db.execute("SELECT count(*) FROM research_claims").fetchone()[0] == 2


def test_deleted_peer_and_ineligible_current_claim_are_excluded(history):
    owner, add, check = history
    add("old", "130")
    add("current")
    with get_db() as db:
        db.execute("INSERT INTO conversations(id,username,deleted_at) VALUES('deleted-peer',?,1)", [owner])
        db.execute("UPDATE research_runs SET conv_id='deleted-peer' WHERE run_id='old'")
    assert check()["conflicts"] == []
    with get_db() as db:
        db.execute("UPDATE research_runs SET conv_id='own-history' WHERE run_id='old'")
        db.execute(
            "INSERT INTO knowledge_reviews VALUES(?,?,?,?,?,?,?,?)",
            ["c-current", owner, 1, "retracted", "synthetic", None, None, 1],
        )
    assert check()["status"] == "no_comparable_inputs"


@pytest.mark.parametrize(
    "malformed", [{"supports": True}, {"supports": [{}]}, {"kind": {}}, {"quotes": {"p-old": True}}]
)
def test_malformed_legacy_metadata_does_not_break_the_live_read(history, malformed):
    _, add, check = history
    add("old", "130", **malformed)
    add("current")
    assert check()["conflicts"] == []


def test_canonical_labels_and_agent_history_read_preserve_original_revision(history):
    owner, add, check = history
    add("old", "130", entities=["合成材料公司"], metric="Ａ产能", period="2025年")
    add("current", metric="a产能")
    with get_db() as db:
        revision = json.dumps({"narrative": "合成历史结论"})
        db.execute("INSERT INTO research_revisions VALUES(?,?,?)", ["revision", "current", revision])
    assert check()["conflicts"]
    from server.agent.knowledge import get

    response = get(owner, "research:current")
    assert response["history_checks"]["status"] == "conflicts_require_review"
    assert response["history_checks"]["semantic_verification"] is False
    with get_db() as db:
        assert (
            db.execute("SELECT payload FROM research_revisions WHERE run_id='current'").fetchone()[0]
            == revision
        )


def test_disabled_owner_cannot_commit_claims_before_history_authorization(history):
    owner, add, _ = history
    add("current")
    with get_db() as db:
        db.execute("UPDATE users SET disabled_at=1 WHERE username=?", [owner])
    with pytest.raises(HTTPException) as denied:
        ledger.record_claims(owner, "current", [{"kind": "hypothesis", "statement": "合成未授权写入"}])
    assert denied.value.status_code == 401
    with get_db() as db:
        assert db.execute("SELECT count(*) FROM research_claims").fetchone()[0] == 1


def test_unreadable_historical_payload_keeps_coverage_incomplete(history):
    _, add, check = history
    add("old", "130", statement="x" * 70000)
    add("current")
    value = check()
    assert value["conflicts"] == [] and value["coverage_incomplete"]
    assert value["historical_unreadable_claims"] == 1


async def test_real_read_routes_return_differences_only_to_the_owner(client, auth_cookies, history):
    owner, add, _ = history
    add("old", "130")
    add("current")
    headers = {"X-Requested-With": "XMLHttpRequest", "X-Prism-Account": owner}
    for path in ("/api/research/runs/current", "/api/knowledge/items/c-current"):
        response = await client.get(path, cookies=auth_cookies, headers=headers)
        assert response.status_code == 200
        assert response.json()["history_checks"]["conflicts"][0]["current_claim_ids"] == ["c-current"]
        from server.auth import create_session

        other = await client.get(
            path,
            cookies={"token": create_session("other-history")},
            headers={"X-Requested-With": "XMLHttpRequest", "X-Prism-Account": "other-history"},
        )
        assert other.status_code == 404 and "c-old" not in other.text
