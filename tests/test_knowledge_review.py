"""Review provenance, expiry, correction propagation and account boundaries."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from server.database import get_db
from server.research import knowledge, ledger


@pytest.fixture
def claims():
    ledger.init_research_db()
    knowledge.init_knowledge_db()
    with get_db() as db:
        for owner in ("knowledge-alice", "knowledge-bob"):
            db.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", [owner, "synthetic-only"])
            db.execute("INSERT INTO conversations(id,username) VALUES(?,?)", [owner, owner])
        for run, owner in (
            ("one", "knowledge-alice"),
            ("two", "knowledge-alice"),
            ("other", "knowledge-bob"),
        ):
            value = {"run_id": run, "as_of": datetime.now(UTC).isoformat(), "gaps": []}
            db.execute(
                "INSERT INTO research_runs VALUES(?,?,?,?,?)",
                [run, owner, owner, value["as_of"], json.dumps(value)],
            )
            claim = {
                "claim_id": run,
                "statement": "Synthetic knowledge signal",
                "kind": "fact",
                "entities": ["sample"],
                "supports": ["p-" + run],
                "citation_status": "valid",
                "limitations": [],
            }
            db.execute("INSERT INTO research_claims VALUES(?,?,?)", [run, run, json.dumps(claim)])
            document = {"document_id": "d-" + run, "url": "https://www.sse.com.cn/", "published_at": None}
            passage = {
                "passage_id": "p-" + run,
                "document_id": "d-" + run,
                "excerpt": "合成原文节选",
                "content_scope": "server_extracted_excerpt",
            }
            db.execute(
                "INSERT INTO research_documents VALUES(?,?,?,?)",
                ["d-" + run, run, "synthetic", json.dumps(document)],
            )
            db.execute(
                "INSERT INTO research_passages VALUES(?,?,?,?)",
                ["p-" + run, run, "d-" + run, json.dumps(passage)],
            )


def _review(**values):
    return knowledge.Review(
        expected_version=0,
        state="reviewed",
        note="合成复核",
        valid_until=(datetime.now(UTC) + timedelta(days=7)).isoformat(),
        **values,
    )


def test_owned_reviews_sources_version_and_deletion(claims):
    assert len(knowledge.search("knowledge-alice")["items"]) == 2
    assert len(knowledge.search("knowledge-alice", limit=1)["items"]) == 1
    reviewed = knowledge.review("knowledge-alice", "one", _review())["item"]
    assert reviewed["semantic_status"] == "owner_reviewed" and reviewed["version"] == 1
    assert knowledge.detail("knowledge-alice", "one")["sources"][0]["excerpt"] == "合成原文节选"
    with pytest.raises(HTTPException) as conflict:
        knowledge.review("knowledge-alice", "one", _review())
    assert conflict.value.status_code == 409
    with pytest.raises(HTTPException) as other:
        knowledge.detail("knowledge-bob", "one")
    assert other.value.status_code == 404
    with get_db() as db:
        db.execute("UPDATE conversations SET deleted_at=1 WHERE id='knowledge-alice'")
    assert knowledge.search("knowledge-alice")["items"] == []


def test_correction_propagates_without_rewriting_history_and_cycles_rejected(claims):
    first = knowledge.review("knowledge-alice", "one", _review())["item"]
    knowledge.record_usage("knowledge-alice", "two", [first])
    replacement = knowledge.Review(
        expected_version=1, state="retracted", note="新原文更正", superseded_by="two"
    )
    changed = knowledge.review("knowledge-alice", "one", replacement)["item"]
    assert changed["effective_state"] == "superseded"
    assert len(knowledge.detail("knowledge-alice", "one")["affected_runs"]) == 1
    with get_db() as db:
        notices = knowledge.change_notices(db, "knowledge-alice", "two")
        assert notices[0]["version"] == 2 and notices[0]["review_version"] == 1
        assert (
            json.loads(db.execute("SELECT payload FROM research_claims WHERE claim_id='one'").fetchone()[0])[
                "statement"
            ]
            == "Synthetic knowledge signal"
        )
    cycle = knowledge.Review(expected_version=0, state="retracted", note="循环不得建立", superseded_by="one")
    with pytest.raises(HTTPException) as caught:
        knowledge.review("knowledge-alice", "two", cycle)
    assert caught.value.status_code == 409
    assert [r["id"] for r in knowledge.search("knowledge-alice", reusable_only=True)["items"]] == ["two"]


def test_expiry_and_missing_evidence_never_become_verified_facts(claims):
    knowledge.review("knowledge-alice", "one", _review())
    with get_db() as db:
        db.execute(
            "UPDATE knowledge_reviews SET valid_until=? WHERE claim_id='one'",
            [(datetime.now(UTC) - timedelta(days=1)).isoformat()],
        )
        claim = json.loads(
            db.execute("SELECT payload FROM research_claims WHERE claim_id='two'").fetchone()[0]
        )
        claim["citation_status"] = "partial"
        db.execute("UPDATE research_claims SET payload=? WHERE claim_id='two'", [json.dumps(claim)])
    assert knowledge.detail("knowledge-alice", "one")["item"]["effective_state"] == "expired"
    assert all(r["id"] != "one" for r in knowledge.search("knowledge-alice", reusable_only=True)["items"])
    with pytest.raises(HTTPException) as caught:
        knowledge.review("knowledge-alice", "two", _review())
    assert caught.value.status_code == 409
