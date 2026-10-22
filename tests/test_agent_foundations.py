"""Regression checks for report delivery, durable records and market time."""

import asyncio
import json
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

from server import analysis_engine as ae
from server.market.clock import market_timestamp


def test_market_dates_have_explicit_shanghai_timezone():
    expected = datetime(2026, 10, 21, tzinfo=timezone(timedelta(hours=8))).timestamp()
    assert market_timestamp("2026-10-21") == expected
    assert market_timestamp("not-a-date") is None


@pytest.mark.asyncio
async def test_quote_keeps_old_source_time_separate_from_retrieval(monkeypatch):
    from server.market import tencent_provider as provider

    fields = [""] * 38
    fields[1:7] = ["Example", "600000", "10", "9", "9.5", "100"]
    fields[30] = "20000104150000"
    monkeypatch.setattr(provider, "_fetch_tencent_quotes", lambda *args: {"600000": fields})
    monkeypatch.setattr(provider.time, "time", lambda: 1_800_000_000)
    quote = await provider.TencentProvider().get_quote("600000")
    assert quote.timestamp == market_timestamp("20000104150000", "%Y%m%d%H%M%S")
    assert quote.data_timestamp == quote.timestamp
    assert quote.timestamp != quote.retrieved_at == 1_800_000_000
    assert quote.timestamp_kind == "trade"


@pytest.mark.asyncio
async def test_quote_without_source_time_does_not_invent_one(monkeypatch):
    from server.market import tencent_provider as provider

    fields = [""] * 38
    fields[1:7] = ["Example", "600000", "10", "9", "9.5", "100"]
    monkeypatch.setattr(provider, "_fetch_tencent_quotes", lambda *args: {"600000": fields})
    quote = await provider.TencentProvider().get_quote("600000")
    assert quote.data_timestamp is None
    assert quote.timestamp_kind == "retrieved"


@pytest.mark.parametrize("structured_fails", [False, True])
@pytest.mark.asyncio
async def test_report_stream_finishes_after_optional_dashboard(monkeypatch, structured_fails):
    class Graph:
        def __init__(self, **kwargs):
            self.graph = self

        def prepare_graph_run(self, ticker, date):
            return {"messages": []}, {}, None

        def stream(self, state, **kwargs):
            yield {"final_trade_decision": "已有报告正文"}

        def finalize_graph_run(self, *args):
            return "观察"

    module = types.ModuleType("tradingagents.graph.trading_graph")
    module.TradingAgentsGraph = Graph
    monkeypatch.setitem(sys.modules, "tradingagents.graph.trading_graph", module)
    monkeypatch.setattr(ae, "_ensure_astock", lambda: None)
    monkeypatch.setattr(ae, "_build_config", lambda *args, **kwargs: {})
    monkeypatch.setattr(ae, "ASTOCK_CACHE_TTL_SECONDS", 0)

    def extract(*args):
        if structured_fails:
            raise RuntimeError("synthetic extraction failure")
        return {"thesis": "整理结果", "confidence": None}

    monkeypatch.setattr(ae, "_extract_structured", extract)
    aid = await ae.start_analysis("600000", username="owner")
    try:
        events = [json.loads(event.removeprefix("data: ")) async for event in ae.stream_events(aid)]
        types_seen = [event["type"] for event in events]
        assert types_seen[-1] == "done"
        assert types_seen.count("done") == 1
        assert types_seen.index("report") < types_seen.index("done")
        if not structured_fails:
            assert types_seen.index("report") < types_seen.index("structured") < types_seen.index("done")
            assert events[-1]["report"]["structured"]["thesis"] == "整理结果"
        assert ae.get_analysis_record(aid, "other") is None
        ae._store.pop(aid)
        restored = ae.get_analysis_record(aid, "owner")
        assert restored["status"] == "done"
        assert "已有报告正文" in restored["report"]["final_decision"]
    finally:
        ae._store.pop(aid, None)


@pytest.mark.asyncio
async def test_durable_incomplete_record_does_not_claim_to_resume():
    entry = {
        "id": "interrupted-test",
        "username": "owner",
        "ticker": "600000",
        "mode": "fast",
        "trade_date": "2026-10-21",
        "status": "running",
        "started_at": 1.0,
    }
    ae._persist_analysis(entry)
    record = ae.get_analysis_record(entry["id"], "owner")
    assert record["status"] == "interrupted"
    assert "未自动重新启动" in record["error"]
    assert ae.get_analysis_record(entry["id"], "other") is None


@pytest.mark.asyncio
async def test_industry_stock_filter_is_complete_and_seed_is_unverified(client):
    response = await client.get("/api/industry/graph", params={"ticker": "688981", "depth": 1})
    assert response.status_code == 200
    data = response.json()
    nodes = {node["id"] for node in data["nodes"]}
    assert len(nodes) > 1
    assert any(node["source_ticker"] == "688981" for node in data["nodes"])
    assert all(edge["source_node_id"] in nodes and edge["target_node_id"] in nodes for edge in data["edges"])
    assert all(node["confidence"] is None and node["market_share"] is None for node in data["nodes"])
    assert all(edge["relation"] == "background_link" for edge in data["edges"])
    empty = (await client.get("/api/industry/graph", params={"q": "no-such-company"})).json()
    assert empty["nodes"] == empty["edges"] == []


@pytest.mark.asyncio
async def test_industry_depth_does_not_escape_category_filter(client):
    data = (
        await client.get("/api/industry/graph", params={"ticker": "300750", "category": "锂电池", "depth": 3})
    ).json()
    assert data["nodes"]
    assert all(node["category"] == "锂电池" for node in data["nodes"])
    assert (await client.get("/api/industry/graph", params={"depth": 20})).status_code == 400


def test_structured_summary_does_not_create_unsourced_trade_numbers(monkeypatch):
    payload = {
        "thesis": "原文摘要",
        "confidence": 99,
        "metrics": {"risk_score": 1},
        "trade": {"target_price": "1000元", "stop_loss": "10元", "position": "80%"},
    }
    monkeypatch.setattr(ae, "_parse_json_lenient", lambda *args: payload)
    # This test owns only dashboard post-processing. Actual child registration
    # and process/tool boundaries are exercised in the lifecycle/auxiliary tests.
    monkeypatch.setattr("server.services.auxiliary_model.run_text", lambda *args, **kwargs: "{}")
    result = ae._extract_structured({"final_decision": "原文未给出价格或仓位"}, "600000", "Example")
    assert result["confidence"] is None
    assert result["metrics"]["risk_score"] is None
    assert all(value is None for value in result["trade"].values())


@pytest.mark.asyncio
async def test_missing_engine_retains_readable_failed_record(monkeypatch):
    def unavailable():
        raise ImportError("synthetic unavailable engine")

    monkeypatch.setattr(ae, "_ensure_astock", unavailable)
    monkeypatch.setattr(ae, "ASTOCK_CACHE_TTL_SECONDS", 0)
    aid = await ae.start_analysis("600000", username="owner")
    try:
        events = [json.loads(event.removeprefix("data: ")) async for event in ae.stream_events(aid)]
        assert events[-1]["type"] == "error"
        assert ae.get_analysis_record(aid, "owner")["status"] == "error"
        ae._store.pop(aid)
        assert ae.get_analysis_record(aid, "owner")["status"] == "error"
        assert ae.get_analysis_record(aid, "other") is None
    finally:
        ae._store.pop(aid, None)


@pytest.mark.asyncio
async def test_analysis_stream_supports_two_readers_and_terminal_replay():
    aid = "multi-reader"
    report = {"final_decision": "已完成报告"}
    ae._store[aid] = {
        "id": aid,
        "status": "done",
        "started_at": 1,
        "report": report,
        "events": [
            {"type": "agent_output", "text": "过程"},
            {"type": "report", "report": report},
            {"type": "done", "report": report},
        ],
    }

    async def read():
        return [json.loads(value.removeprefix("data: ")) async for value in ae.stream_events(aid)]

    try:
        first, second = await asyncio.gather(read(), read())
        assert first == second
        assert [event["type"] for event in first] == ["agent_output", "report", "done"]
        ae._store[aid]["events"] = []
        assert (await read())[-1]["report"] == report
        assert ae._store[aid]["status"] == "done"
    finally:
        ae._store.pop(aid, None)


@pytest.mark.asyncio
async def test_view_timeout_does_not_mark_background_analysis_failed(monkeypatch):
    import time

    aid = "view-timeout"
    ae._store[aid] = {
        "id": aid,
        "status": "running",
        "started_at": time.time() - 100,
        "events": [],
        "report": None,
    }
    monkeypatch.setattr(ae, "ASTOCK_HARD_TIMEOUT_SECONDS", 60)
    try:
        events = [json.loads(value.removeprefix("data: ")) async for value in ae.stream_events(aid)]
        assert events[-1]["type"] == "error"
        assert "后台任务可能仍在运行" in events[-1]["message"]
        assert ae._store[aid]["status"] == "running"
    finally:
        ae._store.pop(aid, None)
