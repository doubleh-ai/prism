"""Test the grading mechanism, never label scripted answers as model quality."""

import copy
import json

import pytest
from research_quality_corpus import CASES, evaluate, prepare


def responses(*, blindly_support=False):
    suite = prepare()
    expected = {c["id"]: c for c in CASES}
    replies = []
    for packet in suite["packets"]:
        rows = []
        for claim in packet["claims"]:
            case, old = expected[claim["claim_id"]], claim["historical_context"]
            rows.append(
                {
                    "claim_id": claim["claim_id"],
                    "verdict": "supported" if blindly_support else case["verdict"],
                    "evidence_ids": [s["passage_id"] for entry in (claim, *old) for s in entry["sources"]],
                    "history_verdict": case.get("history_verdict", "not_compared"),
                    "history_claim_ids": [p["claim_id"] for p in old],
                    "reason": "合成评分器机制检查。",
                }
            )
        replies.append(
            {
                "packet_id": packet["packet_id"],
                "text": json.dumps({"packet_id": packet["packet_id"], "assessments": rows}),
            }
        )
    return {"suite_sha256": suite["suite_sha256"], "responses": replies}


def test_annotations_and_expected_answers_never_enter_model_packets():
    suite = prepare()
    assert len(suite["packets"]) == 6 and sum(len(p["claims"]) for p in suite["packets"]) == 12
    raw = json.dumps(suite["packets"], ensure_ascii=False)
    assert all(c["rationale"] not in raw for c in CASES)
    assert not any('"' + key + '"' in raw for key in ("verdict", "effective_verdict", "rationale"))
    assert all(p["scope"] == "synthetic_quality_fixture" for p in suite["packets"])


def test_exact_annotations_pass_only_the_fixture_without_claiming_model_or_production():
    value = evaluate(responses())
    assert value["passed_this_fixture"] and value["correct"] == 12
    assert (
        value["false_support_count"] == value["protocol_failures"] == value["model_calls_by_evaluator"] == 0
    )
    assert not value["provider_identity_verified"] and not value["production_verified"]
    assert not value["independent_fact_verification"]


def test_an_overconfident_reviewer_fails_despite_well_formed_replies_and_real_ids():
    value = evaluate(responses(blindly_support=True))
    assert not value["passed_this_fixture"] and value["false_support_count"] == 8
    assert value["protocol_failures"] == 0


@pytest.mark.parametrize("change", ["suite", "duplicate", "unknown", "extra_authority"])
def test_changed_scope_or_packet_authority_is_rejected(change):
    value = responses()
    if change == "suite":
        value["suite_sha256"] = "0" * 64
    elif change == "duplicate":
        value["responses"].append(value["responses"][0])
    elif change == "unknown":
        value["responses"][0]["packet_id"] = "0" * 64
    else:
        value["verified"] = True
    with pytest.raises(ValueError):
        evaluate(value)


def test_incomplete_or_untrusted_model_reply_stays_a_failed_protocol_case():
    value = responses()
    body = json.loads(value["responses"][0]["text"])
    body["verified"] = True
    value["responses"][0]["text"] = json.dumps(body)
    value["responses"].pop()
    scored = evaluate(value)
    assert scored["protocol_failures"] == 2 and scored["correct"] == 8
    assert not scored["passed_this_fixture"]


def test_response_order_does_not_change_score_or_mutate_the_corpus():
    before = copy.deepcopy(CASES)
    value = responses()
    expected = evaluate(value)
    value["responses"].reverse()
    assert evaluate(value) == expected
    assert CASES == before
