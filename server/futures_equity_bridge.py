"""Cross-market supply-chain canvas and lead-lag divergence bridge (Futures ↔ Spot ↔ Margin ↔ A-Shares).

Connects all 46 futures varieties to:
1. Their 4-stage industrial transmission flow (Upstream Resource -> Midstream Margin Valve ->
   Current Futures/Spot/Inventory -> Downstream Application & Mapped A-Share Leaders);
2. Prism's curated A-share industry graph (`/api/industry/*` categories & tickers);
3. Read-only cached equity quotes in ``MarketCache`` to surface verifiable futures-vs-equity
   lead-lag divergence without making blocking external stock-API calls during `/card` reads.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import futures, futures_margins, futures_ontology


@dataclass(frozen=True)
class EquityAnchor:
    ticker: str
    name: str
    role: str
    stage: str  # "upstream" | "midstream" | "downstream"


@dataclass(frozen=True)
class SupplyChainDef:
    id: str
    name: str
    industry_category: str
    upstream_futures: tuple[str, ...]
    midstream_margins: tuple[str, ...]
    downstream_futures: tuple[str, ...]
    equities: tuple[EquityAnchor, ...]
    transmission_note: str


SUPPLY_CHAINS: dict[str, SupplyChainDef] = {
    "ferrous": SupplyChainDef(
        id="ferrous",
        name="黑色煤焦钢产业链",
        industry_category="黑色冶炼",
        upstream_futures=("JM", "I"),
        midstream_margins=("rb_raw_margin", "hc_rb_spread", "rb_i_ratio", "rb_jm_ratio"),
        downstream_futures=("RB", "HC"),
        equities=(
            EquityAnchor("601088", "中国神华", "煤炭资源龙头", "upstream"),
            EquityAnchor("600985", "淮北矿业", "炼焦煤主力矿企", "upstream"),
            EquityAnchor("600019", "宝钢股份", "板材高炉龙头", "downstream"),
            EquityAnchor("000932", "华菱钢铁", "普特钢代表钢企", "downstream"),
        ),
        transmission_note="焦煤/铁矿决定高炉铁水成本底；盘面炼钢毛利走阔易刺激钢厂复产，卷螺差反映制造业出口相对地产基建的强弱。",
    ),
    "nonferrous": SupplyChainDef(
        id="nonferrous",
        name="有色金属冶炼与加工链",
        industry_category="有色金属",
        upstream_futures=("AO",),
        midstream_margins=("al_ao_margin",),
        downstream_futures=("CU", "AL", "NI", "SN", "SS"),
        equities=(
            EquityAnchor("601899", "紫金矿业", "全球铜金矿业龙头", "upstream"),
            EquityAnchor("600362", "江西铜业", "铜冶炼与加工龙头", "midstream"),
            EquityAnchor("601600", "中国铝业", "氧化铝—电解铝一体化", "midstream"),
            EquityAnchor("000807", "云铝股份", "绿电水电铝弹性标的", "downstream"),
        ),
        transmission_note="上游矿端与氧化铝供给约束决定成本支撑，电解铝4500万吨产能天花板下，氧化铝让利直接增厚冶炼端盈利。",
    ),
    "new_energy": SupplyChainDef(
        id="new_energy",
        name="硅光锂电新能源材料链",
        industry_category="锂电池",
        upstream_futures=("SI",),
        midstream_margins=("ps_si_margin",),
        downstream_futures=("PS", "LC"),
        equities=(
            EquityAnchor("002460", "赣锋锂业", "锂矿与锂盐龙头", "upstream"),
            EquityAnchor("600438", "通威股份", "工业硅—多晶硅料龙头", "midstream"),
            EquityAnchor("601012", "隆基绿能", "单晶硅片与光伏组件", "downstream"),
            EquityAnchor("300750", "宁德时代", "动力与储能电池龙头", "downstream"),
        ),
        transmission_note="工业硅受西南丰枯水电价驱动传导至多晶硅；碳酸锂期现库存消化与A股锂矿/电池排产预期常出现先行或滞后劈叉。",
    ),
    "precious": SupplyChainDef(
        id="precious",
        name="贵金属宏观与矿企链",
        industry_category="贵金属",
        upstream_futures=("AU",),
        midstream_margins=("au_ag_ratio",),
        downstream_futures=("AG",),
        equities=(
            EquityAnchor("600547", "山东黄金", "高弹性黄金矿产龙头", "upstream"),
            EquityAnchor("600489", "中金黄金", "央企金铜矿山主力", "upstream"),
            EquityAnchor("000975", "山金国际", "低成本金银矿企", "downstream"),
            EquityAnchor("688503", "聚和材料", "光伏正面银浆下游", "downstream"),
        ),
        transmission_note="沪金锚定美债实际利率与央行购金，金银比衡量避险情绪与光伏电子工业需求弹性；金价突破往往先行改善金矿股毛利率。",
    ),
    "energy_chem": SupplyChainDef(
        id="energy_chem",
        name="原油芳烃与煤化工烯烃链",
        industry_category="能源化工",
        upstream_futures=("SC", "PG", "MA"),
        midstream_margins=("mto_spread",),
        downstream_futures=("TA", "PP", "BU", "UR", "RU", "NR", "SP"),
        equities=(
            EquityAnchor("601857", "中国石油", "上游油气开采龙头", "upstream"),
            EquityAnchor("600989", "宝丰能源", "煤制烯烃成本标杆", "midstream"),
            EquityAnchor("002493", "荣盛石化", "炼化—PX—PTA芳烃大厂", "midstream"),
            EquityAnchor("601058", "赛轮轮胎", "天然胶/标胶下游轮胎龙头", "downstream"),
        ),
        transmission_note="原油与煤炭构成国内化工双成本锚；沿海MTO制烯烃价差（PP−3×MA）决定了甲醇需求上限与烯烃边际供给。",
    ),
    "building_chem": SupplyChainDef(
        id="building_chem",
        name="氯碱纯碱与地产竣工建材链",
        industry_category="建材氯碱",
        upstream_futures=("SA", "SH"),
        midstream_margins=("fg_sa_margin",),
        downstream_futures=("FG", "V"),
        equities=(
            EquityAnchor("000683", "远兴能源", "天然碱低成本龙头", "upstream"),
            EquityAnchor("601216", "君正集团", "氯碱PVC电石一体化", "midstream"),
            EquityAnchor("601636", "旗滨集团", "浮法与光伏玻璃龙头", "downstream"),
            EquityAnchor("600660", "福耀玻璃", "汽车玻璃深加工龙头", "downstream"),
        ),
        transmission_note="纯碱与烧碱共享工业盐与高耗能属性：纯碱直供浮法/光伏玻璃，烧碱与PVC形成氯碱平衡（以碱补氯）。",
    ),
    "oilseed_feed": SupplyChainDef(
        id="oilseed_feed",
        name="油籽压榨、软商品与生猪养殖链",
        industry_category="农牧饲渔",
        upstream_futures=("A", "B", "C", "PK", "CF", "SR"),
        midstream_margins=(
            "soy_crush_margin",
            "oil_meal_ratio",
            "y_p_spread",
            "m_rm_spread",
            "hog_feed_ratio",
        ),
        downstream_futures=("M", "RM", "Y", "P", "OI", "CS", "LH", "JD", "AP", "CJ"),
        equities=(
            EquityAnchor("000998", "隆平高科", "玉米水稻种业龙头", "upstream"),
            EquityAnchor("300999", "金龙鱼", "油籽压榨与包装油龙头", "midstream"),
            EquityAnchor("002311", "海大集团", "水产禽畜饲料龙头", "downstream"),
            EquityAnchor("002714", "牧原股份", "生猪自繁自养成本标杆", "downstream"),
        ),
        transmission_note="进口大豆（豆二）经油厂压榨产出豆粕与豆油，与玉米共同构成生猪/蛋鸡饲料成本（猪料比），直接映射养殖企业远期毛利。",
    ),
    "shipping": SupplyChainDef(
        id="shipping",
        name="集运欧线与外贸航运链",
        industry_category="航运港口",
        upstream_futures=(),
        midstream_margins=(),
        downstream_futures=("EC",),
        equities=(
            EquityAnchor("601919", "中远海控", "集装箱班轮欧线龙头", "midstream"),
            EquityAnchor("601872", "招商轮船", "油散集综合航运", "upstream"),
            EquityAnchor("600018", "上港集团", "上海母港集装箱枢纽", "downstream"),
            EquityAnchor("601598", "中国外运", "跨境货运代理下游", "downstream"),
        ),
        transmission_note="EC期货以三期上海航交所SCFIS欧线结算指数现金交割；年末船司GRI挺价直接影响航运龙头次年长协签约基准价。",
    ),
}

# Reverse map from Prism industry_nodes.category -> linked futures varieties for bidirectional navigation
INDUSTRY_TO_FUTURES: dict[str, list[dict[str, str]]] = {
    "锂电池": [
        {"symbol": "LC", "name": "碳酸锂", "role": "正极锂盐定价锚"},
        {"symbol": "NI", "name": "沪镍", "role": "三元前驱体原料"},
        {"symbol": "CU", "name": "沪铜", "role": "锂电铜箔原料"},
        {"symbol": "AL", "name": "沪铝", "role": "电池壳体与铝箔"},
    ],
    "光伏": [
        {"symbol": "PS", "name": "多晶硅", "role": "光伏硅料核心锚"},
        {"symbol": "SI", "name": "工业硅", "role": "上游晶硅原料"},
        {"symbol": "AG", "name": "沪银", "role": "光伏正面银浆原料"},
        {"symbol": "FG", "name": "玻璃", "role": "玻璃产业链参考"},
    ],
    "电动车": [
        {"symbol": "LC", "name": "碳酸锂", "role": "电芯成本锚"},
        {"symbol": "HC", "name": "热卷", "role": "车身板材原料"},
        {"symbol": "AL", "name": "沪铝", "role": "轻量化用铝"},
        {"symbol": "RU", "name": "天然橡胶", "role": "轮胎原料"},
    ],
    "半导体": [
        {"symbol": "SN", "name": "沪锡", "role": "电子封装焊料锚"},
        {"symbol": "SI", "name": "工业硅", "role": "高纯硅基原料"},
        {"symbol": "CU", "name": "沪铜", "role": "引线框架与覆铜板"},
    ],
    "黑色冶炼": [
        {"symbol": "JM", "name": "焦煤", "role": "上游炼焦原料"},
        {"symbol": "I", "name": "铁矿石", "role": "上游高炉炉料"},
        {"symbol": "RB", "name": "螺纹钢", "role": "建筑钢材成材"},
        {"symbol": "HC", "name": "热卷", "role": "制造业板材成材"},
    ],
    "有色金属": [
        {"symbol": "CU", "name": "沪铜", "role": "铜产业链核心"},
        {"symbol": "AL", "name": "沪铝", "role": "电解铝定价"},
        {"symbol": "AO", "name": "氧化铝", "role": "铝冶炼原料"},
        {"symbol": "NI", "name": "沪镍", "role": "镍及不锈钢原料"},
        {"symbol": "SN", "name": "沪锡", "role": "锡焊料定价"},
    ],
    "贵金属": [
        {"symbol": "AU", "name": "沪金", "role": "黄金定价基准"},
        {"symbol": "AG", "name": "沪银", "role": "白银及金银比"},
    ],
    "能源化工": [
        {"symbol": "SC", "name": "原油", "role": "油化总成本锚"},
        {"symbol": "MA", "name": "甲醇", "role": "煤化工与MTO原料"},
        {"symbol": "TA", "name": "PTA", "role": "聚酯化纤核心"},
        {"symbol": "PP", "name": "聚丙烯", "role": "通用塑料成材"},
        {"symbol": "RU", "name": "天然橡胶", "role": "轮胎橡胶原料"},
    ],
    "建材氯碱": [
        {"symbol": "SA", "name": "纯碱", "role": "重碱原料"},
        {"symbol": "FG", "name": "玻璃", "role": "竣工浮法玻璃"},
        {"symbol": "SH", "name": "烧碱", "role": "氯碱液碱"},
        {"symbol": "V", "name": "PVC", "role": "管材型材树脂"},
    ],
    "农牧饲渔": [
        {"symbol": "B", "name": "豆二", "role": "进口压榨大豆"},
        {"symbol": "M", "name": "豆粕", "role": "蛋白饲料核心"},
        {"symbol": "C", "name": "玉米", "role": "能量饲料核心"},
        {"symbol": "LH", "name": "生猪", "role": "养殖终端定价"},
        {"symbol": "Y", "name": "豆油", "role": "植物油脂基准"},
    ],
    "航运港口": [
        {"symbol": "EC", "name": "集运欧线", "role": "亚欧集装箱运价指数"},
        {"symbol": "SC", "name": "原油", "role": "船燃能源成本参考"},
    ],
    "AI算力与服务器": [
        {"symbol": "CU", "name": "沪铜", "role": "高速铜缆/覆铜板与散热核心金属"},
        {"symbol": "SN", "name": "沪锡", "role": "服务器主板与芯片封测焊料锚"},
        {"symbol": "AL", "name": "沪铝", "role": "服务器机箱与液冷散热片原料"},
        {"symbol": "AG", "name": "沪银", "role": "高频连接器与电子浆料金属"},
    ],
    "人形机器人与精密制造": [
        {"symbol": "CU", "name": "沪铜", "role": "电机绕组与线束铜材锚"},
        {"symbol": "AL", "name": "沪铝", "role": "轻量化机身与压铸结构件"},
        {"symbol": "HC", "name": "热卷", "role": "精密结构钢与轴承毛坯"},
    ],
    "电网设备与算力电力": [
        {"symbol": "CU", "name": "沪铜", "role": "变压器绕组与高压电缆核心成本"},
        {"symbol": "AL", "name": "沪铝", "role": "架空导线与开关壳体用铝"},
        {"symbol": "SI", "name": "工业硅", "role": "硅钢片与功率器件上游材料"},
    ],
}


def _cached_equity_quotes(tickers: tuple[EquityAnchor, ...]) -> dict[str, dict]:
    """Read-only non-blocking lookup from MarketCache so `/card` never stalls on external equity HTTP."""
    out: dict[str, dict] = {}
    try:
        from .market.factory import get_chain

        chain = get_chain()
        for eq in tickers:
            for provider in chain.providers:
                key = chain._cache_key("quote", provider.name, eq.ticker)
                val, _ = chain.cache.get_stale(key)
                if isinstance(val, dict) and isinstance(val.get("price"), (int, float)):
                    out[eq.ticker] = {
                        "price": val.get("price"),
                        "change_pct": val.get("change_pct"),
                        "source": val.get("source") or provider.name,
                    }
                    break
    except Exception:
        pass
    return out


def _sibling_pill(sym: str, current_symbol: str, histories: dict[str, list[dict] | None]) -> dict:
    name = futures.PRODUCTS.get(sym, (sym,))[0]
    bars = histories.get(sym) or []
    close = bars[-1]["close"] if bars else None
    day = bars[-1]["date"] if bars else None
    chg_1d = (
        round((bars[-1]["close"] / bars[-2]["close"] - 1.0) * 100.0, 2)
        if len(bars) >= 2 and bars[-2].get("close")
        else None
    )
    chg_5d = (
        round((bars[-1]["close"] / bars[-6]["close"] - 1.0) * 100.0, 2)
        if len(bars) >= 6 and bars[-6].get("close")
        else None
    )
    return {
        "symbol": sym,
        "name": name,
        "is_current": sym == current_symbol,
        "close": close,
        "change_1d_pct": chg_1d,
        "change_5d_pct": chg_5d,
        "data_as_of": day,
    }


def build_chain_canvas(
    symbol: str,
    card: dict,
    histories: dict[str, list[dict] | None],
    margins: list[dict],
) -> dict:
    """Build the 4-stage interactive Futures-Spot-Margin-Equity Supply-Chain Canvas for ``symbol``."""
    code = symbol.strip().upper()
    meta = futures_ontology.get(code)
    chain_id = meta.chain_id if meta and meta.chain_id in SUPPLY_CHAINS else "ferrous"
    chain = SUPPLY_CHAINS[chain_id]
    eq_quotes = _cached_equity_quotes(chain.equities)

    up_futs = [_sibling_pill(s, code, histories) for s in chain.upstream_futures[:4]]
    dn_futs = [_sibling_pill(s, code, histories) for s in chain.downstream_futures[:5]]
    up_eqs = [
        {
            "ticker": e.ticker,
            "name": e.name,
            "role": e.role,
            **(eq_quotes.get(e.ticker) or {}),
        }
        for e in chain.equities
        if e.stage in {"upstream", "midstream"}
    ][:3]
    dn_eqs = [
        {
            "ticker": e.ticker,
            "name": e.name,
            "role": e.role,
            **(eq_quotes.get(e.ticker) or {}),
        }
        for e in chain.equities
        if e.stage in {"downstream", "midstream"}
    ][:3]

    # Stage 1: Upstream Resource & Raw Material
    if up_futs:
        observed_up = [f for f in up_futs if f["close"] is not None]
        up_metric = (
            " · ".join(
                f"{f['name']} {f['close']}"
                + (f" ({f['change_1d_pct']:+.2f}%)" if f["change_1d_pct"] is not None else "")
                for f in observed_up[:2]
            )
            if observed_up
            else "点击关联上游品种核验实时日线"
        )
        up_title = " / ".join(f["name"] for f in up_futs[:3])
    else:
        up_title = "班轮运力排班与船燃成本"
        up_metric = "关注停航缩舱与绕行有效运力"

    # Stage 2: Midstream Margin Valve
    collected_margins = [m for m in margins if m.get("status") == "collected" and m.get("values")]
    if not collected_margins and chain.midstream_margins:
        # Try any collected margin in the same chain
        for mid_id in chain.midstream_margins:
            spec = futures_margins.SPECS_BY_ID.get(mid_id)
            if spec:
                cand = futures_margins.compute_margin(spec, histories)
                if cand.get("status") == "collected":
                    collected_margins.append(cand)
                    break

    if collected_margins:
        m0 = collected_margins[0]
        mv = m0["values"]
        pct = mv.get("pct_5y") if mv.get("pct_5y") is not None else mv.get("pct_1y")
        pct_str = f" · {mv['span_label']} {pct}% 分位" if pct is not None else ""
        mid_title = m0["name"]
        mid_metric = f"{mv['current']} {m0['unit']}{pct_str}"
        mid_badge = "利润偏高" if (pct or 50) >= 70 else "成本挤压" if (pct or 50) <= 30 else "中枢均衡"
        mid_detail = (
            f"公式 {m0['formula']}（同日 {m0['data_as_of']} 对齐复算）：{mv.get('hint', '')}。{m0['notice']}"
        )
    elif code == "EC":
        mid_title = "即期订舱报价 ↔ 交割结算锚"
        mid_metric = "三期 SCFIS 算术均价现金交割"
        mid_badge = "指数交割"
        mid_detail = (
            "集运欧线不设实物仓单；盘面价格反映市场对交割月最后三期上海航交所 SCFIS 欧线结算运价指数的预期。"
        )
    else:
        spec_names = [
            futures_margins.SPECS_BY_ID[mid].name
            for mid in chain.midstream_margins
            if mid in futures_margins.SPECS_BY_ID
        ]
        mid_title = spec_names[0] if spec_names else "中游加工传导阀门"
        mid_metric = "待同链关联品种同日行情齐备后自动复算"
        mid_badge = "待核验"
        mid_detail = "跨品种盘面价差要求各腿均为同一交易日已核验收盘价，缺腿或跨日时不作估算。"

    # Stage 3: Current Product Spot-Futures & Inventory Pool
    b_obs = card.get("basis") or {}
    i_obs = card.get("inventory") or {}
    cur_parts = []
    if b_obs.get("status") == "collected" and b_obs.get("values"):
        bv = b_obs["values"]
        cur_parts.append(f"基差 {bv['basis']:+.2f} {bv.get('quote_unit', '')}")
    if i_obs.get("status") == "collected" and i_obs.get("values"):
        iv = i_obs["values"]
        cur_parts.append(f"仓单 {iv['quantity']} {iv.get('unit', '')}（{iv['change']:+d}）")
    if not cur_parts and (card.get("benchmark") or {}).get("status") == "collected":
        bm = card["benchmark"]["values"]
        if bm.get("rows"):
            r0 = bm["rows"][0]
            cur_parts.append(f"{r0['label']} {r0['value']} {r0.get('unit', '')}")
    cur_metric = " · ".join(cur_parts) if cur_parts else "部分期现或仓单待披露"
    cur_badge = meta.role if meta else "核心标的"
    cur_detail = f"当前查看品种：{card.get('name', code)}（{code}）。" + (
        f"季节性规律：{meta.seasonal_anchor}。" if meta else ""
    )

    # Stage 4: Downstream Terminal & Mapped A-Share Leaders + Lead-Lag Check
    lead_lag = {
        "status": "awaiting_equity_cache",
        "label": f"产业图谱联动：{chain.industry_category}",
        "detail": "点击下方映射 A 股或「打开产业关系图谱」，可对照上市公司股价与大宗商品期现方向。",
    }
    own_bars = histories.get(code) or ((card.get("market") or {}).get("values") or {}).get("bars") or []
    fut_chg = (
        round((own_bars[-1]["close"] / own_bars[-2]["close"] - 1.0) * 100.0, 2)
        if len(own_bars) >= 2 and own_bars[-2].get("close")
        else None
    )
    quoted_eqs = [
        v["change_pct"] for v in eq_quotes.values() if isinstance(v.get("change_pct"), (int, float))
    ]
    if fut_chg is not None and quoted_eqs:
        eq_avg = round(sum(quoted_eqs) / len(quoted_eqs), 2)
        diff = round(fut_chg - eq_avg, 2)
        if diff >= 1.5:
            lead_lag = {
                "status": "futures_leading",
                "label": f"期强股弱（期 {fut_chg:+.2f}% vs 链内A股均值 {eq_avg:+.2f}%）",
                "detail": "商品期货盘面涨幅领先已缓存的链内A股均值，留意现货提价能否向权益盈利预期传导。",
            }
        elif diff <= -1.5:
            lead_lag = {
                "status": "equity_leading",
                "label": f"股强期弱（链内A股均值 {eq_avg:+.2f}% vs 期 {fut_chg:+.2f}%）",
                "detail": "A股板块表现强于当前期货盘面，权益端可能提前交易远期排产/政策预期，需核查现货库存是否配合。",
            }
        else:
            lead_lag = {
                "status": "aligned",
                "label": f"期股同频（期 {fut_chg:+.2f}% · 链内A股 {eq_avg:+.2f}%）",
                "detail": "期货与已缓存的链内代表A股日内方向大体一致。",
            }

    dn_title = " / ".join(e["name"] for e in dn_eqs[:2]) or chain.industry_category
    dn_metric = (
        " · ".join(
            f"{e['name']}"
            + (
                f" {e['change_pct']:+.2f}%"
                if isinstance(e.get("change_pct"), (int, float))
                else f" ({e['ticker']})"
            )
            for e in dn_eqs[:2]
        )
        or f"关联 {chain.industry_category} 图谱"
    )

    stages = [
        {
            "step": 1,
            "stage": "上游资源与原料锚",
            "title": up_title,
            "metric": up_metric,
            "badge": "成本输入",
            "detail": f"【上游传导】{chain.transmission_note}",
            "futures": up_futs,
            "equities": up_eqs,
        },
        {
            "step": 2,
            "stage": "中游盘面利润阀门",
            "title": mid_title,
            "metric": mid_metric,
            "badge": mid_badge,
            "detail": f"【中游利润锚】{mid_detail}",
            "futures": [],
            "equities": [],
        },
        {
            "step": 3,
            "stage": "本品种期现与库存池",
            "title": f"{card.get('name', code)} · {code}",
            "metric": cur_metric,
            "badge": cur_badge,
            "detail": cur_detail,
            "futures": [_sibling_pill(code, code, histories)],
            "equities": [],
        },
        {
            "step": 4,
            "stage": "下游终端与映射A股",
            "title": dn_title,
            "metric": dn_metric,
            "badge": chain.industry_category,
            "detail": f"【期股交叉背景】映射 Prism 产业图谱「{chain.industry_category}」链（背景关系供交叉核查，不构成商业供货认证）。",
            "futures": [f for f in dn_futs if f["symbol"] != code][:3],
            "equities": dn_eqs,
        },
    ]

    return {
        "chain_id": chain.id,
        "chain_name": chain.name,
        "industry_category": chain.industry_category,
        "transmission_note": chain.transmission_note,
        "lead_lag": lead_lag,
        "stages": stages,
    }
