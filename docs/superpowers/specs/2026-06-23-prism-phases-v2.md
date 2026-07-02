# Prism 后续 Phase 总览

> 更新: 2026-06-23 | Phase 1 ✅ 完成 (268 tests)

---

## ✅ Phase 1 — 后端低风险改造（已完成）

| # | 项目 | 状态 |
|---|------|------|
| 1 | LRU 缓存（proxy 1K / term 256） | ✅ |
| 2 | explain-term 限流（10/min） | ✅ |
| 3 | SQLite PRAGMAs + thread-local 连接复用 + autocommit | ✅ |
| 4 | 连接复用测试 | ✅ |
| 5 | aiofiles 异步 jsonl 读 | ✅ |
| 6 | tencent provider asyncio.to_thread | ✅ |
| 7 | GZipMiddleware + StaticCacheControlMiddleware | ✅ |
| + | 安全修复：X-Request-ID 校验、analysis_graph 权限校验 | ✅ |
| + | 渲染修复：Markdown `**` 加粗、HTML 残留符号 `">` | ✅ |
| + | UX：搜索栏对齐、搜索缓存预热、一键分析按钮重设计、自动发送 | ✅ |

---

## 📋 Phase 2 — astock 完整集成（真正的一键分析）

**目标:** 把 TradingAgents-astock 的 7-agent 辩论决策流程完整嵌入 Prism。

**设计文档:** `docs/superpowers/specs/2026-06-24-prism-p2-astock-integration.md`

### P2-1: 依赖安装 + 配置
- 安装 `tradingagents-astock` + 依赖（langgraph, pandas, stockstats, mootdx）
- 新增 astock LLM/数据源配置项

### P2-2: 分析 API 端点
- `POST /api/analysis/start` — 启动 LangGraph 分析
- `GET /api/analysis/{id}/stream` — SSE 流式推送进度
- `GET /api/analysis/{id}/report` — 返回结构化报告 JSON

### P2-3: 分析引擎封装
- `server/analysis_engine.py` — 封装 TradingAgentsGraph
- 异步队列将同步 LangGraph.stream() 转为 SSE 事件
- 支持多分析并发

### P2-4: 前端分析面板
- 实时显示 7 个 agent 进度
- Bull/Bear 辩论阶段可视化
- 最终报告卡片渲染（评级 badge + 摘要 + 各研报折叠面板）

### P2-5: 报告渲染 + 存档
- 结构化报告卡片 UI
- "添加到对话" / 分析历史

### P2 验收
- 选股 → 点"一键分析" → 分析面板显示实时进度
- 7 个 agent 逐个完成，辩论阶段可见
- 最终输出投资评级（Buy/Hold/Sell）+ 目标价 + 摘要
- 分析过程中可切回对话模式

---

## 📋 Phase 3 — 前端流式渲染优化

| # | 项目 | 说明 |
|---|------|------|
| P3-1 | `renderOne` 增量更新 | 当前每帧全量 innerHTML，改为只更新变化 chunk |
| P3-2 | 虚拟滚动 | 50+ 条消息时只渲染可视区 |
| P3-3 | 消息折叠 | 长对话自动折叠旧消息（可展开） |
| P3-4 | Stream chunkSig 优化 | 大文本下 JSON.stringify 性能差，改为 hash |

---

## 📋 Phase 4 — 行情面板 + DOM 优化

| # | 项目 | 说明 |
|---|------|------|
| P4-1 | Quote chip 批量查询 | 多个 `[[quote:XXX]]` 一次 API 拉取 |
| P4-2 | ECharts dispose 修复 | 切换股票时销毁旧实例，避免内存泄漏 |
| P4-3 | 自选刷新节流 | 活跃 30s，后台 120s 指数退避 |
| P4-4 | K 线周期扩展 | 支持周线/月线（当前仅日线） |

---

## 📋 Phase 5 — 离线 + PWA

| # | 项目 | 说明 |
|---|------|------|
| P5-1 | Service Worker 缓存 | 静态资源离线可用，二次加载 < 50ms |
| P5-2 | IndexedDB 对话缓存 | 切换对话优先本地读取 |
| P5-3 | SSE 断线重连 | fetch+getReader 自动重连 + 指数退避 |

---

## 🐛 待修 Bug（可穿插在任意 Phase）

| # | 问题 | 优先级 |
|---|------|--------|
| B1 | Markdown `**加粗**` 渲染异常 | ✅ 已修复 |
| B2 | HTML 残留符号 `">` | ✅ 已修复 |
| B3 | `persist_graph` DELETE+INSERT 非原子 | 低 |
| B4 | conversations.py 同步 jsonl 与 chat.py async 版逻辑漂移 | 低 |

---

## 📊 总览

| Phase | 主题 | 预估工作量 | 依赖 |
|-------|------|-----------|------|
| P1 | 后端优化 | ✅ 完成 | — |
| **P2** | **分析引擎（astock 集成）** | **5 tasks** | P1 |
| P3 | 前端渲染优化 | 4 tasks | P1 |
| P4 | 行情面板优化 | 4 tasks | P1 |
| P5 | 离线 + PWA | 3 tasks | P3, P4 |

**建议顺序:** P2 → P3 → P4 → P5

P2 优先因为它是用户直接感知的核心功能（"一键分析"从空壳变成真正的分析引擎），其他 Phase 是性能和质量优化。
