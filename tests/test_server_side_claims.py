"""Server-side claim registration: the model claim pass is an upgrade, not a prerequisite.

Production saw two identical research requests where only one registered claims: the
run without a model claim call reported every number as unsupported. These tests pin
the fixed server behavior: the ledger extracts the report body's numbers itself and
the model's claim pass can only upgrade them.
"""

import json
from datetime import UTC, datetime

from server.database import get_db
from server.research import (
    create_research_run,
    finalize_research_run,
    read_research_run,
    record_claims,
    register_report_claims,
)
from server.research_context import build_research_context

# A production-shaped research body with 18 report numbers across amounts, growth
# rates, ratios and quantities; every one was unregistered in the failing run.
REPORT = (
    "2025 年公司实现营业收入 128.6 亿元，同比增长 18.2%；归母净利润 15.4 亿元，同比增长 22.5%；"
    "毛利率 31.5%；经营活动现金流净额 19.8 亿元；资产负债率 42.3%；ROE 14.6%；"
    "研发投入 9.2 亿元；产能 320 万吨；产量 285 万吨；产能利用率 89.1%；"
    "单位成本 3200 元/吨；出口量 46 万吨；海外收入 38.5 亿元；市占率 16.8%；"
    "在手订单 12 万片；续约率 92.4%。"
)

# Four of those numbers are present verbatim on one original-document page so the
# model can upgrade exactly those claims to verified_source.
PAGE_EXCERPT = "2025 年产能为 320 万吨，营业收入 128.6 亿元，毛利率 31.5%，ROE 为 14.6%。"


def _user(username="alice"):
    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO users(username,password_hash) VALUES(?,?)",
            [username, "synthetic-q3-user"],
        )


def _run(run_id, question="研究合成公司的 2025 年产能与经营数据"):
    _user()
    context = build_research_context(run_id=run_id, now=datetime(2026, 10, 20, 18, tzinfo=UTC))
    create_research_run("alice", "", question, context)


def _seed_original(run_id, excerpt=PAGE_EXCERPT):
    document = {
        "document_id": f"doc_{run_id}",
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
        "passage_id": f"pass_{run_id}",
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
    return document, passage


def _upgrade(statement, quote, value, unit, period="2025", *, document, passage):
    """A model claim that should upgrade an already server-extracted number."""
    return {
        "kind": "fact",
        "statement": statement,
        "supports": [passage["passage_id"]],
        "quotes": {passage["passage_id"]: quote},
        "period": period,
        "value": value,
        "unit": unit,
        "verification": "verified_source",
        "source_locator": {"document_id": document["document_id"], "page": 1},
    }


# ---- test 1: the model registers nothing -----------------------------------------------


def test_finalize_registers_every_report_number_without_model_claims():
    _run("run-server-auto")
    # The report states financial numbers, so the hard evidence gate needs an
    # obtained original to publish it; the registration behavior under test is
    # independent of that requirement.
    _seed_original("run-server-auto")
    revision = finalize_research_run("alice", "run-server-auto", REPORT)

    check = revision["claim_reconciliation"]
    assert check["status"] == "ok"
    assert check["report_numbers_checked"] == 18
    assert check["claims_checked"] == 18
    assert check["claims_registered"] == 18
    assert check["claims_registered"] >= check["report_numbers_checked"]
    assert check["unsupported_claims"] == 0
    assert check["unsupported_numbers"] == []
    assert check["coverage"] == 1.0
    # The server locates every number that sits verbatim on the obtained original
    # page — the two amounts/quantities and, now, the two percentages — so the model
    # was not needed for the association.
    assert check["verified_claim_ratio"] == round(4 / 18, 4)
    assert check["claims_checked"] + check["unsupported_claims"] == check["report_numbers_checked"]

    stored = read_research_run("alice", "run-server-auto")
    claims = stored["claims"]
    assert len(claims) == 18
    located = [claim for claim in claims if claim["origin"] == "server_locator"]
    assert len(located) == 4
    assert sorted(claim["statement"] for claim in located) == sorted(
        "报告正文数字：" + display for display in ("128.6 亿元", "320 万吨", "31.5%", "14.6%")
    )
    assert located[0]["verification"] == "verified_source"
    assert located[0]["source_locator"] == {
        "document_id": "doc_run-server-auto",
        "page": 1,
    }
    assert "locator_attempt" not in located[0]
    # The locator attaches the page it found as the claim's support too, so the
    # associated number is a complete citation instead of staying partial until
    # a model pass happens to support it.
    assert located[0]["supports"] == ["pass_run-server-auto"]
    assert located[0]["citation_status"] == "valid"
    assert "support_missing" not in located[0]["gaps"]
    # Every other financial number is absent from the page: the level stays and
    # the attempt is recorded for the next upgrade round.
    missed = [claim for claim in claims if claim.get("locator_attempt") == "not_found"]
    assert len(missed) == 14
    for claim in missed:
        assert claim["origin"] == "server_extract"
        assert claim["verification"] == "unverified"
        assert claim["source_locator"] is None
    for claim in claims:
        assert claim["source"] == ""
        assert claim["semantic_status"] == "unreviewed"
        if claim["origin"] != "server_locator":
            assert claim["citation_status"] == "partial"
            assert "support_missing" in claim["gaps"]
        assert claim["statement"].startswith("报告正文数字：")
        assert len(claim["claim_key"]) == 3
    # The extracted claims reach the verification render: numbers are visible as
    # pending, not silently certified.
    rendered = json.dumps(revision["verification_render"], ensure_ascii=False)
    assert "待核验" in rendered
    assert "128.6" in rendered


# ---- test 2: the model upgrades a subset afterwards -------------------------------------


def test_model_claims_upgrade_server_extracted_numbers_in_place():
    _run("run-server-upgrade")
    document, passage = _seed_original("run-server-upgrade")

    registered = register_report_claims("alice", "run-server-upgrade", REPORT)
    assert len(registered["claims"]) == 18
    assert registered["claims_registered"] == 18
    original_claims = {tuple(claim["claim_key"]): claim["claim_id"] for claim in registered["claims"]}
    # The explicit helper is idempotent: the numbers are already registered.
    again = register_report_claims("alice", "run-server-upgrade", REPORT)
    assert again["claims"] == []
    assert again["claims_registered"] == 18

    upgrades = [
        _upgrade(
            "2025 年产能为 320 万吨",
            "2025 年产能为 320 万吨",
            "320",
            "万吨",
            document=document,
            passage=passage,
        ),
        _upgrade(
            "营业收入 128.6 亿元",
            "营业收入 128.6 亿元",
            "128.6",
            "亿元",
            document=document,
            passage=passage,
        ),
        _upgrade(
            "毛利率 31.5%",
            "毛利率 31.5%",
            "31.5",
            "%",
            document=document,
            passage=passage,
        ),
        _upgrade("ROE 为 14.6%", "ROE 为 14.6%", "14.6", "%", document=document, passage=passage),
    ]
    upgraded = record_claims("alice", "run-server-upgrade", upgrades)["claims"]

    stored = read_research_run("alice", "run-server-upgrade")["claims"]
    assert len(stored) == 18
    assert [claim["verification"] for claim in upgraded] == ["verified_source"] * 4
    # Upgrading writes into the existing claims: no duplicate rows, same ids.
    keys = [claim["claim_key"][0] for claim in upgraded]
    assert len({claim["claim_id"] for claim in stored}) == 18
    for claim in upgraded:
        assert claim["claim_id"] == original_claims[tuple(claim["claim_key"])]
        assert claim["origin"] == "server_extract"
        assert claim["verification"] == "verified_source"
    assert sorted({claim["verification"] for claim in stored}) == ["unverified", "verified_source"]
    assert sum(claim["verification"] == "verified_source" for claim in stored) == 4
    assert sum(claim["verification"] == "unverified" for claim in stored) == 14
    assert keys  # the four upgraded keys are the extracted measurement keys

    revision = finalize_research_run("alice", "run-server-upgrade", REPORT)
    check = revision["claim_reconciliation"]
    assert check["status"] == "ok"
    assert check["report_numbers_checked"] == 18
    assert check["claims_registered"] == 18
    assert check["claims_checked"] == 18
    assert check["unsupported_claims"] == 0
    assert check["coverage"] == 1.0
    # The ratio counts verified claims over all claims: verified/total, not the
    # covered report numbers.
    assert check["verified_claim_ratio"] == round(4 / 18, 4)


# ---- test 3: duplicate model registration ----------------------------------------------


def test_duplicate_model_registration_updates_instead_of_inserting():
    _run("run-server-duplicate")
    assert (
        len(register_report_claims("alice", "run-server-duplicate", "2025 年产能为 120 万吨。")["claims"])
        == 1
    )
    initial = read_research_run("alice", "run-server-duplicate")["claims"]
    assert len(initial) == 1

    duplicate = {
        "kind": "fact",
        "statement": "2025 年产能为 120 万吨",
        "period": "2025",
        "value": "120",
        "unit": "万吨",
        "source": "示例年报",
        "verification": "secondhand",
    }
    first = record_claims("alice", "run-server-duplicate", [duplicate])["claims"]
    # A later duplicate without provenance must not downgrade or erase the upgrade.
    plain = {
        "kind": "fact",
        "statement": "2025 年产能为 120 万吨",
        "period": "2025",
        "value": "120",
        "unit": "万吨",
    }
    second = record_claims("alice", "run-server-duplicate", [plain])["claims"]

    stored = read_research_run("alice", "run-server-duplicate")["claims"]
    assert len(stored) == 1
    assert [claim["claim_id"] for claim in first] == [initial[0]["claim_id"]]
    assert [claim["claim_id"] for claim in second] == [initial[0]["claim_id"]]
    assert stored[0]["verification"] == "secondhand"
    assert stored[0]["source"] == "示例年报"
    assert stored[0]["origin"] == "server_extract"
    assert stored[0]["claim_key"] == ["mass_t", None, "1200000"]

    revision = finalize_research_run("alice", "run-server-duplicate", "2025 年产能为 120 万吨。")
    check = revision["claim_reconciliation"]
    assert check["claims_registered"] == 1
    assert check["claims_checked"] == 1
    assert check["unsupported_claims"] == 0
