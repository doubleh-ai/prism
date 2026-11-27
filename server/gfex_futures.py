"""Dated GFEX daily settlements from the exchange's fixed public POST endpoint.

Only the fixed daily quote endpoint is fetched, with a bounded form body. The
exchange's site protection answers clients without a browser-like User-Agent
with an HTML challenge instead of the JSON report; this adapter never executes
or solves that challenge and reports it as unavailable. Chinese product names
are read from the response itself and cross-checked against the code.
"""

from __future__ import annotations

import json
import re
from datetime import date

import httpx

from .data_availability import source_failure
from .exchange_futures import (
    LIMIT,
    _daily_value,
    _fetch,
    _number,
    _unavailable_daily,
)

BASE = "http://www.gfex.com.cn"
URL = f"{BASE}/u/interfacesWebTiDayQuotes/loadList"
PRODUCTS = {"SI": "工业硅", "LC": "碳酸锂", "PS": "多晶硅"}
PRICE_UNITS = {"SI": "元/吨", "LC": "元/吨", "PS": "元/吨"}
HEADERS = {
    "User-Agent": "prism-futures/1.0 (+public daily reports)",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}
CHALLENGE_LIMIT = 64 * 1024
CHALLENGE_MARKERS = (b"EO_Bot_Ssid", b"<script>function a(")
NOTICE = (
    "同日交易所日行情接口返回的具体合约收盘价与结算价；小计与总计行不计入合约。"
    "远月减近月是价差，不是现货基差；会员持仓排名本适配器不读取。"
)


class _NoContracts(Exception):
    """The dated response only carries subtotal rows (a non-trading day)."""


def report_url(day: date):
    return URL


def report_body(day: date):
    return f"trade_date={day:%Y%m%d}&trade_type=0".encode("ascii")


def _price(value):
    if value is None or value == "":
        return None
    number = _number(value)
    return None if number == 0 else number


def _requested_date(raw_param, day: date):
    requested = raw_param.get("trade_date") if isinstance(raw_param, dict) else None
    if isinstance(requested, str):
        requested = [requested]
    return isinstance(requested, list) and requested == [day.strftime("%Y%m%d")]


def parse_daily(raw: bytes, symbol: str, day: date):
    if symbol not in PRODUCTS:
        raise ValueError("unsupported GFEX product")
    if not raw or len(raw) > LIMIT:
        raise ValueError("GFEX report size")
    try:
        value = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise ValueError("GFEX report encoding") from None
    if not isinstance(value, dict) or value.get("code") not in (0, "0"):
        raise ValueError("GFEX report status")
    if not _requested_date(value.get("param"), day):
        raise ValueError("GFEX report date")
    rows = value.get("data")
    if not isinstance(rows, list) or len(rows) > 20000 or any(not isinstance(row, dict) for row in rows):
        raise ValueError("GFEX report schema")
    if not any(str(row.get("delivMonth", "")).strip() for row in rows):
        raise _NoContracts()
    contracts, seen = [], set()
    for row in rows:
        month = str(row.get("delivMonth", "")).strip()
        if not month:
            continue
        if str(row.get("varietyOrder", "")).strip().lower() != symbol.lower():
            continue
        if not re.fullmatch(r"\d{4}", month):
            raise ValueError("invalid GFEX contract month")
        year, number = 2000 + int(month[:2]), int(month[2:])
        if not 1 <= number <= 12 or not day.year - 1 <= year <= day.year + 5:
            raise ValueError("invalid GFEX contract month")
        if month in seen:
            raise ValueError("duplicate GFEX contract")
        if row.get("variety") != PRODUCTS[symbol]:
            raise ValueError("GFEX product mismatch")
        seen.add(month)
        contracts.append(
            {
                "contract": symbol.lower() + month,
                "delivery_month": f"{year:04d}-{number:02d}",
                "close": _price(row.get("close")),
                "settlement": _price(row.get("clearPrice")),
                "volume": _number(row.get("volumn"), integer=True),
                "open_interest": _number(row.get("openInterest"), integer=True),
            }
        )
    if not contracts or len(contracts) > 60:
        raise ValueError("missing GFEX contracts")
    return _daily_value(
        contracts,
        day=day,
        source_url=URL,
        raw=raw,
        quote_unit=PRICE_UNITS[symbol],
        notice=NOTICE,
        source_method="POST",
        date_provenance="request_parameter",
    )


def _challenge(raw: bytes):
    prefix = raw[:CHALLENGE_LIMIT]
    return any(marker in prefix for marker in CHALLENGE_MARKERS)


async def read_daily(client, symbol, day, deadline):
    url = report_url(day)
    errors = (httpx.HTTPError, ValueError, TypeError, KeyError, OverflowError, TimeoutError, RecursionError)
    try:
        raw = await _fetch(client, url, deadline, method="POST", content=report_body(day), headers=HEADERS)
    except errors as error:
        return _unavailable_daily(day, url, source_failure(error))
    if _challenge(raw):
        return _unavailable_daily(
            day,
            url,
            {
                "error_code": "automated_access_restricted",
                "http_status": 200,
                "blocked_by": "anti_automation_challenge",
            },
        )
    try:
        return parse_daily(raw, symbol, day)
    except _NoContracts:
        return _unavailable_daily(day, url, {"error_code": "report_not_found", "http_status": 200})
    except errors as error:
        return _unavailable_daily(day, url, source_failure(error))
