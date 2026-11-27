"""Dated CZCE daily settlements from the exchange's fixed public text file.

Only the fixed daily text report is fetched. The file lists product codes and
prices, not Chinese product names; the names below are the exchange's official
product names confirmed against the same-date warehouse-receipt and holding
reports. Member positions are not read by this adapter.
"""

from __future__ import annotations

import re
from datetime import date

import httpx

from .data_availability import source_failure
from .exchange_futures import (
    LIMIT,
    _daily_value,
    _fetch,
    _grouped_number,
    _unavailable_daily,
)

BASE = "https://www.czce.com.cn"
PRODUCTS = {
    "MA": "甲醇",
    "CF": "棉花",
    "SR": "白糖",
    "FG": "玻璃",
    "TA": "精对苯二甲酸",
    "RM": "菜籽粕",
    "OI": "菜籽油",
    "PF": "涤纶短纤",
    "AP": "苹果",
    "CJ": "红枣",
    "PK": "花生",
    "SA": "纯碱",
    "SH": "烧碱",
    "UR": "尿素",
}
PRICE_UNITS = {
    "MA": "元/吨",
    "CF": "元/吨",
    "SR": "元/吨",
    "FG": "元/吨",
    "TA": "元/吨",
    "RM": "元/吨",
    "OI": "元/吨",
    "PF": "元/吨",
    "AP": "元/吨",
    "CJ": "元/吨",
    "PK": "元/吨",
    "SA": "元/吨",
    "SH": "元/吨（折百）",
    "UR": "元/吨",
}
TITLE = "郑州商品交易所期货每日行情表"
HEADER = [
    "合约代码",
    "昨结算",
    "今开盘",
    "最高价",
    "最低价",
    "今收盘",
    "今结算",
    "涨跌1",
    "涨跌2",
    "成交量(手)",
    "持仓量",
    "增减量",
    "成交额(万元)",
    "交割结算价",
]
NOTICE = (
    "同日交易所期货每日行情表中的具体合约收盘价与结算价；小计与总计行不计入合约，"
    "价格为零表示当日无成交。远月减近月是价差，不是现货基差；会员持仓排名另表披露，"
    "本适配器不读取，也不据此推断资金流向。"
)


def report_url(day: date):
    return f"{BASE}/cn/DFSStaticFiles/Future/{day:%Y}/{day:%Y%m%d}/FutureDataDaily.txt"


def _decode(raw: bytes):
    if not raw or len(raw) > LIMIT:
        raise ValueError("CZCE report size")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("CZCE report encoding") from None


def _month(code: str, symbol: str, day: date):
    match = re.fullmatch(re.escape(symbol) + r"(\d{3,4})", code)
    if not match:
        raise ValueError("invalid CZCE contract")
    digits = match.group(1)
    if len(digits) == 4:
        year, number = 2000 + int(digits[:2]), int(digits[2:])
    else:
        # CZCE prints a three digit year/month code (e.g. MA612 is 2026-12).
        year = (day.year // 10) * 10 + int(digits[0])
        if year < day.year - 1:
            year += 10
        number = int(digits[1:])
    if not 1 <= number <= 12 or not day.year - 1 <= year <= day.year + 5:
        raise ValueError("invalid CZCE contract month")
    return year, number


def _price(cell: str):
    if cell == "":
        return None
    number = _grouped_number(cell)
    return None if number == 0 else number


def parse_daily(raw: bytes, symbol: str, day: date):
    if symbol not in PRODUCTS:
        raise ValueError("unsupported CZCE product")
    lines = _decode(raw).splitlines()
    if len(lines) < 3 or lines[0].strip() != f"{TITLE}({day:%Y-%m-%d})":
        raise ValueError("CZCE report date")
    if [cell.strip() for cell in lines[1].split("|")] != HEADER:
        raise ValueError("CZCE report header")
    contracts, seen = [], set()
    for line in lines[2:]:
        if not line.strip():
            continue
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) != len(HEADER):
            raise ValueError("CZCE report row")
        code = cells[0]
        if code in {"", "小计", "总计"}:
            continue
        code_prefix = re.match(r"[A-Z]{1,2}", code)
        if code_prefix is None or code_prefix.group(0) != symbol:
            # Keep unrelated product rows from failing this product's report.
            continue
        year, number = _month(code, symbol, day)
        if code in seen:
            raise ValueError("duplicate CZCE contract")
        seen.add(code)
        contracts.append(
            {
                "contract": symbol.lower() + code[len(symbol) :],
                "delivery_month": f"{year:04d}-{number:02d}",
                "close": _price(cells[5]),
                "settlement": _price(cells[6]),
                "volume": _grouped_number(cells[9], integer=True),
                "open_interest": _grouped_number(cells[10], integer=True),
            }
        )
    if not contracts or len(contracts) > 60:
        raise ValueError("missing CZCE contracts")
    return _daily_value(
        contracts,
        day=day,
        source_url=report_url(day),
        raw=raw,
        quote_unit=PRICE_UNITS[symbol],
        notice=NOTICE,
        date_provenance="reported_date",
    )


async def read_daily(client, symbol, day, deadline):
    url = report_url(day)
    errors = (httpx.HTTPError, ValueError, TypeError, KeyError, OverflowError, TimeoutError, RecursionError)
    try:
        raw = await _fetch(client, url, deadline)
        return parse_daily(raw, symbol, day)
    except errors as error:
        return _unavailable_daily(day, url, source_failure(error))
