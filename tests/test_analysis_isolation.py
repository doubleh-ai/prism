"""Saved analysis reads remain account-scoped, passive and date-preserving."""

import json

import pytest

from server import analysis_engine as engine
from server.database import get_db


def seed(identifier="saved", owner="owner", status="done", payload=None):
    report = payload if payload is not None else json.dumps({"final_decision": "合成历史观点"})
    with get_db() as db:
        db.execute(
            "INSERT INTO analysis_runs(id,username,ticker,mode,trade_date,status,report_json,started_at,updated_at) "
            "VALUES(?,?,?,'deep','2026-09-17',?,?,1,2)",
            [identifier, owner, "600519", status, report],
        )


def snapshot():
    with get_db() as db:
        return [tuple(row) for row in db.execute("SELECT * FROM analysis_runs ORDER BY id")]


def test_same_stock_history_is_separate_for_two_owners():
    seed("one", "owner-a", payload='{"final_decision":"A historical view"}')
    seed("two", "owner-b", payload='{"final_decision":"B historical view"}')
    before = snapshot()
    assert engine.get_analysis_record("one", "owner-a")["report"]["final_decision"] == "A historical view"
    assert engine.get_analysis_record("two", "owner-b")["report"]["final_decision"] == "B historical view"
    assert engine.get_analysis_record("one", "owner-b") is None
    assert engine.get_analysis_record("two", "owner-a") is None
    assert snapshot() == before


@pytest.mark.parametrize("status", ["starting", "running"])
def test_unfinished_history_is_read_as_interrupted_without_mutating_or_resuming(status):
    seed(status=status)
    before = snapshot()
    value = engine.get_analysis_record("saved", "owner")
    assert value["status"] == "interrupted" and "未自动重新启动" in value["error"]
    assert value["report"] == {"final_decision": "合成历史观点"}
    assert snapshot() == before


@pytest.mark.parametrize("payload", ["not JSON", "[]", '"text"', '{"_analysis_meta":[]}'])
def test_malformed_saved_data_does_not_trigger_model_repair_or_database_writes(payload):
    seed(payload=payload)
    before = snapshot()
    value = engine.get_analysis_record("saved", "owner")
    if payload == '{"_analysis_meta":[]}':
        assert value["report"] == {"_analysis_meta": []} and value["timings"] == {}
    else:
        assert value["status"] == "error" and value["report"] is None
    assert snapshot() == before


def test_original_research_dates_sources_and_timings_are_preserved():
    research = {"as_of": "2026-09-17T12:00:00+08:00", "sources": [{"url": "https://www.sse.com.cn/fixture"}]}
    report = {
        "final_decision": "历史观点，未代表最新事实",
        "_analysis_meta": {"research": research, "timings": {"total_seconds": 12}},
    }
    seed(payload=json.dumps(report))
    value = engine.get_analysis_record("saved", "owner")
    assert value["trade_date"] == "2026-09-17" and value["research"] == research
    assert value["elapsed"] == 12 and value["report"] == report
