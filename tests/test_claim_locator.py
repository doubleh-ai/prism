"""Server-side claim-to-page association for obtained original documents.

Batch fourteen held two original documents yet finalized every claim unverified:
the model had the page text but never linked a claim to one concrete page. The
ledger now performs that link itself after the report-body claims are registered
and before the evidence gate runs. These tests pin the four shapes that matter:
an exact page hit, a range hit, a miss that keeps the level and records the
attempt, and a run without any original document that stays gated. They also pin
that a model-supplied locator is never overwritten. Further tests pin the
representations a report and its table disagree on: 亿元 against bare 元,
declared 万元 tables, thousand separators and full-width digits, a report
interval containing a page value, parenthesized negatives, and a converted value
beyond the relative tolerance that must not match.
"""

import json
from datetime import UTC, datetime

from server.database import get_db
from server.research import (
    create_research_run,
    finalize_research_run,
    read_research_run,
    record_claims,
)
from server.research_context import build_research_context


def _run(run_id, question="研究合成公司的 2025 年营业收入"):
    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO users(username,password_hash) VALUES(?,?)",
            ["alice", "synthetic-q3-user"],
        )
    context = build_research_context(run_id=run_id, now=datetime(2026, 10, 20, 18, tzinfo=UTC))
    create_research_run("alice", "", question, context)


def _seed_original(run_id, pages):
    """Insert one obtained original document with the given page texts in order."""
    document = {
        "document_id": f"doc_{run_id}",
        "run_id": run_id,
        "url": "https://example.test/annual",
        "title": "示例年报",
        "published_at": "2026-03-18T10:00:00+08:00",
        "publication_precision": "day",
        "original_obtained": True,
        "page_count": len(pages),
        "gaps": [],
    }
    with get_db() as db:
        db.execute(
            "INSERT INTO research_documents VALUES (?,?,?,?)",
            [document["document_id"], run_id, f"receipt-{run_id}", json.dumps(document, ensure_ascii=False)],
        )
        for page, text in enumerate(pages, start=1):
            passage = {
                "passage_id": f"pass_{run_id}_{page}",
                "run_id": run_id,
                "document_id": document["document_id"],
                "excerpt": text,
                "content_scope": "original_pdf_page",
                "locator": {"kind": "pdf_page", "page": page, "section": None},
            }
            db.execute(
                "INSERT INTO research_passages VALUES (?,?,?,?)",
                [
                    passage["passage_id"],
                    run_id,
                    document["document_id"],
                    json.dumps(passage, ensure_ascii=False),
                ],
            )
    return document


def _claim_with_display(stored, display):
    """The registered claim whose report text carries ``display``."""
    return next(claim for claim in stored["claims"] if claim["statement"] == f"报告正文数字：{display}")


def test_financial_claim_is_located_on_the_original_page():
    _run("run-locate-page")
    document = _seed_original(
        "run-locate-page",
        ["2025 年公司经营概况。", "2025 年营业收入为 128.6 亿元。"],
    )

    revision = finalize_research_run("alice", "run-locate-page", "2025 年营业收入为 128.6 亿元。")

    stored = read_research_run("alice", "run-locate-page")
    assert len(stored["claims"]) == 1
    claim = _claim_with_display(stored, "128.6 亿元")
    assert claim["verification"] == "verified_source"
    assert claim["origin"] == "server_locator"
    assert claim["source_locator"] == {"document_id": document["document_id"], "page": 2}
    assert "locator_attempt" not in claim
    assert revision["claim_reconciliation"]["verified_claim_ratio"] == 1.0


def test_range_on_the_original_page_covers_a_point_claim():
    _run("run-locate-range")
    document = _seed_original(
        "run-locate-range",
        ["2025 年营业收入为 128.6 亿元至 130 亿元。"],
    )

    finalize_research_run("alice", "run-locate-range", "2025 年营业收入约为 129 亿元。")

    stored = read_research_run("alice", "run-locate-range")
    claim = _claim_with_display(stored, "129 亿元")
    assert claim["verification"] == "verified_source"
    assert claim["origin"] == "server_locator"
    assert claim["source_locator"] == {"document_id": document["document_id"], "page": 1}


def test_unmatched_financial_claim_keeps_its_level_and_records_the_attempt():
    _run("run-locate-miss")
    _seed_original("run-locate-miss", ["2025 年营业收入为 128.6 亿元。"])

    revision = finalize_research_run("alice", "run-locate-miss", "2025 年营业收入为 99 亿元。")

    stored = read_research_run("alice", "run-locate-miss")
    claim = _claim_with_display(stored, "99 亿元")
    assert claim["verification"] == "unverified"
    assert claim["origin"] == "server_extract"
    assert claim["source_locator"] is None
    assert claim["locator_attempt"] == "not_found"
    assert revision["claim_reconciliation"]["verified_claim_ratio"] == 0.0


def test_claim_without_an_original_document_keeps_its_level_for_the_gate():
    _run("run-locate-no-original")

    revision = finalize_research_run("alice", "run-locate-no-original", "2025 年营业收入为 128.6 亿元。")

    assert revision["acceptance_status"] == "blocked"
    assert revision["error_code"] == "original_required"
    stored = read_research_run("alice", "run-locate-no-original")
    claim = _claim_with_display(stored, "128.6 亿元")
    assert claim["verification"] == "unverified"
    assert claim["origin"] == "server_extract"
    assert claim["source_locator"] is None
    assert "locator_attempt" not in claim


def test_existing_model_locator_is_not_overwritten():
    _run("run-locate-model")
    document = _seed_original(
        "run-locate-model",
        ["2025 年公司经营概况。", "2025 年营业收入为 128.6 亿元。"],
    )
    claimed = record_claims(
        "alice",
        "run-locate-model",
        [
            {
                "kind": "fact",
                "statement": "2025 年营业收入为 128.6 亿元",
                "period": "2025",
                "value": "128.6",
                "unit": "亿元",
                "verification": "secondhand",
                "source_locator": {"document_id": document["document_id"], "page": 1},
            }
        ],
    )["claims"][0]
    assert claimed["verification"] == "secondhand"
    assert claimed["source_locator"] == {"document_id": document["document_id"], "page": 1}

    finalize_research_run("alice", "run-locate-model", "2025 年营业收入为 128.6 亿元。")

    stored = read_research_run("alice", "run-locate-model")
    assert len(stored["claims"]) == 1
    # The page the model declared does not carry the number, but the server does
    # not silently replace a model-owned locator with its own page.
    claim = stored["claims"][0]
    assert claim["verification"] == "secondhand"
    assert claim["source_locator"] == {"document_id": document["document_id"], "page": 1}
    assert "origin" not in claim
    assert "locator_attempt" not in claim


def test_rounded_report_amount_matches_a_yuan_table():
    _run("run-locate-unit")
    document = _seed_original(
        "run-locate-unit",
        ["单位：元 币种：人民币\n营业收入 170,899,152,276.34 147,693,604,994.14"],
    )

    finalize_research_run("alice", "run-locate-unit", "2024 年营业收入为 1,708.99 亿元。")

    stored = read_research_run("alice", "run-locate-unit")
    claim = _claim_with_display(stored, "1,708.99 亿元")
    assert claim["verification"] == "verified_source"
    assert claim["origin"] == "server_locator"
    assert claim["source_locator"] == {"document_id": document["document_id"], "page": 1}


def test_declared_wan_yuan_table_matches_a_yi_yuan_claim():
    _run("run-locate-wan")
    document = _seed_original("run-locate-wan", ["单位：万元 币种：人民币\n营业收入 123,456.78"])

    finalize_research_run("alice", "run-locate-wan", "2024 年营业收入为 12.35 亿元。")

    stored = read_research_run("alice", "run-locate-wan")
    claim = _claim_with_display(stored, "12.35 亿元")
    assert claim["verification"] == "verified_source"
    assert claim["origin"] == "server_locator"
    assert claim["source_locator"] == {"document_id": document["document_id"], "page": 1}


def test_thousand_separator_and_full_width_digits_are_normalized():
    _run("run-locate-wide")
    document = _seed_original(
        "run-locate-wide",
        ["单位：元 币种：人民币\n营业收入 １７０，８９９，１５２，２７６．３４"],
    )

    finalize_research_run("alice", "run-locate-wide", "2024 年营业收入为 170,899,152,276.34 元。")

    stored = read_research_run("alice", "run-locate-wide")
    claim = _claim_with_display(stored, "170,899,152,276.34 元")
    assert claim["verification"] == "verified_source"
    assert claim["origin"] == "server_locator"
    assert claim["source_locator"] == {"document_id": document["document_id"], "page": 1}


def test_report_interval_covers_an_exact_page_value():
    _run("run-locate-interval")
    document = _seed_original(
        "run-locate-interval",
        ["单位：元 币种：人民币\n营业收入 170,899,152,276.34"],
    )

    revision = finalize_research_run(
        "alice",
        "run-locate-interval",
        "2024 年营业收入预计为 1,700 亿元至 1,720 亿元。",
    )

    stored = read_research_run("alice", "run-locate-interval")
    claim = _claim_with_display(stored, "1,700 亿元至1,720 亿元")
    assert claim["verification"] == "verified_source"
    assert claim["origin"] == "server_locator"
    assert claim["source_locator"] == {"document_id": document["document_id"], "page": 1}
    assert revision["claim_reconciliation"]["verified_claim_ratio"] == 1.0


def test_parenthesized_negative_page_value_matches_a_negative_claim():
    _run("run-locate-loss")
    document = _seed_original("run-locate-loss", ["2025 年净利润为 (1,234.56 万元)。"])
    record_claims(
        "alice",
        "run-locate-loss",
        [
            {
                "kind": "fact",
                "statement": "2025 年净利润为 (1,234.56 万元)",
                "period": "2025",
                "value": "-1234.56",
                "unit": "万元",
                "verification": "unverified",
            }
        ],
    )

    finalize_research_run("alice", "run-locate-loss", "2025 年净利润为 (1,234.56 万元)。")

    stored = read_research_run("alice", "run-locate-loss")
    claim = stored["claims"][0]
    assert claim["verification"] == "verified_source"
    assert claim["origin"] == "server_locator"
    assert claim["source_locator"] == {"document_id": document["document_id"], "page": 1}


def test_parenthesized_claim_value_is_normalized():
    _run("run-locate-paren-claim")
    document = _seed_original("run-locate-paren-claim", ["2025 年净利润为 (1,234.56) 万元。"])
    record_claims(
        "alice",
        "run-locate-paren-claim",
        [
            {
                "kind": "fact",
                "statement": "2025 年净利润为 (1,234.56) 万元",
                "period": "2025",
                "value": "(1,234.56)",
                "unit": "万元",
                "verification": "unverified",
            }
        ],
    )

    finalize_research_run("alice", "run-locate-paren-claim", "2025 年净利润为 (1,234.56) 万元。")

    stored = read_research_run("alice", "run-locate-paren-claim")
    claim = stored["claims"][0]
    assert claim["verification"] == "verified_source"
    assert claim["origin"] == "server_locator"
    assert claim["source_locator"] == {"document_id": document["document_id"], "page": 1}


def test_converted_value_beyond_tolerance_is_not_located():
    _run("run-locate-tolerance")
    _seed_original(
        "run-locate-tolerance",
        ["单位：元 币种：人民币\n营业收入 174,000,000,000.00"],
    )

    revision = finalize_research_run("alice", "run-locate-tolerance", "2024 年营业收入为 1,708.99 亿元。")

    stored = read_research_run("alice", "run-locate-tolerance")
    claim = _claim_with_display(stored, "1,708.99 亿元")
    assert claim["verification"] == "unverified"
    assert claim["origin"] == "server_extract"
    assert claim["source_locator"] is None
    assert claim["locator_attempt"] == "not_found"
    assert revision["claim_reconciliation"]["verified_claim_ratio"] == 0.0


def test_located_claim_gets_the_page_as_support_and_a_complete_citation():
    _run("run-locate-supports")
    document = _seed_original(
        "run-locate-supports",
        ["2025 年公司经营概况。", "2025 年营业收入为 128.6 亿元。"],
    )

    revision = finalize_research_run("alice", "run-locate-supports", "2025 年营业收入为 128.6 亿元。")

    stored = read_research_run("alice", "run-locate-supports")
    claim = _claim_with_display(stored, "128.6 亿元")
    # The located page is written as the claim's support, so the claim is a
    # complete citation rather than a located-but-unsupported one. Batch
    # fourteen got the locator without the support and stayed blocked as
    # citation_checks_incomplete even after the original was read.
    assert claim["verification"] == "verified_source"
    assert claim["supports"] == ["pass_run-locate-supports_2"]
    assert claim["citation_status"] == "valid"
    assert "support_missing" not in claim["gaps"]
    assert "search_snippet_only" not in claim["gaps"]
    assert "citation_checks_incomplete" not in revision["gaps"]
    # The support id points at the real page passage of the obtained original.
    support = next(passage for passage in stored["passages"] if passage["passage_id"] == claim["supports"][0])
    assert support["document_id"] == document["document_id"]
    assert support["content_scope"] == "original_pdf_page"
    assert support["locator"]["page"] == claim["source_locator"]["page"]


def test_page_support_is_not_duplicated_when_the_model_already_linked_it():
    _run("run-locate-supports-dedupe")
    _seed_original(
        "run-locate-supports-dedupe",
        ["2025 年公司经营概况。", "2025 年营业收入为 128.6 亿元。"],
    )
    page_passage_id = "pass_run-locate-supports-dedupe_2"
    claimed = record_claims(
        "alice",
        "run-locate-supports-dedupe",
        [
            {
                "kind": "fact",
                "statement": "2025 年营业收入为 128.6 亿元",
                "period": "2025",
                "value": "128.6",
                "unit": "亿元",
                "supports": [page_passage_id],
                "verification": "secondhand",
            }
        ],
    )["claims"][0]
    assert claimed["supports"] == [page_passage_id]

    finalize_research_run("alice", "run-locate-supports-dedupe", "2025 年营业收入为 128.6 亿元。")

    stored = read_research_run("alice", "run-locate-supports-dedupe")
    assert len(stored["claims"]) == 1
    claim = stored["claims"][0]
    assert claim["origin"] == "server_locator"
    assert claim["supports"] == [page_passage_id]
    assert claim["citation_status"] == "valid"


# ---- report bodies that state their unit in a header or a row label ---------------


def _claim_containing(stored, text):
    """The registered claim whose report-text statement contains ``text``."""
    return next(claim for claim in stored["claims"] if text in (claim.get("statement") or ""))


def test_percent_column_header_covers_its_bare_cells():
    """A 增减(%) column prints bare cells; +15.71% must still find the page."""
    _run("run-locate-percent-header")
    document = _seed_original(
        "run-locate-percent-header",
        ["单位：元 币种：人民币\n主要会计数据 2024年 2023年 本期比上年同期增减(%)\n营业收入 100 90 15.71 80"],
    )

    finalize_research_run("alice", "run-locate-percent-header", "2024 年营业收入同比增长 15.71%。")

    stored = read_research_run("alice", "run-locate-percent-header")
    claim = _claim_containing(stored, "15.71%")
    assert claim["claim_key"] == ["ratio", None, "0.1571"]
    assert claim["verification"] == "verified_source"
    assert claim["origin"] == "server_locator"
    assert claim["source_locator"] == {"document_id": document["document_id"], "page": 1}
    assert "support_missing" not in claim["gaps"]


def test_inline_row_unit_covers_a_per_share_cell():
    """The unit of a per-share row lives in the label; 68.64 元/股 must find it."""
    _run("run-locate-per-share")
    document = _seed_original(
        "run-locate-per-share",
        [
            "单位：元 币种：人民币\n"
            "主要会计数据 2024年 2023年 本期比上年同期增减(%)\n"
            "基本每股收益（元／股） 68.64 59.49 15.38 49.93"
        ],
    )

    finalize_research_run("alice", "run-locate-per-share", "2024 年基本每股收益为 68.64 元/股。")

    stored = read_research_run("alice", "run-locate-per-share")
    claim = _claim_containing(stored, "68.64 元/股")
    assert claim["claim_key"] == ["unit:元/股", "CNY", "68.64"]
    assert claim["verification"] == "verified_source"
    assert claim["origin"] == "server_locator"
    assert claim["source_locator"] == {"document_id": document["document_id"], "page": 1}


def test_baifen_dian_claim_finds_the_row_that_states_it():
    _run("run-locate-baifen-dian")
    document = _seed_original(
        "run-locate-baifen-dian",
        ["单位：元 币种：人民币\n加权平均净资产收益率（%） 36.02 34.19 增加1.83个百分点 30.26"],
    )

    finalize_research_run(
        "alice",
        "run-locate-baifen-dian",
        "2024 年加权平均净资产收益率同比增加 1.83 个百分点。",
    )

    stored = read_research_run("alice", "run-locate-baifen-dian")
    claim = _claim_containing(stored, "1.83 个百分点")
    assert claim["verification"] == "verified_source"
    assert claim["origin"] == "server_locator"
    assert claim["source_locator"] == {"document_id": document["document_id"], "page": 1}


def test_bare_magnitude_unit_is_read_at_full_scale():
    """A claim in 万亿 (no currency unit) matches a table declared in 亿元."""
    _run("run-locate-magnitude")
    document = _seed_original(
        "run-locate-magnitude",
        ["单位：亿元 币种：人民币\n营业收入 17,100.00"],
    )
    record_claims(
        "alice",
        "run-locate-magnitude",
        [
            {
                "kind": "fact",
                "statement": "2024 年营业收入约 1.71 万亿",
                "period": "2024",
                "value": "1.71",
                "unit": "万亿",
                "verification": "secondhand",
            }
        ],
    )

    finalize_research_run("alice", "run-locate-magnitude", "2024 年营业收入约 1.71 万亿。")

    stored = read_research_run("alice", "run-locate-magnitude")
    claim = stored["claims"][0]
    assert claim["verification"] == "verified_source"
    assert claim["origin"] == "server_locator"
    assert claim["source_locator"] == {"document_id": document["document_id"], "page": 1}


def test_report_range_in_zhi_form_covers_an_exact_page_point():
    _run("run-locate-range-zhi")
    document = _seed_original("run-locate-range-zhi", ["2024 年实际产能为 6 万吨。"])

    finalize_research_run("alice", "run-locate-range-zhi", "2024 年产能预计为 5 万吨至 7 万吨。")

    stored = read_research_run("alice", "run-locate-range-zhi")
    claim = _claim_containing(stored, "5 万吨至7 万吨")
    # The stored claim_key keeps only the interval's low bound; the statement is
    # the source of the interval the locator must cover.
    assert claim["claim_key"] == ["mass_t", None, "50000"]
    assert claim["verification"] == "verified_source"
    assert claim["origin"] == "server_locator"
    assert claim["source_locator"] == {"document_id": document["document_id"], "page": 1}


def test_lot_suffix_claim_is_located_on_its_page():
    _run("run-locate-lots")
    document = _seed_original("run-locate-lots", ["2024 年成交 1,234 手。"])

    finalize_research_run("alice", "run-locate-lots", "2024 年成交 1,234 手。")

    stored = read_research_run("alice", "run-locate-lots")
    claim = _claim_containing(stored, "1,234 手")
    assert claim["verification"] == "verified_source"
    assert claim["origin"] == "server_locator"
    assert claim["source_locator"] == {"document_id": document["document_id"], "page": 1}


def test_match_on_a_late_page_of_a_long_document():
    """Every obtained page is normalized, not only the first pages a run quotes."""
    _run("run-locate-deep-page")
    pages = [f"第{page}页经营概况，未披露关键数字。" for page in range(1, 66)]
    pages.append("2025 年营业收入为 128.6 亿元。")
    document = _seed_original("run-locate-deep-page", pages)

    finalize_research_run("alice", "run-locate-deep-page", "2025 年营业收入为 128.6 亿元。")

    stored = read_research_run("alice", "run-locate-deep-page")
    claim = _claim_with_display(stored, "128.6 亿元")
    assert claim["verification"] == "verified_source"
    assert claim["source_locator"] == {"document_id": document["document_id"], "page": 66}


def test_unmatched_ratio_claim_records_the_attempt():
    """A ratio claim is now attempted; a genuine miss still records the attempt."""
    _run("run-locate-ratio-miss")
    _seed_original("run-locate-ratio-miss", ["2024 年公司经营概况，营业收入 100 亿元。"])

    finalize_research_run("alice", "run-locate-ratio-miss", "2024 年营业收入同比增长 15.71%。")

    stored = read_research_run("alice", "run-locate-ratio-miss")
    claim = _claim_containing(stored, "15.71%")
    assert claim["origin"] == "server_extract"
    assert claim["verification"] == "unverified"
    assert claim["source_locator"] is None
    assert claim["locator_attempt"] == "not_found"
