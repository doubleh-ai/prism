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


class KLine(BaseModel):
    """Single OHLCV bar."""

    timestamp: int = Field(..., gt=0)
    open: float
    high: float
    low: float
    close: float
    volume: float = Field(..., ge=0)
    turnover: float | None = Field(default=None, ge=0)


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
    stale: bool = False
    source: str = Field(..., min_length=1, max_length=32)


class SearchHit(BaseModel):
    """Single ticker search result."""

    ticker: str = Field(..., min_length=1, max_length=32)
    name: str = Field(..., min_length=1, max_length=128)
    market: str | None = Field(default=None, max_length=8)  # e.g. "SH", "SZ", "HK"
