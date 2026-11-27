"""Point-in-time analysis of dated public weekly releases."""

from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .futures_weekly_releases import (
    CALENDAR,
    EC_WINDOW,
    MATURITY,
    NE3_FIRST_VERIFIED,
    NE3_NOTICE,
    NE3_SCHEDULED,
    RELEASES,
    SERIES,
    SETTLEMENT_RULE,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _delta(a, b):
    return float(Decimal(str(a)) - Decimal(str(b)))


def _weekly_change(rows, key):
    series = [row for row in rows if row["key"] == key]
    if len(series) < 2:
        return None
    before, after = series[-2:]
    if date.fromisoformat(after["period"]) - date.fromisoformat(before["period"]) != timedelta(days=7):
        return None
    return _delta(after["value"], before["value"])


def _steel(rows):
    latest = {r["key"]: r for r in rows}
    if not all(k in latest for k in ("output", "mill", "society")):
        return ["等待同一周的产量、厂库与35城社会库存。"]
    period = latest["mill"]["period"]
    if any(latest[k]["period"] != period for k in ("output", "society")):
        return ["产销存样本周尚未对齐。"]
    output, mill, society = (_weekly_change(rows, key) for key in ("output", "mill", "society"))
    if None in (output, mill, society):
        return ["已有单周产销存，等待相邻调查周再拆解库存变化。"]
    inventory_change = round(mill + society, 2)
    apparent = _delta(latest["output"]["value"], inventory_change)
    lines = [
        f"产量环比 {output:+.2f}、厂库 {mill:+.2f}、社库 {society:+.2f} 万吨。样本表观消费为 {apparent:.2f} 万吨，由产量扣除两类库存净变化得到。"
    ]
    if mill > 0 and society < 0:
        lines.append("库存向两个方向变化。社库下降的同时钢厂积货，仅凭社库下降不足以判定需求回暖。")
    friday = date.fromisoformat(period) + timedelta(days=1)
    change = _weekly_change(rows, "purchases")
    if change is None or latest["purchases"]["period"] != friday.isoformat():
        lines.append("本周沪市线螺终端采购尚缺可比读数，等待西本周报交叉核对。")
    else:
        lines.append(f"同期沪市线螺终端采购环比 {change:+.2f} 万吨。该样本覆盖上海线材和螺纹，不能外推全国。")
        if change < 0 and mill > 0 and society < 0:
            lines.append("暂缓按需求复苏看多。下一周若同样本采购回升且厂库下降，重新评估这一判断。")
    return lines


def _meal(rows):
    result = []
    actuals = {r["period"]: r["value"] for r in rows if r["key"] == "crush"}
    plans = [r for r in rows if r["key"] == "crush_plan"]
    last_actual = max(actuals, default=None)
    for plan in plans:
        week = plan["period"]
        if week == last_actual:
            error = _delta(actuals[week], plan["value"])
            line = f"{week} 当周实际压榨与提前发布的预估相差 {error:+.2f} 万吨。"
            previous = (date.fromisoformat(week) - timedelta(days=7)).isoformat()
            if previous in actuals:
                baseline = _delta(actuals[week], actuals[previous])
                line += f"若沿用前周实绩，偏差为 {baseline:+.2f} 万吨。"
            result.append(line)
        elif plan is plans[-1] and (last_actual is None or week > last_actual):
            result.append(f"{week} 当周压榨只有预估，等待同周实绩。")
    change = _weekly_change(rows, "arrivals")
    if change is not None:
        result.append(f"周度到港折量变化 {change:+.2f} 万吨。卸船、入厂和压榨之间存在时滞。")
    beans, meal = (_weekly_change(rows, key) for key in ("beans_stock", "meal_stock"))
    if beans is not None and meal is not None:
        result.append(f"同一调查口径的油厂原料豆库存环比 {beans:+.2f}、豆粕库存 {meal:+.2f} 万吨。")
        if beans > 0 and meal < 0:
            result.append(
                "原料累库、成品去库。要检验需求拉动，下一周核对实际提货与压榨；若压榨回升后成品转为累库，重估供应压力。"
            )
    else:
        result.append("等待同周原料豆及成品库存；饲料厂库存天数只反映下游备货。")
    return result


def _has_carrier_notice(cutoff):
    # The carrier page supplies no publication time. Record the first verified read.
    return cutoff >= NE3_FIRST_VERIFIED and cutoff[:10] <= NE3_SCHEDULED


def _freight(rows, cutoff):
    result = []
    change = _weekly_change(rows, "index")
    if change is not None:
        result.append(f"欧线离港后结算指数周变动 {change:+.2f} 点。")
    contract, fixing_days, settlement_key = EC_WINDOW
    if cutoff[:10] >= fixing_days[0]:
        fixes = {r["period"]: r["value"] for r in rows if r["key"] == "index" and r["period"] in fixing_days}
        dates = "、".join(fixing_days)
        result.append(f"{contract} 三次定价日为 {dates}，已收录 {len(fixes)}/3 次。")
        if len(fixes) == 3:
            result.append(f"三期指数均价为 {sum(fixes.values()) / 3:.2f} 点，最终交割价以交易所公布为准。")
        if cutoff[:10] > fixing_days[-1] and len(fixes) < 3:
            result.append(f"{contract} 已到期，等待补录完整定价和交易所交割结果。")
        elif set(fixes) == set(fixing_days[:2]):
            result.append(
                f"已公布的两项合计 {round(sum(fixes.values()), 2):.2f} 点。剩余定价每变化 30 点，最终三期均价变化 10 点。"
            )
            settlements = [
                r
                for r in rows
                if r["key"] == settlement_key and fixing_days[1] < r["period"] < fixing_days[2]
            ]
            if settlements:
                last = settlements[-1]
                hurdle = _delta(3 * last["value"], round(sum(fixes.values()), 2))
                result.append(
                    f"以 {last['period']} {contract} 日结算 {last['value']:.2f} 点倒算，最后一期需为 {hurdle:.2f} 点才使三期均价等于该价格。"
                )
    if cutoff >= "2026-11-25T08:20" and cutoff[:10] <= "2026-12-15":
        result.append(
            "赫伯罗特公告计划取消12月15日上海出发的 NE3 651W，同日提供 NE1 650W 替代。缺两班实际可订舱位及全航线离港运力，不能量化净减少的 TEU。"
        )
    return result


def research(symbol, as_of):
    if symbol not in RELEASES:
        return None
    if as_of.utcoffset() is None:
        raise ValueError("research cutoff must include its timezone")
    cutoff = as_of.astimezone(SHANGHAI).isoformat(timespec="minutes")[:16]
    releases = [r for r in RELEASES[symbol] if r.known <= cutoff]
    first_crush = {}
    if symbol == "M":
        for r in releases:
            if "crush" in r.values:
                first_crush[r.period] = min(first_crush.get(r.period, r.known), r.known)
    vintages = {}
    for release in releases:
        for key, value in release.values.items():
            # A forecast must have been public by survey day and strictly before the first actual.
            # Later revisions to either series cannot move this boundary.
            if key == "crush_plan" and (
                release.known[:10] > release.period
                or (release.period in first_crush and release.known >= first_crush[release.period])
            ):
                continue
            label, scope, unit = SERIES[symbol][key]
            row = dict(
                key=key,
                label=label,
                scope=scope,
                unit=unit,
                value=value,
                period=release.period,
                known=release.known,
                url=release.url,
                publisher=urlsplit(release.url).hostname,
            )
            previous = vintages.get((key, release.period))
            if previous is None or release.known > previous["known"]:
                vintages[key, release.period] = row
    rows = list(vintages.values())
    if not rows:
        return None
    rows.sort(key=lambda r: (r["period"], r["known"], r["key"]))
    latest_actual = max((r["period"] for r in rows if r["key"] != "crush_plan"), default=None)
    historical = (
        latest_actual is not None
        and (date.fromisoformat(cutoff[:10]) - date.fromisoformat(latest_actual)).days > 7
    )
    if symbol == "RB":
        lines = _steel(rows)
    elif symbol == "M":
        lines = _meal(rows)
    else:
        lines = _freight(rows, cutoff)
    sources = list(dict.fromkeys(r["url"] for r in rows))
    if symbol == "EC":
        sources.extend([CALENDAR, MATURITY, SETTLEMENT_RULE])
        if _has_carrier_notice(cutoff):
            sources.append(NE3_NOTICE)
    return {
        "as_of": cutoff + "+08:00",
        "last_observed": latest_actual,
        "historical": historical,
        "rows": rows,
        "reading": lines,
        "sources": sources,
    }
