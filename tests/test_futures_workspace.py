"""Futures workspace journeys through authenticated routes without external HTTP."""

import json
from datetime import datetime, timedelta

import httpx
import pytest

from server import futures, futures_spot_history, futures_workspace
from server.futures_fundamentals import _HTML


@pytest.fixture(autouse=True)
def isolated_cards(monkeypatch):
    now = datetime.fromisoformat("2026-11-25T18:00:00+08:00")
    monkeypatch.setattr(futures, "_SERIES_HISTORY", {})
    monkeypatch.setattr(futures_workspace, "_cards", {})
    monkeypatch.setattr(futures_workspace, "_overview", {})
    monkeypatch.setattr(futures_workspace, "_overview_fundamentals", None)
    monkeypatch.setattr(futures_workspace, "_benchmarks", {})
    monkeypatch.setattr(futures_workspace, "_now", lambda: now)


async def test_authenticated_universe_is_exact_and_does_not_query_providers(
    client, auth_cookies, monkeypatch
):
    async def unexpected(*args):
        pytest.fail("universe must never contact a provider")

    monkeypatch.setattr(futures, "snapshot", unexpected)
    monkeypatch.setattr(futures_spot_history, "history", unexpected)
    for path in ("/api/futures/universe", "/api/futures/JM/card"):
        assert (await client.get(path)).status_code == 401
    response = await client.get("/api/futures/universe", cookies=auth_cookies)
    assert response.status_code == 200
    body = response.json()
    products = body["products"]
    expected = set(
        "AU AG CU AL NI SN AO LC SI PS JM I RB HC SS C CS CF SR SP LH AP CJ JD SA FG SH "
        "MA TA RU NR PP V UR SC PG BU A B M RM P Y OI PK EC".split()
    )
    assert body["count"] == len(products) == len(expected) == 46
    assert {p["symbol"] for p in products} == expected
    assert all(p["exchange"] == futures.PRODUCTS[p["symbol"]][1] for p in products)
    for product in products:
        dims = product["dimensions"]
        assert set(dims) == {"market", "technicals", "positioning", "fundamentals", "freshness"}
        assert dims["market"]["status"] == dims["freshness"]["status"] == "unknown"
        assert dims["technicals"]["status"] == dims["positioning"]["status"] == "unknown"
        assert all(state["reason_code"] for state in dims.values())

    for unsupported in ("IF", "CU2701"):
        response = await client.get(f"/api/futures/{unsupported}/card", cookies=auth_cookies)
        assert response.status_code == 404


async def test_card_keeps_source_and_date_per_observation_and_preserves_zero(
    client, auth_cookies, monkeypatch
):
    source = {"name": "隔离行情", "url": "https://example.test/quotes"}
    last = {"date": "2026-11-19", "close": 1511, "volume": 0, "open_interest": None}
    calls = []

    async def read_market(symbol, *, workspace):
        assert workspace
        calls.append(symbol)
        return {
            "series_status": "collected",
            "latest": last,
            "bars": [last],
            "data_as_of": "2026-11-24",
            "source": source,
            "contract_dates": {
                "status": "collected",
                "data_as_of": "2026-11-22",
                "source_url": "https://example.test/dates",
                "contracts": [{"contract": "cu2701", "listed_expiry_date": "2027-01-15"}],
            },
            "technical": {"sma60": 1200},
            "workspace_technicals": {"atr14": 0.0, "ema20": None},
            "workspace_positioning": {"open_interest": None, "volume": 0},
        }

    async def read_inventory(symbol):
        return {
            "status": "collected",
            "source": source,
            "quantity": 0,
            "change": 0,
            "unit": "吨",
            "data_as_of": "2026-11-24",
            "basis": "仓库仓单",
            "segment": "铜",
        }

    async def fail_spread(symbol, trading_day):
        raise TimeoutError

    async def no_basis(symbol):
        return {"status": "unavailable", "error_code": "report_not_found", "reason": "本日未披露。"}

    async def history(symbol, start, end):
        assert (symbol, start, end) == ("CU", "2026-11-18", "2026-11-24")
        return {
            "status": "collected",
            "source": source,
            "first_date": "2026-11-23",
            "last_date": "2026-11-23",
            "sample_count": 1,
            "unit": "元/吨",
            "observations": [{"date": "2026-11-23", "price": 1510}],
        }

    monkeypatch.setattr(futures, "snapshot", read_market)
    monkeypatch.setattr(futures_workspace.futures_panel.futures_inventory, "snapshot", read_inventory)
    monkeypatch.setattr(futures_workspace.futures_panel.futures_spread, "spread", fail_spread)
    monkeypatch.setattr(futures_workspace.futures_panel.futures_basis, "snapshot", no_basis)
    monkeypatch.setattr(futures_spot_history, "history", history)
    response = await client.get("/api/futures/cu/card", cookies=auth_cookies)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["symbol"] == "CU"
    assert body["market"]["data_as_of"] == "2026-11-19"
    assert body["market"]["values"]["volume"] == 0
    assert body["market"]["values"]["open_interest"] is None
    assert body["market"]["values"]["series_type"] == "continuous"
    assert body["market"]["values"]["bars"][0]["close"] == 1511
    assert "sma60" not in response.text
    assert body["technicals"]["status"] == body["positioning"]["status"] == "partial"
    assert body["technicals"]["values"] == {"atr14": 0.0, "ema20": None}
    assert body["positioning"]["values"]["volume"] == 0
    assert body["inventory"]["values"]["change"] == body["inventory"]["values"]["quantity"] == 0
    assert body["inventory"]["data_as_of"] == "2026-11-24"
    assert body["spread"]["values"] is None and body["spread"]["reason_code"] == "spread_unavailable"
    assert body["basis"]["reason_code"] == "basis_unavailable:report_not_found"
    assert body["spot_history"]["data_as_of"] == "2026-11-23"
    assert body["contract_dates"]["source"]["url"] == "https://example.test/dates"
    assert body["dimensions"]["freshness"]["status"] == "stale"
    assert body["dimensions"]["fundamentals"]["status"] == "partial"

    grid = (await client.get("/api/futures/universe", cookies=auth_cookies)).json()
    observed = next(p for p in grid["products"] if p["symbol"] == "CU")
    assert observed["dimensions"] == body["dimensions"]
    assert (await client.get("/api/futures/CU/card", cookies=auth_cookies)).json() == body
    assert calls == ["CU"]


def test_unavailable_market_does_not_borrow_date_from_another_source():
    item = futures_workspace._market(
        {"series_status": "unavailable", "data_as_of": "2026-11-24"},
        "JM",
        futures_workspace._now().date(),
    )
    assert item["status"] == "unavailable" and item["data_as_of"] is None and item["values"] is None


async def test_overview_before_any_detail_reuses_the_daily_series_and_one_basis_table(monkeypatch):
    now = futures_workspace._now().date()
    samples = []
    for index in range(260):
        day = (now - timedelta(days=259 - index)).isoformat()
        close = 100 + index
        samples.append([day, close, close + 1, close - 1, close, 310, 100 + 2 * index, close])
    calls = []

    async def respond(request):
        calls.append(request.url.params["symbol"])
        return httpx.Response(200, content=json.dumps(samples))

    def client(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(respond), **kwargs)

    basis_queries = []

    async def basis(symbols, today):
        basis_queries.append((set(symbols), today))
        return {"AU": {"status": "collected", "data_as_of": today.isoformat()}}

    async def warrants(symbols, today):
        return {"SH": {"status": "collected", "data_as_of": today.isoformat()}}

    async def agriculture(today):
        return {}

    monkeypatch.setattr(futures_workspace.public_network, "async_client", client)
    monkeypatch.setattr(futures_workspace.futures_basis, "latest_table", basis)
    monkeypatch.setattr(futures_workspace.futures_inventory, "latest_exchange_tables", warrants)
    monkeypatch.setattr(futures_workspace.futures_workspace_fundamentals, "read", agriculture)
    await futures_workspace._refresh_overview()
    grid = futures_workspace.universe()
    assert sorted(calls) == sorted(symbol + "0" for symbol in futures_workspace.UNIVERSE)
    assert basis_queries == [(set(futures_workspace.UNIVERSE), now)]
    assert futures_workspace._cards == {}
    assert grid["refreshing"] is False
    gold = next(row for row in grid["products"] if row["symbol"] == "AU")["dimensions"]
    assert [gold[key]["status"] for key in futures_workspace.DIMENSIONS] == [
        "collected",
        "collected",
        "collected",
        "partial",
        "recent",
    ]
    assert all(gold[key]["data_as_of"] == now.isoformat() for key in futures_workspace.DIMENSIONS)
    fundamentals = {row["symbol"]: row["dimensions"]["fundamentals"] for row in grid["products"]}
    assert "交易所仓单" in fundamentals["SH"]["reason"]
    assert "欧洲航线结算运价指数" in fundamentals["EC"]["reason"]
    async with client() as test_client:
        raw, error = await futures._continuous(
            test_client, "AU", futures_workspace._now(), float("inf"), workspace=True
        )
    assert error is None and len(raw["bars"]) == 120
    assert raw["workspace_technicals"]["ema20"] == 349.5
    assert raw["workspace_technicals"]["ema60"] == 329.5
    assert raw["workspace_technicals"]["adx14"] == 100.0
    assert raw["workspace_technicals"]["atr14"] == 2.0
    assert raw["workspace_technicals"]["range250_pct"] == round(250 / 251 * 100, 4)
    assert raw["workspace_positioning"]["change"] == 2


def test_short_flat_series_preserves_observed_zero_and_unavailable_metrics():
    from server import futures_signals

    flat = {"close": 50, "low": 50, "high": 50, "volume": 0, "open_interest": 0}
    bars = [{**flat, "date": f"2026-11-{day:02d}"} for day in range(1, 22)]
    signals = futures_signals.technicals(bars)
    assert signals["atr14"] == signals["volatility20_pct"] == 0
    assert signals["ema20"] == 50
    assert signals["adx14"] is signals["range20_pct"] is signals["range250_pct"] is None
    assert futures_signals.positioning(bars) == {
        "open_interest": 0,
        "change": 0,
        "previous_date": "2026-11-20",
        "volume": 0,
        "volume_to_interest": None,
    }


def test_product_benchmarks_keep_publication_date_scope_and_warrant_forecast_separate():
    now = futures_workspace._now()
    farm = futures_workspace.futures_workspace_fundamentals
    report = _HTML()
    report.feed("<p>采集日为11月19日; 全国生猪平均价格9.95元/公斤; 全国鸡蛋平均价格10.38元/公斤</p>")
    reports = farm._livestock_readings(report, farm.LIVESTOCK, now.date())
    assert reports["LH"]["data_as_of"] == "2026-11-19"
    assert reports["JD"]["values"]["rows"][0]["value"] == 10.38
    assert "2026-11-25" in reports["JD"]["values"]["scope"]
    assert farm._starch_reference(now.date())["CS"]["data_as_of"] == "2026-11-24"
    assert farm._starch_reference(now.date() + timedelta(days=7)) == {}

    inventory = futures_workspace.futures_inventory
    rows = [
        ["仓单日报表 2026-11-25"],
        ["品种：苹果AP 单位：张 日期：2026-11-25 每张预报=2手合约*10吨/手=20吨"],
        ["机构编号", "机构简称", "预报数量", "当日增减"],
        ["总计", "", "0.0", "0.0"],
        ["品种：红枣CJ 单位：张 日期：2026-11-25"],
        ["机构编号", "机构简称", "仓单数量", "当日增减"],
        ["总计", "", "137", "-2"],
    ]
    parsed = inventory._parse_czce_rows(rows, now.date())
    assert "AP" not in parsed["segments"]
    assert inventory._apple_forecast(parsed, now.date())["value"] == 0
    assert inventory._czce_observation(parsed, "CJ", now.date())["quantity"] == 137


@pytest.mark.parametrize(
    ("symbol", "session"),
    [("JM", "2026-11-19"), ("RB", "2026-11-16"), ("M", "2026-11-16"), ("EC", "2026-11-17")],
)
async def test_journal_is_bound_to_the_authenticated_instrument(client, auth_cookies, symbol, session):
    url = f"/futures?symbol={symbol.lower()}"
    anon = await client.get(url)
    assert anon.is_redirect and anon.headers["location"] == "/login"
    page = await client.get(url, cookies=auth_cookies)
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert page.text.count('id="journal-study"') == 1
    assert f'data-symbol="{symbol}"' in page.text
    assert f'<option value="{session}">' in page.text
    assert 'maxlength="140"' in page.text
    assert "/static/js/futures-journal.js" in page.text
    assert "__JOURNAL_" not in page.text and "__FUTURES_JOURNAL__" not in page.text


async def test_journal_not_offered_on_overview_or_unimplemented_instruments(client, auth_cookies):
    for symbol in ["", "unknown", "IF", "CU2701"]:
        page = await client.get("/futures", params={"symbol": symbol}, cookies=auth_cookies)
        assert page.status_code == 200
        assert 'id="journal-study"' not in page.text and "/static/js/futures-journal.js" not in page.text
    for symbol in sorted(futures_workspace.UNIVERSE.keys() - {"JM", "RB", "M", "EC"}):
        page = await client.get("/futures", params={"symbol": symbol}, cookies=auth_cookies)
        assert page.status_code == 200
        assert page.text.count('id="journal-study"') == 1
        assert "历史回放" not in page.text
        assert "__JOURNAL_" not in page.text
        assert "__FUTURES_BOOTSTRAP__" not in page.text


async def test_futures_overview_inlines_bootstrap_and_serves_warm_universe_in_milliseconds(
    client, auth_cookies
):
    import time

    from server import futures_margins, futures_signals

    futures_signals._PERCENTILES_CACHE.clear()
    futures_margins._ALL_MARGINS_CACHE.clear()
    base_day = futures_workspace._now().date()
    stamp = time.monotonic()
    for idx, sym in enumerate(futures_workspace.UNIVERSE):
        bars = []
        for offset in range(1250):
            day = (base_day - timedelta(days=1249 - offset)).isoformat()
            close = 1000.0 + idx * 17.0 + (offset % 37) * 3.5 + offset * 0.12
            bars.append(
                {
                    "date": day,
                    "open": close - 2.0,
                    "high": close + 5.0,
                    "low": close - 5.0,
                    "close": close,
                    "volume": 50000 + offset * 10,
                    "open_interest": 120000 + (offset % 19) * 300,
                }
            )
        futures._SERIES_HISTORY[sym] = (stamp, bars)

    t0 = time.perf_counter()
    cold = futures_workspace.universe()
    cold_ms = (time.perf_counter() - t0) * 1000.0

    t1 = time.perf_counter()
    warm = futures_workspace.universe()
    warm_ms = (time.perf_counter() - t1) * 1000.0

    assert cold["count"] == warm["count"] == 46
    assert warm["radar"]["observed_products"] == 46
    sc_preview = next(p["quote_preview"] for p in warm["products"] if p["symbol"] == "SC")
    assert sc_preview["seasonal_pct"] is not None
    assert sc_preview["oi_pct"] is not None
    assert sc_preview["signal_tag"] in {"五年高位", "五年低位", "持仓拥挤", "缩量低仓", None}
    assert warm == cold
    assert warm_ms < 50.0
    assert warm_ms < cold_ms * 0.25

    overview_page = await client.get("/futures", cookies=auth_cookies)
    assert overview_page.status_code == 200
    assert 'id="futures-bootstrap" type="application/json">' in overview_page.text
    assert "__FUTURES_BOOTSTRAP__" not in overview_page.text

    detail_page = await client.get("/futures?symbol=RB", cookies=auth_cookies)
    assert detail_page.status_code == 200
    assert 'id="futures-bootstrap"' not in detail_page.text
    assert "__FUTURES_BOOTSTRAP__" not in detail_page.text
