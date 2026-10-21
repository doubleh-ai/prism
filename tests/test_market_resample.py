"""K-line aggregation shared by daily-only market data providers."""

import datetime as dt

from server.market.models import KLine
from server.market.resample import daily_fetch_limit, resample_kline

TZ = dt.timezone(dt.timedelta(hours=8))


def _bar(day, open_, high, low, close, volume, turnover=None):
    timestamp = int(dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ).timestamp())
    return KLine(
        timestamp=timestamp,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        turnover=turnover,
    )


def _daily_bars():
    return [
        _bar("2024-02-05", 13, 15, 12, 14, 400, 4000),
        _bar("2024-01-30", 11, 13, 10, 12, 200),
        _bar("2024-02-02", 12, 14, 11, 13, 300, 3000),
        _bar("2024-01-29", 10, 12, 9, 11, 100, 1000),
    ]


def test_weekly_resample_uses_first_open_last_close_and_sums_volume():
    bars = resample_kline(_daily_bars(), "1w", 10)

    assert len(bars) == 2
    assert (bars[0].open, bars[0].high, bars[0].low, bars[0].close) == (10, 14, 9, 13)
    assert bars[0].volume == 600
    assert bars[0].turnover == 4000
    assert bars[0].timestamp == _daily_bars()[2].timestamp
    assert (bars[1].open, bars[1].close, bars[1].volume) == (13, 14, 400)


def test_monthly_resample_groups_by_market_calendar_month():
    bars = resample_kline(_daily_bars(), "1m", 10)

    assert len(bars) == 2
    assert (bars[0].open, bars[0].high, bars[0].low, bars[0].close) == (10, 13, 9, 12)
    assert bars[0].volume == 300
    assert (bars[1].open, bars[1].high, bars[1].low, bars[1].close) == (12, 15, 11, 14)
    assert bars[1].volume == 700


def test_resample_sorts_and_trims_after_aggregation():
    bars = resample_kline(_daily_bars(), "1w", 1)
    assert len(bars) == 1
    assert bars[0].close == 14

    daily = resample_kline(_daily_bars(), "1d", 2)
    assert [bar.close for bar in daily] == [13, 14]


def test_daily_fetch_limit_expands_source_history_for_period_bars():
    assert daily_fetch_limit("1d", 20) == 20
    assert daily_fetch_limit("1w", 20) == 120
    assert daily_fetch_limit("1m", 20) == 480
    assert daily_fetch_limit("1w", 2) == 60
    assert daily_fetch_limit("1m", 2) == 250
