"""Optional zero-dependency HTTP JSON-RPC adapter for Tushare Pro institutional futures data.

When ``PRISM_TUSHARE_TOKEN`` (or ``TUSHARE_TOKEN``) is configured in the environment, this module
provides clean, anti-crawler-free fallback reads for contract settlements (`fut_daily`), warehouse
warrants (`fut_wsr`) and top-20 member holdings (`fut_holding`) over standard ``httpx`` without
adding external SDK packages to ``requirements.lock``.

When no token is configured (including offline CI and default public-source deployments),
``configured()`` is ``False`` and zero network calls are attempted.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from datetime import date

import httpx

DEFAULT_ENDPOINT = "https://api.tushare.pro"
SOURCE_NAME = "Tushare Pro 量化数据接口"
EXCHANGE_MAP = {
    "SHFE": "SHF",
    "INE": "INE",
    "DCE": "DCE",
    "CZCE": "ZCE",
    "GFEX": "GFE",
    "CFFEX": "CFX",
}
STREAM_LIMIT = 2 * 1024 * 1024


def _token() -> str:
    return (os.environ.get("PRISM_TUSHARE_TOKEN") or os.environ.get("TUSHARE_TOKEN") or "").strip()


def endpoint() -> str:
    url = (os.environ.get("PRISM_TUSHARE_URL") or DEFAULT_ENDPOINT).strip()
    return url if url.startswith(("https://", "http://")) else DEFAULT_ENDPOINT


def configured() -> bool:
    tok = _token()
    return 16 <= len(tok) <= 128 and not any(ch.isspace() for ch in tok)


def provider_metadata() -> dict:
    active = configured()
    return {
        "id": "tushare_pro_http",
        "name": SOURCE_NAME,
        "configured": active,
        "mode": "institutional_quant_hybrid" if active else "public_verified_sources",
        "ZeroDependencyHttp": True,
    }


def _rows_from_payload(raw: bytes) -> list[dict]:
    if not raw or len(raw) > STREAM_LIMIT:
        raise ValueError("tushare payload size")
    doc = json.loads(raw.decode("utf-8"))
    if not isinstance(doc, dict) or doc.get("code") != 0:
        raise ValueError(f"tushare error: {doc.get('msg') if isinstance(doc, dict) else 'bad payload'}")
    data = doc.get("data")
    if not isinstance(data, dict):
        raise ValueError("tushare data missing")
    fields = data.get("fields")
    items = data.get("items")
    if (
        not isinstance(fields, list)
        or not isinstance(items, list)
        or any(not isinstance(f, str) for f in fields)
    ):
        raise ValueError("tushare table schema")
    out = []
    for item in items:
        if not isinstance(item, list) or len(item) != len(fields):
            raise ValueError("tushare row length")
        out.append(dict(zip(fields, item, strict=True)))
    return out


async def _query(
    client: httpx.AsyncClient, api_name: str, params: dict, fields: str, deadline: float
) -> list[dict]:
    tok = _token()
    if not tok:
        raise RuntimeError("tushare token not configured")
    body = json.dumps(
        {"api_name": api_name, "token": tok, "params": params, "fields": fields},
        separators=(",", ":"),
    ).encode("utf-8")
    buf = bytearray()
    async with asyncio.timeout_at(deadline):
        async with client.stream(
            "POST",
            endpoint(),
            content=body,
            headers={"Content-Type": "application/json", "Accept-Encoding": "identity"},
        ) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes():
                if len(buf) + len(chunk) > STREAM_LIMIT:
                    raise ValueError("tushare stream limit")
                buf.extend(chunk)
    return _rows_from_payload(bytes(buf))


def _delivery_month(contract_code: str, symbol: str, day: date) -> tuple[str, str] | None:
    clean = contract_code.split(".", 1)[0].strip().upper()
    m = re.fullmatch(re.escape(symbol.upper()) + r"(\d{3,4})", clean)
    if not m:
        return None
    digits = m.group(1)
    if len(digits) == 4:
        year, month = 2000 + int(digits[:2]), int(digits[2:])
    else:
        year = (day.year // 10) * 10 + int(digits[0])
        if year < day.year - 1:
            year += 10
        month = int(digits[1:])
    if not (1 <= month <= 12 and day.year - 1 <= year <= day.year + 5):
        return None
    return symbol.lower() + digits, f"{year:04d}-{month:02d}"


def _finite_pos(val) -> float | None:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    f = float(val)
    return f if math.isfinite(f) and f > 0 else None


def _finite_nonneg_int(val) -> int | None:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    f = float(val)
    return int(round(f)) if math.isfinite(f) and f >= 0 else None


async def read_daily_contracts(
    client: httpx.AsyncClient, symbol: str, exchange: str, quote_unit: str, day: date, deadline: float
) -> dict:
    """Read dated per-contract settlements for ``symbol`` on ``day`` via ``fut_daily``."""
    ts_ex = EXCHANGE_MAP.get(exchange)
    if not configured() or not ts_ex:
        return {"status": "unavailable", "requested_date": day.isoformat(), "error_code": "not_configured"}
    stamp = day.strftime("%Y%m%d")
    try:
        rows = await _query(
            client,
            "fut_daily",
            {"trade_date": stamp, "exchange": ts_ex},
            "ts_code,trade_date,close,settle,vol,oi",
            deadline,
        )
    except (httpx.HTTPError, ValueError, TimeoutError, RuntimeError):
        return {"status": "unavailable", "requested_date": day.isoformat(), "error_code": "provider_error"}
    if not rows:
        return {"status": "unavailable", "requested_date": day.isoformat(), "error_code": "report_not_found"}
    contracts = []
    seen = set()
    for row in rows:
        if str(row.get("trade_date") or "") != stamp:
            continue
        parsed = _delivery_month(str(row.get("ts_code") or ""), symbol, day)
        if not parsed:
            continue
        code, deliv = parsed
        if code in seen:
            continue
        seen.add(code)
        contracts.append(
            {
                "contract": code,
                "delivery_month": deliv,
                "close": _finite_pos(row.get("close")),
                "settlement": _finite_pos(row.get("settle")),
                "volume": _finite_nonneg_int(row.get("vol")),
                "open_interest": _finite_nonneg_int(row.get("oi")),
            }
        )
    if not contracts:
        return {"status": "unavailable", "requested_date": day.isoformat(), "error_code": "report_not_found"}
    contracts.sort(key=lambda c: c["delivery_month"])
    return {
        "status": "collected",
        "requested_date": day.isoformat(),
        "data_as_of": day.isoformat(),
        "date_provenance": "reported_date",
        "source_url": "https://tushare.pro/document/2?doc_id=138",
        "source_name": SOURCE_NAME,
        "quote_unit": quote_unit,
        "contracts": contracts,
        "member_positioning": {"status": "not_covered"},
        "notice": "由已配置 Tushare Pro 量化接口按交易日精确核对的合约收盘与结算价。",
    }


async def read_warrant(
    client: httpx.AsyncClient, symbol: str, exchange: str, day: date, deadline: float
) -> dict:
    """Read dated warehouse warrant total for ``symbol`` on ``day`` via ``fut_wsr``."""
    ts_ex = EXCHANGE_MAP.get(exchange)
    if not configured() or not ts_ex:
        return {
            "status": "inventory_unavailable",
            "requested_date": day.isoformat(),
            "error_code": "not_configured",
        }
    stamp = day.strftime("%Y%m%d")
    try:
        rows = await _query(
            client,
            "fut_wsr",
            {"trade_date": stamp, "symbol": symbol.upper(), "exchange": ts_ex},
            "trade_date,symbol,warehouse,vol,vol_chg,unit",
            deadline,
        )
    except (httpx.HTTPError, ValueError, TimeoutError, RuntimeError):
        return {
            "status": "inventory_unavailable",
            "requested_date": day.isoformat(),
            "error_code": "http_error",
        }
    valid = [
        r
        for r in rows
        if str(r.get("trade_date") or "") == stamp and str(r.get("symbol") or "").upper() == symbol.upper()
    ]
    if not valid:
        return {
            "status": "inventory_unavailable",
            "requested_date": day.isoformat(),
            "error_code": "report_not_found",
        }
    units = {str(r.get("unit") or "").strip() for r in valid if str(r.get("unit") or "").strip()}
    if len(units) != 1:
        return {
            "status": "inventory_unavailable",
            "requested_date": day.isoformat(),
            "error_code": "invalid_source_data",
        }
    unit = next(iter(units))
    total_vol = 0
    total_chg = 0
    for r in valid:
        v = _finite_nonneg_int(r.get("vol"))
        chg = r.get("vol_chg")
        if v is None or isinstance(chg, bool) or not isinstance(chg, (int, float)) or not math.isfinite(chg):
            return {
                "status": "inventory_unavailable",
                "requested_date": day.isoformat(),
                "error_code": "invalid_source_data",
            }
        total_vol += v
        total_chg += int(round(float(chg)))
    return {
        "status": "collected",
        "data_as_of": day.isoformat(),
        "source_name": SOURCE_NAME,
        "source_url": "https://tushare.pro/document/2?doc_id=140",
        "segment": f"{symbol.upper()} 汇总仓单",
        "basis": "总计",
        "unit": unit,
        "quantity": total_vol,
        "change": total_chg,
    }
