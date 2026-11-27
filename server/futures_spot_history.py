"""Daily spot-price history from the 100ppi (生意社) spot-versus-futures table.

``server.futures_basis`` reads one dated table row and returns the same-day
basis for one product. This module walks a bounded date range of the *same*
public table through the *same* ``HW_CHECK`` flow and returns the observed
spot-price series, so the numbers a report cites for a spot series can be
re-read from the dated page that printed them.

Only dates the source actually lists are returned: non-trading days, empty
pages and days where the table does not list the product are omitted, never
interpolated, zero-filled or replaced by another date. Prices are returned in
the product's futures quote unit. The one documented conversion (glass:
``元/平方米 * 80 = 元/吨``, printed on the source chart page) is applied
explicitly and disclosed in ``source_unit``/``unit_conversion``; every
observation is additionally cross-checked against the same row's main-contract
futures price so a mislabelled source unit fails as ``unit_unverified``
instead of silently mixing units.
"""

from __future__ import annotations

import asyncio
import copy
import math
import re
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx

from . import futures, futures_basis, public_network
from .data_availability import source_failure

SHANGHAI = ZoneInfo("Asia/Shanghai")

SOURCE_NAME = futures_basis.SOURCE_NAME
SOURCE_KIND = "spot_price_history"
ACCESS_METHOD = "futures_spot_history"

#: The dated table carries real product rows from 2013-01-04 onward; earlier
#: pages exist but only say ``暂无数据``.
MIN_SOURCE_DATE = date(2013, 1, 4)
MAX_SPAN_DAYS = 1000
MAX_OBSERVATIONS = 720
MIN_SAMPLES = 250
MIN_SPAN_DAYS = 365
DOC_PAGE_ROWS = 200
TOTAL_SECONDS = 45
REQUEST_SECONDS = 8
CLIENT_TIMEOUT = 8
_FETCH_SLOTS = 2
_RATIO_TOLERANCE = (0.5, 2.0)

NOTICE = (
    "本页为生意社现期表公开现货价的日频历史；只返回来源当日实际列出的日期，"
    "非交易日、休市日与来源未列出的日期不插值、不补零、不用其他日期替代。"
    "价格换算到该品种期货报价单位，换算依据在 source_unit/unit_conversion 中显式披露；"
    "未做估算、平滑或用其他数据源替代。"
)

_CACHE_TTL = futures_basis._CACHE_TTL
_CACHE_PARTIAL_TTL = futures_basis._CACHE_PARTIAL_TTL
_STATIC_TTL = futures_basis._STATIC_TTL
_CACHE: dict[tuple[str, str, str], tuple[float, int, dict]] = {}

#: Spot unit printed for each mapped product. The daily table's ``单位:元/吨``
#: banner is not reliable (gold is 元/克, silver 元/千克, glass 元/平方米), so
#: every mapped product names its unit explicitly here; nothing is assumed.
_SPOT_UNITS = {symbol: "元/吨" for symbol in futures_basis.BASIS_PRODUCTS}
_SPOT_UNITS.update({"AU": "元/克", "AG": "元/千克", "FG": "元/平方米"})

#: Only conversions that the sources themselves document are applied.
_UNIT_CONVERSIONS = {
    ("元/平方米", "元/吨"): (80.0, "元/平方米*80=元/吨"),
    ("元/吨", "元/公斤"): (0.001, "元/吨/1000=元/公斤"),
    ("元/公斤", "元/吨"): (1000.0, "元/公斤*1000=元/吨"),
}
_CONVERSION_EVIDENCE = {"FG": "https://www.100ppi.com/sf/959.html"}

_CONTRACT_MONTH = re.compile(r"^([A-Z]{1,3})(\d{2,4})$")
_CONTINUOUS = re.compile(r"^([A-Z]{1,3})0$")
_BASE = re.compile(r"^([A-Z]{1,3})")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def convert_price(value, source_unit: str, target_unit: str, *, symbol: str | None = None) -> float:
    """Convert one spot price between documented units.

    Raises ``ValueError`` when no documented conversion exists; a caller must
    never guess a unit. ``symbol`` is accepted for error messages only.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("现货价格无效")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("现货价格无效")
    if not isinstance(source_unit, str) or not isinstance(target_unit, str):
        raise ValueError("现货单位无效")
    if source_unit == target_unit:
        return number
    rule = _UNIT_CONVERSIONS.get((source_unit, target_unit))
    if rule is None:
        label = f"（{symbol}）" if symbol else ""
        raise ValueError(f"缺少{source_unit}到{target_unit}的换算规则{label}")
    return float(Decimal(str(value)) * Decimal(str(rule[0])))


def unit_plan(symbol: str) -> dict | None:
    """Return the explicit spot-unit conversion plan for one mapped product."""
    if symbol not in futures_basis.BASIS_PRODUCTS:
        return None
    source_unit = _SPOT_UNITS.get(symbol)
    canonical = futures._QUOTE_UNITS.get(symbol, "元/吨").split("（", 1)[0]
    if not source_unit or not canonical:
        return None
    if source_unit == canonical:
        return {
            "source_unit": source_unit,
            "unit": canonical,
            "factor": 1.0,
            "formula": None,
            "evidence_url": None,
        }
    rule = _UNIT_CONVERSIONS.get((source_unit, canonical))
    if rule is None:
        return None
    factor, formula = rule
    return {
        "source_unit": source_unit,
        "unit": canonical,
        "factor": factor,
        "formula": formula,
        "evidence_url": _CONVERSION_EVIDENCE.get(symbol),
    }


def covered_symbols() -> list[str]:
    """Mapped products whose spot unit is known and convertible to the quote unit."""
    return sorted(symbol for symbol in futures_basis.BASIS_PRODUCTS if unit_plan(symbol) is not None)


def _today() -> date:
    return datetime.now(SHANGHAI).date()


def _requested_day(value: str | None, label: str) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str) or _DAY.fullmatch(value) is None:
        raise ValueError(f"{label}格式应为 YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{label}格式应为 YYYY-MM-DD") from None


def _base_symbol(text: str) -> str | None:
    matched = _BASE.match(text)
    return matched.group(1) if matched else None


def _variety(value) -> str | None:
    """Map ``contract|variety`` to a futures symbol; ``None`` for a dated contract.

    ``CU``/``CU0`` resolve to ``CU``; a specific contract such as ``CU2701`` or
    ``RB2505`` returns ``None`` because spot is not contract-specific.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("请选择支持的期货品种")
    text = value.strip().upper()
    if text in futures.PRODUCTS:
        return text
    matched = _CONTINUOUS.fullmatch(text)
    if matched is not None and matched.group(1) in futures.PRODUCTS:
        return matched.group(1)
    if _CONTRACT_MONTH.fullmatch(text) is not None:
        base = _base_symbol(text)
        if base is not None and base in futures.PRODUCTS:
            return None
    raise ValueError("请选择支持的期货品种")


def _identity(symbol: str, requested: str) -> dict:
    name, exchange = futures.PRODUCTS[symbol][0], futures.PRODUCTS[symbol][1]
    return {
        "symbol": symbol,
        "requested_contract": requested,
        "name": name,
        "exchange": exchange,
    }


def _remember(key: tuple[str, str, str], ttl: int, value: dict) -> None:
    _CACHE[key] = (time.monotonic(), ttl, copy.deepcopy(value))


def _cached(key: tuple[str, str, str]) -> dict | None:
    cached = _CACHE.get(key)
    if cached is None:
        return None
    stamp, ttl, value = cached
    if time.monotonic() - stamp < ttl:
        return {**copy.deepcopy(value), "cached": True}
    _CACHE.pop(key, None)
    return None


def _unavailable(
    identity: dict,
    *,
    error_code: str,
    reason: str,
    start: date | None,
    end: date | None,
    plan: dict | None = None,
    attempts: list[dict] | None = None,
    coverage: dict | None = None,
    gaps: list[str] | None = None,
    ttl: int = _CACHE_PARTIAL_TTL,
    extra: dict | None = None,
) -> dict:
    return {
        **identity,
        "status": "spot_history_unavailable",
        "requested_start": start.isoformat() if start else None,
        "requested_end": end.isoformat() if end else None,
        "error_code": error_code,
        "reason": reason,
        "unit": plan["unit"] if plan else None,
        "source_unit": plan["source_unit"] if plan else None,
        "canonical_unit": plan["unit"] if plan else None,
        "unit_conversion": None,
        "observations": [],
        "sample_count": 0,
        "first_date": None,
        "last_date": None,
        "span_days": 0,
        "backtest_ready": False,
        "backtest_ready_reasons": ["spot_history_unavailable"],
        "backtest_requirements": {"min_samples": MIN_SAMPLES, "min_span_days": MIN_SPAN_DAYS},
        "coverage": coverage or {},
        "source_attempts": attempts or [],
        "covered_symbols": covered_symbols(),
        "source": None,
        "access_method": ACCESS_METHOD,
        "collected_at": None,
        "notice": NOTICE,
        "gaps": gaps or ["未取得现货价序列；不估算、不插值、不换算"],
        "cache_ttl_seconds": ttl,
        "cached": False,
        **(extra or {}),
    }


def _weekdays(start: date, end: date) -> list[date]:
    days: list[date] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


async def _read_day(client, semaphore: asyncio.Semaphore, symbol: str, day: date, deadline: float) -> dict:
    """Read one dated page and classify it: row / no_publication / not_listed / failure."""
    loop = asyncio.get_running_loop()
    url = futures_basis.page_url(day)
    parsed = None
    last_error = None
    for attempt in range(2):
        try:
            async with semaphore:
                raw = await futures_basis._table_page(
                    client, url, min(deadline, loop.time() + REQUEST_SECONDS)
                )
            parsed = await asyncio.to_thread(futures_basis.parse_basis_page, raw, day)
            break
        except futures_basis._Challenge:
            return {
                "date": day.isoformat(),
                "url": url,
                "status": "challenge",
                "error_code": "challenge_unsolved",
            }
        except (TimeoutError, httpx.TimeoutException):
            return {"date": day.isoformat(), "url": url, "status": "timeout", "error_code": "source_timeout"}
        except (ValueError, RecursionError) as error:
            # A page whose table can not be recognised is not a substitute for a
            # price: the day is skipped and disclosed, never fabricated.
            return {
                "date": day.isoformat(),
                "url": url,
                "status": "invalid_source_data",
                "error_code": source_failure(error)["error_code"],
            }
        except httpx.HTTPError as error:
            last_error = error
            if attempt == 0 and loop.time() < deadline:
                continue
            return {
                "date": day.isoformat(),
                "url": url,
                "status": "error",
                "error_code": source_failure(error)["error_code"],
            }
    if parsed is None:
        return {
            "date": day.isoformat(),
            "url": url,
            "status": "error",
            "error_code": source_failure(last_error if last_error is not None else ValueError()).get(
                "error_code", "invalid_source_data"
            ),
        }
    if not parsed["products"]:
        return {"date": day.isoformat(), "url": url, "status": "no_publication"}
    group, name = futures_basis.BASIS_PRODUCTS[symbol]
    row = parsed["products"].get(name)
    if row is None:
        return {"date": day.isoformat(), "url": url, "status": "not_listed"}
    spot = row["spot_price"]
    main = row["main_price"]
    if row["group"] != group or not _positive(spot) or not _positive(main):
        return {"date": day.isoformat(), "url": url, "status": "error", "error_code": "invalid_source_data"}
    return {
        "date": day.isoformat(),
        "url": url,
        "status": "row",
        "spot": float(spot),
        "futures": float(main),
    }


def _positive(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value)) and float(value) > 0


def _attempts(results: list[dict]) -> list[dict]:
    return [
        {
            "source": SOURCE_NAME,
            "date": result["date"],
            "status": result["status"],
            "error_code": result.get("error_code"),
        }
        for result in results
        if result["status"] != "row"
    ][:8]


def _coverage(start: date, end: date, days: list[date], results: list[dict]) -> dict:
    listed = sum(1 for result in results if result["status"] == "row")
    unlisted = sum(1 for result in results if result["status"] == "not_listed")
    empty = sum(1 for result in results if result["status"] == "no_publication")
    invalid = sum(1 for result in results if result["status"] == "invalid_source_data")
    return {
        "calendar_days": (end - start).days + 1,
        "weekdays_requested": len(days),
        "published_days": listed + unlisted,
        "listed_days": listed,
        "unlisted_product_days": unlisted,
        "no_publication_days": empty,
        "invalid_source_days": invalid,
    }


def _gap_notes(coverage: dict) -> list[str]:
    return [
        (
            f"请求区间共 {coverage['weekdays_requested']} 个工作日，来源发布数据 "
            f"{coverage['published_days']} 日，其中列出该品种 {coverage['listed_days']} 日；"
            f"未列出该品种 {coverage['unlisted_product_days']} 日，"
            f"来源未发布数据 {coverage['no_publication_days']} 日，"
            f"来源页面结构无法识别 {coverage['invalid_source_days']} 日（该日不返回）。"
        ),
        "缺失日期不插值、不补零、不用其他日期或其他来源替代。",
    ]


def _readiness(sample_count: int, span_days: int) -> tuple[bool, list[str], list[str]]:
    reasons: list[str] = []
    notes: list[str] = []
    if sample_count < MIN_SAMPLES:
        reasons.append("insufficient_observations")
        notes.append(f"样本数 {sample_count} 少于 {MIN_SAMPLES}")
    if span_days < MIN_SPAN_DAYS:
        reasons.append("span_too_short")
        notes.append(f"首末跨度 {span_days} 天少于 {MIN_SPAN_DAYS} 天")
    return (not reasons), reasons, notes


async def history(
    contract_or_variety: str, start_date: str | None = None, end_date: str | None = None
) -> dict:
    """Return the observed daily spot-price series for one covered product.

    ``contract_or_variety`` accepts a variety code (``CU``) or a continuous
    contract (``CU0``); a dated contract such as ``CU2701`` is rejected because
    spot prices are not contract-specific. ``start_date``/``end_date`` are
    ``YYYY-MM-DD`` bounds (default: the last 365 days ending yesterday).
    """
    requested = contract_or_variety if isinstance(contract_or_variety, str) else ""
    start_requested = _requested_day(start_date, "开始日期")
    end_requested = _requested_day(end_date, "结束日期")
    if start_requested is not None and end_requested is not None and start_requested > end_requested:
        raise ValueError("开始日期不能晚于结束日期")
    variety = _variety(contract_or_variety)
    today = _today()
    end = end_requested or today - timedelta(days=1)
    start = start_requested or end - timedelta(days=MIN_SPAN_DAYS)
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    cache_key = (variety or requested.strip().upper(), start.isoformat(), end.isoformat())
    cached = _cached(cache_key)
    if cached is not None:
        return cached

    if variety is None:
        base = _base_symbol(requested.strip().upper())
        identity = (
            _identity(base, requested)
            if base in futures.PRODUCTS
            else {
                "symbol": None,
                "requested_contract": requested,
                "name": None,
                "exchange": None,
            }
        )
        value = _unavailable(
            identity,
            error_code="contract_not_supported",
            reason="现货价为品种口径，不支持具体月份合约；请使用品种代码或连续合约代码",
            start=start,
            end=end,
            ttl=_STATIC_TTL,
        )
        _remember(cache_key, _STATIC_TTL, value)
        return value

    identity = _identity(variety, requested)
    plan = unit_plan(variety)
    if variety not in futures_basis.BASIS_PRODUCTS:
        reason = futures_basis.UNCOVERED_REASONS.get(
            variety, "该品种不在生意社现期表映射范围内，未取得现货历史价"
        )
        value = _unavailable(
            identity,
            error_code="symbol_not_covered",
            reason=reason,
            start=start,
            end=end,
            ttl=_STATIC_TTL,
        )
        _remember(cache_key, _STATIC_TTL, value)
        return value
    if plan is None:
        value = _unavailable(
            identity,
            error_code="unit_incompatible",
            reason="该品种现货单位与期货报价单位不一致且没有来源披露的换算规则，未换算、未返回序列",
            start=start,
            end=end,
            ttl=_STATIC_TTL,
            gaps=["现货单位与期货报价单位不可比；不静默混用单位"],
        )
        _remember(cache_key, _STATIC_TTL, value)
        return value

    if end > today:
        value = _unavailable(
            identity,
            error_code="invalid_range",
            reason="结束日期不能晚于今天",
            start=start,
            end=end,
            plan=plan,
            ttl=_STATIC_TTL,
        )
        _remember(cache_key, _STATIC_TTL, value)
        return value
    if start < MIN_SOURCE_DATE:
        value = _unavailable(
            identity,
            error_code="range_before_source_history",
            reason=f"生意社现期表从 {MIN_SOURCE_DATE.isoformat()} 起才有真实品种数据，更早的页面只有“暂无数据”",
            start=start,
            end=end,
            plan=plan,
            ttl=_STATIC_TTL,
            gaps=[
                f"可取得的最早数据日期为 {MIN_SOURCE_DATE.isoformat()}；不早于该日期取数，不用其他来源替代"
            ],
        )
        _remember(cache_key, _STATIC_TTL, value)
        return value
    if (end - start).days + 1 > MAX_SPAN_DAYS:
        value = _unavailable(
            identity,
            error_code="range_too_large",
            reason=f"单次查询跨度不能超过 {MAX_SPAN_DAYS} 天；请缩小日期范围后分段查询",
            start=start,
            end=end,
            plan=plan,
            ttl=_STATIC_TTL,
        )
        _remember(cache_key, _STATIC_TTL, value)
        return value

    days = _weekdays(start, end)
    if len([day for day in days if day >= MIN_SOURCE_DATE]) > MAX_OBSERVATIONS:
        value = _unavailable(
            identity,
            error_code="range_too_large",
            reason=f"请求区间工作日过多（上限 {MAX_OBSERVATIONS} 个观测），请缩小日期范围",
            start=start,
            end=end,
            plan=plan,
            ttl=_STATIC_TTL,
        )
        _remember(cache_key, _STATIC_TTL, value)
        return value

    loop = asyncio.get_running_loop()
    deadline = loop.time() + TOTAL_SECONDS
    semaphore = asyncio.Semaphore(_FETCH_SLOTS)
    try:
        async with public_network.async_client(timeout=CLIENT_TIMEOUT) as client:
            results = await asyncio.gather(
                *(_read_day(client, semaphore, variety, day, deadline) for day in days)
            )
    except (TimeoutError, httpx.HTTPError) as error:
        coverage = {
            "calendar_days": (end - start).days + 1,
            "weekdays_requested": len(days),
            "published_days": 0,
            "listed_days": 0,
            "unlisted_product_days": 0,
            "no_publication_days": 0,
        }
        value = _unavailable(
            identity,
            error_code=source_failure(error)["error_code"],
            reason="读取生意社现期表历史页面失败，未取得序列",
            start=start,
            end=end,
            plan=plan,
            coverage=coverage,
        )
        _remember(cache_key, _CACHE_PARTIAL_TTL, value)
        return value

    attempts = _attempts(results)
    coverage = _coverage(start, end, days, results)
    failures = [result for result in results if result["status"] in {"challenge", "timeout", "error"}]
    if failures:
        error_code = failures[0].get("error_code") or "invalid_source_data"
        value = _unavailable(
            identity,
            error_code=error_code,
            reason=(
                "读取生意社现期表历史页面时出现失败日期（"
                f"{len(failures)}/{len(days)} 日）：{futures_basis.FAILURE_REASONS.get(error_code, '来源数据不可用')}；"
                "为保证序列完整，本次不返回部分序列"
            ),
            start=start,
            end=end,
            plan=plan,
            attempts=attempts,
            coverage=coverage,
            gaps=["任一日期的读取失败即整次不可用，不返回部分序列、不用其他日期替代"],
        )
        _remember(cache_key, _CACHE_PARTIAL_TTL, value)
        return value

    if coverage["published_days"] == 0:
        if coverage["invalid_source_days"]:
            value = _unavailable(
                identity,
                error_code="invalid_source_data",
                reason="请求区间内的生意社现期表页面结构无法识别，未取得可用于核对的现货价",
                start=start,
                end=end,
                plan=plan,
                attempts=attempts,
                coverage=coverage,
            )
        else:
            value = _unavailable(
                identity,
                error_code="report_not_found",
                reason="请求区间内生意社现期表没有发布数据",
                start=start,
                end=end,
                plan=plan,
                attempts=attempts,
                coverage=coverage,
            )
        _remember(cache_key, _CACHE_PARTIAL_TTL, value)
        return value

    rows = [result for result in results if result["status"] == "row"]
    if not rows:
        value = _unavailable(
            identity,
            error_code="product_not_listed",
            reason="请求区间内生意社现期表发布数据，但未列出该品种",
            start=start,
            end=end,
            plan=plan,
            attempts=attempts,
            coverage=coverage,
        )
        _remember(cache_key, _CACHE_PARTIAL_TTL, value)
        return value

    observations: list[dict] = []
    ratios: list[float] = []
    unverified: list[str] = []
    for result in rows:
        try:
            price = convert_price(result["spot"], plan["source_unit"], plan["unit"], symbol=variety)
        except ValueError:
            unverified.append(result["date"])
            continue
        ratio = price / result["futures"]
        if not (_RATIO_TOLERANCE[0] <= ratio <= _RATIO_TOLERANCE[1]):
            unverified.append(result["date"])
            continue
        ratios.append(ratio)
        observations.append(
            {
                "date": result["date"],
                "price": price,
                "unit": plan["unit"],
                "source": SOURCE_NAME,
                "access_method": ACCESS_METHOD,
            }
        )
    if unverified:
        value = _unavailable(
            identity,
            error_code="unit_unverified",
            reason=(
                f"{len(unverified)} 个日期的现货价与同日主力期货价量级不一致，无法确认现货单位；"
                "未换算、未返回可能混用单位的序列"
            ),
            start=start,
            end=end,
            plan=plan,
            attempts=attempts,
            coverage=coverage,
            extra={"unverified_dates": unverified[:8], "ratio_tolerance": list(_RATIO_TOLERANCE)},
            gaps=[
                "现货价与同日主力期货价的比值超出 "
                f"[{_RATIO_TOLERANCE[0]}, {_RATIO_TOLERANCE[1]}]，按单位不可核对处理"
            ],
        )
        _remember(cache_key, _CACHE_PARTIAL_TTL, value)
        return value
    if not observations:
        value = _unavailable(
            identity,
            error_code="invalid_source_data",
            reason="现货价换算后没有可用观测",
            start=start,
            end=end,
            plan=plan,
            attempts=attempts,
            coverage=coverage,
        )
        _remember(cache_key, _CACHE_PARTIAL_TTL, value)
        return value

    observations.sort(key=lambda item: item["date"])
    first_date = observations[0]["date"]
    last_date = observations[-1]["date"]
    # Inclusive calendar span: a series whose first and last observations are
    # 2024-01-02 and 2024-12-31 covers 365 calendar days, i.e. one full year.
    span_days = (date.fromisoformat(last_date) - date.fromisoformat(first_date)).days + 1
    ready, reasons, notes = _readiness(len(observations), span_days)
    value = {
        **identity,
        "status": "collected",
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "unit": plan["unit"],
        "source_unit": plan["source_unit"],
        "canonical_unit": plan["unit"],
        "unit_conversion": (
            {
                "factor": plan["factor"],
                "formula": plan["formula"],
                "evidence_url": plan["evidence_url"],
            }
            if plan["formula"]
            else None
        ),
        "unit_checks": {
            "spot_futures_ratio_min": min(ratios),
            "spot_futures_ratio_max": max(ratios),
            "tolerance": list(_RATIO_TOLERANCE),
        },
        "observations": observations,
        "sample_count": len(observations),
        "first_date": first_date,
        "last_date": last_date,
        "span_days": span_days,
        "backtest_ready": ready,
        "backtest_ready_reasons": reasons,
        "backtest_ready_notes": notes,
        "backtest_requirements": {"min_samples": MIN_SAMPLES, "min_span_days": MIN_SPAN_DAYS},
        "coverage": coverage,
        "source_attempts": attempts,
        "covered_symbols": covered_symbols(),
        "source": {
            "name": SOURCE_NAME,
            "url": futures_basis.page_url(date.fromisoformat(last_date)),
            "kind": SOURCE_KIND,
        },
        "access_method": ACCESS_METHOD,
        "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "notice": NOTICE,
        "gaps": _gap_notes(coverage),
        "cache_ttl_seconds": _CACHE_TTL,
        "cached": False,
    }
    _remember(cache_key, _CACHE_TTL, value)
    return value


def history_document(series: dict):
    """Render one collected spot-history series as an ingestable source document.

    ``None`` means the series is incomplete or ``spot_history_unavailable``; the
    tool result is left untouched either way. Only a collected series is
    registered, because a missing date must never become a number.
    """
    if not isinstance(series, dict) or series.get("status") != "collected":
        return None
    symbol = series.get("symbol")
    if not isinstance(symbol, str) or symbol not in futures.PRODUCTS:
        return None
    source = series.get("source")
    url = source.get("url") if isinstance(source, dict) else None
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    if not isinstance(series.get("collected_at"), str):
        return None
    unit = series.get("unit")
    if not isinstance(unit, str) or not unit:
        return None
    observations = series.get("observations")
    if not isinstance(observations, list) or not observations:
        return None
    rows: list[tuple[date, float]] = []
    for observation in observations:
        if not isinstance(observation, dict):
            return None
        try:
            day = date.fromisoformat(str(observation.get("date")))
        except ValueError:
            return None
        price = observation.get("price")
        if not _positive(price) or observation.get("unit") != unit:
            return None
        if observation.get("source") != SOURCE_NAME or observation.get("access_method") != ACCESS_METHOD:
            return None
        rows.append((day, float(price)))
    if any(later[0] <= earlier[0] for earlier, later in zip(rows, rows[1:], strict=False)):
        return None
    first, last = rows[0][0], rows[-1][0]
    if series.get("first_date") != first.isoformat() or series.get("last_date") != last.isoformat():
        return None
    if series.get("sample_count") != len(rows):
        return None
    name, exchange = futures.PRODUCTS[symbol][0], futures.PRODUCTS[symbol][1]
    conversion = series.get("unit_conversion")
    header = (
        f"数据来源：{SOURCE_NAME}｜数据区间：{futures._chinese_day(first.isoformat())}"
        f"至{futures._chinese_day(last.isoformat())}｜品种：{name} {symbol}（{exchange}）｜单位：{unit}"
    )
    if isinstance(conversion, dict) and conversion.get("formula"):
        header = f"{header}｜单位换算：{conversion['formula']}"
        if conversion.get("evidence_url"):
            header = f"{header}（依据：{conversion['evidence_url']}）"
    label = "日频现货价格序列"
    chunks = [rows[index : index + DOC_PAGE_ROWS] for index in range(0, len(rows), DOC_PAGE_ROWS)]
    pages = []
    sections = []
    for number, chunk in enumerate(chunks, start=1):
        lines = [
            f"{day.isoformat()} 现货价格 {futures._evidence_number(price)} {unit}" for day, price in chunk
        ]
        text = "\n".join([header, f"【{label}】", *lines, NOTICE])
        pages.append({"page": number, "text": text})
        sections.append(
            {
                "page": number,
                "section": "spot_price_history",
                "label": label,
                "source": SOURCE_NAME,
                "url": url,
                "data_period": last.isoformat(),
                "symbol": symbol,
                "name": name,
            }
        )
    return {
        "symbol": symbol,
        "name": name,
        "exchange": exchange,
        "title": f"{name} {symbol} 现货历史价格 {first.isoformat()} 至 {last.isoformat()}",
        "url": url,
        "publisher": SOURCE_NAME,
        "retrieved_at": series["collected_at"],
        "data_period": last.isoformat(),
        "status": "collected",
        "tradable_contract": False,
        "source": {"name": SOURCE_NAME, "url": url, "kind": SOURCE_KIND},
        "sections": sections,
        "pages": pages,
    }
