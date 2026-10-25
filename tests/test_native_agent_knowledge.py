"""Owned synthetic historical records only; temporary DB, no model or network."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from server.agent import knowledge
from server.database import get_db
from server.research.ledger import init_research_db


@pytest.fixture
def owners():
    init_research_db()
    with get_db() as db:
        for user in ("alice", "bob"):
            db.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", [user, "synthetic-only"])
            db.execute("INSERT INTO conversations(id,username) VALUES(?,?)", [user + "-conv", user])


def report(
    identifier,
    *,
    user="alice",
    conv="alice-conv",
    title="合成研究",
    text="SyntheticSignal",
    updated=946684800,
    observed="1999-12-30T09:00:00+08:00",
    run_id=None,
    payload=None,
):
    value = (
        {
            "thesis": text,
            "run_id": run_id,
            "research": {"as_of": observed},
            "semantic_status": "verified",
            "status": "completed",
        }
        if payload is None
        else payload
    )
    with get_db() as db:
        db.execute(
            "INSERT INTO saved_reports(id,username,conv_id,title,report_json,updated_at) VALUES(?,?,?,?,?,?)",
            [identifier, user, conv, title, json.dumps(value), updated],
        )


def research(
    identifier,
    *,
    user="alice",
    conv="alice-conv",
    question="合成研究",
    text="SyntheticSignal",
    created="2000-01-02T09:00:00+08:00",
    observed="1999-12-30T09:00:00+08:00",
    saved="2000-01-03T14:00:00+08:00",
):
    run = {
        "run_id": identifier,
        "question": question,
        "as_of": observed,
        "status": "completed",
        "semantic_status": "verified",
    }
    revision = {"narrative": text, "created_at": saved, "semantic_status": "verified"}
    with get_db() as db:
        db.execute(
            "INSERT INTO research_runs VALUES(?,?,?,?,?)", [identifier, user, conv, created, json.dumps(run)]
        )
        db.execute(
            "INSERT INTO research_revisions VALUES(?,?,?)",
            ["rev-" + identifier, identifier, json.dumps(revision)],
        )


def assert_http(status, function):
    with pytest.raises(HTTPException) as caught:
        function()
    assert caught.value.status_code == status


def test_search_and_exact_get_never_cross_account(owners):
    report("mine", title="SharedSignal", text="Alice-private-synthetic")
    report("other", user="bob", conv="bob-conv", title="SharedSignal", text="Bob-private-synthetic")
    research("mine-research", question="SharedSignal", text="Alice-research")
    research("other-research", user="bob", conv="bob-conv", question="SharedSignal", text="Bob-research")
    result = knowledge.search("alice", "SharedSignal", limit=20)
    assert {row["id"] for row in result["results"]} == {"report:mine", "research:mine-research"}
    assert "Bob-" not in json.dumps(result)
    assert "Alice-private" in knowledge.get("alice", "report:mine")["document"]["text"]
    assert_http(404, lambda: knowledge.get("alice", "report:other"))
    assert_http(404, lambda: knowledge.get("alice", "research:other-research"))


def test_deleted_conversation_hides_report_and_research_but_not_unbound_owned_record(owners):
    report("deleted", title="SharedSignal")
    research("deleted-run", question="SharedSignal")
    report("unbound", conv=None, title="SharedSignal")
    with get_db() as db:
        db.execute("UPDATE conversations SET deleted_at=1 WHERE id='alice-conv'")
    assert {row["id"] for row in knowledge.search("alice", "SharedSignal")["results"]} == {"report:unbound"}
    assert_http(404, lambda: knowledge.get("alice", "report:deleted"))
    assert_http(404, lambda: knowledge.get("alice", "research:deleted-run"))


@pytest.mark.parametrize("operation", ["disable", "delete"])
def test_disabled_or_missing_owner_has_no_history_access(owners, operation):
    report("owned", conv=None)
    research("owned-run", conv=None)
    with get_db() as db:
        if operation == "disable":
            db.execute("UPDATE users SET disabled_at=1 WHERE username='alice'")
        else:
            db.execute("DELETE FROM users WHERE username='alice'")
    assert_http(404, lambda: knowledge.search("alice", "SyntheticSignal"))
    for identifier in ("report:owned", "research:owned-run"):
        assert_http(404, lambda identifier=identifier: knowledge.get("alice", identifier))


def test_history_always_unreviewed_and_times_are_preserved_independently(owners):
    report("historical", text="SyntheticSignal", updated=946684800)
    research("historical-run", text="SyntheticSignal")
    report_doc = knowledge.get("alice", "report:historical")["document"]
    research_doc = knowledge.get("alice", "research:historical-run")["document"]
    for doc in (report_doc, research_doc):
        assert doc["semantic_status"] == "unreviewed"
        assert doc["source_kind"] == "historical_research"
        assert doc["research_observed_at"] == "1999-12-30T09:00:00+08:00"
        assert doc["saved_at"] != doc["research_observed_at"]
    assert report_doc["saved_at"] == datetime.fromtimestamp(946684800, ZoneInfo("Asia/Shanghai")).isoformat()
    assert research_doc["saved_at"] == "2000-01-03T14:00:00+08:00"
    assert "待核验" in knowledge.get("alice", "research:historical-run")["notice"]


def test_unknown_history_times_remain_unknown(owners):
    report("unknown-time", observed=None, updated=None)
    research("unknown-time-run", observed=None, saved=None)
    for identifier in ("report:unknown-time", "research:unknown-time-run"):
        doc = knowledge.get("alice", identifier)["document"]
        assert doc["saved_at"] is None and doc["research_observed_at"] is None


def test_search_excludes_current_run_and_deduplicates_saved_versions(owners):
    research("current", question="SharedSignal")
    report("current-copy", title="SharedSignal", run_id="current")
    research("historical", question="SharedSignal")
    report("historical-copy", title="SharedSignal", run_id="historical")
    result = knowledge.search("alice", "SharedSignal", limit=20, exclude_run_id="current")
    assert len(result["results"]) == 1 and result["results"][0]["run_id"] == "historical"


@pytest.mark.parametrize("kind", ["report", "research"])
def test_exact_owned_id_can_retrieve_beyond_recent_200_search_window(owners, kind):
    if kind == "report":
        report("oldest", title="OnlyOldMarker", text="OldestSource", updated=1)
        for index in range(205):
            report(f"new-{index}", title="NewMarker", text="Recent", updated=1000 + index)
    else:
        research("oldest", question="OnlyOldMarker", text="OldestSource", created="1999-01-01")
        for index in range(205):
            research(f"new-{index}", question="NewMarker", text="Recent", created=f"2000-{index:04d}")
    assert knowledge.search("alice", "OnlyOldMarker", limit=20)["results"] == []
    exact = knowledge.get("alice", kind + ":oldest")["document"]
    assert exact["id"] == kind + ":oldest" and exact["text"] == "OldestSource"


@pytest.mark.parametrize(
    "identifier", ["other:mine", "report:../mine", "research:with/slash", "report:" + "a" * 129]
)
def test_invalid_exact_id_is_not_a_query_escape(owners, identifier):
    assert_http(404, lambda: knowledge.get("alice", identifier))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": ""},
        {"query": " "},
        {"query": "a" * 161},
        {"query": "x", "limit": 0},
        {"query": "x", "limit": 21},
        {"query": "x", "limit": True},
    ],
)
def test_search_argument_bounds_are_strict(owners, kwargs):
    assert_http(422, lambda: knowledge.search("alice", **kwargs))


def test_malformed_saved_json_cannot_create_verified_text_or_fake_time(owners):
    report("non-dict", payload=[{"semantic_status": "verified"}], observed=None, updated=None)
    with get_db() as db:
        db.execute("UPDATE saved_reports SET report_json=? WHERE id='non-dict'", ["{not-json"])
    doc = knowledge.get("alice", "report:non-dict")["document"]
    assert doc["text"] == "" and doc["semantic_status"] == "unreviewed"
    assert doc["research_observed_at"] is None and doc["saved_at"] is None


def test_encoded_legacy_reasoning_never_enters_search_or_exact_knowledge(owners):
    blocks = [
        {"type": "thinking", "thinking": "PrivateReasoningMarker"},
        {"type": "opaque_vendor_block", "text": "PrivateOpaqueMarker"},
        {"type": "text", "text": "PublicResearchMarker"},
    ]
    report(
        "legacy-private", payload={"final_decision": json.dumps(blocks), "reasoning": "PrivateFieldMarker"}
    )
    research("legacy-research", text=json.dumps(blocks, indent=2))
    for identifier in ("report:legacy-private", "research:legacy-research"):
        document = knowledge.get("alice", identifier)["document"]
        assert document["text"] == "PublicResearchMarker"
        assert "Private" not in json.dumps(document)
    assert knowledge.search("alice", "PrivateReasoningMarker")["results"] == []
    assert len(knowledge.search("alice", "PublicResearchMarker")["results"]) == 2
