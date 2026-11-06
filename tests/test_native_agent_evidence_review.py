"""Owned real ledger and Native-loop critique; all model streams are synthetic."""

import asyncio
import hashlib
import json

import pytest
from fastapi import HTTPException
from test_native_agent_runtime_review import done, events, execute, last_usage
from test_native_agent_runtime_review import engine as engine

from server.agent import runtime
from server.database import get_db
from server.research import knowledge, ledger, model_review
from server.research_context import EvidenceTracker

ORIGINAL = "合成公司仍在送样验证，尚未形成量产供货。"
CLAIM = "合成公司已形成量产供货。"


def seed(engine, *, body_hash=True):
    run_id = engine.run["run_id"]
    ledger.create_research_run(
        engine.user,
        engine.conv,
        "PRIVATE_QUESTION_MARKER",
        runtime.build_research_context(run_id=run_id, engine="prism-native"),
    )
    tracker = EvidenceTracker(run_id)
    call = {"id": "actual-source", "name": "WebFetch", "input": {"url": "https://www.sse.com.cn/synthetic"}}
    tracker.record_call(call)
    block = {"tool_use_id": call["id"], "content": ORIGINAL}
    refs = tracker.record_result(block)
    if body_hash:
        refs = [{**ref, "body_sha256": hashlib.sha256(ORIGINAL.encode()).hexdigest()} for ref in refs]
    pid = ledger.ingest_tool_result(engine.user, run_id, call, block, refs)[0]["passage_ids"][0]
    return {
        "kind": "fact",
        "statement": CLAIM,
        "entities": ["合成公司"],
        "supports": [pid],
        "quotes": {pid: ORIGINAL},
    }


def packet_for(engine):
    claim = seed(engine)
    ledger.record_claims(engine.user, engine.run["run_id"], [claim])
    return model_review.prepare(engine.user, engine.run["run_id"])["packet"]


def response(packet, verdict="not_supported", reason="节选明确说明尚未量产，结论把否定改成了肯定。"):
    return {
        "packet_id": packet["packet_id"],
        "assessments": [
            {
                "claim_id": c["claim_id"],
                "verdict": verdict,
                "reason": reason,
                "evidence_ids": [c["sources"][0]["passage_id"]],
            }
            for c in packet["claims"]
        ],
    }


def test_packet_is_finite_owned_and_excludes_question_profile_and_conversation(engine):
    packet = packet_for(engine)
    text = json.dumps(packet, ensure_ascii=False)
    assert len(text) < model_review.MAX_PACKET_CHARS and len(packet["claims"]) == 1
    assert ORIGINAL in text and "PRIVATE_QUESTION_MARKER" not in text
    assert engine.model["api_key"] not in text and engine.user not in text
    assert packet["independent_sources_verified"] is False
    assert (
        model_review.prepare(engine.user, engine.run["run_id"])["packet"]["packet_id"] == packet["packet_id"]
    )
    with get_db() as db:
        db.execute("INSERT INTO users(username,password_hash) VALUES('other-review','synthetic')")
    with pytest.raises(HTTPException) as other:
        model_review.prepare("other-review", engine.run["run_id"])
    assert other.value.status_code == 404


def test_unproven_tool_text_cannot_stand_in_for_server_fetched_excerpts(engine):
    claim = seed(engine, body_hash=False)
    ledger.record_claims(engine.user, engine.run["run_id"], [claim])
    result = model_review.prepare(engine.user, engine.run["run_id"])
    assert result["status"] == "not_performed" and result["reason"] == "eligible_excerpts_missing"


@pytest.mark.parametrize(
    "change",
    [
        "packet",
        "claim",
        "evidence",
        "empty_evidence",
        "duplicate_claim",
        "extra_authority",
        "control_reason",
        "duplicate_json",
    ],
)
def test_reviewer_cannot_invent_ids_scope_or_authority(engine, change):
    packet = packet_for(engine)
    value = response(packet, "supported")
    if change == "packet":
        value["packet_id"] = "0" * 64
    if change == "claim":
        value["assessments"][0]["claim_id"] = "foreign-claim"
    if change == "evidence":
        value["assessments"][0]["evidence_ids"] = ["foreign-passage"]
    if change == "empty_evidence":
        value["assessments"][0]["evidence_ids"] = []
    if change == "duplicate_claim":
        value["assessments"] *= 2
    if change == "extra_authority":
        value["verified"] = True
    if change == "control_reason":
        value["assessments"][0]["reason"] = "bad\x00text"
    raw = json.dumps(value)
    if change == "duplicate_json":
        raw = '{"packet_id":"' + packet["packet_id"] + '",' + raw[1:]
    with pytest.raises(ValueError):
        model_review.parse(packet, raw)


def test_omitted_reference_prevents_a_supported_verdict_becoming_full_support(engine):
    packet = packet_for(engine)
    packet["claims"][0]["references_omitted"] = 1
    parsed = model_review.parse(packet, json.dumps(response(packet, "supported")))
    assert parsed["assessments"][0]["verdict"] == "supported"
    assert parsed["assessments"][0]["effective_verdict"] == "partly_supported"
    assert parsed["assessments"][0]["scope_incomplete"]


def test_advisory_does_not_rewrite_claim_human_review_or_frozen_revision(engine):
    packet = packet_for(engine)
    run_id = engine.run["run_id"]
    parsed = model_review.parse(packet, json.dumps(response(packet)))
    with pytest.raises(HTTPException):
        model_review.save(engine.user, run_id, packet, model_id="synthetic", call_number=2, **parsed)
    model_review.save(engine.user, run_id, packet, status="running", model_id="synthetic", call_number=2)
    saved = model_review.save(
        engine.user, run_id, packet, model_id="synthetic", call_number=2, usage_complete=True, **parsed
    )
    assert saved["context_isolated"] and saved["semantic_verification"] is False
    item = knowledge.detail(engine.user, packet["claims"][0]["claim_id"])["item"]
    assert item["state"] == "candidate" and item["semantic_status"] == "unreviewed"
    assert item["model_review"]["assessment"]["effective_verdict"] == "not_supported"
    frozen = ledger.finalize_research_run(engine.user, run_id, "只支持送样验证，量产供货未获证实。")
    assert frozen["model_review"]["status"] == "completed" and "model_review_disagreement" in frozen["gaps"]
    assert ledger.finalize_research_run(engine.user, run_id, "不应覆盖") == frozen
    with pytest.raises(HTTPException):
        model_review.save(engine.user, run_id, packet, model_id="synthetic", call_number=2, **parsed)
    with get_db() as db:
        assert db.execute("SELECT count(*) FROM knowledge_reviews").fetchone()[0] == 0


async def run_script(engine, monkeypatch, *, review_mode="valid", late_claim=False):
    claim = seed(engine, body_hash=review_mode != "missing_source")
    calls, main_calls, closed = [], [], []

    async def script(messages, **kwargs):
        reviewing = kwargs["system"].startswith(model_review.SYSTEM)
        calls.append("review" if reviewing else "main")
        try:
            if reviewing:
                assert kwargs["tools"] == [] and len(messages) == 1 and messages[0]["role"] == "user"
                assert "PRIVATE_QUESTION_MARKER" not in json.dumps(messages)
                packet = json.loads(messages[0]["content"])
                if review_mode == "provider_error":
                    raise runtime.provider.ProviderError("provider_unavailable")
                if review_mode in {
                    "provider_auth_error",
                    "provider_rate_limited",
                    "provider_configuration_error",
                    "provider_input_error",
                }:
                    raise runtime.provider.ProviderError(review_mode)
                if review_mode == "timeout":
                    await asyncio.Event().wait()
                if review_mode == "tool":
                    yield {
                        "type": "tool_call",
                        "id": "forbidden",
                        "name": "market_quote",
                        "arguments": {"ticker": "forbidden"},
                    }
                    return
                yield {"type": "usage", "input_tokens": 7, "output_tokens": 5}
                raw = (
                    "invalid JSON"
                    if review_mode == "invalid"
                    else json.dumps(response(packet), ensure_ascii=False)
                )
                yield {"type": "text_delta", "text": raw}
                yield done()
                return
            main_calls.append(True)
            yield {"type": "usage", "input_tokens": 11, "output_tokens": 9}
            if len(main_calls) == 1 or late_claim and len(main_calls) == 2:
                arguments = {
                    "claims": [
                        {**claim, "statement": CLAIM if len(main_calls) == 1 else "合成公司仍在送样验证。"}
                    ]
                }
                call_id = "record-" + str(len(main_calls))
                yield {
                    "type": "tool_call",
                    "id": call_id,
                    "name": "research_record_claims",
                    "arguments": arguments,
                }
                yield done(
                    "tool_use",
                    content=[
                        {
                            "type": "tool_use",
                            "id": call_id,
                            "name": "research_record_claims",
                            "input": arguments,
                        }
                    ],
                )
            else:
                if "review" in calls:
                    assert "节选复核建议" in json.dumps(messages, ensure_ascii=False)
                yield {"type": "text_delta", "text": "原始节选只说明送样验证，量产供货尚无依据。"}
                yield done()
        finally:
            closed.append(reviewing)

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)
    return outcome, calls, closed, ledger.read_research_run(engine.user, engine.run["run_id"])


async def test_native_counts_review_in_same_budget_and_never_streams_raw_review_json(engine, monkeypatch):
    outcome, calls, closed, saved = await run_script(engine, monkeypatch)
    assert calls == ["main", "review", "main"] and len(closed) == 3
    assert outcome.status == "completed" and outcome.text == "原始节选只说明送样验证，量产供货尚无依据。"
    usage = last_usage(engine)
    assert usage["model_calls"] == 3 and usage["tool_calls"] == 1 and usage["usage_complete"]
    assert (
        saved["model_review"]["status"] == "completed"
        and saved["model_review"]["unreviewed_claims_count"] == 0
    )
    assert sum(e.get("phase") == "evidence_review" for e in events(engine)) == 1
    assert not any("packet_id" in str(e.get("ui", {}).get("text", "")) for e in events(engine))


@pytest.mark.parametrize("mode", ["invalid", "provider_error", "tool", "timeout"])
async def test_review_failure_is_not_retried_and_keeps_answer_usage_and_sources(engine, monkeypatch, mode):
    monkeypatch.setattr(model_review, "REQUEST_SECONDS", 0.01)
    outcome, calls, closed, saved = await run_script(engine, monkeypatch, review_mode=mode)
    assert calls == ["main", "review", "main"] and len(closed) == 3
    assert outcome.text and len(saved["claims"]) == 1
    assert saved["model_review"]["status"] in {"failed", "invalid"}
    assert "model_review_incomplete" in saved["report_revisions"][0]["gaps"]
    if mode in {"provider_error", "tool", "timeout"}:
        assert last_usage(engine)["usage_complete"] is False
    assert last_usage(engine)["tool_calls"] == 1


@pytest.mark.parametrize("limited", ["calls", "time", "source", "output"])
async def test_insufficient_budget_or_sources_does_not_start_a_review(engine, monkeypatch, limited):
    if limited == "calls":
        engine.service.limits = runtime.Limits(model_calls=2)
    if limited == "time":
        engine.service.limits = runtime.Limits(wall_seconds=60)
    if limited == "output":
        engine.service.limits = runtime.Limits(output_tokens_per_call=256)
    _, calls, _, saved = await run_script(
        engine, monkeypatch, review_mode="missing_source" if limited == "source" else "valid"
    )
    assert calls == ["main", "main"] and last_usage(engine)["model_calls"] == 2
    assert saved["model_review"]["status"] == "not_performed"
    assert saved["model_review"]["reason"] == (
        "eligible_excerpts_missing" if limited == "source" else "run_budget"
    )


async def test_new_claims_after_review_remain_explicitly_unreviewed(engine, monkeypatch):
    _, calls, _, saved = await run_script(engine, monkeypatch, late_claim=True)
    assert calls == ["main", "review", "main", "main"]
    assert saved["model_review"]["unreviewed_claims_count"] == 1
    assert "model_review_partial_scope" in saved["report_revisions"][0]["gaps"]


async def test_cancellation_closes_review_and_preserves_registered_claims(engine, monkeypatch):
    claim = seed(engine)
    entered, closed = asyncio.Event(), []

    async def script(messages, **kwargs):
        if kwargs["system"].startswith(model_review.SYSTEM):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.append(True)
        else:
            arguments = {"claims": [claim]}
            yield {"type": "usage", "input_tokens": 3, "output_tokens": 4}
            yield {
                "type": "tool_call",
                "id": "claims",
                "name": "research_record_claims",
                "arguments": arguments,
            }
            yield done(
                "tool_use",
                content=[
                    {"type": "tool_use", "id": "claims", "name": "research_record_claims", "input": arguments}
                ],
            )

    monkeypatch.setattr(runtime, "model_stream", script)
    parent = engine.manager.admit("request")
    with parent:
        engine.service.start(engine.user, engine.run, engine.model, parent=parent)
    handle = engine.service.active[engine.run["run_id"]].handle
    await asyncio.wait_for(entered.wait(), 2)
    engine.service.cancel(engine.user, engine.run["run_id"])
    outcome = await asyncio.wait_for(handle.wait(), 2)
    assert outcome.status == "cancelled" and closed == [True]
    saved = ledger.read_research_run(engine.user, engine.run["run_id"])
    assert saved["model_review"]["status"] == "cancelled" and len(saved["claims"]) == 1
    assert last_usage(engine)["model_calls"] == 2 and last_usage(engine)["usage_complete"] is False
    assert engine.manager.observe()["blocked"] == 0


async def test_source_read_to_claim_review_answer_uses_actual_native_tools(engine, monkeypatch):
    from test_native_agent_tools import fetched

    from server.agent import tools

    monkeypatch.setattr(
        tools,
        "_fetch_source",
        lambda *a, **kw: fetched(text=ORIGINAL, body_sha256=hashlib.sha256(ORIGINAL.encode()).hexdigest()),
    )
    sequence = []

    async def script(messages, **kwargs):
        yield {"type": "usage", "input_tokens": 3, "output_tokens": 5}
        if kwargs["system"].startswith(model_review.SYSTEM):
            sequence.append("review")
            packet = json.loads(messages[0]["content"])
            assert (
                packet["claims"][0]["sources"][0]["body_sha256"]
                == hashlib.sha256(ORIGINAL.encode()).hexdigest()
            )
            yield {"type": "text_delta", "text": json.dumps(response(packet), ensure_ascii=False)}
            yield done()
            return
        if not sequence:
            sequence.append("source")
            name, args = "source_read", {"url": "https://www.sse.com.cn/disclosure"}
        elif sequence == ["source"]:
            sequence.append("claim")
            result = json.loads(messages[-1]["content"][0]["content"])
            pid = result["refs"][0]["passage_ids"][0]
            name, args = (
                "research_record_claims",
                {
                    "claims": [
                        {"kind": "fact", "statement": CLAIM, "supports": [pid], "quotes": {pid: ORIGINAL}}
                    ]
                },
            )
        else:
            sequence.append("answer")
            yield {"type": "text_delta", "text": "现有原文只支持送样验证，不支持已量产供货。"}
            yield done()
            return
        cid = "native-" + name
        yield {"type": "tool_call", "id": cid, "name": name, "arguments": args}
        yield done("tool_use", content=[{"type": "tool_use", "id": cid, "name": name, "input": args}])

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)
    assert sequence == ["source", "claim", "review", "answer"]
    assert outcome.status == "completed" and last_usage(engine)["model_calls"] == 4
    assert last_usage(engine)["tool_calls"] == 2
    saved = ledger.read_research_run(engine.user, engine.run["run_id"])
    assert saved["model_review"]["assessments"][0]["effective_verdict"] == "not_supported"
    assert saved["passages"][0]["excerpt"] == ORIGINAL


async def test_daily_last_slot_is_saved_for_answer_and_unknown_usage_is_retained(engine, monkeypatch):
    from server import model_routes

    model_routes.init_model_routes_db()
    route = {
        "purpose": "research",
        "version": 1,
        "configured": True,
        "enabled": True,
        "daily_calls_max": 3,
        "model_calls_max": 6,
        "output_tokens_max": 2048,
        "wall_seconds_max": 180,
        "model_id": engine.run["model_id"],
    }
    with get_db() as db:
        db.execute("INSERT INTO model_routes VALUES(?,?,?)", ["research", 1, json.dumps(route)])
    model_routes.reserve(
        "research", engine.user, "old-unknown", engine.run["model_id"], expected_route_version=1
    )
    engine.model["_purpose_route"] = route
    _, calls, _, saved = await run_script(engine, monkeypatch)
    assert calls == ["main", "main"] and last_usage(engine)["model_calls"] == 2
    assert (
        saved["model_review"]["status"] == "not_performed" and saved["model_review"]["reason"] == "run_budget"
    )
    assert not any(e.get("phase") == "evidence_review" for e in events(engine))
    with get_db() as db:
        assert db.execute("SELECT count(*) FROM model_request_reservations").fetchone()[0] == 3
        assert (
            db.execute(
                "SELECT status FROM model_request_reservations WHERE request_id='old-unknown'"
            ).fetchone()[0]
            == "unknown"
        )


def test_unfinished_review_on_exclusive_restart_is_unknown_not_success(engine):
    packet = packet_for(engine)
    model_review.save(
        engine.user, engine.run["run_id"], packet, status="running", model_id="synthetic", call_number=2
    )
    assert ledger.recover_interrupted_runs() == 1
    saved = ledger.read_research_run(engine.user, engine.run["run_id"])
    assert saved["model_review"]["status"] == "failed" and saved["model_review"]["interrupted_usage_unknown"]
    assert saved["model_review"]["usage_complete"] is False
    assert saved["report_revisions"][0]["model_review"]["status"] == "failed"
    assert "model_review_incomplete" in saved["report_revisions"][0]["gaps"]


@pytest.mark.parametrize(
    "code",
    ["provider_auth_error", "provider_rate_limited", "provider_configuration_error", "provider_input_error"],
)
async def test_fatal_provider_failure_stops_after_review_without_another_request(engine, monkeypatch, code):
    outcome, calls, closed, saved = await run_script(engine, monkeypatch, review_mode=code)
    assert calls == ["main", "review"] and len(closed) == 2
    assert outcome.error_code == code and last_usage(engine)["model_calls"] == 2
    assert saved["model_review"]["status"] == "failed" and saved["model_review"]["reason"] == "provider_error"
    assert len(saved["claims"]) == 1 and last_usage(engine)["usage_complete"] is False
