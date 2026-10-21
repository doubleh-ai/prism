"""Pydantic models for the indicator API."""

from typing import Any

from pydantic import BaseModel, Field


class TranslateRequest(BaseModel):
    input: str = Field(..., min_length=1, max_length=200)


class IndicatorCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    description: str = Field(default="", max_length=300)
    dsl: dict
    kind: str = Field(..., pattern="^(plot|signal)$")
    params: dict[str, Any] = Field(default_factory=dict)
    plan: dict[str, Any] = Field(default_factory=dict)
    visual: dict[str, Any] = Field(default_factory=dict)
    source: str = Field(default="", max_length=40)


class IndicatorUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=300)
    dsl: dict | None = None
    kind: str | None = Field(default=None, pattern="^(plot|signal)$")
    params: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    visual: dict[str, Any] | None = None
    source: str | None = Field(default=None, max_length=40)


class EvalRequest(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=32)
    period: str = Field(default="1d", max_length=8)
    kline: list[dict[str, Any]] | None = Field(default=None, max_length=500)
    params: dict[str, Any] = Field(default_factory=dict)


class ScanRequest(BaseModel):
    scope: str = Field(default="watchlist", pattern="^(watchlist|market)$")
    tickers: list[str] = Field(default_factory=list, max_length=200)
    ticker_names: dict[str, str] = Field(default_factory=dict)
    period: str = Field(default="1d", pattern="^(1d|1w|1m)$")
    lookback: int = Field(default=120, ge=30, le=500)
    trigger_window: int = Field(default=5, ge=1, le=30)
    params: dict[str, Any] = Field(default_factory=dict)
    markets: list[str] = Field(default_factory=lambda: ["SH", "SZ", "BJ"], max_length=3)
    boards: list[str] = Field(default_factory=lambda: ["main", "gem", "star", "bse"], max_length=4)
    exclude_st: bool = True
    min_turnover: float = Field(default=0, ge=0, le=100000000000)
    max_universe: int = Field(default=0, ge=0, le=6000)
    max_results: int = Field(default=100, ge=10, le=300)
    sort: str = Field(default="strength", pattern="^(strength|recent_signal|latest_value)$")
    direction: str = Field(default="desc", pattern="^(asc|desc)$")
