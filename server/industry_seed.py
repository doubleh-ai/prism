"""Industry-chain seed data for the 产业链图谱 panel.

Curated A-share core chains so the panel has real content on day one
(analyses feed it more over time via analysis_engine ingestion). Node ids
are positional (``ch{chain}-n{node}``) so they're stable and
language-independent — Chinese names would slugify poorly.
"""
# tier: 0=上游 1=中游 2=下游

# Country centroids (lat, lng) — backs the world-map bubbles.
import contextlib

GEO_COORDS = {
    "CN": (35.0, 105.0),
    "TW": (23.7, 121.0),
    "KR": (36.5, 127.8),
    "JP": (36.2, 138.2),
    "US": (38.0, -97.0),
    "DE": (51.2, 10.4),
    "AU": (-25.3, 133.8),
    "CL": (-35.7, -71.5),
    "SA": (23.9, 45.1),
    "IN": (20.6, 78.9),
    "VN": (16.0, 106.0),
    "MY": (4.2, 101.9),
}

CHAINS = [
    {
        "category": "锂电池",
        "nodes": [
            {"name": "赣锋锂业", "ticker": "002460", "country": "CN", "tier": 0, "role": "锂矿/锂盐"},
            {"name": "天齐锂业", "ticker": "002466", "country": "CN", "tier": 0, "role": "锂矿/锂盐"},
            {"name": "澳洲锂矿", "country": "AU", "tier": 0, "role": "锂辉石原料"},
            {"name": "当升科技", "ticker": "300073", "country": "CN", "tier": 1, "role": "正极材料"},
            {"name": "恩捷股份", "ticker": "002812", "country": "CN", "tier": 1, "role": "隔膜"},
            {"name": "天赐材料", "ticker": "002709", "country": "CN", "tier": 1, "role": "电解液"},
            {
                "name": "宁德时代",
                "ticker": "300750",
                "country": "CN",
                "tier": 1,
                "role": "动力电池",
                "market_share": 0.37,
            },
            {
                "name": "比亚迪",
                "ticker": "002594",
                "country": "CN",
                "tier": 1,
                "role": "电池+整车",
                "market_share": 0.16,
            },
            {"name": "理想汽车", "ticker": "LI", "country": "CN", "tier": 2, "role": "新能源车企"},
        ],
        "edges": [
            {"s": 0, "t": 3},
            {"s": 1, "t": 3},
            {"s": 2, "t": 1},
            {"s": 3, "t": 6},
            {"s": 4, "t": 6},
            {"s": 5, "t": 6},
            {"s": 3, "t": 7},
            {"s": 6, "t": 8},
            {"s": 7, "t": 8},
        ],
    },
    {
        "category": "光伏",
        "nodes": [
            {"name": "通威股份", "ticker": "600438", "country": "CN", "tier": 0, "role": "多晶硅料"},
            {"name": "大全能源", "ticker": "688303", "country": "CN", "tier": 0, "role": "高纯多晶硅"},
            {"name": "隆基绿能", "ticker": "601012", "country": "CN", "tier": 1, "role": "单晶硅片/组件"},
            {"name": "TCL中环", "ticker": "002129", "country": "CN", "tier": 1, "role": "硅片"},
            {"name": "爱旭股份", "ticker": "600732", "country": "CN", "tier": 1, "role": "电池片"},
            {"name": "晶澳科技", "ticker": "002459", "country": "CN", "tier": 1, "role": "组件"},
            {"name": "三峡能源", "ticker": "600905", "country": "CN", "tier": 2, "role": "光伏电站运营"},
        ],
        "edges": [
            {"s": 0, "t": 2},
            {"s": 1, "t": 2},
            {"s": 0, "t": 3},
            {"s": 2, "t": 4},
            {"s": 3, "t": 4},
            {"s": 4, "t": 5},
            {"s": 5, "t": 6},
            {"s": 2, "t": 5},
        ],
    },
    {
        "category": "半导体",
        "nodes": [
            {"name": "北方华创", "ticker": "002371", "country": "CN", "tier": 0, "role": "刻蚀/沉积设备"},
            {"name": "中微公司", "ticker": "688012", "country": "CN", "tier": 0, "role": "刻蚀设备"},
            {"name": "沪硅产业", "ticker": "688126", "country": "CN", "tier": 0, "role": "硅片材料"},
            {"name": "雅克科技", "ticker": "002409", "country": "CN", "tier": 0, "role": "光刻胶/前驱体"},
            {"name": "中芯国际", "ticker": "688981", "country": "CN", "tier": 1, "role": "晶圆代工"},
            {"name": "韦尔股份", "ticker": "603501", "country": "CN", "tier": 1, "role": "CIS芯片设计"},
            {"name": "兆易创新", "ticker": "603986", "country": "CN", "tier": 1, "role": "存储/MCU设计"},
            {"name": "长电科技", "ticker": "600584", "country": "CN", "tier": 2, "role": "封测"},
            {"name": "通富微电", "ticker": "002156", "country": "CN", "tier": 2, "role": "封测"},
        ],
        "edges": [
            {"s": 0, "t": 4},
            {"s": 1, "t": 4},
            {"s": 2, "t": 4},
            {"s": 3, "t": 4},
            {"s": 5, "t": 7},
            {"s": 6, "t": 8},
            {"s": 4, "t": 7},
            {"s": 4, "t": 8},
        ],
    },
    {
        "category": "稀土",
        "nodes": [
            {"name": "北方稀土", "ticker": "600111", "country": "CN", "tier": 0, "role": "轻稀土采选"},
            {"name": "盛和资源", "ticker": "600392", "country": "CN", "tier": 0, "role": "稀土采选/冶炼"},
            {"name": "宁波韵升", "ticker": "600366", "country": "CN", "tier": 1, "role": "钕铁硼磁材"},
            {"name": "中科三环", "ticker": "000970", "country": "CN", "tier": 1, "role": "钕铁硼磁材"},
            {
                "name": "金风科技",
                "ticker": "002202",
                "country": "CN",
                "tier": 2,
                "role": "风电整机（永磁电机）",
            },
        ],
        "edges": [
            {"s": 0, "t": 2},
            {"s": 1, "t": 2},
            {"s": 0, "t": 3},
            {"s": 1, "t": 3},
            {"s": 2, "t": 4},
            {"s": 3, "t": 4},
        ],
    },
    {
        "category": "白酒",
        "nodes": [
            {
                "name": "贵州茅台",
                "ticker": "600519",
                "country": "CN",
                "tier": 1,
                "role": "高端白酒",
                "market_share": 0.6,
            },
            {"name": "五粮液", "ticker": "000858", "country": "CN", "tier": 1, "role": "高端白酒"},
            {"name": "泸州老窖", "ticker": "000568", "country": "CN", "tier": 1, "role": "高端白酒"},
            {"name": "山西汾酒", "ticker": "600809", "country": "CN", "tier": 1, "role": "次高端白酒"},
            {"name": "白酒经销/消费", "country": "CN", "tier": 2, "role": "渠道与终端消费"},
        ],
        "edges": [
            {"s": 0, "t": 4},
            {"s": 1, "t": 4},
            {"s": 2, "t": 4},
            {"s": 3, "t": 4},
        ],
    },
    {
        "category": "创新药",
        "nodes": [
            {"name": "药明康德", "ticker": "603259", "country": "CN", "tier": 0, "role": "CXO研发生产外包"},
            {"name": "凯莱英", "ticker": "002821", "country": "CN", "tier": 0, "role": "CDMO"},
            {"name": "恒瑞医药", "ticker": "600276", "country": "CN", "tier": 1, "role": "创新药"},
            {"name": "百济神州", "ticker": "688235", "country": "CN", "tier": 1, "role": "创新药（肿瘤）"},
            {"name": "迈瑞医疗", "ticker": "300760", "country": "CN", "tier": 2, "role": "医疗器械"},
        ],
        "edges": [
            {"s": 0, "t": 2},
            {"s": 1, "t": 2},
            {"s": 0, "t": 3},
            {"s": 1, "t": 3},
            {"s": 2, "t": 4},
            {"s": 3, "t": 4},
        ],
    },
    {
        "category": "电动车",
        "nodes": [
            {
                "name": "比亚迪",
                "ticker": "002594",
                "country": "CN",
                "tier": 1,
                "role": "新能源整车",
                "market_share": 0.35,
            },
            {"name": "赛力斯", "ticker": "601127", "country": "CN", "tier": 1, "role": "新能源整车（问界）"},
            {"name": "长城汽车", "ticker": "601633", "country": "CN", "tier": 1, "role": "新能源整车"},
            {"name": "宁德时代", "ticker": "300750", "country": "CN", "tier": 0, "role": "动力电池"},
            {"name": "福耀玻璃", "ticker": "600660", "country": "CN", "tier": 0, "role": "汽车玻璃"},
        ],
        "edges": [
            {"s": 3, "t": 0},
            {"s": 3, "t": 1},
            {"s": 3, "t": 2},
            {"s": 4, "t": 0},
            {"s": 4, "t": 1},
            {"s": 4, "t": 2},
        ],
    },
    {
        "category": "消费电子",
        "nodes": [
            {"name": "京东方", "ticker": "000725", "country": "CN", "tier": 0, "role": "显示面板"},
            {"name": "TCL科技", "ticker": "000100", "country": "CN", "tier": 0, "role": "显示面板"},
            {
                "name": "立讯精密",
                "ticker": "002475",
                "country": "CN",
                "tier": 1,
                "role": "消费电子组装/零部件",
            },
            {"name": "歌尔股份", "ticker": "002241", "country": "CN", "tier": 1, "role": "声学/VR组装"},
            {"name": "苹果供应链", "country": "US", "tier": 2, "role": "终端品牌"},
        ],
        "edges": [
            {"s": 0, "t": 2},
            {"s": 1, "t": 2},
            {"s": 0, "t": 3},
            {"s": 2, "t": 4},
            {"s": 3, "t": 4},
        ],
    },
    {
        "category": "黑色冶炼",
        "nodes": [
            {"name": "中国神华", "ticker": "601088", "country": "CN", "tier": 0, "role": "煤炭资源采选"},
            {"name": "淮北矿业", "ticker": "600985", "country": "CN", "tier": 0, "role": "炼焦煤与焦化"},
            {"name": "宝钢股份", "ticker": "600019", "country": "CN", "tier": 1, "role": "高炉板材冶炼"},
            {"name": "华菱钢铁", "ticker": "000932", "country": "CN", "tier": 1, "role": "宽厚板与螺纹"},
            {"name": "中国建筑", "ticker": "601668", "country": "CN", "tier": 2, "role": "基建房建终端用钢"},
        ],
        "edges": [{"s": 0, "t": 2}, {"s": 1, "t": 2}, {"s": 1, "t": 3}, {"s": 2, "t": 4}, {"s": 3, "t": 4}],
    },
    {
        "category": "有色金属",
        "nodes": [
            {"name": "紫金矿业", "ticker": "601899", "country": "CN", "tier": 0, "role": "铜金矿山采选"},
            {"name": "中国铝业", "ticker": "601600", "country": "CN", "tier": 1, "role": "氧化铝与电解铝"},
            {"name": "江西铜业", "ticker": "600362", "country": "CN", "tier": 1, "role": "阴极铜冶炼加工"},
            {"name": "云铝股份", "ticker": "000807", "country": "CN", "tier": 2, "role": "绿电铝材下游"},
        ],
        "edges": [{"s": 0, "t": 2}, {"s": 1, "t": 3}],
    },
    {
        "category": "贵金属",
        "nodes": [
            {"name": "山东黄金", "ticker": "600547", "country": "CN", "tier": 0, "role": "黄金矿山开采"},
            {"name": "中金黄金", "ticker": "600489", "country": "CN", "tier": 1, "role": "金铜冶炼精炼"},
            {"name": "聚和材料", "ticker": "688503", "country": "CN", "tier": 2, "role": "光伏导电银浆应用"},
        ],
        "edges": [{"s": 0, "t": 1}, {"s": 1, "t": 2}],
    },
    {
        "category": "能源化工",
        "nodes": [
            {"name": "中国石油", "ticker": "601857", "country": "CN", "tier": 0, "role": "原油天然气开采"},
            {"name": "宝丰能源", "ticker": "600989", "country": "CN", "tier": 1, "role": "煤制甲醇与烯烃MTO"},
            {"name": "荣盛石化", "ticker": "002493", "country": "CN", "tier": 1, "role": "炼化PX与PTA聚酯"},
            {
                "name": "赛轮轮胎",
                "ticker": "601058",
                "country": "CN",
                "tier": 2,
                "role": "半钢与全钢轮胎制造",
            },
        ],
        "edges": [{"s": 0, "t": 2}, {"s": 1, "t": 3}, {"s": 2, "t": 3}],
    },
    {
        "category": "建材氯碱",
        "nodes": [
            {"name": "远兴能源", "ticker": "000683", "country": "CN", "tier": 0, "role": "天然碱重碱生产"},
            {"name": "君正集团", "ticker": "601216", "country": "CN", "tier": 1, "role": "氯碱烧碱与PVC"},
            {"name": "旗滨集团", "ticker": "601636", "country": "CN", "tier": 2, "role": "浮法与光伏玻璃"},
        ],
        "edges": [{"s": 0, "t": 2}, {"s": 1, "t": 2}],
    },
    {
        "category": "农牧饲渔",
        "nodes": [
            {"name": "隆平高科", "ticker": "000998", "country": "CN", "tier": 0, "role": "玉米谷物育种"},
            {"name": "金龙鱼", "ticker": "300999", "country": "CN", "tier": 1, "role": "大豆压榨豆粕豆油"},
            {
                "name": "海大集团",
                "ticker": "002311",
                "country": "CN",
                "tier": 1,
                "role": "水产与禽畜配合饲料",
            },
            {"name": "牧原股份", "ticker": "002714", "country": "CN", "tier": 2, "role": "生猪规模化养殖"},
        ],
        "edges": [{"s": 0, "t": 2}, {"s": 1, "t": 2}, {"s": 2, "t": 3}],
    },
    {
        "category": "航运港口",
        "nodes": [
            {"name": "招商轮船", "ticker": "601872", "country": "CN", "tier": 0, "role": "远洋油散运力"},
            {"name": "中远海控", "ticker": "601919", "country": "CN", "tier": 1, "role": "亚欧集装箱班轮"},
            {"name": "上港集团", "ticker": "600018", "country": "CN", "tier": 2, "role": "母港集装箱装卸"},
        ],
        "edges": [{"s": 0, "t": 1}, {"s": 1, "t": 2}],
    },
    {
        "category": "AI算力与服务器",
        "nodes": [
            {
                "name": "沪电股份",
                "ticker": "002463",
                "country": "CN",
                "tier": 0,
                "role": "高速高频AI服务器PCB",
            },
            {
                "name": "胜宏科技",
                "ticker": "300476",
                "country": "CN",
                "tier": 0,
                "role": "AI算力高多层PCB/HDI",
            },
            {
                "name": "天孚通信",
                "ticker": "300394",
                "country": "CN",
                "tier": 0,
                "role": "光引擎与高速光器件",
            },
            {"name": "英维克", "ticker": "002837", "country": "CN", "tier": 0, "role": "算力液冷散热系统"},
            {"name": "寒武纪", "ticker": "688256", "country": "CN", "tier": 0, "role": "国产AI训推芯片"},
            {
                "name": "海光信息",
                "ticker": "688041",
                "country": "CN",
                "tier": 0,
                "role": "服务器CPU与DCU算力芯片",
            },
            {
                "name": "中际旭创",
                "ticker": "300308",
                "country": "CN",
                "tier": 1,
                "role": "800G/1.6T高速光模块龙头",
            },
            {"name": "新易盛", "ticker": "300502", "country": "CN", "tier": 1, "role": "高速数通光模块"},
            {
                "name": "工业富联",
                "ticker": "601138",
                "country": "CN",
                "tier": 1,
                "role": "AI服务器整机/机柜与高速交换机ODM龙头",
            },
            {
                "name": "浪潮信息",
                "ticker": "000977",
                "country": "CN",
                "tier": 1,
                "role": "通用及AI服务器整机",
            },
            {
                "name": "中科曙光",
                "ticker": "603019",
                "country": "CN",
                "tier": 1,
                "role": "国产高端计算与液冷服务器",
            },
            {"name": "北美云厂商与AI数据中心", "country": "US", "tier": 2, "role": "超大规模云资本开支终端"},
        ],
        "edges": [
            {"s": 0, "t": 8},
            {"s": 1, "t": 8},
            {"s": 3, "t": 8},
            {"s": 2, "t": 6},
            {"s": 2, "t": 7},
            {"s": 4, "t": 9},
            {"s": 5, "t": 10},
            {"s": 0, "t": 9},
            {"s": 6, "t": 11},
            {"s": 7, "t": 11},
            {"s": 8, "t": 11},
            {"s": 9, "t": 11},
            {"s": 10, "t": 11},
        ],
    },
    {
        "category": "人形机器人与精密制造",
        "nodes": [
            {"name": "绿的谐波", "ticker": "688017", "country": "CN", "tier": 0, "role": "精密谐波减速器"},
            {
                "name": "双环传动",
                "ticker": "002472",
                "country": "CN",
                "tier": 0,
                "role": "RV减速器与精密齿轮",
            },
            {
                "name": "鸣志电器",
                "ticker": "603728",
                "country": "CN",
                "tier": 0,
                "role": "空心杯电机与步进驱动",
            },
            {
                "name": "三花智控",
                "ticker": "002050",
                "country": "CN",
                "tier": 1,
                "role": "机电执行器总成与热管理",
            },
            {
                "name": "拓普集团",
                "ticker": "601689",
                "country": "CN",
                "tier": 1,
                "role": "直线/旋转执行器与轻量化底盘",
            },
            {
                "name": "汇川技术",
                "ticker": "300124",
                "country": "CN",
                "tier": 1,
                "role": "工业伺服与运动控制龙头",
            },
            {"name": "全球人形机器人整机", "country": "US", "tier": 2, "role": "具身智能终端量产"},
        ],
        "edges": [
            {"s": 0, "t": 3},
            {"s": 1, "t": 4},
            {"s": 2, "t": 3},
            {"s": 2, "t": 4},
            {"s": 3, "t": 6},
            {"s": 4, "t": 6},
            {"s": 5, "t": 6},
        ],
    },
    {
        "category": "电网设备与算力电力",
        "nodes": [
            {
                "name": "特变电工",
                "ticker": "600089",
                "country": "CN",
                "tier": 0,
                "role": "特高压变压器与电线电缆",
            },
            {
                "name": "思源电气",
                "ticker": "002028",
                "country": "CN",
                "tier": 1,
                "role": "高压开关/GIS与海外电网设备",
            },
            {
                "name": "国电南瑞",
                "ticker": "600406",
                "country": "CN",
                "tier": 1,
                "role": "电网自动化与特高压直流保护",
            },
            {
                "name": "平高电气",
                "ticker": "600312",
                "country": "CN",
                "tier": 1,
                "role": "特高压高压开关核心供应商",
            },
            {"name": "国家电网与AIDC配电", "country": "CN", "tier": 2, "role": "电网投资与算力中心供电终端"},
        ],
        "edges": [
            {"s": 0, "t": 1},
            {"s": 0, "t": 4},
            {"s": 1, "t": 4},
            {"s": 2, "t": 4},
            {"s": 3, "t": 4},
        ],
    },
]


def seed_all(conn):
    """Idempotently upsert geo coords + industry chains. Called from init_db."""
    import time

    now = int(time.time())

    # Geo coords (lat/lng) — UPDATE existing geo_regions rows.
    for code, (lat, lng) in GEO_COORDS.items():
        with contextlib.suppress(Exception):
            conn.execute(
                "UPDATE geo_regions SET lat=?, lng=?, updated_at=? WHERE country_code=?",
                [lat, lng, now, code],
            )

    for ci, chain in enumerate(CHAINS):
        cat = chain["category"]
        nid_by_idx = {}
        for ni, node in enumerate(chain["nodes"]):
            nid = f"ch{ci}-n{ni}"
            nid_by_idx[ni] = nid
            with contextlib.suppress(Exception):
                conn.execute(
                    "INSERT OR IGNORE INTO industry_nodes"
                    "(id,name,category,country,tier,source_ticker,market_share,description,confidence,source_date) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    [
                        nid,
                        node["name"],
                        cat,
                        node.get("country"),
                        node.get("tier", 0),
                        node.get("ticker"),
                        node.get("market_share"),
                        node.get("role"),
                        1.0,
                        "seed",
                    ],
                )
        for e in chain["edges"]:
            s = nid_by_idx.get(e["s"])
            t = nid_by_idx.get(e["t"])
            if not s or not t:
                continue
            with contextlib.suppress(Exception):
                conn.execute(
                    "INSERT OR IGNORE INTO industry_edges"
                    "(source_node_id,target_node_id,relation,strength,evidence,source_date) "
                    "VALUES(?,?,?,?,?,?)",
                    [s, t, e.get("rel", "supplies"), e.get("strength", 0.8), "seed", "seed"],
                )
