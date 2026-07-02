# Phase 6 — 产业链关系图谱 + 全球分布

> 2026-06-24 | 设计文档

## 目标

每次 astock 分析自动提取上下游关系，持续积累成完整的产业链知识图谱。叠加全球产业地理分布，展示哪些产业集中在哪些国家/地区，以及它们之间的依赖关系。

## 核心理念

```
每次"一键分析"不仅产出报告，还从中提取结构化关系：
  供应商 → 客户 → 竞争对手 → 替代品 → 互补品

多次分析积累后，形成：
  ┌─────────────────────────────────────────┐
  │         全球产业链关系图谱                │
  │                                         │
  │  锂矿(澳洲) ──→ 碳酸锂(中国) ──→ 电池(中日韩) │
  │     │                        ↓            │
  │     └──→ 氢氧化锂(智利)  →  电动车(全球)    │
  │                                         │
  │  芯片设计(美国) → 晶圆(台湾) → 封装(东南亚)  │
  │                                         │
  │  稀土(中国60%) → 永磁材料 → 电机 → 机器人   │
  └─────────────────────────────────────────┘
```

## 数据模型

### 新增表: `industry_nodes`
```sql
CREATE TABLE industry_nodes (
    id TEXT PRIMARY KEY,              -- slug: "lithium-carbonate"
    name TEXT NOT NULL,               -- "碳酸锂"
    category TEXT NOT NULL,           -- raw_material / component / product / service / technology
    country TEXT,                     -- ISO 3166-1 alpha-2: "CN", "AU", "US"
    region TEXT,                      -- "华东", "South America"
    lat REAL,                         -- optional geo-coord for map rendering
    lng REAL,
    description TEXT,                 -- from analysis
    market_share REAL,                -- % of global production (if known)
    concentration TEXT,               -- "high" / "medium" / "low" (supply concentration risk)
    tier INTEGER DEFAULT 0,           -- 0=raw, 1=processed, 2=component, 3=end-product
    source_ticker TEXT,               -- which stock analysis discovered this
    source_date TEXT,                 -- when
    confidence REAL DEFAULT 0.5,      -- 0-1, number of times confirmed across analyses
    created_at INTEGER DEFAULT (strftime('%s','now'))
);
```

### 新增表: `industry_edges`
```sql
CREATE TABLE industry_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_node_id TEXT NOT NULL REFERENCES industry_nodes(id),
    target_node_id TEXT NOT NULL REFERENCES industry_nodes(id),
    relation TEXT NOT NULL,           -- supplies / depends_on / competes_with / substitutes / complements
    strength REAL DEFAULT 0.5,        -- confidence accumulated across analyses
    evidence TEXT,                    -- excerpt from analysis report
    source_ticker TEXT,               -- which analysis produced this
    source_date TEXT,
    created_at INTEGER DEFAULT (strftime('%s','now')),
    UNIQUE(source_node_id, target_node_id, relation)
);
```

### 新增表: `geo_regions`
```sql
CREATE TABLE geo_regions (
    country_code TEXT PRIMARY KEY,    -- ISO 3166-1 alpha-2
    country_name TEXT NOT NULL,
    continent TEXT,
    key_industries TEXT,              -- JSON array of top industry categories
    industrial_policy_summary TEXT,   -- known subsidies, restrictions, trade policies
    updated_at INTEGER
);
```

## 提取管道

### Step 1: 分析完成后自动提取

`analysis_engine.py` 在 astock `propagate()` 完成后调用新模块 `industry_extractor.py`：

```python
async def extract_industry_relations(report: dict, ticker: str) -> list[dict]:
    """Parse supply chain relationships from astock analyst reports.
    
    Uses the 7 analyst reports to find mentions of:
    - Suppliers (上游供应商)
    - Customers (下游客户)  
    - Competitors (竞争对手)
    - Substitute products (替代品)
    - Complementary products (互补品)
    
    Returns list of {source_node, target_node, relation, evidence} dicts.
    """
```

### Step 2: LLM 辅助提取

将分析师研报中的供应链描述喂给轻量 LLM（如 minimax-m2.7-fast），用 structured output 提取结构化关系：

```python
prompt = """
从以下分析报告中提取产业链上下游关系，返回 JSON：

{
  "relations": [
    {
      "from": "节点名称",
      "from_category": "raw_material|component|product|service",
      "from_country": "国家代码",
      "to": "节点名称",
      "to_category": "...",
      "to_country": "国家代码",
      "relation": "supplies|depends_on|competes_with",
      "evidence": "原文引用"
    }
  ]
}

如果没提到产业链关系，返回空数组。
"""
```

### Step 3: 持久化 + 去重合并

- 新节点：INSERT OR IGNORE（按 name slug）
- 新边：INSERT OR IGNORE（按 source+target+relation 唯一键）
- 已有节点/边：`confidence += 0.1 * strength`，`evidence` 追加

## API 端点

```
GET  /api/industry/graph
  ?ticker=600519          ← 只看某股票相关的链
  &category=battery       ← 只看某品类
  &country=CN             ← 只看某国家
  &depth=2                ← 上下游展开几层
  &min_confidence=0.3     ← 过滤低置信度
  → 返回 nodes + edges（JSON）

GET  /api/industry/geo
  → 按国家聚合的产业分布数据
  → { "CN": {"top_industries": [...], "node_count": 42}, ... }

GET  /api/industry/search
  ?q=锂矿
  → 搜索行业节点

GET  /api/industry/ticker/{ticker}/chain
  → 某股票完整的上下游链路（供应商 → 客户 → 终端）
```

## 前端可视化

### 主视图：力导向图

使用 ECharts graph/force 或 D3.js force layout：

```
┌──────────────────────────────────────────────────┐
│ 产业链图谱                            [搜索] [筛选] │
│                                                  │
│     ● 锂矿(澳洲) ──→ ● 碳酸锂(中国)               │
│       │                │                         │
│       │                ↓                         │
│       └──→ ● 氢氧化锂 ──→ ● 电池 ──→ ● 电动车     │
│                                          │        │
│                  ● 芯片(美国) ──→ ● 封装 ←┘        │
│                                                  │
│  图例: ●原料 ●组件 ●成品 ●服务   点击节点查看详情     │
└──────────────────────────────────────────────────┘
```

- 节点大小 = 产业链地位（tier）或 confidence
- 连线粗细 = 关系强度
- 颜色 = 国家/地区
- 点击节点 → 侧面板显示详情（描述、相关股票、历史分析记录）

### 全球分布视图

世界地图热力图，标注各国/地区的主要产业集中度：

- 使用 ECharts map 或 leaflet
- 国家颜色深浅 = 该产业节点数量
- 悬停显示：国家名、主导产业列表、依赖关系

### 与现有分析面板的集成

在分析报告底部增加"产业链关系"卡片：
```
┌─────────────────────────────┐
│ 🔗 本次分析新增 3 条产业链关系  │
│                             │
│ + 碳酸锂 → 电池 (供应)        │
│ + 电池 → 新能源车 (配套)       │
│ + 锂矿 → 碳酸锂 (上游原料)     │
│                             │
│ [查看完整产业链图谱]           │
└─────────────────────────────┘
```

## 种子数据（预置）

内置一份全球主要产业分布参考数据，作为图谱的起点：

| 国家 | 关键产业 |
|------|----------|
| 🇨🇳 中国 | 稀土(60%)、光伏(80%)、锂电池(70%)、电动车(60%) |
| 🇹🇼 台湾 | 先进制程芯片(90%)、封装测试 |
| 🇰🇷 韩国 | 存储芯片、显示面板、造船 |
| 🇯🇵 日本 | 半导体材料、精密设备、汽车 |
| 🇺🇸 美国 | 芯片设计(EDA/IP)、航空航天、制药 |
| 🇩🇪 德国 | 汽车、工业自动化、化工 |
| 🇦🇺 澳大利亚 | 锂矿(50%)、铁矿石 |
| 🇨🇱 智利 | 锂矿、铜矿 |
| 🇸🇦 沙特 | 石油、石化 |
| 🇮🇳 印度 | 仿制药、IT服务、稀土替代 |
| 🇻🇳 越南 | 电子组装、纺织 |
| 🇲🇾 马来西亚 | 芯片封装测试、棕榈油 |

## 实现任务

### Task P6-1: 数据库 + 模型
- 创建 `industry_nodes`, `industry_edges`, `geo_regions` 三张表
- 种子数据插入（全球产业分布）
- Pydantic 模型

### Task P6-2: 关系提取器
- `server/industry_extractor.py`（新）
- LLM 辅助从分析报告提取结构化关系
- 去重合并逻辑

### Task P6-3: API 端点
- `server/industry_router.py`（新）
- 图谱查询（支持过滤/展开/搜索）
- 地理聚合

### Task P6-4: 前端图谱可视化
- ECharts force graph 渲染
- 节点点击详情面板
- 搜索 + 筛选控件

### Task P6-5: 全球分布地图
- ECharts world map 热力图
- 悬停信息卡片
- 与分析报告联动

### Task P6-6: 分析报告集成
- 分析完成后自动提取
- 报告底部显示新增关系卡片
- "查看完整图谱"跳转

## 不在范围

- 实时供应链监控/预警
- 贸易政策影响量化
- 产业链韧性/脆弱性评分
- 多语言搜索
