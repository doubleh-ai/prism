"""Real owned native tools, audited dispatcher and ledger; synthetic data only."""

import asyncio
import copy
import json
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server import capabilities as cap
from server.agent import tools as T
from server.database import get_db
from server.reports import save_report_snapshot
from server.research import create_research_run, read_research_run
from server.research_context import EvidenceTracker, build_research_context
from server.work_lifecycle import Lease, Manager


@pytest.fixture
def native(test_user):
    user = test_user["username"]
    with get_db() as db:
        db.execute("INSERT INTO conversations(id,username) VALUES('native-owned',?)", [user])
        db.execute("INSERT INTO users(username,password_hash) VALUES('other','synthetic-only')")
        db.execute("INSERT INTO conversations(id,username) VALUES('other-owned','other')")
    research = build_research_context(run_id="native-round")
    create_research_run(user, "native-owned", "合成研究", research)
    manager = Manager(max_workers=2)
    parent = manager.admit("agent", persist_required=False)
    tracker = EvidenceTracker(research["run_id"])
    with cap.native_run_context(user, "native-owned", research["run_id"], parent_lease=parent) as context:
        value = T.NativeTools(context, parent, tracker)
        yield SimpleNamespace(manager=manager, parent=parent, tracker=tracker, tools=value, context=context)
    parent.release()
    manager._pool.shutdown(wait=True, cancel_futures=False)


def fetched(**changes):
    return {
        "status": "ok",
        "url": "https://www.sse.com.cn/disclosure",
        "title": "合成公告",
        "text": "本合成资料的产能为120万吨。",
        "retrieved_at": "2026-10-25T18:00:00+08:00",
        "published_at": "2026-10-20",
        "original_obtained": True,
        "original_preserved": False,
        "content_scope": "server_extracted_excerpt",
        "text_truncated": False,
        **changes,
    }


def discovery_result(**kw):
    return {
        "status": "ok",
        "as_of": "2020-01-20T12:00:00+08:00",
        "items": [
            {
                "url": "https://www.sse.com.cn/disclosure",
                "title": "合成披露",
                "snippet": "合成产能公告，仍需核对原文。",
                "published_at": "2020-01-10",
                "published_at_basis": "source_index_metadata",
                "index_url": "https://query.sse.com.cn/fixture",
                "index_body_sha256": "a" * 64,
                "retrieved_at": "2020-01-20T12:00:00+08:00",
                "source_class": "official_disclosure",
                "original_obtained": False,
                "original_verified": False,
                "content_scope": "search_snippet",
            }
        ],
        "coverage": [{"provider": "sse", "status": "collected", "complete_search": False}],
        "complete_search": False,
        **kw,
    }


async def test_source_discovery_ingests_each_index_receipt_then_original_with_separate_hash_and_date(
    native, monkeypatch
):
    calls = []

    def find(arguments):
        calls.append(arguments)
        return discovery_result()

    monkeypatch.setattr(T, "_find_sources", find)
    inputs = {"query": "合成", "providers": ["sse"], "start_date": "2020-01-01", "end_date": "2020-01-20"}
    result = await native.tools.execute("source_find", inputs, "discovery-one")
    assert result["ok"] and len(result["refs"]) == 1
    assert result["refs"][0]["original_obtained"] is False
    again = await native.tools.execute("source_find", inputs, "discovery-one")
    assert result == again and len(calls) == 1
    assert (await native.tools.execute("source_find", {**inputs, "query": "different"}, "discovery-one"))[
        "error"
    ]["code"] == "conflict"
    data = read_research_run(native.context["username"], native.context["run_id"])
    doc = data["documents"][0]
    assert doc["content_scope"] == "search_snippet" and doc["publication_basis"] == "source_index_metadata"
    assert doc["source_index"] == {"url": "https://query.sse.com.cn/fixture", "body_sha256": "a" * 64}
    assert doc["retrieved_body_sha256"] is None
    monkeypatch.setattr(T, "_fetch_source", lambda *a, **k: fetched(published_at=None, body_sha256="b" * 64))
    original = await native.tools.execute(
        "source_read", {"url": "https://www.sse.com.cn/disclosure"}, "read-one"
    )
    assert original["ok"] and original["data"]["published_at"] == "2020-01-10"
    assert original["data"]["published_at_basis"] == "source_index_metadata"
    assert original["data"]["publication_index"]["body_sha256"] == "a" * 64
    docs = read_research_run(native.context["username"], native.context["run_id"])["documents"]
    full = next(d for d in docs if d["content_scope"] == "tool_returned_text")
    assert full["retrieved_body_sha256"] == "b" * 64 and full["source_index"]["body_sha256"] == "a" * 64
    assert full["published_at"] == "2020-01-10" and "original_not_verified" in full["gaps"]
    assert full["original_obtained"] is False and full["independence_status"] == "not_assessed"


async def test_discovery_no_match_and_unavailable_are_distinct_no_fake_references(native, monkeypatch):
    monkeypatch.setattr(T, "_find_sources", lambda *a: discovery_result(items=[]))
    result = await native.tools.execute("source_find", {"query": "合成"}, "empty-search")
    assert result["ok"] and result["refs"] == [] and result["data"]["complete_search"] is False
    monkeypatch.setattr(T, "_find_sources", lambda *a: discovery_result(items=[], status="unavailable"))
    result = await native.tools.execute("source_find", {"query": "合成"}, "failed-search")
    assert result["ok"] is False and result["error"]["code"] == "source_unavailable"
    assert read_research_run(native.context["username"], native.context["run_id"])["documents"] == []


async def test_custom_source_receipt_is_owned_persistent_history_without_query_or_document_duplication(
    native, monkeypatch
):
    monkeypatch.setattr(T, "_find_sources", lambda *a: discovery_result())
    result = await native.tools.execute("source_find", {"stock_code": "600001"}, "metadata-history")
    assert result["ok"]
    call = result["call"]
    owner = native.context["username"]
    assert cap.list_calls(owner, conv_id=native.context["conv_id"])[0]["call_id"] == call["call_id"]
    assert cap.list_calls(owner, view="reports")[0]["label"] == "查找公开资料"
    snapshot = cap.get_call_snapshot(owner, call["call_id"])
    assert snapshot["params"] == {} and snapshot["result"] == {"research_run_id": native.context["run_id"]}
    assert snapshot["target"] == {"view": "reports", "run_id": native.context["run_id"]}
    assert "合成产能公告" not in json.dumps(snapshot, ensure_ascii=False)
    with pytest.raises(Exception) as denied:
        cap.get_call_snapshot("other", call["call_id"])
    assert getattr(denied.value, "status_code", None) == 404
    with get_db() as db:
        db.execute("UPDATE conversations SET deleted_at=1 WHERE id=?", [native.context["conv_id"]])
    assert cap.list_calls(owner) == []
    with pytest.raises(Exception) as removed:
        cap.get_call_snapshot(owner, call["call_id"])
    assert getattr(removed.value, "status_code", None) == 404


async def test_missing_custom_audit_terminal_is_not_ignored_or_automatically_refetched(native, monkeypatch):
    calls = []

    def find(*a):
        calls.append(1)
        return discovery_result()

    monkeypatch.setattr(T, "_find_sources", find)
    with get_db() as db:
        db.execute(
            "CREATE TRIGGER reject_custom_audit BEFORE INSERT ON capability_calls BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        await native.tools.execute("source_find", {"query": "合成"}, "unconfirmed-audit")
    result = await native.tools.execute("source_find", {"query": "合成"}, "unconfirmed-audit")
    assert result["execution_status"] == "unknown" and calls == [1]
    assert cap.list_calls(native.context["username"]) == []


async def test_original_page_metadata_wins_and_redirect_cannot_borrow_index_date(native, monkeypatch):
    monkeypatch.setattr(T, "_find_sources", lambda *a: discovery_result())
    await native.tools.execute("source_find", {"query": "合成"}, "lookup")
    monkeypatch.setattr(
        T,
        "_fetch_source",
        lambda *a, **k: fetched(published_at="2020-01-12", published_at_basis="page_metadata"),
    )
    result = await native.tools.execute(
        "source_read", {"url": "https://www.sse.com.cn/disclosure"}, "dated-original"
    )
    assert (
        result["data"]["published_at"] == "2020-01-12"
        and result["data"]["published_at_basis"] == "page_metadata"
    )
    monkeypatch.setattr(
        T,
        "_fetch_source",
        lambda *a, **k: fetched(url="https://www.sse.com.cn/redirected", published_at=None),
    )
    result = await native.tools.execute(
        "source_read", {"url": "https://www.sse.com.cn/disclosure"}, "redirected-original"
    )
    assert result["data"]["published_at"] is None and result["refs"][0]["published_at"] is None


async def test_cancelled_discovery_keeps_worker_owned_and_does_not_refetch_same_call(native, monkeypatch):
    entered, released = threading.Event(), threading.Event()
    calls = []

    def blocking(arguments):
        calls.append(arguments)
        entered.set()
        released.wait(3)
        return discovery_result()

    monkeypatch.setattr(T, "_find_sources", blocking)
    task = asyncio.create_task(native.tools.execute("source_find", {"query": "合成"}, "stopped-lookup"))
    for _ in range(200):
        if entered.is_set():
            break
        await asyncio.sleep(0.005)
    assert entered.is_set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sum(native.manager.observe()["leases"].values()) > 1
    result = await native.tools.execute("source_find", {"query": "合成"}, "stopped-lookup")
    assert result["execution_status"] == "unknown" and len(calls) == 1
    released.set()
    for _ in range(200):
        if sum(native.manager.observe()["leases"].values()) == 1:
            break
        await asyncio.sleep(0.005)
    assert sum(native.manager.observe()["leases"].values()) == 1


def report(user, conv, source_id, **payload):
    return save_report_snapshot(
        user,
        {
            "source": "manual",
            "source_id": source_id,
            "conv_id": conv,
            "title": "液冷历史研究",
            "report": {"summary": "液冷历史研究，待核验。", **payload},
        },
    )


def test_catalogue_requires_no_lease_and_matches_real_definition_schema(native, monkeypatch):
    expected = native.tools.definitions()
    monkeypatch.setattr(cap, "_run_parent", lambda *_: pytest.fail("static catalogue cannot authorize work"))
    catalogue = T.native_catalog()
    assert type(catalogue) is list
    assert {row["id"] for row in catalogue} == {row["name"] for row in expected}
    assert {r["id"]: r["input_schema"] for r in catalogue} == {r["name"]: r["input_schema"] for r in expected}
    assert all(
        {"label", "description", "source_kind", "read_only", "target_view"} <= set(row) for row in catalogue
    )
    catalogue[0]["input_schema"].clear()
    assert T.native_catalog()[0]["input_schema"]  # No mutation of registered schemas.


@pytest.mark.parametrize(
    "name",
    [
        "Bash",
        "Read",
        "WebSearch",
        "WebFetch",
        "mcp__prism__analysis_start",
        "analysis_start",
        "vibe_start",
        "vibe_followup",
        "scan_start",
        "watchlist_update",
        "indicator_translate",
        "report_save",
    ],
)
async def test_legacy_cli_and_ungranted_tools_never_reach_dispatch(native, monkeypatch, name):
    dispatch = AsyncMock(side_effect=AssertionError("forbidden tool dispatched"))
    monkeypatch.setattr(cap, "dispatch_call", dispatch)
    result = await native.tools.execute(name, {}, "forbidden")
    assert not result["ok"] and result["error"]["code"] == "unknown_tool"
    dispatch.assert_not_called()
    assert name not in {row["name"] for row in native.tools.definitions()}


def test_real_binding_and_exact_owner_lease_are_required(native):
    forged = Lease(native.manager, "agent", None, False)  # Real class, never admitted.
    for context, parent, tracker in (
        (native.context, forged, native.tracker),
        ({**native.context, "username": "other"}, native.parent, native.tracker),
        ({**native.context, "conv_id": "other-owned"}, native.parent, native.tracker),
        (native.context, native.parent, EvidenceTracker("another-run")),
    ):
        with pytest.raises(T.NativeToolError):
            T.NativeTools(context, parent, tracker)


async def test_futures_exchange_report_reaches_the_owned_agent(native, monkeypatch):
    from server import futures

    report = {
        "table": "铜",
        "status": "collected",
        "unit": "吨",
        "quantity": 17,
        "change": -2,
        "data_as_of": "2026-10-30",
        "source_url": "https://www.shfe.com.cn/data/tradedata/future/stockdata/dailystock_20261030/ZH/all.html",
    }
    monkeypatch.setattr(
        futures, "snapshot", AsyncMock(return_value={"symbol": "CU", "warehouse_receipt": report})
    )
    result = await native.tools.execute("futures_snapshot", {"symbol": "CU"}, "synthetic-copper")
    assert result["ok"] and result["data"]["warehouse_receipt"] == report
    assert result["call"]["target"]["href"] == "/futures?symbol=CU"


async def test_platform_dispatch_is_durable_idempotent_and_not_external_evidence(native):
    first = await native.tools.execute("indicator_list", {}, "actual-platform")
    assert first["ok"] and first["source_kind"] == "platform_indicator"
    assert first["evidence_status"] == "platform_result_not_external_verification"
    assert first["call"]["capability_id"] == "indicator_list"
    replay = await native.tools.execute("indicator_list", {}, "actual-platform")
    assert replay == first
    clone = T.NativeTools(native.context, native.parent, native.tracker)
    assert await clone.execute("indicator_list", {}, "actual-platform") == first
    with get_db() as db:
        rows = db.execute(
            "SELECT status,response_json FROM capability_calls WHERE run_id=?", [native.context["run_id"]]
        ).fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "completed"
    assert json.loads(rows[0]["response_json"])["call"]["call_id"] == first["call"]["call_id"]
    assert read_research_run(native.context["username"], native.context["run_id"])["documents"] == []


@pytest.mark.parametrize(
    "name,args,call_id",
    [
        ("indicator_list", {"username": "other"}, "extra-owner"),
        ("knowledge_search", {"query": "x", "limit": True}, "bool-limit"),
        ("knowledge_search", {"query": "x", "limit": 21}, "big-limit"),
        ("knowledge_get", {"id": "../../private"}, "path"),
        ("source_read", {"url": "https://www.sse.com.cn/?api_key=synthetic"}, "credential-url"),
        ("source_read", {"url": "https://127.0.0.1/"}, "private-url"),
        ("source_read", {"url": "https://www.sse.com.cn:8443/"}, "wrong-port"),
        ("indicator_list", {}, "../forged-id"),
        ("indicator_list", {"value": float("nan")}, "non-finite"),
        ("knowledge_search", {"query": "a" * 40000}, "oversize"),
    ],
)
async def test_invalid_arguments_are_rejected_before_any_work(native, monkeypatch, name, args, call_id):
    dispatch = AsyncMock()
    monkeypatch.setattr(cap, "dispatch_call", dispatch)
    monkeypatch.setattr(T, "_fetch_source", lambda *_a, **_k: pytest.fail("invalid source called"))
    result = await native.tools.execute(name, args, call_id)
    assert not result["ok"] and result["error"]["code"] == "invalid_arguments"
    dispatch.assert_not_called()
    assert native.manager.observe()["pending_cf"] == 0


async def test_historical_knowledge_owner_current_run_and_deleted_record_boundaries(native):
    user = native.context["username"]
    mine = report(user, "native-owned", "mine")
    foreign = report("other", "other-owned", "foreign", summary="private-other-record")
    current = report(user, "native-owned", "current", run_id=native.context["run_id"])
    found = await native.tools.execute("knowledge_search", {"query": "液冷"}, "history-search")
    ids = {row["id"] for row in found["data"]["results"]}
    assert "report:" + mine["id"] in ids
    assert "report:" + foreign["id"] not in ids and "report:" + current["id"] not in ids
    own = await native.tools.execute("knowledge_get", {"id": "report:" + mine["id"]}, "own-history")
    assert own["ok"] and own["data"]["document"]["semantic_status"] == "unreviewed"
    for identifier in (foreign["id"], current["id"]):
        denied = await native.tools.execute(
            "knowledge_get", {"id": "report:" + identifier}, "get-" + identifier
        )
        assert denied["error"]["code"] == "not_found" and denied["data"] == {}
    with get_db() as db:
        db.execute("UPDATE conversations SET deleted_at=1 WHERE id='native-owned'")
    denied = await native.tools.execute("knowledge_get", {"id": "report:" + mine["id"]}, "own-history")
    assert denied["error"]["code"] == "unavailable_context"  # Even cached data cannot bypass deletion.


async def test_successful_source_result_creates_actual_owned_ledger_receipt(native, monkeypatch):
    calls = []

    def fake_fetch(url, *, allowed_hosts):
        calls.append((url, allowed_hosts))
        return fetched()

    monkeypatch.setattr(T, "_fetch_source", fake_fetch)
    result = await native.tools.execute("source_read", {"url": "https://www.sse.com.cn/start"}, "source-one")
    assert result["ok"] and result["refs"][0]["url"] == fetched()["url"]
    assert result["data"]["original_verified"] is False
    snapshot = read_research_run(native.context["username"], native.context["run_id"])
    assert len(snapshot["documents"]) == 1 and len(snapshot["passages"]) == 1
    document = snapshot["documents"][0]
    assert document["tool_call_id"] == "source-one" and document["url"] == fetched()["url"]
    assert document["original_obtained"] is False and document["published_at"] == "2026-10-20"
    assert result["refs"][0]["document_id"] == document["document_id"]
    assert native.tracker.completed == {"source-one"}
    assert (
        await native.tools.execute("source_read", {"url": "https://www.sse.com.cn/start"}, "source-one")
        == result
    )
    assert len(calls) == 1 and calls[0][1] == ()
    changed = await native.tools.execute(
        "source_read", {"url": "https://www.sse.com.cn/changed"}, "source-one"
    )
    assert changed["error"]["code"] == "conflict" and len(calls) == 1


@pytest.mark.parametrize(
    "value",
    [
        {"status": "http_error", "url": "https://www.sse.com.cn/", "text": ""},
        fetched(status="unsupported_pdf", text=""),
        fetched(url="https://www.sse.com.cn/?token=synthetic-secret"),
        fetched(text=""),
    ],
)
async def test_failed_fetch_never_registers_source_or_document(native, monkeypatch, value):
    monkeypatch.setattr(T, "_fetch_source", lambda *_a, **_k: copy.deepcopy(value))
    result = await native.tools.execute("source_read", {"url": "https://www.sse.com.cn/"}, "failed-source")
    assert not result["ok"] and result["refs"] == [] and result["call"]["status"] == "failed"
    assert native.tracker.completed == set() and native.tracker.calls == {}
    assert read_research_run(native.context["username"], native.context["run_id"])["documents"] == []
    assert "synthetic-secret" not in json.dumps(result)


async def test_cancelled_waiter_retains_owned_fetch_and_cannot_replay_its_call(native, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def blocking_fetch(*_args, **_kwargs):
        calls.append(True)
        entered.set()
        assert release.wait(3), "synthetic worker was not released"
        return fetched()

    monkeypatch.setattr(T, "_fetch_source", blocking_fetch)
    task = asyncio.create_task(
        native.tools.execute("source_read", {"url": "https://www.sse.com.cn/"}, "cancelled-source")
    )
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.001)
        assert entered.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        observed = native.manager.observe()
        assert observed["pending_cf"] == 1 and observed["leases"]["auxiliary"] == 1
        replay = await native.tools.execute(
            "source_read", {"url": "https://www.sse.com.cn/"}, "cancelled-source"
        )
        assert replay["execution_status"] == "unknown" and replay["automatic_retry"] is False
        assert len(calls) == 1 and not native.tracker.completed
    finally:
        release.set()
        assert await native.parent.wait_owned(timeout=1)
    assert native.manager.observe()["pending_cf"] == 0
    assert native.manager.observe()["leases"]["auxiliary"] == 0
    assert read_research_run(native.context["username"], native.context["run_id"])["documents"] == []


async def test_output_sanitization_truncation_and_cache_copy(native, monkeypatch):
    monkeypatch.setattr(
        T,
        "_knowledge_call",
        lambda *_a: {
            "api_key": "synthetic-key",
            "password": "synthetic-password",
            "large": ["中" * 16000] * 8,
        },
    )
    result = await native.tools.execute("knowledge_search", {"query": "x"}, "large")
    assert len(T._encoded(result)) <= T.MAX_OUTPUT_BYTES and "synthetic-key" not in json.dumps(result)
    result["data"]["caller_mutation"] = True
    replay = await native.tools.execute("knowledge_search", {"query": "x"}, "large")
    assert "caller_mutation" not in replay["data"]
    monkeypatch.setattr(T, "_fetch_source", lambda *_a, **_k: fetched(text="x" * 20000))
    truncated = await native.tools.execute("source_read", {"url": "https://www.sse.com.cn/"}, "truncated")
    assert truncated["ok"] and truncated["data"]["text_truncated"] is True
