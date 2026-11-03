"""Private relationship evidence must not be merged into public seed data."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from server.database import get_db
from server.industry_research import evidence_graph
from server.research import knowledge, ledger


@pytest.fixture
def relations():
    ledger.init_research_db()
    knowledge.init_knowledge_db()
    with get_db() as db:
        for owner in ("chain-alice", "chain-bob"):
            db.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", [owner, "synthetic"])
            db.execute("INSERT INTO conversations(id,username) VALUES(?,?)", [owner, owner])
            run = {"run_id": owner, "as_of": datetime.now(UTC).isoformat(), "gaps": []}
            db.execute(
                "INSERT INTO research_runs VALUES(?,?,?,?,?)",
                [owner, owner, owner, run["as_of"], json.dumps(run)],
            )
            for index, entities in enumerate((("甲矿", "乙厂"), ("乙厂", "丙客户"), ("丙客户", "甲矿"))):
                cid = f"{owner}-{index}"
                claim = {
                    "claim_id": cid,
                    "statement": "合成资料中的供货候选",
                    "kind": "fact",
                    "entities": list(entities),
                    "supports": [cid],
                    "citation_status": "valid",
                    "period": "2026",
                    "limitations": [],
                }
                document = {
                    "document_id": cid,
                    "url": "https://www.sse.com.cn/",
                    "title": "合成公告",
                    "gaps": [],
                }
                passage = {
                    "passage_id": cid,
                    "document_id": cid,
                    "excerpt": "合成公告节选",
                    "content_scope": "server_extracted_excerpt",
                }
                relation = {
                    "relation_id": cid,
                    "from_entity": entities[0],
                    "to_entity": entities[1],
                    "product": "合成材料",
                    "mechanism": "供给影响成本与产出",
                    "dimension": "supply",
                    "business_stage": "验证",
                    "valid_period": "2026",
                    "invalidators": ["订单终止"],
                    "supporting_claims": [cid],
                    "opposing_claims": [],
                }
                db.execute("INSERT INTO research_claims VALUES(?,?,?)", [cid, owner, json.dumps(claim)])
                db.execute(
                    "INSERT INTO research_documents VALUES(?,?,?,?)",
                    [cid, owner, cid, json.dumps(document)],
                )
                db.execute(
                    "INSERT INTO research_passages VALUES(?,?,?,?)", [cid, owner, cid, json.dumps(passage)]
                )
                db.execute("INSERT INTO research_relations VALUES(?,?,?)", [cid, owner, json.dumps(relation)])


def test_owned_graph_has_original_sources_and_finite_acyclic_hypothesis_paths(relations):
    result = evidence_graph("chain-alice")
    assert len(result["relations"]) == 3
    assert all(r["relation_id"].startswith("chain-alice") for r in result["relations"])
    assert result["relations"][0]["evidence"][0]["sources"][0]["excerpt"] == "合成公告节选"
    assert result["transmission_paths"]
    for path in result["transmission_paths"]:
        assert len(path["entities"]) == len(set(path["entities"]))
        assert path["status"] == "hypothesis_requires_cross_link_review"
    assert result["bottleneck_status"] != "verified"
    compact = evidence_graph("chain-alice", compact=True)
    assert "excerpt" not in compact["relations"][0]["evidence"][0]["sources"][0]


def _change_relation(ident, **updates):
    with get_db() as db:
        value = json.loads(
            db.execute("SELECT payload FROM research_relations WHERE relation_id=?", [ident]).fetchone()[0]
        )
        value.update(updates)
        db.execute("UPDATE research_relations SET payload=? WHERE relation_id=?", [json.dumps(value), ident])


@pytest.mark.parametrize("dimension", ["substitution", "certification", "cost", "demand"])
def test_nonsupply_relations_do_not_become_material_flow_links(relations, dimension):
    _change_relation("chain-alice-1", dimension=dimension)
    graph = evidence_graph("chain-alice")
    assert len(graph["relations"]) == 3
    assert graph["transmission_paths"]
    assert all("chain-alice-1" not in path["relation_ids"] for path in graph["transmission_paths"])
    assert all(path["commercial_status"] == "not_established" for path in graph["transmission_paths"])
    assert all(path["business_stages"] and path["invalidators"] for path in graph["transmission_paths"])


@pytest.mark.parametrize(
    ("updates", "gap"),
    [
        ({"business_stage": ""}, "business_stage_unknown"),
        ({"valid_period": "长期"}, "period_scope_unresolved"),
        ({"invalidators": []}, "invalidation_missing"),
        ({"gaps": ["citation_checks_incomplete"]}, "citation_checks_incomplete"),
    ],
)
def test_incomplete_evidence_does_not_reenter_paths(relations, updates, gap):
    _change_relation("chain-alice-1", **updates)
    graph = evidence_graph("chain-alice")
    row = next(row for row in graph["relations"] if row["relation_id"] == "chain-alice-1")
    assert gap in row["blocking_gaps"] and row["status"] == "needs_evidence"
    assert all("chain-alice-1" not in path["relation_ids"] for path in graph["transmission_paths"])


def test_collection_brief_uses_existing_tools_and_never_claims_completion(relations):
    graph = evidence_graph("chain-alice", limit=1)
    brief = graph["synthesis"]
    assert brief["coverage_incomplete"] and brief["research_complete"] is False
    assert brief["semantic_verification"] is False and brief["reuse_applicable_evidence_first"]
    assert {d["dimension"] for d in brief["dimensions"]} == {
        "supply",
        "certification",
        "substitution",
        "cost",
        "demand",
    }
    assert "certification" in brief["collection_order"] and "source_read" in brief["tools"]
    assert all(d["checks_required"] and d["next_action"] for d in brief["dimensions"])
    for index in range(3):
        _change_relation(f"chain-alice-{index}", business_stage="")
    graph = evidence_graph("chain-alice")
    supply = next(d for d in graph["dimensions"] if d["dimension"] == "supply")
    assert supply["status"] == "needs_evidence" and supply["eligible_relation_ids"] == []


def test_historical_numeric_difference_blocks_reuse_until_peer_withdrawn(relations):
    with get_db() as db:
        raw = db.execute("SELECT payload FROM research_claims WHERE claim_id='chain-alice-0'").fetchone()[0]
        current = json.loads(raw)
        current.update(
            entities=["甲矿"],
            metric="设计年产能",
            value="120",
            unit="万吨",
            currency=None,
            accounting_basis="设计产能",
            quotes={"chain-alice-0": "合成公告节选"},
        )
        db.execute(
            "UPDATE research_claims SET payload=? WHERE claim_id='chain-alice-0'", [json.dumps(current)]
        )
        peer = {**current, "claim_id": "past-claim", "value": "130"}
        run = {"run_id": "past-run", "as_of": datetime.now(UTC).isoformat(), "gaps": []}
        db.execute(
            "INSERT INTO research_runs VALUES(?,?,?,?,?)",
            ["past-run", "chain-alice", "chain-alice", run["as_of"], json.dumps(run)],
        )
        db.execute("INSERT INTO research_claims VALUES(?,?,?)", ["past-claim", "past-run", json.dumps(peer)])
    graph = evidence_graph("chain-alice")
    row = next(row for row in graph["relations"] if row["relation_id"] == "chain-alice-0")
    assert "historical_numeric_evidence_conflict" in row["blocking_gaps"]
    assert row["history_review"]["conflicting_support_ids"] == ["chain-alice-0"]
    assert all("chain-alice-0" not in path["relation_ids"] for path in graph["transmission_paths"])
    knowledge.review(
        "chain-alice", "past-claim", knowledge.Review(expected_version=0, state="retracted", note="合成纠错")
    )
    row = next(
        row for row in evidence_graph("chain-alice")["relations"] if row["relation_id"] == "chain-alice-0"
    )
    assert "historical_numeric_evidence_conflict" not in row["blocking_gaps"]


def test_withdrawn_support_cannot_form_a_path_and_history_stays_present(relations):
    knowledge.review(
        "chain-alice",
        "chain-alice-0",
        knowledge.Review(expected_version=0, state="retracted", note="合成更正"),
    )
    result = evidence_graph("chain-alice")
    blocked = next(r for r in result["relations"] if r["relation_id"] == "chain-alice-0")
    assert "support_ineligible" in blocked["blocking_gaps"]
    assert not any("chain-alice-0" in p["relation_ids"] for p in result["transmission_paths"])
    assert blocked["evidence"][0]["sources"]


def test_expired_evidence_is_not_reused(relations):
    knowledge.review(
        "chain-alice",
        "chain-alice-0",
        knowledge.Review(
            expected_version=0,
            state="reviewed",
            note="合成复核",
            valid_until=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        ),
    )
    with get_db() as db:
        db.execute(
            "UPDATE knowledge_reviews SET valid_until=? WHERE claim_id='chain-alice-0'",
            [(datetime.now(UTC) - timedelta(days=1)).isoformat()],
        )
    blocked = next(
        r for r in evidence_graph("chain-alice")["relations"] if r["relation_id"] == "chain-alice-0"
    )
    assert blocked["evidence"][0]["effective_state"] == "expired"
    assert blocked["status"] == "needs_evidence"


def test_query_pagination_and_deleted_conversation(relations):
    first = evidence_graph("chain-alice", limit=1)
    second = evidence_graph("chain-alice", limit=1, before=first["next_cursor"])
    assert first["relations"][0]["relation_id"] != second["relations"][0]["relation_id"]
    assert evidence_graph("chain-alice", query="%")["relations"] == []
    assert evidence_graph("chain-alice", query="不存在")["relations"] == []
    with get_db() as db:
        db.execute("UPDATE conversations SET deleted_at=1 WHERE id='chain-alice'")
    assert evidence_graph("chain-alice")["relations"] == []
    assert len(evidence_graph("chain-bob")["relations"]) == 3


async def test_http_route_requires_identity_and_returns_only_owned_sources(client, auth_cookies, relations):
    response = await client.get("/api/industry/research")
    assert response.status_code == 401
    response = await client.get("/api/industry/research", cookies=auth_cookies)
    assert response.status_code == 200 and response.json()["relations"] == []
