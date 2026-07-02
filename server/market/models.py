"""Pydantic schemas for market data.

All schemas are strictly typed so the router can serialize directly
without ad-hoc dict manipulation. Optional fields default to ``None``
to accommodate partial responses from real-world data providers.
"""
from typing import Optional
from pydantic import BaseModel, Field


class Quote(BaseModel):
    """Real-time quote for a single ticker."""
    ticker: str = Field(..., min_length=1, max_length=32)
    name: Optional[str] = Field(default=None, max_length=128)
    price: float = Field(..., ge=0)
    change: float = 0.0  # absolute change vs prev close
    change_pct: float = 0.0  # percentage change
    volume: float = Field(default=0.0, ge=0)
    turnover: float = Field(default=0.0, ge=0)
    high: Optional[float] = Field(default=None, ge=0)
    low: Optional[float] = Field(default=None, ge=0)
    open: Optional[float] = Field(default=None, ge=0)
    prev_close: Optional[float] = Field(default=None, ge=0)
    timestamp: int = Field(..., gt=0)  # unix seconds
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
    turnover: Optional[float] = Field(default=None, ge=0)


class Fundamentals(BaseModel):
    """Fundamental indicators for a single ticker."""
    ticker: str = Field(..., min_length=1, max_length=32)
    name: Optional[str] = Field(default=None, max_length=128)
    pe_ttm: Optional[float] = None
    pe_static: Optional[float] = None
    pb: Optional[float] = None
    ps_ttm: Optional[float] = None
    roe: Optional[float] = None
    revenue: Optional[float] = None
    net_profit: Optional[float] = None
    market_cap: Optional[float] = None
    timestamp: int = Field(..., gt=0)
    source: str = Field(..., min_length=1, max_length=32)


class SearchHit(BaseModel):
    """Single ticker search result."""
    ticker: str = Field(..., min_length=1, max_length=32)
    name: str = Field(..., min_length=1, max_length=128)
    market: Optional[str] = Field(default=None, max_length=8)  # e.g. "SH", "SZ", "HK"
