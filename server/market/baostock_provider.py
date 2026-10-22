"""Baostock daily market data over the SDK's process-global TCP connection.

Baostock 0.9.2 uses ``socket.socket`` directly and does not read HTTP proxy
variables. Do not mutate process-wide proxy configuration around SDK calls.
One real worker owns login, query and result pagination under the shared
thread lock. The timeout limits the caller's wait, not the worker's lifetime;
the lifecycle manager retains its original future until the SDK returns.
"""

import asyncio
import datetime
import logging
import threading
import time

from ..config import BAOSTOCK_TIMEOUT
from ..work_lifecycle import get_manager
from .clock import market_timestamp
from .models import Fundamentals, KLine, Quote, SearchHit
from .provider import ProviderNotFound, ProviderUnavailable

log = logging.getLogger("stock-app")

try:
    import baostock as bs

    _BS_AVAILABLE = True
except ImportError:
    _BS_AVAILABLE = False


# All SDK clients, including the scanner, must use this transaction boundary.
# Async locks cannot protect a global socket after a cancelled/timed-out await.
_SDK_LOCK = threading.Lock()
_SESSION_GENERATION = 0
_LOGIN_SDK = None
_LOGIN_GENERATION = None


def _bs_or_raise() -> None:
    if not _BS_AVAILABLE:
        raise ProviderUnavailable("baostock not installed (pip install -r requirements-extra.txt)")


def run_serial_transaction(function, *args, **kwargs):
    """Own the SDK until the synchronous transaction actually returns.

    Every transaction advances the generation, even on failure. Callers such
    as the scanner may log out or replace the global socket; their completion
    must invalidate a provider's previously verified login.
    """
    global _SESSION_GENERATION
    with _SDK_LOCK:
        try:
            return function(*args, **kwargs)
        finally:
            _SESSION_GENERATION += 1


# baostock period strings: "d"/"w"/"m"
_PERIOD_MAP = {"1d": "d", "1w": "w", "1m": "m"}


class BaostockProvider:
    name = "baostock"

    async def _call(self, function, *args, **kwargs):
        try:
            return await asyncio.wait_for(
                get_manager().run_blocking("market", run_serial_transaction, function, *args, **kwargs),
                timeout=BAOSTOCK_TIMEOUT,
            )
        except TimeoutError as e:
            raise ProviderUnavailable(f"baostock timeout after {BAOSTOCK_TIMEOUT}s") from e

    @staticmethod
    def _query_sync(code, fields, start, frequency, adjustflag) -> list:
        """One locked worker owns login, query and all result-set pagination."""
        global _LOGIN_SDK, _LOGIN_GENERATION
        try:
            if _LOGIN_SDK is not bs or _LOGIN_GENERATION != _SESSION_GENERATION:
                lg = bs.login()
                if getattr(lg, "error_code", "0") != "0":
                    raise ProviderUnavailable(f"baostock login failed: {getattr(lg, 'error_msg', '?')}")
            rs = bs.query_history_k_data_plus(
                code,
                fields,
                start_date=start,
                end_date="2099-12-31",
                frequency=frequency,
                adjustflag=adjustflag,
            )
            rows = BaostockProvider._drain(rs)
        except BaseException:
            _LOGIN_SDK = _LOGIN_GENERATION = None
            raise
        # run_serial_transaction advances the generation in its finally. A
        # complete provider transaction keeps this verified session reusable;
        # any intervening scanner transaction invalidates the generation.
        _LOGIN_SDK, _LOGIN_GENERATION = bs, _SESSION_GENERATION + 1
        return rows

    async def _query(self, code, fields, start, frequency, adjustflag) -> list:
        return await self._call(self._query_sync, code, fields, start, frequency, adjustflag)

    @staticmethod
    def _prefix(code: str) -> str:
        """Convert '600519' → 'sh.600519', '000001' → 'sz.000001'."""
        if "." in code:
            return code
        if code.startswith("6"):
            return f"sh.{code}"
        return f"sz.{code}"

    async def get_quote(self, ticker: str) -> Quote:
        _bs_or_raise()
        code = self._prefix(ticker)
        start = (datetime.date.today() - datetime.timedelta(days=14)).strftime("%Y-%m-%d")
        rows = await self._query(
            code,
            "date,code,open,high,low,close,preclose,volume,amount,pctChg,turn",
            start,
            "d",
            "3",
        )
        if not rows:
            raise ProviderNotFound(f"baostock: no data for {ticker}")

        r = rows[-1]  # latest trading day

        def _f(v, default=None):
            try:
                if v in (None, "", ""):
                    return default
                return float(v)
            except (TypeError, ValueError):
                return default

        close = _f(r.get("close"), 0.0) or 0.0
        preclose = _f(r.get("preclose"))
        pct = _f(r.get("pctChg"))
        # Quote.change / Quote.change_pct are non-Optional floats (default
        # 0.0). baostock rows sometimes lack preclose/pctChg (empty session,
        # first quote of a ticker) — coerce None → 0.0 instead of building an
        # invalid Quote that raises ValidationError and kills the chain.
        change = (close - preclose) if preclose is not None else 0.0
        change_pct = pct if pct is not None else 0.0
        ts = self._date_to_ts(r.get("date"))

        return Quote(
            ticker=ticker,
            name="",  # baostock K-line has no name field; chip shows code
            price=close,
            change=change,
            change_pct=change_pct,
            volume=_f(r.get("volume"), 0.0) or 0.0,
            turnover=_f(r.get("amount")),
            high=_f(r.get("high")),
            low=_f(r.get("low")),
            open=_f(r.get("open")),
            prev_close=preclose,
            timestamp=ts or int(time.time()),
            data_timestamp=ts or None,
            retrieved_at=int(time.time()),
            timestamp_kind="trade" if ts else "retrieved",
            data_kind="daily_close",
            source=self.name,
        )

    async def get_kline(self, ticker: str, period: str = "1d", limit: int = 120) -> list[KLine]:
        _bs_or_raise()
        if period not in _PERIOD_MAP:
            raise ProviderUnavailable(f"baostock: unsupported K-line period {period}")
        bs_period = _PERIOD_MAP[period]
        code = self._prefix(ticker)
        calendar_days_per_bar = {"1d": 2, "1w": 8, "1m": 35}[period]
        start = (
            datetime.date.today()
            - datetime.timedelta(
                days=limit * calendar_days_per_bar + 45,
            )
        ).strftime("%Y-%m-%d")
        rows = await self._query(
            code,
            "date,open,high,low,close,volume,amount",
            start,
            bs_period,
            "2",  # forward-adjusted (qfq)
        )
        out: list[KLine] = []
        for r in rows[-limit:]:
            try:
                out.append(
                    KLine(
                        timestamp=self._date_to_ts(r.get("date")),
                        open=float(r["open"]),
                        high=float(r["high"]),
                        low=float(r["low"]),
                        close=float(r["close"]),
                        volume=float(r.get("volume") or 0),
                        turnover=float(r.get("amount") or 0) or None,
                    )
                )
            except (TypeError, ValueError, KeyError):
                continue
        return out

    async def get_fundamentals(self, ticker: str) -> Fundamentals:
        _bs_or_raise()
        code = self._prefix(ticker)
        start = (datetime.date.today() - datetime.timedelta(days=14)).strftime("%Y-%m-%d")
        rows = await self._query(
            code,
            "date,close,pctChg,turn,peTTM,pbMRQ",
            start,
            "d",
            "3",
        )
        if not rows:
            raise ProviderNotFound(f"baostock: no fundamentals for {ticker}")
        r = rows[-1]

        def _f(v):
            try:
                if v in (None, "", ""):
                    return None
                return float(v)
            except (TypeError, ValueError):
                return None

        return Fundamentals(
            ticker=ticker,
            name="",
            pe_ttm=_f(r.get("peTTM")),
            pb=_f(r.get("pbMRQ")),
            market_cap=None,  # baostock K-line has no market cap
            timestamp=self._date_to_ts(r.get("date")) or int(time.time()),
            source=self.name,
        )

    async def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        # baostock has no fuzzy name search over the wire; the chain falls
        # through to akshare (whose search endpoint is on a reachable host).
        return []

    async def health(self) -> bool:
        return _BS_AVAILABLE

    # ── helpers ──
    @staticmethod
    def _drain(rs) -> list:
        """Collect every row from a baostock result set into a list of dicts."""
        out = []
        fields = rs.fields if hasattr(rs, "fields") else None
        while (rs.error_code == "0") and rs.next():
            row = rs.get_row_data()
            if fields and len(row) == len(fields):
                out.append(dict(zip(fields, row, strict=False)))
            else:
                out.append(row)
        return out

    @staticmethod
    def _date_to_ts(s) -> int:
        """Convert the source's Shanghai market date to Unix seconds."""
        return market_timestamp(s) or 0
