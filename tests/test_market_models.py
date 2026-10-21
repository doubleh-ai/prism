"""Pydantic model validation for market data layer."""

import pytest
from pydantic import ValidationError

from server.market.models import Fundamentals, KLine, Quote, SearchHit


def test_quote_minimal_required_fields():
    q = Quote(ticker="600519", price=1680.5, timestamp=1718700000, source="akshare")
    assert q.ticker == "600519"
    assert q.price == 1680.5
    assert q.change == 0.0
    assert q.stale is False
    assert q.source == "akshare"


def test_quote_rejects_negative_price():
    with pytest.raises(ValidationError):
        Quote(ticker="X", price=-1, timestamp=1, source="akshare")


def test_quote_rejects_missing_required():
    with pytest.raises(ValidationError):
        Quote(ticker="X")  # missing price/timestamp/source


def test_quote_stale_flag_default_false():
    q = Quote(ticker="X", price=1, timestamp=1, source="akshare")
    assert q.stale is False


def test_kline_required_ohlcv():
    k = KLine(timestamp=1, open=10.0, high=11.0, low=9.0, close=10.5, volume=1000.0)
    assert k.high >= k.low
    assert k.turnover is None


def test_kline_rejects_negative_volume():
    with pytest.raises(ValidationError):
        KLine(timestamp=1, open=10, high=11, low=9, close=10, volume=-1)


def test_fundamentals_all_optional_except_ticker_timestamp_source():
    f = Fundamentals(ticker="600519", timestamp=1, source="akshare")
    assert f.pe_ttm is None
    assert f.pb is None
    assert f.name is None


def test_search_hit_minimal():
    h = SearchHit(ticker="600519", name="贵州茅台")
    assert h.market is None
    assert h.ticker == "600519"
