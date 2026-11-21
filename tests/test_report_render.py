"""Synthetic verification-level rendering checks; no model, credentials or live data."""

import json
from datetime import UTC, datetime

from server.database import get_db
from server.research import (
    create_research_run,
    finalize_research_run,
    read_research_run,
    record_claims,
)
from server.research import ledger as ledger_module
from server.research import render as render_module
from server.research.render import SCHEMA, render_report, unavailable_view
from server.research_context import build_research_context


def _user(username="alice"):
    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO users(username,password_hash) VALUES(?,?)",
            [username, "synthetic-q4-user"],
        )


def _run(run_id="round-q4", question="研究合成公司的 2025 年产能"):
    _user()
    context = build_research_context(run_id=run_id, now=datetime(2026, 10, 20, 18, tzinfo=UTC))
    create_research_run("alice", "", question, context)


def _claim(**extra):
    claim = {"kind": "inference", "statement": "合成数字判断", "limitations": ["合成失效条件"]}
    claim.update(extra)
    return claim


def _seed_document(run_id):
    document = {
        "document_id": "doc_1",
        "run_id": run_id,
        "url": "https://example.test/report",
        "title": "示例公告",
        "published_at": "2026-03-18T10:00:00+08:00",
        "publication_precision": "day",
        "gaps": [],
    }
    passage = {
        "passage_id": "pass_1",
        "run_id": run_id,
        "document_id": "doc_1",
        "excerpt": "2025 年产能为 120 万吨",
        "content_scope": "tool_returned_text",
        "locator": {"kind": "original_section", "section": "产能"},
    }
    with get_db() as db:
        db.execute(
            "INSERT INTO research_documents VALUES (?,?,?,?)",
            ["doc_1", run_id, "receipt-1", json.dumps(document, ensure_ascii=False)],
        )
        db.execute(
            "INSERT INTO research_passages VALUES (?,?,?,?)",
            ["pass_1", run_id, "doc_1", json.dumps(passage, ensure_ascii=False)],
        )


# ---- narrative rendering (pure function) -----------------------------------------------


def test_verified_source_number_gets_marker_and_sentence_source_note():
    view = render_report(
        [
            {
                "value": "120",
                "unit": "万吨",
                "period": "2025",
                "verification": "verified_source",
                "source": "示例公告",
                "published_at": "2026-03-18",
            }
        ],
        "2025 年产能为 120 万吨。",
    )

    rendered = view["narrative"]["text"]
    assert view["status"] == "ok"
    assert view["schema"] == SCHEMA
    assert view["narrative"]["status"] == "rendered"
    assert view["narrative"]["changed"] is True
    assert "120 万吨✅" in rendered
    assert "（来源：示例公告，2026-03-18）" in rendered


def test_secondhand_number_gets_marker_and_secondhand_note():
    view = render_report(
        [{"value": "12.5", "unit": "%", "period": "2025", "verification": "secondhand"}],
        "同比增长 12.5%。",
    )

    rendered = view["narrative"]["text"]
    assert "12.5%◐" in rendered
    assert "（二手来源）" in rendered


def test_inferred_number_gets_marker_and_calculation_note():
    view = render_report(
        [{"value": "9", "unit": "%", "period": "2025", "verification": "inferred"}],
        "净利率约 9%。",
    )

    rendered = view["narrative"]["text"]
    assert "9%≈" in rendered
    assert "（推算）" in rendered


def test_unverified_number_is_replaced_by_pending_phrase():
    view = render_report(
        [{"value": "18", "unit": "%", "verification": "unverified"}],
        "市占率为 18%。",
    )

    rendered = view["narrative"]["text"]
    assert "【待核验：18%】" in rendered
    assert "市占率为 18%" not in rendered


def test_unknown_verification_level_is_treated_as_unverified():
    view = render_report(
        [{"value": "18", "unit": "%", "verification": "trusted"}],
        "市占率为 18%。",
    )

    assert "【待核验：18%】" in view["narrative"]["text"]
    assert view["evidence"]["rows"][0]["verification"] == "unverified"


def test_unmatched_report_number_is_marked_pending():
    view = render_report(
        [
            {
                "value": "120",
                "unit": "万吨",
                "period": "2025",
                "verification": "verified_source",
                "source": "示例公告",
                "published_at": "2026-03-18",
            }
        ],
        "2025 年产能为 120 万吨，但毛利率达到 30.2%。",
    )

    rendered = view["narrative"]["text"]
    assert "120 万吨✅" in rendered
    assert "【待核验：30.2%】" in rendered


def test_years_dates_pages_and_section_numbers_are_not_marked():
    text = "2025 年、2026-10-20、第 12 页、第 4 章第 2 节。"
    view = render_report(
        [{"value": "120", "unit": "万吨", "verification": "verified_source"}],
        text,
    )

    assert view["narrative"]["changed"] is False
    assert view["narrative"]["text"] == text


def test_table_number_gets_marker_without_a_sentence_annotation():
    view = render_report(
        [
            {
                "value": "120",
                "unit": "万吨",
                "verification": "verified_source",
                "source": "示例公告",
                "published_at": "2026-03-18",
            }
        ],
        "| 指标 | 数值 |\n| --- | --- |\n| 产能 | 120 万吨 |",
    )

    rendered = view["narrative"]["text"]
    assert "120✅ 万吨" in rendered
    assert "（来源" not in rendered


def test_report_without_key_numbers_stays_unchanged():
    view = render_report([], "产能利用率处于高位，暂无可核对的关键数字。")

    assert view["status"] == "ok"
    assert view["narrative"]["changed"] is False
    assert view["narrative"]["text"] == "产能利用率处于高位，暂无可核对的关键数字。"
    assert view["evidence"]["total"] == 0


def test_missing_inputs_never_raise():
    view = render_report(None, None)

    assert view["status"] == "ok"
    assert view["narrative"]["text"] == ""
    assert view["evidence"]["total"] == 0
    assert view["evidence"]["coverage"]["verified_claim_ratio"] is None


# ---- evidence list (pure function) ------------------------------------------------------


def test_evidence_lists_every_claim_and_flags_missing_provenance():
    view = render_report(
        [
            {
                "claim_id": "claim-1",
                "statement": "2025 年产能 120 万吨",
                "metric": "设计产能",
                "value": "120",
                "unit": "万吨",
                "period": "2025",
                "verification": "verified_source",
                "source": "示例公告",
                "source_url": "https://example.test/report",
                "published_at": "2026-03-18",
                "scope": "2025 年合并报表",
            },
            {
                "claim_id": "claim-2",
                "statement": "同比增长 12.5%",
                "value": "12.5",
                "unit": "%",
                "period": "2025",
                "verification": "secondhand",
            },
        ],
        "2025 年产能为 120 万吨，同比增长 12.5%。",
    )

    evidence = view["evidence"]
    assert evidence["status"] == "ok"
    assert evidence["total"] == 2
    assert evidence["counts"] == {"verified_source": 1, "secondhand": 1, "inferred": 0, "unverified": 0}
    assert evidence["coverage"]["verified_claim_ratio"] == 0.5
    assert evidence["coverage"]["reached"] is False
    assert evidence["rows"][0]["source"] == "示例公告"
    assert evidence["rows"][0]["published_at"] == "2026-03-18"
    assert evidence["rows"][0]["scope"] == "2025 年合并报表"
    assert evidence["rows"][0]["missing"] == []
    assert evidence["rows"][1]["source"] is None
    assert evidence["rows"][1]["published_at"] is None
    assert evidence["rows"][1]["missing"] == ["source", "published_at"]
    assert evidence["missing"] == {"source": 1, "published_at": 1, "scope": 0}


def test_evidence_falls_back_to_supporting_document_provenance():
    view = render_report(
        [
            {
                "claim_id": "claim-1",
                "statement": "2025 年产能 120 万吨",
                "value": "120",
                "supports": ["pass_1"],
            }
        ],
        "2025 年产能为 120 万吨。",
        documents=[
            {
                "document_id": "doc_1",
                "url": "https://example.test/report",
                "title": "示例公告",
                "published_at": "2026-03-18T10:00:00+08:00",
            }
        ],
        passages=[{"passage_id": "pass_1", "document_id": "doc_1"}],
    )

    row = view["evidence"]["rows"][0]
    assert row["source"] == "示例公告"
    assert row["source_url"] == "https://example.test/report"
    assert row["published_at"] == "2026-03-18"


def test_informal_publication_text_is_treated_as_missing():
    view = render_report(
        [{"value": "120", "unit": "万吨", "published_at": "近期", "source": "示例公告"}],
        "2025 年产能为 120 万吨。",
    )

    row = view["evidence"]["rows"][0]
    assert row["published_at"] is None
    assert "published_at" in row["missing"]


def test_narrative_failure_degrades_to_the_original_text(monkeypatch):
    def boom(_contexts, _text):
        raise RuntimeError("synthetic rendering failure")

    monkeypatch.setattr(render_module, "_render_narrative", boom)
    view = render_report(
        [{"value": "120", "unit": "万吨", "verification": "verified_source"}],
        "2025 年产能为 120 万吨。",
    )

    assert view["status"] == "degraded"
    assert view["narrative"]["status"] == "original"
    assert view["narrative"]["changed"] is False
    assert view["narrative"]["text"] == "2025 年产能为 120 万吨。"
    assert view["evidence"]["status"] == "ok"


def test_evidence_failure_keeps_the_rendered_narrative(monkeypatch):
    def boom(_contexts):
        raise RuntimeError("synthetic evidence failure")

    monkeypatch.setattr(render_module, "_evidence_payload", boom)
    view = render_report(
        [{"value": "120", "unit": "万吨", "verification": "verified_source"}],
        "2025 年产能为 120 万吨。",
    )

    assert view["status"] == "partial"
    assert view["narrative"]["status"] == "rendered"
    assert view["evidence"]["status"] == "unavailable"
    assert view["evidence"]["rows"] == []


def test_unavailable_view_returns_the_raw_report():
    view = unavailable_view("原始报告 120 万吨")

    assert view["status"] == "degraded"
    assert view["narrative"]["status"] == "original"
    assert view["narrative"]["text"] == "原始报告 120 万吨"
    assert view["evidence"]["status"] == "unavailable"
    assert unavailable_view(None)["narrative"]["text"] == ""


# ---- ledger persistence ----------------------------------------------------------------


def test_finalize_adds_verification_render_without_rewriting_the_report():
    _run("run-render")
    record_claims(
        "alice",
        "run-render",
        [
            _claim(
                kind="fact",
                value="120",
                unit="万吨",
                period="2025",
                verification="verified_source",
                source="示例公告",
                published_at="2026-03-18",
            ),
            _claim(kind="calculation", value="12.5", unit="%", period="2025", verification="secondhand"),
        ],
    )
    text = "2025 年产能为 120 万吨，同比增长 12.5%。"
    revision = finalize_research_run("alice", "run-render", text)

    assert revision["narrative"] == text
    view = revision["verification_render"]
    assert view["schema"] == SCHEMA
    assert view["status"] == "ok"
    assert view["narrative"]["text"] != text
    assert "120 万吨✅" in view["narrative"]["text"]
    assert view["evidence"]["total"] == 2
    assert view["evidence"]["counts"]["verified_source"] == 1

    stored = read_research_run("alice", "run-render")
    assert stored["report_revisions"][0] == revision
    assert stored["report_revisions"][0]["narrative"] == text


def test_evidence_uses_the_cited_document_when_the_claim_omits_provenance():
    _run("run-doc")
    _seed_document("run-doc")
    record_claims(
        "alice",
        "run-doc",
        [
            _claim(
                kind="fact",
                value="120",
                unit="万吨",
                period="2025",
                supports=["pass_1"],
                verification="verified_source",
            )
        ],
    )
    revision = finalize_research_run("alice", "run-doc", "2025 年产能为 120 万吨。")

    row = revision["verification_render"]["evidence"]["rows"][0]
    assert row["source"] == "示例公告"
    assert row["source_url"] == "https://example.test/report"
    assert row["published_at"] == "2026-03-18"
    assert row["scope"] == "2025"


def test_claim_provenance_fields_survive_the_ledger_round_trip():
    _run("run-provenance")
    result = record_claims(
        "alice",
        "run-provenance",
        [
            _claim(
                kind="inference",
                value="120",
                unit="万吨",
                period="2025",
                verification="verified_source",
                source="示例公告",
                published_at="2026-03-18",
                scope="2025 年合并报表",
            )
        ],
    )

    claim = result["claims"][0]
    assert claim["source"] == "示例公告"
    assert claim["published_at"] == "2026-03-18"
    assert claim["scope"] == "2025 年合并报表"
    stored = read_research_run("alice", "run-provenance")
    assert stored["claims"][0]["source"] == "示例公告"
    assert stored["claims"][0]["published_at"] == "2026-03-18"


def test_finalize_degrades_the_render_but_keeps_the_report(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("synthetic render failure")

    monkeypatch.setattr(ledger_module, "render_report", boom)
    _run("run-degraded-render")
    record_claims(
        "alice",
        "run-degraded-render",
        [_claim(kind="fact", value="120", unit="万吨", period="2025", verification="verified_source")],
    )
    text = "2025 年产能为 120 万吨。"
    revision = finalize_research_run("alice", "run-degraded-render", text)

    view = revision["verification_render"]
    assert view["status"] == "degraded"
    assert view["narrative"]["status"] == "original"
    assert view["narrative"]["text"] == text
    assert view["evidence"]["status"] == "unavailable"
    assert revision["narrative"] == text
    assert revision["acceptance_status"] in {"completed", "partial"}
    assert read_research_run("alice", "run-degraded-render")["report_revisions"][0] == revision
