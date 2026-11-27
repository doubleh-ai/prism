from datetime import date, datetime

import pytest

from server import futures_basis, futures_evidence, futures_jm_sources
from server.futures_weekly_releases import Release


def brief(symbol="M", stamp="2026-11-24T16:30", editions=futures_jm_sources.ARCHIVE, **readings):
    empty = {"status": "unavailable"}
    card = {"symbol": symbol, "market": empty, "technicals": empty, "inventory": empty, "spot_history": empty}
    now = datetime.fromisoformat(stamp + "+08:00")
    return futures_evidence.research_brief({**card, **readings}, now, editions, fresh_days=4)


@pytest.mark.parametrize(
    ("symbol", "label", "delta", "unit"),
    [
        ("JM", "进口焦煤库存", 21.31, "万吨"),
        ("RB", "螺纹厂库", 4.46, "万吨"),
        ("M", "油厂豆粕库存", -8.03, "万吨"),
        ("EC", "SCFIS欧洲航线", -87.68, "点"),
    ],
)
def test_actual_weekly_vintages_have_dated_comparisons(symbol, label, delta, unit):
    block = brief(symbol)["fundamentals"]
    assert block["status"] == "collected"
    row = next(r for r in block["rows"] if r["label"] == label)
    assert row["change"] == delta and row["unit"] == unit
    assert row["scope"] and row["previous_date"] < row["data_as_of"]
    assert row["previous_source"]["url"].startswith("https://") and row["source"]["url"].startswith(
        "https://"
    )


def test_inventory_disclosure_cutoff_and_later_staleness():
    label = "油厂豆粕库存"
    before = brief(stamp="2026-11-23T16:16")["fundamentals"]
    assert not any(r["label"] == label for r in before["rows"])
    after = brief(stamp="2026-11-23T16:17")["fundamentals"]
    row = next(r for r in after["rows"] if r["label"] == label)
    assert (row["previous"], row["current"], row["known_at"]) == (125.23, 117.20, "2026-11-23T16:17+08:00")
    assert all("预估" not in r["label"] for r in after["rows"])
    old = brief(stamp="2026-12-03T17:00")["fundamentals"]
    assert old["status"] == "stale" and all(r["stale"] for r in old["rows"])


def test_missing_intermediate_jm_survey_is_not_weekly_change():
    prior, latest = (
        Release(f"2026-11-{d}", f"2026-11-{d}T09:00", f"https://example.test/port/{d}", {"port": qty})
        for d, qty in (("06", 2), ("20", 3))
    )
    assert brief("JM", editions=[latest, prior])["fundamentals"]["rows"] == []


def test_daily_sources_preserve_previous_units_dates_and_source_reported_zero():
    end = date(2026, 11, 23)
    name = futures_basis.SOURCE_NAME
    history = {
        "status": "collected",
        "source": {"name": name, "url": futures_basis.page_url(end)},
        "data_as_of": end.isoformat(),
        "values": {
            "unit": "元/吨",
            "observations": [
                {"date": "2026-11-20", "price": 3200, "unit": "元/吨", "source": name},
                {"date": "2026-11-23", "price": 3250, "unit": "元/吨", "source": name},
            ],
        },
    }
    inventory = {
        "status": "collected",
        "source": {"name": "交易所", "url": "https://example.test/daily"},
        "data_as_of": "2026-11-23",
        "values": {"unit": "手", "quantity": 0, "change": 0, "segment": "指定仓库", "basis": "已注册仓单"},
    }
    rows = brief("CU", inventory=inventory, spot_history=history)["fundamentals"]["rows"]
    warrant, spot = rows
    assert warrant["previous"] is None and warrant["previous_date"] is None and warrant["change"] == 0
    assert spot["change"] == 50 and spot["previous_source"]["url"] == futures_basis.page_url(
        date(2026, 11, 20)
    )
    history["values"]["observations"][0]["unit"] = "元/千克"
    assert brief("CU", spot_history=history)["fundamentals"]["status"] == "unavailable"


@pytest.mark.parametrize(
    ("close", "fast", "slow", "day", "expected"),
    [
        (31, 20, 10, "2026-11-24", "bullish"),
        (10, 20, 31, "2026-11-24", "bearish"),
        (30, 20, 40, "2026-11-24", "mixed"),
        (20, 30, 10, "2026-11-24", "mixed"),
        (20, 20, 10, "2026-11-24", "neutral"),
        (10, 20, None, "2026-11-24", "unavailable"),
        (30, 20, 10, "2026-11-19", "unavailable"),
    ],
)
def test_technical_reading_requires_current_aligned_continuous_data(close, fast, slow, day, expected):
    market = {
        "status": "collected",
        "data_as_of": day,
        "source": {"name": "日报", "url": "https://example.test/continuous"},
        "values": {"close": close, "quote_unit": "元/吨"},
    }
    technical = {
        "status": "partial",
        "data_as_of": day,
        "values": {"ema20": fast, "ema60": slow, "adx14": 75},
    }
    row = brief("CU", market=market, technicals=technical)["technical"]
    assert row["bias"] == expected and row["close"] == close and row["data_as_of"] == day
    if day == "2026-11-19":
        assert row["status"] == "stale"
    technical["data_as_of"] = "2026-11-18"
    assert brief("CU", market=market, technicals=technical)["technical"]["bias"] == "unavailable"
