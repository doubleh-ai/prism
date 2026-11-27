"""Public K-line samples: preserve CNY amounts and fail over when they are absent."""

import json
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

import httpx
import pandas as pd
import pytest

from server import public_network
from server.indicators import router as indicators
from server.indicators import scanner
from server.market import akshare_provider
from server.market import router as market
from server.market.cache import MarketCache
from server.market.chain import ProviderChain
from server.market.tencent_provider import TencentProvider

SAMPLES = json.loads((Path(__file__).parent / "fixtures" / "kline_amount_20261113.json").read_text())[
    "samples"
]


@pytest.mark.parametrize("amount", [None, float("nan"), float("inf"), -1, "unavailable", 0])
async def test_invalid_amount_preserves_the_bar_and_latest_trade_date(sources, amount):
    sources["sample"] = deepcopy(SAMPLES[0])
    sources["sample"]["akshare"]["rows"][-1]["amount"] = amount
    bars = await akshare_provider.AkshareProvider().get_kline("600519", limit=10)
    dates = [pd.Timestamp(bar.timestamp, unit="s", tz="Asia/Shanghai").date().isoformat() for bar in bars]
    assert dates == [pd.Timestamp(row["date"]).date().isoformat() for row in SAMPLES[0]["akshare"]["rows"]]
    assert bars[-1].close == SAMPLES[0]["akshare"]["rows"][-1]["close"]
    assert bars[-1].turnover == (0 if amount == 0 else None)


@pytest.mark.parametrize("period,missing_index", [("1d", -1), ("1w", -2), ("1m", -2)])
async def test_nan_amount_cannot_become_previous_day_or_partial_period(sources, period, missing_index):
    from server.market.provider import NoProviderAvailable

    sources["sample"] = deepcopy(SAMPLES[0])
    sources["sample"]["akshare"]["rows"][missing_index]["amount"] = float("nan")
    with pytest.raises(NoProviderAvailable):
        await sources["chain"].get_kline("600519", period=period, limit=10, require_amount=True)


@pytest.mark.parametrize("ttl", [60, -1])
async def test_old_akshare_cache_with_dropped_bar_is_not_reused(sources, ttl):
    from server.market.provider import NoProviderAvailable

    bars = await akshare_provider.AkshareProvider().get_kline("600519", limit=10)
    sources["chain"].cache.set(
        "kline-v3:akshare:600519:1d:10:none", [bar.model_dump() for bar in bars[:-1]], ttl=ttl
    )
    sources["fail"] = True
    with pytest.raises(NoProviderAvailable):
        await sources["chain"].get_kline("600519", limit=10, require_amount=True)


@pytest.mark.parametrize("dsl", [{"amount": 0}, {"ref": [{"amount": 0}, 1]}, {"price": "amount"}])
async def test_amount_formula_without_threshold_falls_back_in_background_scan(sources, dsl):
    indicator = {"id": "amount-formula", "name": "amount", "kind": "plot", "dsl": dsl}
    job = scanner.create_job(
        "owner", indicator, {"scope": "watchlist", "tickers": ["600519"], "lookback": 10}
    )
    await scanner._run_job(job, indicator, indicators._build_df)
    result = scanner.get_job(job["id"], "owner")
    assert result["matched"] == 1
    assert result["failed"] == result["insufficient"] == 0
    assert sources["ak_calls"] == 1


@pytest.mark.parametrize("endpoint", ["scan", "eval"])
async def test_amount_formula_without_threshold_falls_back_in_http_paths(
    sources, client, auth_cookies, endpoint
):
    from server.indicators.models import IndicatorCreate

    amount = SAMPLES[0]["akshare"]["rows"][-2]["amount"]
    ind = indicators.create_owned_indicator(
        "testuser",
        IndicatorCreate(
            name="previous amount", kind="signal", dsl={"gt": [{"ref": [{"amount": 0}, 1]}, amount * 0.99]}
        ),
    )
    response = await client.post(
        f"/api/indicators/{ind['id']}/{endpoint}",
        cookies=auth_cookies,
        json={"tickers": ["600519"]} if endpoint == "scan" else {"ticker": "600519"},
    )
    assert response.status_code == 200
    assert response.json()["matches"] if endpoint == "scan" else response.json()["latest"]
    assert sources["ak_calls"] == 1


@pytest.mark.parametrize("ttl", [60, -1])
async def test_amount_formula_rejects_missing_historical_amount_in_cache(sources, ttl):
    from server.market.provider import NoProviderAvailable

    chain = sources["chain"]
    bars = await akshare_provider.AkshareProvider().get_kline("600519", limit=10)
    bars[-2].turnover = None
    chain.cache.set(
        chain._cache_key("kline-v3", "akshare", "600519", "1d", "10", "none"),
        [bar.model_dump() for bar in bars],
        ttl=ttl,
    )
    sources["fail"] = True
    latest, _ = await chain.get_kline("600519", limit=10, require_amount=True)
    assert latest[-1].turnover is not None
    with pytest.raises(NoProviderAvailable):
        await chain.get_kline("600519", limit=10, require_amount="all")


async def test_amount_eval_keeps_explicit_client_bars_authoritative(sources, client, auth_cookies):
    from server.indicators.models import IndicatorCreate

    ind = indicators.create_owned_indicator(
        "testuser", IndicatorCreate(name="amount", kind="plot", dsl={"amount": 0})
    )
    response = await client.post(
        f"/api/indicators/{ind['id']}/eval",
        cookies=auth_cookies,
        json={
            "ticker": "600519",
            "kline": [{"t": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
        },
    )
    assert response.status_code == 422
    assert sources["sina_calls"] == sources["ak_calls"] == 0


@pytest.mark.parametrize("cached", [False, True])
async def test_history_amount_requirement_falls_back_past_partial_provider(sources, cached):
    complete = await akshare_provider.AkshareProvider().get_kline("600519", limit=10)
    partial = [bar.model_copy() for bar in complete]
    partial[-2].turnover = None

    class Partial:
        name = "partial"

        async def get_kline(self, *args, **kwargs):
            return partial

    chain = ProviderChain([Partial(), akshare_provider.AkshareProvider()], MarketCache())
    if cached:
        chain.cache.set(
            chain._cache_key("kline-v3", "partial", "600519", "1d", "10", "none"),
            [bar.model_dump() for bar in partial],
            ttl=60,
        )
    bars, _ = await chain.get_kline("600519", limit=10, require_amount="all")
    assert [bar.turnover for bar in bars] == [bar.turnover for bar in complete]


async def test_latest_amount_request_does_not_coalesce_with_history_requirement(sources):
    import asyncio

    partial = await akshare_provider.AkshareProvider().get_kline("600519", limit=10)
    partial[-2].turnover = None
    entered, release = asyncio.Event(), asyncio.Event()

    class Partial:
        name = "partial"

        async def get_kline(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return partial

    chain = ProviderChain([Partial(), akshare_provider.AkshareProvider()], MarketCache())
    latest = asyncio.create_task(chain.get_kline("600519", limit=10, require_amount=True))
    await asyncio.wait_for(entered.wait(), 1)
    history = asyncio.create_task(chain.get_kline("600519", limit=10, require_amount="all"))
    await asyncio.sleep(0)
    release.set()
    latest_result, history_result = await asyncio.wait_for(asyncio.gather(latest, history), 3)
    assert latest_result[0][-2].turnover is None
    assert history_result[0][-2].turnover == SAMPLES[0]["akshare"]["rows"][-2]["amount"]


@pytest.fixture
def sources(monkeypatch):
    state = {"sample": SAMPLES[0], "sina_calls": 0, "ak_calls": 0, "fail": False}

    def handle(request):
        state["sina_calls"] += 1
        assert request.url.params["symbol"] == state["sample"]["symbol"]
        return httpx.Response(503 if state["fail"] else 200, json=state["sample"]["sina"]["rows"])

    @contextmanager
    def client(**kwargs):
        with httpx.Client(transport=httpx.MockTransport(handle), trust_env=False) as connection:
            yield connection

    def daily(*, symbol, adjust):
        state["ak_calls"] += 1
        assert symbol == state["sample"]["symbol"]
        assert adjust == ""
        if state["fail"]:
            raise RuntimeError("synthetic source unavailable")
        frame = pd.DataFrame(state["sample"]["akshare"]["rows"])
        frame["date"] = pd.to_datetime(frame["date"]).dt.date
        return frame

    monkeypatch.setattr(public_network, "sync_client", client)
    monkeypatch.setattr(akshare_provider, "_AKSHARE_AVAILABLE", True)
    monkeypatch.setattr(akshare_provider.ak, "stock_zh_a_daily", daily)
    chain = ProviderChain([TencentProvider(), akshare_provider.AkshareProvider()], MarketCache())
    monkeypatch.setattr(market, "get_chain", lambda: chain)
    state["chain"] = chain
    return state


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda row: row["symbol"])
async def test_recorded_kline_units_and_missing_amount(sources, sample):
    sources["sample"] = sample
    ticker = sample["symbol"][2:]
    direct = await TencentProvider().get_kline(ticker, limit=10)
    via_ak = await akshare_provider.AkshareProvider().get_kline(ticker, limit=10)
    assert len(direct) == len(via_ak) == 10
    for sina_bar, ak_bar, raw in zip(direct, via_ak, sample["akshare"]["rows"], strict=True):
        assert sina_bar.timestamp == ak_bar.timestamp
        assert sina_bar.volume == ak_bar.volume == raw["volume"]
        assert sina_bar.turnover is None
        assert ak_bar.turnover == raw["amount"]


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda row: row["symbol"])
@pytest.mark.parametrize("factor", [0.99, 1.01])
async def test_min_turnover_scans_use_real_amount_in_yuan(sources, sample, factor):
    sources["sample"] = sample
    ticker = sample["symbol"][2:]
    amount = sample["akshare"]["rows"][-1]["amount"]
    indicator = {"id": "amount", "name": "close", "kind": "plot", "dsl": {"price": "close"}}
    job = scanner.create_job(
        "owner",
        indicator,
        {
            "scope": "watchlist",
            "tickers": [ticker],
            "min_turnover": amount * factor,
            "lookback": 10,
        },
    )
    await scanner._run_job(job, indicator, indicators._build_df)
    result = scanner.get_job(job["id"], "owner")
    assert result["status"] == "completed"
    assert result["failed"] == result["insufficient"] == 0
    assert result["excluded"] == int(factor > 1)
    assert result["matched"] == int(factor < 1)
    if factor < 1:
        assert result["matches"][0]["turnover"] == amount
    assert sources["sina_calls"] == sources["ak_calls"] == 1


@pytest.mark.parametrize("factor", [0.99, 1.01])
async def test_synchronous_scan_applies_the_same_yuan_threshold(sources, client, auth_cookies, factor):
    from server.indicators.models import IndicatorCreate

    amount = SAMPLES[0]["akshare"]["rows"][-1]["amount"]
    ind = indicators.create_owned_indicator(
        "testuser",
        IndicatorCreate(
            name="positive price",
            kind="signal",
            dsl={"gt": [{"price": "close"}, 0]},
        ),
    )
    response = await client.post(
        f"/api/indicators/{ind['id']}/scan",
        cookies=auth_cookies,
        json={
            "tickers": ["600519"],
            "min_turnover": amount * factor,
        },
    )
    assert response.status_code == 200
    matches = response.json()["matches"]
    assert len(matches) == int(factor < 1)
    if matches:
        assert matches[0]["turnover"] == amount
    assert sources["ak_calls"] == 1


@pytest.mark.parametrize("ttl", [60, -1])
async def test_incomplete_chart_cache_cannot_satisfy_turnover_filter(sources, ttl):
    from server.market.provider import NoProviderAvailable

    chain = sources["chain"]
    bars, _ = await chain.get_kline("600519", limit=10)
    assert all(bar.turnover is None for bar in bars)
    assert sources["ak_calls"] == 0  # Ordinary charts still use the first provider.
    key = chain._cache_key("kline-v3", "tencent", "600519", "1d", "10", "none")
    chain.cache.set(key, [bar.model_dump() for bar in bars], ttl=ttl)
    sources["fail"] = True
    with pytest.raises(NoProviderAvailable):
        await chain.get_kline("600519", limit=10, require_amount=True)
    sources["fail"] = False
    frame, _ = await indicators._build_df("600519", limit=10, include_fundamentals=False, require_amount=True)
    assert frame["amount"].iloc[-1] == SAMPLES[0]["akshare"]["rows"][-1]["amount"]
    amount_bars, stale = await chain.get_kline("600519", limit=10, require_amount=True)
    assert stale is False
    key = chain._cache_key("kline-v3", "akshare", "600519", "1d", "10", "none")
    chain.cache.set(key, [bar.model_dump() for bar in amount_bars], ttl=-1)
    sources["fail"] = True
    fallback, stale = await chain.get_kline("600519", limit=10, require_amount=True)
    assert stale is True and fallback[-1].turnover == frame["amount"].iloc[-1]


@pytest.mark.parametrize("period,days", [("1w", 5), ("1m", 10)])
async def test_period_amount_is_sum_of_actual_daily_yuan(sources, period, days):
    bars, _ = await sources["chain"].get_kline("600519", period=period, limit=10, require_amount=True)
    raw = SAMPLES[0]["akshare"]["rows"][-days:]
    assert bars[-1].turnover == sum(row["amount"] for row in raw)
    assert bars[-1].volume == sum(row["volume"] for row in raw)


@pytest.mark.parametrize("amount", [0, None, float("inf")])
async def test_required_amount_distinguishes_zero_from_unavailable(sources, amount):
    from server.market.models import KLine
    from server.market.provider import NoProviderAvailable

    chain = ProviderChain([], MarketCache())

    class Provider:
        name = "synthetic"

        async def get_kline(self, *args, **kwargs):
            return [
                KLine(
                    timestamp=1, open=1, high=1, low=1, close=1, volume=0, turnover=amount, adjustment="none"
                )
            ]

    chain.providers = [Provider()]
    if amount == 0:
        result, _ = await chain.get_kline("600519", require_amount=True)
        assert result[0].turnover == 0
    else:
        with pytest.raises(NoProviderAvailable):
            await chain.get_kline("600519", require_amount=True)


async def test_amount_and_chart_requests_do_not_coalesce(sources):
    import asyncio

    entered, release = asyncio.Event(), asyncio.Event()

    class Direct(TencentProvider):
        async def get_kline(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return await super().get_kline(*args, **kwargs)

    chain = ProviderChain([Direct(), akshare_provider.AkshareProvider()], MarketCache())
    chart = asyncio.create_task(chain.get_kline("600519", limit=10))
    await asyncio.wait_for(entered.wait(), 1)
    filtered = asyncio.create_task(chain.get_kline("600519", limit=10, require_amount=True))
    await asyncio.sleep(0)
    release.set()
    ordinary, required = await asyncio.wait_for(asyncio.gather(chart, filtered), 3)
    assert ordinary[0][-1].turnover is None
    assert required[0][-1].turnover == SAMPLES[0]["akshare"]["rows"][-1]["amount"]


async def test_partial_period_amount_is_not_accepted(sources):
    from server.market.models import KLine
    from server.market.provider import NoProviderAvailable

    class Daily:
        name = "synthetic"

        async def get_kline(self, *args, **kwargs):
            return [
                KLine(
                    timestamp=1794232800 + day * 86400,
                    open=1,
                    high=1,
                    low=1,
                    close=1,
                    volume=1,
                    turnover=value,
                    adjustment="none",
                )
                for day, value in enumerate([None, 10])
            ]

    chain = ProviderChain([Daily()], MarketCache())
    with pytest.raises(NoProviderAvailable):
        await chain.get_kline("600519", period="1w", require_amount=True)


async def test_all_amount_sources_unavailable_does_not_report_successful_scan(sources):
    sources["chain"].providers = [TencentProvider()]
    indicator = {"id": "missing", "name": "close", "kind": "plot", "dsl": {"price": "close"}}
    job = scanner.create_job(
        "owner",
        indicator,
        {
            "scope": "watchlist",
            "tickers": ["600519"],
            "min_turnover": 1,
            "lookback": 10,
        },
    )
    await scanner._run_job(job, indicator, indicators._build_df)
    result = scanner.get_job(job["id"], "owner")
    assert result["status"] == "failed"
    assert result["matched"] == result["evaluated"] == result["excluded"] == 0
    assert result["failed"] + result["insufficient"] == 1
