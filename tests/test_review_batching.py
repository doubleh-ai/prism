"""Excerpt review runs in bounded claim batches with a per-batch timeout.

A slow provider used to lose the whole semantic review: one 30-second call had
to cover every claim. These tests pin the bounded batches (at most six claims
each), the merged record that keeps completed assessments next to the timed-out
claim IDs, and the claim-level ``unverified_review_timeout`` gap instead of a
whole-run ``model_review_incomplete``.
"""

import asyncio
import json

from test_native_agent_runtime_review import done, events, execute, last_usage
from test_native_agent_runtime_review import engine as engine
from test_server_side_claims import _run

from server.agent import runtime
from server.database import get_db
from server.research import finalize_research_run, ledger, model_review, read_research_run, record_claims


def _seed_reviewable(user, run_id, values):
    """Insert one original document and one eligible excerpt per claim value."""
    document = {
        "document_id": f"doc_{run_id}",
        "run_id": run_id,
        "url": "https://example.test/annual",
        "title": "示例年报",
        "published_at": "2026-03-18T10:00:00+08:00",
        "publication_precision": "day",
        "original_obtained": True,
        "page_count": 3,
        "retrieved_body_sha256": "a" * 64,
        "gaps": [],
    }
    passages = []
    claims = []
    for index, value in enumerate(values):
        excerpt = f"2025 年产能为 {value} 万吨。"
        passage = {
            "passage_id": f"pass_{run_id}_{index}",
            "run_id": run_id,
            "document_id": document["document_id"],
            "excerpt": excerpt,
            "content_scope": "original_pdf_page",
            "locator": {"kind": "pdf_page", "page": 1, "section": None},
        }
        passages.append(passage)
        claims.append(
            {
                "kind": "fact",
                "statement": excerpt,
                "supports": [passage["passage_id"]],
                "quotes": {passage["passage_id"]: excerpt},
                "period": "2025",
                "value": value,
                "unit": "万吨",
            }
        )
    with get_db() as db:
        db.execute(
            "INSERT INTO research_documents VALUES (?,?,?,?)",
            [
                document["document_id"],
                run_id,
                f"receipt-{run_id}",
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
    stored = record_claims(user, run_id, claims)["claims"]
    assert all(claim["citation_status"] == "valid" for claim in stored)
    return document, passages, stored


def _response(packet):
    return {
        "packet_id": packet["packet_id"],
        "assessments": [
            {
                "claim_id": claim["claim_id"],
                "verdict": "supported",
                "reason": "节选与结论表述一致。",
                "evidence_ids": [claim["sources"][0]["passage_id"]],
                "history_verdict": "not_compared",
                "history_claim_ids": [],
            }
            for claim in packet["claims"]
        ],
    }


def _assessments(packet):
    return model_review.parse(packet, json.dumps(_response(packet), ensure_ascii=False))["assessments"]


def test_prepare_batches_chunks_claims_and_keeps_one_packet_for_prepare():
    run_id = "run-review-chunks"
    _run(run_id)
    values = ["700", "710", "720", "730", "740", "750", "760"]
    _seed_reviewable("alice", run_id, values)

    single = model_review.prepare("alice", run_id)
    assert single["status"] == "ready"
    assert single["claim_count"] == 7
    assert len(single["packet"]["claims"]) == 7

    prepared = model_review.prepare_batches("alice", run_id, batch_size=6)
    assert prepared["status"] == "ready"
    assert prepared["claim_count"] == 7
    batches = prepared["batches"]
    assert [len(packet["claims"]) for packet in batches] == [6, 1]
    assert len({packet["packet_id"] for packet in batches}) == 2
    for packet in batches:
        assert packet["schema"] == "prism.excerpt-review-input.v1"
        assert packet["claim_count"] == 7
        assert packet["selection_complete"] is True
        assert packet["independent_sources_verified"] is False
    covered = [claim["claim_id"] for packet in batches for claim in packet["claims"]]
    assert covered == [claim["claim_id"] for claim in single["packet"]["claims"]]


def _save_running_and_merged(run_id, batches, second_status):
    packets = batches
    model_review.save(
        "alice",
        run_id,
        packets[0],
        status="running",
        model_id="synthetic-model",
        call_number=1,
    )
    entries = [
        {"packet": packets[0], "status": "failed", "reason": "review_timeout", "usage_complete": False}
    ]
    for packet in packets[1:]:
        entries.append(
            {
                "packet": packet,
                "status": "completed",
                "reason": None,
                "assessments": _assessments(packet),
                "usage_complete": True,
            }
        )
    final = {"status": "completed", "usage_complete": False}
    if second_status is not None:
        final = second_status
        for entry in entries:
            entry.update(status="failed", reason="review_timeout", assessments=[], usage_complete=False)
    return model_review.save(
        "alice",
        run_id,
        packets[-1],
        model_id="synthetic-model",
        call_number=1,
        batches=entries,
        **final,
    )


def test_timed_out_batch_is_a_claim_level_gap_not_an_incomplete_review():
    run_id = "run-review-timeout"
    _run(run_id)
    values = ["700", "710", "720", "730", "740", "750", "760"]
    _seed_reviewable("alice", run_id, values)
    batches = model_review.prepare_batches("alice", run_id)["batches"]

    record = _save_running_and_merged(run_id, batches, None)
    assert record["status"] == "completed"
    assert [batch["status"] for batch in record["batches"]] == ["failed", "completed"]
    assert record["batches"][0]["reason"] == "review_timeout"
    # Six claims timed out; the completed batch still contributes its assessment.
    assert len(record["timeout_claims"]) == 6
    assert len(record["assessments"]) == 1

    run = read_research_run("alice", run_id)
    presented = model_review.present(run, run["claims"])
    assert presented["unreviewed_review_timeout_count"] == 6

    revision = finalize_research_run("alice", run_id, "产能数据已登记，等待复核。")
    gaps = revision["gaps"]
    assert "unverified_review_timeout" in gaps
    assert "model_review_incomplete" not in gaps
    assert "model_review_partial_scope" not in gaps
    assert "semantic_review_pending" not in gaps


def test_every_batch_failing_keeps_the_whole_review_incomplete():
    run_id = "run-review-all-failed"
    _run(run_id)
    _seed_reviewable("alice", run_id, ["700", "710", "720", "730", "740", "750", "760"])
    batches = model_review.prepare_batches("alice", run_id)["batches"]

    record = _save_running_and_merged(
        run_id,
        batches,
        {"status": "failed", "reason": "review_timeout", "usage_complete": False},
    )
    assert record["status"] == "failed"
    assert record["assessments"] == []
    assert len(record["timeout_claims"]) == 7

    revision = finalize_research_run("alice", run_id, "产能数据已登记，等待复核。")
    gaps = revision["gaps"]
    assert "model_review_incomplete" in gaps
    assert "unverified_review_timeout" not in gaps
    assert "semantic_review_pending" not in gaps


async def test_runtime_runs_one_bounded_call_per_batch(engine, monkeypatch):
    run_id = engine.run["run_id"]
    ledger.create_research_run(
        engine.user,
        engine.conv,
        engine.run["question"],
        runtime.build_research_context(run_id=run_id, engine="prism-native"),
    )
    values = ["700", "710", "720", "730", "740", "750", "760"]
    _, passages, seeded = _seed_reviewable(engine.user, run_id, values)
    new_passage = passages[0]

    monkeypatch.setattr(model_review, "REQUEST_SECONDS", 0.01)
    calls = []
    packets = []

    async def script(messages, **kwargs):
        if kwargs["system"].startswith(model_review.SYSTEM):
            calls.append("review")
            packet = json.loads(messages[0]["content"])
            packets.append(packet)
            assert kwargs["tools"] == []
            if len(calls) == 2:
                # The first batch is slower than its own bounded call.
                await asyncio.Event().wait()
            yield {"type": "usage", "input_tokens": 7, "output_tokens": 7}
            yield {"type": "text_delta", "text": json.dumps(_response(packet), ensure_ascii=False)}
            yield done()
            return
        calls.append("main")
        if len(calls) == 1:
            arguments = {
                "claims": [
                    {
                        "kind": "fact",
                        "statement": "2025 年产能为 700 万吨。",
                        "supports": [new_passage["passage_id"]],
                        "quotes": {new_passage["passage_id"]: new_passage["excerpt"]},
                        "period": "2025",
                        "value": "700",
                        "unit": "万吨",
                    }
                ]
            }
            yield {"type": "usage", "input_tokens": 10, "output_tokens": 10}
            yield {
                "type": "tool_call",
                "id": "record-batched",
                "name": "research_record_claims",
                "arguments": arguments,
            }
            yield done(
                "tool_use",
                content=[
                    {
                        "type": "tool_use",
                        "id": "record-batched",
                        "name": "research_record_claims",
                        "input": arguments,
                    }
                ],
            )
            return
        yield {"type": "usage", "input_tokens": 11, "output_tokens": 11}
        yield {"type": "text_delta", "text": "产能数据已登记，等待复核。"}
        yield done()

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)

    assert outcome.status in {"completed", "partial"}
    assert calls == ["main", "review", "review", "main"]
    assert [len(packet["claims"]) for packet in packets] == [6, 1]
    started = [event for event in events(engine) if event.get("type") == "model_started"]
    phases = [event.get("phase") for event in started]
    assert phases == ["research", "evidence_review", "evidence_review", "research"]
    assert phases.count("evidence_review") == 2

    saved = ledger.read_research_run(engine.user, run_id)
    review = saved["model_review"]
    assert review["status"] == "completed"
    assert [batch["status"] for batch in review["batches"]] == ["failed", "completed"]
    assert review["batches"][0]["reason"] == "review_timeout"
    assert len(review["timeout_claims"]) == 6
    assert len(review["assessments"]) == 1

    revision_gaps = saved["report_revisions"][0]["gaps"]
    assert "unverified_review_timeout" in revision_gaps
    assert "model_review_incomplete" not in revision_gaps
    assert "model_review_partial_scope" not in revision_gaps
    assert "semantic_review_pending" not in revision_gaps
    usage = last_usage(engine)
    assert usage["model_calls"] == 4
    seeded_ids = {claim["claim_id"] for claim in seeded}
    assert seeded_ids <= set(review["claim_ids"])
