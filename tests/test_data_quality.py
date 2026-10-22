"""Financial inputs remain unknown when unavailable; failed coverage is explicit."""

from types import SimpleNamespace

import pandas as pd
import pytest

from server.indicators.dsl import MissingData, evaluate


def bars(values):
    return pd.DataFrame(
        {
            "t": [1792512000 + i * 86400 for i in range(len(values))],
            "open": values,
            "high": values,
            "low": values,
            "close": values,
            "volume": [100.0] * len(values),
            "amount": [1000.0] * len(values),
        }
    )


@pytest.mark.parametrize(
    "values,expected", [(list(range(40)), 100), (list(range(40, 0, -1)), 0), ([10] * 40, 50)]
)
def test_rsi_warmup_and_zero_loss(values, expected):
    result = evaluate({"rsi": 14}, bars(values))
    assert result.iloc[:14].isna().all()
    assert result.iloc[-1] == expected


@pytest.mark.parametrize("value", [None, "", float("nan"), float("inf")])
def test_missing_fundamental_is_not_a_false_signal(value):
    with pytest.raises(MissingData):
        evaluate({"lt": [{"fund": "pe"}, 20]}, bars([10] * 20), {"pe": value})


def test_other_cache_write_preserves_bounded_stale_fallback(monkeypatch):
    from server.market import cache

    now = [1000.0]
    monkeypatch.setattr(cache.time, "time", lambda: now[0])
    c = cache.MarketCache(max_stale_seconds=100)
    c.set("A", {"price": 10}, ttl=10)
    now[0] = 1020
    c.set("B", {"price": 20}, ttl=10)
    assert c.get("A")[0] is None
    assert c.get_stale("A") == ({"price": 10}, True)
    now[0] = 1111
    assert c.get_stale("A") == (None, True)


async def test_supplied_chart_bars_still_load_required_fundamentals(client, auth_cookies, monkeypatch):
    from server.indicators.models import IndicatorCreate
    from server.indicators.router import create_owned_indicator
    from server.market import router

    async def get_fundamentals(ticker):
        assert ticker == "600519"
        return SimpleNamespace(model_dump=lambda: {"pe_ttm": 10}), False

    async def no_kline(*args, **kwargs):
        raise AssertionError("Provided chart bars must not be fetched again")

    monkeypatch.setattr(
        router, "get_chain", lambda: SimpleNamespace(get_fundamentals=get_fundamentals, get_kline=no_kline)
    )
    iid = create_owned_indicator(
        "testuser", IndicatorCreate(name="PE threshold", kind="signal", dsl={"lt": [{"fund": "pe"}, 20]})
    )["id"]
    response = await client.post(
        f"/api/indicators/{iid}/eval",
        cookies=auth_cookies,
        json={"ticker": "600519", "kline": bars([10] * 20).to_dict("records")},
    )
    assert response.status_code == 200
    assert response.json()["latest"] is True


@pytest.mark.parametrize(
    "mode,expected",
    [("failed", "failed"), ("missing", "failed"), ("mixed", "partial"), ("valid", "completed")],
)
async def test_scan_coverage_is_not_silently_completed(mode, expected):
    from server.indicators import scanner

    indicator = {"id": "coverage", "name": "PE", "kind": "signal", "dsl": {"lt": [{"fund": "pe"}, 20]}}
    job = scanner.create_job(
        "owner", indicator, {"scope": "watchlist", "tickers": ["600519", "000001"], "period": "1d"}
    )

    async def build(ticker, *args, **kwargs):
        if mode == "failed":
            raise TimeoutError
        fund = {} if mode == "missing" or mode == "mixed" and ticker == "000001" else {"pe": 30}
        return bars([10] * 20), fund

    await scanner._run_job(job, indicator, build, fast_fetch=False)
    result = scanner.get_job(job["id"], "owner")
    assert result["status"] == expected
    assert result["evaluated"] + result["failed"] + result["insufficient"] + result["excluded"] == 2
    assert result["matched"] == 0  # Valid nonmatches are distinct from missing inputs.


@pytest.mark.parametrize("mode", ["warmup", "missing_turnover", "old_numeric"])
async def test_scan_never_labels_unknown_latest_as_valid(mode):
    from server.indicators import scanner

    dsl = {"gt": [{"rsi": 14}, 70]} if mode == "warmup" else {"price": "close"}
    indicator = {"id": mode, "name": mode, "kind": "signal" if mode == "warmup" else "plot", "dsl": dsl}
    job = scanner.create_job(
        "owner",
        indicator,
        {"scope": "watchlist", "tickers": ["600519"], "min_turnover": 1 if mode == "missing_turnover" else 0},
    )

    async def build(*args, **kwargs):
        df = bars([10.0] * 5)
        if mode == "missing_turnover":
            df = df.drop(columns="amount")
        if mode == "old_numeric":
            df.loc[df.index[-1], "close"] = float("nan")
        return df, {}

    await scanner._run_job(job, indicator, build, fast_fetch=False)
    result = scanner.get_job(job["id"], "owner")
    assert result["status"] == "failed"
    assert result["insufficient"] == 1
    assert result["evaluated"] == result["excluded"] == 0


async def test_background_graph_does_not_filter_by_legacy_confidence(client, auth_cookies):
    from server.database import get_db

    with get_db() as db:
        db.execute(
            "INSERT INTO industry_nodes(id,name,category,tier,confidence,source_date) VALUES(?,?,?,?,?,?)",
            ["low-legacy", "Legacy background", "test", 0, 0.01, "seed"],
        )
    result = (
        await client.get("/api/industry/graph?category=test&min_confidence=1", cookies=auth_cookies)
    ).json()
    assert [x["id"] for x in result["nodes"]] == ["low-legacy"]
    assert result["nodes"][0]["confidence"] is None
    assert result["confidence_filter_applied"] is False
