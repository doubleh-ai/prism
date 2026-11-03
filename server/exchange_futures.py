"""Dated exchange contract settlements and separately ranked member positions.

Only fixed public endpoints are fetched. A settlement curve is an observation,
not a spot basis or an executable spread. Each ranking column has its own
members; their difference cannot identify investor direction or money flow.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation

import httpx

from .data_availability import source_failure

HOSTS = {"RB": "www.shfe.com.cn", "CU": "www.shfe.com.cn", "AU": "www.shfe.com.cn", "SC": "www.ine.cn"}
PRODUCTS = {"RB": ("rb_f", "螺纹钢"), "CU": ("cu_f", "铜"), "AU": ("au_f", "黄金"), "SC": ("sc_f", "原油")}
LIMIT = 2 * 1024 * 1024
PRICE_UNITS = {"RB": "元/吨", "CU": "元/吨", "AU": "元/克", "SC": "元/桶（不含税）"}


def report_url(symbol: str, day: date, *, rankings=False):
    host = HOSTS[symbol]
    return f"https://{host}/data/tradedata/future/dailydata/{'pm' if rankings else 'kx'}{day:%Y%m%d}.dat"


def _number(value, *, signed=False, integer=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("invalid exchange number")
    text = str(value).strip()
    if not re.fullmatch(r"[+-]?\d{1,18}(?:\.\d{1,8})?", text):
        raise ValueError("invalid exchange number")
    try:
        result = Decimal(text)
    except InvalidOperation:
        raise ValueError("invalid exchange number") from None
    if (not signed and result < 0) or abs(result) > 10**15 or (integer and result != int(result)):
        raise ValueError("invalid exchange number")
    return int(result) if integer else float(result)


def _report(raw: bytes, day: date, key: str):
    if not raw or len(raw) > LIMIT:
        raise ValueError("exchange report size")
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or value.get("o_code") != "0000"
        or value.get("report_date") != day.strftime("%Y%m%d")
        or not isinstance(value.get(key), list)
        or len(value[key]) > 20000
        or any(not isinstance(row, dict) for row in value[key])
    ):
        raise ValueError("exchange report date or schema")
    return value[key]


def parse_contracts(raw: bytes, symbol: str, day: date):
    product_id, product_name = PRODUCTS[symbol]
    contracts, seen = [], set()
    for row in _report(raw, day, "o_curinstrument"):
        if row.get("PRODUCTID") != product_id:
            continue
        # Summary and TAS rows are separate products, never delivery contracts.
        month = str(row.get("DELIVERYMONTH", "")).strip()
        if not re.fullmatch(r"\d{4}", month):
            continue
        year, number = 2000 + int(month[:2]), int(month[2:])
        if not 1 <= number <= 12 or not day.year - 1 <= year <= day.year + 5 or month in seen:
            raise ValueError("invalid contract month")
        name = str(row.get("PRODUCTNAME", "")).strip()
        if name not in ({product_name, "中质含硫原油"} if symbol == "SC" else {product_name}):
            raise ValueError("exchange product mismatch")
        seen.add(month)

        def price(key, row=row):
            value = row.get(key)
            return None if value in (None, "", "--") else (_number(value) or None)

        contracts.append(
            {
                "contract": symbol.lower() + month,
                "delivery_month": f"{year:04d}-{number:02d}",
                "close": price("CLOSEPRICE"),
                "settlement": price("SETTLEMENTPRICE"),
                "volume": _number(row.get("VOLUME"), integer=True),
                "open_interest": _number(row.get("OPENINTEREST"), integer=True),
            }
        )
    if not contracts or len(contracts) > 60:
        raise ValueError("missing exchange contracts")
    contracts.sort(key=lambda row: row["delivery_month"])
    # Tie-breaking is deterministic; this is a dated observation, not an order.
    active = max(contracts, key=lambda row: (row["volume"], row["open_interest"], row["contract"]))
    liquid = [row for row in contracts if row["settlement"] and row["volume"] and row["open_interest"]]
    curve = {"status": "insufficient"}
    if len(liquid) >= 2:
        near, far = liquid[:2]
        spread = float(Decimal(str(far["settlement"])) - Decimal(str(near["settlement"])))
        curve = {
            "status": "collected",
            "near_contract": near["contract"],
            "far_contract": far["contract"],
            "near_settlement": near["settlement"],
            "far_settlement": far["settlement"],
            "far_minus_near": spread,
            "shape": "contango" if spread > 0 else "backwardation" if spread < 0 else "flat",
        }
    return {
        "status": "collected",
        "data_as_of": day.isoformat(),
        "source_url": report_url(symbol, day),
        "body_sha256": hashlib.sha256(raw).hexdigest(),
        "contracts": contracts,
        "quote_unit": PRICE_UNITS[symbol],
        "unit_reference_url": "https://www.shfe.com.cn/reports/tradedata/dailyandweeklydata/?query_params=kx",
        "active_contract": active["contract"],
        "active_selection": "largest_reported_daily_volume",
        "curve": curve,
        "basis": {"status": "not_collected", "reason": "missing_same_date_same_grade_spot_price"},
        "contract_risk": {"status": "not_evaluated", "reason": "latest_contract_and_broker_terms_required"},
        "notice": "同日报告的具体合约结算价；远月减近月是价差，不是现货基差。成交最活跃合约只按该日成交量选取，非交易建议。保证金、交割与经纪商规则另行核对。",
    }


def parse_rankings(raw: bytes, symbol: str, day: date, contract: str):
    if not re.fullmatch(re.escape(symbol.lower()) + r"\d{4}", contract):
        raise ValueError("invalid ranking contract")
    rows = [row for row in _report(raw, day, "o_cursor") if row.get("INSTRUMENTID") == contract]
    base = {
        "data_as_of": day.isoformat(),
        "source_url": report_url(symbol, day, rankings=True),
        "contract": contract,
    }
    ranked, ranks = [], set()
    for row in rows:
        rank = _number(row.get("RANK"), signed=True, integer=True)
        if rank in {-1, 0, 999}:
            continue
        if not 1 <= rank <= 20 or rank in ranks:
            raise ValueError("duplicate or invalid rank")
        ranks.add(rank)
        ranked.append((rank, row))
    if not ranked:
        return {**base, "status": "not_reported"}
    if ranks != set(range(1, len(ranks) + 1)):
        raise ValueError("noncontiguous ranking")
    sides = {}
    for column, side in [(1, "volume"), (2, "long"), (3, "short")]:
        members, ids = [], set()
        for rank, row in sorted(ranked):
            name, ident = row.get(f"PARTICIPANTABBR{column}"), row.get(f"PARTICIPANTID{column}")
            if (
                not isinstance(name, str)
                or not 1 <= len(name.strip()) <= 120
                or not isinstance(ident, str)
                or not ident
                or ident in ids
            ):
                raise ValueError("invalid member column")
            ids.add(ident)
            members.append(
                {
                    "rank": rank,
                    "member": name.strip(),
                    "quantity": _number(row.get(f"CJ{column}"), integer=True),
                    "change": _number(row.get(f"CJ{column}_CHG"), signed=True, integer=True),
                }
            )
        sides[side] = {
            "ranked_members": len(members),
            "quantity_sum": sum(row["quantity"] for row in members),
            "change_sum": sum(row["change"] for row in members),
            "members": members,
        }
    return {
        **base,
        "status": "collected",
        "body_sha256": hashlib.sha256(raw).hexdigest(),
        "sides": sides,
        "notice": "分别为成交、持买、持卖排名各列所披露会员的手数合计；三列会员集合不同，未披露会员不视为零。排名变化含样本进出，不等于同一会员持仓变化或资金净流向；不据此推断全市场方向。",
    }


async def _fetch(client, url, day, deadline):
    if deadline <= asyncio.get_running_loop().time():
        raise TimeoutError()
    async with asyncio.timeout_at(deadline):
        raw = bytearray()
        client.cookies.clear()
        async with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
            response.raise_for_status()
            if response.headers.get("content-encoding", "identity").casefold() != "identity":
                raise ValueError("exchange content encoding")
            async for chunk in response.aiter_bytes():
                if len(raw) + len(chunk) > LIMIT:
                    raise ValueError("exchange report size")
                raw.extend(chunk)
        return bytes(raw)


async def read_daily(client, symbol, day, deadline):
    if symbol not in HOSTS:
        return {"status": "not_covered", "member_positioning": {"status": "not_covered"}}
    errors = (httpx.HTTPError, ValueError, TypeError, KeyError, OverflowError, TimeoutError, RecursionError)
    try:
        raw = await _fetch(client, report_url(symbol, day), day, deadline)
        value = parse_contracts(raw, symbol, day)
    except errors as error:
        return {
            "status": "unavailable",
            "requested_date": day.isoformat(),
            "source_url": report_url(symbol, day),
            "member_positioning": {"status": "unavailable"},
            **source_failure(error),
        }
    try:
        ranks = await _fetch(client, report_url(symbol, day, rankings=True), day, deadline)
        value["member_positioning"] = parse_rankings(ranks, symbol, day, value["active_contract"])
    except errors as error:
        value["member_positioning"] = {
            "status": "unavailable",
            "requested_date": day.isoformat(),
            "source_url": report_url(symbol, day, rankings=True),
            "contract": value["active_contract"],
            **source_failure(error),
        }
    return value
