"""Pydantic models for the indicator API."""
from typing import List, Optional
from pydantic import BaseModel, Field


class TranslateRequest(BaseModel):
    input: str = Field(..., min_length=1, max_length=200)


class IndicatorCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    description: str = Field(default="", max_length=300)
    dsl: dict
    kind: str = Field(..., pattern="^(plot|signal)$")


class IndicatorUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=64)
    description: Optional[str] = Field(default=None, max_length=300)
    dsl: Optional[dict] = None
    kind: Optional[str] = Field(default=None, pattern="^(plot|signal)$")


class EvalRequest(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=32)
    period: str = Field(default="1d", max_length=8)


class ScanRequest(BaseModel):
    scope: str = Field(default="watchlist", pattern="^(watchlist|market)$")
    tickers: List[str] = Field(default_factory=list, max_length=50)
