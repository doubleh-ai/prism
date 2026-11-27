"""Read-only futures coverage and date-scoped observations."""

import asyncio
import logging
import math
import time
from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from . import (
    futures,
    futures_auto_engine,
    futures_basis,
    futures_equity_bridge,
    futures_evidence,
    futures_institutional_refresh,
    futures_interpretation,
    futures_inventory,
    futures_jm_sources,
    futures_margins,
    futures_ontology,
    futures_panel,
    futures_quant_provider,
    futures_signals,
    futures_spot_history,
    futures_weekly,
    futures_workspace_fundamentals,
    public_network,
)
from .auth import require_account_context, require_auth, verify_csrf
from .database import check_rate_limit

# This workspace's universe is deliberately narrower than the legacy tools.
# PG (liquefied petroleum gas) is listed on DCE.
EXCHANGES = {
    "SHFE": "AU AG CU AL NI SN AO RB HC SS SP RU BU",
    "INE": "SC NR EC",
    "DCE": "JM I C CS LH JD PP V PG A B M P Y",
    "CZCE": "CF SR SA FG SH MA TA UR AP CJ PK RM OI",
    "GFEX": "LC SI PS",
}
UNIVERSE = {symbol: exchange for exchange, codes in EXCHANGES.items() for symbol in codes.split()}
DIMENSIONS = {
    "market": "行情",
    "technicals": "技术面",
    "positioning": "资金面",
    "fundamentals": "基本面",
    "freshness": "数据新鲜度",
}
FUNDAMENTALS = ("spread", "inventory", "basis", "spot_history")
CACHE_SECONDS = 60
FRESH_DAYS = 4  # Same calendar-age boundary as the continuous futures reader.
OVERVIEW_SECONDS = 1800
_cards: dict[str, tuple[float, dict]] = {}
_locks = {symbol: asyncio.Lock() for symbol in UNIVERSE}
_overview: dict[str, dict] = {}
_overview_fundamentals: dict[str, dict] | None = None
_overview_fund_signals: dict[str, dict] = {}
_benchmarks: dict[str, dict] = {}
BENCHMARK_SYMBOLS = frozenset({"AP", "B", "CS", "EC", "JD", "LH"})
_refreshing = False
_log = logging.getLogger(__name__)


def normalize_symbol(value: str) -> str | None:
    symbol = value.strip().upper()
    return symbol if symbol in UNIVERSE else None


def _now():
    return datetime.now(futures_panel.SHANGHAI)


def _product(symbol):
    meta = futures_ontology.get(symbol)
    return {
        "symbol": symbol,
        "exchange": UNIVERSE[symbol],
        "name": futures_panel._MARKET_NAMES.get(symbol, futures.PRODUCTS[symbol][0]),
        "exchange_name": futures._exchange_source(UNIVERSE[symbol]),
        "archetype": meta.archetype if meta else "general",
        "archetype_label": meta.archetype_label if meta else "商品期货",
        "chain_id": meta.chain_id if meta else "",
    }


def _status(status, reason_code=None, reason=None, data_as_of=None):
    return dict(status=status, reason_code=reason_code, reason=reason, data_as_of=data_as_of)


def _unavailable(code, reason):
    return {**_status("unavailable", code, reason), "source": None, "values": None, "gaps": []}


def _observed(day, source, values, *, collected_at=None, gaps=()):
    return {
        **_status("collected", data_as_of=day),
        "source": source,
        "values": values,
        "collected_at": collected_at,
        "gaps": list(gaps),
    }


def _l1_number(value):
    return value if type(value) in (float, int) and math.isfinite(value) else None


def _market(raw, symbol, today):
    latest = raw.get("latest") or {}
    # Snapshot data_as_of can describe a different source when the bars are absent.
    day = futures._evidence_day(latest.get("date") or raw.get("data_as_of"))
    source = futures_panel._source(raw.get("source"))
    price_fields = ("open", "high", "low", "close")
    fields = (*price_fields, "volume", "open_interest")
    values = {key: _l1_number(latest.get(key)) for key in fields}
    if raw.get("series_status") != "collected":
        return _unavailable("market_unavailable", "本次未取得连续日线。")
    if not day or date.fromisoformat(day) > today or not source or values["close"] is None:
        return _unavailable("invalid_market_observation", "收盘价、数据日期或来源无法核对。")
    return _observed(
        day,
        source,
        {
            **values,
            "bars": [
                {"date": bar["date"], **{key: _l1_number(bar.get(key)) for key in price_fields}}
                for bar in raw.get("bars", [])[-60:]
            ],
            "series_symbol": raw.get("series_symbol") or symbol + "0",
            "series_type": "continuous",
            "quote_unit": futures._QUOTE_UNITS.get(symbol),
        },
        collected_at=raw.get("collected_at"),
    )


def _exchange_report(raw, symbol, key, fields, today):
    report = raw.get(key) or {}
    if report.get("status") != "collected":
        return _unavailable(report.get("error_code") or key + "_unavailable", "本次未取得交易所资料。")
    day = futures._evidence_day(report.get("data_as_of"))
    contracts = [
        {field: row.get(field) for field in ("contract", *fields)}
        for row in report.get("contracts") or []
        if row.get("contract") and any(row.get(field) is not None for field in fields)
    ]
    if not day or date.fromisoformat(day) > today or not report.get("source_url") or not contracts:
        return _unavailable("invalid_exchange_observation", "报价、合约日期或来源无法核对。")
    return _observed(
        day,
        {"name": futures._exchange_source(UNIVERSE[symbol]), "url": report["source_url"]},
        {"contracts": contracts, "quote_unit": report.get("quote_unit") or futures._QUOTE_UNITS.get(symbol)},
        collected_at=report.get("collected_at"),
    )


async def _history(symbol, day):
    end = day - timedelta(days=1)
    start = end - timedelta(days=6)
    raw = await futures_spot_history.history(symbol, start.isoformat(), end.isoformat())
    if raw.get("status") != "collected" or not raw.get("observations"):
        return _unavailable(
            raw.get("error_code") or "spot_history_unavailable", raw.get("reason") or "本次未取得现货历史。"
        )
    return _observed(
        raw["last_date"],
        futures_panel._source(raw.get("source")),
        {key: raw[key] for key in ("first_date", "last_date", "sample_count", "unit", "observations")},
        collected_at=raw.get("collected_at"),
        gaps=raw.get("gaps", []),
    )


def _safe_result(value, key):
    if isinstance(value, asyncio.CancelledError):
        raise value
    if isinstance(value, BaseException):
        return _unavailable(key + "_unavailable", "暂未取得数据，请稍后重试。")
    return value


def _continuous_observation(raw, market, key):
    if market["status"] != "collected":
        return _unavailable("no_continuous_series", "连续日线不可用，无法计算。")
    values = raw.get(f"workspace_{key}") or {}
    if key == "technicals":
        measures = values.values()
        reason = "有效日线不足，或区间内没有价格波动。"
    else:
        measures = [
            values.get(field) for field in ("open_interest", "change", "volume", "volume_to_interest")
        ]
        reason = "部分持仓数据缺失，或持仓量为零，无法计算对应指标。"
    if not any(value is not None for value in measures):
        return _unavailable("insufficient_observations", reason)
    item = _observed(market["data_as_of"], market["source"], values, collected_at=market["collected_at"])
    if any(value is None for value in measures):
        item.update(_status("partial", "insufficient_observations", reason, market["data_as_of"]))
    return item


def _rankings(raw, symbol, today):
    report = (raw.get("exchange_daily") or {}).get("member_positioning") or {}
    day = futures._evidence_day(report.get("data_as_of"))
    source_url, contract = report.get("source_url"), report.get("contract")
    sides = {
        key: item for key, item in (report.get("sides") or {}).items() if item.get("status") == "collected"
    }
    if report.get("status") != "collected" or not (
        day and date.fromisoformat(day) <= today and source_url and contract and sides
    ):
        return _unavailable(report.get("error_code") or "rankings_unavailable", "本次未取得交易所会员排名。")
    return _observed(
        day,
        {"name": futures._exchange_source(UNIVERSE[symbol]), "url": source_url},
        {"contract": contract, "sides": sides, "notice": report.get("notice")},
    )


def _basis_rate(item):
    if item["status"] == "collected":
        values = item["values"]
        spot, basis = (_l1_number(values.get(name)) for name in ("spot_price", "basis"))
        rate = round(100 * basis / spot, 4) if spot is not None and spot > 0 and basis is not None else None
        return {**item, "values": {**values, "basis_rate_pct": rate}}
    return item


def _freight_benchmark(now):
    research = futures_weekly.research("EC", now)
    index = [row for row in (research or {}).get("rows", []) if row["key"] == "index"]
    if not index:
        return _unavailable("freight_index_unavailable", "本次未取得欧洲航线结算运价指数。")
    latest = index[-1]
    gaps = ["本卡展示最近已收录的正式发布值。"]
    if research["historical"]:
        gaps.append("已超过一周，使用时留意数据日期。")
    return _observed(
        latest["period"],
        {"name": "上海航运交易所", "url": latest["url"]},
        {
            "label": "欧洲航线结算运价指数",
            "rows": [{"label": "SCFIS 欧洲航线", "value": latest["value"], "unit": latest["unit"]}],
            "scope": latest["scope"],
        },
        gaps=gaps,
    )


def _fundamental_status(symbol, readings=None):
    if readings is None:
        if _overview_fundamentals and symbol in _overview_fundamentals:
            return _overview_fundamentals[symbol]
        coverage = futures_panel.availability(symbol)
        supported = any(coverage[key]["status"] == "available" for key in ("spread", "inventory", "basis"))
        if supported or futures_spot_history.unit_plan(symbol):
            reason = (
                "总览未核验到基本面数据，详情提供各来源的覆盖情况。"
                if _overview_fundamentals is not None
                else "有接入来源，待读取核验。"
            )
            return _status("unknown", "awaiting_observation", reason)
        return _status("unavailable", "fundamentals_not_covered", "目前没有接入可用的基本面来源。")
    observations = [readings[key] for key in FUNDAMENTALS]
    if "benchmark" in readings and readings["benchmark"]["status"] == "collected":
        observations.append(readings["benchmark"])
    dates = [
        item["data_as_of"] for item in observations if item["status"] == "collected" and item["data_as_of"]
    ]
    if dates:
        if all(item["status"] == "collected" for item in observations):
            return _status("collected", data_as_of=max(dates))
        return _status("partial", "some_sources_unavailable", "已取得部分基本面数据。", max(dates))
    return _status("unavailable", "fundamentals_unavailable", "本次未取得基本面数据，详见卡片。")


def _summary(item):
    return {key: item[key] for key in ("status", "reason_code", "reason", "data_as_of")}


def _series_observations(raw, symbol, today):
    market = _market(raw, symbol, today)
    return {
        "market": market,
        **{key: _continuous_observation(raw, market, key) for key in ("technicals", "positioning")},
    }


def _dimensions(symbol, now, card=None):
    overview = _overview.get(symbol, {})
    series = {
        key: _summary(card[key])
        if card
        else overview.get(key, _status("unknown", "awaiting_observation", "等候行情核验。"))
        for key in ("market", "technicals", "positioning")
    }
    if card and series["positioning"]["status"] != "collected" and card["rankings"]["status"] == "collected":
        series["positioning"] = _status(
            "partial", "rankings_collected", "已取得交易所会员排名。", card["rankings"]["data_as_of"]
        )
    day = series["market"]["data_as_of"]
    freshness = _status("unknown", "no_market_date", "尚未取得行情日期。")
    if day:
        age = (now.date() - date.fromisoformat(day)).days
        if age <= FRESH_DAYS:
            freshness = _status("recent", data_as_of=day)
        else:
            freshness = _status("stale", "older_observation", f"行情距离上海今天已有 {age} 个自然日。", day)
    return {
        **series,
        "fundamentals": _fundamental_status(symbol, card),
        "freshness": freshness,
    }


def _pick_tile_signal(
    symbol: str,
    bars: list[dict],
    pct_map: dict,
    observed: dict | None,
) -> tuple[str | None, str | None]:
    """Derive a concise O(1) fundamental or positioning badge for overview cards without guessing."""
    c_prev = bars[-2].get("close")
    o_now = bars[-1].get("open")
    if (
        isinstance(c_prev, (int, float))
        and isinstance(o_now, (int, float))
        and c_prev > 0
        and o_now > 0
        and abs(o_now / c_prev - 1.0) >= 0.045
    ):
        return ("换月跳空", "warn")

    basis_rate: float | None = None
    inv_chg: float | None = None
    inv_qty: float | None = None
    if observed:
        b_vals = (observed.get("basis") or {}).get("values") or {}
        if isinstance(b_vals.get("basis_rate_pct"), (int, float)):
            basis_rate = float(b_vals["basis_rate_pct"])
        i_vals = (observed.get("inventory") or {}).get("values") or {}
        if isinstance(i_vals.get("change"), (int, float)):
            inv_chg = float(i_vals["change"])
        if isinstance(i_vals.get("quantity"), (int, float)):
            inv_qty = float(i_vals["quantity"])
    elif _overview_fundamentals is not None and symbol in _overview_fund_signals:
        fs = _overview_fund_signals[symbol]
        basis_rate = fs.get("basis_rate_pct")
        inv_chg = fs.get("inv_chg")
        inv_qty = fs.get("inv_qty")

    if basis_rate is not None and abs(basis_rate) >= 2.5:
        return (f"升水+{basis_rate:.1f}%", "bull") if basis_rate > 0 else (f"贴水{basis_rate:.1f}%", "bear")
    if inv_chg is not None and inv_chg != 0:
        base_qty = max(1.0, (inv_qty - inv_chg) if (inv_qty is not None and inv_qty > inv_chg) else 1000.0)
        if abs(inv_chg) / base_qty >= 0.015 or abs(inv_chg) >= 200:
            return ("仓单去库", "bull") if inv_chg < 0 else ("仓单累库", "bear")
    if basis_rate is not None and abs(basis_rate) >= 0.5:
        return (f"升水+{basis_rate:.1f}%", "bull") if basis_rate > 0 else (f"贴水{basis_rate:.1f}%", "bear")
    if inv_chg is not None and inv_chg != 0:
        return ("仓单去库", "bull") if inv_chg < 0 else ("仓单累库", "bear")

    p5 = pct_map.get("close_pct_5y")
    if isinstance(p5, (int, float)):
        if p5 >= 85.0:
            return ("五年高位", "bull")
        if p5 <= 15.0:
            return ("五年低位", "bear")
    oi_p = pct_map.get("open_interest_pct_rank")
    if isinstance(oi_p, (int, float)):
        if oi_p >= 85.0:
            return ("持仓拥挤", "warn")
        if oi_p <= 15.0:
            return ("缩量低仓", "neutral")
    return (None, None)


def universe():
    now = _now()
    products = []
    histories = {symbol: futures.series_history(symbol) for symbol in UNIVERSE}
    names = {}
    percentiles_by_symbol: dict[str, dict] = {}
    mono_now = time.monotonic()
    for symbol in UNIVERSE:
        timestamp, value = _cards.get(symbol, (0, None))
        observed = value if mono_now - timestamp < OVERVIEW_SECONDS else None
        prod = _product(symbol)
        names[symbol] = prod["name"]
        bars = histories.get(symbol)
        preview = None
        if bars and len(bars) >= 2:
            pct_map = futures_signals.percentiles(bars)
            percentiles_by_symbol[symbol] = pct_map
            pct = pct_map.get("close_pct_5y")
            prev_c = bars[-2].get("close")
            chg = round((bars[-1]["close"] / prev_c - 1.0) * 100.0, 2) if prev_c else None
            sig_tag, sig_tone = _pick_tile_signal(symbol, bars, pct_map, observed)
            preview = {
                "close": bars[-1]["close"],
                "change_pct": chg,
                "pct_5y": pct,
                "pct_1y": pct_map.get("close_pct_1y"),
                "seasonal_pct": pct_map.get("close_seasonal_pct"),
                "oi_pct": pct_map.get("open_interest_pct_rank"),
                "signal_tag": sig_tag,
                "signal_tone": sig_tone,
                "data_as_of": bars[-1]["date"],
            }
        products.append({**prod, "dimensions": _dimensions(symbol, now, observed), "quote_preview": preview})
    return {
        "count": len(products),
        "dimensions": [{"key": key, "label": label} for key, label in DIMENSIONS.items()],
        "archetypes": [{"key": k, "label": v} for k, v in futures_ontology.ARCHETYPE_LABELS.items()],
        "products": products,
        "radar": futures_auto_engine.build_market_radar(
            histories, names, now, fresh_days=FRESH_DAYS, percentiles_by_symbol=percentiles_by_symbol
        ),
        "quant_provider": futures_quant_provider.provider_metadata(),
        "generated_at": now.isoformat(timespec="seconds"),
        "freshness_calendar_days": FRESH_DAYS,
        "refreshing": _refreshing,
    }


async def _refresh_overview():
    global _refreshing
    _refreshing = True
    try:
        now = _now()
        symbols = iter(UNIVERSE)

        async def scan_series():
            async with public_network.async_client(timeout=6) as client:
                loop = asyncio.get_running_loop()
                for symbol in symbols:
                    async with futures._slots:
                        value, _ = await futures._continuous(
                            client, symbol, now, loop.time() + 6, workspace=True
                        )
                    _overview[symbol] = {
                        key: _summary(item)
                        for key, item in _series_observations(value or {}, symbol, now.date()).items()
                    }
                    await asyncio.sleep(0.25)

        async def scan_fundamentals():
            global _benchmarks, _overview_fundamentals, _overview_fund_signals
            async with asyncio.TaskGroup() as sources:
                basis = sources.create_task(futures_basis.latest_table(UNIVERSE, now.date()))
                warrants = sources.create_task(futures_inventory.latest_exchange_tables(UNIVERSE, now.date()))
                agriculture = sources.create_task(futures_workspace_fundamentals.read(now.date()))
            warehouses = warrants.result()
            apple = warehouses.pop("AP", None)
            benchmarks = {"EC": _freight_benchmark(now)}
            for symbol, raw in agriculture.result().items():
                benchmarks[symbol] = _observed(raw["data_as_of"], raw["source"], raw["values"])
            if apple:
                benchmarks["AP"] = _observed(
                    apple["data_as_of"],
                    {"name": apple["source_name"], "url": apple["source_url"]},
                    {
                        "label": "苹果预报仓单",
                        "rows": [{key: apple[key] for key in ("label", "value", "unit")}],
                        "scope": apple["scope"],
                    },
                )
            _benchmarks = benchmarks
            basis_map = basis.result()
            inputs = {
                "现货基差": basis_map,
                "交易所仓单": warehouses,
            }
            result = {}
            sig_map = {}
            for symbol in UNIVERSE:
                observed = [
                    (label, items[symbol]["data_as_of"])
                    for label, items in inputs.items()
                    if items.get(symbol, {}).get("status") == "collected"
                ]
                benchmark = benchmarks.get(symbol, {})
                if benchmark.get("status") == "collected":
                    observed.append((benchmark["values"]["label"], benchmark["data_as_of"]))
                if observed:
                    result[symbol] = _status(
                        "partial",
                        "sources_collected",
                        "已核验" + "、".join(label for label, _ in observed) + "。",
                        max(day for _, day in observed),
                    )
                b_row = basis_map.get(symbol, {})
                w_row = warehouses.get(symbol, {})
                b_rate = None
                if b_row.get("status") == "collected":
                    sp = _l1_number(b_row.get("spot_price"))
                    bv = _l1_number(b_row.get("basis"))
                    if sp and sp > 0 and bv is not None:
                        b_rate = round(100.0 * bv / sp, 1)
                inv_qty = _l1_number(w_row.get("quantity")) if w_row.get("status") == "collected" else None
                inv_chg = _l1_number(w_row.get("change")) if w_row.get("status") == "collected" else None
                if b_rate is not None or inv_chg is not None:
                    sig_map[symbol] = {"basis_rate_pct": b_rate, "inv_qty": inv_qty, "inv_chg": inv_chg}
            _overview_fund_signals = sig_map
            _overview_fundamentals = result

        async with asyncio.TaskGroup() as scans:
            scans.create_task(scan_series())
            scans.create_task(scan_series())
            scans.create_task(scan_fundamentals())
    finally:
        _refreshing = False


async def refresh_overview_periodically(manager):
    while True:
        try:
            await _refresh_overview()
        except Exception:
            _log.exception("Futures overview refresh failed")
        try:
            await futures_jm_sources.capture_for_saved_judgments()
        except Exception:
            _log.exception("JM research publication refresh failed")
        try:
            await futures_institutional_refresh.refresh_due(manager)
        except Exception:
            _log.exception("Futures institutional report refresh failed")
        try:
            await futures_auto_engine.reconcile_pending_judgments(manager)
        except Exception:
            _log.exception("Futures judgment auto-reconciliation failed")
        try:
            cached_cards = [val for _, val in list(_cards.values())]
            if cached_cards:
                await futures_auto_engine.preinterpret_due_cards(manager, cached_cards)
        except Exception:
            _log.exception("Futures event-gated pre-interpretation failed")
        await asyncio.sleep(OVERVIEW_SECONDS)


async def _read_card(symbol):
    now = _now()
    day = futures_panel._trading_day(None)
    sources = await asyncio.gather(
        futures.snapshot(symbol, workspace=True),
        futures_panel._spread_dimension(symbol, day),
        futures_panel._inventory_dimension(symbol),
        futures_panel._basis_dimension(symbol),
        _history(symbol, now.date()),
        *([futures_jm_sources.editions(now)] if symbol == "JM" else []),
        return_exceptions=True,
    )
    raw, spread, inventory, basis, history, *jm = sources
    if jm and isinstance(jm[0], asyncio.CancelledError):
        raise jm[0]
    if isinstance(raw, asyncio.CancelledError):
        raise raw
    snapshot = {} if isinstance(raw, BaseException) else raw
    value = {
        **_product(symbol),
        "generated_at": now.isoformat(timespec="seconds"),
        **_series_observations(snapshot, symbol, now.date()),
        "rankings": _rankings(snapshot, symbol, now.date()),
        **(
            {
                "benchmark": _benchmarks.get(symbol)
                or _unavailable("benchmark_unavailable", "本次未取得该品种的专项基本面资料。")
            }
            if symbol in BENCHMARK_SYMBOLS
            else {}
        ),
        "spread": _safe_result(spread, "spread"),
        "inventory": _safe_result(inventory, "inventory"),
        "basis": _basis_rate(_safe_result(basis, "basis")),
        "spot_history": _safe_result(history, "spot_history"),
        "settlements": _exchange_report(
            snapshot, symbol, "exchange_daily", ("close", "settlement", "volume", "open_interest"), now.date()
        ),
        "contract_dates": _exchange_report(
            snapshot,
            symbol,
            "contract_dates",
            ("listed_expiry_date", "delivery_start", "delivery_end"),
            now.date(),
        ),
        "contract_parameters": _exchange_report(
            snapshot,
            symbol,
            "exchange_parameters",
            ("upper_limit_pct", "lower_limit_pct", "spec_long_margin_pct", "spec_short_margin_pct"),
            now.date(),
        ),
    }
    jm_editions = jm[0] if jm and not isinstance(jm[0], BaseException) else futures_jm_sources.ARCHIVE
    histories = {s: futures.series_history(s) for s in UNIVERSE}
    own_bars = histories.get(symbol) or snapshot.get("bars") or []
    if own_bars and not histories.get(symbol):
        histories[symbol] = own_bars
    value["percentiles"] = snapshot.get("workspace_percentiles") or futures_signals.percentiles(own_bars)
    value["roll_guard"] = futures_signals.roll_diagnostics(own_bars, snapshot.get("exchange_daily"))
    value["margins"] = futures_margins.margins_for_symbol(
        symbol, histories, today=now.date(), fresh_days=FRESH_DAYS
    )
    value["research_brief"] = futures_evidence.research_brief(value, now, jm_editions, fresh_days=FRESH_DAYS)
    value["chain_canvas"] = futures_equity_bridge.build_chain_canvas(
        symbol, value, histories, value["margins"]
    )
    return value


async def card(symbol):
    async with _locks[symbol]:
        existing = _cards.get(symbol)
        if existing and time.monotonic() - existing[0] < CACHE_SECONDS:
            value = existing[1]
        else:
            value = await _read_card(symbol)
            _cards[symbol] = (time.monotonic(), value)
    return {**value, "dimensions": _dimensions(symbol, _now(), value)}


router = APIRouter(prefix="/api/futures", tags=["futures"])


@router.get("/universe")
async def read_universe(request: Request):
    require_auth(request)
    return JSONResponse(universe(), headers={"Cache-Control": "no-store"})


@router.get("/{symbol}/card")
async def read_card(symbol: str, request: Request):
    owner = require_auth(request)
    code = normalize_symbol(symbol)
    if code is None:
        raise HTTPException(404, "该品种不在期货分析目录中。")
    if not check_rate_limit("futures_card:" + owner, max_req=12, window=60):
        raise HTTPException(429, "查询较频繁，请稍后再试。")
    return JSONResponse(
        futures_interpretation.enrich(await card(code)), headers={"Cache-Control": "no-store"}
    )


@router.post("/{symbol}/warm-chain")
async def warm_chain(symbol: str, request: Request):
    """Explicit user-requested chain warmup to populate sibling futures legs for same-day margin verification."""
    owner = require_auth(request)
    verify_csrf(request)
    code = normalize_symbol(symbol)
    if code is None:
        raise HTTPException(404, "该品种不在期货分析目录中。")
    if not check_rate_limit("futures_warm_chain:" + owner, max_req=6, window=60):
        raise HTTPException(429, "同步请求较频繁，请稍后再试。")
    meta = futures_ontology.get(code)
    chain = futures_equity_bridge.SUPPLY_CHAINS.get(meta.chain_id if meta else "")
    siblings = []
    if chain:
        for s in (*chain.upstream_futures, *chain.downstream_futures):
            if s != code and futures.series_history(s) is None and s not in siblings:
                siblings.append(s)
    if siblings:
        now = _now()
        async with public_network.async_client(timeout=6) as client:
            loop = asyncio.get_running_loop()
            for s in siblings[:5]:
                async with futures._slots:
                    await futures._continuous(client, s, now, loop.time() + 6, workspace=True)
        _cards.pop(code, None)
    return JSONResponse(
        futures_interpretation.enrich(await card(code)), headers={"Cache-Control": "no-store"}
    )


@router.post("/{symbol}/interpret")
async def interpret_card(symbol: str, request: Request):
    owner = require_auth(request)
    if not request.headers.get("X-Prism-Account"):
        raise HTTPException(409, "请刷新页面以确认当前账号。")
    require_account_context(request, owner)
    verify_csrf(request)
    code = normalize_symbol(symbol)
    if code is None:
        raise HTTPException(404, "该品种不在期货分析目录中。")
    if not check_rate_limit("futures_interpret:" + owner, max_req=4, window=60):
        raise HTTPException(429, "解读请求较频繁，请稍后再试。")
    current = futures_interpretation.enrich(await card(code))
    if require_auth(request) != owner:
        raise HTTPException(401, "请重新登录")
    result = await futures_interpretation.interpret(
        current, owner, request.headers.get("X-Request-ID"), request.headers.get("X-Futures-Evidence")
    )
    return JSONResponse(result, headers={"Cache-Control": "private, no-store"})
