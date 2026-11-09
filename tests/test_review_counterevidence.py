"""Counterevidence must not silently become positive support or disappear.

The actual owned ledger builds packets; model replies are finite synthetic
inputs, not a claim of real-model semantic acceptance.
"""

import hashlib
import json

import pytest
from test_native_agent_runtime_review import engine as engine

from server.database import get_db
from server.research import knowledge, ledger, model_review
from server.research_context import EvidenceTracker, build_research_context


def packet_for(engine):
    run_id = engine.run["run_id"]
    ledger.create_research_run(
        engine.user,
        engine.conv,
        "合成反证质量样例",
        build_research_context(run_id=run_id, engine="prism-native"),
    )
    tracker, links, quotes = EvidenceTracker(run_id), {}, {}
    for role, excerpt in (
        ("supports", "2025年合成公司X产品已形成量产供货。"),
        ("opposes", "2025年合成公司X产品仍在送样，尚未形成量产供货。"),
    ):
        call = {"id": role, "name": "WebFetch", "input": {"url": "https://www.sse.com.cn/synthetic/" + role}}
        tracker.record_call(call)
        block = {"tool_use_id": role, "content": excerpt}
        refs = [
            {**r, "body_sha256": hashlib.sha256(excerpt.encode()).hexdigest()}
            for r in tracker.record_result(block)
        ]
        passage = ledger.ingest_tool_result(engine.user, run_id, call, block, refs)[0]["passage_ids"][0]
        links[role], quotes[passage] = [passage], excerpt
    ledger.record_claims(
        engine.user,
        run_id,
        [
            {
                "kind": "fact",
                "statement": "2025年合成公司X产品已形成量产供货。",
                "entities": ["合成公司"],
                "metric": "X产品商业阶段",
                "period": "2025",
                "accounting_basis": "公司披露的量产供货",
                "quotes": quotes,
                **links,
            }
        ],
    )
    return model_review.prepare(engine.user, run_id)["packet"]


def reply(packet, *, verdict="supported", roles=("supports",), history_roles=None):
    claim = packet["claims"][0]
    sources = [s for s in claim["sources"] if s["role"] in roles]
    old = claim.get("historical_context", [])
    sources += [s for p in old for s in p["sources"] if s["role"] in (history_roles or ())]
    value = {
        "claim_id": claim["claim_id"],
        "verdict": verdict,
        "reason": "合成复核意见，用于验证引用范围与材料绑定，不认证事实。",
        "evidence_ids": [s["passage_id"] for s in sources],
    }
    if old:
        value.update(history_verdict="consistent", history_claim_ids=[p["claim_id"] for p in old])
    return json.dumps({"packet_id": packet["packet_id"], "assessments": [value]})


@pytest.mark.parametrize("verdict", ["supported", "partly_supported"])
def test_supporting_verdict_cannot_cite_only_declared_opposition(engine, verdict):
    packet = packet_for(engine)
    assert {s["role"] for s in packet["claims"][0]["sources"]} == {"supports", "opposes"}
    with pytest.raises(ValueError, match="evidence binding"):
        model_review.parse(packet, reply(packet, verdict=verdict, roles=("opposes",)))


def test_unreferenced_counterevidence_prevents_whole_support(engine):
    packet = packet_for(engine)
    result = model_review.parse(packet, reply(packet))["assessments"][0]
    assert result["verdict"] == "supported"  # preserve the model's original opinion
    assert result["effective_verdict"] == "partly_supported"
    assert result["scope_incomplete"]


def test_citing_both_directions_does_not_certify_the_claim(engine):
    packet = packet_for(engine)
    result = model_review.parse(packet, reply(packet, roles=("supports", "opposes")))
    assert result["assessments"][0]["effective_verdict"] == "supported"
    assert not result["assessments"][0]["scope_incomplete"]
    model_review.save(
        engine.user, engine.run["run_id"], packet, status="running", model_id="synthetic", call_number=2
    )
    model_review.save(
        engine.user, engine.run["run_id"], packet, model_id="synthetic", call_number=2, **result
    )
    item = knowledge.detail(engine.user, packet["claims"][0]["claim_id"])["item"]
    assert item["state"] == "candidate" and item["semantic_status"] == "unreviewed"


def test_storage_recomputes_an_omitted_opposition_downgrade(engine):
    packet = packet_for(engine)
    result = model_review.parse(packet, reply(packet))
    result["assessments"][0].update(effective_verdict="supported", scope_incomplete=False)
    model_review.save(
        engine.user, engine.run["run_id"], packet, status="running", model_id="synthetic", call_number=2
    )
    stored = model_review.save(
        engine.user, engine.run["run_id"], packet, model_id="synthetic", call_number=2, **result
    )
    assert stored["assessments"][0]["effective_verdict"] == "partly_supported"
    assert stored["assessments"][0]["scope_incomplete"]


def test_opposition_can_still_explain_a_negative_verdict(engine):
    packet = packet_for(engine)
    value = model_review.parse(packet, reply(packet, verdict="not_supported", roles=("opposes",)))
    assert value["assessments"][0]["effective_verdict"] == "not_supported"


@pytest.mark.parametrize(
    "old_method", [None, "0" * 64, hashlib.sha256(model_review.SYSTEM.encode()).hexdigest()]
)
def test_old_rule_advice_cannot_be_reused_as_current_support_or_rewrite_history(engine, old_method):
    packet = packet_for(engine)
    result = model_review.parse(packet, reply(packet, roles=("supports", "opposes")))
    model_review.save(
        engine.user, engine.run["run_id"], packet, status="running", model_id="synthetic", call_number=2
    )
    model_review.save(
        engine.user, engine.run["run_id"], packet, model_id="synthetic", call_number=2, **result
    )
    with get_db() as db:
        original = json.loads(
            db.execute("SELECT payload FROM research_runs WHERE run_id=?", [engine.run["run_id"]]).fetchone()[
                0
            ]
        )
        original["model_review"]["method_sha256"] = old_method
        frozen = json.dumps(original)
        db.execute("UPDATE research_runs SET payload=? WHERE run_id=?", [frozen, engine.run["run_id"]])
    live = model_review.present(original, packet["claims"])
    assert live["status"] == "invalid" and live["reason"] == "review_policy_changed"
    assert live["unreviewed_claims_count"] == 1 and not live["method_current"]
    item = knowledge.detail(engine.user, packet["claims"][0]["claim_id"])["item"]
    assert item["model_review"]["status"] == "invalid"
    assert item["model_review"]["assessment"]["effective_verdict"] == "insufficient_evidence"
    assert "旧复核规则" in item["model_review"]["assessment"]["reason"]
    assert item["state"] == "candidate" and item["semantic_status"] == "unreviewed"
    with get_db() as db:
        assert (
            db.execute("SELECT payload FROM research_runs WHERE run_id=?", [engine.run["run_id"]]).fetchone()[
                0
            ]
            == frozen
        )


@pytest.mark.parametrize("old_roles", [("opposes",), ("supports",)])
def test_historical_support_and_counterevidence_keep_their_roles(engine, old_roles):
    packet = packet_for(engine)
    claim = packet["claims"][0]
    old = {
        "claim_id": "synthetic-historical",
        "context_matches": True,
        "references_omitted": 0,
        "sources": [{**source, "passage_id": "old-" + source["passage_id"]} for source in claim["sources"]],
    }
    claim["historical_context"] = [old]
    packet["historical_scope"] = {"complete": True}
    raw = reply(packet, roles=("supports", "opposes"), history_roles=old_roles)
    if old_roles == ("opposes",):
        with pytest.raises(ValueError, match="history comparison"):
            model_review.parse(packet, raw)
    else:
        value = model_review.parse(packet, raw)["assessments"][0]
        assert value["effective_verdict"] == "partly_supported" and value["scope_incomplete"]
