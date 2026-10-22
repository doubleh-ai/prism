"""Pydantic schemas for market data.

All schemas are strictly typed so the router can serialize directly
without ad-hoc dict manipulation. Optional fields default to ``None``
to accommodate partial responses from real-world data providers.
"""

from typing import Literal

from pydantic import BaseModel, Field


class Quote(BaseModel):
    """Real-time quote for a single ticker."""

    ticker: str = Field(..., min_length=1, max_length=32)
    name: str | None = Field(default=None, max_length=128)
    price: float = Field(..., ge=0)
    change: float = 0.0  # absolute change vs prev close
    change_pct: float = 0.0  # percentage change
    volume: float = Field(default=0.0, ge=0)
    turnover: float = Field(default=0.0, ge=0)
    high: float | None = Field(default=None, ge=0)
    low: float | None = Field(default=None, ge=0)
    open: float | None = Field(default=None, ge=0)
    prev_close: float | None = Field(default=None, ge=0)
    timestamp: int = Field(..., gt=0)  # unix seconds
    data_timestamp: int | None = Field(default=None, gt=0)
    retrieved_at: int | None = Field(default=None, gt=0)
    timestamp_kind: Literal["trade", "retrieved", "unknown"] = "unknown"
    data_kind: Literal["quote", "daily_close", "unknown"] = "unknown"
    source: str = Field(..., min_length=1, max_length=32)
    stale: bool = False  # True when served from expired cache after provider failure


class KLineProvenance(BaseModel):
    """Observed acquisition metadata; an adapter is not an original source."""

    source: Literal["sina", "unknown"] = "unknown"
    source_url: str | None = Field(default=None, max_length=2048)
    adapter: str | None = Field(default=None, max_length=32)
    retrieved_at: int | None = Field(default=None, gt=0)


class KLine(BaseModel):
    """Single OHLCV bar."""

    timestamp: int = Field(..., gt=0)
    open: float
    high: float
    low: float
    close: float
    volume: float = Field(..., ge=0)
    turnover: float | None = Field(default=None, ge=0)
    provenance: KLineProvenance | None = None


def describe_kline(bars: list[KLine], *, stale: bool) -> dict:
    """Summarize recorded facts without guessing origin, age or completeness.

    A bar timestamp is its last observed data date, including aggregated periods.
    It does not establish a close, a completed week/month, or acquisition time.
    Legacy/mixed cache entries retain unknown metadata rather than being relabeled.
    """
    provenance = [bar.provenance for bar in bars]
    complete = bool(provenance) and all(item is not None for item in provenance)

    def shared(field):
        if not complete:
            return None
        first = getattr(provenance[0], field)
        return first if all(getattr(item, field) == first for item in provenance) else None

    source = shared("source") or "unknown"
    return {
        "source": source,
        "source_url": shared("source_url") if source != "unknown" else None,
        "adapter": shared("adapter"),
        "retrieved_at": shared("retrieved_at"),
        "data_timestamp": max((bar.timestamp for bar in bars), default=None),
        "timestamp_kind": "bar_date",
        "data_kind": "kline",
        "period_completeness": "unknown",
        "stale": stale,
    }


class Fundamentals(BaseModel):
    """Fundamental indicators for a single ticker."""

    ticker: str = Field(..., min_length=1, max_length=32)
    name: str | None = Field(default=None, max_length=128)
    pe_ttm: float | None = None
    pe_static: float | None = None
    pb: float | None = None
    ps_ttm: float | None = None
    roe: float | None = None
    revenue: float | None = None
    net_profit: float | None = None
    market_cap: float | None = None
    timestamp: int = Field(..., gt=0)
    data_timestamp: int | None = Field(default=None, gt=0)
    retrieved_at: int | None = Field(default=None, gt=0)
    # Providers often mix current valuation and financial-statement figures.
    # Absence remains unknown; retrieval time never becomes a reporting period.
    reporting_period: str | None = None
    currency: str | None = None
    monetary_unit: str | None = None
    accounting_scope: str | None = None
    original_source_url: str | None = None
    stale: bool = False
    source: str = Field(..., min_length=1, max_length=32)


class SearchHit(BaseModel):
    """Single ticker search result."""

    ticker: str = Field(..., min_length=1, max_length=32)
    name: str = Field(..., min_length=1, max_length=128)
    market: str | None = Field(default=None, max_length=8)  # e.g. "SH", "SZ", "HK"
