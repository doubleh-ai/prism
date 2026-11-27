"""Question-led, dated coking-coal research on two fixed contracts and consistent survey samples."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from . import futures_jm_sources as sources
from .futures import PRODUCTS

METRICS = {
    "restock": ("coke", "焦企煤库存会回升", "230 家独立焦企煤库存", "万吨"),
    "imports": ("port", "港口进口焦煤库存会下降", "16 港进口焦煤库存", "万吨"),
    "curve": ("spread", "近远月价差会扩大", "固定合约收盘价差", "元/吨"),
    "wait": ("coke", "暂缓归因，等待下期焦企调查", "230 家独立焦企煤库存", "万吨"),
}


def _local(cutoff):
    if cutoff.tzinfo is None:
        raise ValueError("cutoff requires a timezone")
    return cutoff.astimezone(sources.SHANGHAI)


def _topic(question, legs):
    q = question.strip()
    named_contracts = re.findall(r"(?<![A-Z])([A-Z]{1,3}[0-9]{3,4})(?![A-Z0-9])", q.upper())
    if any(code not in legs for code in named_contracts) or any(
        info[0] in q for symbol, info in PRODUCTS.items() if symbol != "JM"
    ):
        return None
    if not q or re.search(
        r"螺纹|动力煤|原油|豆粕|集运|沪金|股票|铜价|铜期|铁矿|买点|买入|止损|目标价|建议做[多空]|应该做[多空]|能涨吗|赚多少|天气",
        q,
    ):
        return None
    themes = {
        "imports": bool(re.search(r"进口|通关|港口|供应|供给|煤矿|矿山", q)),
        "restock": bool(re.search(r"补库|焦企|采购|耗用|需求|开工", q)),
        "spread": bool(re.search(r"价差|月差|近月|远月|合约|收盘|两腿|跨期|盘面", q)),
        "positions": bool(re.search(r"持仓|资金|净流入|增仓|减仓", q)),
    }
    if (themes["imports"] and themes["restock"]) or "供需" in q:
        return "drivers"
    if themes["positions"]:
        return "positions"
    if themes["imports"]:
        return "imports"
    if themes["restock"]:
        return "restock"
    if themes["spread"]:
        return "spread"
    return None


def _legs(day):
    # Research the nearest two Jan/May/Sep benchmark months strictly after
    # this month. The pair is frozen in every individual prediction.
    months = (
        f"JM{year % 100:02}{month:02}"
        for year in (day.year, day.year + 1)
        for month in (1, 5, 9)
        if (year, month) > (day.year, day.month)
    )
    return next(months), next(months)


def _next_day(day):
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _value(row, key, legs):
    if key == "spread":
        near, far = legs
        if near in row.values and far in row.values:
            return round(row.values[near] - row.values[far], 4)
        return None
    return row.values.get(key)


def _series(rows, key, legs):
    by_day = {}
    for row in rows:
        if _value(row, key, legs) is not None:
            by_day[row.period] = row
    return list(by_day.values())


def _number(v):
    return f"{v:,.4f}".rstrip("0").rstrip(".")


def _evidence(row, label, value):
    return dict(
        label=label,
        value=value,
        as_of=datetime.fromisoformat(row.known)
        .replace(tzinfo=sources.SHANGHAI)
        .isoformat(timespec="seconds"),
        data_date=row.period,
        source_name="钢之家" if "steelhome.com/" in row.url else "Mysteel",
        source_url=row.url,
    )


def _books(rows, legs):
    return {k: _series(rows, k, legs) for k in ("spread", "coke", "port", "mine")}


def _describe(key, row, legs):
    v = row.values
    if key == "spread":
        a, b = legs
        return f"{a} 收盘 {_number(v[a])}、{b} 收盘 {_number(v[b])}，近减远 {_number(_value(row, key, legs))} 元/吨。"
    if key == "coke":
        return f"煤库存 {_number(v['coke'])} 万吨，可用 {_number(v['days'])} 天，焦炉产能利用率 {_number(v['use'])}%。"
    if key == "port":
        return f"进口焦煤库存 {_number(v['port'])} 万吨。"
    return f"精煤日产 {_number(v['output'])} 万吨，矿山精煤库存 {_number(v['mine'])} 万吨。"


def _evidence_for(topic, books, legs):
    keys = {
        "drivers": ("spread", "coke", "port", "mine"),
        "imports": ("port", "mine"),
        "restock": ("coke",),
        "spread": ("spread",),
        "positions": ("spread",),
    }[topic]
    names = {
        "spread": "固定合约收盘",
        "coke": "230 家独立焦企",
        "port": "16 港进口焦煤",
        "mine": "523 家炼焦煤矿山",
    }
    evidence = []
    for key in keys:
        for row in books[key][-2:]:
            if topic == "positions":
                near, far = legs
                if near + "_change" not in row.values or far + "_change" not in row.values:
                    continue
                text = f"{near} 较前日持仓 {row.values[near + '_change']:+,} 手，{far} {row.values[far + '_change']:+,} 手。"
                evidence.append(_evidence(row, "按合约的持仓变化", text))
            else:
                evidence.append(_evidence(row, names[key], _describe(key, row, legs)))
    return evidence


def _analysis(topic, books, legs):
    market = books["spread"]
    coke = books["coke"]
    port = books["port"]
    if topic == "positions":
        headline = "按合约核对持仓变化"
        if market and all(k + "_change" in market[-1].values for k in legs):
            a, b = legs
            r = market[-1]
            return (
                headline,
                f"{r.period}，{a} 持仓变化 {r.values[a + '_change']:+,} 手，{b} {r.values[b + '_change']:+,} 手。持仓量同时计入多空的配对交易，无法据此确认净流入或识别移仓主体。",
            )
        return headline, "本次没有两份可比合约各自的持仓变化。"
    if topic == "spread":
        if len(market) < 2:
            return "还缺可比的两期收盘", "需要固定两个交割月份，在两个已披露的交易日都有有效收盘。"
        old, new = market[-2:]
        a, b = legs
        dx, dy = (round(new.values[k] - old.values[k], 4) for k in legs)
        ds = round(dx - dy, 4)
        headline = "近月相对走强" if ds > 0 else "近月相对走弱" if ds < 0 else "月差持平"
        return (
            headline,
            f"{old.period} 至 {new.period}，{a} 收盘变化 {dx:+g}、{b} {dy:+g} 元/吨。两者价差从 {_number(_value(old, 'spread', legs))} 变为 {_number(_value(new, 'spread', legs))} 元/吨。",
        )
    parts = []
    if topic in ("restock", "drivers"):
        if coke:
            text = f"{coke[-1].period}，230 家焦企煤库存 {_number(coke[-1].values['coke'])} 万吨。"
            if len(coke) > 1:
                text += f"比 {coke[-2].period} 变化 {coke[-1].values['coke'] - coke[-2].values['coke']:+.2f} 万吨。"
            parts.append(text)
        else:
            parts.append("当前没有可核对的 230 家焦企调查。")
    if topic in ("imports", "drivers"):
        if port:
            text = f"{port[-1].period}，16 港进口焦煤库存 {_number(port[-1].values['port'])} 万吨。"
            if len(port) > 1:
                text += f"比 {port[-2].period} 变化 {port[-1].values['port'] - port[-2].values['port']:+.2f} 万吨。"
            parts.append(text)
        else:
            parts.append("当前没有可核对的 16 港进口焦煤库存。")
    if topic == "drivers":
        parts.insert(0, _analysis("spread", books, legs)[1])
    explanations = {
        "imports": (
            "港口存量需要与流量交叉核对",
            "进口到港和疏港提货都改变库存。确认进口是否收紧，还需同期间通关或到港流量。",
        ),
        "restock": (
            "核对焦企补库是否形成库存",
            "库存反映到货减去耗用后的结果；归因还需同样本的采购、到货与耗用量。",
        ),
        "drivers": (
            "近远月价差、进口供应与下游补库",
            "检验补库还需对照焦企到货与耗用，检验进口收紧要核对通关和疏港量。现有库存调查单独不能分辨两种解释。",
        ),
    }
    headline, explanation = explanations[topic]
    return headline, "".join(parts + [explanation])


def _choices(books, legs, cutoff):
    choices = []
    for id_, (key, label, name, unit) in METRICS.items():
        if not books[key]:
            continue
        row = books[key][-1]
        period = date.fromisoformat(row.period)
        target = _next_day(period) if key == "spread" else period + timedelta(days=7)
        if target < cutoff.date():
            continue
        v = _value(row, key, legs)
        baseline = {
            "key": key,
            "period": row.period,
            "target": target.isoformat(),
            "value": v,
            "unit": unit,
            "legs": list(legs) if key == "spread" else [],
            "known": row.known,
            "source_url": row.url,
        }
        rule = f"以 {row.period} 的{name} {_number(v)} {unit}为基数，核对 {target.isoformat()} 同口径披露。"
        if key == "spread":
            rule = f"下一工作日核对固定 {legs[0]} 减 {legs[1]}。" + rule + " 遇休市或缺报暂不判定。"
        if id_ == "wait":
            rule = "等下一期同样本披露以重新核对；观察项不评价方向。"
        choices.append(dict(id=id_, label=label, rule=rule, baseline=baseline))
    return choices


async def analyze(question: str, cutoff: datetime):
    local = _local(cutoff)
    legs = _legs(local.date())
    topic = _topic(question, legs)
    result = dict(
        question=question,
        as_of=local.isoformat(),
        method="按问题匹配研究主题，依据已披露的数据计算",
        evidence=[],
        choices=[],
    )
    if topic is None:
        return {
            **result,
            "status": "unsupported",
            "headline": "可核对焦煤供需、月差与持仓",
            "explanation": f"此时点研究焦煤进口、焦企补库、{legs[0]} 和 {legs[1]} 的收盘价差与持仓变化。请围绕其中一项提问。",
        }
    rows = await sources.editions(local)
    books = _books(rows, legs)
    evidence = _evidence_for(topic, books, legs)
    headline, explanation = _analysis(topic, books, legs)
    if evidence:
        relevant = {
            "drivers": ("spread", "coke", "port"),
            "imports": ("port",),
            "restock": ("coke",),
            "spread": ("spread",),
            "positions": ("spread",),
        }[topic]
        stale = []
        for key in relevant:
            if not books[key]:
                continue
            last = date.fromisoformat(books[key][-1].period)
            next_expected = _next_day(last) if key == "spread" else last + timedelta(days=7)
            if next_expected < local.date():
                stale.append({"spread": "合约收盘", "port": "港口库存", "coke": "焦企调查"}[key])
        if stale:
            explanation += " " + "、".join(stale) + "为已收录的历史数据，后续期次尚待核对。"
    choices = _choices(books, legs, local) if evidence else []
    return {
        **result,
        "status": "available" if evidence else "unavailable",
        "headline": headline,
        "explanation": explanation,
        "evidence": evidence,
        "choices": choices,
    }


def _outcome(status, headline, explanation, evidence=None):
    return dict(status=status, headline=headline, explanation=explanation, evidence=evidence or [])


async def review(choice: dict, cutoff: datetime):
    local = _local(cutoff)
    cid = choice["id"]
    key, _, name, unit = METRICS[cid]
    base = choice["baseline"]
    legs = base["legs"]
    day = base["target"]
    if day > local.date().isoformat():
        return _outcome("pending", "等待下一期披露", f"将核对 {day} 的{name}。")
    rows = _series(
        await sources.editions(local, target=day, series="market" if key == "spread" else key), key, legs
    )
    target = next((r for r in rows if r.period == day), None)
    if target is None:
        return _outcome("pending", "指定期次尚无可核对的披露", f"缺少 {day} 的{name}。将继续等待该期数据。")
    started = choice.get("tracking_started_at")
    if started and datetime.fromisoformat(target.known).replace(tzinfo=sources.SHANGHAI) <= _local(
        datetime.fromisoformat(started)
    ):
        return _outcome("unverifiable", "封存前已披露", "这份数据先于判断封存，无法作为事后验证。")
    current = _value(target, key, legs)
    evidence = [_evidence(target, name, _describe(key, target, legs))]
    if cid == "wait":
        return _outcome(
            "observed",
            "下期焦企调查已披露",
            "新库存和利用率可供核查，单凭本次披露仍无法拆分到货与耗用。这条观察不计方向。",
            evidence,
        )
    delta = round(current - base["value"], 4)
    matches = (delta < 0) if cid == "imports" else (delta > 0)
    reading = f"{base['period']} 至 {day}，{name}从 {_number(base['value'])} 变为 {_number(current)} {unit}，"
    change = f"{'增加' if delta > 0 else '减少'} {_number(abs(delta))} {unit}。" if delta else "两期持平。"
    return _outcome(
        "supported" if matches else "contradicted",
        "原判断得到支持" if matches else "原判断未得到支持",
        reading + change,
        evidence,
    )
