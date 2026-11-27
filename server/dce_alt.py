"""Read DCE warrant inventory from the public 99期货 (fx168) series.

The DCE official site answers automated requests with a site-wide HTTP 412
JavaScript challenge, so DCE products cannot read the official warrant report.
The 99期货 aggregator instead publishes one dated warrant-quantity series per
variety, requested with a short-lived ``_pcc`` token that the site returns in
response headers. Each row is one disclosed trading day; a missing day is never
filled from another date.

Quantities are kept in lots (手) exactly as published: the source's tonne
metadata label is wrong for these series, so values are never converted to
tonnes. The source does not publish a same-day change, so the change is the
difference to the previous disclosed row and the result says so explicitly.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import date, timedelta
from urllib.parse import urlencode

import httpx

from .data_availability import source_failure

BASE = "https://centerapi.fx168api.com"
TOKEN_URL = BASE + "/app/common/v.js"
SERIES_URL = BASE + "/app/qh/api/stock/trend"
SITE = "https://www.99qh.com"
SOURCE_NAME = "99期货"
SOURCE_KIND = "secondary_warehouse_series"
UNIT = "手"
# 99期货 product id and the printed product name for each DCE symbol.
PRODUCTS = {
    "M": ("63", "豆粕"),
    "C": ("59", "玉米"),
    "I": ("76", "铁矿石"),
    "P": ("65", "棕榈油"),
}
NOTICE = (
    "本页为 99期货公开仓单序列快照；数量单位按来源披露保持为手，未换算吨数，"
    "不代表社会库存或全部库存；缺少的交易日不补零、不用邻近日期替代。"
    "来源未单列当日增减，增减为与上一披露日的差。"
)
PREVIOUS_DAYS = 60
WINDOW_PAGE_SIZE = 120
FULL_PAGE_SIZE = 5000
STREAM_LIMIT = 512 * 1024
TOKEN_SECONDS = 300
TOKEN_MARGIN = 30
_TOKEN: dict[str, object] = {"value": "", "expires": 0.0}


class _AccessDenied(Exception):
    """The source answered with its anti-automation challenge."""


def _source_url(symbol: str, day: date) -> str:
    product = PRODUCTS[symbol][0]
    start = (day - timedelta(days=PREVIOUS_DAYS)).isoformat()
    query = urlencode(
        {
            "productId": product,
            "type": "1",
            "pageNo": "1",
            "appCategory": "web",
            "startDate": start,
            "endDate": day.isoformat(),
        }
    )
    return f"{SERIES_URL}?{query}"


def _headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "Referer": SITE + "/",
        "Origin": SITE,
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "identity",
    }
    if token:
        headers["_pcc"] = token
        headers["site-channel"] = "003"
    return headers


async def _read_body(response: httpx.Response) -> bytes:
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise ValueError("source encoding")
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > STREAM_LIMIT:
            raise ValueError("source size")
        body.extend(chunk)
    return bytes(body)


async def _token(client: httpx.AsyncClient, deadline: float) -> str:
    """Return a cached ``_pcc`` token, refreshing it shortly before expiry."""
    now = time.time()
    expires = float(_TOKEN["expires"])
    if isinstance(_TOKEN["value"], str) and _TOKEN["value"] and expires > now + TOKEN_MARGIN:
        return str(_TOKEN["value"])
    if deadline <= asyncio.get_running_loop().time():
        raise TimeoutError
    async with asyncio.timeout_at(deadline):
        async with client.stream("GET", TOKEN_URL, headers=_headers(), follow_redirects=False) as response:
            if response.status_code == 412:
                raise _AccessDenied
            response.raise_for_status()
            value = str(response.headers.get("_pcc", "")).strip()
            expire = str(response.headers.get("_pcc_expire", "")).strip()
    if not 20 <= len(value) <= 1024 or any(character.isspace() for character in value):
        raise ValueError("source token")
    try:
        expiry = int(expire) / 1000
    except ValueError:
        expiry = now + TOKEN_SECONDS
    _TOKEN.update(value=value, expires=min(max(expiry, now), now + 3600))
    return value


def _document(body: bytes) -> dict:
    try:
        document = json.loads(body)
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError("source payload") from error
    if not isinstance(document, dict):
        raise ValueError("source payload")
    return document


def _rows(document: dict) -> list[tuple[date, int]]:
    if document.get("code") != 0:
        raise ValueError("source rejection")
    data = document.get("data")
    if not isinstance(data, dict):
        raise ValueError("source series")
    rows = data.get("list")
    total = data.get("total")
    if not isinstance(rows, list) or not isinstance(total, int) or total < len(rows):
        raise ValueError("source series")
    if not rows and total == 0:
        return []
    parsed = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 3:
            raise ValueError("source row")
        stamp, quantity = row[0], row[2]
        if not isinstance(stamp, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", stamp) is None:
            raise ValueError("source row date")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
            raise ValueError("source row quantity")
        parsed.append((date.fromisoformat(stamp), quantity))
    return parsed


async def _fetch_series(
    client: httpx.AsyncClient, symbol: str, day: date, deadline: float
) -> tuple[list[tuple[date, int]], str]:
    """Read the disclosed rows on or before ``day`` and the URL that served them."""
    product = PRODUCTS[symbol][0]
    token = await _token(client, deadline)
    params = {
        "productId": product,
        "type": "1",
        "pageNo": "1",
        "pageSize": str(WINDOW_PAGE_SIZE),
        "startDate": (day - timedelta(days=PREVIOUS_DAYS)).isoformat(),
        "endDate": day.isoformat(),
        "appCategory": "web",
    }
    url = f"{SERIES_URL}?{urlencode(params)}"
    document = await _request(client, url, deadline, token)
    if document.get("code") == 100010:
        # The site keeps only three years of dated windows. A full series is
        # still available without a date bound and stays a bounded response.
        params.update(startDate="", endDate="", pageSize=str(FULL_PAGE_SIZE))
        url = f"{SERIES_URL}?{urlencode(params)}"
        document = await _request(client, url, deadline, token)
    return _rows(document), url


async def _request(client: httpx.AsyncClient, url: str, deadline: float, token: str) -> dict:
    if deadline <= asyncio.get_running_loop().time():
        raise TimeoutError
    async with asyncio.timeout_at(deadline):
        async with client.stream("GET", url, headers=_headers(token), follow_redirects=False) as response:
            if response.status_code == 412:
                raise _AccessDenied
            response.raise_for_status()
            body = await _read_body(response)
    return _document(body)


def _failure(day: date, url: str, failure: dict) -> dict:
    return {
        "status": "unavailable",
        "requested_date": day.isoformat(),
        "source_url": url,
        **failure,
    }


def _no_report(day: date, url: str) -> dict:
    return _failure(day, url, {"error_code": "report_not_found"})


async def read_inventory(client: httpx.AsyncClient, symbol: str, day: date, deadline: float) -> dict:
    """Read one dated warrant observation for a DCE product from 99期货."""
    if symbol not in PRODUCTS:
        return {"status": "not_covered"}
    url = _source_url(symbol, day)
    if deadline <= asyncio.get_running_loop().time():
        return _failure(day, url, source_failure(TimeoutError()))
    try:
        rows, url = await _fetch_series(client, symbol, day, deadline)
    except _AccessDenied:
        _TOKEN.update(value="", expires=0.0)
        try:
            rows, url = await _fetch_series(client, symbol, day, deadline)
        except _AccessDenied:
            return _failure(day, url, {"error_code": "source_access_denied", "http_status": 412})
        except (ValueError, httpx.HTTPError, TimeoutError) as error:
            return _failure(day, url, source_failure(error))
    except (ValueError, httpx.HTTPError, TimeoutError) as error:
        return _failure(day, url, source_failure(error))
    current = next((row for row in rows if row[0] == day), None)
    if current is None:
        return _no_report(day, url)
    earlier = [row for row in rows if row[0] < day]
    previous = max(earlier, key=lambda row: row[0]) if earlier else None
    return {
        "status": "collected",
        "data_as_of": day.isoformat(),
        "source_name": SOURCE_NAME,
        "source_url": url,
        "source_kind": SOURCE_KIND,
        "table": PRODUCTS[symbol][1],
        "row": "仓单数量",
        "label": "仓单库存（99期货公开序列）",
        "unit": UNIT,
        "quantity": current[1],
        "change": current[1] - previous[1] if previous is not None else None,
        "change_basis": "delta_vs_previous_disclosed_row" if previous is not None else None,
        "basis": "公开序列披露值",
        "segment": "仓单库存（99期货）",
        "date_provenance": "reported_date",
        "notice": NOTICE,
    }


async def read_report(client: httpx.AsyncClient, symbol: str, day: date, deadline: float) -> dict:
    """Snapshot-path wrapper: one dated DCE warrant report for the evidence page."""
    return await read_inventory(client, symbol, day, deadline)
