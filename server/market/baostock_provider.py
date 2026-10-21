"""baostock market data provider.

baostock speaks a raw TCP protocol to its own domestic data server
(``baostock.com``) — directly reachable, no eastmoney CDN involved.
This makes it the most reliable provider from a CN-hosted box whose
route to eastmoney's push-CDN is blocked.

CRITICAL — proxy env stripping
------------------------------
The systemd ``proxy.conf`` drop-in injects ``http_proxy``/``https_proxy``
into the service environment (needed for the Claude / DeepSeek API
calls). baostock's socket layer honors those env vars and then hangs,
because an HTTP proxy cannot tunnel baostock's non-HTTP protocol.
Every blocking call here is wrapped in :func:`_run_blocking`, which
temporarily removes the proxy env for the duration of the call so
baostock connects directly.

All upstream calls are also capped with :func:`asyncio.wait_for` using
``BAOSTOCK_TIMEOUT`` so a stuck remote can't tie up workers.
"""

import asyncio
import datetime
import logging
import os
import time

from ..config import BAOSTOCK_TIMEOUT
from .clock import market_timestamp
from .models import Fundamentals, KLine, Quote, SearchHit
from .provider import ProviderNotFound, ProviderUnavailable

log = logging.getLogger("stock-app")

try:
    import baostock as bs

    _BS_AVAILABLE = True
except ImportError:
    _BS_AVAILABLE = False


# Env vars that tell socket-based clients to use an HTTP proxy.
# baostock must NOT go through the proxy, so we strip these per-call.
_PROXY_ENV_KEYS = (
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "all_proxy",
    "ALL_PROXY",
)


def _bs_or_raise() -> None:
    if not _BS_AVAILABLE:
        raise ProviderUnavailable("baostock not installed (pip install -r requirements-extra.txt)")


def _run_blocking(func, *args, **kwargs):
    """Run ``func`` with proxy env vars temporarily removed.

    Restores the original env in a ``finally`` so the Claude-API path
    (which DOES need the proxy) keeps working. The clear window is the
    single blocking call only.
    """
    saved = {k: os.environ.pop(k) for k in _PROXY_ENV_KEYS if k in os.environ}
    try:
        return func(*args, **kwargs)
    finally:
        os.environ.update(saved)


# baostock period strings: "d"/"w"/"m"
_PERIOD_MAP = {"1d": "d", "1w": "w", "1m": "m"}


class BaostockProvider:
    name = "baostock"

    def __init__(self):
        # baostock keeps a single global connection + result-set handle and is
        # NOT safe under concurrent access: overlapping query/drain calls
        # overwrite each other's global state, so one call returns another's
        # (wrong) rows. Every baostock op must run under this one serial lock.
        self._bs_lock = asyncio.Lock()
        self._logged_in = False

    # ── low-level call helper: clear proxy + enforce timeout ──
    async def _call(self, func, *args, **kwargs):
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_run_blocking, func, *args, **kwargs),
                timeout=BAOSTOCK_TIMEOUT,
            )
        except TimeoutError as e:
            raise ProviderUnavailable(f"baostock timeout after {BAOSTOCK_TIMEOUT}s") from e

    async def _ensure_login(self):
        """Login once per process; baostock sessions persist.

        Caller MUST hold ``self._bs_lock`` — login touches the same global
        connection as queries, so it must run serialized too.
        """
        if self._logged_in:
            return
        lg = await self._call(bs.login)
        if getattr(lg, "error_code", "0") != "0":
            raise ProviderUnavailable(f"baostock login failed: {getattr(lg, 'error_msg', '?')}")
        self._logged_in = True

    async def _query(self, code, fields, start, frequency, adjustflag) -> list:
        """Login → query → drain, all under the serial lock.

        This is the single entry point for baostock reads; holding the lock
        across query *and* drain is what prevents one coroutine's
        ``query_history_k_data_plus`` from clobbering another's in-flight
        result set.
        """
        async with self._bs_lock:
            await self._ensure_login()
            try:
                rs = await self._call(
                    bs.query_history_k_data_plus,
                    code,
                    fields,
                    start_date=start,
                    end_date="2099-12-31",
                    frequency=frequency,
                    adjustflag=adjustflag,
                )
            except ProviderUnavailable:
                # Session may have died — reset so the next call re-logs in.
                self._logged_in = False
                raise
            return await self._call(self._drain, rs)

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
