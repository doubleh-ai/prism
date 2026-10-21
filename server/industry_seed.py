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
