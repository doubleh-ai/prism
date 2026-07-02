# 分析面板重构 — 设计文档（全宽仪表盘）

> 日期: 2026-06-24 · 状态: 设计已确认

## 1. 问题

当前 `#analysisPanel` 报告区是单列 `.ar-card` 堆 Markdown 文本：信号/决策/7分析师/多空/交易/风险全是文字块，无视觉层级、无指标、主区被 sidebar 挤窄。用户反馈"low/混乱/全文本/窄"。

## 2. 目标

把分析结果做成**全宽仪表盘**：进 analysis mode 自动折叠侧栏，主区满宽；HERO 信号 + 指标卡 + 分析师网格 + 多空双栏 + 交易/风险 + 最终决策。内容**结构化呈现**（指标卡、verdict chip、bullet 摘要），不再全是文本。

## 3. 数据：后置 LLM 抽取

astock 报告全是自由文本。分析成功后加 `_extract_structured(report, ticker, name)`：
- quick LLM (JSON mode)，输入 final_decision + 7 analyst reports + 多空辩论 + 交易方案 + 风险。
- 输出 schema：
  ```json
  {
    "signal": "买入|持有|卖出|观望",
    "confidence": 0-100,
    "thesis": "1-2句核心论点",
    "metrics": {"valuation":"高|中|低|合理","growth":"...","risk_score":0-10,"momentum":"..."},
    "trade": {"target_price":"...","stop_loss":"...","position":"...","horizon":"...","rationale":"..."},
    "analyst_digests": [{"agent":"market","verdict":"多|空|中","bullets":["...","..."]}, ...7],
    "bull_bear": {"bull":"...","bear":"...","judge":"偏多|偏空|中性"},
    "verdict_summary": "最终结论摘要"
  }
  ```
- 严格 schema 校验；失败 → `structured=null`，前端降级为现有文本卡片。
- 抽取结果存 `_store[aid]["report"]["structured"]`，`get_report` 一并返回。

## 4. 布局

- `switchMode('analysis')` → `appShell.classList.add('collapsed')`（折叠侧栏满宽）；离开移除。
- 仪表盘网格（满宽）：
  1. **HERO**：左 股票+名称+行业标签；右 大信号徽章（买入绿/持有黄/卖出红）+ 置信度环；下 thesis。
  2. **指标卡行**：估值/增长/风险分/动量，tile + 评级 chip + 图标。
  3. **分析师研报**：7 卡网格，每卡 = 图标 + verdict chip + 2-3 bullet；点开看完整。
  4. **多空辩论**：左右双栏（绿/红）+ 中间裁决。
  5. **交易方案 + 风险三栏**（激进/中性/保守）并排。
  6. **最终决策**：突出卡片。
- 响应式：窄屏退化为单列。

## 5. 文件

| 文件 | 动作 |
|---|---|
| `server/analysis_engine.py` | 加 `_extract_structured()`；结果入 report.structured |
| `static/js/app.analysis.js` | 重写 `_renderFinalReport` 为仪表盘组件；加载 structured |
| `static/css/app.css` | `.ap-hero/.ap-signal-ring/.ap-tiles/.ap-analyst-grid/.ap-debate/.ap-trade/.ap-risk-tri` |
| `static/js/app.core.js` | `switchMode` analysis→collapsed |
| `templates/app.html` | `?v=` 递增 |

## 6. 状态/降级

- 分析中：进度区（已实现）。
- 抽取中：HERO 先出，其余"正在提炼结构化摘要…"骨架。
- 抽取失败/无 structured：降级为现有文本卡片（保底）。

## 7. 分期

- **P1 布局**：满宽 + HERO + 分区卡片 + 多空双栏 + 风险三栏（用现有文本数据）。
- **P2 结构化**：`_extract_structured` + 指标卡 + 分析师摘要 + 交易结构化。

## 8. 验证

- 后端单测：`_extract_structured` schema 校验、降级（mock LLM 失败）。
- pytest 全过；前端静态审查 + 用户浏览器验收（无 headless browser）。
