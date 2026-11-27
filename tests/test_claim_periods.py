"""Period writing variants between a claim and the excerpt that supports it.

A parsed period drives ``period_not_in_excerpt``. The model writes ``2024``,
``2024年``, ``2024Q3`` or ``2024-09-30`` and sometimes restates the period in
parentheses; the page states one of those forms. Comparing the literal string
flagged a correct citation whenever the writings differed, so ``period_matches``
normalizes both sides into year/quarter/half/month-range/date tokens. A
genuinely different period must still fail.
"""

import json
from datetime import UTC, datetime

import pytest

from server.database import get_db
from server.research import (
    create_research_run,
    finalize_research_run,
    read_research_run,
    record_claims,
)
from server.research.quantities import period_matches
from server.research_context import build_research_context


@pytest.mark.parametrize(
    ("period", "excerpt"),
    [
        ("2024", "2024 年年度报告"),
        ("2024年", "公司 2024 年年报"),
        ("2024Q3", "2024年第三季度报告"),
        ("2024Q3", "2024 年 3 季度经营数据"),
        ("2024-09-30", "截至 2024年9月30日"),
        ("2024-09-30", "2024-09-30 资产负债表"),
        ("2024年度（2024-01-01至2024-12-31）", "2024 年年度报告"),
        ("2024年度（2024年1—12月）", "2024 年年度报告"),
        ("2024年1—6月", "2024年上半年报告"),
        ("2024年1—6月", "2024年1-6月经营数据"),
    ],
)
def test_period_writings_align_with_the_excerpt(period, excerpt):
    assert period_matches(period, excerpt)


@pytest.mark.parametrize(
    ("period", "excerpt"),
    [
        ("2024年度", "2023年年度报告"),
        ("2024Q3", "2024年第二季度报告"),
        ("2024-09-30", "截至 2024年9月29日"),
        ("2024-09-30", "2024 年年度报告"),
        ("2024年1—6月", "2024年下半年报告"),
        ("2024H1", "2024年第三季度报告"),
    ],
)
def test_a_different_period_still_fails(period, excerpt):
    assert not period_matches(period, excerpt)


def test_blank_period_never_creates_a_gap_and_blank_excerpt_never_matches():
    assert period_matches("", "2024 年年度报告")
    assert period_matches(None, "2024 年年度报告")
    assert not period_matches("2024", "")


def _user(username="alice"):
    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO users(username,password_hash) VALUES(?,?)",
            [username, "synthetic-period-user"],
        )


def _run(run_id):
    _user()
    context = build_research_context(run_id=run_id, now=datetime(2026, 10, 20, 18, tzinfo=UTC))
    create_research_run("alice", "", "研究合成公司 2024 年经营情况", context)


def _seed_original(run_id, excerpt):
    document = {
        "document_id": f"doc_{run_id}",
        "run_id": run_id,
        "url": "https://example.test/annual",
        "title": "示例年报",
        "published_at": "2026-03-18T10:00:00+08:00",
        "publication_precision": "day",
        "original_obtained": True,
        "page_count": 1,
        "gaps": [],
    }
    passage = {
        "passage_id": f"pass_{run_id}_1",
        "run_id": run_id,
        "document_id": document["document_id"],
        "excerpt": excerpt,
        "content_scope": "original_pdf_page",
        "locator": {"kind": "pdf_page", "page": 1, "section": None},
    }
    with get_db() as db:
        db.execute(
            "INSERT INTO research_documents VALUES (?,?,?,?)",
            [document["document_id"], run_id, f"receipt-{run_id}", json.dumps(document, ensure_ascii=False)],
        )
        db.execute(
            "INSERT INTO research_passages VALUES (?,?,?,?)",
            [passage["passage_id"], run_id, document["document_id"], json.dumps(passage, ensure_ascii=False)],
        )
    return passage["passage_id"]


def _period_claim(period):
    # The period/basis checks run for a fact with a value, which is the shape
    # production claims with period_not_in_excerpt had.
    return {
        "kind": "fact",
        "statement": "合成期间的营业收入判断",
        "period": period,
        "value": "100",
        "unit": "亿元",
        "verification": "secondhand",
    }


def test_expanded_period_does_not_raise_a_false_gap():
    _run("run-period-expanded")
    passage_id = _seed_original(
        "run-period-expanded", "贵州茅台酒股份有限公司 2024 年年度报告，营业收入 100 亿元"
    )
    record_claims(
        "alice",
        "run-period-expanded",
        [{**_period_claim("2024年度（2024-01-01至2024-12-31）"), "supports": [passage_id]}],
    )

    finalize_research_run("alice", "run-period-expanded", "2024 年经营情况详见年报。")

    stored = read_research_run("alice", "run-period-expanded")
    claim = next(c for c in stored["claims"] if c["statement"] == "合成期间的营业收入判断")
    assert "period_not_in_excerpt" not in claim["gaps"]


def test_quarter_claim_matches_a_quarter_excerpt():
    _run("run-period-quarter")
    passage_id = _seed_original("run-period-quarter", "公司 2024年第三季度报告已披露营业收入 100 亿元。")
    record_claims(
        "alice",
        "run-period-quarter",
        [{**_period_claim("2024Q3"), "supports": [passage_id]}],
    )

    finalize_research_run("alice", "run-period-quarter", "2024 年三季度经营情况详见季报。")

    stored = read_research_run("alice", "run-period-quarter")
    claim = next(c for c in stored["claims"] if c["statement"] == "合成期间的营业收入判断")
    assert "period_not_in_excerpt" not in claim["gaps"]


def test_a_genuinely_different_period_still_raises_the_gap():
    _run("run-period-mismatch")
    passage_id = _seed_original("run-period-mismatch", "公司 2023 年年度报告，营业收入 100 亿元")
    record_claims(
        "alice",
        "run-period-mismatch",
        [{**_period_claim("2024年度"), "supports": [passage_id]}],
    )

    finalize_research_run("alice", "run-period-mismatch", "2024 年经营情况详见年报。")

    stored = read_research_run("alice", "run-period-mismatch")
    claim = next(c for c in stored["claims"] if c["statement"] == "合成期间的营业收入判断")
    assert "period_not_in_excerpt" in claim["gaps"]
