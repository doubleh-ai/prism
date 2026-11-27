"""Read-only futures panel: coverage catalog plus one symbol's four observations.

The panel is a read-only view over the existing futures sources. It never
estimates a missing number: an unavailable dimension keeps its status, a
visible reason code and no values, so the front end can render ``暂无数据``
with the reason instead of a zero or a placeholder.
"""

from __future__ import annotations

import asyncio
import html
import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request

from . import futures, futures_basis, futures_inventory, futures_parameters, futures_spread, futures_weekly
from .auth import require_auth
from .database import check_rate_limit

SHANGHAI = ZoneInfo("Asia/Shanghai")

DIMENSIONS = (
    ("snapshot", "最新行情", "新浪财经日线"),
    ("spread", "跨期价差", "交易所公开日报"),
    ("inventory", "仓单库存", "交易所与公开仓单来源"),
    ("basis", "现货基差", "生意社现期表"),
)

SPREAD_COVERED = tuple(futures_parameters.PRODUCTS)
INVENTORY_COVERED = tuple(futures_inventory.COVERED)
BASIS_COVERED = tuple(futures_basis.BASIS_PRODUCTS)

DIMENSION_LABELS = {key: label for key, label, _ in DIMENSIONS}

SHAPE_LABELS = {
    "contango": "远月升水（远月结算价高于近月）",
    "backwardation": "远月贴水（远月结算价低于近月）",
    "flat": "平水（远近月结算价相同）",
}
BASIS_LABELS = {
    "spot_premium": "现货升水（基差为正）",
    "futures_premium": "期货升水（基差为负）",
    "flat": "持平（基差为零）",
}

PANEL_NOTICE = "只读面板；每个维度独立显示数据日期与来源，缺失维度不估算、不补零。"


def _available() -> dict:
    return {"status": "available", "reason_code": None, "reason": None}


def _uncovered(reason_code: str, reason: str) -> dict:
    return {"status": "unavailable", "reason_code": reason_code, "reason": reason}


def availability(symbol: str) -> dict:
    """Static coverage of one product across the four panel dimensions."""
    spread = (
        _available()
        if symbol in SPREAD_COVERED
        else _uncovered(
            "spread_unavailable:contract_months_not_covered",
            futures_spread.not_covered_reason(symbol),
        )
    )
    inventory = (
        _available()
        if symbol in INVENTORY_COVERED
        else _uncovered("inventory_unavailable", futures_inventory.UNCOVERED_REASONS[symbol])
    )
    basis = (
        _available()
        if symbol in BASIS_COVERED
        else _uncovered(
            "basis_unavailable:symbol_not_covered",
            futures_basis.UNCOVERED_REASONS[symbol],
        )
    )
    return {"snapshot": _available(), "spread": spread, "inventory": inventory, "basis": basis}


def catalog() -> dict:
    """Return every product with per-dimension availability and reason codes."""
    products = []
    for item in futures.catalog():
        products.append(
            {
                "symbol": item["symbol"],
                "name": item["name"],
                "exchange": item["exchange"],
                "official_url": item["official_url"],
                "dimensions": availability(item["symbol"]),
            }
        )
    return {
        "dimensions": [
            {"key": key, "label": label, "source_name": source} for key, label, source in DIMENSIONS
        ],
        "products": products,
        "count": len(products),
        "generated_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "notice": PANEL_NOTICE,
    }


def options_markup() -> str:
    return "".join(
        f'<option value="{html.escape(item["symbol"])}" data-name="{html.escape(item["name"])}">'
        f"{html.escape(item['name'])} · {html.escape(item['symbol'])} · {html.escape(item['exchange'])}</option>"
        for item in futures.catalog()
    )


# Common trading names are searchable alongside the exchange's product name.
_MARKET_NAMES = {
    "AU": "沪金",
    "AG": "沪银",
    "CU": "沪铜",
    "AL": "沪铝",
    "NI": "沪镍",
    "SN": "沪锡",
    "HC": "热卷",
    "TA": "PTA",
    "PG": "液化气",
    "EC": "集运欧线",
}


def catalog_markup() -> str:
    """Render the availability catalog as the table cells of the page shell."""
    headers = "".join(f"<th>{html.escape(label)}</th>" for _, label, _ in DIMENSIONS)
    rows = []
    for product in catalog()["products"]:
        symbol = html.escape(product["symbol"])
        name = html.escape(product["name"])
        if symbol in _MARKET_NAMES:
            name += f"（{html.escape(_MARKET_NAMES[symbol])}）"
        exchange = html.escape(product["exchange"])
        cells = []
        for key, _, _ in DIMENSIONS:
            dimension = product["dimensions"][key]
            if dimension["status"] == "available":
                cells.append('<td><span class="futures-panel-chip is-available">可用</span></td>')
                continue
            code = html.escape(dimension["reason_code"])
            reason = html.escape(dimension["reason"] or "")
            cells.append(
                f'<td><span class="futures-panel-chip is-unavailable" title="{reason}">{code}</span></td>'
            )
        rows.append(
            f'<tr data-symbol="{symbol}"><th scope="row">{name} · {symbol}'
            f'<span class="muted"> · {exchange}</span></th>{"".join(cells)}'
            f'<td><button type="button" class="futures-panel-open" data-symbol="{symbol}">查看</button></td></tr>'
        )
    return (
        '<table class="futures-panel-table"><thead><tr><th scope="col">品种</th>'
        + headers
        + '<th scope="col">数据</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table>"
    )


def _latest_weekday() -> date:
    """Latest Monday-Friday date; holidays still surface their own reason code."""
    today = datetime.now(SHANGHAI).date()
    while today.weekday() >= 5:
        today -= timedelta(days=1)
    return today


def _trading_day(value: str | None) -> str:
    if value is None:
        return _latest_weekday().isoformat()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise HTTPException(422, "日期参数应为 YYYY-MM-DD") from None


def _error_reason(error: BaseException) -> str:
    detail = getattr(error, "detail", None)
    if isinstance(detail, str) and detail:
        return detail
    return "该维度的数据来源暂未完成，未取得数据。"


def _unavailable(dimension: str, reason_code: str, reason: str) -> dict:
    return {
        "dimension": dimension,
        "label": DIMENSION_LABELS[dimension],
        "status": "unavailable",
        "available": False,
        "data_as_of": None,
        "source": None,
        "reason_code": reason_code,
        "reason": reason,
        "values": None,
        "gaps": [],
    }


def _source(value: dict | None) -> dict | None:
    if not isinstance(value, dict):
        return None
    name = value.get("name")
    url = value.get("url")
    if not isinstance(name, str) or not name:
        return None
    return {"name": name, "url": url if isinstance(url, str) else None}


async def _snapshot_dimension(symbol: str) -> dict:
    try:
        value = await futures.snapshot(symbol)
    except Exception as error:  # one unavailable source must not fail the panel
        return _unavailable("snapshot", "snapshot_unavailable", _error_reason(error))
    latest = value.get("latest") if isinstance(value.get("latest"), dict) else {}
    close = latest.get("close")
    series_ok = value.get("series_status") == "collected" and isinstance(close, (int, float))
    series_ok = series_ok and not isinstance(close, bool) and math.isfinite(float(close))
    if not series_ok:
        return _unavailable(
            "snapshot",
            "snapshot_unavailable",
            "连续日线行情暂未取得；不估算收盘价与成交持仓。",
        )
    technical = value.get("technical") if isinstance(value.get("technical"), dict) else {}
    positioning = value.get("positioning") if isinstance(value.get("positioning"), dict) else {}
    return {
        "dimension": "snapshot",
        "label": DIMENSION_LABELS["snapshot"],
        "status": "collected",
        "available": True,
        "data_as_of": value.get("data_as_of") if isinstance(value.get("data_as_of"), str) else None,
        "source": _source(value.get("source")),
        "reason_code": None,
        "reason": None,
        "values": {
            "latest_close": close,
            "quote_unit": futures._QUOTE_UNITS.get(symbol),
            "closes": [
                {"date": bar["date"], "close": bar["close"]}
                for bar in value.get("bars", [])[-120:]
                if isinstance(bar, dict)
                and bar.get("date")
                and type(bar.get("close")) in (float, int)
                and math.isfinite(bar["close"])
            ],
            "change_pct": technical.get("change_pct"),
            "volume": positioning.get("volume"),
            "open_interest": positioning.get("open_interest"),
        },
        "gaps": [gap for gap in (value.get("gaps") or []) if isinstance(gap, str)],
    }


async def _spread_dimension(symbol: str, day: str) -> dict:
    try:
        value = await futures_spread.spread(symbol, day)
    except Exception as error:
        return _unavailable("spread", "spread_unavailable", _error_reason(error))
    if value.get("status") != "collected":
        code = value.get("reason_code") if isinstance(value.get("reason_code"), str) else "unavailable"
        return _unavailable(
            "spread",
            f"spread_unavailable:{code}",
            value.get("reason") if isinstance(value.get("reason"), str) else "该交易日未取得跨期价差。",
        )
    months = value.get("contract_months") if isinstance(value.get("contract_months"), dict) else {}
    listed = months.get("months") if isinstance(months.get("months"), list) else []
    return {
        "dimension": "spread",
        "label": DIMENSION_LABELS["spread"],
        "status": "collected",
        "available": True,
        "data_as_of": value.get("data_as_of") if isinstance(value.get("data_as_of"), str) else day,
        "source": _source(value.get("source")),
        "reason_code": None,
        "reason": None,
        "values": {
            "near_contract": value.get("near_contract"),
            "near_delivery_month": value.get("near_delivery_month"),
            "near_settlement": value.get("near_settlement"),
            "far_contract": value.get("far_contract"),
            "far_delivery_month": value.get("far_delivery_month"),
            "far_settlement": value.get("far_settlement"),
            "far_minus_near": value.get("far_minus_near"),
            "shape": value.get("shape"),
            "shape_label": SHAPE_LABELS.get(value.get("shape")),
            "quote_unit": value.get("quote_unit"),
            "contract_months": [
                {
                    "contract": month.get("contract"),
                    "delivery_month": month.get("delivery_month"),
                    "settlement": month.get("settlement"),
                    "volume": month.get("volume"),
                    "open_interest": month.get("open_interest"),
                }
                for month in listed
                if isinstance(month, dict)
            ],
        },
        "gaps": [],
    }


async def _inventory_dimension(symbol: str) -> dict:
    try:
        value = await futures_inventory.snapshot(symbol)
    except Exception as error:
        return _unavailable("inventory", "inventory_unavailable", _error_reason(error))
    if value.get("status") != "collected":
        code = value.get("error_code")
        reason_code = "inventory_unavailable" if not code else f"inventory_unavailable:{code}"
        return _unavailable(
            "inventory",
            reason_code,
            value.get("reason")
            if isinstance(value.get("reason"), str)
            else "未取得交易所仓单日报；不估算、不补零。",
        )
    return {
        "dimension": "inventory",
        "label": DIMENSION_LABELS["inventory"],
        "status": "collected",
        "available": True,
        "data_as_of": value.get("data_as_of") if isinstance(value.get("data_as_of"), str) else None,
        "source": _source(value.get("source")),
        "reason_code": None,
        "reason": None,
        "values": {
            "quantity": value.get("quantity"),
            "change": value.get("change"),
            "unit": value.get("unit") if isinstance(value.get("unit"), str) else None,
            "segment": value.get("segment") if isinstance(value.get("segment"), str) else None,
            "basis": value.get("basis") if isinstance(value.get("basis"), str) else None,
        },
        "gaps": [gap for gap in (value.get("gaps") or []) if isinstance(gap, str)],
    }


async def _basis_dimension(symbol: str) -> dict:
    try:
        value = await futures_basis.snapshot(symbol)
    except Exception as error:
        return _unavailable("basis", "basis_unavailable", _error_reason(error))
    if value.get("status") != "collected":
        code = value.get("error_code")
        reason_code = "basis_unavailable" if not code else f"basis_unavailable:{code}"
        return _unavailable(
            "basis",
            reason_code,
            value.get("reason")
            if isinstance(value.get("reason"), str)
            else "未取得现货与期货同日报价；不估算、不换算。",
        )
    return {
        "dimension": "basis",
        "label": DIMENSION_LABELS["basis"],
        "status": "collected",
        "available": True,
        "data_as_of": value.get("data_as_of") if isinstance(value.get("data_as_of"), str) else None,
        "source": _source(value.get("source")),
        "reason_code": None,
        "reason": None,
        "values": {
            "spot_price": value.get("spot_price"),
            "futures_contract": value.get("futures_contract"),
            "futures_price": value.get("futures_price"),
            "basis": value.get("basis"),
            "basis_direction": value.get("basis_direction"),
            "basis_label": BASIS_LABELS.get(value.get("basis_direction")),
            "source_published_basis": value.get("source_published_basis"),
            "quote_unit": value.get("quote_unit"),
        },
        "gaps": [gap for gap in (value.get("gaps") or []) if isinstance(gap, str)],
    }


async def panel(symbol: str, trading_day: str | None = None) -> dict:
    """Return the four read-only observations for one product.

    Every dimension is normalized to the same envelope; an unavailable
    dimension carries a visible reason code and ``values: null``. One failing
    source never hides the dimensions that did return data.
    """
    if symbol not in futures.PRODUCTS:
        raise HTTPException(422, "请选择支持的期货品种")
    day = _trading_day(trading_day)
    name, exchange = futures.PRODUCTS[symbol][0], futures.PRODUCTS[symbol][1]
    keys = ("snapshot", "spread", "inventory", "basis")
    results = await asyncio.gather(
        _snapshot_dimension(symbol),
        _spread_dimension(symbol, day),
        _inventory_dimension(symbol),
        _basis_dimension(symbol),
        return_exceptions=True,
    )
    dimensions = {}
    for key, result in zip(keys, results, strict=True):
        if isinstance(result, asyncio.CancelledError):
            raise result
        dimensions[key] = (
            _unavailable(key, f"{key}_unavailable", _error_reason(result))
            if isinstance(result, BaseException)
            else result
        )
    now = datetime.now(SHANGHAI)
    cutoff = (
        min(now, datetime.fromisoformat(day + "T23:59:59").replace(tzinfo=SHANGHAI)) if trading_day else now
    )
    return {
        "symbol": symbol,
        "name": name,
        "exchange": exchange,
        "weekly_research": futures_weekly.research(symbol, cutoff),
        "trading_day": day,
        "generated_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "dimensions": dimensions,
        "notice": PANEL_NOTICE,
    }


router = APIRouter(prefix="/api/futures", tags=["futures"])


@router.get("/panel/catalog")
async def read_panel_catalog(request: Request):
    require_auth(request)
    return catalog()


@router.get("/panel/{symbol}")
async def read_panel(symbol: str, request: Request, date: str | None = None):
    owner = require_auth(request)
    if not check_rate_limit("futures_panel:" + owner, max_req=6, window=60):
        raise HTTPException(429, "查询较频繁，请稍后再试")
    return await panel(symbol, date)


@router.get("/compare/{symbol}")
async def read_comparison(symbol: str, request: Request, start: date, end: date):
    from .futures_comparison import compare

    owner = require_auth(request)
    if not check_rate_limit("futures_compare:" + owner, max_req=6, window=60):
        raise HTTPException(429, "比较较频繁，请稍后再试")
    return await compare(symbol, start, end)
