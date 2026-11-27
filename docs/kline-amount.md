# K 线成交量与成交额口径

取证日期：2026-11-13；代码基线：`c531ec26`。检查对象为 `min_turnover` 的实际输入。

## 调用链

后台 `scanner._run_job` / 同步 `scan_indicator` → `_build_df` → `ProviderChain.get_kline`。
`KLine.turnover` 原样映射成 DataFrame 的 `amount`，再与 `min_turnover` 比较，门槛单位为元。
周/月线使用对应周期内完整日成交额之和，不是最新一天的报价成交额。

## 同日真实返回

TencentProvider 的 K 线实际来自新浪 `CN_MarketData.getKLineData`，并非腾讯报价接口。
2026-11-13 21:46:32（北京时间）抓取沪、深、北三只股票各 10 根日线，全部未提供 amount 字段。
AKShare 1.18.64 的 `stock_zh_a_daily(adjust="")` 于 21:47:16—18 取得同日区间未复权行情。
[AKShare 官方说明](https://github.com/akfamily/akshare/blob/main/docs/data/stock/stock_quote.md#历史行情数据-新浪)规定 volume 为股、amount 为元；其 turnover 是换手率，不是成交额。

以下为 2026-11-13 原始值，未作单位换算：

| 股票 | 新浪直连 volume | 新浪直连 amount | AKShare volume | AKShare amount |
| --- | --- | --- | --- | --- |
| 沪 600519 | 4177984 | 未返回 | 4177984.0 | 5954834070.0 |
| 深 000001 | 218942998 | 未返回 | 218942998.0 | 2624481927.0 |
| 北 920047 | 2309995 | 未返回 | 2309995.0 | 29511004.0 |

原始请求地址、抓取时间和完整返回行保存在 `tests/fixtures/kline_amount_20261113.json`。
三只股票各 10 个交易日的 volume 都相同，均为股；没有报价接口中的 ×100 差异。
直连接口没有成交额，不能对它推断元/万元，更不能用 close × volume 或当日报价补历史成交额。
AKShare 的 amount 原样进入 KLine.turnover，单位元，不需要 ×10000。

## 修复

原链路收到有 OHLCV、无 amount 的 K 线就停止回退，扫描随后报资料不足或被同步入口略过。
`min_turnover > 0` 时要求最后一根周期 K 线有有限成交额，否则继续尝试下一家。
公式直接或嵌套读取 `amount`（含 `price: amount`）时，后台扫描、同步扫描和服务端 `/eval` 取数要求整个返回窗口的金额完整；调用方显式提交的 K 线不被替换。
AKShare 金额缺失、非有限或无效时保留当日 OHLCV，将金额记为未知；金额缺失不再造成交易日丢失或周/月部分金额冒充总额。旧 AKShare K 线缓存不再复用。
新鲜缓存、过期缓存和周期聚合结果均执行同一检查；真实 0 有效，缺失和非有限数值不可冒充 0。
普通图表不强制成交额；图表与金额筛选的并发请求分开合并，避免共享不满足要求的结果。
金额来源全不可用时后台任务仍失败，不制造匹配结果；同步入口维持原有略过行为。

回归重放真实样本，覆盖同日量额、三市场门槛上下 1%、两种扫描入口、缓存、并发、周/月合计及金额缺失。
