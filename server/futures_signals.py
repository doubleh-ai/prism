"""Daily technical, positioning, 5-year percentile and roll diagnostics for continuous bars."""

from datetime import date
from itertools import accumulate, pairwise
from math import isfinite, log, sqrt
from statistics import median

_DAYS_COMMON = (0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
_DAYS_LEAP = (0, 31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
_YDAY_COMMON = (0, 0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
_YDAY_LEAP = (0, 0, 31, 60, 91, 121, 152, 182, 213, 244, 274, 305, 335)
_PERCENTILES_CACHE: dict[tuple, dict] = {}
_PERCENTILES_CACHE_MAX = 256


def _sample_stdev(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((x - mean) * (x - mean) for x in values) / (n - 1)
    return sqrt(var)


def _parse_iso_year_yday(raw: str) -> tuple[int, int]:
    if (
        len(raw) == 10
        and raw[4] == "-"
        and raw[7] == "-"
        and raw[0:4].isdigit()
        and raw[5:7].isdigit()
        and raw[8:10].isdigit()
        and raw.isascii()
    ):
        y = int(raw[0:4])
        m = int(raw[5:7])
        d = int(raw[8:10])
        leap = (y % 4 == 0 and y % 100 != 0) or (y % 400 == 0)
        days = _DAYS_LEAP if leap else _DAYS_COMMON
        if 1 <= m <= 12 and 1 <= d <= days[m]:
            offsets = _YDAY_LEAP if leap else _YDAY_COMMON
            return y, offsets[m] + d
    parsed = date.fromisoformat(raw)
    return parsed.year, (parsed - date(parsed.year, 1, 1)).days + 1


def _bars_cache_key(bars: list[dict]) -> tuple | None:
    if not isinstance(bars, list) or len(bars) < 20:
        return None
    first, mid, prev, last = bars[0], bars[len(bars) // 2], bars[-2], bars[-1]
    if not (
        isinstance(first, dict)
        and isinstance(mid, dict)
        and isinstance(prev, dict)
        and isinstance(last, dict)
    ):
        return None
    return (
        id(bars),
        len(bars),
        first.get("date"),
        first.get("close"),
        mid.get("date"),
        mid.get("close"),
        prev.get("date"),
        prev.get("close"),
        prev.get("open_interest"),
        last.get("date"),
        last.get("open"),
        last.get("close"),
        last.get("open_interest"),
    )


def _smooth(values, period, weight):
    if len(values) < period:
        return []
    average = sum(values[:period]) / period
    result = [average]
    for value in values[period:]:
        average += weight * (value - average)
        result.append(average)
    return result


def ema_series(values, period):
    """Return an EMA list aligned to the tail of ``values`` (length ``len(values) - period + 1``)."""
    return _smooth(values, period, 2 / (period + 1))


def technicals(bars):
    closes = [bar["close"] for bar in bars]
    ranges, gains, losses = [], [], []
    for previous, current in pairwise(bars):
        high, low, prior_close = current["high"], current["low"], previous["close"]
        ranges.append(max(high - low, abs(high - prior_close), abs(low - prior_close)))
        up, down = high - previous["high"], previous["low"] - low
        gains.append(up if up > max(down, 0) else 0)
        losses.append(down if down > max(up, 0) else 0)
    atr = _smooth(ranges, 14, 1 / 14)
    dx = [
        100 * abs(up - down) / (up + down) if up + down else 0
        for up, down in zip(_smooth(gains, 14, 1 / 14), _smooth(losses, 14, 1 / 14), strict=True)
    ]
    adx = _smooth(dx, 14, 1 / 14)
    values = {"atr14": atr[-1] if atr else None, "adx14": adx[-1] if adx else None}
    for period in (20, 60):
        ema = _smooth(closes, period, 2 / (period + 1))
        values[f"ema{period}"] = ema[-1] if ema else None
        volatility = None
        if len(closes) > period:
            window = closes[-period - 1 :]
            returns = [log(current) - log(previous) for previous, current in pairwise(window)]
            volatility = _sample_stdev(returns) * sqrt(252) * 100
        values[f"volatility{period}_pct"] = volatility
    for period in (20, 60, 250):
        position = None
        if len(bars) >= period:
            high = max(bar["high"] for bar in bars[-period:])
            low = min(bar["low"] for bar in bars[-period:])
            if high > low:
                position = 100 * (closes[-1] - low) / (high - low)
        values[f"range{period}_pct"] = position
    return {key: round(value, 4) if value is not None else None for key, value in values.items()}


def positioning(bars):
    latest = bars[-1]
    interest, volume = latest.get("open_interest"), latest.get("volume")
    prior = bars[-2].get("open_interest") if len(bars) > 1 else None
    return {
        "open_interest": interest,
        "change": interest - prior if interest is not None and prior is not None else None,
        "previous_date": bars[-2]["date"] if interest is not None and prior is not None else None,
        "volume": volume,
        "volume_to_interest": round(volume / interest, 4) if volume is not None and interest else None,
    }


def _empirical_percentile_clean(clean: list[float], current: float) -> float | None:
    n = len(clean)
    if n < 20:
        return None
    low = high = clean[0]
    below = equal = 0
    for v in clean:
        if v < low:
            low = v
        elif v > high:
            high = v
        if v < current:
            below += 1
        elif v == current:
            equal += 1
    if high <= low:
        return None
    return round(100.0 * (below + 0.5 * max(0, equal - 1)) / max(1, n - 1), 1)


def empirical_percentile(samples: list[float], current: float) -> float | None:
    """Empirical rank in [0, 100] when ``samples`` has dispersion; ``None`` on flat/short series."""
    if not (isinstance(current, (int, float)) and not isinstance(current, bool) and isfinite(current)):
        return None
    clean = [
        float(v) for v in samples if isinstance(v, (int, float)) and not isinstance(v, bool) and isfinite(v)
    ]
    return _empirical_percentile_clean(clean, float(current))


def _day_distance(d1: date, d2: date) -> int:
    """Circular day-of-year distance in days (0..183) for seasonal comparison."""
    y1, y2 = d1.timetuple().tm_yday, d2.timetuple().tm_yday
    diff = abs(y1 - y2)
    return min(diff, 365 - diff)


def percentiles(bars: list[dict]) -> dict:
    """Compute up to 5-year (~1250 trading days) historical & seasonal percentiles without guessing."""
    cache_key = _bars_cache_key(bars)
    if cache_key is not None:
        cached = _PERCENTILES_CACHE.get(cache_key)
        if cached is not None:
            return dict(cached)

    valid = [
        b
        for b in (bars or [])
        if isinstance(b, dict)
        and isinstance(b.get("date"), str)
        and isinstance(b.get("close"), (int, float))
        and not isinstance(b.get("close"), bool)
        and isfinite(b["close"])
        and b["close"] > 0
    ]
    if len(valid) < 20:
        return {}
    window = valid[-1250:]
    closes = [float(b["close"]) for b in window]
    latest_close = closes[-1]
    _, latest_yday = _parse_iso_year_yday(window[-1]["date"])

    close_pct_5y = _empirical_percentile_clean(closes, latest_close)
    close_pct_1y = _empirical_percentile_clean(closes[-250:], latest_close)

    seasonal_closes: list[float] = []
    seasonal_years: set[int] = set()
    for b, close_val in zip(window, closes, strict=True):
        yr, yday = _parse_iso_year_yday(b["date"])
        diff = abs(yday - latest_yday)
        if min(diff, 365 - diff) <= 20:
            seasonal_closes.append(close_val)
            seasonal_years.add(yr)
    seasonal_pct = (
        _empirical_percentile_clean(seasonal_closes, latest_close)
        if len(seasonal_years) >= 3 and len(seasonal_closes) >= 30
        else None
    )
    seasonal_med = round(median(seasonal_closes), 4) if seasonal_pct is not None else None

    vol_series: list[float] = []
    if len(closes) >= 40:
        log_rets = [log(curr) - log(prev) for prev, curr in pairwise(closes)]
        mu0 = sum(log_rets) / len(log_rets)
        centered = [r - mu0 for r in log_rets]
        ps1 = list(accumulate(centered, initial=0.0))
        ps2 = list(accumulate((x * x for x in centered), initial=0.0))
        vol_scale = sqrt(252.0 / 19.0) * 100.0
        vol_series = [
            sqrt(max(0.0, (ps2[idx] - ps2[idx - 20]) - 0.05 * (ps1[idx] - ps1[idx - 20]) ** 2)) * vol_scale
            for idx in range(20, len(log_rets) + 1)
        ]
    vol_pct_rank = _empirical_percentile_clean(vol_series, vol_series[-1]) if len(vol_series) >= 20 else None

    oi_series = [
        float(b["open_interest"])
        for b in window
        if isinstance(b.get("open_interest"), (int, float))
        and not isinstance(b.get("open_interest"), bool)
        and isfinite(b["open_interest"])
        and b["open_interest"] > 0
    ]
    latest_oi = window[-1].get("open_interest")
    oi_pct_rank = (
        _empirical_percentile_clean(oi_series, float(latest_oi))
        if isinstance(latest_oi, (int, float))
        and not isinstance(latest_oi, bool)
        and isfinite(latest_oi)
        and latest_oi > 0
        and len(oi_series) >= 20
        else None
    )

    result = {
        "data_as_of": window[-1]["date"],
        "sample_days": len(window),
        "since_date": window[0]["date"],
        "full_5y": len(window) >= 1000,
        "span_label": "近5年" if len(window) >= 1000 else f"近{len(window)}交易日",
        "close_pct_5y": close_pct_5y,
        "close_pct_1y": close_pct_1y,
        "close_seasonal_pct": seasonal_pct,
        "seasonal_median": seasonal_med,
        "seasonal_years": len(seasonal_years) if seasonal_pct is not None else 0,
        "volatility20_pct_rank": vol_pct_rank,
        "open_interest_pct_rank": oi_pct_rank,
    }
    if cache_key is not None:
        if len(_PERCENTILES_CACHE) >= _PERCENTILES_CACHE_MAX:
            _PERCENTILES_CACHE.pop(next(iter(_PERCENTILES_CACHE)))
        _PERCENTILES_CACHE[cache_key] = result
    return dict(result)


def roll_diagnostics(bars: list[dict], exchange_daily: dict | None = None) -> dict:
    """Detect continuous-series contract-roll jump risk and aggregate contract concentration."""
    if not bars or len(bars) < 2:
        return {"roll_suspect": False, "roll_notice": None, "active_contract": None}
    curr, prev = bars[-1], bars[-2]
    c_now, c_prev, o_now = curr.get("close"), prev.get("close"), curr.get("open")
    cont_ret_pct = (
        round((c_now / c_prev - 1.0) * 100.0, 2)
        if all(isinstance(v, (int, float)) and v > 0 for v in (c_now, c_prev))
        else None
    )
    gap_open_pct = (
        round((o_now / c_prev - 1.0) * 100.0, 2)
        if all(isinstance(v, (int, float)) and v > 0 for v in (o_now, c_prev))
        else None
    )
    oi_now, oi_prev = curr.get("open_interest"), prev.get("open_interest")
    oi_chg_pct = (
        round((oi_now / oi_prev - 1.0) * 100.0, 2)
        if all(isinstance(v, (int, float)) and v > 0 for v in (oi_now, oi_prev))
        else None
    )

    active_contract = None
    total_oi = None
    active_oi_share_pct = None
    second_contract = None
    if (
        isinstance(exchange_daily, dict)
        and exchange_daily.get("status") == "collected"
        and exchange_daily.get("data_as_of") == curr.get("date")
    ):
        contracts = [
            c
            for c in (exchange_daily.get("contracts") or [])
            if isinstance(c, dict)
            and c.get("contract")
            and isinstance(c.get("open_interest"), (int, float))
            and c["open_interest"] >= 0
        ]
        if contracts:
            ranked = sorted(
                contracts,
                key=lambda r: (r.get("open_interest") or 0, r.get("volume") or 0),
                reverse=True,
            )
            active_contract = ranked[0]["contract"].upper()
            if len(ranked) > 1 and (ranked[1].get("open_interest") or 0) > 0:
                second_contract = ranked[1]["contract"].upper()
            summed = sum(float(r["open_interest"]) for r in contracts)
            if summed > 0:
                total_oi = int(summed)
                active_oi_share_pct = round(100.0 * float(ranked[0]["open_interest"]) / summed, 1)

    roll_suspect = False
    reasons = []
    if (
        gap_open_pct is not None
        and abs(gap_open_pct) >= 3.2
        and oi_chg_pct is not None
        and abs(oi_chg_pct) >= 15.0
    ):
        roll_suspect = True
        reasons.append(
            f"连续序列开盘跳变 {gap_open_pct:+.2f}% 且单日持仓变动 {oi_chg_pct:+.1f}%，疑似主力换月移仓"
        )
    elif oi_chg_pct is not None and abs(oi_chg_pct) >= 22.0:
        roll_suspect = True
        reasons.append(f"连续序列单日持仓剧烈变动 {oi_chg_pct:+.1f}%，可能处于主力移仓换月窗口")
    if active_oi_share_pct is not None and second_contract and active_oi_share_pct < 48.0:
        reasons.append(
            f"当前第一大持仓合约 {active_contract} 占全品种持仓 {active_oi_share_pct}%（次主力 {second_contract} 分流明显，正值换月双合约博弈期）"
        )

    return {
        "data_as_of": curr.get("date"),
        "roll_suspect": roll_suspect,
        "roll_notice": "；".join(reasons) + "。" if reasons else None,
        "continuous_return_pct": cont_ret_pct,
        "gap_open_pct": gap_open_pct,
        "oi_change_pct": oi_chg_pct,
        "active_contract": active_contract,
        "second_contract": second_contract,
        "total_contract_open_interest": total_oi,
        "active_oi_share_pct": active_oi_share_pct,
    }
