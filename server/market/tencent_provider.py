"""Tencent + Sina direct-HTTP market data provider (from TradingAgents-astock).

Uses Tencent qt.gtimg.cn for real-time quotes (batch, sub-100ms) and
Sina money.finance.sina.com.cn for daily K-line. Both endpoints are
confirmed reachable in the prod network (no akshare/baostock dependency).
"""

import asyncio
import json
import logging
import re
import time
import urllib.parse
import urllib.request

from .clock import market_timestamp
from .models import Fundamentals, KLine, Quote, SearchHit
from .provider import ProviderNotFound, ProviderUnavailable
from .resample import daily_fetch_limit, resample_kline

log = logging.getLogger("stock-app")

_TENCENT_URL = "https://qt.gtimg.cn/q="
_SINA_KLINE_URL = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
)


def _sina_symbol(ticker: str) -> str:
    """'600519' → 'sh600519', '002050' → 'sz002050'."""
    if "." in ticker:
        return ticker.lower()
    if ticker.startswith("6"):
        return "sh" + ticker
    if ticker.startswith(("0", "3")):
        return "sz" + ticker
    if ticker.startswith(("4", "8", "92")):
        return "bj" + ticker
    return "sz" + ticker


def _tencent_prefix(ticker: str) -> str:
    """'002050' → 'sz002050', '600519' → 'sh600519'."""
    if ticker.startswith("6"):
        return "sh" + ticker
    if ticker.startswith(("4", "8", "92")):
        return "bj" + ticker
    return "sz" + ticker


def _decode_gbk(raw: bytes) -> str:
    """Tencent returns GBK-encoded data. Decode to UTF-8 str."""
    try:
        return raw.decode("gbk")
    except Exception:
        return raw.decode("utf-8", errors="replace")


def _fetch_tencent_quotes(codes: list[str], timeout: float = 8) -> dict:
    """Batch-fetch real-time quotes from Tencent. Returns {ticker: fields_list}."""
    if not codes:
        return {}
    prefixed = [_tencent_prefix(c) for c in codes]
    url = _TENCENT_URL + ",".join(prefixed)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = _decode_gbk(resp.read())
    except Exception as e:
        raise ProviderUnavailable(f"Tencent quote fetch failed: {e}") from e

    result = {}
    for line in body.strip().split("\n"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        # v_sz002050="51~三花智控~002050~..."
        m = re.search(r'v_(\w+)="(.*)"', line)
        if not m:
            continue
        code_prefixed = m.group(1)
        fields = m.group(2).split("~")
        ticker = fields[2] if len(fields) > 2 else code_prefixed
        result[ticker] = fields
    return result


class TencentProvider:
    """Tencent + Sina direct-HTTP market data provider."""

    name = "tencent"

    async def get_quote(self, ticker: str) -> Quote:
        try:
            quotes = await asyncio.to_thread(_fetch_tencent_quotes, [ticker], 8)
        except ProviderUnavailable:
            raise
        if ticker not in quotes:
            raise ProviderNotFound(f"tencent: no quote for {ticker}")
        f = quotes[ticker]
        try:
            price = float(f[3]) if f[3] else 0.0
            prev_close = float(f[4]) if f[4] else 0.0
            change = price - prev_close
            change_pct = (change / prev_close * 100.0) if prev_close else 0.0
            retrieved_at = int(time.time())
            data_timestamp = market_timestamp(f[30], "%Y%m%d%H%M%S") if len(f) > 30 else None
            return Quote(
                ticker=ticker,
                name=f[1] if len(f) > 1 else ticker,
                price=price,
                change=change,
                change_pct=change_pct,
                volume=float(f[6]) if len(f) > 6 and f[6] else 0.0,
                turnover=float(f[37]) if len(f) > 37 and f[37] else 0.0,
                high=float(f[33]) if len(f) > 33 and f[33] else None,
                low=float(f[34]) if len(f) > 34 and f[34] else None,
                open=float(f[5]) if len(f) > 5 and f[5] else None,
                prev_close=prev_close,
                timestamp=data_timestamp or retrieved_at,
                data_timestamp=data_timestamp,
                retrieved_at=retrieved_at,
                timestamp_kind="trade" if data_timestamp else "retrieved",
                data_kind="quote",
                source=self.name,
            )
        except (ValueError, IndexError) as e:
            raise ProviderUnavailable(f"tencent: bad quote data: {e}") from e

    async def get_kline(self, ticker: str, period: str = "1d", limit: int = 120) -> list[KLine]:
        if period not in {"1d", "1w", "1m"}:
            raise ProviderUnavailable(f"tencent: unsupported K-line period {period}")
        symbol = _sina_symbol(ticker)
        source_limit = daily_fetch_limit(period, limit)
        # Sina exposes daily bars here. Weekly/monthly requests fetch enough
        # source history and are aggregated after normalization.
        url = f"{_SINA_KLINE_URL}?symbol={urllib.parse.quote(symbol)}&scale=240&ma=no&datalen={source_limit}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            # Run sync urllib in a worker thread so the event loop is free.
            raw = await asyncio.to_thread(self._fetch_kline_sync, req, 12)
        except Exception as e:
            raise ProviderUnavailable(f"Sina kline fetch failed: {e}") from e
        try:
            bars = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ProviderUnavailable(f"Sina kline bad JSON: {e}") from e
        if not bars or not isinstance(bars, list):
            return []
        out: list[KLine] = []
        for b in bars:
            try:
                ts = market_timestamp(b["day"])
                if ts is None:
                    continue
                out.append(
                    KLine(
                        timestamp=ts,
                        open=float(b["open"]),
                        high=float(b["high"]),
                        low=float(b["low"]),
                        close=float(b["close"]),
                        volume=float(b["volume"]),
                    )
                )
            except (KeyError, ValueError):
                continue
        return resample_kline(out, period, limit)

    async def get_fundamentals(self, ticker: str) -> Fundamentals:
        raise ProviderUnavailable("tencent: fundamentals not available via Sina")

    async def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        return []  # fall through to akshare for search

    async def health(self) -> bool:
        return True

    @staticmethod
    def _fetch_kline_sync(req, timeout: int) -> str:
        """Sync helper for asyncio.to_thread — reads the response body."""
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
