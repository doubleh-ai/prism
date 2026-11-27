"""Tests for deterministic cross-commodity margins, 5Y percentiles, roll diagnostics and quant adapter."""

from datetime import date, timedelta

import httpx

from server import futures_margins, futures_quant_provider, futures_signals


def _make_series(
    start_price: float, step: float, days: int = 300, end_day: date = date(2026, 11, 24)
) -> list[dict]:
    bars = []
    for idx in range(days):
        d = end_day - timedelta(days=days - 1 - idx)
        close = round(start_price + step * idx, 2)
        bars.append(
            {
                "date": d.isoformat(),
                "open": close - 1,
                "high": close + 2,
                "low": close - 2,
                "close": close,
                "volume": 10000 + idx * 10,
                "open_interest": 50000 + idx * 25,
                "settlement": close,
            }
        )
    return bars


def test_soy_crush_and_blast_margins_require_exact_same_day_alignment():
    day = date(2026, 11, 24)
    histories = {
        "B": _make_series(3600, 1.0, 260, day),
        "M": _make_series(2900, 1.2, 260, day),
        "Y": _make_series(7800, 2.0, 260, day),
        "RB": _make_series(3200, 1.5, 260, day),
        "HC": _make_series(3350, 1.8, 260, day),
        "I": _make_series(750, 0.3, 260, day),
        "JM": _make_series(1250, 0.4, 260, day),
    }
    crush = futures_margins.compute_margin(
        futures_margins.SPECS_BY_ID["soy_crush_margin"], histories, today=day
    )
    assert crush["status"] == "collected"
    assert crush["data_as_of"] == "2026-11-24"
    b_last = histories["B"][-1]["close"]
    m_last = histories["M"][-1]["close"]
    y_last = histories["Y"][-1]["close"]
    expected = round(0.785 * m_last + 0.185 * y_last - b_last, 2)
    assert crush["values"]["current"] == expected
    assert crush["values"]["pct_5y"] == 100.0
    assert crush["values"]["change_1d"] > 0

    # Mismatched date on one leg must immediately degrade without mixing dates
    mismatched = {**histories, "Y": histories["Y"][:-1]}
    bad = futures_margins.compute_margin(
        futures_margins.SPECS_BY_ID["soy_crush_margin"], mismatched, today=day
    )
    assert bad["status"] == "unavailable"
    assert bad["reason_code"] == "leg_date_mismatch"
    assert bad["values"] is None

    # Missing leg must return leg_unavailable
    missing = {**histories, "M": None}
    miss = futures_margins.compute_margin(futures_margins.SPECS_BY_ID["soy_crush_margin"], missing, today=day)
    assert miss["status"] == "unavailable"
    assert miss["reason_code"] == "leg_unavailable"


def test_au_ag_mass_ratio_and_all_specs_map_cleanly():
    day = date(2026, 11, 24)
    histories = {
        "AU": _make_series(580, 0.2, 60, day),
        "AG": _make_series(7200, 1.0, 60, day),
    }
    ratio = futures_margins.compute_margin(futures_margins.SPECS_BY_ID["au_ag_ratio"], histories, today=day)
    assert ratio["status"] == "collected"
    au_close = histories["AU"][-1]["close"]
    ag_close = histories["AG"][-1]["close"]
    assert ratio["values"]["current"] == round(au_close * 1000.0 / ag_close, 2)
    assert len(futures_margins.specs_for_symbol("RB")) == 4


def test_five_year_and_seasonal_percentiles_and_roll_detection():
    bars = _make_series(2000, 0.8, days=1100, end_day=date(2026, 11, 24))
    pct = futures_signals.percentiles(bars)
    assert pct["full_5y"] is True
    assert pct["span_label"] == "近5年"
    assert pct["close_pct_5y"] == 100.0
    assert pct["close_seasonal_pct"] is not None and pct["seasonal_years"] >= 3
    assert pct["seasonal_median"] is not None and pct["seasonal_median"] > 0
    assert pct["open_interest_pct_rank"] == 100.0

    # Verify seasonal thermometer card does not duplicate percentile text in value_text
    from pathlib import Path

    from server import futures_ontology

    causal = futures_ontology.build_causal_narrative(
        {
            "symbol": "AU",
            "name": "黄金",
            "market": {"values": {"close": 767.62, "quote_unit": "元/克"}},
            "percentiles": pct,
        }
    )
    seasonal_card = next(t for t in causal["thermometers"] if t["id"] == "price_seasonal")
    assert seasonal_card["value_text"].startswith("同期中位 ")
    assert "元/克" in seasonal_card["value_text"]
    assert "分位" not in seasonal_card["value_text"]
    css = Path("static/css/futures.css").read_text(encoding="utf-8")
    assert ".futures-thermo-head { display: grid;" in css
    assert "white-space: nowrap;" in css

    # Simulate roll gap + OI drop + split active/second contract OI
    rolled = [
        *bars[:-1],
        {
            **bars[-1],
            "open": bars[-2]["close"] * 1.045,
            "open_interest": int(bars[-2]["open_interest"] * 0.75),
        },
    ]
    ex_daily = {
        "status": "collected",
        "data_as_of": "2026-11-24",
        "contracts": [
            {"contract": "rb2701", "open_interest": 42000, "volume": 80000},
            {"contract": "rb2705", "open_interest": 51000, "volume": 95000},
        ],
    }
    diag = futures_signals.roll_diagnostics(rolled, ex_daily)
    assert diag["roll_suspect"] is True
    assert diag["active_contract"] == "RB2705"
    assert diag["second_contract"] == "RB2701"
    assert "换月" in diag["roll_notice"]


async def test_quant_provider_stays_disabled_by_default_and_parses_when_configured(monkeypatch):
    monkeypatch.delenv("PRISM_TUSHARE_TOKEN", raising=False)
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    assert futures_quant_provider.configured() is False

    monkeypatch.setenv("PRISM_TUSHARE_TOKEN", "synthetic_valid_token_1234567890")
    assert futures_quant_provider.configured() is True

    def serve(request: httpx.Request):
        payload = __import__("json").loads(request.content.decode())
        if payload["api_name"] == "fut_daily":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "fields": ["ts_code", "trade_date", "close", "settle", "vol", "oi"],
                        "items": [
                            ["M2701.DCE", "20261124", 2890.0, 2885.0, 620000, 1450000],
                            ["M2705.DCE", "20261124", 2780.0, 2776.0, 310000, 890000],
                        ],
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "fields": ["trade_date", "symbol", "warehouse", "vol", "vol_chg", "unit"],
                    "items": [
                        ["20261124", "LC", "广期所交割库A", 28500, -320, "手"],
                        ["20261124", "LC", "广期所交割库B", 11500, 120, "手"],
                    ],
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        daily = await futures_quant_provider.read_daily_contracts(
            client, "M", "DCE", "元/吨", date(2026, 11, 24), float("inf")
        )
        assert daily["status"] == "collected"
        assert [c["contract"] for c in daily["contracts"]] == ["m2701", "m2705"]
        wsr = await futures_quant_provider.read_warrant(
            client, "LC", "GFEX", date(2026, 11, 24), float("inf")
        )
        assert wsr["status"] == "collected"
        assert (wsr["quantity"], wsr["change"], wsr["unit"]) == (40000, -200, "手")
