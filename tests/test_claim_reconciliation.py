"""Synthetic report-number coverage checks; no model, credentials or live data."""

import json
from datetime import UTC, datetime

from server import operations as ops
from server.agent.runtime import _WRAP_UP_VERIFICATION_PROMPT
from server.database import get_db
from server.research import (
    create_research_run,
    finalize_research_run,
    read_research_run,
    reconciliation,
    record_claims,
)
from server.research import ledger as ledger_module
from server.research.reconciliation import SCHEMA, reconcile_report
from server.research_context import build_research_context


def _user(username="alice"):
    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO users(username,password_hash) VALUES(?,?)",
            [username, "synthetic-q3-user"],
        )


def _run(run_id="round-q3", question="研究合成公司的 2025 年产能"):
    _user()
    context = build_research_context(run_id=run_id, now=datetime(2026, 10, 20, 18, tzinfo=UTC))
    create_research_run("alice", "", question, context)


def _claim(**extra):
    claim = {"kind": "inference", "statement": "合成数字判断", "limitations": ["合成失效条件"]}
    claim.update(extra)
    return claim


# ---- extraction and matching (pure function) -------------------------------------------


def test_report_numbers_matching_claims_are_covered():
    claims = [
        {"value": "120", "unit": "万吨", "verification": "verified_source"},
        {"value": "12.5", "unit": "%", "verification": "secondhand"},
    ]
    result = reconcile_report(claims, "2025 年产能为 120 万吨，同比增长 12.5%。")

    assert result["schema"] == SCHEMA
    assert result["status"] == "ok"
    assert result["unsupported_claims"] == 0
    assert result["claims_checked"] == 2
    assert result["report_numbers_checked"] == 2
    assert result["verified_claim_ratio"] == 0.5
    assert result["unsupported_numbers"] == []


def test_uncovered_report_number_is_counted_and_identifiable():
    claims = [{"value": "120", "unit": "万吨", "verification": "verified_source"}]
    result = reconcile_report(claims, "2025 年产能为 120 万吨，但毛利率达到 30.2%，市占率 12%。")

    assert result["unsupported_claims"] == 2
    numbers = {item["number"] for item in result["unsupported_numbers"]}
    assert numbers == {"30.2%", "12%"}
    assert all(item["reason"] == "no_matching_claim" for item in result["unsupported_numbers"])
    assert any("毛利率达到 30.2%" in item["sentence"] for item in result["unsupported_numbers"])


def test_years_dates_pages_sections_and_citations_are_not_key_numbers():
    text = (
        "2025 年、2026-10-20、第 12 页、第 4 章第 2 节、[1] 号引用与 [3] 号引用、"
        "序号 3.1、共 12 条记录、第 3 名、第 5 家客户。"
    )
    result = reconcile_report([], text)

    assert result["status"] == "ok"
    assert result["report_numbers_checked"] == 0
    assert result["unsupported_claims"] == 0
    assert result["verified_claim_ratio"] is None


def test_markdown_headings_and_rules_are_ignored():
    text = "# 核心判断\n\n1.1 结论\n\n---\n\n## 数字与计算\n\n产能利用率处于高位。\n"
    result = reconcile_report([], text)

    assert result["report_numbers_checked"] == 0
    assert result["unsupported_claims"] == 0


def test_no_claims_returns_null_ratio_without_dividing_by_zero():
    result = reconcile_report([], "营收 1.2 亿元，毛利率 25%。")

    assert result["status"] == "ok"
    assert result["verified_claim_ratio"] is None
    assert result["claims_checked"] == 0
    assert result["unsupported_claims"] == 2
    assert reconcile_report(None, None)["status"] == "ok"


def test_scaled_units_and_scaled_amounts_match_after_normalization():
    claims = [{"value": 120000000, "unit": "元", "currency": "CNY", "verification": "verified_source"}]
    result = reconcile_report(claims, "营收 1.2 亿元。")
    assert result["unsupported_claims"] == 0

    bare_scale = reconcile_report(
        [{"value": "120000000000", "unit": "元", "currency": "CNY", "verification": "verified_source"}],
        "市值 1200 亿。",
    )
    assert bare_scale["unsupported_claims"] == 0


def test_range_bounds_and_negative_signs_are_handled():
    claims = [
        {"value": "3", "unit": "%", "verification": "verified_source"},
        {"value": "5", "unit": "%", "verification": "secondhand"},
        {"value": "-3.2", "unit": "亿元", "currency": "CNY", "verification": "verified_source"},
    ]
    result = reconcile_report(claims, "增长 3%-5%，净利 -3.2 亿元。")

    assert result["unsupported_claims"] == 0
    assert result["report_numbers_checked"] == 3


def test_repeated_number_is_counted_once():
    result = reconcile_report([], "毛利率为 25%，下半年毛利率仍为 25%。")

    assert result["report_numbers_checked"] == 1
    assert result["unsupported_claims"] == 1


def test_table_cells_still_match_the_registered_claim():
    claims = [{"value": "120", "unit": "万吨", "verification": "verified_source"}]
    result = reconcile_report(claims, "| 指标 | 数值 | 单位 |\n| --- | --- | --- |\n| 产能 | 120 | 万吨 |")

    assert result["unsupported_claims"] == 0


def test_url_digits_are_not_report_numbers():
    result = reconcile_report([], "资料见 https://example.test/a/1.2?q=25%25 ，正文未给出关键数字。")
    assert result["report_numbers_checked"] == 0
    assert result["unsupported_claims"] == 0


def test_unexpected_failure_degrades_to_unavailable(monkeypatch):
    def boom(_text):
        raise RuntimeError("synthetic extraction failure")

    monkeypatch.setattr(reconciliation, "_report_numbers", boom)
    result = reconcile_report([], "毛利率 25%。")

    assert result == {"schema": SCHEMA, "status": "unavailable"}


# ---- ledger persistence ----------------------------------------------------------------


def test_finalize_persists_reconciliation_in_the_immutable_revision():
    _run("run-recon")
    record_claims(
        "alice",
        "run-recon",
        [_claim(kind="fact", value="120", unit="万吨", period="2025", verification="verified_source")],
    )
    revision = finalize_research_run("alice", "run-recon", "2025 年产能为 120 万吨，毛利率达到 25%。")

    check = revision["claim_reconciliation"]
    assert check["status"] == "ok"
    assert check["unsupported_claims"] == 1
    assert check["verified_claim_ratio"] == 1.0
    assert check["claims_checked"] == 1
    assert check["unsupported_numbers"][0]["number"] == "25%"
    stored = read_research_run("alice", "run-recon")
    assert stored["report_revisions"][0]["claim_reconciliation"] == check


def test_finalize_without_claims_keeps_null_ratio_and_never_divides_by_zero():
    _run("run-empty")
    revision = finalize_research_run("alice", "run-empty", "报告正文给出 25% 的毛利率。")

    check = revision["claim_reconciliation"]
    assert check["status"] == "ok"
    assert check["verified_claim_ratio"] is None
    assert check["claims_checked"] == 0
    assert check["unsupported_claims"] == 1


def test_finalize_degrades_reconciliation_but_still_returns_the_report(monkeypatch):
    def boom(_claims, _text):
        raise RuntimeError("synthetic reconciliation failure")

    monkeypatch.setattr(ledger_module, "reconcile_report", boom)
    _run("run-degraded")
    record_claims(
        "alice",
        "run-degraded",
        [_claim(kind="fact", value="120", unit="万吨", period="2025", verification="verified_source")],
    )
    revision = finalize_research_run("alice", "run-degraded", "2025 年产能为 120 万吨。")

    assert revision["claim_reconciliation"] == {"schema": SCHEMA, "status": "unavailable"}
    assert revision["narrative"].startswith("2025 年产能")
    assert revision["acceptance_status"] in {"completed", "partial"}
    assert read_research_run("alice", "run-degraded")["report_revisions"][0] == revision


# ---- admin aggregation -----------------------------------------------------------------


def test_operations_aggregates_revision_reconciliation():
    _run("run-admin")
    record_claims(
        "alice",
        "run-admin",
        [
            _claim(kind="fact", value="120", unit="万吨", period="2025", verification="verified_source"),
            _claim(kind="calculation", value="12.5", unit="%", period="2025", verification="secondhand"),
        ],
    )
    finalize_research_run("alice", "run-admin", "2025 年产能 120 万吨，同比增长 12.5%，毛利率 30%。")

    section = ops.research_observation()
    assert section["claim_verification"] == {"status": "ok"}
    assert section["verified_claim_ratio"] == 0.5
    assert section["unsupported_claims"] == 1


def test_operations_falls_back_to_claim_levels_for_unreconciled_runs(monkeypatch):
    def boom(_claims, _text):
        raise RuntimeError("synthetic reconciliation failure")

    monkeypatch.setattr(ledger_module, "reconcile_report", boom)
    _run("run-fallback")
    record_claims(
        "alice",
        "run-fallback",
        [
            _claim(kind="fact", value="120", unit="万吨", period="2025", verification="verified_source"),
            _claim(kind="inference", verification="unverified"),
        ],
    )
    finalize_research_run("alice", "run-fallback", "2025 年产能为 120 万吨。")

    section = ops.research_observation()
    assert section["claim_verification"] == {"status": "ok"}
    assert section["verified_claim_ratio"] == 0.5
    assert section["unsupported_claims"] == 1


# ---- model-visible wrap-up requirement -------------------------------------------------


def test_wrap_up_prompt_requires_a_complete_four_tuple():
    prompt = _WRAP_UP_VERIFICATION_PROMPT

    for token in (
        "关键数字",
        "research_record_claims",
        "source",
        "published_at",
        "scope",
        "verification",
        "verified_source",
        "secondhand",
        "inferred",
        "unverified",
        "编造来源",
    ):
        assert token in prompt
    assert "财务指标" in prompt and "产业数据" in prompt
    assert json.dumps(prompt, ensure_ascii=False)
