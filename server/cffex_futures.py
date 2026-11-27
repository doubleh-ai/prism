"""Dated CFFEX daily settlements from the exchange's fixed GBK CSV file.

Only the fixed daily CSV report is fetched. The report has no Chinese product
name column; the names below are the exchange's official product names from its
public navigation. Options are ignored; only dated futures contracts form the
settlement curve. Member positions are not read by this adapter.
"""

from __future__ import annotations

import re
from datetime import date

import httpx

from .data_availability import source_failure
from .exchange_futures import (
    LIMIT,
    ReportNotFound,
    _daily_value,
    _fetch,
    _grouped_number,
    _unavailable_daily,
)

BASE = "http://www.cffex.com.cn"
PRODUCTS = {
    "IF": "沪深300股指期货",
    "IC": "中证500股指期货",
    "T": "10年期国债期货",
    "TF": "5年期国债期货",
    "TS": "2年期国债期货",
}
PRICE_UNITS = {"IF": "点", "IC": "点", "T": "元", "TF": "元", "TS": "元"}
MISSING_PAGE = "/error_page/error_404.html"
HEADER = [
    "合约代码",
    "今开盘",
    "最高价",
    "最低价",
    "成交量",
    "成交金额",
    "持仓量",
    "持仓变化",
    "今收盘",
    "今结算",
    "前结算",
    "涨跌1",
    "涨跌2",
    "Delta",
]
NOTICE = (
    "同日交易所日行情 CSV 的具体合约收盘价与结算价；小计、合计与期权行不计入期货曲线。"
    "报价单位按交易所列示：股指期货为指数点，国债期货为百元面值报价。"
    "远月减近月是价差，不是现货基差；会员持仓排名本适配器不读取。"
)


def report_url(day: date):
    return f"{BASE}/fzjy/mrhq/{day:%Y%m}/{day:%d}/{day:%Y%m%d}_1.csv"


def _decode(raw: bytes):
    if not raw or len(raw) > LIMIT:
        raise ValueError("CFFEX report size")
    try:
        return raw.decode("gbk")
    except UnicodeDecodeError:
        raise ValueError("CFFEX report encoding") from None


def _price(cell: str):
    if cell in {"", "--"}:
        return None
    number = _grouped_number(cell)
    return None if number == 0 else number


def parse_daily(raw: bytes, symbol: str, day: date):
    if symbol not in PRODUCTS:
        raise ValueError("unsupported CFFEX product")
    lines = _decode(raw).splitlines()
    if len(lines) < 2 or [cell.strip() for cell in lines[0].split(",")] != HEADER:
        raise ValueError("CFFEX report header")
    contracts, seen = [], set()
    for line in lines[1:]:
        if not line.strip():
            continue
        cells = [cell.strip() for cell in line.split(",")]
        if len(cells) != len(HEADER):
            raise ValueError("CFFEX report row")
        match = re.fullmatch(r"([A-Z]{1,2})(\d{4})", cells[0])
        if match is None or match.group(1) != symbol:
            # Options, subtotal/total rows and other products are not contracts.
            continue
        year, number = 2000 + int(match.group(2)[:2]), int(match.group(2)[2:])
        if not 1 <= number <= 12 or not day.year - 1 <= year <= day.year + 5:
            raise ValueError("invalid CFFEX contract month")
        if cells[0] in seen:
            raise ValueError("duplicate CFFEX contract")
        seen.add(cells[0])
        contracts.append(
            {
                "contract": symbol.lower() + match.group(2),
                "delivery_month": f"{year:04d}-{number:02d}",
                "close": _price(cells[8]),
                "settlement": _price(cells[9]),
                "volume": _grouped_number(cells[4], integer=True),
                "open_interest": _grouped_number(cells[6], integer=True),
            }
        )
    if not contracts or len(contracts) > 60:
        raise ValueError("missing CFFEX contracts")
    return _daily_value(
        contracts,
        day=day,
        source_url=report_url(day),
        raw=raw,
        quote_unit=PRICE_UNITS[symbol],
        notice=NOTICE,
        date_provenance="request_parameter",
    )


async def read_daily(client, symbol, day, deadline):
    url = report_url(day)
    errors = (httpx.HTTPError, ValueError, TypeError, KeyError, OverflowError, TimeoutError, RecursionError)
    try:
        raw = await _fetch(client, url, deadline, redirect_not_found=MISSING_PAGE)
        return parse_daily(raw, symbol, day)
    except ReportNotFound:
        return _unavailable_daily(day, url, {"error_code": "report_not_found", "http_status": 302})
    except errors as error:
        return _unavailable_daily(day, url, source_failure(error))
