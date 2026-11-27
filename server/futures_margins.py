"""Deterministic cross-commodity industrial margins and price ratios across the 46-product universe.

Every formula combines validated daily closes on the exact same trading date (`data_as_of` inner join).
If any leg is missing or carries a different observation date, the margin explicitly degrades to
``unavailable`` (`leg_unavailable` or `leg_date_mismatch`) rather than mixing dates or estimating prices.
All values are explicitly labelled as continuous-series board theoretical spreads/ratios, never physical
factory accounting profits.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from . import futures_signals

NOTICE = (
    "盘面理论价差/比价基于各品种同日连续合约收盘价按行业标准配比计算，仅反映产业链上下游相对强弱与估值分位；"
    "不含现货升贴水、加工辅料、人工电费、副产品折价及运费，不代表企业实际现货利润。"
)


@dataclass(frozen=True)
class MarginSpec:
    id: str
    name: str
    chain_id: str
    unit: str
    legs: tuple[str, ...]
    formula_label: str
    compute: Callable[[dict[str, float]], float | None]
    interpretation_low: str
    interpretation_high: str


def _blast_margin(p: dict[str, float]) -> float:
    # 1t rebar vs ~1.6t iron ore + ~0.5t coking coal proxy (board raw-material spread)
    return p["RB"] - 1.6 * p["I"] - 0.5 * p["JM"]


def _hc_rb(p: dict[str, float]) -> float:
    return p["HC"] - p["RB"]


def _rb_i(p: dict[str, float]) -> float | None:
    return p["RB"] / p["I"] if p["I"] > 0 else None


def _rb_jm(p: dict[str, float]) -> float | None:
    return p["RB"] / p["JM"] if p["JM"] > 0 else None


def _soy_crush(p: dict[str, float]) -> float:
    return 0.785 * p["M"] + 0.185 * p["Y"] - p["B"]


def _oil_meal(p: dict[str, float]) -> float | None:
    return p["Y"] / p["M"] if p["M"] > 0 else None


def _y_p(p: dict[str, float]) -> float:
    return p["Y"] - p["P"]


def _m_rm(p: dict[str, float]) -> float:
    return p["M"] - p["RM"]


def _hog_feed(p: dict[str, float]) -> float | None:
    denom = 0.65 * p["C"] + 0.25 * p["M"]
    return p["LH"] / denom if denom > 0 else None


def _mto(p: dict[str, float]) -> float:
    return p["PP"] - 3.0 * p["MA"]


def _al_ao(p: dict[str, float]) -> float:
    return p["AL"] - 1.93 * p["AO"]


def _ps_si(p: dict[str, float]) -> float:
    return p["PS"] - 1.15 * p["SI"]


def _fg_sa(p: dict[str, float]) -> float:
    return p["FG"] - 0.22 * p["SA"]


def _au_ag(p: dict[str, float]) -> float | None:
    # AU is CNY/g, AG is CNY/kg -> AU*1000 / AG gives dimensionless mass ratio
    return (p["AU"] * 1000.0) / p["AG"] if p["AG"] > 0 else None


SPECS: tuple[MarginSpec, ...] = (
    MarginSpec(
        id="rb_raw_margin",
        name="螺矿煤盘面原料差",
        chain_id="ferrous",
        unit="元/吨",
        legs=("RB", "I", "JM"),
        formula_label="RB − 1.6×I − 0.5×JM",
        compute=_blast_margin,
        interpretation_low="成材相对炉料偏弱（亏损挤压区，关注钢厂减产负反馈）",
        interpretation_high="盘面炼钢毛利偏高（盈利修复区，警惕铁水复产增加远期供给）",
    ),
    MarginSpec(
        id="hc_rb_spread",
        name="卷螺盘面价差",
        chain_id="ferrous",
        unit="元/吨",
        legs=("HC", "RB"),
        formula_label="HC − RB",
        compute=_hc_rb,
        interpretation_low="螺纹相对热卷偏强（建筑需求或减产弹性好于板材）",
        interpretation_high="热卷相对螺纹偏强（制造业与出口需求韧性显著强于地产建材）",
    ),
    MarginSpec(
        id="rb_i_ratio",
        name="螺矿比",
        chain_id="ferrous",
        unit="倍",
        legs=("RB", "I"),
        formula_label="RB / I",
        compute=_rb_i,
        interpretation_low="矿强材弱（铁矿石吞噬成材利润）",
        interpretation_high="材强矿弱（成材端利润留存能力改善）",
    ),
    MarginSpec(
        id="rb_jm_ratio",
        name="螺煤比",
        chain_id="ferrous",
        unit="倍",
        legs=("RB", "JM"),
        formula_label="RB / JM",
        compute=_rb_jm,
        interpretation_low="煤强钢弱（焦煤供给弹性偏紧挤压下游）",
        interpretation_high="钢强煤弱（煤矿供给宽松让渡冶炼利润）",
    ),
    MarginSpec(
        id="soy_crush_margin",
        name="进口大豆盘面榨利",
        chain_id="oilseed_feed",
        unit="元/吨",
        legs=("B", "M", "Y"),
        formula_label="0.785×M + 0.185×Y − B",
        compute=_soy_crush,
        interpretation_low="盘面压榨深度倒挂（买船与远期开机意愿受抑）",
        interpretation_high="盘面榨利丰厚（油厂套保锁利与积极买船压榨激励增强）",
    ),
    MarginSpec(
        id="oil_meal_ratio",
        name="豆油粕比",
        chain_id="oilseed_feed",
        unit="倍",
        legs=("Y", "M"),
        formula_label="Y / M",
        compute=_oil_meal,
        interpretation_low="粕强油弱（蛋白饲用或南美天气支撑强于油脂）",
        interpretation_high="油强粕弱（生柴/食用油脂驱动占优，油厂挺油抛粕）",
    ),
    MarginSpec(
        id="y_p_spread",
        name="豆棕盘面价差",
        chain_id="oilseed_feed",
        unit="元/吨",
        legs=("Y", "P"),
        formula_label="Y − P",
        compute=_y_p,
        interpretation_low="豆棕倒挂或极窄（棕榈油产地偏紧，豆油性价比凸显替代棕油）",
        interpretation_high="豆棕价差偏宽（棕榈油恢复性价比优势）",
    ),
    MarginSpec(
        id="m_rm_spread",
        name="豆菜粕盘面价差",
        chain_id="oilseed_feed",
        unit="元/吨",
        legs=("M", "RM"),
        formula_label="M − RM",
        compute=_m_rm,
        interpretation_low="豆菜粕差极窄（豆粕单位蛋白性价比高，挤压菜粕配方比例）",
        interpretation_high="豆菜粕差拉大（水产及禽料倾向增加菜粕替代）",
    ),
    MarginSpec(
        id="hog_feed_ratio",
        name="盘面猪料比指标",
        chain_id="oilseed_feed",
        unit="倍",
        legs=("LH", "C", "M"),
        formula_label="LH / (0.65×C + 0.25×M)",
        compute=_hog_feed,
        interpretation_low="猪贱粮贵（养殖端亏损去产能压力加大）",
        interpretation_high="猪强料弱（盘面养殖毛利修复，补栏与压栏空间改善）",
    ),
    MarginSpec(
        id="mto_spread",
        name="MTO 盘面制烯烃差",
        chain_id="energy_chem",
        unit="元/吨",
        legs=("PP", "MA"),
        formula_label="PP − 3×MA",
        compute=_mto,
        interpretation_low="MTO 装置盘面深亏（沿海甲醇制烯烃负荷易受倒逼检修）",
        interpretation_high="MTO 盘面加工差充裕（烯烃刚需对甲醇形成正向支撑）",
    ),
    MarginSpec(
        id="al_ao_margin",
        name="电解铝—氧化铝原料差",
        chain_id="nonferrous",
        unit="元/吨",
        legs=("AL", "AO"),
        formula_label="AL − 1.93×AO",
        compute=_al_ao,
        interpretation_low="氧化铝强势挤占电解铝利润（矿石与焙烧端紧缺主导）",
        interpretation_high="氧化铝让利，电解铝冶炼端毛利扩张",
    ),
    MarginSpec(
        id="ps_si_margin",
        name="多晶硅—工业硅盘面差",
        chain_id="new_energy",
        unit="元/吨",
        legs=("PS", "SI"),
        formula_label="PS − 1.15×SI",
        compute=_ps_si,
        interpretation_low="多晶硅环节价差压缩（逼近硅料现金成本减产线）",
        interpretation_high="硅料对工业硅价差走阔（光伏硅料挺价弹性大于上游工业硅）",
    ),
    MarginSpec(
        id="fg_sa_margin",
        name="浮法玻璃—纯碱原料差",
        chain_id="building_chem",
        unit="元/吨",
        legs=("FG", "SA"),
        formula_label="FG − 0.22×SA",
        compute=_fg_sa,
        interpretation_low="玻璃受纯碱成本挤压或竣工偏弱（冷修压力上升）",
        interpretation_high="纯碱让利或玻璃去库顺畅（玻璃产线利润改善）",
    ),
    MarginSpec(
        id="au_ag_ratio",
        name="沪金银比",
        chain_id="precious",
        unit="倍",
        legs=("AU", "AG"),
        formula_label="(AU×1000) / AG",
        compute=_au_ag,
        interpretation_low="金银比偏低（白银工业与投机弹性显著强于黄金）",
        interpretation_high="金银比偏高（避险与央行购金逻辑主导，白银受工业预期拖累）",
    ),
)

SPECS_BY_ID: dict[str, MarginSpec] = {spec.id: spec for spec in SPECS}


def specs_for_symbol(symbol: str) -> list[MarginSpec]:
    code = symbol.strip().upper()
    return [spec for spec in SPECS if code in spec.legs]


def _clean_bars(bars: list[dict] | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in bars or []:
        if not isinstance(row, dict):
            continue
        day = row.get("date")
        close = row.get("close")
        if (
            isinstance(day, str)
            and len(day) == 10
            and isinstance(close, (int, float))
            and not isinstance(close, bool)
            and math.isfinite(close)
            and close > 0
        ):
            out[day] = float(close)
    return out


def _zone_hint(pct: float | None, spec: MarginSpec) -> str:
    if pct is None:
        return "历史样本不足以划分分位区间"
    if pct <= 25.0:
        return spec.interpretation_low
    if pct >= 75.0:
        return spec.interpretation_high
    return "处于历史中性波动区间"


def compute_margin(
    spec: MarginSpec,
    histories: dict[str, list[dict] | None],
    *,
    today: date | None = None,
    fresh_days: int = 4,
) -> dict:
    """Compute one deterministic cross-commodity spread/ratio from aligned daily histories."""
    base = {
        "id": spec.id,
        "name": spec.name,
        "chain_id": spec.chain_id,
        "unit": spec.unit,
        "legs": list(spec.legs),
        "formula": spec.formula_label,
        "source": {
            "name": "新浪财经连续日线（同日对齐复算）",
            "url": f"https://finance.sina.com.cn/futures/quotes/{spec.legs[0]}0.shtml",
        },
        "notice": NOTICE,
    }
    leg_maps: dict[str, dict[str, float]] = {}
    missing_legs: list[str] = []
    latest_dates: dict[str, str] = {}
    for leg in spec.legs:
        cleaned = _clean_bars(histories.get(leg))
        if not cleaned:
            missing_legs.append(leg)
            continue
        leg_maps[leg] = cleaned
        latest_dates[leg] = max(cleaned)

    if missing_legs:
        return {
            **base,
            "status": "unavailable",
            "reason_code": "leg_unavailable",
            "reason": f"关联品种 {'、'.join(missing_legs)} 日线尚未核验，未跨品种估算。",
            "data_as_of": None,
            "values": None,
        }

    if len(set(latest_dates.values())) > 1:
        detail = "，".join(f"{k}:{v}" for k, v in latest_dates.items())
        return {
            **base,
            "status": "unavailable",
            "reason_code": "leg_date_mismatch",
            "reason": f"关联品种最新行情日期不一致（{detail}），未跨日拼凑价差。",
            "data_as_of": None,
            "values": None,
        }

    latest_day = next(iter(latest_dates.values()))
    common_dates = sorted(set.intersection(*(set(m.keys()) for m in leg_maps.values())))
    series: list[tuple[str, float]] = []
    for d in common_dates[-1250:]:
        prices = {leg: leg_maps[leg][d] for leg in spec.legs}
        val = spec.compute(prices)
        if val is not None and math.isfinite(val):
            series.append((d, round(val, 2)))

    if not series or series[-1][0] != latest_day:
        return {
            **base,
            "status": "unavailable",
            "reason_code": "insufficient_common_observations",
            "reason": "共同交易日有效价格不足，无法计算。",
            "data_as_of": None,
            "values": None,
        }

    obs_day = date.fromisoformat(latest_day)
    stale = today is not None and (today - obs_day).days > fresh_days
    nums = [v for _, v in series]
    current = nums[-1]
    previous = nums[-2] if len(nums) >= 2 else None
    previous_date = series[-2][0] if len(series) >= 2 else None
    change_1d = round(current - previous, 2) if previous is not None else None
    change_5d = round(current - nums[-6], 2) if len(nums) >= 6 else None
    change_20d = round(current - nums[-21], 2) if len(nums) >= 21 else None
    pct_5y = futures_signals._empirical_percentile_clean(nums, current)
    pct_1y = futures_signals._empirical_percentile_clean(nums[-250:], current)
    leg_quotes = {leg: leg_maps[leg][latest_day] for leg in spec.legs}

    return {
        **base,
        "status": "stale" if stale else "collected",
        "reason_code": "older_observation" if stale else None,
        "reason": f"行情止于 {latest_day}。" if stale else None,
        "data_as_of": latest_day,
        "stale": stale,
        "values": {
            "current": current,
            "previous": previous,
            "previous_date": previous_date,
            "change_1d": change_1d,
            "change_5d": change_5d,
            "change_20d": change_20d,
            "pct_5y": pct_5y,
            "pct_1y": pct_1y,
            "sample_days": len(nums),
            "since_date": series[0][0],
            "span_label": "近5年" if len(nums) >= 1000 else f"近{len(nums)}共同交易日",
            "leg_closes": leg_quotes,
            "hint": _zone_hint(pct_5y if pct_5y is not None else pct_1y, spec),
        },
    }


_ALL_MARGIN_LEGS: tuple[str, ...] = tuple(sorted({leg for spec in SPECS for leg in spec.legs}))
_ALL_MARGINS_CACHE: dict[tuple, list[dict]] = {}
_ALL_MARGINS_CACHE_MAX = 16


def _leg_cache_sig(bars: list[dict] | None) -> tuple | None:
    if not isinstance(bars, list) or not bars:
        return None
    first = bars[0]
    last = bars[-1]
    prev = bars[-2] if len(bars) >= 2 else None
    if not (isinstance(first, dict) and isinstance(last, dict)):
        return None
    return (
        id(bars),
        len(bars),
        first.get("date"),
        first.get("close"),
        prev.get("close") if isinstance(prev, dict) else None,
        last.get("date"),
        last.get("close"),
    )


def _clone_margins(items: list[dict]) -> list[dict]:
    return [
        {**m, "values": {**m["values"], "leg_closes": dict(m["values"]["leg_closes"])}}
        if m.get("values")
        else dict(m)
        for m in items
    ]


def margins_for_symbol(
    symbol: str,
    histories: dict[str, list[dict] | None],
    *,
    today: date | None = None,
    fresh_days: int = 4,
) -> list[dict]:
    return [
        compute_margin(spec, histories, today=today, fresh_days=fresh_days)
        for spec in specs_for_symbol(symbol)
    ]


def all_margins(
    histories: dict[str, list[dict] | None],
    *,
    today: date | None = None,
    fresh_days: int = 4,
) -> list[dict]:
    cache_key = (
        today,
        fresh_days,
        tuple((leg, _leg_cache_sig(histories.get(leg))) for leg in _ALL_MARGIN_LEGS),
    )
    cached = _ALL_MARGINS_CACHE.get(cache_key)
    if cached is not None:
        return _clone_margins(cached)
    computed = [compute_margin(spec, histories, today=today, fresh_days=fresh_days) for spec in SPECS]
    if len(_ALL_MARGINS_CACHE) >= _ALL_MARGINS_CACHE_MAX:
        _ALL_MARGINS_CACHE.pop(next(iter(_ALL_MARGINS_CACHE)))
    _ALL_MARGINS_CACHE[cache_key] = computed
    return _clone_margins(computed)
