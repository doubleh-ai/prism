"""Same-day near/far settlement spread from official contract and settlement reports.

A cross-month spread here is one dated observation: the nearest and the farthest
listed delivery months that both carry a settlement price in the same exchange
bulletin. It is not a tradable spread quote, a spot basis or an execution
instruction, and this module never estimates a missing month or price.

Only products whose complete listed-contract calendar is published by the
exchange are covered. The calendar decides which delivery months exist, the
same-day settlement report supplies the prices, and both must be collected for
the requested trading day. Everything else returns an explicit
``spread_unavailable`` reason. The rendered pages reuse ``futures`` evidence
formatting so the research ledger can register this observation as an obtained
original document and bind a report figure to the page that carries it.
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from . import exchange_futures, futures, futures_parameters, public_network
from .futures_curve_research import active_pair

SHANGHAI = ZoneInfo("Asia/Shanghai")
TOTAL_SECONDS = 15
REQUEST_SECONDS = 7
CLIENT_TIMEOUT = 8

NOT_COVERED = "contract_months_not_covered"
CALENDAR_UNAVAILABLE = "contract_months_unavailable"
SETTLEMENT_UNAVAILABLE = "settlement_unavailable"
INSUFFICIENT = "insufficient_settlement_contracts"

PAIR_SELECTION = "nearest_and_farthest_priced_listed_delivery_months"
NOTICE = (
    "价差为同一交易日、同一品种的远月结算价减近月结算价，只使用交易所公开业务参数与结算日报；"
    "不是可交易价差报价、现货基差或交易建议，缺失的合约月份或结算价保持未知，未做估算或插值。"
)
DCE_NOT_COVERED_REASON = (
    "大连商品交易所官网对自动化访问统一返回 412 反爬挑战，替代来源也未提供可核对的完整"
    "挂牌合约月份清单（新浪仅提供已命名合约的行情），未计算价差。"
)


def not_covered_reason(symbol: str) -> str:
    """Name the missing listing calendar without inventing delivery months."""
    if futures.PRODUCTS[symbol][1] == "DCE":
        return DCE_NOT_COVERED_REASON
    covered = "、".join(sorted(futures_parameters.PRODUCTS))
    return f"{symbol} 没有可核对的完整挂牌合约月份清单；仅有 {covered} 的合约日历可用，未计算价差。"


_SHAPES = {
    "contango": "远月升水（远月结算价高于近月）",
    "backwardation": "远月贴水（远月结算价低于近月）",
    "flat": "平水（两月结算价相同）",
}


def _number(value):
    """One finite float from an already validated report value, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _priced(value):
    return (number := _number(value)) is not None and number > 0


def _day(value):
    if not isinstance(value, str):
        raise ValueError("trading_day 必须为 YYYY-MM-DD")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("trading_day 必须为 YYYY-MM-DD") from None


def _identity(symbol):
    product = futures.PRODUCTS.get(symbol)
    return (product[0], product[1]) if product is not None else (None, None)


def _quote(symbol):
    return exchange_futures.PRICE_UNITS.get(symbol) or futures._QUOTE_UNITS.get(symbol)


def _report_meta(report):
    """Bounded source metadata for one report attempt, never its raw body."""
    if not isinstance(report, dict):
        return None
    return {
        "status": report.get("status"),
        "requested_date": report.get("requested_date"),
        "data_as_of": report.get("data_as_of"),
        "source_url": report.get("source_url"),
        "error_code": report.get("error_code"),
        "http_status": report.get("http_status"),
    }


def _unavailable(symbol, day, reason_code, reason, **details):
    name, exchange = _identity(symbol)
    return {
        "status": "spread_unavailable",
        "symbol": symbol,
        "name": name,
        "exchange": exchange,
        "requested_date": day.isoformat(),
        "data_as_of": None,
        "reason_code": reason_code,
        "reason": reason,
        "covered_symbols": sorted(futures_parameters.PRODUCTS),
        "quote_unit": _quote(symbol),
        "pair_selection": PAIR_SELECTION,
        "near": None,
        "far": None,
        "near_contract": None,
        "near_delivery_month": None,
        "near_settlement": None,
        "far_contract": None,
        "far_delivery_month": None,
        "far_settlement": None,
        "far_minus_near": None,
        "shape": None,
        "contract_months": details.get("contract_months"),
        "settlement_report": details.get("settlement_report"),
        "source": None,
        "collected_at": None,
        "tradable_contract": False,
        "notice": NOTICE,
    }


def _observed_months(calendar, settlements):
    """Join listed delivery months with this day's settlement prices, in order."""
    listed = {}
    for contract in calendar.get("contracts") or []:
        if not isinstance(contract, dict):
            continue
        code = contract.get("contract")
        start = contract.get("delivery_start")
        if not isinstance(code, str) or not code or not isinstance(start, str):
            continue
        listed[code.lower()] = contract
    prices = {}
    for contract in settlements.get("contracts") or []:
        if not isinstance(contract, dict):
            continue
        code = contract.get("contract")
        if isinstance(code, str) and code:
            prices[code.lower()] = contract
    months = []
    for code in sorted(listed, key=lambda item: listed[item]["delivery_start"]):
        contract = listed[code]
        quote = prices.get(code) or {}
        months.append(
            {
                "contract": code,
                "delivery_month": contract["delivery_start"][:7],
                "delivery_start": contract["delivery_start"],
                "listed_on": contract.get("listed_on"),
                "listed_expiry_date": contract.get("listed_expiry_date"),
                "settlement": _number(quote.get("settlement")),
                "volume": _number(quote.get("volume")),
                "open_interest": _number(quote.get("open_interest")),
            }
        )
    return months


def _collected(symbol, day, calendar, settlements, months, priced):
    name, exchange = _identity(symbol)
    near, far = priced[0], priced[-1]
    quote = _quote(symbol)
    near_settlement = near["settlement"]
    far_settlement = far["settlement"]
    difference = float(Decimal(str(far_settlement)) - Decimal(str(near_settlement)))
    shape = "contango" if difference > 0 else "backwardation" if difference < 0 else "flat"
    exchange_name = futures._exchange_source(exchange)
    settlement_url = settlements.get("source_url")
    calendar_url = calendar.get("source_url")
    return {
        "status": "collected",
        "symbol": symbol,
        "name": name,
        "exchange": exchange,
        "requested_date": day.isoformat(),
        "data_as_of": day.isoformat(),
        "pair_selection": PAIR_SELECTION,
        "quote_unit": quote,
        "active_pair": active_pair(priced, day),
        "near": near,
        "far": far,
        "near_contract": near["contract"],
        "near_delivery_month": near["delivery_month"],
        "near_settlement": near_settlement,
        "far_contract": far["contract"],
        "far_delivery_month": far["delivery_month"],
        "far_settlement": far_settlement,
        "far_minus_near": difference,
        "shape": shape,
        "contract_months": {
            "status": "collected",
            "data_as_of": calendar.get("data_as_of"),
            "source_updated_at": calendar.get("source_updated_at"),
            "source_url": calendar_url,
            "body_sha256": calendar.get("body_sha256"),
            "count": len(months),
            "listed_contracts": len(months),
            "priced_contracts": len(priced),
            "months": months,
            "notice": calendar.get("notice"),
        },
        "settlement_report": {
            "status": "collected",
            "data_as_of": settlements.get("data_as_of"),
            "date_provenance": settlements.get("date_provenance"),
            "source_url": settlement_url,
            "body_sha256": settlements.get("body_sha256"),
            "submitted_contracts": len(settlements.get("contracts") or []),
            "notice": settlements.get("notice"),
        },
        "source": {
            "name": "交易所公开日报",
            "url": settlement_url or calendar_url,
            "kind": "exchange_market_data",
        },
        "sources": [
            {
                "role": "contract_calendar",
                "name": "上海期货交易所业务参数查询",
                "url": calendar_url,
                "data_as_of": calendar.get("data_as_of"),
            },
            {
                "role": "settlement",
                "name": exchange_name,
                "url": settlement_url,
                "data_as_of": settlements.get("data_as_of"),
            },
        ],
        "collected_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "tradable_contract": False,
        "notice": NOTICE,
    }


async def spread(symbol, trading_day):
    """Return one dated near/far settlement spread with its source documents.

    Raises ``ValueError`` for a malformed trading day; every data gap returns a
    ``spread_unavailable`` result that names the missing step instead of a number.
    """
    day = _day(trading_day)
    if symbol not in futures.PRODUCTS:
        raise ValueError("不支持的期货品种")
    if symbol not in futures_parameters.PRODUCTS:
        return _unavailable(symbol, day, NOT_COVERED, not_covered_reason(symbol))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TOTAL_SECONDS
    async with public_network.async_client(timeout=CLIENT_TIMEOUT) as client:
        calendar = await futures_parameters.read_contract_dates(
            client,
            symbol,
            day,
            min(deadline, loop.time() + REQUEST_SECONDS),
        )
        if calendar.get("status") != "collected":
            return _unavailable(
                symbol,
                day,
                CALENDAR_UNAVAILABLE,
                "该交易日没有取得完整的挂牌合约月份清单，未计算价差。",
                contract_months=_report_meta(calendar),
            )
        settlements = await exchange_futures.read_settlements(
            client,
            symbol,
            day,
            min(deadline, loop.time() + REQUEST_SECONDS),
        )
        if settlements.get("status") != "collected":
            return _unavailable(
                symbol,
                day,
                SETTLEMENT_UNAVAILABLE,
                "该交易日没有取得同日交易所结算价，未计算价差。",
                contract_months=_report_meta(calendar),
                settlement_report=_report_meta(settlements),
            )
    months = _observed_months(calendar, settlements)
    priced = [month for month in months if _priced(month["settlement"])]
    if len(priced) < 2:
        return _unavailable(
            symbol,
            day,
            INSUFFICIENT,
            (f"该交易日只有 {len(priced)} 个挂牌合约有同日结算价，少于近月与远月两个，未计算价差。"),
            contract_months=_report_meta(calendar),
            settlement_report={
                **_report_meta(settlements),
                "listed_contracts": len(months),
                "priced_contracts": len(priced),
            },
        )
    return _collected(symbol, day, calendar, settlements, months, priced)


def _calendar_lines(observation, quote):
    lines = []
    for month in observation["contract_months"].get("months") or []:
        if not isinstance(month, dict):
            continue
        code = month.get("contract")
        delivery = month.get("delivery_month")
        if not isinstance(code, str) or not isinstance(delivery, str):
            continue
        settlement = futures._evidence_number(month.get("settlement"))
        if settlement is None:
            lines.append(f"{code.upper()} 交割月份 {delivery} 无当日结算价")
        else:
            lines.append(f"{code.upper()} 交割月份 {delivery} 结算价 {settlement} {quote}")
    return lines


def _spread_lines(observation, quote):
    near = observation["near"]
    near_settlement = futures._evidence_number(observation.get("near_settlement"))
    far_settlement = futures._evidence_number(observation.get("far_settlement"))
    difference = futures._evidence_number(observation.get("far_minus_near"))
    if near_settlement is None or far_settlement is None or difference is None:
        return []
    lines = [
        (
            f"近月合约 {observation['near_contract'].upper()} 交割月份 "
            f"{observation['near_delivery_month']} 结算价 {near_settlement} {quote}"
        ),
        (
            f"远月合约 {observation['far_contract'].upper()} 交割月份 "
            f"{observation['far_delivery_month']} 结算价 {far_settlement} {quote}"
        ),
        f"远月减近月价差 {difference} {quote}",
        f"价差方向 {_SHAPES.get(observation.get('shape'), '未知')}",
        "配对方式 最近与最远的有结算价挂牌交割月份",
    ]
    pair = observation.get("active_pair")
    if pair:
        lines.append(
            f"排除当月与次月后按当日成交量选取两份合约：{pair['near_contract'].upper()} "
            f"结算价 {pair['near_settlement']} {quote}，{pair['far_contract'].upper()} "
            f"结算价 {pair['far_settlement']} {quote}，远月减近月 {pair['far_minus_near']} {quote}"
        )
    volume = futures._evidence_number(near.get("volume"))
    open_interest = futures._evidence_number(near.get("open_interest"))
    if volume is not None:
        lines.append(f"近月合约成交量 {volume} 手")
    if open_interest is not None:
        lines.append(f"近月合约持仓量 {open_interest} 手")
    return lines


def _calendar_section(observation, quote):
    months = observation["contract_months"]
    return futures._evidence_page(
        "contract_months",
        "挂牌合约月份与同日结算价",
        "上海期货交易所业务参数查询",
        months.get("source_url"),
        months.get("data_as_of"),
        _calendar_lines(observation, quote),
    )


def _spread_section(observation, exchange, quote):
    report = observation.get("settlement_report") or {}
    return futures._evidence_page(
        "settlement_spread",
        "近月与远月结算价差",
        futures._exchange_source(exchange),
        report.get("source_url"),
        report.get("data_as_of"),
        _spread_lines(observation, quote),
    )


def spread_document(observation):
    """Render one collected spread observation as a source- and page-addressable document.

    ``None`` means the observation is incomplete or ``spread_unavailable``; the
    tool result is left untouched either way. Only a collected spread is registered
    as evidence, because a missing month must never become a number.
    """
    if not isinstance(observation, dict):
        return None
    symbol = observation.get("symbol")
    if not isinstance(symbol, str) or symbol not in futures.PRODUCTS:
        return None
    if observation.get("status") != "collected":
        return None
    source = observation.get("source")
    url = source.get("url") if isinstance(source, dict) else None
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    if not isinstance(observation.get("collected_at"), str):
        return None
    months = observation.get("contract_months")
    if not isinstance(months, dict) or not isinstance(months.get("months"), list):
        return None
    if not isinstance(observation.get("near"), dict) or not isinstance(observation.get("far"), dict):
        return None
    name, exchange = _identity(symbol)
    day = futures._evidence_day(observation.get("data_as_of"))
    quote = observation.get("quote_unit") if isinstance(observation.get("quote_unit"), str) else "元/吨"
    sections = [
        page
        for page in (
            _calendar_section(observation, quote),
            _spread_section(observation, exchange, quote),
        )
        if page is not None
    ]
    observed = [page["data_period"] for page in sections if page.get("data_period")]
    data_period = day or (max(observed) if observed else None)
    if not sections or data_period is None:
        return None
    pages = []
    for index, page in enumerate(sections, start=1):
        header = (
            f"数据来源：{page['source']}｜数据日期：{futures._chinese_day(page['data_period'])}｜"
            f"品种：{name} {symbol}（{exchange}）"
        )
        pages.append(
            {
                "page": index,
                "text": "\n".join([header, f"【{page['label']}】", *page["lines"], NOTICE]),
            }
        )
    return {
        "symbol": symbol,
        "name": name,
        "exchange": exchange,
        "title": f"{name} {symbol} 跨期价差快照 {data_period}",
        "url": sections[0]["url"] or url,
        "publisher": "、".join(dict.fromkeys(page["source"] for page in sections)),
        "retrieved_at": observation["collected_at"],
        "data_period": data_period,
        "status": "collected",
        "tradable_contract": False,
        "source": {"name": source.get("name"), "url": url, "kind": source.get("kind")},
        "sections": [
            {
                "page": index,
                "section": page["section"],
                "label": page["label"],
                "source": page["source"],
                "url": page["url"],
                "data_period": page["data_period"],
                "symbol": symbol,
                "name": name,
            }
            for index, page in enumerate(sections, start=1)
        ],
        "pages": pages,
    }
