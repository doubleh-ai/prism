"""Bounded public daily futures observations and evidence-led research inputs.

Continuous series are not tradable contracts. Open interest is not net capital
inflow. Missing inventory, basis or positioning is never filled by inference.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, HTTPException, Request

from . import exchange_futures, futures_fundamentals, futures_parameters, warehouse_receipts
from .auth import require_auth
from .data_availability import source_failure
from .database import check_rate_limit

router = APIRouter(prefix="/api/futures", tags=["futures"])
SHANGHAI = ZoneInfo("Asia/Shanghai")
ENDPOINT = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/=/InnerFuturesNewService.getDailyKLine"
PRODUCTS = {
    "RB": (
        "螺纹钢",
        "SHFE",
        "https://www.shfe.cn/",
        ["钢厂产量与开工", "社会与钢厂库存", "建筑需求", "铁矿石与焦炭成本"],
    ),
    "CU": (
        "铜",
        "SHFE",
        "https://www.shfe.cn/",
        ["矿山供应与冶炼", "交易所库存与仓单", "电网与制造需求", "现货升贴水"],
    ),
    "AU": (
        "黄金",
        "SHFE",
        "https://www.shfe.cn/",
        ["实际利率与美元", "央行及实物需求", "库存与交割", "地缘与流动性变化"],
    ),
    "M": (
        "豆粕",
        "DCE",
        "https://www.dce.com.cn/",
        ["大豆进口与到港", "油厂压榨与库存", "饲料需求", "现货基差"],
    ),
    "SC": (
        "原油",
        "INE",
        "https://www.ine.cn/",
        ["原油供应与产量", "库存与炼厂开工", "运输与终端需求", "期限结构与交割"],
    ),
}
_cache = {}
_slots = asyncio.Semaphore(2)


def catalog():
    return [
        {"symbol": key, "name": value[0], "exchange": value[1], "official_url": value[2]}
        for key, value in PRODUCTS.items()
    ]


def _number(value, positive=False):
    if isinstance(value, bool):
        raise ValueError("invalid number")
    number = float(value)
    if not math.isfinite(number) or number < 0 or (positive and number == 0):
        raise ValueError("invalid number")
    return number


def parse_bars(raw, today):
    text = raw.decode("utf-8").strip()
    if text.startswith("/*"):
        end = text.find("*/", 2, 512)
        if end < 0:
            raise ValueError("invalid response wrapper")
        text = text[end + 2 :].strip()
    if text.startswith("=(") and text.endswith(");"):
        text = text[2:-2]
    value = json.loads(text)
    if not isinstance(value, list) or not 2 <= len(value) <= 20000:
        raise ValueError("invalid series")

    def row_day(row):
        if isinstance(row, dict):
            raw_day = row.get("d", row.get("date"))
        elif isinstance(row, list) and len(row) == 8:
            raw_day = row[0]
        else:
            raise ValueError("invalid row")
        return datetime.strptime(raw_day, "%Y-%m-%d").date()

    # Select the newest observation window regardless of feed ordering. Older
    # OHLC is outside this product's calculation window and is not certified.
    window = sorted(value, key=row_day)[-180:]
    bars, dates = [], set()
    for row in window:
        if isinstance(row, dict):
            values = [
                row.get(short, row.get(long))
                for short, long in [
                    ("d", "date"),
                    ("o", "open"),
                    ("h", "high"),
                    ("l", "low"),
                    ("c", "close"),
                    ("v", "volume"),
                    ("p", "hold"),
                    ("s", "settle"),
                ]
            ]
        elif isinstance(row, list) and len(row) == 8:
            values = row
        else:
            raise ValueError("invalid row")
        day = datetime.strptime(values[0], "%Y-%m-%d").date()
        if day > today or day in dates:
            raise ValueError("invalid date")
        dates.add(day)
        numbers = [_number(x, index < 4) for index, x in enumerate(values[1:])]
        opening, high, low, close, volume, interest, settlement = numbers
        if not low <= min(opening, close) <= max(opening, close) <= high:
            raise ValueError("invalid OHLC")
        bars.append(
            {
                "date": day.isoformat(),
                "open": opening,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "open_interest": interest,
                "settlement": settlement or None,
            }
        )
    bars.sort(key=lambda row: row["date"])
    return bars[-180:]


def _base_snapshot(symbol, collected_at):
    name, exchange, official_url, drivers = PRODUCTS[symbol]
    return {
        "symbol": symbol,
        "name": name,
        "exchange": exchange,
        "series_symbol": symbol + "0",
        "series_type": "continuous",
        "series_status": "unavailable",
        "tradable_contract": False,
        "data_as_of": None,
        "collected_at": collected_at,
        "calendar_age_days": None,
        "freshness": "unavailable",
        "source": {
            "name": "新浪财经",
            "kind": "secondary_market_data",
            "url": f"https://finance.sina.com.cn/futures/quotes/{symbol}0.shtml",
            "status": "unavailable",
            "original_verified": False,
        },
        "latest": {
            key: None
            for key in ("date", "open", "high", "low", "close", "volume", "open_interest", "settlement")
        },
        "technical": {
            key: None
            for key in (
                "sma20",
                "sma60",
                "change_pct",
                "return_5_observations_pct",
                "range20_low",
                "range20_high",
            )
        },
        "positioning": {
            "volume": None,
            "open_interest": None,
            "open_interest_change": None,
            "net_capital_flow": None,
            "member_rankings": None,
            "notice": "成交量、持仓量及其变化不能证明资金净流入或多空方向；连续合约换月也会影响变化。",
        },
        "fundamentals": [
            {"topic": topic, "status": "not_collected", "official_url": official_url} for topic in drivers
        ],
        "gaps": [
            "社会库存、现货基差、期限结构和会员持仓尚需原始数据",
            "当前合约、保证金及交割规则须按交易所和经纪商最新披露核对",
        ],
        "notice": "连续日线仅用于观察，可能含换月跳变，不能直接下单或作为策略回测收益；当日数据可能尚未收盘。",
        "bars": [],
        "portfolio_risk_status": "not_evaluated",
    }


def analyze(symbol, bars, collected_at):
    base = _base_snapshot(symbol, collected_at)
    last, previous = bars[-1], bars[-2]
    today = datetime.fromisoformat(collected_at).astimezone(SHANGHAI).date()
    age = (today - datetime.strptime(last["date"], "%Y-%m-%d").date()).days
    closes = [bar["close"] for bar in bars]

    def mean(days):
        return round(sum(closes[-days:]) / days, 4) if len(closes) >= days else None

    return {
        **base,
        "series_status": "collected",
        "data_as_of": last["date"],
        "collected_at": collected_at,
        "calendar_age_days": age,
        "freshness": "older_observation" if age > 4 else "daily_observation",
        "source": {**base["source"], "status": "collected"},
        "latest": last,
        "technical": {
            "sma20": mean(20),
            "sma60": mean(60),
            "change_pct": round((last["close"] / previous["close"] - 1) * 100, 4),
            "return_5_observations_pct": round((closes[-1] / closes[-6] - 1) * 100, 4)
            if len(closes) > 5
            else None,
            "range20_low": min(row["low"] for row in bars[-20:]) if len(bars) >= 20 else None,
            "range20_high": max(row["high"] for row in bars[-20:]) if len(bars) >= 20 else None,
        },
        "positioning": {
            **base["positioning"],
            "volume": last["volume"],
            "open_interest": last["open_interest"],
            "open_interest_change": last["open_interest"] - previous["open_interest"],
        },
        "bars": bars[-120:],
    }


async def _continuous(client, symbol, now, deadline):
    try:
        async with asyncio.timeout_at(deadline):
            raw = bytearray()
            client.cookies.clear()
            async with client.stream(
                "GET", ENDPOINT, params={"symbol": symbol + "0"}, headers={"Accept-Encoding": "identity"}
            ) as response:
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ValueError("unexpected series encoding")
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > 4 * 1024 * 1024:
                        raise ValueError("series too large")
                    raw.extend(chunk)
            bars = parse_bars(bytes(raw), now.date())
            return analyze(symbol, bars, now.isoformat(timespec="seconds")), None
    except (
        httpx.HTTPError,
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        RecursionError,
        TimeoutError,
    ) as error:
        return None, source_failure(error)


async def _reports(client, symbol, day, deadline):
    """Only a dated 404 permits a distinct earlier report query, never a retry.

    Keep collected sources when another source is missing. Each report parser
    verifies its requested date; this bounded window is not a trading calendar.
    Network, TLS, access and schema failures do not trigger older-date requests.
    """
    readers = {
        "warehouse_receipt": warehouse_receipts.read_report,
        "exchange_daily": exchange_futures.read_daily,
        "contract_dates": futures_parameters.read_contract_dates,
        "exchange_parameters": futures_parameters.read_exchange_parameters,
    }
    reports, attempts, pending = {}, [], list(readers)
    loop = asyncio.get_running_loop()
    for offset in range(3):
        if not pending or loop.time() >= deadline:
            break
        requested = day - timedelta(days=offset)
        selected = list(pending)
        values = await asyncio.gather(
            *[
                readers[key](
                    client,
                    symbol,
                    requested,
                    min(deadline, loop.time() + (4 if key == "warehouse_receipt" else 6)),
                )
                for key in selected
            ]
        )
        pending = []
        for key, value in zip(selected, values, strict=True):
            reports[key] = value
            attempts.append(
                {
                    "source": key,
                    "requested_date": requested.isoformat(),
                    "status": value["status"],
                    "error_code": value.get("error_code"),
                }
            )
            if value["status"] == "unavailable" and value.get("error_code") == "report_not_found":
                pending.append(key)
    for key in readers:
        reports.setdefault(
            key, {"status": "unavailable", "requested_date": day.isoformat(), "error_code": "source_timeout"}
        )
    return reports, attempts


async def snapshot(symbol):
    if symbol not in PRODUCTS:
        raise HTTPException(422, "请选择支持的期货品种")
    cached = _cache.get(symbol)
    if cached and time.monotonic() - cached[0] < cached[1].get("cache_ttl_seconds", 300):
        return copy.deepcopy({**cached[1], "cached": True})
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15
        async with asyncio.timeout_at(deadline), _slots:
            async with httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=False) as client:
                now = datetime.now(SHANGHAI)
                value, error = await _continuous(client, symbol, now, min(deadline - 6.25, loop.time() + 6))
                has_series = value is not None
                if value is None:
                    value = _base_snapshot(symbol, now.isoformat(timespec="seconds"))
                    value["source"].update(error or {})
                    value["gaps"].insert(
                        0, "未取得连续日线，技术指标与连续持仓变化未计算；其他来源按各自日期显示"
                    )
                # Keep both asynchronous readers owned until normal exit,
                # cancellation or an unexpected failure has closed siblings.
                async with asyncio.TaskGroup() as sources:
                    dated = sources.create_task(_reports(client, symbol, now.date(), deadline - 0.25))
                    monthly = sources.create_task(
                        futures_fundamentals.read_supply(client, symbol, now.date(), deadline - 0.25)
                    )
                reports, attempts = dated.result()
                supply = monthly.result()
                value.update(reports)
                value["fundamental_supply"] = supply
                if symbol == "RB":
                    value["fundamentals"][0] = {
                        **supply,
                        "official_url": supply.get("source_url", supply["index_url"]),
                    }
                value["source_attempts"] = attempts
                value["report_date_basis"] = "bounded_calendar_window"
                if value["exchange_daily"]["status"] == "collected":
                    value["positioning"]["member_rankings"] = value["exchange_daily"]["member_positioning"]
                    value["gaps"][1 if not has_series else 0] = (
                        "社会库存和同日同规格现货基差尚需原始数据；合约资料见其日期和范围。"
                        + (
                            "会员排名已取得，见对应披露范围。"
                            if value["exchange_daily"]["member_positioning"].get("status") == "collected"
                            else "会员排名尚未取得，不据成交或持仓手数推断资金方向。"
                        )
                    )
                coverage = {
                    "continuous_series": value["series_status"],
                    **{key: item["status"] for key, item in reports.items()},
                }
                coverage["member_positioning"] = (
                    value["exchange_daily"].get("member_positioning", {}).get("status", "unavailable")
                )
                coverage["fundamental_supply"] = supply["status"]
                value["coverage"] = coverage
                if not any(status == "collected" for status in coverage.values()):
                    raise HTTPException(
                        503,
                        "期货资料暂不可用，行情和交易所来源均未取得；请稍后手动重试，本次未编造数据",
                        headers={"X-Prism-Data-Status": "unavailable", "Retry-After": "30"},
                    )
                value["status"] = (
                    "partial" if any(s != "collected" for s in coverage.values()) else "collected"
                )
                value["cache_ttl_seconds"] = 30 if value["status"] == "partial" else 300
                dates = [value["data_as_of"]] if has_series else []
                dates.extend(
                    item["data_as_of"]
                    for item in reports.values()
                    if item["status"] == "collected"
                    and item.get("data_as_of")
                    and item.get("date_provenance") != "request_parameter"
                )
                value["observation_date_gaps"] = [
                    key
                    for key, item in reports.items()
                    if item["status"] == "collected" and item.get("date_provenance") == "request_parameter"
                ]
                value["observation_dates"] = sorted(set(dates))
                value["date_alignment_scope"] = (
                    "daily_observations_only; monthly supply retains its own periods"
                )
                value["date_alignment"] = (
                    "mixed"
                    if len(set(dates)) > 1
                    else "unverified"
                    if value["observation_date_gaps"]
                    else "same_day"
                    if len(dates) >= 2
                    else "not_compared"
                )
            _cache[symbol] = (time.monotonic(), value)
            return copy.deepcopy({**value, "cached": False})
    except (httpx.HTTPError, ValueError, TypeError, KeyError, OverflowError, RecursionError, TimeoutError):
        raise HTTPException(503, "期货资料暂不可用，请稍后手动重试；本次没有使用编造数据") from None


@router.get("/products")
async def products(request: Request):
    require_auth(request)
    return {"products": catalog()}


@router.get("/{symbol}/snapshot")
async def read_snapshot(symbol: str, request: Request):
    owner = require_auth(request)
    if not check_rate_limit("futures:" + owner, max_req=12, window=60):
        raise HTTPException(429, "查询较频繁，请稍后再试")
    return await snapshot(symbol)
