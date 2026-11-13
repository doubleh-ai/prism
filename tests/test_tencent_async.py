"""Synthetic public transport; worker ownership, bounds and market semantics."""

import asyncio
import json
import threading
from pathlib import Path

import httpx
import pandas as pd
import pytest

from server import public_network
from server.market import tencent_provider
from server.market.provider import ProviderUnavailable


@pytest.fixture
def network(monkeypatch):
    state = {"requests": [], "handler": None}

    def handle(request):
        state["requests"].append(request)
        return state["handler"](request)

    class Adapter:
        def sync_transport(self):
            return httpx.MockTransport(handle)

        def async_transport(self):
            return httpx.MockTransport(handle)

        def connect_tls(self, *args):
            raise AssertionError("market HTTP must use its installed transport")

    monkeypatch.setattr(public_network, "_network", None)
    public_network.install(Adapter())
    return state


QUOTE = 'v_sz002050="51~三花智控~002050~45.74~45.50~45.80~1000000~500000~45.74~45.50~45.74"'
BAR = {"day": "2024-01-01", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1000.0}


@pytest.mark.parametrize("kind", ["quote", "kline"])
async def test_public_reads_are_owned_workers_and_leave_event_loop_free(network, kind):
    started, release = threading.Event(), threading.Event()
    caller = threading.get_ident()

    def handle(request):
        assert threading.get_ident() != caller
        started.set()
        assert release.wait(2), "event loop did not release the owned HTTP worker"
        return httpx.Response(
            200, content=QUOTE.encode("gbk") if kind == "quote" else json.dumps([BAR]).encode()
        )

    network["handler"] = handle
    provider = tencent_provider.TencentProvider()
    task = asyncio.create_task(
        provider.get_quote("002050") if kind == "quote" else provider.get_kline("600519", "1d", 10)
    )

    async def wait_started():
        while not started.is_set():
            await asyncio.sleep(0.001)

    try:
        await asyncio.wait_for(wait_started(), 1)
        assert not task.done()
    finally:
        release.set()
        result = await task
    if kind == "quote":
        assert (result.ticker, result.name, result.price, result.source) == (
            "002050",
            "三花智控",
            45.74,
            "tencent",
        )
    else:
        assert len(result) == 1 and result[0].close == 1.5
    assert len(network["requests"]) == 1


async def test_weekly_bars_keep_sina_provenance_and_fetch_enough_daily_history(network):
    payload = [
        {"day": "2024-01-29", "open": 10, "high": 12, "low": 9, "close": 11, "volume": 100},
        {"day": "2024-02-02", "open": 11, "high": 14, "low": 10, "close": 13, "volume": 200},
        {"day": "2024-02-05", "open": 13, "high": 15, "low": 12, "close": 14, "volume": 300},
    ]
    network["handler"] = lambda request: httpx.Response(200, json=payload)
    bars = await tencent_provider.TencentProvider().get_kline("600519", "1w", 2)
    assert "datalen=60" in str(network["requests"][0].url)
    assert len(bars) == 2
    assert (bars[0].open, bars[0].high, bars[0].low, bars[0].close, bars[0].volume) == (10, 14, 9, 13, 300)
    assert bars[0].provenance.source == "sina"


@pytest.mark.parametrize("status", [301, 302, 307, 401, 429, 500])
async def test_market_failure_does_not_redirect_or_retry(network, status):
    network["handler"] = lambda request: httpx.Response(
        status, headers={"Location": "https://other.invalid/"}
    )
    with pytest.raises(ProviderUnavailable):
        await tencent_provider.TencentProvider().get_quote("002050")
    assert len(network["requests"]) == 1


async def test_market_response_is_bounded(network, monkeypatch):
    monkeypatch.setattr(tencent_provider, "MAX_BODY_BYTES", 32)
    network["handler"] = lambda request: httpx.Response(200, content=b"x" * 33)
    with pytest.raises(ProviderUnavailable, match="response too large"):
        await tencent_provider.TencentProvider().get_kline("600519")
    assert len(network["requests"]) == 1


# Public closing quotes captured in PR #34; compare real provider conversion paths.
UNIT_SAMPLES = json.loads((Path(__file__).parent / "fixtures" / "quote_units_20261113.json").read_text())[
    "samples"
]


@pytest.mark.parametrize("sample", UNIT_SAMPLES, ids=lambda row: row["symbol"])
@pytest.mark.parametrize("field", ["volume", "turnover"])
async def test_quote_units_match_akshare_within_one_percent(network, monkeypatch, sample, field):
    from server.market import akshare_provider

    fields = [""] * 38
    for index, value in sample["tencent_fields"].items():
        fields[int(index)] = value
    payload = f'v_{sample["symbol"]}="' + "~".join(fields) + '";'
    network["handler"] = lambda request: httpx.Response(200, content=payload.encode("gbk"))

    def daily(*, symbol, adjust):
        assert symbol == sample["symbol"]
        assert adjust == "qfq"
        return pd.DataFrame([sample["akshare_row"]])

    monkeypatch.setattr(akshare_provider, "_AKSHARE_AVAILABLE", True)
    monkeypatch.setattr(akshare_provider.ak, "stock_zh_a_daily", daily)
    ticker = sample["symbol"][2:]
    tencent = await tencent_provider.TencentProvider().get_quote(ticker)
    akshare = await akshare_provider.AkshareProvider().get_quote(ticker)
    assert network["requests"][0].url.path == "/q=" + sample["symbol"]
    actual, reference = getattr(tencent, field), getattr(akshare, field)
    raw_field = "volume" if field == "volume" else "amount"
    assert reference == sample["akshare_row"][raw_field]
    assert abs(actual - reference) / reference < 0.01


@pytest.mark.parametrize("raw", [None, "", "0"])
async def test_quote_unit_conversion_keeps_missing_and_zero_values(network, raw):
    fields = ["51", "三花智控", "002050", "45.74", "45.50", "45.80"]
    if raw is not None:
        fields += [""] * (38 - len(fields))
        fields[6] = fields[37] = raw
    payload = 'v_sz002050="' + "~".join(fields) + '";'
    network["handler"] = lambda request: httpx.Response(200, content=payload.encode("gbk"))
    quote = await tencent_provider.TencentProvider().get_quote("002050")
    assert quote.volume == 0
    assert quote.turnover == 0
