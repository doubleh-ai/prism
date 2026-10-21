"""Indicator library CRUD + translate endpoint."""

from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_library_seeds_builtins(client, auth_cookies):
    r = await client.get("/api/indicators", cookies=auth_cookies)
    assert r.status_code == 200
    inds = r.json()["indicators"]
    assert len(inds) >= 7  # built-in starter indicators
    assert any(i["kind"] == "signal" for i in inds)
    assert any(i.get("params", {}).get("items") for i in inds)
    assert all("visual" in i and "plan" in i for i in inds)
    assert any(i["name"] == "多级均线放量共振(5/10/20)" for i in inds)


@pytest.mark.asyncio
async def test_builtin_parameters_have_indicator_specific_ui_metadata(client, auth_cookies):
    r = await client.get("/api/indicators", cookies=auth_cookies)
    assert r.status_code == 200
    by_name = {item["name"]: item for item in r.json()["indicators"]}

    ma = by_name["均线金叉(5/20)"]
    rsi = by_name["RSI超卖反弹"]
    volume = by_name["放量突破"]
    boll = by_name["BOLL上轨"]
    composite = by_name["多级均线放量共振(5/10/20)"]

    assert ma["params"]["ui"]["family"] == "ma_cross"
    assert rsi["params"]["ui"]["family"] == "rsi"
    assert volume["params"]["ui"]["family"] == "volume_breakout"
    assert boll["params"]["ui"]["family"] == "bollinger"
    assert composite["params"]["ui"]["family"] == "ma_stack_volume"
    assert len(composite["params"]["ui"]["conditions"]) == 3
    assert composite["params"]["ui"]["logic_template"].endswith("三个条件全部满足才触发")
    assert ma["params"]["ui"]["logic_template"] != volume["params"]["ui"]["logic_template"]
    assert ma["params"]["ui"]["presets"]
    assert rsi["params"]["items"][0]["control"] == "period"
    assert rsi["params"]["items"][1]["control"] == "threshold"
    assert volume["params"]["items"][1]["control"] == "ratio"
    assert all(item.get("help") for item in boll["params"]["items"])
    composite_params = {item["key"]: item for item in composite["params"]["items"]}
    assert composite_params["sync_window"]["group"] == "共振规则"
    assert "不包含当天" in composite_params["baseline_window"]["help"]


@pytest.mark.asyncio
async def test_existing_library_gets_composite_once_and_deletion_is_respected(client, auth_cookies):
    created = await client.post(
        "/api/indicators",
        cookies=auth_cookies,
        json={"name": "existing-custom", "kind": "plot", "dsl": {"ma": 12}},
    )
    assert created.status_code == 200

    first = await client.get("/api/indicators", cookies=auth_cookies)
    composites = [item for item in first.json()["indicators"] if item["name"] == "多级均线放量共振(5/10/20)"]
    assert len(composites) == 1

    second = await client.get("/api/indicators", cookies=auth_cookies)
    assert sum(item["name"] == "多级均线放量共振(5/10/20)" for item in second.json()["indicators"]) == 1

    deleted = await client.delete("/api/indicators/" + composites[0]["id"], cookies=auth_cookies)
    assert deleted.status_code == 200
    after_delete = await client.get("/api/indicators", cookies=auth_cookies)
    assert not any(item["name"] == "多级均线放量共振(5/10/20)" for item in after_delete.json()["indicators"])


@pytest.mark.asyncio
async def test_legacy_indicator_params_are_enriched_without_rewrite(client, auth_cookies):
    created = await client.post(
        "/api/indicators",
        cookies=auth_cookies,
        json={
            "name": "legacy-window",
            "kind": "plot",
            "dsl": {"ma": {"param": ["window", 20]}},
            "params": {
                "items": [{"key": "window", "label": "周期", "value": 20, "min": 2, "max": 120, "step": 1}]
            },
        },
    )
    assert created.status_code == 200

    listed = await client.get("/api/indicators", cookies=auth_cookies)
    legacy = next(item for item in listed.json()["indicators"] if item["name"] == "legacy-window")
    param = legacy["params"]["items"][0]
    assert param["control"] == "period"
    assert param["quick_values"]
    assert param["help"]
    assert legacy["params"]["ui"]["family"] == "custom"


@pytest.mark.asyncio
async def test_create_and_delete(client, auth_cookies):
    r = await client.post(
        "/api/indicators",
        cookies=auth_cookies,
        json={"name": "myMA", "kind": "plot", "dsl": {"ma": 10}, "description": "x"},
    )
    assert r.status_code == 200
    iid = r.json()["id"]
    r = await client.get("/api/indicators", cookies=auth_cookies)
    assert "myMA" in [i["name"] for i in r.json()["indicators"]]
    r = await client.delete("/api/indicators/" + iid, cookies=auth_cookies)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_create_rejects_non_whitelist_dsl(client, auth_cookies):
    r = await client.post(
        "/api/indicators",
        cookies=auth_cookies,
        json={"name": "bad", "kind": "plot", "dsl": {"exec": ["malicious"]}},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_translate_passthrough(client, auth_cookies):
    with patch("server.indicators.router.translate") as tr:
        tr.return_value = {
            "name": "金叉",
            "kind": "signal",
            "description": "d",
            "dsl": {"cross_up": [{"ma": 5}, {"ma": 20}]},
        }
        r = await client.post("/api/indicators/translate", cookies=auth_cookies, json={"input": "均线金叉"})
    assert r.status_code == 200
    assert r.json()["dsl"]["cross_up"] is not None


@pytest.mark.asyncio
async def test_translate_requires_auth(client):
    r = await client.post("/api/indicators/translate", json={"input": "x"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_isolation_between_users(client, auth_cookies):
    # user A creates an indicator
    await client.post(
        "/api/indicators", cookies=auth_cookies, json={"name": "privateA", "kind": "plot", "dsl": {"ma": 5}}
    )
    # user B (different session) should NOT see it
    from server.auth import create_session, hash_password
    from server.database import get_db

    with get_db() as db:
        db.execute(
            "INSERT OR REPLACE INTO users(username,password_hash) VALUES(?,?)", ["userB", hash_password("p")]
        )
    b_cookies = {"token": create_session("userB")}
    r = await client.get("/api/indicators", cookies=b_cookies)
    assert "privateA" not in [i["name"] for i in r.json()["indicators"]]


@pytest.mark.asyncio
async def test_build_df_uses_sync_market_chain(monkeypatch):
    """Indicator evaluation should call the shared market chain directly."""
    from server.indicators import router as indicator_router
    from server.market import router as market_router
    from server.market.models import Fundamentals, KLine

    class FakeChain:
        def __init__(self):
            self.kline_calls = 0

        async def get_kline(self, ticker, period="1d", limit=150):
            self.kline_calls += 1
            assert ticker == "003000"
            assert period == "1d"
            return [
                KLine(
                    timestamp=1,
                    open=10,
                    high=11,
                    low=9,
                    close=10.5,
                    volume=1000,
                )
            ], False

        async def get_fundamentals(self, ticker):
            return Fundamentals(ticker=ticker, timestamp=1, source="fake", pe_ttm=12.3), False

    fake = FakeChain()
    monkeypatch.setattr(market_router, "get_chain", lambda: fake)

    df, fund = await indicator_router._build_df("003000", period="daily", limit=1)

    assert fake.kline_calls == 1
    assert df is not None
    assert df.iloc[0]["close"] == 10.5
    assert fund["pe"] == 12.3


@pytest.mark.asyncio
async def test_build_df_falls_back_to_chart_cache_limit(monkeypatch):
    """Indicator evaluation should reuse the 120-bar chart cache if 150 bars miss."""
    from server.indicators import router as indicator_router
    from server.market import router as market_router
    from server.market.models import KLine

    class FakeChain:
        def __init__(self):
            self.kline_limits = []

        async def get_kline(self, ticker, period="1d", limit=150):
            self.kline_limits.append(limit)
            if limit == 150:
                raise RuntimeError("provider temporarily unavailable")
            return [
                KLine(timestamp=1, open=10, high=11, low=9, close=10.5, volume=1000),
                KLine(timestamp=2, open=10.5, high=12, low=10, close=11.5, volume=1200),
            ], False

        async def get_fundamentals(self, ticker):
            raise RuntimeError("optional")

    fake = FakeChain()
    monkeypatch.setattr(market_router, "get_chain", lambda: fake)

    df, fund = await indicator_router._build_df("003000", period="daily", limit=150)

    assert fake.kline_limits == [150, 120]
    assert df is not None
    assert df.iloc[-1]["close"] == 11.5
    assert fund == {}


@pytest.mark.asyncio
async def test_eval_uses_client_kline_without_provider(client, auth_cookies, monkeypatch):
    """Chart overlay should compute against current chart data without refetching K-line."""
    from server.indicators import router as indicator_router

    async def fail_build_df(*args, **kwargs):
        raise AssertionError("provider should not be called when kline payload is present")

    monkeypatch.setattr(indicator_router, "_build_df", fail_build_df)
    created = await client.post(
        "/api/indicators",
        cookies=auth_cookies,
        json={"name": "MA2", "kind": "plot", "dsl": {"ma": 2}, "description": "ma"},
    )
    assert created.status_code == 200
    iid = created.json()["id"]

    r = await client.post(
        f"/api/indicators/{iid}/eval",
        cookies=auth_cookies,
        json={
            "ticker": "003000",
            "period": "1d",
            "kline": [
                {"timestamp": 1, "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1000},
                {"timestamp": 2, "open": 10, "high": 12, "low": 9.5, "close": 12, "volume": 1200},
                {"timestamp": 3, "open": 12, "high": 13, "low": 11, "close": 11, "volume": 900},
            ],
        },
    )

    assert r.status_code == 200
    data = r.json()
    assert data["kind"] == "plot"
    assert data["points"][-1]["v"] == 11.5


@pytest.mark.asyncio
async def test_eval_signal_returns_chart_events_and_params(client, auth_cookies, monkeypatch):
    """Signal indicators should return rich chart metadata, not only timestamps."""
    from server.indicators import router as indicator_router

    async def fail_build_df(*args, **kwargs):
        raise AssertionError("provider should not be called when kline payload is present")

    monkeypatch.setattr(indicator_router, "_build_df", fail_build_df)
    created = await client.post(
        "/api/indicators",
        cookies=auth_cookies,
        json={
            "name": "closeAboveMA",
            "kind": "signal",
            "description": "close > ma",
            "dsl": {"gt": [{"price": "close"}, {"ma": {"param": ["window", 2]}}]},
            "params": {
                "items": [{"key": "window", "label": "窗口", "value": 2, "min": 2, "max": 20, "step": 1}]
            },
            "visual": {"style": "signal_track", "color": "#22c55e", "marker": "▲", "track": True},
            "plan": {"period": "1d", "lookback": 120},
        },
    )
    assert created.status_code == 200
    iid = created.json()["id"]

    r = await client.post(
        f"/api/indicators/{iid}/eval",
        cookies=auth_cookies,
        json={
            "ticker": "003000",
            "period": "1d",
            "params": {"window": 2},
            "kline": [
                {"timestamp": 1, "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1000},
                {"timestamp": 2, "open": 10, "high": 12, "low": 9.5, "close": 12, "volume": 1200},
                {"timestamp": 3, "open": 12, "high": 13, "low": 11, "close": 13, "volume": 900},
            ],
        },
    )

    assert r.status_code == 200
    data = r.json()
    assert data["kind"] == "signal"
    assert data["latest"] is True
    assert data["events"][-1]["reason"]
    assert data["events"][-1]["strength"] == 100
    assert data["tracks"][0]["events"]
    assert data["zones"]
    assert data["visual"]["color"] == "#22c55e"
    assert data["params"]["window"] == 2


def test_market_universe_filters_market_board_and_st():
    from server.indicators.scanner import _board_for_code, _market_for_code, filter_universe

    assert _market_for_code("920000") == "BJ"
    assert _board_for_code("920000") == "bse"

    rows = [
        {"ticker": "600000", "name": "浦发银行", "market": "SH", "board": "main"},
        {"ticker": "688001", "name": "华兴源创", "market": "SH", "board": "star"},
        {"ticker": "300001", "name": "特锐德", "market": "SZ", "board": "gem"},
        {"ticker": "000001", "name": "ST测试", "market": "SZ", "board": "main"},
        {"ticker": "920000", "name": "安徽凤凰", "market": "BJ", "board": "bse"},
    ]

    selected = filter_universe(
        rows,
        {
            "markets": ["SH", "SZ"],
            "boards": ["main", "gem"],
            "exclude_st": True,
        },
    )

    assert [row["ticker"] for row in selected] == ["600000", "300001"]


@pytest.mark.asyncio
async def test_scan_job_api_persists_full_market_config(client, auth_cookies, monkeypatch):
    from server.indicators import scanner

    library = await client.get("/api/indicators", cookies=auth_cookies)
    indicator = next(item for item in library.json()["indicators"] if item["kind"] == "signal")
    monkeypatch.setattr(scanner, "start_job", lambda *args, **kwargs: None)

    response = await client.post(
        f"/api/indicators/{indicator['id']}/scan-jobs",
        cookies=auth_cookies,
        json={
            "scope": "market",
            "period": "1d",
            "lookback": 180,
            "trigger_window": 3,
            "params": {"fast": 8, "slow": 34},
            "markets": ["SH", "SZ"],
            "boards": ["main", "gem", "star"],
            "exclude_st": True,
            "min_turnover": 50000000,
            "max_results": 80,
            "sort": "recent_signal",
            "direction": "asc",
        },
    )

    assert response.status_code == 202
    job = response.json()["job"]
    assert job["status"] == "queued"
    assert job["config"]["scope"] == "market"
    assert job["config"]["lookback"] == 180
    assert job["config"]["params"]["fast"] == 8
    assert job["config"]["sort"] == "recent_signal"
    assert job["config"]["direction"] == "asc"

    detail = await client.get(f"/api/indicators/scan-jobs/{job['id']}", cookies=auth_cookies)
    assert detail.status_code == 200
    assert detail.json()["job"]["indicator_name"] == indicator["name"]

    cancelled = await client.post(
        f"/api/indicators/scan-jobs/{job['id']}/cancel",
        cookies=auth_cookies,
    )
    assert cancelled.status_code == 200
    assert scanner.get_job(job["id"], "testuser")["status"] == "cancelled"


@pytest.mark.asyncio
async def test_background_scan_returns_partial_signal_results():
    import pandas as pd

    from server.indicators import scanner

    indicator = {
        "id": "scan-signal",
        "name": "收盘强于均线",
        "kind": "signal",
        "dsl": {"gt": [{"price": "close"}, {"ma": {"param": ["window", 2]}}]},
        "params": {"items": [{"key": "window", "value": 2}]},
    }
    config = {
        "scope": "watchlist",
        "tickers": ["000001", "000002"],
        "ticker_names": {"000001": "平安银行", "000002": "万科A"},
        "period": "1d",
        "lookback": 60,
        "trigger_window": 1,
        "params": {"window": 2},
        "markets": ["SZ"],
        "boards": ["main"],
        "exclude_st": True,
        "min_turnover": 0,
        "max_results": 20,
        "sort": "strength",
        "direction": "desc",
    }
    job = scanner.create_job("testuser", indicator, config)

    async def fake_build_df(ticker, period, limit, include_fundamentals=True):
        closes = [1.0, 2.0, 3.0] if ticker == "000001" else [3.0, 2.0, 1.0]
        return pd.DataFrame(
            {
                "t": [1, 2, 3],
                "open": closes,
                "high": [x + 0.2 for x in closes],
                "low": [x - 0.2 for x in closes],
                "close": closes,
                "volume": [1000, 1200, 1400],
                "amount": [10000, 24000, 42000],
            }
        ), {}

    await scanner._run_job(job, indicator, fake_build_df, fast_fetch=False)
    completed = scanner.get_job(job["id"], "testuser")

    assert completed["status"] == "completed"
    assert completed["processed"] == 2
    assert completed["matched"] == 1
    assert completed["matches"][0]["ticker"] == "000001"
    assert completed["matches"][0]["name"] == "平安银行"
