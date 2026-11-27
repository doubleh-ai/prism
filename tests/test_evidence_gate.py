"""Hard evidence gate: financial/trading numbers require an original document.

The server withholds the report body when a run states financial or trading
numbers but never obtained an original document (``original_obtained=true``).
These tests pin the classification function, the blocked revision, the
first-hand path when an original exists, and the policy-only path that must not
be hit by the gate.
"""

import json
from datetime import UTC, datetime

import pytest
from test_native_agent_runtime_review import done, execute
from test_native_agent_runtime_review import engine as engine

from server.agent import runtime
from server.database import get_db
from server.research import (
    create_research_run,
    finalize_research_run,
    read_research_run,
    record_claims,
)
from server.research.evidence_gate import (
    claim_involves_financial_number,
    original_document_obtained,
    original_evidence_missing,
    report_needs_original,
)
from server.research_context import build_research_context

USER = "evidence-gate-owner"


def _user():
    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO users(username,password_hash) VALUES(?,?)",
            [USER, "synthetic-evidence-gate-user"],
        )


def _run(run_id, question="研究合成公司的 2025 年营业收入"):
    _user()
    context = build_research_context(run_id=run_id, now=datetime(2026, 10, 20, 18, tzinfo=UTC))
    create_research_run(USER, "", question, context)
    return run_id


def _insert_document(run_id, document, passages):
    with get_db() as db:
        db.execute(
            "INSERT INTO research_documents VALUES (?,?,?,?)",
            [
                document["document_id"],
                run_id,
                f"receipt-seed-{document['document_id']}",
                json.dumps(document, ensure_ascii=False),
            ],
        )
        for passage in passages:
            db.execute(
                "INSERT INTO research_passages VALUES (?,?,?,?)",
                [
                    passage["passage_id"],
                    run_id,
                    passage["document_id"],
                    json.dumps(passage, ensure_ascii=False),
                ],
            )


def _seed_web_text(run_id, excerpt):
    """A tool-returned web page: readable, but not the original announcement."""

    document_id = f"doc_web_{run_id}"
    passage_id = f"pass_web_1_{run_id}"
    document = {
        "document_id": document_id,
        "run_id": run_id,
        "url": "https://example.test/synthetic-news",
        "title": "合成资讯",
        "published_at": "2026-03-18T10:00:00+08:00",
        "publication_precision": "day",
        "content_scope": "tool_returned_text",
        "original_obtained": False,
        "page_count": None,
        "gaps": [],
    }
    passage = {
        "passage_id": passage_id,
        "run_id": run_id,
        "document_id": document_id,
        "excerpt": excerpt,
        "content_scope": "tool_returned_text",
        "locator": {"kind": "web_text", "section": None},
    }
    _insert_document(run_id, document, [passage])
    return passage_id


def _seed_original_page(run_id, excerpt):
    """One server-owned original PDF page."""

    document_id = f"doc_original_{run_id}"
    passage_id = f"pass_original_1_{run_id}"
    document = {
        "document_id": document_id,
        "run_id": run_id,
        "url": "https://example.test/annual",
        "title": "示例年报",
        "published_at": "2026-03-18T10:00:00+08:00",
        "publication_precision": "day",
        "content_scope": "original_pdf",
        "original_obtained": True,
        "page_count": 3,
        "gaps": [],
    }
    passage = {
        "passage_id": passage_id,
        "run_id": run_id,
        "document_id": document_id,
        "excerpt": excerpt,
        "content_scope": "original_pdf_page",
        "locator": {"kind": "pdf_page", "page": 1, "section": None},
    }
    _insert_document(run_id, document, [passage])
    return document_id, passage_id


def _financial_claim(passage_id, excerpt):
    return {
        "kind": "fact",
        "statement": "2025 年甲公司营业收入为 120 亿元",
        "supports": [passage_id],
        "quotes": {passage_id: excerpt},
        "entities": ["甲公司"],
        "metric": "营业收入",
        "period": "2025",
        "value": "120",
        "unit": "亿元",
        "verification": "secondhand",
    }


# ---- classification: which claims carry financial or trading numbers --------

FINANCIAL_CLAIMS = [
    {"kind": "fact", "statement": "营业收入为120亿元", "value": "120", "unit": "亿元"},
    {"kind": "fact", "statement": "营业收入为120万元", "value": "120", "unit": "万元"},
    {"kind": "fact", "statement": "营业收入为120000元", "value": "120000", "unit": "元"},
    {"kind": "fact", "statement": "金额为120", "value": "120", "currency": "CNY"},
    {"kind": "fact", "statement": "吨价为120元/吨", "value": "120", "unit": "元/吨"},
    {"kind": "fact", "statement": "每股收益为1.2元/股", "value": "1.2", "unit": "元/股"},
    {"kind": "fact", "statement": "成交量为5000手", "value": "5000", "unit": "手"},
    {"kind": "fact", "statement": "回购1200万股", "value": "1200", "unit": "万股"},
    {"kind": "inference", "statement": "年报披露归母净利润为1.2亿港元", "limitations": []},
    {"kind": "inference", "statement": "成交金额为120 USD", "limitations": []},
    {"kind": "inference", "statement": "公司拟以1,200万元收购标的资产", "limitations": []},
]

NON_FINANCIAL_CLAIMS = [
    {"kind": "fact", "statement": "设计产能为120万吨", "value": "120", "unit": "万吨"},
    {"kind": "fact", "statement": "设计产能为120吨/年", "value": "120", "unit": "吨/年"},
    {"kind": "fact", "statement": "同比增速为12.5%", "value": "12.5", "unit": "%"},
    {"kind": "fact", "statement": "利差变化5bp", "value": "5", "unit": "bp"},
    {"kind": "fact", "statement": "装机目标为1.2亿千瓦", "value": "1.2", "unit": "亿千瓦"},
    {"kind": "inference", "statement": "规划要求2027年装机达到1.2亿千瓦，年均增速5%", "limitations": []},
    {"kind": "inference", "statement": "政策要求加快建设储能与电网调节能力。", "limitations": []},
    {"kind": "inference", "statement": "2025年、2026年与第3页的时间线仍待核对。", "limitations": []},
    {"kind": "inference", "statement": "第 3 页提到产能扩张。", "limitations": []},
    {"kind": "inference", "statement": "合成判断", "limitations": ["合成失效条件"]},
]


@pytest.mark.parametrize("claim", FINANCIAL_CLAIMS)
def test_claim_with_financial_or_trading_number_is_detected(claim):
    assert claim_involves_financial_number(claim) is True


@pytest.mark.parametrize("claim", NON_FINANCIAL_CLAIMS)
def test_claim_without_financial_number_is_ignored(claim):
    assert claim_involves_financial_number(claim) is False


def test_non_dict_claim_is_not_financial():
    assert claim_involves_financial_number("营业收入120亿元") is False
    assert claim_involves_financial_number(None) is False


def test_gate_helpers_combine_claims_and_documents():
    financial = {"kind": "fact", "statement": "营业收入为120亿元", "value": "120", "unit": "亿元"}
    policy = {"kind": "fact", "statement": "装机目标为1.2亿千瓦", "value": "1.2", "unit": "亿千瓦"}

    assert report_needs_original([financial]) is True
    assert report_needs_original([policy]) is False
    assert report_needs_original([]) is False
    assert original_document_obtained([{"original_obtained": True}]) is True
    assert original_document_obtained([{"original_obtained": False}]) is False

    assert original_evidence_missing([financial], []) is True
    assert original_evidence_missing([financial], [{"original_obtained": False}]) is True
    assert original_evidence_missing([financial], [{"original_obtained": True}]) is False
    assert original_evidence_missing([policy], []) is False


# ---- finalization: blocked without an original, published with one ----------


def test_finalize_withholds_report_when_financial_claim_has_no_original():
    run_id = _run("gate-blocked")
    excerpt = "2025年甲公司营业收入为120亿元。"
    passage_id = _seed_web_text(run_id, excerpt)
    record_claims(USER, run_id, [_financial_claim(passage_id, excerpt)])

    revision = finalize_research_run(USER, run_id, excerpt)

    assert revision["acceptance_status"] == "blocked"
    assert revision["error_code"] == "original_required"
    assert revision["narrative"] == ""
    assert revision["narrative_status"] == "evidence_gate_blocked"
    assert "original_source_missing" in revision["gaps"]
    gate = revision["evidence_gate"]
    assert gate["status"] == "blocked"
    assert gate["error_code"] == "original_required"
    assert gate["required"] == "original_document"
    assert gate["report_withheld"] is True
    assert gate["claims_checked"] == 1
    assert gate["financial_claims"] == 1
    assert gate["documents_retained"] == 1
    assert gate["original_documents"] == 0
    assert [block["kind"] for block in revision["blocks"]] == ["boundary", "gaps"]
    assert revision["claim_reconciliation"] == {
        "schema": "prism.claim-reconciliation.v1",
        "status": "unavailable",
    }

    saved = read_research_run(USER, run_id)
    assert saved["run"]["status"] == "blocked"
    # The fetched documents stay readable so the next attempt can reuse them.
    assert len(saved["documents"]) == 1
    assert saved["documents"][0]["original_obtained"] is False
    assert len(saved["claims"]) == 1
    assert saved["report_revisions"] == [revision]

    # Idempotent: a second finalize returns the same frozen revision.
    assert finalize_research_run(USER, run_id, excerpt) == revision


def test_failed_run_without_draft_keeps_failed_status():
    """No report body existed, so the gate does not relabel an engine failure."""

    run_id = _run("gate-failed")
    excerpt = "2025年甲公司营业收入为120亿元。"
    passage_id = _seed_web_text(run_id, excerpt)
    record_claims(USER, run_id, [_financial_claim(passage_id, excerpt)])

    revision = finalize_research_run(USER, run_id, "", error=True)

    assert revision["acceptance_status"] == "failed"
    assert revision["narrative"] == ""
    assert "evidence_gate" not in revision
    assert "engine_failed" in revision["gaps"]


def test_finalize_publishes_report_when_original_document_exists():
    run_id = _run("gate-original")
    excerpt = "2025年甲公司营业收入为120亿元。"
    document_id, passage_id = _seed_original_page(run_id, excerpt)
    claim = _financial_claim(passage_id, excerpt)
    claim["verification"] = "verified_source"
    claim["source_locator"] = {"document_id": document_id, "page": 1}

    recorded = record_claims(USER, run_id, [claim])
    assert recorded["claims"][0]["verification"] == "verified_source"

    revision = finalize_research_run(USER, run_id, excerpt)

    assert revision["acceptance_status"] in {"completed", "partial"}
    assert revision["narrative"] == excerpt
    assert revision["claim_reconciliation"]["status"] == "ok"
    assert revision["claim_reconciliation"]["verified_claim_ratio"] == 1.0
    assert "evidence_gate" not in revision
    assert "original_source_missing" not in revision["gaps"]
    saved = read_research_run(USER, run_id)
    assert saved["claims"][0]["verification"] == "verified_source"


def test_policy_only_secondhand_report_is_not_blocked():
    run_id = _run("gate-policy", question="解读合成储能政策对装机目标的要求")
    excerpt = "政策要求2027年装机达到1.2亿千瓦，年均增速5%。"
    passage_id = _seed_web_text(run_id, excerpt)
    record_claims(
        USER,
        run_id,
        [
            {
                "kind": "fact",
                "statement": "政策要求2027年装机达到1.2亿千瓦",
                "supports": [passage_id],
                "quotes": {passage_id: "政策要求2027年装机达到1.2亿千瓦"},
                "period": "2027",
                "value": "1.2",
                "unit": "亿千瓦",
                "verification": "secondhand",
            },
            {
                "kind": "fact",
                "statement": "政策提出年均增速5%",
                "supports": [passage_id],
                "quotes": {passage_id: "年均增速5%"},
                "period": "2027",
                "value": "5",
                "unit": "%",
                "verification": "secondhand",
            },
        ],
    )

    revision = finalize_research_run(USER, run_id, excerpt)

    assert revision["acceptance_status"] != "blocked"
    assert revision["acceptance_status"] in {"completed", "partial"}
    assert revision["narrative"] == excerpt
    assert "original_source_missing" not in revision["gaps"]
    assert "evidence_gate" not in revision


# ---- secondhand financial claims: verification level does not matter -------


def _secondhand_claim(passage_id, sentence, *, value, unit, period="2025", metric="指标"):
    return {
        "kind": "fact",
        "statement": sentence.rstrip("。"),
        "supports": [passage_id],
        "quotes": {passage_id: sentence},
        "entities": ["甲公司"],
        "metric": metric,
        "period": period,
        "value": value,
        "unit": unit,
        "verification": "secondhand",
    }


def test_ten_secondhand_financial_claims_without_original_are_blocked():
    """The batch-fourteen shape: a report that only cites web text is withheld."""

    run_id = _run("gate-secondhand-ten")
    amounts = [101, 102, 103, 104, 105, 106, 107, 108, 109, 110]
    sentences = [f"2025年甲公司指标{i}为{amount}亿元。" for i, amount in enumerate(amounts, start=1)]
    excerpt = "".join(sentences)
    passage_id = _seed_web_text(run_id, excerpt)
    claims = [
        _secondhand_claim(passage_id, sentence, value=str(amount), unit="亿元", metric=f"指标{i}")
        for i, (sentence, amount) in enumerate(zip(sentences, amounts, strict=True), start=1)
    ]
    recorded = record_claims(USER, run_id, claims)
    assert len(recorded["claims"]) == 10
    assert {claim["verification"] for claim in recorded["claims"]} == {"secondhand"}

    revision = finalize_research_run(USER, run_id, excerpt)

    assert revision["acceptance_status"] == "blocked"
    assert revision["error_code"] == "original_required"
    assert revision["narrative"] == ""
    assert revision["narrative_status"] == "evidence_gate_blocked"
    assert "original_source_missing" in revision["gaps"]
    gate = revision["evidence_gate"]
    assert gate["status"] == "blocked"
    assert gate["reason"] == "original_source_missing"
    assert gate["report_withheld"] is True
    assert gate["claims_checked"] == 10
    assert gate["financial_claims"] == 10
    assert gate["documents_retained"] == 1
    assert gate["original_documents"] == 0
    saved = read_research_run(USER, run_id)
    assert len(saved["claims"]) == 10
    assert saved["run"]["status"] == "blocked"


def test_original_document_allows_secondhand_claim_upgrade_and_publication():
    """With an obtained original the same report publishes and claims upgrade."""

    run_id = _run("gate-secondhand-upgrade")
    excerpt = "2025年甲公司营业收入为120亿元。"
    document_id, passage_id = _seed_original_page(run_id, excerpt)
    web_passage_id = _seed_web_text(run_id, excerpt)

    recorded = record_claims(USER, run_id, [_financial_claim(web_passage_id, excerpt)])
    secondhand = recorded["claims"][0]
    assert secondhand["verification"] == "secondhand"
    assert secondhand.get("origin") != "server_extract"

    upgrade = _financial_claim(passage_id, excerpt)
    upgrade["verification"] = "verified_source"
    upgrade["source_locator"] = {"document_id": document_id, "page": 1}
    upgraded = record_claims(USER, run_id, [upgrade])["claims"][0]
    assert upgraded["claim_id"] == secondhand["claim_id"]
    assert upgraded["verification"] == "verified_source"

    revision = finalize_research_run(USER, run_id, excerpt)

    assert revision["acceptance_status"] in {"completed", "partial"}
    assert revision["narrative"] == excerpt
    assert "evidence_gate" not in revision
    assert "original_source_missing" not in revision["gaps"]
    check = revision["claim_reconciliation"]
    assert check["status"] == "ok"
    assert check["claims_checked"] == 1
    assert check["verified_claim_ratio"] == 1.0


def test_mixed_financial_and_policy_claims_without_original_are_blocked():
    """One financial number is enough; the policy claims do not exempt the report."""

    run_id = _run("gate-mixed", question="解读合成储能政策并核对甲公司 2025 年营业收入")
    policy = "政策要求2027年装机达到1.2亿千瓦。"
    financial = "2025年甲公司营业收入为120亿元。"
    excerpt = policy + financial
    passage_id = _seed_web_text(run_id, excerpt)
    record_claims(
        USER,
        run_id,
        [
            _secondhand_claim(passage_id, policy, value="1.2", unit="亿千瓦", period="2027", metric="装机"),
            _secondhand_claim(passage_id, financial, value="120", unit="亿元", metric="营业收入"),
        ],
    )

    revision = finalize_research_run(USER, run_id, excerpt)

    assert revision["acceptance_status"] == "blocked"
    assert revision["error_code"] == "original_required"
    assert revision["narrative"] == ""
    assert "original_source_missing" in revision["gaps"]
    gate = revision["evidence_gate"]
    assert gate["claims_checked"] == 2
    assert gate["financial_claims"] == 1
    assert gate["original_documents"] == 0


def test_report_body_financial_numbers_are_gated_without_registered_claims():
    """The report body is classified server-side; no claim registration is needed."""

    run_id = _run("gate-body-only")
    excerpt = "2025年甲公司营业收入为120亿元，同比增长8%。"
    _seed_web_text(run_id, excerpt)
    assert read_research_run(USER, run_id)["claims"] == []

    revision = finalize_research_run(USER, run_id, excerpt)

    assert revision["acceptance_status"] == "blocked"
    assert revision["error_code"] == "original_required"
    assert revision["narrative"] == ""
    gate = revision["evidence_gate"]
    assert gate["report_withheld"] is True
    assert gate["original_documents"] == 0
    # The server registered the report number before the gate, so the withheld
    # revision still carries the extracted claim instead of losing the number.
    saved = read_research_run(USER, run_id)
    assert saved["claims"]
    assert {claim["origin"] for claim in saved["claims"]} == {"server_extract"}
    assert gate["claims_checked"] == len(saved["claims"])
    assert gate["financial_claims"] >= 1


def test_gate_classifies_report_body_numbers_without_claims():
    financial = "2025年甲公司营业收入为120亿元。"
    policy = "政策要求2027年装机达到1.2亿千瓦。"

    assert report_needs_original([], financial) is True
    assert report_needs_original([], policy) is False
    assert report_needs_original([], "") is False
    assert original_evidence_missing([], [], financial) is True
    assert original_evidence_missing([], [{"original_obtained": True}], financial) is False
    assert original_evidence_missing([], [], policy) is False


# ---- runtime: the withheld body never reaches the persisted message ---------


async def test_runtime_replaces_blocked_report_with_evidence_notice(engine, monkeypatch):
    from server.research import ledger
    from server.research_context import EvidenceTracker

    run_id = engine.run["run_id"]
    ledger.create_research_run(
        engine.user,
        engine.conv,
        engine.run["question"],
        runtime.build_research_context(run_id=run_id, engine="prism-native"),
    )
    excerpt = "2025年甲公司营业收入为120亿元。"
    tracker = EvidenceTracker(run_id)
    call = {
        "id": "gate-source-call",
        "name": "WebFetch",
        "input": {"url": "https://www.sse.com.cn/synthetic-gate"},
    }
    tracker.record_call(call)
    block = {"tool_use_id": call["id"], "content": excerpt}
    refs = tracker.record_result(block)
    ledger.ingest_tool_result(engine.user, run_id, call, block, refs)
    count = []

    async def script(messages, **kwargs):
        count.append(True)
        if len(count) == 1:
            name, arguments = "research_read", {}
        elif len(count) == 2:
            result = json.loads(messages[-1]["content"][0]["content"])
            passages = result["data"]["passages"]
            claim = _financial_claim(passages[0]["passage_id"], passages[0]["excerpt"])
            name, arguments = "research_record_claims", {"claims": [claim]}
        else:
            yield {"type": "text_delta", "text": excerpt}
            yield done()
            return
        call_id = "gate-native-" + str(len(count))
        yield {"type": "tool_call", "id": call_id, "name": name, "arguments": arguments}
        yield done(
            "tool_use",
            content=[{"type": "tool_use", "id": call_id, "name": name, "input": arguments}],
        )

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)

    assert len(count) == 3
    assert outcome.text.startswith("证据不足")
    assert excerpt not in outcome.text
    assert outcome.error_code == "original_required"

    payload = json.loads(outcome.message_payload)
    assert payload["text"] == outcome.text
    assert payload["chunks"] == [{"t": "text", "c": outcome.text}]
    assert payload["report_status"] == "blocked"
    assert payload["research"]["status"] == "blocked"
    assert payload["research"]["error_code"] == "original_required"
    assert payload["research"]["evidence_gate"]["documents_retained"] == 1

    with get_db() as db:
        saved_message = db.execute(
            "SELECT content FROM messages WHERE id=?", [outcome.message_id]
        ).fetchone()[0]
    assert saved_message == outcome.message_payload

    saved = ledger.read_research_run(engine.user, run_id)
    revision = saved["report_revisions"][-1]
    assert revision["acceptance_status"] == "blocked"
    assert revision["error_code"] == "original_required"
    assert revision["narrative"] == ""
    assert "original_source_missing" in revision["gaps"]
    assert len(saved["documents"]) == 1
    assert len(saved["claims"]) == 1
