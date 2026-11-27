"""Scenario-based verification bar for research reports.

One flat bar punished structurally different research: a single-company annual
report reaches a high verified ratio, while a cross-category synthesis relies on
derived figures that cannot appear on an original page. The ledger therefore
classifies every run from objective facts it already stores (document kinds,
snapshot symbols and data channels, codes and product names in the question),
persists the scenario and its gate in payload JSON, and exposes both through the
read-only operations telemetry. These tests pin the classifier rule, the two
thresholds, the persistence shape, the advisory gap, and the replay of revisions
written before the scenario field existed.
"""

import json
import time
from datetime import UTC, datetime

import pytest

from server import operations as ops
from server.database import get_db
from server.futures import snapshot_document
from server.research import (
    create_research_run,
    finalize_research_run,
    ingest_market_snapshot,
    init_research_db,
    read_research_run,
    record_claims,
)
from server.research.quality_bar import (
    MULTI_SUBJECT,
    SCENARIO_THRESHOLDS,
    SCHEMA,
    SINGLE_SUBJECT,
    classify_scenario,
    is_below_bar,
    quality_gate,
    scenario_facts,
    scenario_threshold,
)
from server.research_context import build_research_context


def _user(username="alice"):
    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO users(username,password_hash) VALUES(?,?)",
            [username, "synthetic-scenario-user"],
        )


def _run(run_id, question="研究合成期货品种的行情数据"):
    _user()
    context = build_research_context(run_id=run_id, now=datetime(2026, 11, 22, 18, tzinfo=UTC))
    create_research_run("alice", "", question, context)


def _claim(run_id, verification="inferred"):
    record_claims(
        "alice",
        run_id,
        [
            {
                "kind": "inference",
                "statement": "合成判断：远月贴水反映库存压力",
                "limitations": ["现货基差回落"],
                "verification": verification,
            }
        ],
    )


def _snapshot(symbol="RB", day="2026-11-20", close=3125.0):
    name, exchange = {"RB": ("螺纹钢", "SHFE"), "M": ("豆粕", "DCE")}[symbol]
    return {
        "symbol": symbol,
        "name": name,
        "exchange": exchange,
        "status": "partial",
        "series_status": "collected",
        "tradable_contract": False,
        "data_as_of": day,
        "collected_at": day + "T15:30:00+08:00",
        "source": {
            "name": "新浪财经",
            "kind": "secondary_market_data",
            "url": f"https://finance.sina.com.cn/futures/quotes/{symbol}0.shtml",
            "status": "collected",
            "original_verified": False,
        },
        "latest": {
            "date": day,
            "open": close - 25.0,
            "high": close + 25.0,
            "low": close - 35.0,
            "close": close,
            "settlement": close - 5.0,
            "volume": 123456.0,
            "open_interest": 789012.0,
        },
        "gaps": [],
        "notice": "",
    }


def _ingest(run_id, *, symbol="RB", day="2026-11-20", access_method="futures_snapshot", call_id=None):
    call = {
        "type": "tool_use",
        "id": call_id or f"call-{run_id}-{symbol}-{access_method}-{day}",
        "name": access_method,
        "input": {"symbol": symbol},
    }
    document = snapshot_document(_snapshot(symbol=symbol, day=day))
    assert document is not None
    return ingest_market_snapshot(
        "alice", run_id, call, document, access_method=access_method, request={"symbol": symbol}
    )


# ---- classifier: objective facts only, no model self-report ------------------------------


def _snapshot_doc(symbol="RB", access_method="futures_snapshot"):
    return {
        "source_kind": "market_data_snapshot",
        "access_method": access_method,
        "request": {"symbol": symbol},
    }


def test_classify_single_subject_from_one_snapshot_symbol():
    result = classify_scenario("分析该品种的行情", [_snapshot_doc("RB")])

    assert result["scenario"] == SINGLE_SUBJECT
    assert result["facts"]["subjects"] == ["RB"]
    assert result["facts"]["data_channels"] == ["futures_snapshot"]


def test_classify_multi_subject_when_two_market_symbols_appear():
    result = classify_scenario("对比两个品种", [_snapshot_doc("RB"), _snapshot_doc("M")])

    assert result["scenario"] == MULTI_SUBJECT
    assert result["facts"]["market_symbols"] == ["M", "RB"]
    assert result["facts"]["subjects"] == ["M", "RB"]


def test_classify_multi_subject_when_one_symbol_spans_data_channels():
    documents = [
        _snapshot_doc("RB", "futures_snapshot"),
        _snapshot_doc("RB", "futures_inventory"),
        _snapshot_doc("RB", "futures_spread"),
    ]

    result = classify_scenario("分析螺纹钢主力合约与仓单、价差", documents)

    assert result["scenario"] == MULTI_SUBJECT
    assert result["facts"]["data_channels"] == ["futures_inventory", "futures_snapshot", "futures_spread"]
    assert result["facts"]["subjects"] == ["RB"]


def test_classify_multi_subject_when_question_names_two_security_codes():
    result = classify_scenario("比较贵州茅台 600519 与五粮液 000858 的营收", [])

    assert result["scenario"] == MULTI_SUBJECT
    assert result["facts"]["security_codes"] == ["000858", "600519"]


def test_classify_multi_subject_when_question_names_two_futures_products():
    result = classify_scenario("分析螺纹钢与豆粕的价差", [])

    assert result["scenario"] == MULTI_SUBJECT
    assert result["facts"]["products"] == ["M", "RB"]


def test_classify_defaults_to_single_subject_without_evidence():
    result = classify_scenario(None, [])

    assert result["scenario"] == SINGLE_SUBJECT
    assert result["facts"]["subjects"] == []
    assert result["facts"]["documents"] == 0


def test_scenario_facts_records_document_count_as_audit_fact():
    facts = scenario_facts("研究问题", [_snapshot_doc("RB"), {"source_kind": "external_web"}])

    assert facts["documents"] == 2
    assert facts["subjects"] == ["RB"]


# ---- thresholds and gate ------------------------------------------------------------------


def test_scenario_threshold_is_per_scenario():
    assert scenario_threshold(SINGLE_SUBJECT) == 0.8
    assert scenario_threshold(MULTI_SUBJECT) == 0.4
    assert scenario_threshold("unknown") is None
    assert SCENARIO_THRESHOLDS == {SINGLE_SUBJECT: 0.8, MULTI_SUBJECT: 0.4}


@pytest.mark.parametrize(
    ("scenario", "ratio", "passed"),
    [
        (SINGLE_SUBJECT, 0.8, True),
        (SINGLE_SUBJECT, 0.79, False),
        (SINGLE_SUBJECT, 0.42, False),
        (MULTI_SUBJECT, 0.4, True),
        (MULTI_SUBJECT, 0.39, False),
        (MULTI_SUBJECT, 0.43, True),
    ],
)
def test_quality_gate_passes_and_fails_at_the_scenario_bar(scenario, ratio, passed):
    gate = quality_gate(scenario, ratio)

    assert gate == {
        "schema": SCHEMA,
        "scenario": scenario,
        "threshold": SCENARIO_THRESHOLDS[scenario],
        "verified_claim_ratio": ratio,
        "passed": passed,
        "status": "ok",
    }
    assert is_below_bar(gate) is (not passed)


def test_quality_gate_without_ratio_is_not_applicable():
    gate = quality_gate(SINGLE_SUBJECT, None)

    assert gate["status"] == "not_applicable"
    assert gate["passed"] is None
    assert gate["threshold"] == 0.8
    assert is_below_bar(gate) is False


def test_quality_gate_rejects_unknown_scenario():
    gate = quality_gate("unknown_scenario", 0.9)

    assert gate["status"] == "unknown_scenario"
    assert gate["passed"] is None
    assert gate["threshold"] is None


# ---- ledger persistence and the advisory gap ---------------------------------------------


def _ratio(revision):
    return revision["claim_reconciliation"]["verified_claim_ratio"]


def test_single_subject_run_persists_scenario_and_passes_bar():
    run_id = "scenario-single-pass"
    _run(run_id)
    _ingest(run_id)

    revision = finalize_research_run("alice", run_id, "螺纹钢 RB 收盘价 3125 元/吨。")

    assert revision["scenario"] == SINGLE_SUBJECT
    assert revision["scenario_facts"]["subjects"] == ["RB"]
    assert revision["quality_gate"]["threshold"] == 0.8
    assert revision["quality_gate"]["passed"] is True
    assert "quality_below_scenario_threshold" not in revision["gaps"]
    assert revision["acceptance_status"] != "blocked"
    stored = read_research_run("alice", run_id)
    assert stored["run"]["scenario"] == SINGLE_SUBJECT
    assert stored["report_revisions"][-1]["scenario"] == SINGLE_SUBJECT
    assert _ratio(revision) >= 0.8


def test_single_subject_run_below_bar_gets_advisory_gap_only():
    run_id = "scenario-single-fail"
    _run(run_id)
    _ingest(run_id)
    _claim(run_id)

    revision = finalize_research_run(
        "alice", run_id, "螺纹钢 RB 收盘价 3125 元/吨，较远月合约贴水 88 元/吨。"
    )

    assert revision["scenario"] == SINGLE_SUBJECT
    assert _ratio(revision) < 0.8
    assert revision["quality_gate"]["passed"] is False
    assert "quality_below_scenario_threshold" in revision["gaps"]
    # Advisory only: the report is still produced and committed. The stronger
    # "keeps acceptance_status completed" invariant is covered end to end by
    # tests/test_native_agent_evidence_review.py, which has a clean run.
    assert revision["acceptance_status"] != "blocked"
    assert revision["narrative_status"] == "model_output_unreviewed"
    stored = read_research_run("alice", run_id)
    assert "quality_below_scenario_threshold" in stored["run"]["gaps"]


def test_multi_subject_run_uses_the_lower_bar():
    run_id = "scenario-multi-pass"
    _run(run_id, "比较螺纹钢与豆粕的行情，并给出跨品类综合判断")
    _ingest(run_id)

    revision = finalize_research_run("alice", run_id, "螺纹钢 RB 收盘价 3125 元/吨。")

    assert revision["scenario"] == MULTI_SUBJECT
    assert revision["quality_gate"]["threshold"] == 0.4
    assert revision["quality_gate"]["passed"] is True
    assert "quality_below_scenario_threshold" not in revision["gaps"]


def test_multi_subject_run_below_bar_gets_advisory_gap_only():
    run_id = "scenario-multi-fail"
    _run(run_id, "比较螺纹钢与豆粕的行情，并给出跨品类综合判断")
    _ingest(run_id)
    _claim(run_id)

    revision = finalize_research_run(
        "alice",
        run_id,
        "螺纹钢 RB 收盘价 3125 元/吨，较远月贴水 88 元/吨，持仓集中度 62.5%。",
    )

    assert revision["scenario"] == MULTI_SUBJECT
    assert _ratio(revision) < 0.4
    assert revision["quality_gate"]["passed"] is False
    assert "quality_below_scenario_threshold" in revision["gaps"]
    assert revision["acceptance_status"] != "blocked"


def test_blocked_run_records_scenario_without_a_threshold_result():
    run_id = "scenario-blocked"
    _run(run_id)

    revision = finalize_research_run("alice", run_id, "报告正文提到 2024 年营收 100 亿元。")

    assert revision["acceptance_status"] == "blocked"
    assert revision["scenario"] == SINGLE_SUBJECT
    assert revision["quality_gate"]["status"] == "not_applicable"
    assert revision["quality_gate"]["passed"] is None
    assert "quality_below_scenario_threshold" not in revision["gaps"]


# ---- admin telemetry ---------------------------------------------------------------------


def test_operations_section_exposes_latest_gate_and_scenario_breakdown():
    init_research_db()
    now = time.time()
    single_id = "ops-scenario-single"
    multi_id = "ops-scenario-multi"
    _run(single_id)
    _ingest(single_id)
    finalize_research_run("alice", single_id, "螺纹钢 RB 收盘价 3125 元/吨。")
    _run(multi_id, "比较螺纹钢与豆粕的行情")
    _ingest(multi_id)
    finalize_research_run("alice", multi_id, "螺纹钢 RB 收盘价 3125 元/吨。")

    section = ops.research_observation(now=now)

    assert section["scenario"] == MULTI_SUBJECT
    assert section["quality_gate"]["scenario"] == MULTI_SUBJECT
    assert section["quality_gate"]["threshold"] == 0.4
    assert section["quality_gate"]["passed"] is True
    assert section["scenarios"][SINGLE_SUBJECT]["runs"] == 1
    assert section["scenarios"][SINGLE_SUBJECT]["threshold"] == 0.8
    assert section["scenarios"][SINGLE_SUBJECT]["passed"] is True
    assert section["scenarios"][MULTI_SUBJECT]["runs"] == 1
    assert section["scenarios"][MULTI_SUBJECT]["threshold"] == 0.4
    assert section["scenarios"][MULTI_SUBJECT]["passed"] is True


def test_operations_replays_scenario_for_legacy_revision():
    """A revision stored before this change has no scenario; it is replayed from ledger facts."""
    init_research_db()
    now = time.time()
    run_id = "ops-scenario-legacy"
    _run(run_id)
    _ingest(run_id, access_method="futures_snapshot", day="2026-11-19")
    _ingest(run_id, access_method="futures_inventory", day="2026-11-20")
    with get_db() as db:
        db.execute(
            "INSERT INTO research_revisions VALUES(?,?,?)",
            [
                "rev-legacy-scenario",
                run_id,
                json.dumps(
                    {
                        "revision_id": "rev-legacy-scenario",
                        "run_id": run_id,
                        "claim_reconciliation": {
                            "schema": "prism.claim-reconciliation.v1",
                            "status": "ok",
                            "verified_claim_ratio": 0.43,
                            "unsupported_claims": 1,
                        },
                    },
                    ensure_ascii=False,
                ),
            ],
        )
        db.execute(
            "UPDATE research_runs SET created_at=? WHERE run_id=?",
            [datetime.now(UTC).isoformat(timespec="seconds"), run_id],
        )

    section = ops.research_observation(now=now)

    assert section["scenarios"][MULTI_SUBJECT]["runs"] == 1
    assert section["scenarios"][MULTI_SUBJECT]["evaluated_runs"] == 1
    assert section["scenarios"][MULTI_SUBJECT]["verified_claim_ratio"] == 0.43
    assert section["scenarios"][MULTI_SUBJECT]["passed"] is True
    assert section["quality_gate"]["scenario"] == MULTI_SUBJECT
    assert section["quality_gate"]["threshold"] == 0.4
    assert section["quality_gate"]["passed"] is True
