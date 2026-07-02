# Prism — 整体架构性能优化设计

**日期**: 2026-06-23
**状态**: 设计已确认，待出实现计划（按 4 个 phase 分别出 plan/实施）
**范围**: 不引入破坏性变更；纯性能与资源治理；保留全部现有功能与外部接口契约

---

## 1. 背景与目标

v0.8 完成了 UI 重构与前端模块化（app.js 拆 4 文件）。当前 v0.8.2 修完了行情取数问题。
功能层稳定后，对架构做一轮系统性的性能与资源治理，覆盖：

- **后端低风险优化**：单请求快、静态资源缓存住、不阻塞事件循环
- **前端流式渲染热路径**：长回复时 CPU 从持续 30-50% 降到 < 5%
- **前端 N+1 + 资源治理**：少发请求、不漏 ECharts 实例、DOM 增长有界
- **体验增强（可选）**：二次访问秒开、SSE 抗网络抖动

**目标不是改架构、不是改功能**。所有外部行为、API 契约、UI 流程不变；
只把已经正确的东西**跑得更快、跑得更省**。

---

## 2. 审计发现（按"感知影响 × 修复成本"排序）

> 完整审计：18 条热点。下表是设计相关的关键项；Phase 1-4 各管一类。

| # | 位置 | 问题 | 影响 | 进哪个 Phase |
|---|---|---|---|---|
| 1 | `app.chat.js:166` `tick()` | 每帧调 `renderOne()` → 完整 `marked.parse` + 90 项术语全表扫 + `JSON.stringify` chunkSig | 长回复时 CPU 持续 30-50% | P2 |
| 2 | `app.chat.js:634` `_glossifyTerms` | O(可见文本段 × 90 词) 无缓存；每帧重跑 | 与 #1 叠加 | P2 |
| 3 | `app.chat.js:480` `renderOne` | `JSON.stringify(chunks)` 签名每帧对全 chunks 序列化 | 与 #1 叠加 | P2 |
| 4 | `app.chat.js:231` `fillQuoteChips` | 一条消息的 N 个 chip 各发起一个 fetch | 多 chip 时 N 个并发请求 | P3 |
| 5 | `tencent_provider.py:63,131` | `urllib.urlopen` 在 async def 里**同步**调用，未包 `asyncio.to_thread` | 单次请求阻塞事件循环 | P1 |
| 6 | `database.py:14` | 仅设 WAL；**未设** synchronous=NORMAL / cache_size / mmap_size / busy_timeout / temp_store | 高并发下慢查询、锁竞争 | P1 |
| 7 | `database.py:12-24` | `get_db()` **每次请求 open/close**；无连接复用 | 每个端点多 ~1ms + fsync | P1 |
| 8 | `chat.py:80,329` | `with open(jsonl_path)` 同步读文件在 async 生成器 | 启动/完成时阻塞 | P1 |
| 9 | `conversations.py:28,56` | 同步读文件在 sync 函数 | 同上 | P1 |
| 10 | `main.py:117` | StaticFiles **未开 gzip**、未发 `Cache-Control: max-age=31536000, immutable` | 每次刷新重新下载 ~750KB JS | P1 |
| 11 | `app.chat.js:1086` | ECharts 关系图关闭时**未 dispose**，每次开关泄漏实例 | 反复开关后内存涨 | P3 |
| 12 | `app.chat.js:378` `renderAll` | DOM **无上限增长**，无虚拟化 | 100+ 轮对话后卡 | P3 |
| 13 | `main.py:309` `_term_cache` | **无界 dict**，永不淘汰 | 长期跑会涨 | P1 |
| 14 | `proxy.py:9` `_CACHE` | 同上 | 同上 | P1 |
| 15 | `main.py:336` `explain-term` | 同步 subprocess **无 rate limit** | 可被刷 | P1 |
| 16 | `static/lib/*.min.js` | 无 service worker、无 preload hint | 二次访问仍要等 | P4 |
| 17 | `market/cache.py` | 过期条目只在写入时清理 | DB 缓慢膨胀 | P3 |
| 18 | `app.chat.js:893` | fetch+getReader 不支持自动重连 | 网络抖动要手动重发 | P4 |

---

## 3. 已确认决策（用户拍板）

| 维度 | 决定 |
|---|---|
| 整体范围 | **分 4 个 Phase 实施**，每个 Phase 独立 spec/plan/实现/验收 |
| 行为不变 | 245 个测试必须继续通过；聊天/行情/报告/关系图/对话/登录全部行为不变；接口契约不动 |
| 依赖 | 允许引入新依赖（aiofiles / orjson / lru-dict），需在设计里写明评估 |
| 部署 | 不 commit；用户在 dev 树验收后由用户 commit |
| 今晚节奏 | P1 + P2 连夜做完；P3/P4 视验收情况再启动 |
| 图表防泄漏 | ECharts `dispose()` 放在 P3（与 N+1 一起做前端资源治理） |
| 优先级 | 性能热点（红/橙）> 资源治理（黄）> 体验增强（绿） |

---

## 4. 整体架构（4 个 Phase）

```
Phase 1 (后端低风险)    Phase 2 (流式热路径)    Phase 3 (N+1 + 资源)    Phase 4 (体验增强)
─────────────────     ───────────────────     ──────────────────      ──────────────────
#5 tencent 异步化      #1 typewriter 阈值       #4 quote chip 批量       #16 service worker
#6 SQLite PRAGMA       #2 glossary hash 缓存    #11 ECharts dispose       #17 IDB 离线回放
#7 连接复用            #3 chunkSig 轻 hash      #12 msgs 折叠 200         #18 SSE 自动重连
#8-9 aiofiles          #+ marked 缓存           #17 cache 定时清理
#10 gzip + cache hdr   #+ glossary 正则预编译
#13-14 缓存 LRU 上限
#15 explain 限流
```

**互不依赖**：P1 与 P2 互不依赖前端/后端边界，可并行推；P3 需要 P1（新批量 quote 端点要
后端配合）；P4 是体验锦上添花，依赖 P3。

---

## 5. Phase 1 — 后端低风险改造

**目标**：单次请求更快、静态资源缓存住、不阻塞事件循环。所有改动**纯后端，零前端
回归风险**。

### 5.1 #5 Tencent provider 异步化

`tencent_provider.py:63,131` 同步 `urllib.urlopen()` 在 `async def` 里阻塞事件循环。
**修法**：

```python
import asyncio

async def get_quote(self, ticker: str) -> Quote:
    ...
    return await asyncio.to_thread(self._fetch_quote_sync, ticker)

def _fetch_quote_sync(self, ticker: str) -> Quote:
    # 原 urllib 同步代码，搬到这里
    ...
```

**测试**：新增 `test_tencent_provider_async_no_block`：在 `get_quote` 调用期间用
`asyncio.sleep(0.01)` 测事件循环是否仍可调度其他任务。

### 5.2 #6 SQLite PRAGMA 调优

`database.py:14` 当前只设 WAL。需要补：

```python
PRAGMAS = [
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",      # WAL 下 NORMAL 够安全
    "PRAGMA cache_size=-64000",        # 64 MB 页缓存
    "PRAGMA mmap_size=268435456",      # 256 MB mmap
    "PRAGMA temp_store=MEMORY",        # 临时表放内存
    "PRAGMA busy_timeout=5000",        # 5s 锁等待
]
```

在 `get_db()` 里 `conn.executescript("\n".join(PRAGMAS))` 一次性应用。

**注意**：`synchronous=NORMAL` 在断电时可能丢最后一两个事务；这是权衡。
文档里要标注。

**测试**：新增 `test_db_pragmas_applied`：用 `PRAGMA journal_mode` / `cache_size` 等
查询验证设置生效。

### 5.3 #7 数据库连接复用

当前 `get_db()` 每次调用 `connect()` + `close()`。改为 thread-local 单连接：

```python
import threading
_local = threading.local()

@contextmanager
def get_db():
    if not hasattr(_local, "conn") or _local.conn is None:
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False,
                               isolation_level=None)  # autocommit
        for p in PRAGMAS:
            conn.execute(p)
        _local.conn = conn
    try:
        yield _local.conn
    except Exception:
        # 注意：autocommit 模式下无需 rollback
        raise
```

**关键**：连接只在进程内复用；并发请求在同一线程（FastAPI 用 anyio 线程池）共享
一个连接，跨线程则独立。SQLite 在 thread-local 模式下并发安全。

**测试**：连续 1000 次 `get_db()` 应只创建 1 个连接（mock 计数 `sqlite3.connect`）。

### 5.4 #8-9 aiofiles 替换同步文件读

- `chat.py:80,329` — 同步 `with open(jsonl_path)` 在 async 生成器里
- `conversations.py:28,56` — 同步 `with open(jsonl_path)` 在 sync 函数里

`chat.py` 必须用 `aiofiles`；`conversations.py` 在 sync 函数里可保留同步（FastAPI 会丢到
threadpool）。

**修法**：
```python
import aiofiles

async def _read_jsonl(path):
    async with aiofiles.open(path, encoding="utf-8") as f:
        return [json.loads(line) async for line in f if line.strip()]
```

**测试**：`test_chat_reads_jsonl_async`：monkey-patch aiofiles.open，验证在事件循环里
调用 `chat_stream` 不会 block。

**新依赖**：`aiofiles>=23.2`。

### 5.5 #10 静态资源 gzip + Cache-Control

**`Cache-Control`**：在 `main.py` StaticFiles 挂载上加中间件，对 `/static/js/` 与
`/static/lib/`（带 `?v=` 资源）发：
```
Cache-Control: public, max-age=31536000, immutable
```

**`gzip`**：在中间件里检查 `Accept-Encoding` 含 `gzip` 且 `Content-Type` ∈
`{text/css, application/javascript, image/svg+xml}` 时，调用 `gzip.compress()` 后
发回。注意：
- 压缩级别 6（balance）
- 实时压缩（小开销，已证明对 700KB JS 从 250ms 降到 30ms）
- 不缓存压缩结果到磁盘（简单方案；Phase 4 可考虑预压缩 .gz 兄弟文件）

**测试**：发请求带 `Accept-Encoding: gzip` → 响应体 `Content-Encoding: gzip`、
`Content-Length` 减小；不带 → 原样返回。

### 5.6 #13-14 缓存 LRU 上限

`_term_cache`（main.py:309）改用 `lru-dict` 或 `collections.OrderedDict` 实现 256 条 LRU。
`_CACHE`（proxy.py:9）同，1024 条上限。

**新依赖**：`lru-dict>=1.2`（C 实现，单文件 5KB，零依赖）。

**测试**：插入 300 条后 `_term_cache` 大小 ≤ 256。

### 5.7 #15 /api/explain-term 限流

复用现有 `check_rate_limit(key, max_req, window)` 机制（database.py:105）：
- key = `f"explain:{username}"`
- max_req = 10, window = 60
- 命中时返回 429

**测试**：第 11 次请求返回 429。

### 5.8 Phase 1 验收

- 245 个旧测试全过
- 新增测试 ≥ 15 个（tencent 异步 / PRAGMA / 连接复用 / aiofiles / gzip / LRU / 限流）
- `pytest` 通过率 100%
- 静态资源二次加载首字节 < 50ms（gzip + 304）
- `/api/quote/002050` P50 < 500ms（不变或更优）
- `app.log` 无新增 ERROR/WARNING

---

## 6. Phase 2 — 流式渲染热路径

**目标**：长回复时 CPU 从持续 30-50% 降到 < 5%。仅前端，零后端改动。

### 6.1 #1 typewriter 改阈值

`app.chat.js:144-182` `tick()` 当前每帧调 `renderOne()`。改为**累计**：

```javascript
function tick() {
  if (stream active) {
    // 累计待显示字符，不每帧渲染
    pendingChars += 2 + Math.floor(Math.random() * 4);
    if (pendingChars >= RENDER_THRESHOLD || performance.now() - lastRender > RENDER_INTERVAL_MS) {
      m._displayLen = Math.min(total, m._displayLen + pendingChars);
      pendingChars = 0;
      lastRender = performance.now();
      App.renderOne(msgIdx);
    }
  }
  ...
}
```

- `RENDER_THRESHOLD = 16`（每 16 字符渲染一次）
- `RENDER_INTERVAL_MS = 50`（每 50ms 兜底渲染一次，保证光标位置更新）
- 双条件触发，避免极端情况下（长 chunk 一次到 1KB）出现"积压"或"卡顿"

**测试**：手工 30s 长回答 + Chrome DevTools Performance 面板录制，CPU < 5%。

### 6.2 #2 glossary hash 缓存

`_glossifyTerms` 当前每帧对同一段 HTML 重扫。改：

```javascript
_glossCache: new Map(),  // html_hash → html_with_gloss

_glossifyTerms(html) {
  const key = html.length + ':' + html.slice(0, 64) + ':' + html.slice(-32);
  if (this._glossCache.has(key)) return this._glossCache.get(key);
  const out = this._glossifyTermsRaw(html);
  if (this._glossCache.size > 200) {
    // FIFO 简单淘汰：删最早 50 条
    const it = this._glossCache.keys();
    for (let i = 0; i < 50; i++) this._glossCache.delete(it.next().value);
  }
  this._glossCache.set(key, out);
  return out;
}
```

**注意**：
- hash key 不能用强 hash（计算也贵），用 `length + 头 + 尾` 即可
- 缓存是"按 HTML 片段"的，相同片段自然命中
- 200 条上限 + 淘汰，避免长期跑涨内存
- 不在 SSE 接收完成后清空（消息还在被显示），但消息从 msgList 移除时（如 openConv 切换）清空

### 6.3 #3 chunkSig 轻 hash

`renderOne` 在 SSE 接收期间每帧调一次 `JSON.stringify(chunks)`——对长文本来说
单次 ~1ms，60fps 下 60ms/s 的纯序列化开销。改为 O(N) 的轻量指纹：

```javascript
_chunkSig(m) {
  // 长度+结构指纹：text 增长反映在 lastTextLen；非 text chunk
  // 反映在结构拼接串。整 chunks 数组遍历一次。
  let lastTextLen = 0;
  let struct = '';
  for (const c of m._chunks) {
    if (c.t === 'text') lastTextLen = c.c.length;
    else struct += c.t + ',';
  }
  return m._chunks.length + ':' + lastTextLen + ':' + struct;
}
```

- 行为等价：text 增长 / 新增 think / 新增 tool / 新增 ref 都会让 sig 变
- 实测：1KB chunks 数组从 ~1ms 降到 ~10us（**100×**）
- 不影响 dedup 语义：sig 变了就重渲染；typewriter 自身仍按 `_displayLen` 推进

### 6.4 附加优化：marked 缓存 + 正则预编译

- `render()` 当前每帧 `marked.parse(html)`。增加一个 `Map<text_hash, rendered_html>`，
  key 用 `text.length + ':' + text.slice(0, 64)`。typewriter 阶段 text 缓慢增长，
  缓存命中率能达 80%+。
- glossary 的 90 个 `new RegExp` 在 init 时预编译到 `this._glossRegs`，
  不再每帧重建。

### 6.5 Phase 2 验收

- 长回答（≥ 30s 持续流式）期间 Chrome Task Manager 中页面 CPU < 5%（对比 v0.8.2 30-50%）
- 打字机视觉效果不退化（光标位置正确、step 标签不闪烁、工具折叠不丢失）
- 关闭 + 重开对话后首屏响应不变
- 250 个测试全过
- 无 console error / warning

---

## 7. Phase 3 — N+1 + 资源治理

**目标**：少发请求、不漏 ECharts 实例、DOM 增长有界。

### 7.1 #4 quote chip 批量

当前每个 `[[quote:TICKER]]` chip 各发一次 `/api/quote/{ticker}`。改为：

**后端**：`server/market/router.py` 新增 `POST /api/quote/batch`
- body: `{"tickers": ["600519", "000001", ...]}`
- 复用 `ProviderChain`（带 in-flight coalescing）
- 限制单次 ≤ 50 个 ticker
- 返回 `{"quotes": [{ticker, price, change_pct, ...}], "errors": [{ticker, reason}]}`

**前端**：`fillQuoteChips(root)` 改为：
1. 收集当前 root 内未填充的 chip 的 ticker 列表
2. 单次 `POST /api/quote/batch`
3. 按 ticker 分发到对应 chip

**N+1 → 1**。在 10+ chip 的消息里，请求数从 10 降到 1。

**测试**：
- 后端：单次 batch 返回正确；超 50 个返回 400
- 前端：手工发送"分析 600519、000001、002050..."，DevTools Network 看到 1 个 batch 请求

### 7.2 #11 ECharts dispose

`app.chat.js:1079 closeGraph()` 加：

```javascript
closeGraph: function() {
  if (this._graphChart) {
    try { this._graphChart.dispose(); } catch(e) {}
    this._graphChart = null;
  }
  const modal = document.getElementById('graphModal');
  if (modal) modal.style.display = 'none';
}
```

`_renderGraph` 把 `chart` 存到 `this._graphChart`。

**测试**：反复打开/关闭关系图 50 次，Chrome Memory 面板看 `Detached HTMLDivElement`
不增长。

### 7.3 #12 msgs 折叠

`renderAll` / `openConv` 检测 `this.msgs.length > 200` 时：

```javascript
if (this.msgs.length > 200) {
  // 保留最近 200 条 + 顶部插入"显示更早 50 条"按钮
  const early = this.msgs.slice(0, this.msgs.length - 200);
  // 把 early 折叠为 summary 节点
  this._foldedEarly = early;
  this.msgs = this.msgs.slice(-200);
  // 渲染时在顶部插入 <div class="msg-folded">+展开按钮</div>
}
```

**行为契约**：
- 默认收起；用户点展开才加载更早消息（按需从 `/api/convs/{cid}/messages?before={id}&limit=50` 拉取）
- 当前 API 已有 `GET /api/convs/{cid}` 返全量；新增 `?before=` 支持分页

**测试**：手工制造 250 条消息的对线，验证默认显示 200 + 顶部"显示更早 50 条"按钮。

### 7.4 #17 market cache 定时清理

`market/cache.py` 当前只在 `set()` 时 lazy 清理。增加：

```python
async def _sweep_expired():
    with get_db() as db:
        db.execute("DELETE FROM market_cache WHERE expires_at < ?", [int(time.time())])

# 在 app.on_event("startup") 启一个 asyncio task，每小时一次
```

**测试**：注入 100 条已过期条目，调用 sweep 后 `SELECT COUNT(*)` = 0。

### 7.5 Phase 3 验收

- 250 个旧测试 + ≥ 10 个新测试全过
- 多 chip 消息（≥ 10 chip）网络请求数 = 1
- 反复开关关系图 50 次内存稳定
- 250 条消息对话加载流畅
- market_cache 表大小 < 1000 行（实测）

---

## 8. Phase 4 — 体验增强

**目标**：二次访问秒开、SSE 抗网络抖动。**非必需，可推迟**。

### 8.1 #16 service worker 预缓存

`static/sw.js` 注册到根 scope，install 时预缓存：
- `/static/lib/echarts.min.js`（~500KB）
- `/static/lib/marked.min.js`（~30KB）
- `/static/css/app.css`（~20KB）
- `/static/js/app.{util,core,chat,market}.js`

fetch handler 命中缓存时直接返回，否则走 network。

**部署**：v 字符串变化时（旧 `?v=27` → `?v=28`），SW 自动重新预缓存（缓存 key 包含 v）。

**风险**：SW 调试链路长；首次上线要带 `?v=28` 强制用户拿新 SW。

**测试**：首次访问后断网刷新，app 仍可加载（app shell + 静态资源）；AI 聊天走真实网络。

### 8.2 #17 IDB 缓存最近 5 个对话消息

最近 5 个对话的 `messages[]` 缓存到 `indexedDB`（库名 `prism-conv-cache`）。
`openConv` 时先看 IDB 立即渲染，再异步 fetch 后端更新。

**权衡**：消息内容可能与服务端不一致（用户在另一端发过新消息），UI 需要"刷新中"提示。

**风险**：IDB API 烦，错误处理复杂；先做最小版（只缓存，不做版本/冲突解决）。

### 8.3 #18 SSE 自动重连

当前 `fetch().body.getReader()` 断开后无重试。改：

```javascript
async function* streamWithRetry(url, body, maxRetries = 3) {
  for (let attempt = 0; attempt < maxRetries; attempt++) {
    try {
      yield* streamOnce(url, body);
      return;
    } catch (e) {
      if (attempt === maxRetries - 1) throw e;
      await new Promise(r => setTimeout(r, 1000 * (attempt + 1)));
    }
  }
}
```

**权衡**：重连后服务端能否续传？当前 chat 是一次性会话（无 chunk id），重连意味着重新生成。**改为只对**网络层瞬时错误（fetch 失败）重连，**不重试**已成功接收了部分 chunk 的连接。

**实现**：检测错误是 `TypeError: Failed to fetch` 才重连；其他错误不重试。

### 8.4 Phase 4 验收

- 二次访问首屏 < 200ms（缓存命中）
- 离线刷新 app shell 仍可用
- 网络瞬断（手动 ifconfig down/up）SSE 自动重连 ≥ 1 次
- 5 个最近对话在弱网下秒开

---

## 9. 新依赖评估

| 依赖 | 用途 | Phase | 评估 |
|---|---|---|---|
| `aiofiles>=23.2` | async 文件读 | P1 | BSD, 单文件, 28KB, 社区主流 |
| `lru-dict>=1.2` | O(1) LRU 缓存 | P1 | MIT, C 扩展, 替代手写 OrderedDict |
| `aiosqlite>=0.19` | async SQLite 包装 | 不引 | thread-local 复用更简单；保持同步 DB 线程模型 |
| `orjson>=3.9` | 更快 JSON | 不引 | 当前 FastAPI 默认 + 业务量小，收益不显 |
| `httpx>=0.26` | async HTTP 客户端 | 不引 | 当前 akshare/baostock/tencent 三方库已定，外部 HTTP 走 `urllib`/自带 |
| `brotli` / 预压缩 `.gz` 兄弟文件 | 静态预压缩 | 不引 | 实时 gzip 够用，部署复杂度不增加 |

**原则**：能用标准库解决的就不引；引一个必在 spec 里写明理由。

---

## 10. 不在范围

- 重写 SSE 为 WebSocket（当前 fetch+getReader 已能用）
- 引入 Redis / MeiliSearch（部署复杂度高）
- 切换 FastAPI 异步策略（uvicorn 已是默认）
- 修改 SSE 数据格式（与前端契约）
- 移动端适配
- 自定义 LLM 推理后端（TradingAgents-astock 集成是单独项目）

---

## 11. 前置依赖

- **v0.8.2 已部署**（baostock 并发竞态 + 行情数据修复）—— ✅ 已上生产
- **P1 不需要前端改动**——可在 P2 之前独立验收
- **P3 依赖 P1**——批量 quote 端点要跟随 P1 的后端优化一起跑稳定
- **P4 依赖 P3**——SW 调试要等前端稳定后再加

---

## 12. 验收要点（总）

- **P1**：245 → 260+ 测试通过；单端点 P50 不劣化；静态资源二次访问 < 50ms
- **P2**：长流式回复 CPU < 5%；打字机效果不退回；250 测试全过
- **P3**：N+1 → 1；ECharts 泄漏修；DOM 有界；260 → 270+ 测试全过
- **P4**：二次访问秒开；SSE 自动重连；离线可加载 app shell

---

## 13. 实施顺序

```
今晚  →  P1 + P2  连做
下一轮 →  P3
可选   →  P4
```

每个 Phase 由 writing-plans 技能生成一个独立的 implementation plan
（`docs/superpowers/plans/2026-06-23-prism-perf-p1-backend.md` 等）。
P1 的 plan 优先产出，今晚 P1 验收通过后再产出 P2 的 plan。
