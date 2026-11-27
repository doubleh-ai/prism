"""Declarative 6-archetype pricing ontology and deterministic causal narrator for all 46 futures products.

Maps every workspace symbol in ``UNIVERSE`` to its pricing archetype, supply-chain role, primary
observable drivers, seasonal calendar anchor, preset verification questions, and deterministic
plain-language causal synthesis ("白话结论 + 分期限多空拔河 + 可证伪如果-那么路标").

Strict truthfulness rule: narrative synthesis inspects only ``collected`` observations on the card.
Missing dimensions are named explicitly as unverified gaps and never fabricated.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProductOntology:
    symbol: str
    archetype: str
    archetype_label: str
    chain_id: str
    role: str
    drivers: tuple[str, ...]
    seasonal_anchor: str
    presets: tuple[tuple[str, str], ...]


ARCHETYPE_LABELS = {
    "ferrous_smelting": "黑色冶炼链",
    "nonferrous_new_energy": "有色与新能源材料",
    "precious_macro": "贵金属宏观锚",
    "energy_chem_material": "能源化工与建材",
    "agri_crush_livestock": "农牧油脂与软商品",
    "cash_settled_index": "航运现金交割指数",
}


def _default_presets(name: str) -> tuple[tuple[str, str], ...]:
    return (
        ("期现基差", f"{name}当前现货升贴水与盘面方向一致吗？"),
        ("库存仓单", f"{name}最新仓单变化说明什么交割压力？"),
        ("趋势位置", f"{name}在近5年历史区间处于什么位置？"),
    )


ONTOLOGY: dict[str, ProductOntology] = {
    # 1. Ferrous smelting chain (5)
    "JM": ProductOntology(
        "JM",
        "ferrous_smelting",
        "黑色冶炼链",
        "ferrous",
        "上游资源",
        ("523家矿山精煤产量", "蒙煤口岸通关与港口库存", "230家独立焦企开工补库"),
        "关注四季度冬储补库与煤矿岁末安监节奏",
        (
            ("供需原因", "近月相对走强，是进口收紧还是焦企补库？"),
            ("近远月价差", "两份合约的价差怎么走？"),
            ("持仓变化", "持仓变化能说明资金在做多吗？"),
        ),
    ),
    "I": ProductOntology(
        "I",
        "ferrous_smelting",
        "黑色冶炼链",
        "ferrous",
        "上游资源",
        ("澳巴矿山周度发运与到港", "45港铁矿石库存水位", "日均铁水产量与钢厂盈利率"),
        "一季度南半球飓风雨季扰动发运，年底钢厂存在刚需补库窗",
        (
            ("矿螺强弱", "铁矿石近期走势相对螺纹钢偏强还是偏弱？"),
            ("基差保护", "铁矿石当前现货升贴水处于什么水平？"),
            ("仓单与趋势", "铁矿石仓单与日线均线方向一致吗？"),
        ),
    ),
    "RB": ProductOntology(
        "RB",
        "ferrous_smelting",
        "黑色冶炼链",
        "ferrous",
        "中下游成材",
        ("钢厂周度螺纹产量", "35城社库与钢厂厂库分化", "工地建材日均成交与表需"),
        "金三银四与金九银十为施工旺季，12月至春节前转入冬储累库博弈",
        (
            ("去库原因", "这周螺纹钢去库，是需求回暖还是钢厂减产？"),
            ("厂库与社库", "社库和厂库的变化一致吗？"),
            ("终端采购", "终端采购能印证需求吗？"),
        ),
    ),
    "HC": ProductOntology(
        "HC",
        "ferrous_smelting",
        "黑色冶炼链",
        "ferrous",
        "中下游成材",
        ("热卷周度产量与铁水流向", "板材社会库存去化斜率", "汽车家电制造与钢材直接出口"),
        "受制造业排产与海外关税窗口驱动，季节性弱于建筑螺纹",
        (
            ("卷螺强弱", "当前热卷相对螺纹钢的卷螺差处于什么位置？"),
            ("期现对照", "热卷现货报价跟上期货盘面变化了吗？"),
            ("仓单压力", "上期所热卷仓库仓单近期增减如何？"),
        ),
    ),
    "SS": ProductOntology(
        "SS",
        "ferrous_smelting",
        "黑色冶炼链",
        "nonferrous",
        "中下游成材",
        ("高镍铁与高碳铬铁成本", "300系不锈钢排产", "无锡佛山社会库存与仓单"),
        "仓单体量对近月交割压制明显，留意钢厂联合减产窗口",
        _default_presets("不锈钢"),
    ),
    # 2. Nonferrous & New Energy Materials (8)
    "CU": ProductOntology(
        "CU",
        "nonferrous_new_energy",
        "有色与新能源材料",
        "nonferrous",
        "中游冶炼加工",
        ("铜精矿TC加工费", "全球显性库存与上期所仓单", "电网投资与新能源用铜需求"),
        "一季度传统累库后，二季度迎电网与空调排产去库旺季",
        (
            ("期现升贴水", "沪铜当前现货升贴水与上期所仓单如何配合？"),
            ("趋势分位", "沪铜收盘价在近5年处于什么历史分位？"),
            ("期限结构", "沪铜近远月结算价差呈现升水还是贴水？"),
        ),
    ),
    "AL": ProductOntology(
        "AL",
        "nonferrous_new_energy",
        "有色与新能源材料",
        "nonferrous",
        "中游冶炼加工",
        ("国内4500万吨产能天花板运行率", "氧化铝与预焙阳极电力成本", "铝锭铝棒出库与社会库存"),
        "西南丰枯水期影响水电铝成本，光伏与新能源汽车轻量化支撑需求",
        (
            ("铝氧利润", "电解铝相对氧化铝的盘面原料差在什么分位？"),
            ("库存与基差", "沪铝仓单与现货基差是否同步走强？"),
            ("趋势结构", "沪铝日线均线排列与跨期结构一致吗？"),
        ),
    ),
    "AO": ProductOntology(
        "AO",
        "nonferrous_new_energy",
        "有色与新能源材料",
        "nonferrous",
        "上游资源",
        ("几内亚铝土矿发运与国产矿复产", "氧化铝焙烧运行产能", "下游电解铝刚需采购"),
        "几内亚雨季（6–9月）易扰动矿端发运，新投产能释放决定远月估值",
        (
            ("产业链位置", "氧化铝与电解铝盘面价差目前偏向哪一端？"),
            ("仓单验证", "上期所氧化铝仓库仓单最新变化如何？"),
            ("趋势强度", "氧化铝日线趋势与历史波动率处于什么状态？"),
        ),
    ),
    "NI": ProductOntology(
        "NI",
        "nonferrous_new_energy",
        "有色与新能源材料",
        "nonferrous",
        "中游冶炼加工",
        ("印尼镍矿RKAB配额审批", "电积镍交割品增量与内外库存", "三元前驱体与不锈钢需求"),
        "印尼矿端政策与雨季升水主导成本底，纯镍显性累库压制上方弹性",
        _default_presets("沪镍"),
    ),
    "SN": ProductOntology(
        "SN",
        "nonferrous_new_energy",
        "有色与新能源材料",
        "nonferrous",
        "中游冶炼加工",
        ("缅甸佤邦与刚果金锡矿供应", "国内精炼锡冶炼开工", "半导体焊料与光伏焊带需求"),
        "矿端供给集中度高，库存与仓单弹性极敏感",
        _default_presets("沪锡"),
    ),
    "LC": ProductOntology(
        "LC",
        "nonferrous_new_energy",
        "有色与新能源材料",
        "new_energy",
        "中游冶炼加工",
        ("澳矿/非矿到港与云母提锂现金成本", "正极材料与动力储能电芯排产", "冶炼厂、下游与贸易商显性库存"),
        "青海盐湖冬季减产，年末抢装与春节前备货决定短期去库斜率",
        (
            ("期现基差", "碳酸锂现货报价与期货主力基差收敛了吗？"),
            ("趋势与波动", "碳酸锂当前价格处于上市以来什么分位？"),
            ("持仓博弈", "碳酸锂持仓变化与日线趋势是否共振？"),
        ),
    ),
    "SI": ProductOntology(
        "SI",
        "nonferrous_new_energy",
        "有色与新能源材料",
        "new_energy",
        "上游资源",
        ("川滇丰枯水期电价与硅炉开炉数", "多晶硅与有机硅耗硅需求", "行业社会库存与仓单集中注销期"),
        "5–10月西南丰水期成本下移增产，11–4月枯水期停炉减产",
        (
            ("硅光价差", "多晶硅与工业硅盘面价差当前处于什么水平？"),
            ("现期对照", "工业硅现货基差与价格分位说明什么？"),
            ("日线结构", "工业硅日线均线方向与波动率如何？"),
        ),
    ),
    "PS": ProductOntology(
        "PS",
        "nonferrous_new_energy",
        "有色与新能源材料",
        "new_energy",
        "中游冶炼加工",
        ("多晶硅头部企业检修与N型料产出比", "下游硅片排产与库存去化", "工业硅原料与综合电耗成本"),
        "光伏装机季度脉冲与行业自律减产直接左右硅料去库拐点",
        (
            ("晶硅利润差", "多晶硅相对工业硅的盘面加工差在扩大还是收窄？"),
            ("基差核对", "多晶硅当前现货价与主力合约升贴水如何？"),
            ("趋势核验", "多晶硅连续日线走势偏多还是偏空？"),
        ),
    ),
    # 3. Precious Metals (2)
    "AU": ProductOntology(
        "AU",
        "precious_macro",
        "贵金属宏观锚",
        "precious",
        "宏观定价锚",
        ("美债实际利率与美联储路径", "全球央行购金与地缘避险溢价", "人民币汇率折算内外盘价差"),
        "宏观事件与利率决议主导，不受单一国内工业淡旺季束缚",
        (
            ("金银比价", "当前沪金银比处于近5年偏高还是偏低区间？"),
            ("期现与仓单", "沪金指定交割金库仓单与基差有何变化？"),
            ("中长期趋势", "沪金日线均线排列与历史分位如何？"),
        ),
    ),
    "AG": ProductOntology(
        "AG",
        "precious_macro",
        "贵金属宏观锚",
        "precious",
        "宏观定价锚",
        ("贵金属金融属性与金银比修复", "光伏银浆与电子工业需求", "上期所及海外金库显性白银库存"),
        "兼具黄金避险锚与光伏工业弹性，波动率显著高于沪金",
        (
            ("金银比回归", "沪银相对沪金的金银比反映了怎样的强弱分化？"),
            ("白银库存", "上期所白银仓单近期是在去化还是累积？"),
            ("趋势波动", "沪银当前年化波动率与日线方向如何？"),
        ),
    ),
    # 4. Energy, Chemicals & Building Materials (14)
    "SC": ProductOntology(
        "SC",
        "energy_chem_material",
        "能源化工与建材",
        "energy_chem",
        "上游资源",
        ("OPEC+产量政策与中东地缘运费", "国内主营及地炼常减压开工率", "INE保税原油仓单与中东贴水"),
        "北半球夏季出行旺季与冬季取暖季构成年内两大消费锚",
        _default_presets("原油"),
    ),
    "PG": ProductOntology(
        "PG",
        "energy_chem_material",
        "能源化工与建材",
        "energy_chem",
        "上游资源",
        ("中东CP/北美FEI进口到岸成本", "PDH丙烷脱氢装置利润与开工", "冬季民用燃烧旺季需求"),
        "四季度至一季度燃烧需求走强，3月集中仓单注销期压制盘面",
        _default_presets("液化石油气"),
    ),
    "BU": ProductOntology(
        "BU",
        "energy_chem_material",
        "能源化工与建材",
        "energy_chem",
        "下游成材消费",
        ("原油原料成本与稀释沥青贴水", "炼厂排产转产渣油比例", "下半年公路市政赶工铺路需求"),
        "上半年梅雨淡季累库，8–11月迎年内道路施工去库高峰",
        _default_presets("石油沥青"),
    ),
    "MA": ProductOntology(
        "MA",
        "energy_chem_material",
        "能源化工与建材",
        "energy_chem",
        "上游资源",
        ("内地煤制开工与伊朗气头装置限气", "华东华南港口库存与到港船期", "沿海MTO制烯烃装置利润与负荷"),
        "冬季海外天然气限气缩减进口供给，MTO利润决定上方空间天花板",
        (
            ("MTO利润锚", "下游PP相对甲醇的MTO盘面价差目前能支撑甲醇吗？"),
            ("基差与仓单", "甲醇现货基差与郑商所仓单最新变化如何？"),
            ("趋势位置", "甲醇日线均线与近5年价格分位处于什么状态？"),
        ),
    ),
    "TA": ProductOntology(
        "TA",
        "energy_chem_material",
        "能源化工与建材",
        "energy_chem",
        "中游冶炼加工",
        ("原油—PX芳烃调油与成本传导", "PTA大型装置检修与现货加工费", "江浙聚酯负荷与织造终端订单"),
        "金三银四、金九银十服装家纺备货旺季，春节前后聚酯集中降负累库",
        _default_presets("PTA"),
    ),
    "PP": ProductOntology(
        "PP",
        "energy_chem_material",
        "能源化工与建材",
        "energy_chem",
        "中游冶炼加工",
        ("油制/煤制/PDH多工艺路线成本与检修", "石化两油库存去化速度", "塑编、BOPP薄膜与注塑家电需求"),
        "新产能投放周期内，装置意外临停与MTO成本线构成波段边界",
        (
            ("MTO加工差", "聚丙烯与甲醇算出的MTO盘面价差处于什么历史分位？"),
            ("现货升贴水", "聚丙烯当前现货基差对盘面有支撑吗？"),
            ("日线结构", "聚丙烯连续日线趋势偏多还是偏空？"),
        ),
    ),
    "V": ProductOntology(
        "V",
        "energy_chem_material",
        "能源化工与建材",
        "building_chem",
        "中游冶炼加工",
        ("电石法与乙烯法氯碱综合利润（以碱补氯）", "社会高库存与仓单消化压力", "地产管材型材与印度出口窗口"),
        "印度雨季前后采购脉冲明显，国内受地产施工季节性约束",
        _default_presets("PVC"),
    ),
    "UR": ProductOntology(
        "UR",
        "energy_chem_material",
        "能源化工与建材",
        "energy_chem",
        "中游冶炼加工",
        ("固定床与水煤浆日产水平", "春耕夏管农需追肥与复合肥开工", "法检出口政策与淡储节奏"),
        "3–7月春耕及高氮肥追肥旺季弹性最大，秋冬转入淡储与气头检修",
        _default_presets("尿素"),
    ),
    "SA": ProductOntology(
        "SA",
        "energy_chem_material",
        "能源化工与建材",
        "building_chem",
        "上游资源",
        ("天然碱、联碱与氨碱周度碱厂开工及库存", "浮法玻璃与光伏玻璃重碱刚需日熔量", "玻碱产业链利润再分配"),
        "夏季高温碱厂例行检修季易引发阶段性反弹，大产能背景下看低成本出清",
        (
            ("玻碱传导", "玻璃—纯碱盘面差反映下游玻璃厂承接力如何？"),
            ("基差仓单", "纯碱现货基差与郑商所仓单有何变化？"),
            ("历史分位", "纯碱当前价格在近5年处于什么分位？"),
        ),
    ),
    "FG": ProductOntology(
        "FG",
        "energy_chem_material",
        "能源化工与建材",
        "building_chem",
        "下游成材消费",
        ("全国浮法玻璃在产日熔量与冷修点火", "沙河及湖北样本厂库产销率", "保交楼竣工门窗家装深加工订单"),
        "梅雨季防霉降价去库，下半年金九银十竣工赶工需求最强",
        (
            ("玻璃原料差", "浮法玻璃扣除纯碱后的盘面价差处于盈利还是挤压区？"),
            ("仓单核查", "郑商所玻璃仓单数量与近期趋势如何？"),
            ("日线走势", "玻璃连续日线均线排列偏多还是偏空？"),
        ),
    ),
    "SH": ProductOntology(
        "SH",
        "energy_chem_material",
        "能源化工与建材",
        "building_chem",
        "上游资源",
        ("氯碱装置开工与液氯倒贴程度", "氧化铝投产送碱囤货需求", "32%液碱与50%高度碱折百价差及区域套利"),
        "氯碱耗电高且液碱储罐库容有限，检修季与氧化铝备货易放大短周期弹性",
        _default_presets("烧碱"),
    ),
    "RU": ProductOntology(
        "RU",
        "energy_chem_material",
        "能源化工与建材",
        "energy_chem",
        "上游资源",
        (
            "泰国及云南海南产区物候开割与台风降雨",
            "青岛一般贸易与深浅色库存",
            "半钢胎/全钢胎开工与老仓单集中注销",
        ),
        "2–4月全球低产停割季供给最紧，11月老胶仓单强制注销影响9-1跨期结构",
        _default_presets("天然橡胶"),
    ),
    "NR": ProductOntology(
        "NR",
        "energy_chem_material",
        "能源化工与建材",
        "energy_chem",
        "上游资源",
        ("东南亚标胶船货加工利润", "青岛保税及一般贸易标胶去库", "国内全钢胎重卡替换与轮胎出口"),
        "逐月连续交割无老胶集中注销断层，直接锚定海外轮胎级现货供需",
        _default_presets("20号胶"),
    ),
    "SP": ProductOntology(
        "SP",
        "energy_chem_material",
        "能源化工与建材",
        "energy_chem",
        "上游资源",
        (
            "海外阔叶/针叶浆厂外盘美金报价与罢工检修",
            "国内主流港口木浆库存",
            "生活用纸、双胶纸与白卡纸提价落地率",
        ),
        "春季教辅教材招标与秋季双十一包装备货为传统造纸旺季",
        _default_presets("纸浆"),
    ),
    # 5. Agricultural, Oilseeds, Softs & Livestock (16)
    "A": ProductOntology(
        "A",
        "agri_crush_livestock",
        "农牧油脂与软商品",
        "oilseed_feed",
        "上游资源",
        ("东北国产非转基因大豆单产与蛋白含量", "中储粮收购底价与拍卖投放", "豆制品食用消费刚需"),
        "10–11月东北新豆集中上市承压，中储粮收购挂牌价形成政策锚",
        _default_presets("豆一"),
    ),
    "B": ProductOntology(
        "B",
        "agri_crush_livestock",
        "农牧油脂与软商品",
        "oilseed_feed",
        "上游资源",
        ("CBOT美豆与巴西CNF升贴水进口完税成本", "国内港口月度大豆到港船期", "油厂盘面压榨毛利与买船进度"),
        "2–6月南美大豆集中上市，9–1月转向美豆出口装船与南美播种天气窗",
        (
            ("压榨毛利", "进口大豆盘面榨利当前是盈利还是倒挂？"),
            ("趋势分位", "豆二收盘价在近5年处于什么位置？"),
            ("日线信号", "豆二日线均线排列偏多还是偏空？"),
        ),
    ),
    "M": ProductOntology(
        "M",
        "agri_crush_livestock",
        "农牧油脂与软商品",
        "oilseed_feed",
        "中游冶炼加工",
        ("大豆到港与样本油厂周度压榨量", "油厂豆粕物理库存与未执行合同", "生猪禽料存栏与豆菜粕替代比价"),
        "4–8月南美豆巨量到港易累库，11–3月美豆衔接期与双节育肥备货易走基差",
        (
            ("去库原因", "豆粕去库是需求拉动还是压榨变化？"),
            ("油厂库存", "原料豆和豆粕库存变化一致吗？"),
            ("压榨进度", "实际压榨达到计划了吗？"),
        ),
    ),
    "RM": ProductOntology(
        "RM",
        "agri_crush_livestock",
        "农牧油脂与软商品",
        "oilseed_feed",
        "中游冶炼加工",
        ("加拿大菜籽进口政策与到港压榨", "沿海油厂菜粕库存", "水产投苗旺季需求与豆菜粕价差"),
        "5–9月南方水产饲料消费高峰期菜粕刚需最强",
        (
            ("豆菜粕价差", "当前豆菜粕盘面价差对菜粕替代需求有利吗？"),
            ("基差与仓单", "菜粕现货升贴水与郑商所仓单如何？"),
            ("日线方向", "菜粕连续日线走势处于什么状态？"),
        ),
    ),
    "P": ProductOntology(
        "P",
        "agri_crush_livestock",
        "农牧油脂与软商品",
        "oilseed_feed",
        "中游冶炼加工",
        ("MPOB马棕月度产量与库存报告", "印尼B40/B50生物柴油掺混政策", "国内进口倒挂幅度与豆棕价差"),
        "11月至次年2月东南亚雨季减产去库，3–10月进入增产累库周期",
        (
            ("豆棕替代", "当前豆棕盘面价差是否出现倒挂或极窄？"),
            ("期现与仓单", "棕榈油现货基差与仓单变化一致吗？"),
            ("历史分位", "棕榈油价格在近5年处于偏高还是偏低分位？"),
        ),
    ),
    "Y": ProductOntology(
        "Y",
        "agri_crush_livestock",
        "农牧油脂与软商品",
        "oilseed_feed",
        "中游冶炼加工",
        ("国内油厂大豆压榨被动产出", "商业豆油库存与餐饮包装油备货", "油粕比与三大油脂间价差"),
        "中秋国庆与元旦春节前小包装油集中备货去库",
        (
            ("油粕强弱", "当前豆油粕比与大豆盘面榨利处于什么区间？"),
            ("豆棕价差", "豆油相对棕榈油的价差在走阔还是收窄？"),
            ("基差核对", "豆油现货报价与主力期价升贴水如何？"),
        ),
    ),
    "OI": ProductOntology(
        "OI",
        "agri_crush_livestock",
        "农牧油脂与软商品",
        "oilseed_feed",
        "中游冶炼加工",
        ("加菜籽与毛菜油直接进口量", "华东及沿海菜油商业库存", "川渝小榨风味刚需与豆菜油价差"),
        "四季度冬季火锅餐饮旺季提振西南华东菜油提货",
        _default_presets("菜籽油"),
    ),
    "PK": ProductOntology(
        "PK",
        "agri_crush_livestock",
        "农牧油脂与软商品",
        "oilseed_feed",
        "上游资源",
        ("豫鲁产区花生种植面积与收获期降雨定质", "主力油厂开秤收购态度与通货米报价", "花生油粕压榨利润"),
        "9–10月新季春花生、麦茬花生上市定全年成本锚，农户惜售节奏左右波段",
        _default_presets("花生"),
    ),
    "C": ProductOntology(
        "C",
        "agri_crush_livestock",
        "农牧油脂与软商品",
        "oilseed_feed",
        "上游资源",
        (
            "东北华北新粮基层售粮进度与地趴粮潮粮压力",
            "北方四港下海量与南方港口内贸外贸谷物库存",
            "饲企玉米小麦替代与深加工开机",
        ),
        "11月至春节前新粮集中上量易季节性承压，二三季度进入青黄不接渠道博弈",
        (
            ("猪料比价", "玉米与豆粕构成的盘面猪料比目前对饲用需求意味着什么？"),
            ("期现与仓单", "玉米现货基差与99期货公开仓单近期怎么变？"),
            ("区间分位", "玉米价格处于近5年什么分位？"),
        ),
    ),
    "CS": ProductOntology(
        "CS",
        "agri_crush_livestock",
        "农牧油脂与软商品",
        "oilseed_feed",
        "中游冶炼加工",
        (
            "原料玉米收购成本与副产品折算利润",
            "山东河北淀粉企业开机率与行业库存",
            "淀粉糖替代白糖价差与造纸啤酒消费",
        ),
        "夏季冷饮啤酒旺季与双节备货拉动淀粉走货",
        _default_presets("玉米淀粉"),
    ),
    "CF": ProductOntology(
        "CF",
        "agri_crush_livestock",
        "农牧油脂与软商品",
        "oilseed_feed",
        "上游资源",
        ("新疆籽棉机采抢收开秤价", "纺企纱线开机、原料补库与坯布成品库存", "滑准税进口棉配额与内外棉价差"),
        "9–10月新疆棉采摘开秤博弈剧烈，3–4月及9–10月看纺织订单成色",
        _default_presets("棉花"),
    ),
    "SR": ProductOntology(
        "SR",
        "agri_crush_livestock",
        "农牧油脂与软商品",
        "oilseed_feed",
        "上游资源",
        (
            "巴西中南部甘蔗制糖比与原糖出口",
            "广西云南榨季产糖率与工业库存",
            "配额外进口糖浆预拌粉政策与进口利润",
        ),
        "11月至次年4月国内开榨累库，夏季冷饮消费旺季去库",
        _default_presets("白糖"),
    ),
    "LH": ProductOntology(
        "LH",
        "agri_crush_livestock",
        "农牧油脂与软商品",
        "oilseed_feed",
        "下游成材消费",
        ("10个月前能繁母猪存栏基数", "出栏均重、二次育肥与冻品入库节奏", "玉米豆粕饲料成本与屠宰白条走货"),
        "二三季度易现二育惜售断档反弹，冬至腌腊与春节前迎全年集中出栏大考",
        (
            ("养殖利润锚", "盘面猪料比指标当前处于近5年盈利还是亏损分位？"),
            ("全国均价对照", "生猪农业部均价参考与盘面走势方向一致吗？"),
            ("趋势结构", "生猪连续日线均线与波动率处于什么状态？"),
        ),
    ),
    "AP": ProductOntology(
        "AP",
        "agri_crush_livestock",
        "农牧油脂与软商品",
        "oilseed_feed",
        "上游资源",
        (
            "陕甘鲁花期冻害、套袋量与下树优果率",
            "11月入库峰值冷库库存与周度出库速度",
            "柑橘梨等时令水果替代比价",
        ),
        "4–5月炒作花期天气，10–11月定优果率与入库成本，春节及清明核验去库",
        _default_presets("苹果"),
    ),
    "CJ": ProductOntology(
        "CJ",
        "agri_crush_livestock",
        "农牧油脂与软商品",
        "oilseed_feed",
        "上游资源",
        ("新疆灰枣坐果期高温天气与下树商品率", "阿克苏阿拉尔喀什统货收购价", "沧州崔尔庄及广州销区到货消化"),
        "11月新疆新枣下树定产定价，中秋端午及春节滋补礼盒旺季去库",
        _default_presets("红枣"),
    ),
    "JD": ProductOntology(
        "JD",
        "agri_crush_livestock",
        "农牧油脂与软商品",
        "oilseed_feed",
        "下游成材消费",
        ("全国在产蛋鸡存栏与鸡苗补栏延淘节奏", "玉米豆粕蛋鸡配合料成本", "中秋国庆与春节商超食品厂备货"),
        "梅雨季储运受限易跌，8–9月中秋备货冲年内高点后近月转淡",
        _default_presets("鸡蛋"),
    ),
    # 6. Cash-settled Freight Index (1)
    "EC": ProductOntology(
        "EC",
        "cash_settled_index",
        "航运现金交割指数",
        "shipping",
        "现金交割指数",
        (
            "上海航交所SCFIS欧线每周一15:00结算指数",
            "班轮联盟GRI宣涨函与线上即期订舱实际成交价",
            "苏伊士/好望角绕行运距与停航缩舱净运力",
        ),
        "7–8月欧美圣诞备货与11–12月次年长协签约挺价季为两大博弈高潮",
        (
            ("运价指数", "欧线结算运价指数最近一期怎么变了？"),
            ("交割结算", "EC2611 的三次定价如何影响交割结算？"),
            ("停航影响", "停航公告能说明运力减少吗？"),
        ),
    ),
}


def get(symbol: str) -> ProductOntology | None:
    return ONTOLOGY.get(symbol.strip().upper())


def _fmt_num(val, digits: int = 2) -> str:
    if val is None:
        return ""
    if isinstance(val, int) or (isinstance(val, float) and val.is_integer()):
        return str(int(val))
    return f"{float(val):.{digits}f}".rstrip("0").rstrip(".")


def build_causal_narrative(card: dict) -> dict:
    """Synthesize plain-Chinese headline, horizon tug-of-war, thermometers and If-Then scenarios from verified card data."""
    symbol = card["symbol"]
    name = card.get("name") or symbol
    meta = get(symbol)
    market = card.get("market") or {}
    m_vals = market.get("values") or {}
    tech_brief = (card.get("research_brief") or {}).get("technical") or {}
    basis = card.get("basis") or {}
    b_vals = basis.get("values") or {}
    inv = card.get("inventory") or {}
    i_vals = inv.get("values") or {}
    spread = card.get("spread") or {}
    s_vals = spread.get("values") or {}
    pct = card.get("percentiles") or {}
    roll = card.get("roll_guard") or {}
    margins = [m for m in (card.get("margins") or []) if m.get("status") == "collected" and m.get("values")]
    inst = (card.get("research_brief") or {}).get("institutions") or {}
    fund_rows = ((card.get("research_brief") or {}).get("fundamentals") or {}).get("rows") or []

    # 1. Build contextual thermometers (only real observed metrics with valid [0,100] percentiles or rates)
    thermometers = []
    if pct.get("close_pct_5y") is not None and m_vals.get("close") is not None:
        p5 = pct["close_pct_5y"]
        unit = m_vals.get("quote_unit") or ""
        zone = "低位安全垫区" if p5 <= 25 else "高位拥挤区" if p5 >= 75 else "中枢震荡区"
        thermometers.append(
            {
                "id": "price_5y",
                "label": f"价格历史位置（{pct.get('span_label', '历史区间')}）",
                "value_text": f"{_fmt_num(m_vals['close'])} {unit}".strip(),
                "pct": p5,
                "hint": f"位于{pct.get('span_label', '历史')} {p5}% 分位（{zone}，起点 {pct.get('since_date')}）",
            }
        )
    if pct.get("close_seasonal_pct") is not None and pct.get("seasonal_years", 0) >= 3:
        sp = pct["close_seasonal_pct"]
        unit = m_vals.get("quote_unit") or ""
        s_med = pct.get("seasonal_median")
        seasonal_val = (
            f"同期中位 {_fmt_num(s_med)} {unit}".strip()
            if s_med is not None
            else f"历年同期 {pct['seasonal_years']} 年"
        )
        thermometers.append(
            {
                "id": "price_seasonal",
                "label": f"历年同期位置（{pct['seasonal_years']}年±20日）",
                "value_text": seasonal_val,
                "pct": sp,
                "hint": "高于历年同期中枢"
                if sp >= 65
                else "低于历年同期中枢"
                if sp <= 35
                else "贴近历年同期常态水平",
            }
        )
    if margins:
        primary_m = margins[0]
        mv = primary_m["values"]
        mp = mv.get("pct_5y") if mv.get("pct_5y") is not None else mv.get("pct_1y")
        if mp is not None:
            thermometers.append(
                {
                    "id": "chain_margin",
                    "label": f"{primary_m['name']}（{mv.get('span_label', '历史')}）",
                    "value_text": f"{_fmt_num(mv['current'])} {primary_m['unit']}",
                    "pct": mp,
                    "hint": mv.get("hint") or primary_m["formula"],
                }
            )
    if pct.get("volatility20_pct_rank") is not None:
        vp = pct["volatility20_pct_rank"]
        v_raw = ((card.get("technicals") or {}).get("values") or {}).get("volatility20_pct")
        thermometers.append(
            {
                "id": "vol_rank",
                "label": "20日波动率热度分位",
                "value_text": f"{_fmt_num(v_raw)}%" if v_raw is not None else f"{vp}% 分位",
                "pct": vp,
                "hint": "波动放大，需放宽止损带宽防洗盘"
                if vp >= 75
                else "波动收敛，留意突破变盘窗口"
                if vp <= 25
                else "波动温和正常",
            }
        )

    # 2. Build Horizon-Aligned Tug-of-War (Near-term Reality vs Medium-term Structure/Expectation)
    near_bull, near_bear = [], []
    mid_bull, mid_bear = [], []

    if basis.get("status") == "collected" and b_vals.get("basis") is not None:
        b_num = b_vals["basis"]
        b_rate = b_vals.get("basis_rate_pct")
        rate_txt = f"（基差率 {b_rate:+.2f}%）" if isinstance(b_rate, (int, float)) else ""
        if b_num > 0:
            near_bull.append(
                f"现货升水 +{_fmt_num(b_num)} {b_vals.get('quote_unit', '')}{rate_txt}，期货贴水提供下沿保护"
            )
        elif b_num < 0:
            near_bear.append(
                f"现货贴水 {_fmt_num(b_num)} {b_vals.get('quote_unit', '')}{rate_txt}，盘面升水需现货跟涨兑现"
            )

    if inv.get("status") == "collected" and isinstance(i_vals.get("change"), (int, float)):
        ic = i_vals["change"]
        u = i_vals.get("unit") or ""
        if ic < 0:
            near_bull.append(
                f"交易所仓单减少 {_fmt_num(abs(ic))} {u}（总量 {_fmt_num(i_vals.get('quantity'))} {u}），显性交割压力缓释"
            )
        elif ic > 0:
            near_bear.append(
                f"交易所仓单增加 +{_fmt_num(ic)} {u}（总量 {_fmt_num(i_vals.get('quantity'))} {u}），实盘注册意愿增强"
            )

    for row in fund_rows[:2]:
        if row.get("stale") or row.get("label") in {"仓单数量", "现货报价"}:
            continue
        chg = row.get("change")
        if isinstance(chg, (int, float)) and chg != 0:
            lbl = row["label"]
            if "库存" in lbl:
                target = near_bull if chg < 0 else near_bear
                verb = "去库" if chg < 0 else "累库"
                target.append(
                    f"{lbl}较上期{verb} {_fmt_num(abs(chg))} {row.get('unit', '')}（{row['data_as_of']}）"
                )
            elif "指数" in lbl:
                target = near_bull if chg > 0 else near_bear
                target.append(f"{lbl}较上期 {chg:+.2f} {row.get('unit', '')}（{row['data_as_of']}）")

    if tech_brief.get("bias") == "bullish":
        mid_bull.append(f"日线均线偏多排列（收盘 {_fmt_num(tech_brief.get('close'))} > EMA20 > EMA60）")
    elif tech_brief.get("bias") == "bearish":
        mid_bear.append(f"日线均线偏空排列（收盘 {_fmt_num(tech_brief.get('close'))} < EMA20 < EMA60）")
    elif tech_brief.get("bias") == "mixed":
        mid_bull.append("短中期均线交织分歧，趋势正在重选方向")

    if spread.get("status") == "collected" and isinstance(s_vals.get("far_minus_near"), (int, float)):
        fmn = s_vals["far_minus_near"]
        if fmn < 0:
            near_bull.append(
                f"期限呈近强远弱 Backwardation（远减近 {_fmt_num(fmn)} {s_vals.get('quote_unit', '')}）"
            )
        elif fmn > 0:
            mid_bear.append(
                f"期限呈远月升水 Contango（远减近 +{_fmt_num(fmn)} {s_vals.get('quote_unit', '')}）"
            )

    if margins:
        mv = margins[0]["values"]
        mp = mv.get("pct_5y")
        if isinstance(mp, (int, float)):
            if mp >= 75:
                mid_bear.append(f"{margins[0]['name']}升至 {mp}% 高分位，中游盈利修复易刺激远期供应回升")
            elif mp <= 25:
                mid_bull.append(f"{margins[0]['name']}压至 {mp}% 低分位，亏损倒挂增强成本底部韧性")

    for item in inst.get("items") or []:
        st = item.get("stance")
        hz = item.get("horizon") or "机构研判"
        tag = f"{item['institution']}（{hz}）：{item['summary']}"
        if st == "bullish":
            mid_bull.append(tag)
        elif st == "bearish":
            mid_bear.append(tag)

    def _score_bar(bulls: list[str], bears: list[str]) -> int | None:
        total = len(bulls) + len(bears)
        if total == 0:
            return None
        return round(100 * len(bulls) / total)

    tug_of_war = [
        {
            "id": "near_reality",
            "horizon": "近端现实（现货基差 · 仓单 · 当期披露）",
            "bull_score": _score_bar(near_bull, near_bear),
            "bull_points": near_bull,
            "bear_points": near_bear,
            "empty_reason": "待取得同日现货基差或仓单增减后判定近端强弱。"
            if not (near_bull or near_bear)
            else None,
        },
        {
            "id": "mid_expectation",
            "horizon": "中期结构（均线趋势 · 盘面利润 · 机构期限）",
            "bull_score": _score_bar(mid_bull, mid_bear),
            "bull_points": mid_bull,
            "bear_points": mid_bear,
            "empty_reason": "待取得足够连续日线或机构研报后判定中期预期。"
            if not (mid_bull or mid_bear)
            else None,
        },
    ]

    # 3. Synthesize Plain-Human Headline & Explanation without jargon
    parts_head = []
    explain_parts = []
    if basis.get("status") == "collected" and b_vals.get("basis") is not None:
        if b_vals["basis"] > 0:
            parts_head.append("现货贴水保护仍在")
            explain_parts.append(
                f"现货报价高于主力期货 {_fmt_num(b_vals['basis'])} {b_vals.get('quote_unit', '')}（期货贴水），向下追空有现货锚牵制"
            )
        elif b_vals["basis"] < 0:
            parts_head.append("期货升水抢跑预期")
            explain_parts.append(
                f"主力期货高于现货 {_fmt_num(abs(b_vals['basis']))} {b_vals.get('quote_unit', '')}（期货升水），需要后续现货提价或去库兑现"
            )
    if tech_brief.get("bias") in {"bullish", "bearish", "mixed"}:
        t_map = {"bullish": "日线趋势偏多", "bearish": "日线趋势偏空", "mixed": "日线短中期分歧"}
        parts_head.append(t_map[tech_brief["bias"]])
        explain_parts.append(tech_brief.get("reason", "").rstrip("。"))
    if pct.get("close_pct_5y") is not None:
        p5 = pct["close_pct_5y"]
        explain_parts.append(f"当前收盘价处于{pct.get('span_label', '历史')} {p5}% 分位")
    if margins:
        m0 = margins[0]
        mv = m0["values"]
        explain_parts.append(
            f"关联产业链【{m0['name']}】当前为 {_fmt_num(mv['current'])} {m0['unit']}（{mv.get('hint', '')}）"
        )

    missing_dims = [
        lbl
        for key, lbl in (("basis", "现货基差"), ("inventory", "交易所仓单"), ("spread", "跨期结算价差"))
        if (card.get(key) or {}).get("status") != "collected"
    ]
    if missing_dims:
        explain_parts.append(f"当前{'、'.join(missing_dims)}暂未取得同日核验值，系统保持留白不作猜测")

    if not parts_head:
        if market.get("status") == "collected" and m_vals.get("close") is not None:
            headline = f"{name}最新连续收盘 {_fmt_num(m_vals['close'])} {m_vals.get('quote_unit', '')}，等待更多期现分项核验"
        else:
            headline = f"{name}实时核验数据待读取，请对照下方已披露分项查看"
    elif len(parts_head) >= 2 and (
        ("贴水保护" in parts_head[0] and "偏空" in parts_head[1])
        or ("升水" in parts_head[0] and "偏多" not in parts_head[1])
    ):
        headline = f"{parts_head[0]}，但{parts_head[1]}——典型「近端现实与盘面趋势劈叉」格局"
    else:
        headline = "，".join(parts_head)

    plain_explain = "；".join(p for p in explain_parts if p) + ("。" if explain_parts else "")

    # 4. Falsifiable If-Then Decision Scenarios anchored to real numbers
    ema20 = tech_brief.get("ema20")
    unit = m_vals.get("quote_unit") or ""
    driver_0 = meta.drivers[0] if meta and meta.drivers else "下期库存与现货成交"
    driver_1 = meta.drivers[1] if meta and len(meta.drivers) > 1 else "仓单与基差变化"
    ema_cond_up = (
        f"且日线收盘有效站稳 20 日均线（{_fmt_num(ema20)} {unit}）上方" if ema20 else "且日线均线转入多头排列"
    )
    ema_cond_dn = f"且日线受制于 20 日均线（{_fmt_num(ema20)} {unit}）下方" if ema20 else "且日线维持空头压制"

    scenarios = [
        {
            "id": "confirm_bull",
            "title": "情景 A · 偏多驱动确认条件",
            "if_condition": f"若后续披露的【{driver_0}】出现收紧/去化信号，现货升贴水走强{ema_cond_up}",
            "then_implication": "确认现实端支撑向盘面传导成立，空头被动回补概率上升，可将该假设加入判断账本跟踪下一期数据对账。",
        },
        {
            "id": "falsify_bear",
            "title": "情景 B · 偏空/证伪观察条件",
            "if_condition": f"若【{driver_1}】反向累积、交易所注册仓单放量增加{ema_cond_dn}",
            "then_implication": "推翻需求改善或挺价假设，高升水/高分位品种面临向现货成本锚回归的压力。",
        },
    ]

    return {
        "archetype": meta.archetype if meta else "general",
        "archetype_label": meta.archetype_label if meta else "商品期货",
        "chain_id": meta.chain_id if meta else "",
        "role": meta.role if meta else "",
        "drivers": list(meta.drivers) if meta else [],
        "seasonal_anchor": meta.seasonal_anchor if meta else "",
        "presets": [list(p) for p in meta.presets] if meta else [],
        "headline": headline,
        "plain_explain": plain_explain,
        "roll_guard": roll,
        "thermometers": thermometers,
        "tug_of_war": tug_of_war,
        "scenarios": scenarios,
    }
