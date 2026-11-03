"""Explicit quantity scope, arithmetic precision and evidence ownership."""

import shutil
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from server.capabilities import ClaimsArgs, RelationsArgs
from server.database import get_db
from server.research import ledger
from server.research.quantities import audit_claims, path_checks, period_span, quantity
from server.research_context import EvidenceTracker, build_research_context


def candidate(cid, value="120", unit="万吨", **changes):
    return {
        "claim_id": cid,
        "kind": "fact",
        "value": value,
        "unit": unit,
        "currency": None,
        "entities": ["甲公司"],
        "metric": "设计年产能",
        "accounting_basis": "设计产能",
        "period": "2025",
        "supports": ["passage"],
        "quotes": {"passage": "合成原文"},
        "citation_status": "valid",
        **changes,
    }


@pytest.mark.parametrize(
    "text,start,end",
    [
        ("2026", "2026-01-01", "2026-12-31"),
        ("2024Q1", "2024-01-01", "2024-03-31"),
        ("2026年第一季度", "2026-01-01", "2026-03-31"),
        ("2026H2", "2026-07-01", "2026-12-31"),
        ("2026年上半年", "2026-01-01", "2026-06-30"),
        ("2026-01-01/2026-03-31", "2026-01-01", "2026-03-31"),
        ("2026-11-03", "2026-11-03", "2026-11-03"),
    ],
)
def test_only_explicit_calendar_periods_are_normalized(text, start, end):
    assert period_span(text) == (date.fromisoformat(start), date.fromisoformat(end))


@pytest.mark.parametrize(
    "text",
    [None, "", "最近", "FY2026Q1", "2026-02-30", "2026Q5", "2026-12-31/2026-01-01", "2200", "2026年至今"],
)
def test_unknown_periods_are_not_filled_from_current_date(text):
    assert period_span(text) is None


@pytest.mark.parametrize(
    "value,unit,currency,expected,dimension",
    [
        ("1.25", "亿元", "CNY", "125000000", "amount"),
        ("12500", "万元", "CNY", "125000000", "amount"),
        ("10", "元/千克", "CNY", "10000", "amount_per_t"),
        ("120", "万吨", None, "1200000", "mass_t"),
        ("1200000000", "kg", None, "1200000", "mass_t"),
        ("50", "%", None, "0.5", "ratio"),
        ("5000", "bps", None, "0.5", "ratio"),
        ("-0", "%", None, "0", "ratio"),
    ],
)
def test_exact_explicit_unit_conversion_does_not_guess_fx(value, unit, currency, expected, dimension):
    result, error = quantity(value, unit, currency)
    assert error is None and result == {"value": expected, "dimension": dimension, "currency": currency}


@pytest.mark.parametrize(
    "value,unit,currency",
    [
        (True, "吨", None),
        ("1e999999999", "吨", None),
        ("NaN", "吨", None),
        ("1,2", "元", "CNY"),
        ("1", "亿元", None),
        ("1", "元", "JPY"),
        ("1", "吨", "CNY"),
        ("1", "箱", None),
        ("9" * 101, "吨", None),
    ],
)
def test_unsafe_or_underspecified_numbers_remain_unassessed(value, unit, currency):
    assert quantity(value, unit, currency)[0] is None


def test_only_comparable_scope_can_create_a_potential_conflict():
    a, b = candidate("a"), candidate("b", "130")
    checked = audit_claims([a, b])
    assert checked["status"] == "conflicts_require_review"
    assert checked["conflicts"][0]["claim_ids"] == ["a", "b"]
    assert [v["normalized_value"] for v in checked["conflicts"][0]["values"]] == ["1200000", "1300000"]
    assert checked["semantic_verification"] is checked["independent_sources_verified"] is False
    for key, value in {
        "metric": "实际产量",
        "accounting_basis": "权益产能",
        "entities": ["乙公司"],
        "period": "2026",
        "unit": "片",
    }.items():
        assert audit_claims([a, {**b, key: value}])["conflicts"] == []


def test_equivalent_units_and_date_ranges_are_not_conflicting():
    rows = [candidate("a", period="2026Q1"), candidate("b", "1200000", "吨", period="2026-01-01/2026-03-31")]
    result = audit_claims(rows)
    assert result["status"] == "bounded_checks_complete" and not result["conflicts"]
    assert result["comparable_claims"] == 2
    assert (
        audit_claims([candidate("a", "1", "元", currency="CNY"), candidate("b", "2", "元", currency="USD")])[
            "conflicts"
        ]
        == []
    )


def test_ineligible_missing_scope_and_withdrawn_claims_do_not_count_as_evidence():
    a, b = candidate("a"), candidate("b", "130")
    for change in (
        {"quotes": {}},
        {"metric": None},
        {"accounting_basis": None},
        {"entities": []},
        {"citation_status": "partial"},
        {"period": "未知"},
    ):
        report = audit_claims([a, {**b, **change}])
        assert not report["conflicts"] and report["not_comparable_count"]
    assert not audit_claims([a, b], excluded={"b"})["conflicts"]


def test_paths_have_actual_period_and_conversion_checks():
    rows = [
        {"to_entity": "乙厂", "product": "硅片", "valid_period": "2026H1"},
        {"to_entity": "丙客户", "product": "电池片", "valid_period": "2026Q4"},
    ]
    checked = path_checks(rows)
    assert checked["period"]["status"] == "nonoverlap"
    assert checked["products"][0]["status"] == "conversion_evidence_required"
    rows[1]["valid_period"] = "2026Q1"
    assert path_checks(rows)["period"] == {"status": "overlap", "start": "2026-01-01", "end": "2026-03-31"}
    rows[1]["valid_period"] = "待核对"
    assert path_checks(rows)["period"]["status"] == "unknown"
    assert path_checks([])["period"]["status"] == "unknown"


def test_native_tool_schemas_explain_nested_claims_and_refuse_fake_authority():
    schema = ClaimsArgs.model_json_schema()
    assert schema["$defs"]["ResearchClaim"]["additionalProperties"] is False
    assert {"kind", "statement"}.issubset(schema["$defs"]["ResearchClaim"]["required"])
    assert {"metric", "quotes", "period", "currency", "accounting_basis"}.issubset(
        schema["$defs"]["ResearchClaim"]["properties"]
    )
    data = {"kind": "fact", "statement": "合成", "supports": ["passage"]}
    assert ClaimsArgs(claims=[data]).claims[0].supports == ["passage"]
    for change in (
        {"semantic_status": "verified"},
        {"value": True},
        {"value": float("inf")},
        {"formula": {"operator": "eval", "input_claim_ids": ["a", "b"]}},
    ):
        with pytest.raises(ValidationError):
            ClaimsArgs(claims=[{**data, **change}])
    relational = RelationsArgs.model_json_schema()["$defs"]["ResearchRelation"]
    assert {"from_entity", "to_entity", "product", "dimension", "mechanism"}.issubset(relational["required"])


def test_numeric_conflict_rendering_runs_actual_shipped_javascript():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, str(root / "tests/research_quantities_runtime.cjs")],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "9 checks passed" in result.stdout


@pytest.fixture
def owned():
    ledger.init_research_db()
    with get_db() as db:
        for owner in ("numeric-alice", "numeric-bob"):
            db.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", [owner, "synthetic-only"])
            db.execute("INSERT INTO conversations(id,username) VALUES(?,?)", [owner, owner])
    context = build_research_context(run_id="numeric-run", now=datetime(2026, 11, 3, tzinfo=UTC))
    ledger.create_research_run("numeric-alice", "numeric-alice", "核对合成产能", context)


def receipt(text, call_id="a"):
    tracker = EvidenceTracker("numeric-run")
    call = {"id": call_id, "name": "WebFetch", "input": {"url": "https://www.sse.com.cn/synthetic"}}
    tracker.record_call(call)
    block = {"tool_use_id": call_id, "content": text}
    refs = tracker.record_result(block)
    return ledger.ingest_tool_result("numeric-alice", "numeric-run", call, block, refs)[0]["passage_ids"][0]


def fact(pid, value, **changes):
    return {
        "kind": "fact",
        "statement": "合成数字",
        "supports": [pid],
        "value": value,
        "unit": "万吨",
        "period": "2025",
        "entities": ["甲公司"],
        "metric": "设计年产能",
        "accounting_basis": "设计产能",
        **changes,
    }


def test_real_ledger_exposes_and_freezes_conflicts_without_rewriting_sources(owned):
    first, second = "2025年甲公司产能120万吨。", "2025年甲公司产能130万吨。"
    p1, p2 = receipt(first, "one"), receipt(second, "two")
    result = ledger.record_claims(
        "numeric-alice",
        "numeric-run",
        [fact(p1, "120", quotes={p1: first}), fact(p2, "130", quotes={p2: second})],
    )
    assert len(result["numerical_checks"]["conflicts"]) == 1
    revision = ledger.finalize_research_run("numeric-alice", "numeric-run", "合成报告保留数字矛盾。")
    assert "numeric_evidence_conflict" in revision["gaps"]
    assert revision["numerical_checks"] == result["numerical_checks"]
    actual = ledger.read_research_run("numeric-alice", "numeric-run")
    assert len(actual["documents"]) == 2 and actual["numerical_checks"]["conflicts"]
    with pytest.raises(HTTPException) as denied:
        ledger.read_research_run("numeric-bob", "numeric-run")
    assert denied.value.status_code == 404
    with get_db() as db:
        db.execute("UPDATE conversations SET deleted_at=1 WHERE id='numeric-alice'")
    with pytest.raises(HTTPException):
        ledger.read_research_run("numeric-alice", "numeric-run")


def test_registering_real_receipts_compares_previous_research_without_an_extra_model(owned):
    context = build_research_context(run_id="previous-run", now=datetime(2026, 11, 3, tzinfo=UTC))
    ledger.create_research_run("numeric-alice", "numeric-alice", "合成既有研究", context)
    tracker = EvidenceTracker("previous-run")
    call = {"id": "old-source", "name": "WebFetch", "input": {"url": "https://www.sse.com.cn/synthetic-old"}}
    tracker.record_call(call)
    original = "2025年甲公司产能130万吨。"
    block = {"tool_use_id": "old-source", "content": original}
    refs = tracker.record_result(block)
    pid = ledger.ingest_tool_result("numeric-alice", "previous-run", call, block, refs)[0]["passage_ids"][0]
    past = ledger.record_claims("numeric-alice", "previous-run", [fact(pid, "130", quotes={pid: original})])[
        "claims"
    ][0]
    frozen = ledger.finalize_research_run("numeric-alice", "previous-run", "固定合成历史报告。")
    current_text = "2025年甲公司产能120万吨。"
    pid = receipt(current_text)
    result = ledger.record_claims(
        "numeric-alice", "numeric-run", [fact(pid, "120", quotes={pid: current_text})]
    )
    assert result["history_checks"]["status"] == "conflicts_require_review"
    conflict = result["history_checks"]["conflicts"][0]
    assert set(conflict["claim_ids"]) == {past["claim_id"], result["claims"][0]["claim_id"]}
    assert ledger.finalize_research_run("numeric-alice", "previous-run", "不应覆盖") == frozen


def test_research_arithmetic_does_not_round_29_digit_addition_or_double_count_input(owned):
    number = "12345678901234567890123456789"
    pid = receipt("2025年甲公司金额A为" + number + "元，金额B为1元。")
    a, b = ledger.record_claims(
        "numeric-alice",
        "numeric-run",
        [fact(pid, number, unit="元", currency="CNY"), fact(pid, "1", unit="元", currency="CNY")],
    )["claims"]
    base = {
        "kind": "calculation",
        "statement": "合成加法",
        "period": "2025",
        "unit": "元",
        "currency": "CNY",
        "value": "12345678901234567890123456790",
        "formula": {"operator": "add", "input_claim_ids": [a["claim_id"], b["claim_id"]]},
    }
    actual = ledger.record_claims("numeric-alice", "numeric-run", [base])["claims"][0]
    assert actual["citation_status"] == "valid" and actual["formula"]["computed_value"] == base["value"]
    duplicate = {**base, "formula": {"operator": "add", "input_claim_ids": [a["claim_id"]] * 2}}
    actual = ledger.record_claims("numeric-alice", "numeric-run", [duplicate])["claims"][0]
    assert "duplicate_calculation_input" in actual["gaps"] and actual["formula"]["computed_value"] is None
    with pytest.raises(HTTPException):
        ledger.record_claims("numeric-alice", "numeric-run", [fact(pid, "1" * 101)])


def test_industry_paths_do_not_reuse_conflicting_or_withdrawn_numeric_support(owned):
    from server.industry_research import evidence_graph
    from server.research import knowledge

    texts = ["2025年甲公司产能120万吨。", "2025年甲公司产能130万吨。", "2025年乙厂向丙客户供应合成材料。"]
    pids = [receipt(text, "source-" + str(i)) for i, text in enumerate(texts)]
    claims = ledger.record_claims(
        "numeric-alice",
        "numeric-run",
        [
            fact(pids[0], "120", quotes={pids[0]: texts[0]}),
            fact(pids[1], "130", quotes={pids[1]: texts[1]}),
            {"kind": "fact", "statement": "合成供货", "supports": [pids[2]], "quotes": {pids[2]: texts[2]}},
        ],
    )["claims"]
    base = {
        "product": "合成材料",
        "dimension": "supply",
        "mechanism": "原料影响产出，仍需核验实际分配",
        "valid_period": "2025",
        "business_stage": "候选供货",
        "invalidators": ["订单取消"],
    }
    relations = ledger.record_relations(
        "numeric-alice",
        "numeric-run",
        [
            {
                **base,
                "from_entity": "甲公司",
                "to_entity": "乙厂",
                "supporting_claims": [claims[0]["claim_id"]],
            },
            {
                **base,
                "from_entity": "乙厂",
                "to_entity": "丙客户",
                "supporting_claims": [claims[2]["claim_id"]],
            },
        ],
    )["relations"]
    graph = evidence_graph("numeric-alice")
    blocked = next(r for r in graph["relations"] if r["relation_id"] == relations[0]["relation_id"])
    assert "numeric_evidence_conflict" in blocked["blocking_gaps"] and graph["transmission_paths"] == []
    knowledge.review(
        "numeric-alice",
        claims[1]["claim_id"],
        knowledge.Review(expected_version=0, state="retracted", note="合成更正，保留旧记录"),
    )
    graph = evidence_graph("numeric-alice")
    assert len(graph["transmission_paths"]) == 1
    assert graph["transmission_paths"][0]["cross_link_checks"]["period"]["status"] == "overlap"
    with get_db() as db:
        import json

        row = db.execute(
            "SELECT payload FROM research_relations WHERE relation_id=?", [relations[1]["relation_id"]]
        ).fetchone()
        changed = json.loads(row[0])
        changed["valid_period"] = "2026"
        db.execute(
            "UPDATE research_relations SET payload=? WHERE relation_id=?",
            [json.dumps(changed), relations[1]["relation_id"]],
        )
    path = evidence_graph("numeric-alice")["transmission_paths"][0]
    assert (
        path["status"] == "incompatible_periods"
        and path["cross_link_checks"]["period"]["status"] == "nonoverlap"
    )
