"""akshare market data provider.

Uses Sina Finance (``stock_zh_a_daily``) for quotes and K-line
because eastmoney CDN nodes are blocked on some production networks.
Sina provides daily bars with open/high/low/close/volume/amount.

All upstream calls are wrapped with :func:`asyncio.wait_for` using
``AKSHARE_TIMEOUT`` from config so a hung remote service can't tie up
FastAPI workers indefinitely.
"""
import asyncio
import math
import time
import logging
from typing import List
from .provider import ProviderUnavailable, ProviderNotFound
from .models import Quote, KLine, Fundamentals, SearchHit
from ..config import AKSHARE_TIMEOUT

log = logging.getLogger("stock-app")

try:
    import akshare as ak
    _AKSHARE_AVAILABLE = True
except ImportError:
    _AKSHARE_AVAILABLE = False


def _akshare_or_raise() -> None:
    if not _AKSHARE_AVAILABLE:
        raise ProviderUnavailable("akshare not installed (pip install -r requirements-extra.txt)")


async def _with_timeout(coro):
    """Run ``coro`` (a to_thread call) under AKSHARE_TIMEOUT. TimeoutError → ProviderUnavailable."""
    try:
        return await asyncio.wait_for(coro, timeout=AKSHARE_TIMEOUT)
    except asyncio.TimeoutError as e:
        raise ProviderUnavailable(f"akshare timeout after {AKSHARE_TIMEOUT}s") from e


# ── Stock list cache ──
# Loading the full A-share stock list (~5500 rows) via akshare takes
# 300-800 ms.  Cache it in memory for 30 minutes so repeated search
# keystrokes don't each trigger a new upstream fetch.
_stock_list_cache_data = None
_stock_list_cache_ts = 0.0
_STOCK_LIST_CACHE_TTL = 1800  # 30 minutes


def _stock_list_cache():
    """Return the cached stock list DataFrame, refreshing if stale."""
    global _stock_list_cache_data, _stock_list_cache_ts
    now = time.time()
    if _stock_list_cache_data is not None and (now - _stock_list_cache_ts) < _STOCK_LIST_CACHE_TTL:
        return _stock_list_cache_data
    try:
        _stock_list_cache_data = ak.stock_info_a_code_name()
        _stock_list_cache_ts = now
    except Exception:
        # If the upstream call fails, return whatever we had (even if stale).
        # Better to show slightly outdated results than none at all.
        pass
    return _stock_list_cache_data


def _sina_symbol(ticker: str) -> str:
    """Convert '600519' → 'sh600519', '000001' → 'sz000001'."""
    if "." in ticker:
        return ticker.lower()
    if ticker.startswith("6"):
        return "sh" + ticker
    return "sz" + ticker


class AkshareProvider:
    """akshare-backed market data provider. Wraps blocking calls in to_thread."""

    name = "akshare"

    async def get_quote(self, ticker: str) -> Quote:
        """Derive a pseudo-quote from the two most recent daily bars via Sina."""
        _akshare_or_raise()
        symbol = _sina_symbol(ticker)
        try:
            df = await _with_timeout(asyncio.to_thread(
                ak.stock_zh_a_daily, symbol=symbol, adjust="qfq",
            ))
        except ProviderUnavailable:
            raise
        except Exception as e:
            raise ProviderUnavailable(f"akshare stock_zh_a_daily failed: {e}") from e
        if df is None or df.empty:
            raise ProviderNotFound(f"akshare: ticker not found: {ticker}")
        if len(df) < 1:
            raise ProviderNotFound(f"akshare: ticker not found: {ticker}")

        def _f(v, default=None):
            try:
                if v is None or (isinstance(v, float) and math.isnan(v)):
                    return default
                return float(v)
            except Exception:
                return default

        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else latest
        price = _f(latest.get("close"), 0.0) or 0.0
        prev_close = _f(prev.get("close"), 0.0) or 0.0
        change = price - prev_close
        change_pct = (change / prev_close * 100.0) if prev_close else 0.0

        return Quote(
            ticker=ticker,
            name=ticker,
            price=price,
            change=change,
            change_pct=change_pct,
            volume=_f(latest.get("volume"), 0.0) or 0.0,
            turnover=_f(latest.get("amount"), 0.0) or 0.0,
            high=_f(latest.get("high")),
            low=_f(latest.get("low")),
            open=_f(latest.get("open")),
            prev_close=prev_close,
            timestamp=int(time.time()),
            source=self.name,
        )

    async def get_kline(
        self, ticker: str, period: str = "1d", limit: int = 120
    ) -> List[KLine]:
        _akshare_or_raise()
        symbol = _sina_symbol(ticker)
        try:
            df = await _with_timeout(asyncio.to_thread(
                ak.stock_zh_a_daily, symbol=symbol, adjust="qfq",
            ))
        except ProviderUnavailable:
            raise
        except Exception as e:
            raise ProviderUnavailable(f"akshare stock_zh_a_daily failed: {e}") from e
        if df is None or df.empty:
            return []
        out: List[KLine] = []
        for _, r in df.tail(limit).iterrows():
            try:
                out.append(KLine(
                    timestamp=int(_pd_timestamp(r["date"])),
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                    volume=float(r["volume"]),
                    turnover=float(r.get("amount", 0)) if r.get("amount") is not None else None,
                ))
            except Exception:
                continue
        return out

    async def get_fundamentals(self, ticker: str) -> Fundamentals:
        _akshare_or_raise()
        raise ProviderUnavailable("akshare fundamentals not available (eastmoney CDN blocked)")

    async def search(self, query: str, limit: int = 10) -> List[SearchHit]:
        _akshare_or_raise()
        df = _stock_list_cache()
        if df is None or df.empty:
            return []
        q = query.strip().lower()
        hits: List[SearchHit] = []
        for _, r in df.iterrows():
            code = str(r.get("code", ""))
            name = str(r.get("name", ""))
            if q in code.lower() or q in name.lower():
                hits.append(SearchHit(
                    ticker=code, name=name,
                    market=("SH" if code.startswith("6") else "SZ" if code.startswith(("0", "3")) else None),
                ))
                if len(hits) >= limit:
                    break
        return hits

    async def health(self) -> bool:
        return _AKSHARE_AVAILABLE


def _pd_timestamp(s):
    """Convert '2024-01-15' style string to unix seconds."""
    import datetime
    if isinstance(s, (int, float)):
        return int(s)
    return int(datetime.datetime.strptime(str(s), "%Y-%m-%d").timestamp())