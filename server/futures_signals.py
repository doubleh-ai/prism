"""Daily technical, positioning, 5-year percentile and roll diagnostics for continuous bars."""

from datetime import date
from itertools import pairwise
from math import isfinite, log, sqrt
from statistics import median, stdev


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
            volatility = stdev(returns) * sqrt(252) * 100
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


def empirical_percentile(samples: list[float], current: float) -> float | None:
    """Empirical rank in [0, 100] when ``samples`` has dispersion; ``None`` on flat/short series."""
    clean = [
        float(v) for v in samples if isinstance(v, (int, float)) and not isinstance(v, bool) and isfinite(v)
    ]
    if len(clean) < 20 or not (
        isinstance(current, (int, float)) and not isinstance(current, bool) and isfinite(current)
    ):
        return None
    low, high = min(clean), max(clean)
    if high <= low:
        return None
    below = sum(1 for v in clean if v < current)
    equal = sum(1 for v in clean if v == current)
    return round(100.0 * (below + 0.5 * max(0, equal - 1)) / max(1, len(clean) - 1), 1)


def _day_distance(d1: date, d2: date) -> int:
    """Circular day-of-year distance in days (0..183) for seasonal comparison."""
    y1, y2 = d1.timetuple().tm_yday, d2.timetuple().tm_yday
    diff = abs(y1 - y2)
    return min(diff, 365 - diff)


def percentiles(bars: list[dict]) -> dict:
    """Compute up to 5-year (~1250 trading days) historical & seasonal percentiles without guessing."""
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
    latest_day = date.fromisoformat(window[-1]["date"])

    close_pct_5y = empirical_percentile(closes, latest_close)
    close_pct_1y = empirical_percentile(closes[-250:], latest_close)

    seasonal_closes = []
    seasonal_years = set()
    for b in window:
        d = date.fromisoformat(b["date"])
        if _day_distance(d, latest_day) <= 20:
            seasonal_closes.append(float(b["close"]))
            seasonal_years.add(d.year)
    seasonal_pct = (
        empirical_percentile(seasonal_closes, latest_close)
        if len(seasonal_years) >= 3 and len(seasonal_closes) >= 30
        else None
    )
    seasonal_med = round(median(seasonal_closes), 4) if seasonal_pct is not None else None

    vol_series = []
    if len(closes) >= 40:
        log_rets = [log(curr) - log(prev) for prev, curr in pairwise(closes)]
        for idx in range(20, len(log_rets) + 1):
            seg = log_rets[idx - 20 : idx]
            vol_series.append(stdev(seg) * sqrt(252) * 100)
    vol_pct_rank = empirical_percentile(vol_series, vol_series[-1]) if len(vol_series) >= 20 else None

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
        empirical_percentile(oi_series, float(latest_oi))
        if isinstance(latest_oi, (int, float)) and latest_oi > 0 and len(oi_series) >= 20
        else None
    )

    return {
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
