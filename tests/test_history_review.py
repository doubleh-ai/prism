"""Actual owned ledger, historical source bindings and a synthetic Native loop."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from test_native_agent_runtime_review import done, events, execute, last_usage
from test_native_agent_runtime_review import engine as engine

from server.agent import runtime
from server.database import get_db
from server.research import history_review, knowledge, ledger, model_review
from server.research_context import EvidenceTracker, build_research_context

OLD = "2025年合成公司X产品仍在送样，尚未形成量产供货。"
NEW = "2025年合成公司X产品已形成量产供货。"


@pytest.fixture
def history(engine):
    with get_db() as db:
        db.execute("INSERT INTO users(username,password_hash) VALUES('history-foreign','synthetic')")
        db.execute("INSERT INTO conversations(id,username) VALUES('history-foreign','history-foreign')")
        db.execute("INSERT INTO conversations(id,username) VALUES('historical-owned',?)", [engine.user])

    def add(run_id, *, excerpt=OLD, statement=None, owner=None, conv=None, as_of=None, **changes):
        owner = owner or engine.user
        conv = conv or ("historical-owned" if owner == engine.user else "history-foreign")
        context = build_research_context(run_id=run_id, engine="prism-native")
        if as_of:
            context["as_of"] = as_of
        ledger.create_research_run(owner, conv, "PRIVATE_HISTORICAL_QUESTION", context)
        tracker = EvidenceTracker(run_id)
        call = {
            "id": "read-" + run_id,
            "name": "WebFetch",
            "input": {"url": "https://www.sse.com.cn/synthetic/" + run_id},
        }
        tracker.record_call(call)
        block = {"tool_use_id": call["id"], "content": excerpt}
        refs = [
            {**ref, "published_at": "2026-01-01", "body_sha256": hashlib.sha256(excerpt.encode()).hexdigest()}
            for ref in tracker.record_result(block)
        ]
        pid = ledger.ingest_tool_result(owner, run_id, call, block, refs)[0]["passage_ids"][0]
        claim = {
            "kind": "fact",
            "statement": statement or excerpt,
            "entities": ["合成公司"],
            "metric": "X产品商业阶段",
            "period": "2025",
            "accounting_basis": "公司披露的量产供货",
            "supports": [pid],
            "quotes": {pid: excerpt},
            **changes,
        }
        saved = ledger.record_claims(owner, run_id, [claim])["claims"][0]
        return saved, claim

    def prepare(**kwargs):
        return model_review.prepare(engine.user, engine.run["run_id"], **kwargs)["packet"]

    return SimpleNamespace(engine=engine, add=add, prepare=prepare)


def pair(history, **old_changes):
    old, _ = history.add("historical-old", **old_changes)
    current, proposal = history.add(history.engine.run["run_id"], excerpt=NEW, conv=history.engine.conv)
    return old, current, proposal, history.prepare()


def response(packet, *, history_verdict="potential_conflict"):
    return {
        "packet_id": packet["packet_id"],
        "assessments": [
            {
                "claim_id": claim["claim_id"],
                "verdict": "supported",
                "evidence_ids": [claim["sources"][0]["passage_id"]]
                + [p["sources"][0]["passage_id"] for p in claim["historical_context"]],
                "history_verdict": history_verdict if claim["historical_context"] else "not_compared",
                "history_claim_ids": [p["claim_id"] for p in claim["historical_context"]],
                "reason": "本轮原文支持量产；同期间的历史原文否定量产，须核对两份披露的对象、日期和更正情况。",
            }
            for claim in packet["claims"]
        ],
    }


def test_history_uses_actual_old_excerpts_without_questions_notes_or_foreign_data(history, monkeypatch):
    old, _, _, packet = pair(history)
    history.add("foreign-related", owner="history-foreign", excerpt="FOREIGN_MARKER")
    knowledge.review(
        history.engine.user,
        old["claim_id"],
        knowledge.Review(expected_version=0, state="candidate", note="PRIVATE_REVIEW_NOTE"),
    )
    decode = history_review._decode

    def owner_only(raw):
        assert all(
            marker not in str(raw)
            for marker in ("FOREIGN_MARKER", "PRIVATE_HISTORICAL_QUESTION", "PRIVATE_REVIEW_NOTE")
        )
        return decode(raw)

    monkeypatch.setattr(history_review, "_decode", owner_only)
    packet = history.prepare()
    raw = json.dumps(packet, ensure_ascii=False)
    peer = packet["claims"][0]["historical_context"][0]
    assert peer["claim_id"] == old["claim_id"] and OLD in raw and NEW in raw
    assert peer["context_matches"] and peer["independent_sources_verified"] is False
    assert all(
        value not in raw
        for value in (
            "PRIVATE_HISTORICAL_QUESTION",
            "FOREIGN_MARKER",
            history.engine.user,
            history.engine.model["api_key"],
        )
    )
    assert len(raw) < model_review.MAX_PACKET_CHARS
    assert history.prepare()["packet_id"] == packet["packet_id"]
    with pytest.raises(HTTPException):
        model_review.prepare("history-foreign", history.engine.run["run_id"])


@pytest.mark.parametrize(
    "change",
    [
        {"entities": ["另一公司"]},
        {"entities": ["合成公司", "客户"]},
        {"metric": "Y产品商业阶段"},
        {"period": "最近"},
        {"accounting_basis": None},
        {"as_of": "2099-01-01T00:00:00+08:00"},
    ],
)
def test_missing_ambiguous_unrelated_or_future_context_is_not_reused(history, change):
    _, _, _, packet = pair(history, **change)
    assert packet["claims"][0]["historical_context"] == []


def test_different_period_is_explicit_and_cannot_be_called_a_same_context_conflict(history):
    _, _, _, packet = pair(history, period="2024")
    assert packet["claims"][0]["historical_context"][0]["context_matches"] is False
    with pytest.raises(ValueError):
        model_review.parse(packet, json.dumps(response(packet)))
    parsed = model_review.parse(packet, json.dumps(response(packet, history_verdict="changed_context")))
    assert parsed["assessments"][0]["history_verdict"] == "changed_context"


def test_same_source_is_visible_and_never_becomes_independent_corroboration(history):
    _, _, _, packet = pair(history, excerpt=NEW, statement=OLD)
    peer = packet["claims"][0]["historical_context"][0]
    assert peer["shared_body"] and peer["independent_sources_verified"] is False


def test_currency_difference_is_a_scope_change_not_a_numeric_conflict(history):
    fields = {"metric": "X产品营业收入", "accounting_basis": "营业收入", "value": "120", "unit": "元"}
    history.add(
        "historical-old", excerpt="2025年合成公司X产品营业收入为120元，旧披露。", currency="USD", **fields
    )
    history.add(
        history.engine.run["run_id"],
        excerpt="2025年合成公司X产品营业收入为120元，新披露。",
        conv=history.engine.conv,
        currency="CNY",
        **fields,
    )
    packet = history.prepare()
    assert not packet["claims"][0]["historical_context"][0]["context_matches"]
    with pytest.raises(ValueError):
        model_review.parse(packet, json.dumps(response(packet)))
    assert (
        model_review.parse(packet, json.dumps(response(packet, history_verdict="changed_context")))["status"]
        == "completed"
    )


@pytest.mark.parametrize("state", ["disputed", "retracted", "expired", "deleted", "unproven"])
def test_ineligible_history_is_excluded_when_preparing_a_new_packet(history, state):
    old, _, _, _ = pair(history)
    if state in {"disputed", "retracted"}:
        knowledge.review(
            history.engine.user,
            old["claim_id"],
            knowledge.Review(expected_version=0, state=state, note="合成状态变化"),
        )
    else:
        with get_db() as db:
            if state == "deleted":
                db.execute("UPDATE conversations SET deleted_at=1 WHERE id='historical-owned'")
            elif state == "expired":
                db.execute(
                    "INSERT INTO knowledge_reviews VALUES(?,?,1,'reviewed','synthetic','2000-01-01T00:00:00+00:00',NULL,1)",
                    [old["claim_id"], history.engine.user],
                )
            else:
                row = db.execute(
                    "SELECT document_id,payload FROM research_documents WHERE run_id='historical-old'"
                ).fetchone()
                doc = json.loads(row["payload"])
                doc.pop("retrieved_body_sha256")
                db.execute(
                    "UPDATE research_documents SET payload=? WHERE document_id=?",
                    [json.dumps(doc), row["document_id"]],
                )
    assert history.prepare()["claims"][0]["historical_context"] == []


@pytest.mark.parametrize(
    "change",
    [
        "missing_history",
        "invented_id",
        "duplicate_id",
        "no_current_evidence",
        "no_old_evidence",
        "not_compared",
        "authority",
    ],
)
def test_comparison_requires_bound_history_ids_and_both_sides_evidence(history, change):
    _, _, _, packet = pair(history)
    value = response(packet)
    a = value["assessments"][0]
    if change == "missing_history":
        a.pop("history_claim_ids")
    elif change == "invented_id":
        a["history_claim_ids"] = ["invented"]
    elif change == "duplicate_id":
        a["history_claim_ids"] *= 2
    elif change == "no_current_evidence":
        a["evidence_ids"].pop(0)
    elif change == "no_old_evidence":
        a["evidence_ids"].pop()
    elif change == "not_compared":
        a["history_verdict"] = "not_compared"
    else:
        a["authorized"] = True
    with pytest.raises(ValueError):
        model_review.parse(packet, json.dumps(value))


def test_advice_persists_bindings_and_change_notices_without_rewriting_history(history):
    old, _, _, packet = pair(history)
    run_id, owner = history.engine.run["run_id"], history.engine.user
    original = knowledge.detail(owner, old["claim_id"])["item"]
    parsed = model_review.parse(packet, json.dumps(response(packet)))
    model_review.save(owner, run_id, packet, status="running", model_id="synthetic", call_number=2)
    saved = model_review.save(owner, run_id, packet, model_id="synthetic", call_number=2, **parsed)
    assert saved["status"] == "completed" and saved["semantic_verification"] is False
    assert saved["assessments"][0]["effective_verdict"] == "partly_supported"
    assert saved["history_bindings"][0]["historical_claim_id"] == old["claim_id"]
    assert OLD not in json.dumps(saved, ensure_ascii=False)  # Packet body is not duplicated into the outcome.
    assert knowledge.detail(owner, old["claim_id"])["item"] == original
    knowledge.review(
        owner, old["claim_id"], knowledge.Review(expected_version=0, state="disputed", note="合成复核更新")
    )
    bundle = ledger.read_research_run(owner, run_id)
    assert bundle["knowledge_changes"][0]["source_claim_id"] == old["claim_id"]


@pytest.mark.parametrize("change", ["retracted", "deleted", "body", "claim", "claim_time"])
def test_history_change_before_start_or_completion_invalidates_binding(history, change):
    old, _, _, packet = pair(history)
    owner, run_id = history.engine.user, history.engine.run["run_id"]
    parsed = model_review.parse(packet, json.dumps(response(packet)))
    model_review.save(owner, run_id, packet, status="running", model_id="synthetic", call_number=2)
    if change == "retracted":
        knowledge.review(
            owner, old["claim_id"], knowledge.Review(expected_version=0, state="retracted", note="合成撤回")
        )
    else:
        with get_db() as db:
            if change == "deleted":
                db.execute("UPDATE conversations SET deleted_at=1 WHERE id='historical-owned'")
            elif change == "body":
                row = db.execute(
                    "SELECT document_id,payload FROM research_documents WHERE run_id='historical-old'"
                ).fetchone()
                value = json.loads(row["payload"])
                value["retrieved_body_sha256"] = "f" * 64
                db.execute(
                    "UPDATE research_documents SET payload=? WHERE document_id=?",
                    [json.dumps(value), row["document_id"]],
                )
            else:
                row = db.execute(
                    "SELECT payload FROM research_claims WHERE claim_id=?", [old["claim_id"]]
                ).fetchone()
                value = json.loads(row[0])
                if change == "claim_time":
                    value["created_at"] = "2099-01-01T00:00:00+08:00"
                else:
                    value["statement"] = "历史表述已变化"
                db.execute(
                    "UPDATE research_claims SET payload=? WHERE claim_id=?",
                    [json.dumps(value), old["claim_id"]],
                )
    assert model_review.history_current(owner, run_id, packet) is False
    saved = model_review.save(owner, run_id, packet, model_id="synthetic", call_number=2, **parsed)
    assert saved["status"] == "invalid" and saved["reason"] == "history_changed" and not saved["assessments"]


def test_repeated_agreeing_records_do_not_hide_an_older_difference(history):
    old, _ = history.add("historical-difference")
    for n in range(85):
        history.add(f"repeated-{n}", excerpt=NEW)
    history.add(history.engine.run["run_id"], excerpt=NEW, conv=history.engine.conv)
    peers = history.prepare()["claims"][0]["historical_context"]
    assert [peer["claim_id"] for peer in peers] == [old["claim_id"]]


def test_later_claim_registration_cannot_enter_an_earlier_research_cutoff(history):
    old, _, _, packet = pair(history)
    assert packet["claims"][0]["historical_context"][0]["claim_recorded_at"]
    with get_db() as db:
        row = db.execute("SELECT payload FROM research_claims WHERE claim_id=?", [old["claim_id"]]).fetchone()
        value = json.loads(row[0])
        value["created_at"] = "2099-01-01T00:00:00+08:00"
        db.execute(
            "UPDATE research_claims SET payload=? WHERE claim_id=?", [json.dumps(value), old["claim_id"]]
        )
    assert history.prepare()["claims"][0]["historical_context"] == []


def test_history_scan_and_packet_are_bounded_without_certifying_absence(history, monkeypatch):
    pair(history)
    monkeypatch.setattr(history_review, "SCAN_SECONDS", -1)
    packet = history.prepare()
    assert packet["claims"][0]["historical_context"] == []
    assert packet["historical_scope"] == {
        "scope": "owned_explicit_entity_and_metric",
        "complete": False,
        "reason": "scan_time_limit",
    }


def test_previously_used_deleted_history_is_flagged_even_without_a_human_review_row(history):
    old, _, _, packet = pair(history)
    owner, run_id = history.engine.user, history.engine.run["run_id"]
    parsed = model_review.parse(packet, json.dumps(response(packet)))
    model_review.save(owner, run_id, packet, status="running", model_id="synthetic", call_number=2)
    model_review.save(owner, run_id, packet, model_id="synthetic", call_number=2, **parsed)
    with get_db() as db:
        assert db.execute("SELECT count(*) FROM knowledge_reviews").fetchone()[0] == 0
        db.execute("UPDATE conversations SET deleted_at=1 WHERE id='historical-owned'")
    changes = ledger.read_research_run(owner, run_id)["knowledge_changes"]
    assert changes[0]["source_claim_id"] == old["claim_id"] and changes[0]["source_available"] == 0
    assert OLD not in json.dumps(changes, ensure_ascii=False)


@pytest.mark.parametrize("deleted_before_model", [False, True])
async def test_native_compares_history_in_existing_three_calls_and_saves_replay(
    history, monkeypatch, deleted_before_model
):
    history.add("historical-old")
    # Ingest the real current source; the model, rather than the fixture, records its claim.
    _, proposal = history.add(history.engine.run["run_id"], excerpt=NEW, conv=history.engine.conv)
    with get_db() as db:
        db.execute("DELETE FROM research_claims WHERE run_id=?", [history.engine.run["run_id"]])
    calls = []
    if deleted_before_model:
        original = model_review.history_current

        def revoke(owner, run_id, packet):
            with get_db() as db:
                db.execute("UPDATE conversations SET deleted_at=1 WHERE id='historical-owned'")
            return original(owner, run_id, packet)

        monkeypatch.setattr(model_review, "history_current", revoke)

    async def script(messages, **kwargs):
        reviewing = kwargs["system"].startswith(model_review.SYSTEM)
        calls.append("review" if reviewing else "main")
        yield {"type": "usage", "input_tokens": 11, "output_tokens": 9}
        if reviewing:
            assert kwargs["tools"] == [] and len(messages) == 1
            packet = json.loads(messages[0]["content"])
            assert packet["claims"][0]["historical_context"][0]["statement"] == OLD
            yield {"type": "text_delta", "text": json.dumps(response(packet), ensure_ascii=False)}
            yield done()
        elif len(calls) == 1:
            arguments = {"claims": [proposal]}
            yield {
                "type": "tool_call",
                "id": "record-current",
                "name": "research_record_claims",
                "arguments": arguments,
            }
            yield done(
                "tool_use",
                content=[
                    {
                        "type": "tool_use",
                        "id": "record-current",
                        "name": "research_record_claims",
                        "input": arguments,
                    }
                ],
            )
        else:
            if deleted_before_model:
                assert OLD not in json.dumps(messages, ensure_ascii=False)
            else:
                assert "potential_conflict" in json.dumps(messages)
            yield {"type": "text_delta", "text": "同期间的新旧披露有差异，暂保留争议并核对更正公告。"}
            yield done()

    monkeypatch.setattr(runtime, "model_stream", script)
    result = await execute(history.engine)
    assert result.status == "completed" and calls == (
        ["main", "main"] if deleted_before_model else ["main", "review", "main"]
    )
    assert last_usage(history.engine)["model_calls"] == (2 if deleted_before_model else 3)
    bundle = ledger.read_research_run(history.engine.user, history.engine.run["run_id"])
    if deleted_before_model:
        assert bundle["model_review"]["status"] == "not_performed"
        assert bundle["model_review"]["reason"] == "history_changed"
        return
    assert bundle["model_review"]["assessments"][0]["history_verdict"] == "potential_conflict"
    assert bundle["report_revisions"][0]["model_review"]["history_bindings"]
    assert not any("binding_sha256" in str(e.get("ui", {}).get("text", "")) for e in events(history.engine))
