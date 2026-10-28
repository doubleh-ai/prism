"""Regression checks for report delivery, durable records and market time."""

import asyncio
import json
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


@pytest.mark.parametrize("has_dashboard", [False, True])
@pytest.mark.asyncio
async def test_saved_analysis_dashboard_remains_owned_history(has_dashboard):
    from server.database import get_db

    report = {"final_decision": "已有合成报告正文"}
    if has_dashboard:
        report["structured"] = {"thesis": "已保存整理结果", "confidence": None}
    with get_db() as db:
        db.execute(
            "INSERT INTO analysis_runs(id,username,ticker,mode,trade_date,status,report_json,started_at,updated_at) "
            "VALUES('saved-dashboard','owner','600000','fast','2026-10-28','done',?,1,1)",
            [json.dumps(report)],
        )
    assert ae.get_analysis_record("saved-dashboard", "owner")["report"] == report
    assert ae.get_analysis_record("saved-dashboard", "other") is None


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
    from server.database import get_db

    with get_db() as db:
        db.execute(
            "INSERT INTO analysis_runs(id,username,ticker,mode,trade_date,status,started_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,1)",
            [
                entry[key]
                for key in ("id", "username", "ticker", "mode", "trade_date", "status", "started_at")
            ],
        )
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


@pytest.mark.asyncio
async def test_retired_engine_creates_no_failed_task_or_external_import():
    from fastapi import HTTPException

    from server.database import get_db

    with pytest.raises(HTTPException) as blocked:
        await ae.start_analysis("600000", username="owner")
    assert blocked.value.status_code == 503
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_saved_analysis_stream_is_repeatable_without_resuming_work(client, auth_cookies):
    from server.database import get_db

    report = {"final_decision": "已完成合成报告"}
    with get_db() as db:
        db.execute(
            "INSERT INTO analysis_runs(id,username,ticker,mode,trade_date,status,report_json,started_at,updated_at) "
            "VALUES('repeatable','testuser','600519','fast','2026-10-28','done',?,1,1)",
            [json.dumps(report)],
        )
    first, second = await asyncio.gather(
        *[client.get("/api/analysis/repeatable/stream", cookies=auth_cookies) for _ in range(2)]
    )
    assert first.status_code == second.status_code == 200 and first.text == second.text
    frame = json.loads(first.text.removeprefix("data: "))
    assert frame["type"] == "done" and frame["report"] == report
    assert ae.get_analysis_record("repeatable", "testuser")["status"] == "done"


@pytest.mark.asyncio
async def test_incomplete_saved_stream_returns_interrupted_history_immediately(client, auth_cookies):
    from server.database import get_db

    with get_db() as db:
        db.execute(
            "INSERT INTO analysis_runs(id,username,ticker,mode,trade_date,status,started_at,updated_at) "
            "VALUES('incomplete','testuser','600519','fast','2026-10-28','running',1,1)"
        )
    response = await client.get("/api/analysis/incomplete/stream", cookies=auth_cookies)
    frame = json.loads(response.text.removeprefix("data: "))
    assert frame["type"] == "error" and "未自动重新启动" in frame["message"]
    with get_db() as db:
        assert db.execute("SELECT status FROM analysis_runs WHERE id='incomplete'").fetchone()[0] == "running"
