"""Point-in-time Europe freight research and first-publication SCFIS judgments."""

import re
from datetime import datetime
from decimal import Decimal
from itertools import pairwise

from . import futures_weekly as weekly
from .futures_weekly_releases import (
    CALENDAR,
    EC_WINDOW,
    MATURITY,
    NE3_FIRST_VERIFIED,
    NE3_NOTICE,
    NE3_SCHEDULED,
    RELEASES,
    SCFIS,
    SERIES,
    SETTLEMENT_RULE,
)

RULE_VERSION = "ec-scfis-v1"
CONTRACT, FIXING_DAYS, SETTLEMENT_KEY = EC_WINDOW
# Reviewed November–December dates from the 2026 SSE index calendar (CALENDAR).
# Keep verified publication dates even when the delivery contract rolls forward.
INDEX_PUBLICATION_DAYS = (
    "2026-11-02",
    "2026-11-09",
    "2026-11-16",
    "2026-11-23",
    "2026-11-30",
    "2026-12-07",
    "2026-12-14",
    "2026-12-21",
    "2026-12-28",
)
NEXT_PUBLICATION = dict(pairwise(INDEX_PUBLICATION_DAYS))
CALENDAR_PUBLISHED = "2025-12-05"
CALENDAR_KNOWN = "2025-12-06T00:00"
DIRECTIONS = {"index_down": ("下降", -1), "index_up": ("上涨", 1)}


def _local(cutoff):
    if cutoff.utcoffset() is None:
        raise ValueError("cutoff requires a timezone")
    return cutoff.astimezone(weekly.SHANGHAI)


def _known(stamp):
    return datetime.fromisoformat(stamp).replace(tzinfo=weekly.SHANGHAI)


def _topic(question):
    q = question.lower()
    if any(w in q for w in ("做多", "做空", "止损", "买入", "卖出", "目标价", "螺纹", "豆粕", "焦煤")):
        return None
    if re.search(r"scfi(?!s)", q) or any(code != CONTRACT.lower() for code in re.findall(r"ec[0-9]{4}", q)):
        return None
    if any(w in q for w in ("停航", "运力", "舱位", "航次", "替代船", "撤班")):
        return "capacity"
    if any(w in q for w in ("交割", "日结算", "结算价", "定价日", "三次定价", "ec2611", "最后一期倒算")):
        return "settlement"
    if any(w in q for w in ("scfis", "欧线", "集运", "运价", "指数", "基本面")):
        return "index"
    return None


def _evidence(row):
    return dict(
        label=f"{row['period']} {row['label']}",
        value=f"{row['value']:.2f} {row['unit']}",
        data_date=row["period"],
        as_of=_known(row["known"]).isoformat(),
        source_name="上海航运交易所" if row["key"] == "index" else "上海国际能源交易中心",
        source_url=row["url"],
    )


def _reference(label, value, url, publisher):
    return dict(label=label, value=value, source_url=url, source_name=publisher, data_date=None, as_of=None)


def _calendar_evidence(value):
    item = _reference("指数发布日历", value, CALENDAR, "上海航运交易所")
    item.update(data_date=CALENDAR_PUBLISHED, as_of=_known(CALENDAR_KNOWN).isoformat())
    return item


def _index(result, indexes, local):
    if not indexes:
        return dict(
            result,
            status="unavailable",
            headline="等待欧线指数",
            explanation="尚无截至该时间可核对的欧线指数。",
        )
    latest = indexes[-1]
    period, value = latest["period"], latest["value"]
    parts = [f"{period} 上海出口至欧洲基本港的离港后结算运价指数为 {value:.2f} 点。"]
    change = weekly._weekly_change(indexes, "index")
    if change is not None:
        parts.append(f"较上一周变化 {change:+.2f} 点。")
    evidence = [_evidence(r) for r in indexes[-2:]]
    target = NEXT_PUBLICATION.get(period)
    choices = []
    if target and target > local.date().isoformat():
        base = {k: latest[k] for k in ("key", "scope", "unit", "value", "period", "known")}
        base.update(version=RULE_VERSION, target=target, source_url=latest["url"], calendar_url=CALENDAR)
        evidence.insert(0, _calendar_evidence(f"目标发布日 {target}"))
        for cid, (label, sign) in DIRECTIONS.items():
            rule = f"{target} 上海航运交易所同一欧线指数的首个收录值须{'低于' if sign < 0 else '高于'} {value:.2f} 点；持平不满足判断。"
            choices.append(dict(id=cid, label=f"下期欧线指数{label}", rule=rule, baseline=base))
        parts.append(f"下一次核对日期为 {target}，以同口径首次收录值检验方向。")
    else:
        parts.append("待核对新的指数与后续发布日期。")
    return dict(
        result,
        status="available",
        headline="欧线结算运价指数",
        explanation="".join(parts),
        evidence=evidence,
        choices=choices,
    )


def _settlement(result, rows, local):
    fixes = [r for r in rows if r["key"] == "index" and r["period"] in FIXING_DAYS and r["url"] == SCFIS]
    parts = [
        f"{CONTRACT} 交割结算取 {'、'.join(FIXING_DAYS)} 三次欧线指数的算术平均，已收录 {len(fixes)}/3 次。"
    ]
    parts.extend(f"{r['period']} 为 {r['value']:.2f} 点。" for r in fixes)
    evidence = [_evidence(r) for r in fixes]
    evidence.extend(
        [
            _calendar_evidence("欧洲航线"),
            _reference("合约到期日", f"{CONTRACT} · {FIXING_DAYS[-1]}", MATURITY, "上海国际能源交易中心"),
            _reference("交割结算规则", "三次定价的算术平均", SETTLEMENT_RULE, "上海国际能源交易中心"),
        ]
    )
    daily = [r for r in rows if r["key"] == SETTLEMENT_KEY and FIXING_DAYS[1] < r["period"] < FIXING_DAYS[2]]
    total = sum(Decimal(str(r["value"])) for r in fixes)
    if len(fixes) == 3:
        parts.append(f"三期均价为 {total / 3:.2f} 点，最终交割价以交易所公布为准。")
    else:
        missing = "、".join(d for d in FIXING_DAYS if d not in {r["period"] for r in fixes})
        parts.append(f"尚缺 {missing} 定价，三期均价待定。")
        if (
            daily
            and [r["period"] for r in fixes] == list(FIXING_DAYS[:2])
            and local.date().isoformat() < FIXING_DAYS[-1]
        ):
            last = daily[-1]
            threshold = 3 * Decimal(str(last["value"])) - total
            parts.append(
                f"{last['period']} 该合约日结算为 {last['value']:.2f} 点。要使三期均价恰好等于这一天的期货结算价，"
                f"末次指数需为 {threshold:.2f} 点。这个条件值只用于对照，末次实绩仍待发布。"
            )
            evidence.append(_evidence(last))
    return dict(
        result,
        status="available",
        headline=f"{CONTRACT} 交割定价",
        explanation="".join(parts),
        evidence=evidence,
    )


def _capacity(result, local):
    if not weekly._has_carrier_notice(local.strftime("%Y-%m-%dT%H:%M")):
        return dict(
            result,
            status="unavailable",
            headline="缺少该时段可核对的航次公告",
            explanation="需要船司的航次安排、替代舱位和全航线实际离港运力。",
        )
    notice = _reference(
        "上海出发的航次安排", "NE3 651W 计划取消，同日提供 NE1 650W 替代", NE3_NOTICE, "赫伯罗特"
    )
    notice.update(as_of=_known(NE3_FIRST_VERIFIED).isoformat(), data_date=NE3_SCHEDULED)
    return dict(
        result,
        status="available",
        headline="计划停航与替代航次",
        evidence=[notice],
        explanation=(
            f"{NE3_SCHEDULED} 上海出发的 NE3 651W 计划取消，同日提供 NE1 650W 替代。计划航次在 {CONTRACT} 到期后。"
            "核对净运力变化还需要两班实际可订舱位、全航线离港运力和装载率。"
        ),
    )


async def analyze(question: str, cutoff: datetime):
    local = _local(cutoff)
    result = dict(
        question=question,
        as_of=local.isoformat(),
        method="按上海时间回看已核对的指数、合约和航次公告",
        choices=[],
        evidence=[],
    )
    topic = _topic(question)
    if topic is None:
        return dict(
            result,
            status="unsupported",
            headline="可以查证欧线运价、交割定价和航次",
            explanation="例如：下期欧线结算运价指数怎样复核？",
        )
    if topic == "capacity":
        return _capacity(result, local)
    data = weekly.research("EC", local)
    rows = data["rows"] if data else []
    if topic == "settlement":
        if not rows:
            return dict(
                result,
                status="unavailable",
                headline="缺少该时段可核对的合约数据",
                explanation=f"等待 {CONTRACT} 对应的已发布数据。",
            )
        return _settlement(result, rows, local)
    indexes = [r for r in rows if r["key"] == "index" and r["url"] == SCFIS]
    return _index(result, indexes, local)


async def review(choice: dict, cutoff: datetime):
    local = _local(cutoff)
    base = choice.get("baseline", {})
    rule = DIRECTIONS.get(choice.get("id"))
    fault = dict(
        status="unverifiable",
        headline="无法核对原判断",
        explanation="需要封存的欧线指数、发布日、来源和目标期。",
        evidence=[],
    )
    _, scope, unit = SERIES["EC"]["index"]
    target = NEXT_PUBLICATION.get(base.get("period"))
    expected = dict(
        version=RULE_VERSION,
        key="index",
        scope=scope,
        unit=unit,
        source_url=SCFIS,
        target=target,
        calendar_url=CALENDAR,
    )
    if not rule or not target or any(base.get(k) != v for k, v in expected.items()):
        return fault
    originals = [
        r
        for r in RELEASES["EC"]
        if r.period == base["period"]
        and r.known == base.get("known")
        and r.url == SCFIS
        and "index" in r.values
    ]
    if not originals or originals[0].values["index"] != base.get("value"):
        return fault
    started = choice.get("tracking_started_at")
    if started:
        sealed = _local(datetime.fromisoformat(started))
        if sealed < _known(base["known"]) or sealed.date().isoformat() >= target:
            return dict(
                fault,
                headline="保存时间超出可跟踪窗口",
                explanation=f"须在基准指数可见后且在 {target} 上海时间零点前保存。",
            )
    matches = [
        r
        for r in RELEASES["EC"]
        if r.period == target and r.url == SCFIS and "index" in r.values and _known(r.known) <= local
    ]
    if not matches:
        return dict(
            status="pending",
            headline="等待指定期欧线指数",
            explanation=f"等待 {target} 上海出口至欧洲基本港的结算运价指数。",
            evidence=[],
        )
    first = min(matches, key=lambda r: r.known)
    value = first.values["index"]
    change = weekly._delta(value, base["value"])
    supported = rule[1] * change > 0
    row = dict(
        period=target,
        label=SERIES["EC"]["index"][0],
        value=value,
        unit=unit,
        key="index",
        known=first.known,
        url=first.url,
    )
    return dict(
        status="supported" if supported else "contradicted",
        headline="原判断得到支持" if supported else "原判断未得到支持",
        explanation=f"欧线指数由 {base['period']} 的 {base['value']:.2f} 点变为 {target} 的 {value:.2f} 点，变化 {change:+.2f} 点。",
        evidence=[_evidence(row)],
    )
