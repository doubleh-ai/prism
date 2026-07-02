# Phase 2 — astock 完整集成设计

> 2026-06-24 | 基于 TradingAgents-astock v0.2.15 架构分析

## 目标

把 TradingAgents-astock 的 7-agent 辩论决策流程完整嵌入 Prism，替换当前"把文本发给 LLM 自由发挥"的空壳实现。

---

## 架构概览

```
Prism 前端                     Prism 后端                        astock 引擎
─────────                     ─────────                        ──────────
点击"一键分析"          →   POST /api/analysis/start    →   TradingAgentsGraph
SSE 流式接收进度        ←   /api/analysis/stream        ←   graph.stream()
分析面板渲染报告        ←   /api/analysis/{id}/report   ←   结构化 JSON 输出
```

### 调用方式

astock 支持直接 import，不需要 subprocess：

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

config = DEFAULT_CONFIG.copy()
config["data_vendors"] = {"core_stock_apis": "a_stock", ...}
config["quick_think_llm"] = "deepseek-v4-pro"  # 复用 Prism 的 LLM 配置
config["deep_think_llm"] = "deepseek-v4-pro"

ta = TradingAgentsGraph(debug=False, config=config)
final_state, decision = ta.propagate("600519", "2026-06-24")
# decision: "Buy" | "Overweight" | "Hold" | "Underweight" | "Sell"
```

### 7 个 Agent 角色

| Agent | 职责 | A 股特有 |
|-------|------|----------|
| 市场分析师 | 技术面（OHLCV、指标） | |
| 舆情分析师 | 新闻/社交媒体情绪 | |
| 新闻分析师 | 新闻 + 内部交易 | |
| 基本面分析师 | 财报、预测、行业对比 | |
| 政策分析师 | 监管/产业政策影响 | ✅ |
| 游资追踪师 | 龙虎榜/北向资金/主力动向 | ✅ |
| 解禁监控师 | 限售解禁/大股东减持/质押 | ✅ |

### 决策流程

```
7 Analyst 研报 → Bull vs Bear 辩论 → Research Manager 研判
→ Trader 交易方案 → Aggressive/Conservative/Neutral 风险辩论
→ Portfolio Manager 最终决策（Buy/Hold/Sell + 仓位 + 目标价）
```

---

## 实现任务

### Task P2-1: 依赖安装 + 配置

**文件:** `requirements-extra.txt`, `server/config.py`

- 安装 astock 及依赖：
  ```
  -e /home/develop/projects/TradingAgents-astock
  ```
  （或 pip install tradingagents-astock）
- 新增配置项：
  ```python
  ASTOCK_LLM_QUICK = os.environ.get("ASTOCK_LLM_QUICK", "deepseek-v4-pro")
  ASTOCK_LLM_DEEP = os.environ.get("ASTOCK_LLM_DEEP", "deepseek-v4-pro")
  ASTOCK_DATA_VENDOR = os.environ.get("ASTOCK_DATA_VENDOR", "a_stock")
  ASTOCK_MAX_DEBATE_ROUNDS = int(os.environ.get("ASTOCK_MAX_DEBATE_ROUNDS", "1"))
  ```

### Task P2-2: 分析 API 端点

**文件:** `server/analysis_engine.py`（新）, `server/main.py`

新增 3 个端点：

```
POST /api/analysis/start
  body: { ticker, name? }
  → 后台启动 LangGraph 分析流
  → 返回 { analysis_id }

GET  /api/analysis/{id}/stream (SSE)
  → 流式推送分析进度：
    { "type": "analyst_start",  "analyst": "市场分析师" }
    { "type": "tool_call",      "tool": "get_stock_quotes" }
    { "type": "analyst_report", "analyst": "市场分析师", "content": "..." }
    { "type": "debate_round",   "round": 1, "bull/bear": "..." }
    { "type": "final_decision", "rating": "Buy", "report": "..." }
    { "type": "done" }

GET  /api/analysis/{id}/report
  → 返回完整结构化报告 JSON
```

### Task P2-3: 分析引擎封装

**文件:** `server/analysis_engine.py`（新）

```python
class AnalysisEngine:
    """封装 astock TradingAgentsGraph，提供异步接口"""

    async def start_analysis(self, ticker: str, name: str, date: str) -> str:
        """启动分析，返回 analysis_id"""
        config = self._build_config()
        graph = TradingAgentsGraph(debug=True, config=config)
        # 在 executor 线程中运行同步 LangGraph
        loop = asyncio.get_event_loop()
        self._tasks[analysis_id] = loop.run_in_executor(
            None, self._run_graph, analysis_id, graph, ticker, date
        )
        return analysis_id

    def _run_graph(self, aid, graph, ticker, date):
        """同步运行 graph.stream()，把事件推入队列"""
        for event in graph.stream(ticker, date):
            self._queues[aid].put(event)
        self._queues[aid].put({"type": "done"})

    async def stream_events(self, analysis_id: str):
        """Async generator yielding SSE events"""
        while True:
            event = await self._queues[aid].get()
            yield self._format_sse(event)
            if event["type"] == "done":
                break
```

### Task P2-4: 前端分析面板

**文件:** `static/js/app.analysis.js`（新）, `templates/app.html`, `static/css/app.css`

分析面板从空态文字改为实时进度展示：

```
┌─────────────────────────────────────────┐
│  🔍 正在分析 三柏硕 (001300)            │
│                                         │
│  ✅ 市场分析师 — 已完成                  │
│  ✅ 基本面分析师 — 已完成                │
│  🔄 政策分析师 — 分析中…                 │
│  ⏳ 游资追踪师                           │
│  ⏳ 解禁监控师                           │
│                                         │
│  ── 辩论阶段 ──                         │
│  🔄 Bull vs Bear 第 1 轮                │
│                                         │
│  [最终报告区 — 实时填充]                 │
└─────────────────────────────────────────┘
```

JS 新增：
- `App._startAnalysis(ticker, name)` → POST `/api/analysis/start`
- `App._streamAnalysis(analysisId)` → SSE 接收进度事件
- `App._renderAnalysisProgress(event)` → 更新分析面板 DOM
- `App._renderFinalReport(report)` → 渲染最终研究报告

### Task P2-5: 报告渲染 + 存档

**文件:** `static/css/app.css`, `static/js/app.analysis.js`

最终报告渲染为结构化卡片：
- **投资评级** 大 badge（Buy 绿色/ Hold 黄色 / Sell 红色）
- **执行摘要** 卡片
- **各分析师研报** 折叠面板（7 个）
- **辩论记录** 折叠面板
- **风险矩阵** 表格
- "添加到对话"按钮（将报告追加到当前对话）
- "导出 PDF"按钮

---

## 依赖评估

| 依赖 | 用途 | 风险 |
|------|------|------|
| `tradingagents-astock` | 核心分析引擎 | 用户自有项目，Apache 2.0 |
| `langgraph>=0.4.8` | 状态图编排 | MIT，Google 维护 |
| `langchain-openai` | LLM 客户端 | MIT |
| `pandas>=2.3` | 数据处理 | BSD |
| `stockstats` | 技术指标 | MIT |
| `mootdx` | 行情 TCP 数据 | MIT |

全部通过 pip 安装，无需外部服务。

---

## 验收标准

- [ ] 选股 → 点"一键分析" → 分析面板显示实时进度（7 个 agent 逐个完成）
- [ ] Bull/Bear 辩论阶段可见
- [ ] 最终报告含投资评级（Buy/Hold/Sell）、目标价、执行摘要
- [ ] 报告可展开查看各分析师研报
- [ ] 分析过程中可切换回对话模式，不影响分析运行
- [ ] 分析完成后自动保存到分析历史
- [ ] 268+ 测试全部通过
- [ ] 不引入破坏性变更（现有对话/行情功能不受影响）

---

## 不在范围

- 回测 / 模拟交易
- 多股票对比分析
- 自定义分析师角色
- 分析参数调优 UI（先用默认配置）
- PDF 导出（后续迭代）
