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


class _Proc:
    def __init__(self, out):
        self.stdout = out.encode() if isinstance(out, str) else out


def test_extract_structured_ok(monkeypatch):
    payload = {"signal": "买入", "confidence": 82, "thesis": "龙头订单饱满",
               "metrics": {"valuation": "合理"}, "trade": {}, "analyst_digests": [],
               "bull_bear": {}, "verdict_summary": "加仓"}
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(json.dumps(payload)))
    out = ae._extract_structured({"final_decision": "x"}, "300750", "宁德时代")
    assert out and out["signal"] == "买入" and out["confidence"] == 82


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
