"""Dated SHFE business reports, shared by the research page and Native tool.

Exchange ratios are not a broker's required margin. The published contract
calendar explicitly excludes holiday adjustments; never certify eligibility or
silently overwrite a user's portfolio terms from these observations.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal

import httpx

from .data_availability import source_failure
from .exchange_futures import LIMIT, _fetch, _number

PRODUCTS = {"RB": "螺纹钢", "CU": "铜", "AU": "黄金", "SC": "原油"}
REPORTS = {"contract_dates": "ContractBaseInfo", "exchange_parameters": "ContractDailyTradeArgument"}
REFERENCE = "https://www.shfe.com.cn/reports/businessdata/prmsummary/"


def report_url(kind: str, day: date):
    return f"https://www.shfe.com.cn/data/busiparamdata/future/{REPORTS[kind]}{day:%Y%m%d}.dat"


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{8}", value):
        raise ValueError("invalid exchange calendar date")
    return datetime.strptime(value, "%Y%m%d").date()


def _rows(raw, kind, symbol, day):
    if not raw or len(raw) > LIMIT or symbol not in PRODUCTS:
        raise ValueError("invalid parameter source")
    doc = json.loads(raw)
    key = REPORTS[kind]
    if (
        not isinstance(doc, dict)
        or doc.get("report_date") != day.strftime("%Y%m%d")
        or not isinstance(doc.get(key), list)
        or len(doc[key]) > 20000
    ):
        raise ValueError("invalid parameter report date or schema")
    rows, seen = [], set()
    for row in doc[key]:
        if not isinstance(row, dict) or not isinstance(row.get("INSTRUMENTID"), str):
            raise ValueError("invalid parameter row")
        ident = row["INSTRUMENTID"].strip()
        if not ident.startswith(symbol.lower()):
            continue
        if not re.fullmatch(re.escape(symbol.lower()) + r"\d{4}", ident):
            raise ValueError("not a concrete delivery contract")
        month = int(ident[-2:])
        year = 2000 + int(ident[-4:-2])
        if not 1 <= month <= 12 or not day.year - 1 <= year <= day.year + 5 or ident in seen:
            raise ValueError("invalid or duplicate parameter contract")
        if _day(row.get("TRADINGDAY")) != day:
            raise ValueError("parameter row date mismatch")
        seen.add(ident)
        rows.append((ident, row))
    if not rows or len(rows) > 60:
        raise ValueError("missing or excessive parameter contracts")
    return sorted(rows), doc.get("update_date")


def _base(raw, kind, symbol, day, updated):
    # Keep only the documented date format, not arbitrary source metadata.
    if not isinstance(updated, str) or not re.fullmatch(r"\d{8} \d{2}:\d{2}:\d{2}", updated):
        raise ValueError("invalid exchange update date")
    stamp = datetime.strptime(updated, "%Y%m%d %H:%M:%S")
    if stamp.date() != day:
        raise ValueError("exchange update date mismatch")
    return {
        "status": "collected",
        "symbol": symbol,
        "data_as_of": day.isoformat(),
        "source_updated_at": stamp.isoformat() + "+08:00",
        "source_url": report_url(kind, day),
        "reference_url": REFERENCE,
        "body_sha256": hashlib.sha256(raw).hexdigest(),
        "broker_terms_status": "not_evaluated",
        "delivery_eligibility_status": "not_evaluated",
        "latest_effective_terms_verified": False,
    }


def parse_contract_dates(raw: bytes, symbol: str, day: date):
    kind = "contract_dates"
    rows, updated = _rows(raw, kind, symbol, day)
    contracts = []
    for ident, row in rows:
        if row.get("COMMODITYID") != symbol.lower() or row.get("COMMODITYNAME") != PRODUCTS[symbol]:
            raise ValueError("parameter product mismatch")
        opening, expiry, start, end = [
            _day(row.get(k)) for k in ("OPENDATE", "EXPIREDATE", "STARTDELIVDATE", "ENDDELIVDATE")
        ]
        if not opening <= day <= expiry <= start <= end or (end - expiry).days > 62:
            raise ValueError("inconsistent contract dates")
        delivery = date(2000 + int(ident[-4:-2]), int(ident[-2:]), 1)
        if start.year != delivery.year or start.month != delivery.month:
            raise ValueError("delivery month mismatch")
        # The official combined report also labels INE instruments EXCHANGEID
        # SHFE. Commodity identity and exact contract, not this field, bind SC.
        contracts.append(
            {
                "contract": ident,
                "listed_on": opening.isoformat(),
                "listed_expiry_date": expiry.isoformat(),
                "delivery_start": start.isoformat(),
                "delivery_end": end.isoformat(),
            }
        )
    return {
        **_base(raw, kind, symbol, day, updated),
        "contracts": contracts,
        "calendar_adjustments_status": "not_evaluated",
        "notice": "官方合约参数表的列示日期；原表注明未含法定节假日调整，实际交易日历、自然人持仓限制及经纪商交割资格须另行核对。",
    }


def parse_exchange_parameters(raw: bytes, symbol: str, day: date):
    kind = "exchange_parameters"
    rows, updated = _rows(raw, kind, symbol, day)
    contracts = []
    for ident, row in rows:
        values = {}
        for key, field in [
            ("spec_long_margin_pct", "SPEC_LONGMARGINRATIO"),
            ("spec_short_margin_pct", "SPEC_SHORTMARGINRATIO"),
            ("hedge_long_margin_pct", "HDEGE_LONGMARGINRATIO"),
            ("hedge_short_margin_pct", "HDEGE_SHORTMARGINRATIO"),
            ("upper_limit_pct", "UPPER_VALUE"),
            ("lower_limit_pct", "LOWER_VALUE"),
        ]:
            raw_value = row.get(field)
            if raw_value in (None, "", "--"):
                values[key] = None
                continue
            value = _number(raw_value)
            if not 0 < value <= 1:
                raise ValueError("invalid exchange ratio")
            values[key] = float(Decimal(str(value)) * 100)
        special = row.get("PRICE_LIMITS")
        if special is not None and (not isinstance(special, str) or len(special) > 120):
            raise ValueError("invalid special limit flag")
        contracts.append(
            {
                "contract": ident,
                **values,
                "additional_limit_notice_required": bool(special and special.strip()),
            }
        )
    return {
        **_base(raw, kind, symbol, day, updated),
        "ratio_unit": "percent",
        "contracts": contracts,
        "notice": "该日期交易所一般/套保持仓买卖保证金率及涨跌停幅度，非经纪商保证金；不含客户附加要求、临时公告核验或实际交割资格，缺失项不按零计算。",
    }


async def _read(client, symbol, day, deadline, kind, parser):
    if symbol not in PRODUCTS:
        return {"status": "not_covered"}
    try:
        raw = await _fetch(client, report_url(kind, day), day, deadline)
        return parser(raw, symbol, day)
    except (
        httpx.HTTPError,
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        TimeoutError,
        RecursionError,
    ) as error:
        return {
            "status": "unavailable",
            "requested_date": day.isoformat(),
            "source_url": report_url(kind, day),
            **source_failure(error),
        }


async def read_contract_dates(client, symbol, day, deadline):
    return await _read(client, symbol, day, deadline, "contract_dates", parse_contract_dates)


async def read_exchange_parameters(client, symbol, day, deadline):
    return await _read(client, symbol, day, deadline, "exchange_parameters", parse_exchange_parameters)
