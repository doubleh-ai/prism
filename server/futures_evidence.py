"""Point-in-time changes and daily trend readings from the workspace's existing evidence."""

from datetime import date, datetime, timedelta
from math import isfinite
from urllib.parse import urlsplit

from . import futures_basis, futures_weekly

# Each key compares observations of one actual series with a stable definition.
# Forecasts and contract prices are intentionally excluded from survey comparisons.
WEEKLY_KEYS = {
    "JM": ("port", "mine", "coke", "output"),
    "RB": ("output", "mill", "society", "purchases"),
    "M": ("meal_stock", "beans_stock", "crush", "arrivals", "feed_days"),
    "EC": ("index",),
}
JM_SERIES = {
    "port": ("进口焦煤库存", "16个港口", "万吨"),
    "mine": ("矿山精煤库存", "523家炼焦煤矿山样本", "万吨"),
    "coke": ("焦企炼焦煤库存", "230家独立焦企样本", "万吨"),
    "output": ("精煤日均产量", "523家炼焦煤矿山样本", "万吨/日"),
}


def _weekly_rows(symbol, now, jm_editions):
    keys = WEEKLY_KEYS.get(symbol, ())
    if not keys:
        return []
    cutoff = now.astimezone(futures_weekly.SHANGHAI).replace(tzinfo=None)
    if symbol == "JM":
        # The archive is also used if the live publisher is unavailable.
        observations = [
            dict(
                key=key,
                label=label,
                scope=scope,
                unit=unit,
                value=item.values[key],
                period=item.period,
                known=item.known,
                url=item.url,
            )
            for item in jm_editions
            if item.period <= cutoff.date().isoformat() and datetime.fromisoformat(item.known) <= cutoff
            for key, (label, scope, unit) in JM_SERIES.items()
            if key in item.values
        ]
        observations.sort(key=lambda r: (r["period"], r["known"]))
    else:
        observations = (futures_weekly.research(symbol, now) or {}).get("rows", [])
    rows = []
    for key in keys:
        by_period = {r["period"]: r for r in observations if r["key"] == key}
        if len(by_period) < 2:
            continue
        previous, current = list(by_period.values())[-2:]
        day = date.fromisoformat(current["period"])
        if day - date.fromisoformat(previous["period"]) != timedelta(days=7):
            continue
        rows.append(
            {
                "label": current["label"],
                "scope": current["scope"],
                "unit": current["unit"],
                "current": current["value"],
                "previous": previous["value"],
                "change": futures_weekly._delta(current["value"], previous["value"]),
                "data_as_of": current["period"],
                "previous_date": previous["period"],
                "known_at": current["known"] + "+08:00",
                "previous_known_at": previous["known"] + "+08:00",
                "source": _publication_source(current["url"]),
                "previous_source": _publication_source(previous["url"]),
                "stale": (cutoff.date() - day).days > 7,
            }
        )
    return rows


def _publication_source(url):
    host = urlsplit(url).hostname
    return {"name": {"www.sse.net.cn": "上海航运交易所"}.get(host, host), "url": url}


def _date(value):
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _number(value):
    return type(value) in (int, float) and isfinite(value)


def _card_changes(card, today, fresh_days):
    rows = []
    inventory = card["inventory"]
    if inventory["status"] == "collected" and (inventory.get("source") or {}).get("url"):
        values = inventory["values"]
        day = _date(inventory.get("data_as_of"))
        if (
            day is not None
            and day <= today
            and _number(values.get("change"))
            and values.get("unit")
            and _number(values.get("quantity"))
        ):
            rows.append(
                {
                    "label": "仓单数量",
                    "scope": " · ".join(filter(None, (values.get("segment"), values.get("basis")))),
                    "unit": values["unit"],
                    "current": values["quantity"],
                    "previous": None,
                    "change": values["change"],
                    "data_as_of": day.isoformat(),
                    "previous_date": None,
                    "source": inventory["source"],
                    "previous_source": None,
                    "stale": (today - day).days > fresh_days,
                }
            )
    history = card["spot_history"]
    if history["status"] == "collected":
        values = history["values"]
        obs = values["observations"]
        if len(obs) >= 2:
            earlier, later = obs[-2:]
            before, day = _date(earlier.get("date")), _date(later.get("date"))
            unit = values["unit"]
            if (
                before is not None
                and day is not None
                and before < day <= today
                and later["date"] == history["data_as_of"]
                and all(
                    _number(p.get("price"))
                    and p.get("unit") == unit
                    and p.get("source") == futures_basis.SOURCE_NAME
                    for p in (earlier, later)
                )
                and (history.get("source") or {}).get("url") == futures_basis.page_url(day)
            ):
                rows.append(
                    {
                        "label": "现货报价",
                        "scope": "生意社同品种最近两次公开报价",
                        "unit": unit,
                        "previous": earlier["price"],
                        "current": later["price"],
                        "change": futures_weekly._delta(later["price"], earlier["price"]),
                        "previous_date": earlier["date"],
                        "data_as_of": later["date"],
                        "source": history["source"],
                        "previous_source": {
                            "name": futures_basis.SOURCE_NAME,
                            "url": futures_basis.page_url(before),
                        },
                        "stale": (today - day).days > fresh_days,
                    }
                )
    return rows


def _technical(card, today, fresh_days):
    market, technicals = card["market"], card["technicals"]
    values = technicals.get("values") or {}
    price = market.get("values") or {}
    close, fast, slow = price.get("close"), values.get("ema20"), values.get("ema60")
    day = market.get("data_as_of")
    observed = _date(day)
    item = dict(
        status="unavailable",
        bias="unavailable",
        label="暂不能判定",
        reason="需要同一连续日线的收盘价、20日和60日指数均线。",
        data_as_of=day,
        source=market.get("source"),
        close=close,
        ema20=fast,
        ema60=slow,
        quote_unit=price.get("quote_unit"),
    )
    if (
        market["status"] != "collected"
        or observed is None
        or technicals.get("data_as_of") != day
        or not (market.get("source") or {}).get("url")
        or not all(_number(v) and v > 0 for v in (close, fast, slow))
    ):
        return item
    age = (today - observed).days
    if age < 0:
        return item
    if age > fresh_days:
        return {**item, "status": "stale", "reason": f"数据止于 {day}，等待新的日线。"}
    if close > fast > slow:
        bias, label, reason = "bullish", "偏多", "收盘高于20日指数均线，20日线高于60日线。"
    elif close < fast < slow:
        bias, label, reason = "bearish", "偏空", "收盘低于20日指数均线，20日线低于60日线。"
    elif close == fast or fast == slow:
        bias, label, reason = "neutral", "中性", "收盘与20日线持平，或20日线与60日线持平。"
    else:
        bias, label, reason = "mixed", "短中期分歧", "收盘相对20日线的方向，与20日线相对60日线的方向相反。"
    if age == 0:
        reason += "当日日线可能尚未定盘。"
    return {**item, "status": "collected", "bias": bias, "label": label, "reason": reason}


def research_brief(card, now, jm_editions, *, fresh_days):
    """Use each series' own date, units and release horizon."""
    today = now.astimezone(futures_weekly.SHANGHAI).date()
    rows = _weekly_rows(card["symbol"], now, jm_editions) + _card_changes(card, today, fresh_days)
    if not rows:
        status, reason = "unavailable", "尚无同口径的可比读数。"
    elif all(row["stale"] for row in rows):
        status, reason = "stale", "当前只收录了历史变化，等待新一期披露。"
    else:
        status, reason = "collected", None
    return dict(
        fundamentals=dict(status=status, rows=rows, reason=reason),
        technical=_technical(card, today, fresh_days),
    )
