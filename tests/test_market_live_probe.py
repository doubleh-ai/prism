"""Synthetic/offline market probe regressions; these never certify live I/O."""

import importlib
import types
import urllib.request

import pytest

from scripts import market_live_probe as probe


def pins():
    return {"qt.gtimg.cn": ["1.1.1.1"], "money.finance.sina.com.cn": ["8.8.4.4"]}


@pytest.mark.parametrize(
    "value",
    ["127.0.0.1", "10.0.0.1", "169.254.169.254", "224.0.0.1", "::1", "::ffff:1.1.1.1", "64:ff9b::101:101"],
)
def test_nonpublic_and_translated_peers_rejected(value):
    with pytest.raises(probe.ProbeError):
        probe.public_addresses("qt.gtimg.cn", ["1.1.1.1", value])


def test_hosts_and_routes_are_fixed():
    with pytest.raises(probe.ProbeError):
        probe.validate_pins({**pins(), "example.com": ["1.1.1.1"]})
    transport = probe.FixedTransport(pins(), connector=lambda *args: pytest.fail("no network"))
    with pytest.raises(probe.ProbeError, match="fixed_request_budget_exhausted"):
        transport.urlopen(urllib.request.Request("https://example.com/"))
    assert transport.counts == {"quote": 0, "kline": 0}


def test_failed_upstream_consumes_only_slot_no_retry():
    calls = []

    def failed(*args):
        calls.append(args)
        raise OSError("public synthetic failure")

    transport = probe.FixedTransport(pins(), connector=failed)
    request = urllib.request.Request(probe.ROUTES["quote"], headers={"User-Agent": "Mozilla/5.0"})
    with pytest.raises(OSError):
        transport.urlopen(request)
    with pytest.raises(probe.ProbeError, match="fixed_request_budget_exhausted"):
        transport.urlopen(request)
    assert calls == [("qt.gtimg.cn", "1.1.1.1", 12)]
    assert transport.counts["quote"] == 1


@pytest.mark.parametrize(
    "status,encoding", [(301, "identity"), (302, "identity"), (200, "gzip"), (500, "identity")]
)
def test_redirect_and_encoded_or_failed_responses_rejected(monkeypatch, status, encoding):
    sent = []

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, target, headers):
            sent.append((method, target, headers))

        def getresponse(self):
            return types.SimpleNamespace(status=status, getheader=lambda *args: encoding)

        def close(self):
            pass

    monkeypatch.setattr(probe.http.client, "HTTPSConnection", Connection)
    transport = probe.FixedTransport(pins(), connector=lambda *args: object())
    with pytest.raises(probe.ProbeError, match="upstream_response_rejected"):
        transport.urlopen(urllib.request.Request(probe.ROUTES["quote"]))
    assert len(sent) == 1 and sent[0][0:2] == ("GET", "/q=sh600519")


def quote(**updates):
    return {
        "ticker": "600519",
        "source": "tencent",
        "price": 100,
        "stale": False,
        "timestamp_kind": "trade",
        "data_kind": "quote",
        "retrieved_at": 200,
        "data_timestamp": 100,
        "timestamp": 100,
        **updates,
    }


def test_trade_time_remains_distinct_from_retrieval():
    assert probe.quote_valid(quote(), 199, 201)
    assert not probe.quote_valid(quote(data_timestamp=None, timestamp_kind="retrieved"), 199, 201)
    assert not probe.quote_valid(quote(timestamp=200), 199, 201)
    assert not probe.quote_valid(quote(retrieved_at=100), 199, 201)


def bars():
    return [
        {"timestamp": day, "open": 10, "high": 12, "low": 9, "close": 11, "volume": 100} for day in (100, 200)
    ]


@pytest.mark.parametrize(
    "case", ["empty", "too_many", "descending", "duplicate", "ohlc", "nan", "negative", "future"]
)
def test_invalid_kline_shape_order_and_numbers_rejected(case):
    value = bars()
    if case == "empty":
        value = []
    elif case == "too_many":
        value = value * 6
    elif case == "descending":
        value.reverse()
    elif case == "duplicate":
        value[1]["timestamp"] = 100
    elif case == "ohlc":
        value[0]["low"] = 11
    elif case == "nan":
        value[0]["close"] = float("nan")
    elif case == "negative":
        value[0]["volume"] = -1
    elif case == "future":
        value[1]["timestamp"] = 999
    assert not probe.bars_valid(value, 201)
    assert probe.bars_valid(bars(), 201)


def test_profile_network_allowlist_and_filesystem_guards(tmp_path):
    value = probe.mac_profile(tmp_path, pins())
    assert "(deny network*)" in value
    assert '(remote ip "1.1.1.1:443")' in value and '(remote ip "8.8.4.4:443")' in value
    assert "localhost:*" not in value and 'remote tcp "*:*"' not in value
    assert "(deny process-fork)" in value and "/Backups" in value
    assert '(regex #"(^|/)[.]env($|[.])")' in value


def test_failed_os_controls_never_import_candidate_or_start_network(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "checked_work", lambda work: tmp_path)

    def rejected(*args):
        raise probe.ProbeError("actual_os_denial_controls_failed")

    monkeypatch.setattr(probe, "bootstrap", rejected)
    monkeypatch.setattr(
        probe, "candidate_modules", lambda *args: pytest.fail("candidate before actual kernel proof")
    )
    monkeypatch.setattr(probe, "write_private", lambda *args: None)
    result = probe.worker(tmp_path)
    assert result["passed"] is False and result["error_class"] == "market_probe_failed"
    assert result["error_detail"] == "actual_os_denial_controls_failed"


@pytest.mark.parametrize(
    "error,expected",
    [
        (probe.ProbeError("fixed_public_peer_required"), "fixed_public_peer_required"),
        (probe.ProbeError("SYNTHETIC_PRIVATE_TEXT"), "unexpected_error"),
        (OSError("SYNTHETIC_PRIVATE_TEXT"), "operating_system_error"),
        (ValueError("SYNTHETIC_PRIVATE_TEXT"), "unexpected_error"),
    ],
)
def test_error_diagnostics_only_fixed_categories(error, expected):
    assert probe.safe_error(error) == expected


async def test_retired_cli_parent_probe_is_rejected_before_external_io(monkeypatch):
    # This old probe remains byte-for-byte frozen for historical gate regressions.
    # It cannot qualify a Native candidate; the real Native HTTP/capability market
    # coverage lives in test_market_kline_provenance.py instead.
    from server.work_lifecycle import LifecycleError

    modules = {
        name: importlib.import_module(name)
        for name in (
            "server.main",
            "server.auth",
            "server.database",
            "server.capabilities",
            "server.research",
            "server.research_context",
            "server.market.factory",
            "server.market.router",
            "httpx",
        )
    }
    factory = modules["server.market.factory"]
    previous_chain = factory._chain
    transport = probe.FixedTransport(pins(), connector=lambda *args: pytest.fail("no external I/O"))
    try:
        with pytest.raises(LifecycleError, match="work_kind_rejected"):
            await probe.exercise(modules, transport)
    finally:
        factory._chain = previous_chain
    assert transport.counts == {"quote": 0, "kline": 0}
