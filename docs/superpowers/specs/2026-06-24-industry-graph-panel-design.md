# 产业链图谱板块 — 设计文档

> 日期: 2026-06-24 · 状态: 设计已确认（方案 A 分层卡片列），待评审

---

## 1. 背景与问题

Prism 现有"分析关系图"是 `#graphModal` 弹窗，调 `/api/convs/{id}/graph` 把 Claude 推理过程的原始节点（`question`/`tool_call`/`think`/`analysis`/`reference`）画成力导向图 —— 用户看到的是"一大堆看不懂的节点"，并非产业链。

Phase 6 后端（`/api/industry/*` + `industry_nodes`/`industry_edges`/`geo_regions` 三表 + 12 国地理种子）早已写好，但**前端零接入**，4 个 API 全闲置，节点/边表基本为空。

## 2. 目标

把"产业链关系图 + 地图"做成**第 4 个一级板块**（与 对话/行情/分析 并列），做到：

1. **看得懂**：链路按 上游→中游→下游 分层呈现，不再是力导向一坨。
2. **有内容**：人工种子产业链开箱即用，不依赖分析先跑通。
3. **能钻取**：地图总览 → 点产业/国家/个股 → 看具体链路 → 点公司看详情。
4. **会沉淀**：astock 分析跑通后，自动把涉及的公司/关系累积进共享图谱。

## 3. 范围

**纳入：**
- 第 4 个一级 mode `industry` + icon-rail 入口。
- 地图视图（全球产业格局）+ 链路视图（分层卡片列）+ 详情抽屉。
- 8-10 条 A 股核心产业链种子数据 + 地理经纬度补全。
- 后端 API 扩展（按 tier 分组、categories 列表、节点详情）。
- 分析完成后抽取→沉淀（Phase 3，依赖 astock 可跑）。

**不纳入（YAGNI）：**
- 手动编辑/新增图谱节点。
- 多股票对比图。
- 卡片实时行情（改为点击跳转到「行情」面板查）。
- 分钟级数据、产业链预警通知。

## 4. 信息架构

### 4.1 导航

```
icon-rail:  对话 | 行情 | 分析 | 图谱(🌐/🔗)
```

- 新 mode `industry`，复用 `App.switchMode(mode)` 机制，与 market/analysis 一致切主区。
- 离开时销毁 ECharts 实例释放内存。

### 4.2 侧栏（`#industrySidebar`，复用现有 sidebar 风格）

```
🔍 搜索公司/产业…
─────────────────────
产业链
  🔋 锂电池        (12)
  ☀️ 光伏           (9)
  💾 半导体         (15)
  ⛏ 稀土           (7)
  🚗 电动车         (10)
  💊 创新药         (8)
  🍶 白酒           (6)
  🎖 军工           (9)
  📱 消费电子       (11)
  🏗 钢铁           (8)
─────────────────────
自选股（点开直跳该股链路）
  · 300750 宁德时代
  · 600519 贵州茅台
─────────────────────
面包屑：地图 / 🔋 锂电池
```

## 5. 视图设计

### 5.1 地图视图（默认总览）

- **渲染**：ECharts `geo` + `scatter`。世界地图为底（需注册 `world.json` geoJSON），每国一个气泡。
- **气泡编码**：
  - 大小 = 该国产业节点数。
  - 颜色 = 该国主导产业 category（与侧栏配色一致）。
- **交互**：
  - hover → tooltip：国名 + key 产业 + 节点数。
  - 点国家 → 侧栏过滤到该国涉及的产业链。
  - 点侧栏产业链条目 / 自选股 → 切到链路视图。
- **顶部切换**：`〔地图〕〔链路〕` + 链路视图带「← 返回地图」。

### 5.2 链路视图（钻取，方案 A 分层卡片列）—— 核心

**结构**：主区横向三列 `上游 ｜ 中游 ｜ 下游`，每列垂直堆叠公司卡片，SVG 连线表达供需关系（多对多）。卡片可点 → 详情抽屉。

```
链路视图：🔋 锂电池                       〔← 返回地图〕
┌──────────────┬──────────────┬──────────────┐
│  ⛏ 上游       │  🔧 中游       │  🚗 下游       │
│  (原材料)     │  (制造)       │  (应用)       │
├──────────────┼──────────────┼──────────────┤
│ ┌──────────┐ │ ┌──────────┐ │ ┌──────────┐ │
│ │赣锋锂业   │─│ │当升科技   │─│ │宁德时代   │ │
│ │002460     │ │ │正极·18%   │ │ │电池·37%   │ │
│ └──────────┘ │ └──────────┘ │ └──────────┘ │
│ ┌──────────┐ │ ┌──────────┐ │ ┌──────────┐ │
│ │天齐锂业   │─│ │恩捷股份   │─│ │比亚迪     │ │
│ │002466     │ │ │隔膜       │ │ │电池+整车  │ │
│ └──────────┘ │ └──────────┘ │ └──────────┘ │
└──────────────┴──────────────┴──────────────┘
        ↓ 点「宁德时代」卡片
┌────────────────────────────────────────────┐
│ 详情抽屉（右侧滑入）                          │
│ 宁德时代 300750 · 电池制造 · 🇨🇳             │
│ 全球动力电池市占 37%（第一）                  │
│ 上游：当升(正极) 恩捷(隔膜) 天齐(锂矿)        │
│ 下游：理想 蔚来 特斯拉                        │
│ 〔查看 astock 分析报告 →〕〔去行情面板 →〕     │
└────────────────────────────────────────────┘
```

**卡片内容**：公司名 + 代码 + 角色（正极/隔膜/电池…）+ 市占率（若有）+ 国旗（若非中国）。
**连线**：SVG `<path>`，贝塞尔曲线，方向上游→下游；hover 高亮该节点所有连边。
**单股钻取**：从自选股/搜索进入时，高亮该股所在路径，其余节点降透明度。
**布局**：三列等宽 flex，卡片纵向可滚动；窄屏（<900px）退化为单列 + tier 标签。

### 5.3 详情抽屉

右侧滑入面板，展示选中节点：
- 基本信息：名称、代码、category、tier、国家、市占、集中度。
- 关系：上游依赖列表、下游客户列表（点击可跳转）。
- 来源：`source_ticker` + `confidence` + `source_date`（种子节点标"种子数据"）。
- 动作：〔查看该股最近 astock 分析报告〕（跳分析面板）、〔去行情面板〕（`switchMode('market')` + 选股）。

## 6. 数据模型（复用现有表）

### 6.1 表结构（已存在，无需改 schema）

- `industry_nodes(id, name, category, country, region, lat, lng, description, market_share, concentration, tier, source_ticker, source_date, confidence, created_at)`
- `industry_edges(id, source_node_id, target_node_id, relation, strength, evidence, source_ticker, source_date, created_at)`
- `geo_regions(country_code, country_name, continent, key_industries, industrial_policy_summary, updated_at)`

**字段约定**：
- `category` = 产业链名（`锂电池`/`光伏`…），即侧栏分组键。
- `tier`：`0`=上游 / `1`=中游 / `2`=下游。
- `relation`：`supplies`（A 供 B）/ `competes_with`（竞争）/ `subsidiary`（从属）。
- `confidence`：种子数据 `1.0`；分析沉淀 `0.3-0.8`。
- `source_ticker`：种子为空；分析沉淀填触发分析的 ticker。

### 6.2 种子数据（人工整理，开箱即用）

10 条 A 股核心链，每条含真实公司节点 + 供需边：
锂电池 · 光伏 · 半导体 · 稀土 · 电动车 · 创新药 · 白酒 · 军工 · 消费电子 · 钢铁。

每条链 6-15 个节点，覆盖上/中/下游。同时为 `geo_regions` **新增 `lat`/`lng` 两列**（国家中心点经纬度），供地图按国家聚合定位气泡（`industry_nodes` 已有 lat/lng 字段，留给未来按节点定位用，本期不用）。

### 6.3 分析沉淀（Phase 3，依赖 astock 可跑）

在 `analysis_engine._run()` 分析成功（`_emit("done")`）后加一步 `_ingest_to_industry_graph(final_state, ticker)`：

1. 用 LLM（`ASTOCK_LLM_QUICK`）从 `final_state` 的各 analyst report + final_decision 抽取：提到的公司（含代码）、它们之间的供需关系。
2. 结构化为 nodes/edges，`upsert` 进 `industry_nodes`/`industry_edges`（按 `id`=`_slug(name)` 去重，已存在则合并/提升 confidence）。
3. 标 `source_ticker`/`source_date`/`confidence=0.5`。
4. 失败静默（不影响分析结果返回）。

> 注意：此阶段依赖 astock 分析能跑通（DEEPSEEK_API_KEY 已就位后）。可独立后置上线。

## 7. 后端 API（industry_router.py 扩展）

| 端点 | 现状 | 改动 |
|---|---|---|
| `GET /api/industry/categories` | **新增** | 返回 `[{category, node_count, edge_count}]`，供侧栏 |
| `GET /api/industry/graph?category=&country=&ticker=&q=` | 已有 | 返回增加 `tier` 分组结构（或前端按 `tier` 分组） |
| `GET /api/industry/geo` | 已有 | 返回补 `lat`/`lng`，带每国节点数 |
| `GET /api/industry/search?q=` | 已有 | 不变 |
| `GET /api/industry/ticker/{ticker}/chain` | 已有 | 不变（单股钻取用） |
| `GET /api/industry/node/{id}` | **新增** | 节点详情 + 上下游邻居（详情抽屉用） |
| `POST /api/industry/seed` | **新增**（管理员） | 重新装载种子数据（维护用） |

所有端点 `require_auth`。

## 8. 前端文件

| 文件 | 动作 | 说明 |
|---|---|---|
| `static/js/app.industry.js` | **新建** | mode 初始化、地图渲染、链路分层卡片、详情抽屉、搜索/侧栏联动、ECharts 生命周期 |
| `templates/app.html` | 改 | icon-rail 加「图谱」按钮 + `#industryPanel` 主容器 + `#industrySidebar` |
| `static/css/app.css` | 改 | `.ind-chain-cols`、`.ind-card`、`.ind-edge`(svg)、`.ind-drawer`、地图气泡 tooltip |
| `static/lib/world.json` | **新增** | ECharts 世界地图 geoJSON（~100KB，本地离线） |
| `static/js/app.core.js` | 改 | `switchMode` 增加 `industry` 分支；销毁/重建 ECharts |
| `server/industry_router.py` | 改 | 加 categories / node 详情 / seed 端点 |
| `server/database.py` | 改 | 种子产业链数据 + geo 经纬度；`init_db` 幂等装载 |
| `server/analysis_engine.py` | 改（Phase 3） | 分析完成后 `_ingest_to_industry_graph` |

`app.html` 静态资源版本号 `?v=` 递增，确保 SW 缓存更新。

## 9. 状态处理

- **加载中**：骨架屏（复用 `.shimmer-bar`）。
- **空态**：
  - 某产业链无种子 → 链路视图"暂无该产业链数据"。
  - 全表空（新部署未跑 seed）→ 地图"暂无产业数据，请运行种子装载"。
- **分析沉淀未就位**：地图/链路正常显示种子；沉淀数据带"分析来源"标记与种子区分。
- **ECharts 未加载 / world.json 缺失** → 地图视图降级为"地图资源加载失败"，链路视图（纯 DOM）不受影响。

## 10. 构建顺序（分阶段交付）

1. **Phase 1 — 地基 + 地图（可独立验收）**
   - icon-rail + mode + panel 骨架 + 侧栏。
   - 种子数据 + `categories`/`geo` 端点。
   - 地图视图（气泡 + tooltip + 点国家过滤）。
2. **Phase 2 — 链路 + 钻取**
   - 链路分层卡片列 + SVG 连线 + 详情抽屉。
   - 单股钻取（高亮路径）+ 跨面板跳转（行情/分析）。
3. **Phase 3 — 分析沉淀**
   - `_ingest_to_industry_graph` + seed 刷新端点。（依赖 astock 可跑）

## 11. 验证

- **后端单测**：种子装载幂等、`categories`/`node/{id}` 端点、tier 分组正确、geo 经纬度非空。
- **前端**：无 headless 浏览器（dev 环境限制），用静态审查 + pytest + 用户浏览器验收。
- **手动验收路径**：
  1. 点 icon-rail「图谱」→ 地图显示，气泡大小/颜色合理。
  2. 点侧栏「锂电池」→ 切链路视图，三列卡片 + 连线，结构清晰。
  3. 点「宁德时代」卡片 → 抽屉显示上下游 + 跳转按钮。
  4. 点自选股 → 高亮该股路径。
  5. （Phase 3）跑一次 astock 分析 → 图谱多出带"分析来源"标记的节点。

## 12. 风险与对策

| 风险 | 对策 |
|---|---|
| 种子数据手工量大/易错 | 先做 3 条核心链（锂电/光伏/半导体）跑通全链路，再补齐其余 |
| 分析沉淀 LLM 抽取有幻觉 | confidence≤0.6 + 必须有 ticker 才入库 + 详情标注来源 |
| world.json 体积 | 仅 ~100KB，本地托管，不走 CDN |
| ECharts 内存泄漏 | 切 mode 时 `chart.dispose()` |
