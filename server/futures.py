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
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, HTTPException, Request

from .auth import require_auth
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


def analyze(symbol, bars, collected_at):
    name, exchange, official_url, drivers = PRODUCTS[symbol]
    last, previous = bars[-1], bars[-2]
    today = datetime.fromisoformat(collected_at).astimezone(SHANGHAI).date()
    age = (today - datetime.strptime(last["date"], "%Y-%m-%d").date()).days
    closes = [bar["close"] for bar in bars]

    def mean(days):
        return round(sum(closes[-days:]) / days, 4) if len(closes) >= days else None

    return {
        "symbol": symbol,
        "name": name,
        "exchange": exchange,
        "series_symbol": symbol + "0",
        "series_type": "continuous",
        "tradable_contract": False,
        "data_as_of": last["date"],
        "collected_at": collected_at,
        "calendar_age_days": age,
        "freshness": "older_observation" if age > 4 else "daily_observation",
        "source": {
            "name": "新浪财经",
            "kind": "secondary_market_data",
            "url": f"https://finance.sina.com.cn/futures/quotes/{symbol}0.shtml",
            "original_verified": False,
        },
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
            "volume": last["volume"],
            "open_interest": last["open_interest"],
            "open_interest_change": last["open_interest"] - previous["open_interest"],
            "net_capital_flow": None,
            "member_rankings": None,
            "notice": "成交量、持仓量及其变化不能证明资金净流入或多空方向；连续合约换月也会影响变化。",
        },
        "fundamentals": [
            {"topic": topic, "status": "not_collected", "official_url": official_url} for topic in drivers
        ],
        "gaps": [
            "库存、现货基差、期限结构和会员持仓尚需原始数据",
            "当前合约、保证金及交割规则须按交易所和经纪商最新披露核对",
        ],
        "notice": "连续日线仅用于观察，可能含换月跳变，不能直接下单或作为策略回测收益；当日数据可能尚未收盘。",
        "bars": bars[-120:],
        "portfolio_risk_status": "not_evaluated",
    }


async def snapshot(symbol):
    if symbol not in PRODUCTS:
        raise HTTPException(422, "请选择支持的期货品种")
    cached = _cache.get(symbol)
    if cached and time.monotonic() - cached[0] < 300:
        return copy.deepcopy({**cached[1], "cached": True})
    try:
        async with asyncio.timeout(15), _slots:
            async with httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=False) as client:
                raw = bytearray()
                async with client.stream("GET", ENDPOINT, params={"symbol": symbol + "0"}) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 4 * 1024 * 1024:
                            raise ValueError("series too large")
            now = datetime.now(SHANGHAI)
            bars = parse_bars(bytes(raw), now.date())
            value = analyze(symbol, bars, now.isoformat(timespec="seconds"))
            _cache[symbol] = (time.monotonic(), value)
            return copy.deepcopy({**value, "cached": False})
    except (httpx.HTTPError, ValueError, TypeError, KeyError, OverflowError, RecursionError, TimeoutError):
        raise HTTPException(503, "期货日线暂不可用，请稍后手动重试；本次没有使用编造数据") from None


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
