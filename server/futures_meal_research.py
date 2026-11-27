"""Point-in-time oilseed crush and soybean meal inventory research."""

from datetime import date, datetime, timedelta

from . import futures_weekly as weekly
from .futures_weekly_releases import RELEASES, SERIES

VERSION = "m-weekly-v1"
# Baseline series, observed series, target-week offset, direction, label.
RULES = {
    "meal_stock_down": ("meal_stock", "meal_stock", 7, -1, "下周油厂豆粕去库"),
    "beans_stock_up": ("beans_stock", "beans_stock", 7, 1, "下周油厂原料豆累库"),
    "crush_below_plan": ("crush_plan", "crush", 0, -1, "实际压榨低于事前预估"),
    "crush_up": ("crush", "crush", 7, 1, "下周实际压榨回升"),
}
STOCKS = ("meal_stock", "beans_stock")
CRUSH = ("crush", "crush_plan")


def _topics(question):
    if any(
        w in question
        for w in (
            "买入",
            "卖出",
            "做多",
            "做空",
            "目标价",
            "资金流",
            "价格",
            "涨跌",
            "走势",
            "基差",
            "饲料厂库存",
            "库存天数",
            "到港",
            "螺纹",
            "焦煤",
            "集运",
            "菜粕",
        )
    ):
        return ()
    stocks = any(w in question for w in ("库存", "去库", "累库", "厂库", "原料豆", "成品"))
    crush = any(w in question for w in ("压榨", "开机", "开工", "减产"))
    if any(w in question for w in ("需求", "供需", "基本面", "提货")) or (stocks and crush):
        return (*STOCKS, *CRUSH)
    if stocks:
        return STOCKS
    return CRUSH if crush else ()


def _evidence(key, period, known, value, url):
    label, scope, unit = SERIES["M"][key]
    return dict(
        label=f"{label} · {scope}",
        value=f"{value:.2f} {unit}",
        data_date=period,
        as_of=datetime.fromisoformat(known).replace(tzinfo=weekly.SHANGHAI).isoformat(),
        source_name="Mysteel 农产品",
        source_url=url,
    )


def _choices(latest, cutoff):
    choices = []
    for cid, (key, observed, days, direction, label) in RULES.items():
        row = latest.get(key)
        if not row:
            continue
        target = date.fromisoformat(row["period"]) + timedelta(days=days)
        if key == "crush_plan" and latest.get(observed, {}).get("period", "") >= target.isoformat():
            continue
        if cutoff.date() >= target:
            continue
        base = {k: row[k] for k in ("key", "period", "known", "scope", "unit", "value")}
        base.update(version=VERSION, target=target.isoformat(), source_url=row["url"])
        comparator = "低于" if direction == -1 else "高于"
        rule = (
            f"{target} 当周{SERIES['M'][observed][0]}须{comparator} "
            f"{row['period']} 当周{row['label']} {row['value']:.2f} {row['unit']}（{row['scope']}）。"
        )
        choices.append(dict(id=cid, label=label, rule=rule, baseline=base))
    return choices


def _stock_reading(rows, latest):
    lines = []
    for key in STOCKS:
        if key not in latest:
            continue
        row = latest[key]
        delta = weekly._weekly_change(rows, key)
        comparison = f"，环比 {delta:+.2f} 万吨" if delta is not None else "，缺少前一周同口径读数"
        lines.append(f"{row['period']} {row['label']} {row['value']:.2f} 万吨{comparison}。")
    if not lines:
        return ["尚无已披露的油厂原料豆和成品库存。"]
    if not all(k in latest for k in STOCKS) or latest[STOCKS[0]]["period"] != latest[STOCKS[1]]["period"]:
        lines.append("两项库存调查周尚未对齐。")
    return lines


def _crush_reading(rows, latest):
    lines = []
    actual, plan = latest.get("crush"), latest.get("crush_plan")
    if actual:
        delta = weekly._weekly_change(rows, "crush")
        comparison = f"，环比 {delta:+.2f} 万吨" if delta is not None else ""
        lines.append(f"{actual['period']} 大豆实际压榨 {actual['value']:.2f} 万吨{comparison}。")
        prior_plan = next(
            (r for r in rows if r["key"] == "crush_plan" and r["period"] == actual["period"]), None
        )
        if prior_plan:
            lines.append(
                f"同周事前预估 {prior_plan['value']:.2f} 万吨（{prior_plan['known'][:10]} 发布），"
                f"实际较预估 {weekly._delta(actual['value'], prior_plan['value']):+.2f} 万吨。"
            )
        else:
            lines.append("未收录该周事前压榨预估。")
    if plan and (not actual or plan["period"] > actual["period"]):
        lines.append(
            f"{plan['period']} 当周预估压榨 {plan['value']:.2f} 万吨"
            f"（{plan['known'][:10]} 发布），该周实际压榨尚待披露。"
        )
    return lines or ["尚无带调查周和发布时间的压榨记录。"]


async def analyze(question: str, cutoff: datetime):
    if cutoff.utcoffset() is None:
        raise ValueError("cutoff requires a timezone")
    local = cutoff.astimezone(weekly.SHANGHAI)
    keys = _topics(question)
    result = dict(
        question=question,
        as_of=local.isoformat(),
        method="油厂周报按上海披露时间核对",
        evidence=[],
        choices=[],
    )
    if not keys:
        return dict(
            **result,
            status="unsupported",
            headline="可以查油厂库存和压榨",
            explanation="例如：实际压榨达到预估了吗？",
        )
    data = weekly.research("M", local)
    rows = [r for r in data["rows"] if r["key"] in keys] if data else []
    if not rows:
        return dict(
            **result,
            status="unavailable",
            headline="截至提问时尚无相应周报",
            explanation="等待带调查周和发布时间的油厂样本数据。",
        )
    latest = {r["key"]: r for r in rows}
    parts = []
    if "meal_stock" in keys:
        parts.extend(_stock_reading(rows, latest))
    if "crush" in keys:
        parts.extend(_crush_reading(rows, latest))
    if keys != STOCKS and keys != CRUSH:
        periods = [latest[k]["period"] for k in (*STOCKS, "crush") if k in latest]
        if len(periods) != 3 or len(set(periods)) != 1:
            parts.append("压榨与两项库存尚未齐报，需等同一周的数据再判断。")
        else:
            beans, meal, crush = (
                weekly._weekly_change(rows, k) for k in ("beans_stock", "meal_stock", "crush")
            )
            if beans is not None and meal is not None and beans > 0 > meal:
                parts.append("原料累库、成品去库。")
            if meal is not None and meal < 0 and crush is not None:
                parts.append(
                    "实际压榨未比前周减少，单靠减产难以解释去库。"
                    if crush >= 0
                    else "同期压榨也下降，供应收缩可能参与去库。"
                )
        parts.append("区分履行旧合同与新增饲料采购，还需同期实际提货和新增订单。")
    evidence = []
    for key in keys:
        history = [r for r in rows if r["key"] == key]
        if key == "crush_plan" and "crush" in latest:
            history = [r for r in history if r["period"] >= latest["crush"]["period"]]
        for row in history[-2:]:
            evidence.append(_evidence(key, row["period"], row["known"], row["value"], row["url"]))
    choices = _choices(latest, local)
    if not choices:
        parts.append("现有基准已到下一调查日，等待新一期周报后再跟踪。")
    result.update(
        status="available",
        headline={STOCKS: "油厂成品与原料库存", CRUSH: "实际压榨与事前预估"}.get(keys, "豆粕库存与压榨核对"),
        explanation="".join(parts),
        evidence=evidence,
        choices=choices,
    )
    return result


def _outcome(status, headline, explanation, evidence=None):
    return dict(status=status, headline=headline, explanation=explanation, evidence=evidence or [])


async def review(choice: dict, cutoff: datetime):
    if cutoff.utcoffset() is None:
        raise ValueError("cutoff requires a timezone")
    local = cutoff.astimezone(weekly.SHANGHAI)
    rule, base = RULES.get(choice["id"]), choice["baseline"]
    if not rule or base.get("version") != VERSION:
        return _outcome("unverifiable", "判断规则无法核对", "缺少原判断保存的豆粕周报规则。")
    key, observed, days, direction, _ = rule
    label, scope, unit = SERIES["M"][observed]
    target = (date.fromisoformat(base["period"]) + timedelta(days=days)).isoformat()
    if (base["key"], base["scope"], base["unit"], base["target"]) != (key, scope, unit, target):
        return _outcome("unverifiable", "调查口径与保存时不一致", "需要原判断的样本、单位和目标周。")
    started = choice.get("tracking_started_at")
    if started and datetime.fromisoformat(started).astimezone(weekly.SHANGHAI).date().isoformat() >= target:
        return _outcome("unverifiable", "保存时已到目标调查日", f"须在 {target} 上海时间零点前保存。")
    known = local.strftime("%Y-%m-%dT%H:%M")
    first = min(
        (r for r in RELEASES["M"] if r.period == target and observed in r.values and r.known <= known),
        key=lambda r: r.known,
        default=None,
    )
    if first is None:
        return _outcome("pending", f"等待 {target} 当周数据", f"等待{scope}披露{label}。")
    actual = first.values[observed]
    delta = weekly._delta(actual, base["value"])
    text = (
        f"{target} 当周{label} {actual:.2f} {unit}，"
        f"较 {base['period']} 当周{SERIES['M'][key][0]} {base['value']:.2f} {unit} 变化 {delta:+.2f} {unit}。"
    )
    supported = direction * delta > 0
    return _outcome(
        "supported" if supported else "contradicted",
        "原判断得到支持" if supported else "原判断未得到支持",
        text,
        [_evidence(observed, first.period, first.known, actual, first.url)],
    )
