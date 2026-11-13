"""Bounded DCE public-report adapter; access restrictions remain explicit.

Only the published public HTTP endpoints below are used, without credentials,
cookies, redirects, challenge handling or automatic retries. This is not a TLS
downgrade/fallback: the documented source transport is recorded as unauthenticated.
Field contracts are described by the upstream public AKShare DCE adapters; live
access, actual report values and completeness require independent acceptance.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import date

import httpx

from .data_availability import source_failure
from .exchange_futures import LIMIT, _contract_summary, _number

BASE = "http://www.dce.com.cn/dcereport/publicweb/dailystat/"
URLS = {"daily": BASE + "dayQuotes", "warehouse": BASE + "wbillWeeklyQuotes"}


def _unique(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate exchange field")
        result[key] = value
    return result


def _report(raw, day, kind):
    if not raw or len(raw) > LIMIT:
        raise ValueError("exchange report size")
    value = json.loads(raw, object_pairs_hook=_unique)
    if not isinstance(value, dict) or "data" not in value:
        raise ValueError("exchange report schema")
    if "success" in value and value["success"] is not True:
        raise ValueError("exchange report refused")
    if "code" in value and (
        type(value["code"]) not in {int, str} or value["code"] not in {0, 200, "0", "200", "0000"}
    ):
        raise ValueError("exchange report refused")
    data = value["data"]
    if kind == "warehouse" and not isinstance(data, dict):
        raise ValueError("exchange warehouse schema")
    rows = data.get("entityList") if kind == "warehouse" else data
    if not isinstance(rows, list) or len(rows) > 20000 or any(not isinstance(row, dict) for row in rows):
        raise ValueError("exchange report rows")
    dated = False
    for item in (value, data, *rows):
        if not isinstance(item, dict):
            continue
        for key in ("tradeDate", "trade_date", "reportDate", "report_date"):
            if key not in item or item[key] is None or item[key] == "":
                continue
            observed = item[key]
            if type(observed) not in {str, int} or str(observed) not in {
                day.isoformat(),
                day.strftime("%Y%m%d"),
            }:
                raise ValueError("exchange report date mismatch")
            dated = True
    return rows, "reported_date" if dated else "request_parameter"


def _num(value, *, signed=False, integer=False):
    if isinstance(value, str) and "," in value:
        if not re.fullmatch(r"[+-]?[1-9]\d{0,2}(?:,\d{3})+(?:\.\d{1,8})?", value.strip()):
            raise ValueError("invalid exchange number grouping")
        value = value.strip().replace(",", "")
    return _number(value, signed=signed, integer=integer)


def _metadata(raw, day, kind, provenance):
    return {
        "data_as_of": day.isoformat(),
        "source_url": URLS[kind],
        "source_method": "POST",
        "requested_date": day.isoformat(),
        "date_provenance": provenance,
        "transport_authenticated": False,
        "original_verified": False,
        "body_sha256": hashlib.sha256(raw).hexdigest(),
    }


def parse_daily(raw: bytes, day: date):
    rows, provenance = _report(raw, day, "daily")
    contracts, seen = [], set()
    for row in rows:
        name = row.get("variety")
        if name != "豆粕":
            continue
        contract = row.get("contractId")
        if contract is None or contract == "" or contract in ("小计", "合计", "总计"):
            continue
        if not isinstance(contract, str) or not re.fullmatch(r"[mM]\d{4}", contract):
            raise ValueError("invalid DCE contract")
        contract = contract.lower()
        year, month = 2000 + int(contract[1:3]), int(contract[3:])
        if contract in seen or not 1 <= month <= 12 or not day.year - 1 <= year <= day.year + 5:
            raise ValueError("duplicate or invalid DCE contract month")
        seen.add(contract)

        def price(key, row=row):
            value = row.get(key)
            return None if value in (None, "", "--", "-") else (_num(value) or None)

        contracts.append(
            {
                "contract": contract,
                "delivery_month": f"{year:04d}-{month:02d}",
                "close": price("close"),
                "settlement": price("clearPrice"),
                "volume": _num(row.get("volumn"), integer=True),
                "open_interest": _num(row.get("openInterest"), integer=True),
            }
        )
    if not contracts or len(contracts) > 60:
        raise ValueError("missing DCE contracts")
    notice = "具体合约按接口查询日期返回；成交最活跃仅按该日成交量选取，非交易建议。期限价差不是现货基差，持仓手数不代表资金净流入。公开接口使用 HTTP，传输及原始内容未独立核验。"
    if provenance == "request_parameter":
        notice += "响应未单列日期，所示日期来自查询参数。"
    return {
        **_metadata(raw, day, "daily", provenance),
        **_contract_summary(contracts),
        "status": "collected",
        "quote_unit": "元/吨",
        "basis": {"status": "not_collected", "reason": "missing_same_date_same_grade_spot_price"},
        "contract_risk": {"status": "not_evaluated", "reason": "latest_contract_and_broker_terms_required"},
        "member_positioning": {
            "status": "not_covered",
            "notice": "此接口不提供会员多空排名，不从成交或持仓变化推算资金方向。",
        },
        "notice": notice,
    }


def parse_warehouse(raw: bytes, day: date):
    rows, provenance = _report(raw, day, "warehouse")
    totals = []
    for row in rows:
        name, location = row.get("variety"), row.get("whAbbr")
        if (name == "豆粕" and location in ("小计", "合计", "总计")) or (
            name in ("豆粕小计", "豆粕合计", "豆粕总计") and location in (None, "", "小计", "合计", "总计")
        ):
            totals.append(row)
    if len(totals) != 1:
        raise ValueError("missing or ambiguous DCE warehouse total")
    row = totals[0]
    quantity, previous = _num(row.get("wbillQty"), integer=True), _num(row.get("lastWbillQty"), integer=True)
    change = _num(row.get("diff"), signed=True, integer=True)
    if quantity - previous != change:
        raise ValueError("DCE warehouse change mismatch")
    return {
        **_metadata(raw, day, "warehouse", provenance),
        "status": "collected",
        "table": "豆粕",
        "row": row["variety"] if row["variety"] != "豆粕" else row["whAbbr"],
        "unit": "手",
        "quantity": quantity,
        "change": change,
        "notice": "仅为明确标注的豆粕仓单合计，未换算吨数，不代表社会库存。"
        + ("报告日期见接口回显。" if provenance == "reported_date" else "响应未单列日期，日期来自查询参数。")
        + "公开 HTTP 来源传输与原始内容未独立核验。",
    }


async def _read(client, day, deadline, kind):
    payload = {"tradeDate": day.strftime("%Y%m%d"), "varietyId": "all"}
    if kind == "daily":
        payload.update(contractId="", lang="zh", optionSeries="", statisticsType="0", tradeType="1")
    try:
        if deadline <= asyncio.get_running_loop().time():
            raise TimeoutError()
        if client.auth is not None or any(
            key in client.headers for key in ("authorization", "proxy-authorization", "cookie")
        ):
            raise ValueError("public source client contains authentication")
        async with asyncio.timeout_at(deadline):
            client.cookies.clear()
            raw = bytearray()
            async with client.stream(
                "POST",
                URLS[kind],
                json=payload,
                headers={"Accept-Encoding": "identity"},
                auth=None,
                follow_redirects=False,
            ) as response:
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ValueError("unexpected exchange encoding")
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > LIMIT:
                        raise ValueError("exchange report size")
                    raw.extend(chunk)
            return (parse_daily if kind == "daily" else parse_warehouse)(bytes(raw), day)
    except (
        httpx.HTTPError,
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        TimeoutError,
        RecursionError,
    ) as error:
        failure = source_failure(error)
        if isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 412:
            failure = {"error_code": "automated_access_restricted", "http_status": 412}
        value = {
            "status": "unavailable",
            "requested_date": day.isoformat(),
            "source_url": URLS[kind],
            "source_method": "POST",
            "transport_authenticated": False,
            **failure,
        }
        if kind == "daily":
            value["member_positioning"] = {"status": "not_covered"}
        return value


async def read_daily(client, day, deadline):
    return await _read(client, day, deadline, "daily")


async def read_warehouse(client, day, deadline):
    return await _read(client, day, deadline, "warehouse")
