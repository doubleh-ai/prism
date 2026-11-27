"""Synthetic report-number coverage checks; no model, credentials or live data."""

import json
import random
from datetime import UTC, datetime

from server import operations as ops
from server.agent.runtime import _CLAIM_PASS_PROMPT, _WRAP_UP_VERIFICATION_PROMPT
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


def _seed_original(run_id):
    """One server-owned original PDF page so the strict no-original rejection does not fire."""

    document = {
        "document_id": f"doc_page_{run_id}",
        "run_id": run_id,
        "url": "https://example.test/annual",
        "title": "示例年报",
        "published_at": "2026-03-18T10:00:00+08:00",
        "publication_precision": "day",
        "original_obtained": True,
        "page_count": 3,
        "gaps": [],
    }
    passage = {
        "passage_id": f"pass_page_1_{run_id}",
        "run_id": run_id,
        "document_id": document["document_id"],
        "excerpt": "2025 年产能为 120 万吨",
        "content_scope": "original_pdf_page",
        "locator": {"kind": "pdf_page", "page": 1, "section": None},
    }
    with get_db() as db:
        db.execute(
            "INSERT INTO research_documents VALUES (?,?,?,?)",
            [
                document["document_id"],
                run_id,
                f"receipt-page-{run_id}",
                json.dumps(document, ensure_ascii=False),
            ],
        )
        db.execute(
            "INSERT INTO research_passages VALUES (?,?,?,?)",
            [passage["passage_id"], run_id, document["document_id"], json.dumps(passage, ensure_ascii=False)],
        )


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

    # "3%-5%" is one interval measurement, not two independent bounds; "-3.2" is a
    # real negative amount, so both bounds stay covered and the count is range + amount.
    assert result["unsupported_claims"] == 0
    assert result["report_numbers_checked"] == 2


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
    _seed_original("run-recon")
    record_claims(
        "alice",
        "run-recon",
        [_claim(kind="fact", value="120", unit="万吨", period="2025", verification="verified_source")],
    )
    revision = finalize_research_run("alice", "run-recon", "2025 年产能为 120 万吨，毛利率达到 25%。")

    check = revision["claim_reconciliation"]
    assert check["status"] == "ok"
    assert check["unsupported_claims"] == 0
    # The model claim covers 120 万吨; the server extracted the remaining 25% as
    # its own unverified claim, so every report number is registered and checked.
    assert check["claims_checked"] == check["report_numbers_checked"] == 2
    assert check["claims_registered"] == 2
    assert check["coverage"] == 1.0
    # The strict original-page gate demotes a declared verified_source without a
    # source_locator, and the extracted 25% claim is unverified, so nothing counts
    # as verified.
    assert check["verified_claim_ratio"] == 0.0
    assert check["unsupported_numbers"] == []
    stored = read_research_run("alice", "run-recon")
    assert stored["report_revisions"][0]["claim_reconciliation"] == check
    server_claims = [claim for claim in stored["claims"] if claim.get("origin") == "server_extract"]
    assert len(server_claims) == 1
    assert server_claims[0]["verification"] == "unverified"
    assert server_claims[0]["source"] == ""
    assert server_claims[0]["claim_key"] == ["ratio", None, "0.25"]


def test_finalize_auto_registers_report_numbers_when_the_model_registered_none():
    _run("run-empty")
    revision = finalize_research_run("alice", "run-empty", "报告正文给出 25% 的毛利率。")

    check = revision["claim_reconciliation"]
    assert check["status"] == "ok"
    assert check["verified_claim_ratio"] == 0.0
    assert check["claims_checked"] == check["report_numbers_checked"] == 1
    assert check["claims_registered"] == 1
    assert check["unsupported_claims"] == 0
    assert check["claims_checked"] + check["unsupported_claims"] == check["report_numbers_checked"]
    stored = read_research_run("alice", "run-empty")
    assert [claim["origin"] for claim in stored["claims"]] == ["server_extract"]
    assert stored["claims"][0]["verification"] == "unverified"
    assert stored["claims"][0]["statement"] == "报告正文数字：25%"


def test_finalize_degrades_reconciliation_but_still_returns_the_report(monkeypatch):
    def boom(_claims, _text):
        raise RuntimeError("synthetic reconciliation failure")

    monkeypatch.setattr(ledger_module, "reconcile_report", boom)
    _run("run-degraded")
    _seed_original("run-degraded")
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
    _seed_original("run-admin")
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
    # verified_source without an original-page source_locator is demoted to secondhand.
    assert section["verified_claim_ratio"] == 0.0
    # The uncovered 30% is auto-registered, so no report number stays unsupported.
    assert section["unsupported_claims"] == 0


def test_operations_falls_back_to_claim_levels_for_unreconciled_runs(monkeypatch):
    def boom(_claims, _text):
        raise RuntimeError("synthetic reconciliation failure")

    monkeypatch.setattr(ledger_module, "reconcile_report", boom)
    _run("run-fallback")
    _seed_original("run-fallback")
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
    # verified_source without an original-page source_locator is demoted to secondhand.
    assert section["verified_claim_ratio"] == 0.0
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


# ---- range measurements ----------------------------------------------------------------


def test_a_range_is_covered_by_a_claim_inside_the_interval():
    claims = [{"value": 4, "unit": "%", "verification": "verified_source"}]

    section = reconcile_report(claims, "公司毛利率介于3%-5%之间。")

    assert section["unsupported_claims"] == 0
    assert section["report_numbers_checked"] == 1


def test_a_range_is_unsupported_when_no_claim_falls_inside_it():
    claims = [{"value": 9, "unit": "%", "verification": "verified_source"}]

    section = reconcile_report(claims, "公司毛利率介于3%-5%之间。")

    assert section["unsupported_claims"] == 1
    assert [row["number"] for row in section["unsupported_numbers"]] == ["3%-5%"]


def test_a_quantity_range_keeps_both_bounds():
    claims = [{"value": 110, "unit": "万吨", "verification": "secondhand"}]

    section = reconcile_report(claims, "产量预计100-120万吨。")

    assert section["unsupported_claims"] == 0
    assert section["report_numbers_checked"] == 1


def test_ranges_and_single_measurements_coexist_in_one_sentence():
    claims = [{"value": 4, "unit": "%"}, {"value": 120, "unit": "万吨"}]

    section = reconcile_report(claims, "毛利率介于3%-5%之间，产量100-120万吨。")

    assert section["unsupported_claims"] == 0
    assert section["report_numbers_checked"] == 2


# ---- coverage metric and the advisory low-coverage gap ---------------------------------


def test_coverage_is_the_share_of_report_numbers_backed_by_a_claim():
    assert reconcile_report([], "营收 1.2 亿元，毛利率 25%。")["coverage"] == 0.0
    assert (
        reconcile_report([{"value": "120", "unit": "万吨"}], "产能 120 万吨，毛利率 25%。")["coverage"] == 0.5
    )
    assert reconcile_report([{"value": 25, "unit": "%"}], "毛利率 25%。")["coverage"] == 1.0
    assert reconcile_report([], "")["coverage"] is None


def test_coverage_is_not_inflated_by_claims_that_match_no_report_number():
    claims = [{"value": 1, "unit": "%"}, {"value": 2, "unit": "%"}, {"value": 3, "unit": "%"}]

    section = reconcile_report(claims, "毛利率 25%。")

    # Registered claims must not certify a number the report never wrote: only the
    # three uncovered numbers would count as checked under the old claim-side count.
    assert section["report_numbers_checked"] == 1
    assert section["claims_checked"] == 0
    assert section["claims_registered"] == 3
    assert section["coverage"] == 0.0


def test_finalize_auto_registers_uncovered_numbers_before_the_coverage_check():
    _run("run-coverage-low")
    _seed_original("run-coverage-low")
    record_claims("alice", "run-coverage-low", [_claim(verification="secondhand")])

    revision = finalize_research_run("alice", "run-coverage-low", "2025 年产能为 120 万吨，毛利率 25%。")

    check = revision["claim_reconciliation"]
    # The model claim carries no value, so it covers neither report number; the
    # server extracts one claim per number instead. Registration is not a gate:
    # the counters still describe the report numbers, not the model's work.
    assert check["claims_checked"] == 2
    assert check["claims_registered"] == 3
    assert check["report_numbers_checked"] == 2
    assert check["unsupported_claims"] == 0
    assert check["claims_checked"] + check["unsupported_claims"] == check["report_numbers_checked"]
    assert check["coverage"] == 1.0
    # With every number covered the advisory gap disappears; the model's own
    # incomplete citation work still keeps the revision partial.
    assert "claim_coverage_low" not in revision["gaps"]
    assert revision["acceptance_status"] == "partial"
    assert revision["narrative"].startswith("2025 年产能")


def test_finalize_skips_low_coverage_gap_when_every_number_is_covered():
    _run("run-coverage-full")
    _seed_original("run-coverage-full")
    record_claims(
        "alice",
        "run-coverage-full",
        [
            _claim(verification="secondhand", value="120", unit="万吨", period="2025"),
            _claim(verification="secondhand", value="25", unit="%", period="2025"),
        ],
    )

    revision = finalize_research_run("alice", "run-coverage-full", "2025 年产能为 120 万吨，毛利率 25%。")

    assert revision["claim_reconciliation"]["coverage"] == 1.0
    assert "claim_coverage_low" not in revision["gaps"]


# ---- number-centric accounting ---------------------------------------------------------


def test_every_report_number_is_classified_exactly_once():
    """The published counters always partition the extracted report numbers.

    Production run 6ad8e2c reported ``claims_checked=9``, ``report_numbers_checked=15``
    and ``unsupported_claims=12``: the counters came from different populations (claims
    versus numbers), so ``claims_checked + uncovered`` was never required to add up.
    """

    rng = random.Random(20261122)
    for _ in range(200):
        claims, text = _generated_report(rng)
        result = reconcile_report(claims, text)

        assert result["status"] == "ok"
        report_count = result["report_numbers_checked"]
        assert result["claims_checked"] + result["unsupported_claims"] == report_count
        assert 0 <= result["claims_checked"] <= report_count
        assert result["claims_registered"] == len(claims)
        assert len(result["unsupported_numbers"]) == min(result["unsupported_claims"], 20)
        expected = round(result["claims_checked"] / report_count, 4) if report_count else None
        assert result["coverage"] == expected
        assert all(
            row["number"] and row["reason"] == "no_matching_claim" for row in result["unsupported_numbers"]
        )


def test_annotated_rounded_restatement_counts_as_one_covered_number():
    claims = [{"value": "170899152276.34", "unit": "元", "currency": "CNY"}]

    result = reconcile_report(claims, "营业收入 170,899,152,276.34（约1708.99亿）。")

    # The parenthetical restates the exact amount, so it is not a second number and
    # the covered number is the exact value, not the rounded display.
    assert result["report_numbers_checked"] == 1
    assert result["claims_checked"] == 1
    assert result["unsupported_claims"] == 0
    assert result["coverage"] == 1.0


def test_annotated_restatement_tolerates_display_truncation():
    claims = [{"value": "1234567890", "unit": "元", "currency": "CNY"}]

    result = reconcile_report(claims, "营业收入 1,234,567,890（约12.3亿）。")

    assert result["report_numbers_checked"] == 1
    assert result["claims_checked"] == 1
    assert result["unsupported_claims"] == 0


def test_parenthetical_that_does_not_restate_the_number_is_read_normally():
    claims = [{"value": "170899152276.34", "unit": "元", "currency": "CNY"}]

    result = reconcile_report(claims, "营业收入 170,899,152,276.34（约999亿）。")

    # 999 亿 is not the rounded form of the exact amount, so it is an independent
    # number; the exact value has no adjacent unit and is not extracted.
    assert result["report_numbers_checked"] == 1
    assert result["claims_checked"] == 0
    assert result["unsupported_claims"] == 1
    assert [row["number"] for row in result["unsupported_numbers"]] == ["999亿"]


PRODUCTION_SHAPED_REPORT = (
    "| 指标 | 2024年（元） | 2023年（元） | 同比增减 |\n"
    "| --- | --- | --- | --- |\n"
    "| 营业收入 | 170,899,152,276.34（约1708.99亿） | 147,693,604,994.14（约1476.94亿） "
    "| **+15.71%** |\n"
    "| 归母净利润 | 86,228,146,421.62（约862.28亿） | 74,734,071,550.75（约747.34亿） "
    "| **+15.38%** |\n"
    "| 扣非净利润 | 86,240,905,977.42（约862.41亿） | 74,752,564,425.52（约747.53亿） "
    "| **+15.37%** |\n"
    "年报摘要披露营业总收入1,741.44亿元，同比增长15.66%，与本表营业收入1,708.99亿元口径不同。\n"
    "两者差额为 86,240,905,977.42 − 86,228,146,421.62 = 12,759,555.80 元（约1275.96万元），"
    "净额约 −1276万元。\n"
)


def _production_shaped_claims():
    """Nine claims matching the six exact table amounts and three growth rates."""

    values = (
        ("170899152276.34", "元"),
        ("147693604994.14", "元"),
        ("86228146421.62", "元"),
        ("74734071550.75", "元"),
        ("86240905977.42", "元"),
        ("74752564425.52", "元"),
        ("15.71", "%"),
        ("15.38", "%"),
        ("15.37", "%"),
    )
    return [
        {"claim_id": f"claim-{index}", "value": value, "unit": unit, "verification": "verified_source"}
        for index, (value, unit) in enumerate(values)
    ]


def test_production_shaped_report_accounts_every_number_once():
    """Regression for the counters observed on production run 6ad8e2c (9/15/12)."""

    result = reconcile_report(_production_shaped_claims(), PRODUCTION_SHAPED_REPORT)

    assert result["status"] == "ok"
    assert result["report_numbers_checked"] == 15
    assert result["claims_registered"] == 9
    assert result["claims_checked"] == 9
    assert result["unsupported_claims"] == 6
    assert result["claims_checked"] + result["unsupported_claims"] == result["report_numbers_checked"]
    assert result["coverage"] == 0.6
    assert {row["number"] for row in result["unsupported_numbers"]} == {
        "1,741.44亿元",
        "15.66%",
        "1,708.99亿元",
        "12,759,555.80 元",
        "1275.96万元",
        "1276万元",
    }
    assert not result["unsupported_numbers_truncated"]


def _generated_report(rng):
    """One deterministic synthetic report plus the claims the model might register."""

    claims = []
    sentences = []
    for index in range(rng.randint(0, 8)):
        unit = rng.choice(("元", "%", "万吨"))
        if unit == "%":
            value = round(rng.uniform(0.1, 99.9), 2)
            rendered = f"{value}%"
        else:
            value = rng.randint(1, 9_999_999)
            rendered = f"{value:,} {unit}"
        claims.append(
            {
                "value": value,
                "unit": unit,
                "verification": rng.choice(("verified_source", "secondhand")),
            }
        )
        sentences.append(f"指标{index}为 {rendered}。")
    for _ in range(rng.randint(0, 4)):
        sentences.append(f"参考值 {rng.randint(1, 9999)}% 与 {rng.randint(1, 999)} 万吨。")
    return claims, "".join(sentences)


def test_claim_pass_prompt_keeps_the_bounded_baseline_instructions():
    prompt = _CLAIM_PASS_PROMPT

    # The claim pass must stay a short, baseline-sized instruction. Production
    # run 1f70e9239401829679ecaae59c37bad8 asked the model to register every report
    # number, the claim turn streamed a tool-argument JSON that hit the output
    # ceiling, and the truncated JSON failed the whole run. Run
    # 8b4e43d18a6369f18158267068e9f9ab then registered two claims for four report
    # numbers (coverage 0.5) because each claim merged an amount with its growth
    # rate. The fix is one sentence: one claim carries one value, never merge.
    for token in (
        "先登记报告正文的每个数值，再直接写报告正文。",
        "一个 claim 只填一个 value：金额、百分比/增速、数量、比率各登记一条，不得合并或省略",
        "每次调用最多登记 6 条 claim，超出的下一轮再报。",
        "证据不足的标 verification=unverified 或 inferred，但仍必须出现在 claims 中。",
        "本轮唯一可用工具是 research_record_claims",
        "不要调用其它工具、不要继续检索",
        "已登记的数字才能在正文中作为结论依据",
        "未登记的数字不要写进正文",
        "不要为了凑数编造来源、公告编号或发布日期。",
    ):
        assert token in prompt
    for token in ("每一个数字", "单次最多 20 条", "170,899,152,276.34", "1,708.99 亿元", "15.66%"):
        assert token not in prompt
    # ... and the reverted 4bcf9fd variant (1221 chars) is out of reach: keep the
    # ff33161 baseline (799 chars) plus at most ~150 chars of new wording. The
    # 6-claim batching rule is one short sentence so the prompt stays bounded.
    assert len(prompt) <= 950
