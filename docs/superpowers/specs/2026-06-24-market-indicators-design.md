# 行情栏 · LLM 指标翻译 + 个人指标库 — 设计文档

> 日期: 2026-06-24 · 状态: 设计已确认（DSL/公式树 + 技术+基本面数据）

## 1. 目标

用户用任意方式（自然语言/伪公式/Pine-like/标准名）描述一个指标，LLM 翻译成**可执行 DSL**，pandas 安全求值。每用户有独立指标库，可：叠加到 K 线可视化 / 选股扫描 / 信号回测。

## 2. 三层架构

```
任意输入 → [LLM 翻译] → DSL 表达式树(JSON) → [校验+预览] → 个人指标库 → [pandas 求值] → 可视化/选股/信号
```

LLM 解决"任意输入"，DSL 解决"可执行+安全"，库解决"复用"。

## 3. DSL 设计（安全核心）

表达式树 = 叶子(基指标) + 内部节点(操作符)，白名单递归求值，**无 eval/exec**。

- **基指标（叶子）**：
  - 技术面：`price(o|h|l|c)` `ma(n)` `ema(n)` `macd(f,s,sig)` `rsi(n)` `kdj(n)` `boll(n,k)` `vol` `ma_vol(n)` `turnover`
  - 基本面：`pe` `pb` `roe` `revenue_growth` `market_cap` `dividend_yield`
- **操作符**：算术 `add/sub/mul/div` · 比较 `gt/lt/ge/le/eq` · 时序 `cross_up/cross_down/highest(n)/lowest(n)/ref(n)` · 逻辑 `and/or/not`
- **`kind`**：`plot`（线/带，画图）或 `signal`（布尔序列，触发/筛选）

DSL 示例 — "5日线上穿20日线 且 量>5日均量2倍"：
```json
{"and":[
  {"cross_up":[{"ma":5},{"ma":20}]},
  {"gt":[{"vol"},{"mul":[2,{"ma_vol":5}]}]}
]}
```

## 4. 翻译层

- `POST /api/indicators/translate {input}` → quick LLM (JSON mode) → `{name, dsl, kind, description}`。
- few-shot 覆盖标准名/伪公式/中文NL。
- 翻译后**即时预览**（当前股 K 线试算画出），用户确认再存库 —— 人眼校验解决翻译不准。
- 低置信/失败 → 用户可手动编辑 DSL。

## 5. 执行层（pandas，安全）

- `_eval_dsl(dsl, df_ohlcv, fundamentals)`：递归求值，白名单节点，未知 → 报错。
- 基指标用 stockstats（新依赖）或纯 pandas 实现。
- 返回：plot → pd.Series；signal → 布尔 Series + 触发索引列表。

## 6. 个人指标库（per-user）

- 新表 `indicators(id, username, name, description, dsl_json, kind, created_at, updated_at)`。
- API：`GET/POST /api/indicators`、`PUT/DELETE /api/indicators/{id}`、`POST /api/indicators/translate`、`POST /api/indicators/{id}/eval?ticker=`、`POST /api/indicators/{id}/scan?scope=watchlist|market`。全部 `require_auth` + username 隔离。
- 内置种子指标 4-5 个（金叉/死叉/超卖反弹/放量突破）。

## 7. 三用途（分期）

| 期 | 内容 |
|---|---|
| **P1 可视化** | 翻译+预览+求值 → K线叠加（主图线/带、副图振荡、触发箭头）+ 库CRUD |
| **P2 选股扫描** | signal DSL 批量执行 watchlist/market → 满足的票+触发日 |
| **P3 信号/回测** | 单股历史触发点 + 持有N日收益→胜率；预警(定时扫,可选) |

## 8. 文件

| 文件 | 动作 |
|---|---|
| `server/indicators/dsl.py` | **新建** 求值引擎（白名单） |
| `server/indicators/translator.py` | **新建** LLM 翻译 + schema |
| `server/indicators/router.py` | **新建** API |
| `server/indicators/models.py` | **新建** Pydantic |
| `server/database.py` | 新表 `indicators` + 种子 |
| `server/main.py` | 注册 router |
| `static/js/app.indicators.js` | **新建** 翻译/预览/库/K线叠加/选股 |
| `static/js/app.market.js` | K线动态 series + 信号标记 |
| `templates/app.html` | 行情侧栏"指标"区 + 翻译框 |
| `static/css/app.css` | 指标库/翻译/预览样式 |
| `requirements.txt` | 加 `stockstats` |

## 9. 安全

DSL 白名单求值（无 eval/exec）· LLM 输出 schema 校验非法即拒 · 选股速率/并发限制 · 库 per-user 隔离。

## 10. 风险

翻译准确率 → few-shot + 即时预览 + 手动编辑。形态识别不在本期。全市场扫描慢 → 限范围/异步。

## 11. 验证

- 后端单测：DSL 求值正确性（金叉/放量示例）、白名单拒绝未知节点、translator schema、库 CRUD + 隔离、scan 限流。
- pytest 全过；前端静态审查 + 用户浏览器验收。
