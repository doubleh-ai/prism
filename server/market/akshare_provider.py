"""akshare market data provider.

Uses Sina Finance (``stock_zh_a_daily``) for quotes and K-line
because eastmoney CDN nodes are blocked on some production networks.
Sina provides daily bars with open/high/low/close/volume/amount.

All upstream calls are wrapped with :func:`asyncio.wait_for` using
``AKSHARE_TIMEOUT`` from config so a hung remote service can't tie up
FastAPI workers indefinitely.
"""

import asyncio
import logging
import math
import re
import time

from ..config import AKSHARE_TIMEOUT
from ..work_lifecycle import get_manager
from .clock import market_timestamp
from .models import Fundamentals, KLine, Quote, SearchHit
from .provider import ProviderNotFound, ProviderUnavailable
from .resample import daily_fetch_limit, resample_kline

log = logging.getLogger("stock-app")

try:
    import akshare as ak

    _AKSHARE_AVAILABLE = True
except ImportError:
    _AKSHARE_AVAILABLE = False

try:
    from pypinyin import lazy_pinyin
except Exception:  # pragma: no cover - optional local enhancement
    lazy_pinyin = None


def _akshare_or_raise() -> None:
    if not _AKSHARE_AVAILABLE:
        raise ProviderUnavailable("akshare not installed (pip install -r requirements-extra.txt)")


async def _with_timeout(coro):
    """Run ``coro`` (a to_thread call) under AKSHARE_TIMEOUT. TimeoutError → ProviderUnavailable."""
    try:
        return await asyncio.wait_for(coro, timeout=AKSHARE_TIMEOUT)
    except TimeoutError as e:
        raise ProviderUnavailable(f"akshare timeout after {AKSHARE_TIMEOUT}s") from e


# ── Stock list cache ──
# Loading the full A-share stock list (~5500 rows) via akshare takes
# 300-800 ms.  Cache it in memory for 30 minutes so repeated search
# keystrokes don't each trigger a new upstream fetch.
_stock_list_cache_data = None
_stock_list_cache_ts = 0.0
_STOCK_LIST_CACHE_TTL = 1800  # 30 minutes


_GBK_INITIALS = [
    (-20319, "a"),
    (-20283, "b"),
    (-19775, "c"),
    (-19218, "d"),
    (-18710, "e"),
    (-18526, "f"),
    (-18239, "g"),
    (-17922, "h"),
    (-17417, "j"),
    (-16474, "k"),
    (-16212, "l"),
    (-15640, "m"),
    (-15165, "n"),
    (-14922, "o"),
    (-14914, "p"),
    (-14630, "q"),
    (-14149, "r"),
    (-14090, "s"),
    (-13318, "t"),
    (-12838, "w"),
    (-12556, "x"),
    (-11847, "y"),
    (-11055, "z"),
]


def _compact_search_text(value: str) -> str:
    return re.sub(r"[\s._\-]+", "", str(value or "").strip().lower())


def _canonical_code(value: str) -> str:
    text = _compact_search_text(value)
    text = re.sub(r"^(sh|sz|bj)", "", text)
    text = re.sub(r"(sh|sz|bj)$", "", text)
    return text


def _cn_initial(ch: str) -> str:
    if not ch:
        return ""
    if ch.isascii():
        return ch.lower() if ch.isalnum() else ""
    try:
        encoded = ch.encode("gbk")
    except UnicodeEncodeError:
        return ""
    if len(encoded) != 2:
        return ""
    code = encoded[0] * 256 + encoded[1] - 65536
    initial = ""
    for boundary, letter in _GBK_INITIALS:
        if code >= boundary:
            initial = letter
        else:
            break
    return initial


def _name_initials(name: str) -> str:
    return "".join(_cn_initial(ch) for ch in str(name or ""))


def _name_pinyin(name: str) -> str:
    if lazy_pinyin is None:
        return ""
    try:
        return "".join(lazy_pinyin(str(name or ""), errors="ignore")).lower()
    except Exception:
        return ""


def _search_rank(code: str, name: str, query: str) -> tuple[int, int, str]:
    q = _compact_search_text(query)
    q_code = _canonical_code(query)
    code_text = _canonical_code(code)
    name_text = _compact_search_text(name)
    initials = _name_initials(name)
    pinyin = _name_pinyin(name)

    if q_code and q_code == code_text:
        return (0, 0, code_text)
    if q_code and code_text.startswith(q_code):
        return (1, 0, code_text)
    if q_code and q_code in code_text:
        return (2, code_text.find(q_code), code_text)
    if q and q in name_text:
        return (3, name_text.find(q), code_text)
    if q and initials.startswith(q):
        return (4, 0, code_text)
    if q and pinyin.startswith(q):
        return (5, 0, code_text)
    if q and q in initials:
        return (6, initials.find(q), code_text)
    if q and pinyin and q in pinyin:
        return (7, pinyin.find(q), code_text)
    return (99, 9999, code_text)


def _stock_list_cache():
    """Return the cached stock list DataFrame, refreshing if stale."""
    global _stock_list_cache_data, _stock_list_cache_ts
    now = time.time()
    if _stock_list_cache_data is not None and (now - _stock_list_cache_ts) < _STOCK_LIST_CACHE_TTL:
        return _stock_list_cache_data
    try:
        base = ak.stock_info_a_code_name()
        try:
            import pandas as pd

            bj = ak.stock_info_bj_name_code().rename(columns={"证券代码": "code", "证券简称": "name"})
            bj = bj[["code", "name"]]
            base = pd.concat([base, bj], ignore_index=True).drop_duplicates(subset=["code"], keep="last")
        except Exception as exc:
            log.warning("Beijing stock list unavailable: %s", exc)
        _stock_list_cache_data = base
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
    if ticker.startswith(("4", "8", "92")):
        return "bj" + ticker
    return "sz" + ticker


class AkshareProvider:
    """akshare-backed market data provider. Wraps blocking calls in to_thread."""

    name = "akshare"

    async def get_quote(self, ticker: str) -> Quote:
        """Derive a pseudo-quote from the two most recent daily bars via Sina."""
        _akshare_or_raise()
        symbol = _sina_symbol(ticker)
        try:
            df = await _with_timeout(
                get_manager().run_blocking(
                    "market",
                    ak.stock_zh_a_daily,
                    symbol=symbol,
                    adjust="qfq",
                )
            )
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
        data_timestamp = market_timestamp(str(latest.get("date", "")))
        retrieved_at = int(time.time())

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
            timestamp=data_timestamp or retrieved_at,
            data_timestamp=data_timestamp,
            retrieved_at=retrieved_at,
            timestamp_kind="trade" if data_timestamp else "retrieved",
            data_kind="daily_close",
            source=self.name,
        )

    async def get_kline(self, ticker: str, period: str = "1d", limit: int = 120) -> list[KLine]:
        if period not in {"1d", "1w", "1m"}:
            raise ProviderUnavailable(f"akshare: unsupported K-line period {period}")
        _akshare_or_raise()
        symbol = _sina_symbol(ticker)
        try:
            df = await _with_timeout(
                get_manager().run_blocking(
                    "market",
                    ak.stock_zh_a_daily,
                    symbol=symbol,
                    adjust="qfq",
                )
            )
        except ProviderUnavailable:
            raise
        except Exception as e:
            raise ProviderUnavailable(f"akshare stock_zh_a_daily failed: {e}") from e
        if df is None or df.empty:
            return []
        out: list[KLine] = []
        for _, r in df.tail(daily_fetch_limit(period, limit)).iterrows():
            try:
                out.append(
                    KLine(
                        timestamp=int(_pd_timestamp(r["date"])),
                        open=float(r["open"]),
                        high=float(r["high"]),
                        low=float(r["low"]),
                        close=float(r["close"]),
                        volume=float(r["volume"]),
                        turnover=float(r.get("amount", 0)) if r.get("amount") is not None else None,
                    )
                )
            except Exception:
                continue
        return resample_kline(out, period, limit)

    async def get_fundamentals(self, ticker: str) -> Fundamentals:
        _akshare_or_raise()
        raise ProviderUnavailable("akshare fundamentals not available (eastmoney CDN blocked)")

    async def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        _akshare_or_raise()
        df = _stock_list_cache()
        if df is None or df.empty:
            return []
        ranked: list[tuple[tuple[int, int, str], SearchHit]] = []
        for _, r in df.iterrows():
            code = str(r.get("code", ""))
            name = str(r.get("name", ""))
            rank = _search_rank(code, name, query)
            if rank[0] < 99:
                ranked.append(
                    (
                        rank,
                        SearchHit(
                            ticker=code,
                            name=name,
                            market=(
                                "SH"
                                if code.startswith("6")
                                else "BJ"
                                if code.startswith(("4", "8", "92"))
                                else "SZ"
                                if code.startswith(("0", "3"))
                                else None
                            ),
                        ),
                    )
                )
        ranked.sort(key=lambda item: item[0])
        return [hit for _, hit in ranked[:limit]]

    async def health(self) -> bool:
        return _AKSHARE_AVAILABLE


def _pd_timestamp(s):
    """Convert '2024-01-15' style string to unix seconds."""
    from .clock import market_timestamp

    if isinstance(s, (int, float)):
        return int(s)
    value = market_timestamp(str(s))
    if value is None:
        raise ValueError("invalid market date")
    return value
