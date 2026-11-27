"""Continuous futures K-lines for ``market_kline`` (production run 01f6e865: RB0).

The production failure was a coverage gap, not a dead data source: Sina's
futures daily series was reachable while the stock-only providers mapped the
continuous code ``RB0`` to ``szRB0`` and returned no bars.
"""

import json
from datetime import datetime, timedelta

import pytest

from server.market import tencent_provider as provider_mod
from server.market.clock import MARKET_TZ, market_timestamp
from server.market.provider import ProviderUnavailable
from server.market.tencent_provider import TencentProvider, is_futures_continuous_ticker


def _jsonp(rows) -> bytes:
    return ("/*<script>location.href='//sina.com';</script>*/\n=(" + json.dumps(rows) + ");").encode("utf-8")


def _daily_rows(count: int) -> list[dict]:
    """Weekday rows ending comfortably before today, so no date is in the future."""
    rows: list[dict] = []
    day = datetime.now(tz=MARKET_TZ).date() - timedelta(days=count * 2 + 7)
    while len(rows) < count:
        if day.weekday() < 5:
            price = 3000.0 + len(rows)
            rows.append(
                {
                    "d": day.isoformat(),
                    "o": f"{price:.2f}",
                    "h": f"{price + 3:.2f}",
                    "l": f"{price - 3:.2f}",
                    "c": f"{price + 1:.2f}",
                    "v": str(1000 + len(rows)),
                    "p": "500",
                    "s": "0.000",
                }
            )
        day += timedelta(days=1)
    return rows


def _patch_sina(monkeypatch, body: bytes) -> dict:
    captured: dict = {"body": body}

    def fake_fetch(req, timeout, max_bytes=provider_mod.MAX_BODY_BYTES):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["max_bytes"] = max_bytes
        return captured["body"]

    monkeypatch.setattr(provider_mod, "_fetch_public", fake_fetch)
    return captured


def test_futures_continuous_ticker_detection():
    for ticker in ("RB0", "rb0", "IF0", "T0", "AU0"):
        assert is_futures_continuous_ticker(ticker) is True
    for ticker in ("600519", "000001", "AAPL", "00700", "RB", "RB00", "sh600519"):
        assert is_futures_continuous_ticker(ticker) is False


async def test_daily_continuous_kline_comes_from_sina_futures_series(monkeypatch):
    rows = _daily_rows(60)
    captured = _patch_sina(monkeypatch, _jsonp(rows))

    bars = await TencentProvider().get_kline("RB0", period="1d", limit=30)

    assert len(bars) == 30
    assert captured["url"].startswith(provider_mod._SINA_FUTURES_KLINE_URL)
    assert "symbol=RB0" in captured["url"]
    assert captured["timeout"] == 12
    assert captured["max_bytes"] == provider_mod.MAX_FUTURES_BODY_BYTES
    last_row, last_bar = rows[-1], bars[-1]
    assert last_bar.close == pytest.approx(float(last_row["c"]))
    assert last_bar.open == pytest.approx(float(last_row["o"]))
    assert last_bar.volume == pytest.approx(float(last_row["v"]))
    assert last_bar.turnover is None
    assert last_bar.adjustment == "none"
    assert last_bar.timestamp == market_timestamp(last_row["d"])
    assert bars[0].timestamp == market_timestamp(rows[-30]["d"])
    assert last_bar.provenance is not None
    assert last_bar.provenance.source == "sina"
    assert last_bar.provenance.adapter == "tencent"
    assert last_bar.provenance.source_url == captured["url"]
    assert last_bar.provenance.retrieved_at > 0


async def test_weekly_and_monthly_continuous_bars_are_aggregated(monkeypatch):
    rows = _daily_rows(300)
    _patch_sina(monkeypatch, _jsonp(rows))
    provider = TencentProvider()

    weekly = await provider.get_kline("RB0", period="1w", limit=8)
    assert len(weekly) == 8
    assert weekly[-1].close == pytest.approx(float(rows[-1]["c"]))
    assert all(bar.high >= bar.close >= bar.low for bar in weekly)
    assert all(bar.provenance is not None and bar.provenance.source == "sina" for bar in weekly)

    monthly = await provider.get_kline("RB0", period="1m", limit=3)
    assert len(monthly) == 3
    assert monthly[-1].close == pytest.approx(float(rows[-1]["c"]))
    assert all(bar.high >= bar.close >= bar.low for bar in monthly)


async def test_unknown_futures_product_returns_no_bars_instead_of_raising(monkeypatch):
    # Sina answers an unknown product with the JSONP wrapper around null.
    _patch_sina(monkeypatch, b"/*<script>location.href='//sina.com';</script>*/\n=(null);")

    assert await TencentProvider().get_kline("ZZ0", period="1d", limit=10) == []


async def test_malformed_futures_series_raises_provider_unavailable(monkeypatch):
    rows = _daily_rows(10)
    rows[-1]["h"] = "1.00"  # high below close violates the OHLC contract
    _patch_sina(monkeypatch, _jsonp(rows))

    with pytest.raises(ProviderUnavailable, match="Sina futures kline parse failed"):
        await TencentProvider().get_kline("RB0", period="1d", limit=5)


async def test_futures_fetch_failure_raises_provider_unavailable(monkeypatch):
    def failing_fetch(req, timeout, max_bytes=provider_mod.MAX_BODY_BYTES):
        raise OSError("network down")

    monkeypatch.setattr(provider_mod, "_fetch_public", failing_fetch)

    with pytest.raises(ProviderUnavailable, match="Sina futures kline fetch failed"):
        await TencentProvider().get_kline("RB0", period="1d", limit=10)


async def test_futures_adjusted_bars_are_rejected(monkeypatch):
    _patch_sina(monkeypatch, _jsonp(_daily_rows(10)))

    with pytest.raises(ProviderUnavailable, match="unadjusted"):
        await TencentProvider().get_kline("RB0", period="1d", limit=5, adjustment="qfq")
