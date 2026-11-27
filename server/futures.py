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

from . import (
    cffex_futures,
    czce_futures,
    exchange_futures,
    futures_fundamentals,
    futures_gold,
    futures_parameters,
    futures_soybean,
    futures_weekly,
    gfex_futures,
    public_network,
    warehouse_receipts,
)
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
        ["国内黄金产量与消费背景", "实际利率与美元", "央行需求", "库存、交割与流动性变化"],
    ),
    "AL": (
        "铝",
        "SHFE",
        "https://www.shfe.cn/",
        ["电解铝产能与运行", "氧化铝与电力成本", "铝锭与铝棒库存", "地产与新能源需求"],
    ),
    "ZN": (
        "锌",
        "SHFE",
        "https://www.shfe.cn/",
        ["锌矿加工费与冶炼利润", "冶炼检修与产量", "社会与保税库存", "镀锌与压铸需求"],
    ),
    "NI": (
        "镍",
        "SHFE",
        "https://www.shfe.cn/",
        ["镍矿与镍铁供应", "精炼镍与硫酸镍产能", "交易所库存", "不锈钢与新能源需求"],
    ),
    "AG": (
        "白银",
        "SHFE",
        "https://www.shfe.cn/",
        ["白银矿产与回收", "工业与光伏需求", "交易所库存与ETF", "金银比与宏观利率"],
    ),
    "RU": (
        "天然橡胶",
        "SHFE",
        "https://www.shfe.cn/",
        ["产区割胶与天气", "青岛与交易所库存", "轮胎开工与出口", "合成胶价差与替代"],
    ),
    "BU": (
        "石油沥青",
        "SHFE",
        "https://www.shfe.cn/",
        ["原油与稀释沥青成本", "炼厂开工与排产", "道路施工需求", "厂库与社会库存"],
    ),
    "M": (
        "豆粕",
        "DCE",
        "https://www.dce.com.cn/",
        ["大豆年度供需与压榨背景", "油厂库存与开工", "饲料需求", "现货基差"],
    ),
    "C": (
        "玉米",
        "DCE",
        "https://www.dce.com.cn/",
        ["新作产量与售粮进度", "渠道与港口库存", "深加工与饲料需求", "进口与替代谷物价差"],
    ),
    "I": (
        "铁矿石",
        "DCE",
        "https://www.dce.com.cn/",
        ["矿山发运与到港", "港口与钢厂库存", "高炉开工与铁水产量", "钢材利润与现货基差"],
    ),
    "P": (
        "棕榈油",
        "DCE",
        "https://www.dce.com.cn/",
        ["产地棕榈油产量与库存", "出口与进口节奏", "豆棕与菜棕价差", "生物柴油需求政策"],
    ),
    "SC": (
        "原油",
        "INE",
        "https://www.ine.cn/",
        ["原油供应与产量", "库存与炼厂开工", "运输与终端需求", "期限结构与交割"],
    ),
    "NR": (
        "20号胶",
        "INE",
        "https://www.ine.cn/",
        ["东南亚产区天气与割胶", "轮胎开工与出口", "青岛保税库存", "20号胶与全乳价差"],
    ),
    "LU": (
        "低硫燃料油",
        "INE",
        "https://www.ine.cn/",
        ["原油与船燃价差", "炼厂低硫组分供应", "船运需求与加注", "新加坡燃料油库存"],
    ),
    "MA": (
        "甲醇",
        "CZCE",
        "https://www.czce.com.cn/",
        ["煤制与天然气制成本", "港口与内地库存", "MTO与甲醛需求", "进口与运费"],
    ),
    "CF": (
        "棉花",
        "CZCE",
        "https://www.czce.com.cn/",
        ["新棉产量与收购进度", "纱厂开工与库存", "内外棉价差", "储备与进口政策"],
    ),
    "SR": (
        "白糖",
        "CZCE",
        "https://www.czce.com.cn/",
        ["甘蔗与甜菜压榨进度", "食糖产销与库存", "配额外进口利润", "消费与替代"],
    ),
    "FG": (
        "玻璃",
        "CZCE",
        "https://www.czce.com.cn/",
        ["浮法玻璃产线冷修与在产", "地产竣工需求", "纯碱与燃料成本", "生产企业库存"],
    ),
    "TA": (
        "精对苯二甲酸",
        "CZCE",
        "https://www.czce.com.cn/",
        ["PX与原油成本", "聚酯开工与产销", "社会库存与仓单", "加工费与装置检修"],
    ),
    "RM": (
        "菜籽粕",
        "CZCE",
        "https://www.czce.com.cn/",
        ["菜籽进口与压榨", "水产与禽畜饲料需求", "豆菜粕价差与替代", "油厂库存与开机"],
    ),
    "OI": (
        "菜籽油",
        "CZCE",
        "https://www.czce.com.cn/",
        ["菜籽与菜油进口", "油脂库存与消费", "豆棕菜油价差", "收储与政策"],
    ),
    "PF": (
        "涤纶短纤",
        "CZCE",
        "https://www.czce.com.cn/",
        ["PTA与乙二醇成本", "纱厂开工与订单", "涤短库存与产销", "替代品价差"],
    ),
    "AP": (
        "苹果",
        "CZCE",
        "https://www.czce.com.cn/",
        ["新季产量与优果率", "冷库库存与去库", "消费与替代水果", "交割标准与仓单"],
    ),
    "IF": (
        "沪深300股指期货",
        "CFFEX",
        "http://www.cffex.com.cn/",
        ["成分股盈利与估值", "市场成交与资金面", "基差与贴水结构", "分红与展期成本"],
    ),
    "IC": (
        "中证500股指期货",
        "CFFEX",
        "http://www.cffex.com.cn/",
        ["中盘成长盈利", "市场流动性与成交", "基差与贴水结构", "分红与展期成本"],
    ),
    "T": (
        "10年期国债期货",
        "CFFEX",
        "http://www.cffex.com.cn/",
        ["资金利率与流动性", "经济与通胀预期", "国债发行与供给", "基差与交割期权"],
    ),
    "TF": (
        "5年期国债期货",
        "CFFEX",
        "http://www.cffex.com.cn/",
        ["短端资金利率", "政策利率与曲线形态", "国债发行与供给", "基差与交割期权"],
    ),
    "TS": (
        "2年期国债期货",
        "CFFEX",
        "http://www.cffex.com.cn/",
        ["短端资金利率", "政策利率与流动性", "国债发行与供给", "基差与交割期权"],
    ),
    "SI": (
        "工业硅",
        "GFEX",
        "http://www.gfex.com.cn/",
        ["丰水期开工与成本", "多晶硅与有机硅需求", "社会库存", "电价与还原剂成本"],
    ),
    "LC": (
        "碳酸锂",
        "GFEX",
        "http://www.gfex.com.cn/",
        ["锂矿与盐湖供给", "正极材料排产", "电池库存与回收", "进口与成本曲线"],
    ),
    "PS": (
        "多晶硅",
        "GFEX",
        "http://www.gfex.com.cn/",
        ["硅料产能与检修", "组件排产与装机", "硅片库存", "电价与工业硅成本"],
    ),
    "SN": ("锡", "SHFE", "https://www.shfe.cn/", ["锡矿供应", "精炼锡产量", "焊料需求"]),
    "AO": ("氧化铝", "SHFE", "https://www.shfe.cn/", ["铝土矿到港", "氧化铝焙烧产能", "电解铝开工"]),
    "HC": ("热轧卷板", "SHFE", "https://www.shfe.cn/", ["钢厂热卷产量", "制造业用钢", "板卷库存"]),
    "SS": ("不锈钢", "SHFE", "https://www.shfe.cn/", ["镍铁与铬铁成本", "钢厂排产", "冷轧库存"]),
    "SP": ("纸浆", "SHFE", "https://www.shfe.cn/", ["外盘浆价", "港口库存", "纸厂开工"]),
    "EC": (
        "集运指数（欧线）",
        "INE",
        "https://www.ine.cn/",
        ["上海至欧洲基本港运价", "班轮运力与停航", "欧线结算运价指数"],
    ),
    "JM": ("焦煤", "DCE", "https://www.dce.com.cn/", ["矿山产量", "蒙煤进口", "焦化开工"]),
    "CS": ("玉米淀粉", "DCE", "https://www.dce.com.cn/", ["玉米原料成本", "深加工开工", "淀粉库存"]),
    "LH": ("生猪", "DCE", "https://www.dce.com.cn/", ["能繁母猪存栏", "出栏体重", "屠宰量"]),
    "JD": ("鸡蛋", "DCE", "https://www.dce.com.cn/", ["在产蛋鸡存栏", "饲料成本", "蛋品走货"]),
    "PP": ("聚丙烯", "DCE", "https://www.dce.com.cn/", ["石化装置检修", "丙烯成本", "塑编开工"]),
    "V": (
        "聚氯乙烯（PVC）",
        "DCE",
        "https://www.dce.com.cn/",
        ["电石与乙烯成本", "PVC装置开工", "管材型材需求"],
    ),
    "PG": ("液化石油气", "DCE", "https://www.dce.com.cn/", ["进口到岸成本", "炼厂外放量", "燃烧与化工需求"]),
    "A": ("豆一", "DCE", "https://www.dce.com.cn/", ["国产非转基因大豆产量", "产区收购", "食用豆需求"]),
    "B": ("豆二", "DCE", "https://www.dce.com.cn/", ["进口大豆到港", "压榨利润", "港口库存"]),
    "Y": ("豆油", "DCE", "https://www.dce.com.cn/", ["大豆压榨量", "油厂库存", "食用油消费"]),
    "CJ": ("红枣", "CZCE", "https://www.czce.com.cn/", ["新疆产量与等级", "产区收购", "销区库存"]),
    "PK": ("花生", "CZCE", "https://www.czce.com.cn/", ["产区种植与单产", "油厂收购", "花生油粕需求"]),
    "SA": ("纯碱", "CZCE", "https://www.czce.com.cn/", ["联碱与氨碱装置开工", "重碱库存", "玻璃厂采购"]),
    "SH": ("烧碱", "CZCE", "https://www.czce.com.cn/", ["氯碱装置开工", "液碱浓度与运费", "氧化铝耗碱需求"]),
    "UR": ("尿素", "CZCE", "https://www.czce.com.cn/", ["煤炭与天然气成本", "农业施肥节奏", "复合肥开工"]),
}
_cache = {}
_SERIES_HISTORY: dict[str, tuple[float, list[dict]]] = {}
_slots = asyncio.Semaphore(2)


def series_history(symbol: str, *, max_age: float = 3600.0) -> list[dict] | None:
    """Return bounded validated daily bars cached from a recent continuous-series read."""
    entry = _SERIES_HISTORY.get(symbol)
    if entry is None:
        return None
    stamp, bars = entry
    if time.monotonic() - stamp > max_age:
        return None
    return bars


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


def parse_bars(raw, today, *, window: int = 180):
    """Decode the bounded Sina JSONP daily series into the newest ``window`` rows."""
    window = int(window)
    if not 1 <= window <= 20000:
        raise ValueError("invalid window")
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
    recent = sorted(value, key=row_day)[-window:]
    bars, dates = [], set()
    for row in recent:
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
    return bars[-window:]


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


async def _continuous(client, symbol, now, deadline, *, workspace=False):
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
            bars = parse_bars(bytes(raw), now.date(), window=1300 if workspace else 180)
            _SERIES_HISTORY[symbol] = (time.monotonic(), bars)
            value = analyze(symbol, bars, now.isoformat(timespec="seconds"))
            if workspace:
                from . import futures_signals

                value["workspace_technicals"] = futures_signals.technicals(bars)
                value["workspace_positioning"] = futures_signals.positioning(bars)
                value["workspace_percentiles"] = futures_signals.percentiles(bars)
                value["workspace_roll"] = futures_signals.roll_diagnostics(bars)
            return value, None
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


async def snapshot(symbol, *, workspace=False):
    if symbol not in PRODUCTS:
        raise HTTPException(422, "请选择支持的期货品种")
    cache_key = (symbol, workspace) if workspace else symbol
    cached = _cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < cached[1].get("cache_ttl_seconds", 300):
        return copy.deepcopy({**cached[1], "cached": True})
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15
        async with asyncio.timeout_at(deadline), _slots:
            async with public_network.async_client(timeout=8) as client:
                now = datetime.now(SHANGHAI)
                options = {"workspace": True} if workspace else {}
                value, error = await _continuous(
                    client, symbol, now, min(deadline - 6.25, loop.time() + 6), **options
                )
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
                        (
                            futures_soybean
                            if symbol == "M"
                            else futures_gold
                            if symbol == "AU"
                            else futures_fundamentals
                        ).read_supply(client, symbol, now.date(), deadline - 0.25)
                    )
                reports, attempts = dated.result()
                supply = monthly.result()
                value.update(reports)
                value["fundamental_supply"] = supply
                if symbol in {"RB", "SC", "M", "AU"}:
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
                weekly = futures_weekly.research(symbol, now)
                if weekly:
                    value["weekly_research"] = weekly
                value["cache_ttl_seconds"] = 30 if value["status"] == "partial" else 300
                dates = [value["data_as_of"]] if has_series else []
                dates.extend(
                    item["data_as_of"]
                    for item in reports.values()
                    if item["status"] == "collected"
                    and item.get("data_as_of")
                    and item.get("date_provenance") != "request_parameter"
                )
                ranking = value["exchange_daily"].get("member_positioning", {})
                if ranking.get("status") == "collected" and ranking.get("data_as_of"):
                    dates.append(ranking["data_as_of"])
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
            _cache[cache_key] = (time.monotonic(), value)
            return copy.deepcopy({**value, "cached": False})
    except (httpx.HTTPError, ValueError, TypeError, KeyError, OverflowError, RecursionError, TimeoutError):
        raise HTTPException(503, "期货资料暂不可用，请稍后手动重试；本次没有使用编造数据") from None


# ---------------------------------------------------------------------------
# Evidence pages
#
# A research run may quote futures figures in its report. The research ledger
# can only verify a figure against an obtained document, so one successful
# snapshot is rendered here as an ordinary page-addressed document: one page per
# source section, each carrying the product, the source and the data date next
# to the numbers the platform actually returned. The report writer's own tool
# result is never changed; this is evidence bookkeeping only.
# ---------------------------------------------------------------------------
_EXCHANGE_SOURCES = {
    "SHFE": "上海期货交易所",
    "DCE": "大连商品交易所",
    "INE": "上海国际能源交易中心",
    "CZCE": "郑州商品交易所",
    "CFFEX": "中国金融期货交易所",
    "GFEX": "广州期货交易所",
}
_QUOTE_UNITS = {
    "M": "元/吨",
    "C": "元/吨",
    "I": "元/吨",
    "P": "元/吨",
    **{symbol: unit.split("（", 1)[0] for symbol, unit in exchange_futures.PRICE_UNITS.items()},
    **{
        symbol: unit
        for module in (czce_futures, cffex_futures, gfex_futures)
        for symbol, unit in module.PRICE_UNITS.items()
    },
    "JM": "元/吨",
    "CS": "元/吨",
    "LH": "元/吨",
    "JD": "元/500千克",
    "PP": "元/吨",
    "V": "元/吨",
    "PG": "元/吨",
    "A": "元/吨",
    "B": "元/吨",
    "Y": "元/吨",
}
_EVIDENCE_NOTICE = (
    "本页为公开行情数据快照；连续合约不可直接交易，成交量与持仓量不代表资金净流入，数值以对应来源字段为准。"
)


def _evidence_number(value, digits=2):
    """Format one finite measurement without inventing missing precision."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    if number == int(number):
        return str(int(number))
    return f"{number:.{digits}f}".rstrip("0").rstrip(".")


def _evidence_day(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def _chinese_day(value):
    day = _evidence_day(value)
    if day is None:
        return "未单列"
    year, month, number = day.split("-")
    return f"{year}年{month}月{number}日"


def _evidence_page(section, label, source, url, data_period, lines):
    lines = [line for line in lines if line]
    if not lines:
        return None
    return {
        "section": section,
        "label": label,
        "source": source if isinstance(source, str) and source else "公开数据来源",
        "url": url if isinstance(url, str) and url.startswith(("http://", "https://")) else None,
        "data_period": _evidence_day(data_period),
        "lines": lines,
    }


def _series_section(snapshot, quote):
    latest = snapshot.get("latest")
    if snapshot.get("series_status") != "collected" or not isinstance(latest, dict):
        return None
    day = _evidence_day(latest.get("date"))
    if day is None:
        return None
    lines = []
    for key, label in (
        ("open", "开盘价"),
        ("high", "最高价"),
        ("low", "最低价"),
        ("close", "收盘价"),
        ("settlement", "结算价"),
    ):
        number = _evidence_number(latest.get(key))
        if number is not None:
            lines.append(f"{label} {number} {quote}")
    for key, label in (("volume", "成交量"), ("open_interest", "持仓量")):
        number = _evidence_number(latest.get(key))
        if number is not None:
            lines.append(f"{label} {number} 手")
    technical = snapshot.get("technical")
    if isinstance(technical, dict):
        for key, label, unit in (
            ("change_pct", "较前一交易日涨跌幅", "%"),
            ("return_5_observations_pct", "最近五个交易日涨跌幅", "%"),
            ("sma20", "20日均线", quote),
            ("sma60", "60日均线", quote),
            ("range20_low", "20日区间最低价", quote),
            ("range20_high", "20日区间最高价", quote),
        ):
            number = _evidence_number(technical.get(key))
            if number is not None:
                separator = "" if unit == "%" else " "
                lines.append(f"{label} {number}{separator}{unit}")
    positioning = snapshot.get("positioning")
    if isinstance(positioning, dict):
        number = _evidence_number(positioning.get("open_interest_change"))
        if number is not None:
            lines.append(f"持仓量变化 {number} 手")
    source = snapshot.get("source") if isinstance(snapshot.get("source"), dict) else {}
    return _evidence_page(
        "continuous_series",
        "连续合约行情（新浪财经日线）",
        source.get("name") or "新浪财经",
        source.get("url"),
        day,
        lines,
    )


def _exchange_source(exchange):
    return _EXCHANGE_SOURCES.get(exchange, "交易所公开数据")


def _exchange_daily_section(snapshot, exchange, quote):
    report = snapshot.get("exchange_daily")
    if not isinstance(report, dict) or report.get("status") != "collected":
        return None
    day = _evidence_day(report.get("data_as_of"))
    lines = []
    contracts = report.get("contracts")
    if isinstance(contracts, list):
        for contract in contracts[:8]:
            if not isinstance(contract, dict):
                continue
            code = contract.get("contract")
            if not isinstance(code, str) or not code:
                continue
            for key, label in (("settlement", "结算价"), ("close", "收盘价")):
                number = _evidence_number(contract.get(key))
                if number is not None:
                    lines.append(f"{code.upper()} {label} {number} {quote}")
            for key, label in (("volume", "成交量"), ("open_interest", "持仓量")):
                number = _evidence_number(contract.get(key))
                if number is not None:
                    lines.append(f"{code.upper()} {label} {number} 手")
    curve = report.get("curve")
    if isinstance(curve, dict) and curve.get("status") == "collected":
        for key, label in (
            ("near_settlement", "近月合约结算价"),
            ("far_settlement", "远月合约结算价"),
        ):
            number = _evidence_number(curve.get(key))
            if number is not None:
                lines.append(f"{label} {number} {quote}")
        spread = _evidence_number(curve.get("far_minus_near"))
        if spread is not None:
            lines.append(f"远月减近月价差 {spread} {quote}")
    ranking = report.get("member_positioning")
    ranked = isinstance(ranking, dict) and ranking.get("status") == "collected"
    if ranked:
        sides = ranking.get("sides")
        if isinstance(sides, dict):
            for key, label in (("volume", "成交"), ("long", "多头持仓"), ("short", "空头持仓")):
                side = sides.get(key)
                if not isinstance(side, dict):
                    continue
                total = _evidence_number(side.get("quantity_sum"))
                if total is not None:
                    lines.append(f"会员{label}排名合计 {total} 手")
    return _evidence_page(
        "exchange_daily",
        "交易所结算日报与会员排名" if ranked else "交易所结算日报",
        _exchange_source(exchange),
        report.get("source_url"),
        day,
        lines,
    )


def _warehouse_section(snapshot, exchange):
    report = snapshot.get("warehouse_receipt")
    if not isinstance(report, dict) or report.get("status") != "collected":
        return None
    unit = report.get("unit") if isinstance(report.get("unit"), str) else "吨"
    lines = []
    quantity = _evidence_number(report.get("quantity"))
    if quantity is not None:
        lines.append(f"仓单数量 {quantity} {unit}")
    change = _evidence_number(report.get("change"))
    if change is not None:
        derived = report.get("change_basis") == "delta_vs_previous_disclosed_row"
        lines.append(f"仓单增减{'（较上一披露日）' if derived else ''} {change} {unit}")
    table = report.get("table")
    label = report.get("label")
    if not isinstance(label, str) or not label:
        label = f"仓单日报（{table}）" if isinstance(table, str) and table else "仓单日报"
    source_name = report.get("source_name")
    if not isinstance(source_name, str) or not source_name:
        source_name = _exchange_source(exchange)
    return _evidence_page(
        "warehouse_receipt",
        label,
        source_name,
        report.get("source_url"),
        report.get("data_as_of"),
        lines,
    )


def _contract_dates_section(snapshot, exchange):
    report = snapshot.get("contract_dates")
    if not isinstance(report, dict) or report.get("status") != "collected":
        return None
    lines = []
    contracts = report.get("contracts")
    if isinstance(contracts, list):
        for contract in contracts[:6]:
            if not isinstance(contract, dict):
                continue
            code = contract.get("contract")
            if not isinstance(code, str) or not code:
                continue
            for key, label in (
                ("listed_on", "挂牌日期"),
                ("listed_expiry_date", "最后交易日"),
            ):
                if _evidence_day(contract.get(key)) is not None:
                    lines.append(f"{code.upper()} {label} {_chinese_day(contract.get(key))}")
            start = _evidence_day(contract.get("delivery_start"))
            end = _evidence_day(contract.get("delivery_end"))
            if start is not None and end is not None:
                lines.append(f"{code.upper()} 交割日期 {_chinese_day(start)} 至 {_chinese_day(end)}")
    return _evidence_page(
        "contract_dates",
        "合约挂牌与交割日期",
        _exchange_source(exchange),
        report.get("source_url"),
        report.get("data_as_of"),
        lines,
    )


def _parameters_section(snapshot, exchange):
    report = snapshot.get("exchange_parameters")
    if not isinstance(report, dict) or report.get("status") != "collected":
        return None
    lines = []
    contracts = report.get("contracts")
    if isinstance(contracts, list):
        for contract in contracts[:6]:
            if not isinstance(contract, dict):
                continue
            code = contract.get("contract")
            if not isinstance(code, str) or not code:
                continue
            for key, label in (
                ("spec_long_margin_pct", "投机多头保证金率"),
                ("spec_short_margin_pct", "投机空头保证金率"),
                ("hedge_long_margin_pct", "套保多头保证金率"),
                ("hedge_short_margin_pct", "套保空头保证金率"),
                ("upper_limit_pct", "涨停板幅度"),
                ("lower_limit_pct", "跌停板幅度"),
            ):
                number = _evidence_number(contract.get(key))
                if number is not None:
                    lines.append(f"{code.upper()} {label} {number}%")
    return _evidence_page(
        "exchange_parameters",
        "保证金与涨跌停参数",
        _exchange_source(exchange),
        report.get("source_url"),
        report.get("data_as_of"),
        lines,
    )


def snapshot_document(snapshot):
    """Render one futures snapshot as a source- and page-addressable document.

    The research ledger stores the returned pages as an ordinary obtained
    document, so its existing body-number matcher can bind report figures to the
    page that carries them. ``None`` means the observation is too incomplete to
    register as evidence; the platform tool result is left untouched either way.
    """
    if not isinstance(snapshot, dict):
        return None
    symbol = snapshot.get("symbol")
    if not isinstance(symbol, str) or symbol not in PRODUCTS:
        return None
    source = snapshot.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("url"), str):
        return None
    if not isinstance(snapshot.get("collected_at"), str):
        return None
    name = snapshot.get("name") if isinstance(snapshot.get("name"), str) else PRODUCTS[symbol][0]
    exchange = snapshot.get("exchange") if isinstance(snapshot.get("exchange"), str) else PRODUCTS[symbol][1]
    quote = _QUOTE_UNITS.get(symbol, "元/吨")
    sections = [
        page
        for page in (
            _series_section(snapshot, quote),
            _exchange_daily_section(snapshot, exchange, quote),
            _warehouse_section(snapshot, exchange),
            _contract_dates_section(snapshot, exchange),
            _parameters_section(snapshot, exchange),
        )
        if page is not None
    ]
    weekly = snapshot.get("weekly_research")
    if isinstance(weekly, dict):
        for row in weekly.get("rows", []):
            sections.append(
                _evidence_page(
                    "weekly_" + row["key"],
                    row["label"],
                    row["publisher"],
                    row["url"],
                    row["period"],
                    [
                        f"{row['label']} {row['value']} {row['unit']}；统计范围 {row['scope']}；"
                        f"该来源可知时间 {row['known']}（上海）。"
                    ],
                )
            )
    if not sections:
        return None
    observed = [
        page["data_period"]
        for page in sections
        if page.get("data_period") and page["section"] != "weekly_crush_plan"
    ]
    data_period = _evidence_day(snapshot.get("data_as_of")) or (max(observed) if observed else None)
    if data_period is None:
        return None
    pages = []
    for index, page in enumerate(sections, start=1):
        header = (
            f"数据来源：{page['source']}｜数据日期：{_chinese_day(page['data_period'])}｜"
            f"品种：{name} {symbol}（{exchange}）"
        )
        pages.append(
            {
                "page": index,
                "text": "\n".join([header, f"【{page['label']}】", *page["lines"], _EVIDENCE_NOTICE]),
            }
        )
    return {
        "symbol": symbol,
        "name": name,
        "exchange": exchange,
        "title": f"{name} {symbol} 市场数据快照 {data_period}",
        "url": sections[0]["url"] or source["url"],
        "publisher": "、".join(dict.fromkeys(page["source"] for page in sections)),
        "retrieved_at": snapshot["collected_at"],
        "data_period": data_period,
        "status": snapshot.get("status"),
        "tradable_contract": snapshot.get("tradable_contract") is True,
        "source": {"name": source.get("name"), "url": source["url"], "kind": source.get("kind")},
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
