"""Shared K-line period aggregation for providers that expose daily bars."""
from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

from .models import KLine


_MARKET_TZ = dt.timezone(dt.timedelta(hours=8), name="Asia/Shanghai")


def daily_fetch_limit(period: str, limit: int) -> int:
    """Return enough daily source bars to build ``limit`` period bars."""
    limit = max(1, int(limit or 1))
    if period == "1w":
        return min(5000, max(60, limit * 6))
    if period == "1m":
        return min(5000, max(250, limit * 24))
    return limit


def resample_kline(bars: Iterable[KLine], period: str, limit: int) -> list[KLine]:
    """Aggregate sorted daily/native bars into genuine weekly or monthly bars."""
    ordered = sorted(bars, key=lambda item: item.timestamp)
    limit = max(1, int(limit or 1))
    if period not in {"1w", "1m"}:
        return ordered[-limit:]

    grouped: list[list[KLine]] = []
    current_key = None
    for bar in ordered:
        market_date = dt.datetime.fromtimestamp(bar.timestamp, tz=_MARKET_TZ).date()
        if period == "1w":
            iso = market_date.isocalendar()
            key = (iso.year, iso.week)
        else:
            key = (market_date.year, market_date.month)
        if key != current_key:
            grouped.append([])
            current_key = key
        grouped[-1].append(bar)

    result: list[KLine] = []
    for group in grouped:
        first, last = group[0], group[-1]
        turnovers = [bar.turnover for bar in group if bar.turnover is not None]
        result.append(KLine(
            timestamp=last.timestamp,
            open=first.open,
            high=max(bar.high for bar in group),
            low=min(bar.low for bar in group),
            close=last.close,
            volume=sum(bar.volume for bar in group),
            turnover=sum(turnovers) if turnovers else None,
        ))
    return result[-limit:]
