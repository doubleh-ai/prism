"""Dated rebar supply, inventory and end-user purchase research for the futures journal."""

from datetime import date, datetime, timedelta

from . import futures_weekly as weekly
from .futures_weekly_releases import RELEASES, SERIES

# Persisted choices bind the direction, sample, comparison week and first publication.
# New rules must have a different version so saved judgements keep their meaning.
RULE_VERSION = "rb-weekly-v1"
RULES = {
    "society_down": ("society", "下周社会库存下降", -1),
    "mill_down": ("mill", "下周钢厂库存下降", -1),
    "output_down": ("output", "下周螺纹产量下降", -1),
    "purchases_up": ("purchases", "下周沪市终端采购回升", 1),
}


def _topic(question):
    if any(
        word in question
        for word in (
            "买入",
            "卖出",
            "做多",
            "做空",
            "目标价",
            "止损",
            "资金流",
            "铁矿石",
            "焦煤",
            "豆粕",
            "集运",
        )
    ):
        return None
    if any(word in question for word in ("终端采购", "终端成交", "沪市", "西本", "采购量")):
        return "purchases"
    if any(word in question for word in ("需求", "表需", "表观消费", "供需", "去库", "减产", "基本面")):
        return "balance"
    if any(word in question for word in ("库存", "社库", "厂库", "累库", "产量", "产销存")):
        return "stocks"
    return None


def _evidence(key, period, known, value, url):
    label, scope, unit = SERIES["RB"][key]
    return {
        "label": f"{label} · {scope}",
        "value": f"{value:.2f} {unit}",
        "data_date": period,
        "as_of": datetime.fromisoformat(known).replace(tzinfo=weekly.SHANGHAI).isoformat(),
        "source_name": "西本"
        if key == "purchases"
        else "Mysteel（转载）"
        if "eastmoney.com/" in url
        else "Mysteel",
        "source_url": url,
    }


def _choices(latest, keys, cutoff):
    choices = []
    for cid, (key, label, direction) in RULES.items():
        row = latest.get(key)
        if not row or key not in keys:
            continue
        target = date.fromisoformat(row["period"]) + timedelta(days=7)
        if target <= cutoff.date():
            continue
        baseline = {k: row[k] for k in ("value", "known", "period", "scope", "unit", "key")}
        baseline.update(target=target.isoformat(), source_url=row["url"], version=RULE_VERSION)
        rule = (
            f"目标调查日 {target}，{row['label']}须{'低于' if direction < 0 else '高于'}"
            f" {row['period']} 的 {row['value']:.2f} {row['unit']}（{row['scope']}）。"
        )
        choices.append(dict(id=cid, label=label, rule=rule, baseline=baseline))
    return choices


async def analyze(question: str, cutoff: datetime):
    if cutoff.utcoffset() is None:
        raise ValueError("cutoff requires a timezone")
    local = cutoff.astimezone(weekly.SHANGHAI)
    result = dict(
        question=question,
        as_of=local.isoformat(),
        method="依据已核对发布时间的螺纹钢周报计算",
        choices=[],
        evidence=[],
    )
    topic = _topic(question)
    if topic is None:
        return dict(
            **result,
            status="unsupported",
            headline="可以问螺纹钢产销存和终端采购",
            explanation="例如：社库和厂库的变化一致吗？",
        )
    keys = {
        "stocks": ("output", "mill", "society"),
        "balance": ("output", "mill", "society", "purchases"),
        "purchases": ("purchases",),
    }[topic]
    data = weekly.research("RB", local)
    rows = [r for r in data["rows"] if r["key"] in keys] if data else []
    if not rows:
        return dict(
            **result,
            status="unavailable",
            headline="截至提问时尚无已收录的同口径周报",
            explanation="等待带有调查周、样本和发布时间的披露。",
        )
    latest = {r["key"]: r for r in rows}
    if topic == "purchases":
        row = latest["purchases"]
        parts = [f"{row['period']} 沪市线螺终端周采购为 {row['value']:.2f} 万吨。"]
        change = weekly._weekly_change(rows, "purchases")
        if change is not None:
            parts.append(f"较上周变化 {change:+.2f} 万吨。")
        parts.append("该样本限上海线材和螺纹，判断全国需求还要对照同周钢厂产量和两类库存。")
        headline = "沪市终端采购核对"
    elif topic == "stocks":
        parts = []
        for key in keys:
            if key not in latest:
                continue
            row = latest[key]
            change = weekly._weekly_change(rows, key)
            delta = f"，环比 {change:+.2f} 万吨" if change is not None else ""
            parts.append(f"{row['period']} {row['label']} {row['value']:.2f} 万吨{delta}。")
        headline = "螺纹钢产量、厂库与社库"
    else:
        parts = list(data["reading"])
        if len(parts) == 1 and all(k in latest for k in ("output", "mill", "society")):
            parts.append(
                f"本期产量 {latest['output']['value']:.2f}、厂库 {latest['mill']['value']:.2f}、"
                f"社库 {latest['society']['value']:.2f} 万吨。需要相邻两周变化才能核对去库。"
            )
        headline = "螺纹钢去库与需求核对"
    # A rolling front page or an undated quote cannot establish when a survey became public.
    # Until the next dated edition is registered, show its absence and never backfill it.
    dates = "、".join(
        f"{r['label']} {r['period']}" for r in latest.values() if r["key"] in ("society", "purchases")
    )
    parts.append(f"最近收录的调查周：{dates}。后续期次以核验后的周报为准。")
    evidence = []
    for key in keys:
        for row in [r for r in rows if r["key"] == key][-2:]:
            evidence.append(_evidence(key, row["period"], row["known"], row["value"], row["url"]))
    result.update(
        status="available",
        headline=headline,
        explanation="".join(parts),
        evidence=evidence,
        choices=_choices(latest, keys, local),
    )
    return result


def _outcome(status, headline, explanation, evidence=None):
    return dict(status=status, headline=headline, explanation=explanation, evidence=evidence or [])


async def review(choice: dict, cutoff: datetime):
    if cutoff.utcoffset() is None:
        raise ValueError("cutoff requires a timezone")
    local = cutoff.astimezone(weekly.SHANGHAI)
    rule = RULES.get(choice["id"])
    base = choice["baseline"]
    if rule is None or base.get("version") != RULE_VERSION:
        return _outcome("unverifiable", "判断规则无法核对", "缺少原判断保存的螺纹钢周报规则。")
    key, _, direction = rule
    label, scope, unit = SERIES["RB"][key]
    target = (date.fromisoformat(base["period"]) + timedelta(days=7)).isoformat()
    if (base["key"], base["scope"], base["unit"], base["target"]) != (key, scope, unit, target):
        return _outcome("unverifiable", "调查口径与封存时不一致", "需要相同调查样本、单位和目标周。")
    # Editors may publish different metrics during the survey day, before an omnibus
    # report is indexed. Seal strictly before that day, including for an older draft.
    started = choice.get("tracking_started_at")
    if started and datetime.fromisoformat(started).astimezone(weekly.SHANGHAI).date().isoformat() >= target:
        return _outcome("unverifiable", "保存时已到目标调查日", f"这条判断须在 {target} 上海时间零点前保存。")
    cutoff_text = local.strftime("%Y-%m-%dT%H:%M")
    matched = [
        release
        for release in RELEASES["RB"]
        if release.period == target and key in release.values and release.known <= cutoff_text
    ]
    if not matched:
        return _outcome("pending", "等待指定周的调查", f"等待收录 {target} 的{label}（{scope}）。")
    first = min(matched, key=lambda r: r.known)
    observed = _evidence(key, target, first.known, first.values[key], first.url)
    delta = weekly._delta(first.values[key], base["value"])
    supported = direction * delta > 0
    text = f"{scope}的{label}由 {base['period']} 的 {base['value']:.2f} 变为 {target} 的 {first.values[key]:.2f} {unit}，环比 {delta:+.2f} {unit}。"
    return _outcome(
        "supported" if supported else "contradicted",
        "原判断得到支持" if supported else "原判断未得到支持",
        text,
        [observed],
    )
