"""Tests for analysis_engine structured-extraction (dashboard data)."""

import json
import subprocess

from server import analysis_engine as ae


def test_parse_json_lenient_raw():
    assert ae._parse_json_lenient('{"a":1}') == {"a": 1}


def test_parse_json_lenient_wrapped_result():
    # claude -p --output-format json may wrap as {"result": "<json>"}
    assert ae._parse_json_lenient(json.dumps({"result": '{"signal":"买入"}'})) == {"signal": "买入"}


def test_parse_json_lenient_embedded():
    assert ae._parse_json_lenient('prefix {"signal":"买入"} suffix') == {"signal": "买入"}


def test_parse_json_lenient_garbage():
    assert ae._parse_json_lenient("not json") is None
    assert ae._parse_json_lenient("") is None


def test_get_last_message_extracts_nested_content_blocks():
    msg = {
        "content": [
            {"type": "text", "text": "第一段"},
            {"type": "text", "text": "第二段"},
        ]
    }
    assert ae._get_last_message(msg) == "第一段\n第二段"


def test_get_last_message_strips_object_artifacts_and_nested_dicts():
    msg = {
        "final_trade_decision": [
            "[object Object]",
            {"text": "最终结论：谨慎持有"},
        ]
    }
    assert ae._get_last_message(msg) == "最终结论：谨慎持有"


def test_extract_report_uses_readable_nested_decision():
    report = ae._extract_report(
        {
            "final_trade_decision": {"content": [{"text": "Sell：估值偏高"}]},
            "market_report": {"content": [{"text": "市场弱势"}]},
            "signal": "Sell",
        },
        "003000",
        "劲仔食品",
    )
    assert report["final_decision"] == "Sell：估值偏高"
    assert report["analyst_reports"]["market"] == "市场弱势"


def test_semantic_stream_updates_ignores_raw_messages_key():
    updates = ae._semantic_stream_updates({"messages": [{"content": "tool chatter"}]})
    assert updates == []


def test_semantic_stream_updates_maps_report_field_to_agent():
    updates = ae._semantic_stream_updates({"market_report": "市场量价改善"})
    assert len(updates) == 1
    assert updates[0]["node"] == "Market Analyst"
    assert updates[0]["label"] == "市场分析师"
    assert updates[0]["agent"] == "market"
    assert updates[0]["stage"] == "analysts"
    assert updates[0]["text"] == "市场量价改善"


def test_semantic_stream_updates_skips_unchanged_snapshot():
    previous = {"market_report": "市场量价改善"}
    updates = ae._semantic_stream_updates(
        {"messages": [{"content": "still running"}], "market_report": "市场量价改善"},
        previous,
    )
    assert updates == []


def test_astock_values_snapshot_keeps_nested_debate_state():
    initial = {
        "messages": [],
        "investment_debate_state": {"bull_history": "", "bear_history": ""},
        "risk_debate_state": {"risky_history": ""},
    }
    snapshot = {
        "messages": [{"content": "done"}],
        "market_report": "market ready",
        "investment_debate_state": {"bull_history": "bull case", "bear_history": "bear case"},
        "risk_debate_state": {"risky_history": "risk case"},
    }

    state = ae._merge_astock_stream_state(initial, snapshot, initial)

    assert state["investment_debate_state"]["bull_history"] == "bull case"
    assert state["risk_debate_state"]["risky_history"] == "risk case"
    assert "bull_history" not in state


def test_astock_legacy_node_delta_is_still_supported():
    initial = {"messages": [], "investment_debate_state": {"bull_history": ""}}
    previous = dict(initial)

    state = ae._merge_astock_stream_state(
        previous,
        {"market_analyst": {"market_report": "market ready"}},
        initial,
    )

    assert state["market_report"] == "market ready"
    assert state["investment_debate_state"] == {"bull_history": ""}


class _Proc:
    def __init__(self, out):
        self.stdout = out.encode() if isinstance(out, str) else out
        self.returncode = 0


def test_extract_structured_ok(monkeypatch):
    payload = {
        "signal": "买入",
        "confidence": 82,
        "thesis": "龙头订单饱满",
        "metrics": {"valuation": "合理"},
        "trade": {},
        "analyst_digests": [],
        "bull_bear": {},
        "verdict_summary": "加仓",
    }
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(json.dumps(payload)))
    out = ae._extract_structured({"final_decision": "x"}, "300750", "宁德时代")
    assert out and out["signal"] == "买入"
    assert out["confidence"] is None


def test_extract_structured_timeout_returns_none(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)

    monkeypatch.setattr(subprocess, "run", boom)
    assert ae._extract_structured({"final_decision": "x"}, "300750", "n") is None


def test_extract_structured_garbage_returns_none(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc("totally not json"))
    assert ae._extract_structured({"final_decision": "x"}, "300750", "n") is None


def test_extract_structured_missing_signal_and_thesis(monkeypatch):
    # Valid JSON but no signal/thesis → treated as garbage → None (fallback)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc('{"foo":"bar"}'))
    assert ae._extract_structured({}, "300750", "n") is None
