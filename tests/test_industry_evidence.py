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
