"""Synthetic receipt/claim regressions: no model, media, credentials or live data."""

import hashlib
import json
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from server.research import (
    create_research_run,
    finalize_research_run,
    ingest_tool_result,
    read_research_run,
    record_claims,
    record_relations,
    set_research_scope,
)
from server.research_context import EvidenceTracker, build_research_context


def run(run_id="round-one", user="alice", conv=""):
    context = build_research_context(run_id=run_id, now=datetime(2026, 10, 20, 18, tzinfo=UTC))
    create_research_run(user, conv, "研究合成公司的 2025 年产能", context)
    return context


def receipt(
    run_id="round-one",
    *,
    user="alice",
    tool="WebFetch",
    text="2025 年产能为 120 万吨。",
    url="https://example.test/disclosure",
    call_id="fetch-one",
):
    tracker = EvidenceTracker(run_id)
    call = {"id": call_id, "name": tool, "input": {"url": url}}
    block = {"type": "tool_result", "tool_use_id": call_id, "content": text}
    tracker.record_call(call)
    refs = tracker.record_result(block, retrieved_at="2026-10-21T02:00:01+08:00")
    return ingest_tool_result(user, run_id, call, block, refs)


def fact(passage_id, **extra):
    return {
        "kind": "fact",
        "statement": "年产能 120 万吨",
        "supports": [passage_id],
        "value": "120",
        "unit": "万吨",
        "period": "2025",
        **extra,
    }


def test_success_receipt_hash_scope_clock_and_unknown_publication():
    context = run()
    text = "2025 年产能为 120 万吨。"
    refs = receipt(text=text)
    result = read_research_run("alice", context["run_id"])
    (doc,) = result["documents"]
    (passage,) = result["passages"]
    assert doc["content_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert passage["excerpt_sha256"] == doc["content_sha256"]
    assert doc["content_scope"] == "tool_returned_text"
    assert doc["original_obtained"] is False
    assert doc["published_at"] is None and doc["publication_precision"] == "unknown"
    assert doc["final_url"] is None and doc["publisher"] is None
    assert doc["retrieved_at"] == "2026-10-21T02:00:01+08:00"
    assert result["run"]["as_of"] == "2026-10-21T02:00:00+08:00"
    assert refs[0]["document_id"] == doc["document_id"]
    assert refs[0]["passage_ids"] == [passage["passage_id"]]


@pytest.mark.parametrize(
    "text",
    [
        "API Error: 403 forbidden",
        "403 Forbidden",
        "<html><title>Sign in</title></html>",
        '{"success":false,"url":"https://example.test"}',
    ],
)
def test_failed_and_login_results_never_become_sources(text):
    run()
    assert receipt(text=text) == []
    assert read_research_run("alice", "round-one")["documents"] == []


def test_search_fetch_and_later_correction_are_distinct_receipts():
    run()
    tracker = EvidenceTracker("round-one")
    for call_id, tool, text in [
        (
            "search",
            "WebSearch",
            '[{"url":"https://example.test/a","title":"Notice","snippet":"产能 120 万吨"}]',
        ),
        ("fetch", "WebFetch", "2025 年产能 120 万吨"),
        ("correction", "WebFetch", "更正：2025 年产能 100 万吨"),
    ]:
        call = {"id": call_id, "name": tool, "input": {"url": "https://example.test/a"}}
        block = {"tool_use_id": call_id, "content": text}
        tracker.record_call(call)
        refs = tracker.record_result(block)
        assert len(refs) == 1
        ingest_tool_result("alice", "round-one", call, block, refs)
    docs = read_research_run("alice", "round-one")["documents"]
    assert len(docs) == 3
    assert docs[0]["content_scope"] == "search_snippet"
    assert docs[1]["content_sha256"] != docs[2]["content_sha256"]
    assert all(d["revision_of"] is None for d in docs)  # Correction relation cannot be guessed.


def test_metadata_keeps_day_precision_and_future_information_gap():
    run()
    text = json.dumps(
        [{"url": "https://example.test/a", "snippet": "产能120万吨", "published_at": "2026-10-22"}]
    )
    receipt(tool="WebSearch", text=text)
    result = read_research_run("alice", "round-one")
    (doc,) = result["documents"]
    assert doc["published_at"] == "2026-10-22"
    assert doc["publication_precision"] == "day"
    assert "published_after_as_of" in doc["gaps"]
    (claim,) = record_claims("alice", "round-one", [fact(result["passages"][0]["passage_id"])])["claims"]
    assert "future_publication" in claim["gaps"]
    assert "search_snippet_only" in claim["gaps"]


def test_historical_scope_preserves_clock_and_is_fixed_idempotently():
    context = run()
    original = set_research_scope("alice", "round-one", "2025-12-31", "2025 财年")
    assert original["run"]["as_of"] == context["as_of"]
    assert original["run"]["cutoff_precision"] == "day"
    assert original["run"]["cutoff_basis"] == "research_scope_tool"
    assert set_research_scope("alice", "round-one", "2025-12-31", "2025 财年") == original
    with pytest.raises(HTTPException) as changed:
        set_research_scope("alice", "round-one", "2026-01-01", "2025 财年")
    assert changed.value.status_code == 409
    pid = receipt()[0]["passage_ids"][0]
    claim = record_claims("alice", "round-one", [fact(pid)])["claims"][0]
    assert claim["citation_status"] == "partial"
    assert "historical_availability_unknown" in claim["gaps"]
    revision = finalize_research_run("alice", "round-one", "历史资料尚需补充发布日期")
    assert revision["acceptance_status"] == "partial"
    assert revision["information_cutoff"] == "2025-12-31"
    assert "historical_availability_unknown" in revision["gaps"]
    assert "2025 财年" in revision["blocks"][0]["text"]
    final_run = set_research_scope("alice", "round-one", "2025-12-31", "2025 财年")["run"]
    assert final_run["as_of"] == context["as_of"]
    assert read_research_run("alice", "round-one")["report_revisions"] == [revision]
    with pytest.raises(HTTPException) as denied:
        set_research_scope("bob", "round-one", "2025-12-31", "2025 财年")
    assert denied.value.status_code == 404


@pytest.mark.parametrize("existing", ["source", "claim", "relation", "finished"])
def test_scope_cannot_be_added_after_research_material_or_finalization(existing):
    run()
    if existing == "source":
        receipt()
    elif existing == "claim":
        record_claims("alice", "round-one", [{"kind": "hypothesis", "statement": "待检验"}])
    elif existing == "relation":
        record_relations(
            "alice",
            "round-one",
            [
                {
                    "from_entity": "A",
                    "to_entity": "B",
                    "product": "P",
                    "dimension": "supply",
                    "mechanism": "待核验",
                }
            ],
        )
    else:
        finalize_research_run("alice", "round-one", "已结束")
    before = read_research_run("alice", "round-one")
    with pytest.raises(HTTPException) as denied:
        set_research_scope("alice", "round-one", "2025-12-31")
    assert denied.value.status_code == 409
    assert read_research_run("alice", "round-one") == before


@pytest.mark.parametrize(
    "cutoff",
    [
        "yesterday",
        "2025-02-30",
        "2025-01-01T12:00:00",
        "2025-01-01T12:00Z",
        "0001-01-01T00:00:00+23:59",
        "2026-10-22",
        "2026-10-21T02:00:01+08:00",
    ],
)
def test_scope_rejects_unrecordable_or_future_cutoff(cutoff):
    run()
    with pytest.raises(HTTPException) as invalid:
        set_research_scope("alice", "round-one", cutoff)
    assert invalid.value.status_code == 422
    assert read_research_run("alice", "round-one")["run"]["information_cutoff"] is None


@pytest.mark.parametrize(
    "cutoff,published,gap",
    [
        ("2025-12-31", "2025-12-31", None),
        ("2025-12-31", "2025-12-31T23:59:59+08:00", None),
        ("2025-12-31", "2026-01-01", "published_after_cutoff"),
        ("2025-12-31T12:00:00+08:00", "2025-12-31", "publication_precision_insufficient"),
        ("2025-12-31T12:00:00+08:00", "2025-12-30", None),
        ("2025-12-31T12:00:00+08:00", "2025-12-31T11:59:59+08:00", None),
        ("2025-12-31T12:00:00+08:00", "2025-12-31T12:00:01+08:00", "published_after_cutoff"),
        ("2025-12-31T12:00:00+08:00", None, "historical_availability_unknown"),
    ],
)
def test_cutoff_respects_publication_precision_without_inventing_midnight(cutoff, published, gap):
    run()
    set_research_scope("alice", "round-one", cutoff)
    receipt(
        tool="WebSearch",
        text=json.dumps(
            [{"url": "https://example.test/date", "snippet": "2025 年产能120万吨", "published_at": published}]
        ),
    )
    detail = read_research_run("alice", "round-one")
    doc = detail["documents"][0]
    assert doc["published_at"] == published
    assert "published_after_as_of" not in doc["gaps"]
    temporal = {
        "published_after_cutoff",
        "publication_precision_insufficient",
        "historical_availability_unknown",
    }
    assert set(doc["gaps"]) & temporal == ({gap} if gap else set())
    claim = record_claims("alice", "round-one", [fact(detail["passages"][0]["passage_id"])])["claims"][0]
    assert set(claim["gaps"]) & temporal == ({gap} if gap else set())


def test_scope_timestamp_equivalent_timezone_retry_is_idempotent():
    run()
    first = set_research_scope("alice", "round-one", "2025-12-31T04:00:00Z")
    assert first["run"]["information_cutoff"] == "2025-12-31T12:00:00+08:00"
    assert first == set_research_scope("alice", "round-one", "2025-12-31T12:00:00+08:00")


@pytest.mark.asyncio
async def test_scope_capability_uses_token_run_and_rejects_scope_override(test_user):
    from server import capabilities as cap
    from server.database import get_db

    user = test_user["username"]
    with get_db() as db:
        db.execute("INSERT INTO conversations(id,username) VALUES('scope-conv',?)", [user])
    run("scope-run", user=user, conv="scope-conv")
    token = cap.issue_run_token(user, "scope-conv", "scope-run")
    context = {"username": user, "conv_id": "scope-conv", "run_id": "scope-run", "token": token}
    response = await cap.dispatch_call(
        context,
        cap.CallRequest(
            name="research_scope", arguments={"information_cutoff": "2025-12-31"}, request_id="scope-first"
        ),
    )
    assert response["ok"] is True
    assert response["data"]["run"]["information_cutoff"] == "2025-12-31"
    assert response["call"]["target"]["run_id"] == "scope-run"
    with pytest.raises(HTTPException) as denied:
        await cap.dispatch_call(
            context,
            cap.CallRequest(
                name="research_scope",
                arguments={"information_cutoff": "2025-12-31", "run_id": "other"},
                request_id="scope-override",
            ),
        )
    assert denied.value.status_code == 422


def test_same_text_reprints_share_group_without_independence_claim():
    run()
    receipt(call_id="one", url="https://example.test/a")
    receipt(call_id="two", url="https://other.test/reprint")
    a, b = read_research_run("alice", "round-one")["documents"]
    assert a["content_group"] == b["content_group"]
    assert a["independence_status"] == b["independence_status"] == "not_assessed"


def test_known_internal_platform_results_are_never_original_documents():
    run()
    refs = receipt(tool="mcp__prism__report_read", text="[旧报告](https://example.test/old)")
    assert refs[0]["source_kind"] == "historical_research"
    assert read_research_run("alice", "round-one")["documents"] == []


def test_claim_validates_numbers_and_quotes_without_faking_semantic_review():
    run()
    pid = receipt()[0]["passage_ids"][0]
    candidates = [
        fact(pid, semantic_status="verified"),
        fact(pid, value="999"),
        fact(pid, quotes={pid: "伪造原文"}),
        fact(pid, unit="亿元"),
    ]
    claims = record_claims("alice", "round-one", candidates)["claims"]
    assert claims[0]["citation_status"] == "valid"
    assert all(c["semantic_status"] == "unreviewed" for c in claims)
    assert "value_not_in_excerpt" in claims[1]["gaps"]
    assert claims[2]["citation_status"] == "invalid"
    assert "unit_not_in_excerpt" in claims[3]["gaps"]


def test_owner_run_and_deleted_conversation_boundaries_are_uniform():
    from server.database import get_db

    with get_db() as db:
        db.execute(
            "INSERT INTO conversations(id,username,title,skill) VALUES('conv','alice','x','prism-research')"
        )
    run(conv="conv")
    run("round-two")
    pid = receipt()[0]["passage_ids"][0]
    with pytest.raises(HTTPException) as exc:
        read_research_run("bob", "round-one")
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        record_claims("alice", "round-two", [fact(pid)])
    assert exc.value.status_code == 422
    with get_db() as db:
        db.execute("UPDATE conversations SET deleted_at='2026-10-21' WHERE id='conv'")
    with pytest.raises(HTTPException) as exc:
        record_claims("alice", "round-one", [fact(pid)])
    assert exc.value.status_code == 404


def test_claim_batch_rolls_back_if_one_reference_is_foreign():
    run()
    pid = receipt()[0]["passage_ids"][0]
    with pytest.raises(HTTPException):
        record_claims("alice", "round-one", [fact(pid), fact("foreign")])
    assert read_research_run("alice", "round-one")["claims"] == []


def test_calculation_uses_finite_owned_inputs_not_executable_expression():
    run()
    pid = receipt()[0]["passage_ids"][0]
    first, second = record_claims("alice", "round-one", [fact(pid), fact(pid)])["claims"]
    base = {
        "kind": "calculation",
        "statement": "两项相加",
        "value": "240",
        "unit": "万吨",
        "period": "2025",
        "formula": {"operator": "add", "input_claim_ids": [first["claim_id"], second["claim_id"]]},
    }
    good, bad, unsafe = record_claims(
        "alice",
        "round-one",
        [base, {**base, "value": "241"}, {**base, "formula": {"operator": "__import__('os')"}}],
    )["claims"]
    assert good["formula"]["computed_value"] == "240"
    assert good["citation_status"] == "valid"
    assert "calculation_mismatch" in bad["gaps"]
    assert "formula_missing" in unsafe["gaps"]


def test_immutable_revision_survives_followup_and_rejects_late_claims():
    run()
    pid = receipt()[0]["passage_ids"][0]
    record_claims("alice", "round-one", [fact(pid)])
    original = finalize_research_run("alice", "round-one", "第一轮原始结论")
    assert original["acceptance_status"] == "partial"
    assert "semantic_review_pending" in original["gaps"]
    assert finalize_research_run("alice", "round-one", "替换结论") == original
    with pytest.raises(HTTPException) as exc:
        record_claims("alice", "round-one", [fact(pid)])
    assert exc.value.status_code == 409
    run("round-two")
    followup = finalize_research_run("alice", "round-two", "追问")
    assert followup["revision_id"] != original["revision_id"]
    assert read_research_run("alice", "round-one")["report_revisions"][0]["narrative"] == "第一轮原始结论"


def test_relations_are_mechanism_candidates_not_supply_facts():
    run()
    pid = receipt()[0]["passage_ids"][0]
    claim = record_claims("alice", "round-one", [fact(pid)])["claims"][0]
    candidate = {
        "dimension": "certification",
        "from_entity": "合成材料",
        "to_entity": "合成部件",
        "product": "材料A",
        "mechanism": "认证通过后才可能进入小批量",
        "supporting_claims": [claim["claim_id"]],
        "status": "verified",
        "invalidators": ["认证失败"],
        "business_stage": "送样",
        "valid_period": "2025",
    }
    relation = record_relations("alice", "round-one", [candidate])["relations"][0]
    assert relation["status"] == "has_references"
    assert relation["semantic_status"] == "unreviewed"
    assert "confidence" not in relation
    run("round-two")
    with pytest.raises(HTTPException):
        record_relations("alice", "round-two", [candidate])


@pytest.mark.asyncio
async def test_chat_ingests_real_event_chain_and_saves_each_run(client, auth_cookies, test_user, monkeypatch):
    from server import chat
    from server.database import get_db

    with get_db() as db:
        db.execute(
            "INSERT INTO conversations(id,username,title,skill) VALUES('ledger-chat',?,'新对话','prism-research')",
            [test_user["username"]],
        )

    async def engine(*args, **kwargs):
        run_id = kwargs["research_context"]["run_id"]
        yield {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "native-fetch",
                        "name": "WebFetch",
                        "input": {"url": "https://example.test/report"},
                    }
                ]
            },
        }
        yield {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "native-fetch",
                        "content": "2025 年产能为 120 万吨。",
                    }
                ]
            },
        }
        ledger = read_research_run(test_user["username"], run_id)
        record_claims(test_user["username"], run_id, [fact(ledger["passages"][0]["passage_id"])])
        yield {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "## 报告已交付\n合成公司的年产能为120万吨，尚待原文复核。"}
                ]
            },
        }

    monkeypatch.setattr(chat, "run_claude_stream", engine)
    run_ids = []
    for question in ("合成年产能", "追问有效期间"):
        response = await client.post(
            "/api/chat/stream",
            cookies=auth_cookies,
            json={"conv_id": "ledger-chat", "question": question, "skill": "prism-research"},
        )
        assert response.status_code == 200
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
        research = [e["research"] for e in events if "research" in e][-1]
        assert research["status"] == "partial"
        assert [e["ref"] for e in events if "ref" in e][0]["document_id"]
        run_ids.append(research["run_id"])
    assert len(set(run_ids)) == 2
    with get_db() as db:
        reports = db.execute(
            "SELECT source_id FROM saved_reports WHERE username=? AND source='chat'", [test_user["username"]]
        ).fetchall()
    assert {r[0] for r in reports} == set(run_ids)
    response = await client.get("/api/research/runs/" + run_ids[0], cookies=auth_cookies)
    assert response.status_code == 200
    assert response.json()["claims"][0]["semantic_status"] == "unreviewed"


@pytest.mark.asyncio
@pytest.mark.parametrize("model,expected", [("unlisted-model", 422), ("unconfigured", 503)])
async def test_chat_rejects_unavailable_models_before_side_effects(
    client, auth_cookies, test_user, monkeypatch, model, expected
):
    from server import chat
    from server.database import get_db

    monkeypatch.setattr(chat, "MODELS", {"unconfigured": {"api_key": ""}})

    async def forbidden(*args, **kwargs):
        raise AssertionError("No model should start")
        yield

    monkeypatch.setattr(chat, "run_claude_stream", forbidden)
    response = await client.post(
        "/api/chat/stream",
        cookies=auth_cookies,
        json={"question": "合成测试", "model": model, "skill": "prism-research"},
    )
    assert response.status_code == expected
    assert "api_key" not in response.text
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_empty_engine_preserves_failed_fixed_research(client, auth_cookies, monkeypatch):
    from server import chat

    async def engine(*args, **kwargs):
        yield {"type": "error", "message": "合成引擎失败"}

    monkeypatch.setattr(chat, "run_claude_stream", engine)
    response = await client.post(
        "/api/chat/stream",
        cookies=auth_cookies,
        json={"question": "合成测试", "skill": "prism-research"},
    )
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
    research = [e["research"] for e in events if "research" in e][-1]
    assert research["status"] != "completed"
    assert "engine_failed" in research["gaps"]


def test_inference_missing_failure_conditions_and_date_mismatch_stay_partial():
    run()
    pid = receipt()[0]["passage_ids"][0]
    claims = record_claims(
        "alice",
        "round-one",
        [
            {"kind": "inference", "statement": "产能必然成为收入", "supports": [pid]},
            fact(pid, period="2024"),
        ],
    )["claims"]
    assert "invalidation_missing" in claims[0]["gaps"]
    assert "period_not_in_excerpt" in claims[1]["gaps"]


def test_original_unknown_or_imprecise_dates_are_not_fabricated():
    run()
    for index, value in enumerate(["2026-10-21T08:00:00", "2026-02-30", "yesterday"]):
        receipt(
            tool="WebSearch",
            call_id=str(index),
            text=json.dumps(
                [{"url": f"https://example.test/{index}", "snippet": "测试", "published_at": value}]
            ),
        )
    assert all(d["published_at"] is None for d in read_research_run("alice", "round-one")["documents"])


@pytest.mark.asyncio
async def test_disconnect_closes_engine_and_freezes_interrupted_revision(monkeypatch):
    from server import chat
    from server.research.ledger import list_research_runs

    closed = []

    class Request:
        async def json(self):
            return {"question": "中途断线", "skill": "prism-research"}

    async def engine(*args, **kwargs):
        try:
            yield {"type": "assistant", "message": {"content": [{"type": "text", "text": "已取得的部分"}]}}
            yield {"type": "heartbeat"}
        finally:
            closed.append(True)

    monkeypatch.setattr(chat, "require_auth", lambda request: "alice")
    monkeypatch.setattr(chat, "run_claude_stream", engine)
    response = await chat.chat_stream(Request())
    events = response.body_iterator
    await anext(events)  # Clock
    await anext(events)  # Start
    await anext(events)  # Engine text
    await events.aclose()
    assert closed == [True]
    saved = list_research_runs("alice")[0]
    assert saved["status"] == "cancelled"
    detail = read_research_run("alice", saved["run_id"])
    assert detail["report_revisions"][0]["narrative"] == "已取得的部分"
    assert "interrupted" in saved["gaps"]


@pytest.mark.asyncio
async def test_research_api_never_leaks_other_user_or_deleted_run(client, auth_cookies, test_user):
    from server.database import get_db

    run("foreign-run", user="somebody-else")
    run("own-run", user=test_user["username"])
    response = await client.get("/api/research/runs/foreign-run", cookies=auth_cookies)
    assert response.status_code == 404
    response = await client.get("/api/research/runs", cookies=auth_cookies)
    assert {r["run_id"] for r in response.json()["runs"]} == {"own-run"}
    with get_db() as db:
        db.execute(
            "INSERT INTO conversations(id,username,title,skill) VALUES('deleted-ledger',?,'x','prism-research')",
            [test_user["username"]],
        )
    run("deleted-run", user=test_user["username"], conv="deleted-ledger")
    with get_db() as db:
        db.execute("UPDATE conversations SET deleted_at='2026-10-21' WHERE id='deleted-ledger'")
    response = await client.get("/api/research/runs/deleted-run", cookies=auth_cookies)
    assert response.status_code == 404


def test_exact_time_future_metadata_not_replaced_by_same_day():
    run()
    receipt(
        tool="WebSearch",
        text=json.dumps(
            [
                {
                    "url": "https://example.test/later",
                    "snippet": "120万吨",
                    "published_at": "2026-10-21T03:00:00+08:00",
                }
            ]
        ),
    )
    assert "published_after_as_of" in read_research_run("alice", "round-one")["documents"][0]["gaps"]


def test_explicit_single_worker_restart_recovery_preserves_receipts_and_old_revisions():
    from server.research import recover_interrupted_runs

    run("finished")
    old = finalize_research_run("alice", "finished", "原始固定报告")
    run("unfinished")
    pid = receipt("unfinished")[0]["passage_ids"][0]
    record_claims("alice", "unfinished", [fact(pid)])
    before = read_research_run("alice", "unfinished")
    assert recover_interrupted_runs() == 1
    after = read_research_run("alice", "unfinished")
    assert after["run"]["status"] == "partial"
    assert after["run"]["as_of"] == before["run"]["as_of"]
    assert after["documents"] == before["documents"]
    assert after["passages"] == before["passages"]
    assert after["claims"] == before["claims"]
    assert "interrupted_by_restart" in after["gaps"]
    assert len(after["report_revisions"]) == 1
    assert read_research_run("alice", "finished")["report_revisions"] == [old]
    assert recover_interrupted_runs() == 0


@pytest.mark.asyncio
async def test_explicit_engine_cancellation_is_not_misreported_as_failure(client, auth_cookies, monkeypatch):
    from server import chat

    async def engine(*args, **kwargs):
        yield {"type": "assistant", "message": {"content": [{"type": "text", "text": "已取得部分线索"}]}}
        yield {"type": "cancelled", "message": "用户已取消本轮研究"}

    monkeypatch.setattr(chat, "run_claude_stream", engine)
    response = await client.post(
        "/api/chat/stream",
        cookies=auth_cookies,
        json={"question": "中途取消", "skill": "prism-research"},
    )
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
    research = [e["research"] for e in events if "research" in e][-1]
    assert research["status"] == "cancelled"
    assert "engine_failed" not in research["gaps"]
    assert "用户已取消" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["success", "error", "missing"])
async def test_delivery_notice_distinguishes_structure_gap_from_engine_failure(
    client, auth_cookies, monkeypatch, terminal
):
    from server import chat

    async def engine(*args, **kwargs):
        yield {"type": "assistant", "message": {"content": [{"type": "text", "text": "这是一段正常说明。"}]}}
        if terminal != "missing":
            yield {
                "type": "result",
                "subtype": "success" if terminal == "success" else "error_max_turns",
                "is_error": terminal == "error",
            }

    monkeypatch.setattr(chat, "run_claude_stream", engine)
    response = await client.post(
        "/api/chat/stream",
        cookies=auth_cookies,
        json={"question": "说明研究边界", "skill": "prism-research"},
    )
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
    note = next(e["text"] for e in events if e.get("delivery"))
    assert note.startswith("\n\n**本轮尚未形成完整可核查报告**")
    assert ">" not in note
    assert "形成最终结论前结束" not in note
    research = [e["research"] for e in events if "research" in e][-1]
    assert research["status"] == "partial"
    assert ("engine_failed" in research["gaps"]) is (terminal != "success")
    if terminal == "success":
        assert "研究引擎" not in note
