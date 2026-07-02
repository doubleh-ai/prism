# Changelog

All notable changes to Prism are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/), and this project
follows [Semantic Versioning](https://semver.org/).

## [v0.10] - 2026-06-24

### Added — Astock Multi-Agent Analysis (Phase 2)
- **7-agent LangGraph pipeline**: Market/Social/News/Fundamentals/Policy/HotMoney/Lockup
  analysts → Bull/Bear debate → Research Manager → Trader → Risk debate
  (Aggressive/Conservative/Neutral) → Portfolio Manager decision.
- **SSE real-time progress**: stage timeline + agent grid + progress bar with
  percentage tracking. Each stage emits events so the frontend shows which
  analyst is running.
- **Analysis persistence**: analysis state survives page refresh via
  sessionStorage; auto-reconnects SSE or fetches completed report on restore.
- **Structured report**: rating badge (BUY/SELL/HOLD), foldable analyst
  reports, debate summary, trading plan, risk assessment cards.
- **Copy to chat**: one-click "add to conversation" pastes final decision.

### Added — Industry Chain Graph (Phase 6)
- **`industry_nodes` / `industry_edges` / `geo_regions`** tables in SQLite.
- **12-country seed data** (CN/TW/KR/JP/US/DE/AU/CL/SA/IN/VN/MY) with key
  industries and industrial policy summaries.
- **4 API endpoints**: `/api/industry/graph` (filtered nodes+edges with depth
  expansion), `/api/industry/geo` (region data for world-map rendering),
  `/api/industry/search` (fuzzy name search), `/api/industry/ticker/{ticker}/chain`
  (upstream/downstream traversal).
- **Accumulates over time**: each astock analysis can feed new nodes/edges.

### Added — Technical Indicators
- **30+ indicators** via pandas-ta: SMA, EMA, MACD, RSI, BOLL, KDJ, ATR, OBV, etc.
- **DSL expression parser**: `GET /api/indicators/{ticker}?expr=macd(12,26,9)|rsi(14)`.
- **Cached**: results cached per-ticker for 60s.

### Added — Performance Optimizations (Phase 1)
- **LRU caches**: explain-term (256 entries), market data (64 entries).
- **SQLite PRAGMAs**: WAL mode, 64MB page cache, 256MB mmap, memory temp store,
  5s busy timeout, thread-local connection reuse.
- **aiofiles**: async file I/O for report serving.
- **Tencent async**: tencent market provider uses `asyncio.to_thread`.
- **GZip + Cache-Control**: gzip responses ≥512 bytes; `immutable` for /static/.

### Added — Offline + PWA (Phase 5)
- **Service Worker**: caches static assets (CSS/JS/lib/HTML) for offline access.
- **IndexedDB conversation cache**: stores recent messages locally.
- **SSE reconnect**: exponential backoff (5 retries) on stream disconnect.

### Added — Frontend Optimizations (Phase 3)
- **chunkSig O(1)**: hash-based dedup for chat text chunks, replacing linear search.
- **renderOne**: incremental DOM update — each message renders in isolation.
- **Markdown fixes**: `**bold**` rendering and HTML residual `">` cleanup.

### Fixed
- **Analysis button visibility**: CSS selector (child vs descendant) fixed.
- **AskUserQuestion**: now POSTs `/api/chat/respond` instead of only filling input.
- **Analysis refresh**: sessionStorage persistence + auto-restore on page load.
- **persist_graph atomicity**: explicit `BEGIN`/`COMMIT`/`ROLLBACK` transaction.
- **mmap_size fallback**: graceful degradation when mmap is unsupported.
- **Mode transition**: CSS `@keyframes modeFadeIn` for smooth chat↔market switching.
- **XSS in ref.url**: `safeUrl()` strips `javascript:`/`vbscript:`/`data:` schemes.
- **IDOR in api_graph**: per-user ownership check on analysis nodes.
- **CRLF in X-Request-ID**: stripped before use.
- **Search bar alignment**: matched sidebar width.
- **akshare cache warmup**: pre-fetched on search for faster results.

### Changed
- **JS modules**: 1,830-line `app.js` split into 9 modules (util, core, chat,
  chat-render, market, analysis, industry, indicators, tour).
- **Analysis panel redesign**: progress bar + stage timeline + agent grid (2-col)
  + report cards with foldable sections.
- **Quote refresh throttling**: 30s for active watchlist, 120s for background.
- **K-line periods**: added weekly/monthly support.
- **Default model**: changed to `deepseek-v4-pro`.

## [v0.9] - 2026-06-23

### Added
- **Phase 4 — Quote panel optimization**: batch quote queries, watchlist
  refresh throttling (30s/120s), K-line weekly/monthly, search cache warmup.
- **Phase 5 — Offline + PWA**: Service Worker, IndexedDB conversation cache,
  SSE disconnect reconnection with exponential backoff (5 retries).
- **Phase 6 prerequisites**: DB schema + seed data.

### Fixed
- **D1**: `persist_graph` now wrapped in explicit `BEGIN`/`COMMIT`/`ROLLBACK`.
- **D3**: `mmap_size` graceful fallback tests (2 new tests).
- **U4**: Mode transition fade-in animation (`@keyframes modeFadeIn`).
- **Analysis refresh**: sessionStorage state persistence + `_restoreAnalysisState()`.

## [v0.8.2] - 2026-06-20

### Fixed (production market data unavailable)
- **Root cause**: baostock ranked first in provider chain but its TCP port
  (`baostock.com:10087`) was unreachable in production; returning partial data
  with `None` for non-optional `Quote.change / change_pct` (float) triggered
  `ValidationError`. The failover chain only caught `ProviderUnavailable`, so
  the exception leaked through without falling back to akshare.
- **Fix**:
  - Provider chain now failovers on **any exception**, not just `ProviderUnavailable`.
  - K-line empty results treated as "no data from this provider", try next.
  - baostock `get_quote` normalizes missing `change/change_pct` to `0.0`.
  - Default provider order changed to `akshare,baostock` (akshare via Sina most
    stable in current network).

## [v0.8.1] - 2026-06-20

### Fixed (UI redesign finishing)
- **Icon rail**: conversation/market tabs separated into fixed 52px left column.
- **Composer controls**: 🛠 tools / model selector / send button unified to 34px height.
- **Skill drawer anchoring**: `position:absolute` pinned to button, closes on outside click.
- **Sidebar width**: both modes now 264px (removed market-mode 300px override).
- **Logo static**: Prism icon no longer toggles on click; collapse moved to ☰ button.

## [v0.8] - 2026-06-20

### Refactored
- **App skeleton**: three-column layout (icon rail + content sidebar + main area).
- **Chat cards**: messages rendered as cards; composer redone (chip + toolbar + send/stop).
- **Frontend modularization**: 1,830-line `app.js` split into `app.util.js` / `app.core.js` /
  `app.chat.js` / `app.market.js`, mounted on global `App` object.
- Default model changed from `deepseek` to `minimax-m3`.

## [v0.7.1] - 2026-06-20

### Fixed
- **baostock concurrency race**: `asyncio.Lock` serializes login → query → drain into
  atomic `_query()`, preventing session cross-talk / hangs under concurrent requests.

## [v0.7] - 2026-06-19

### Added
- **Standalone market panel**: market upgraded to first-class mode (icon rail "行情").
- **Market sidebar**: search + watchlist (localStorage, 60s batch refresh), "⊕ 自选" button.
- **Market main panel**: detail card (price/change/OHLCV) + ECharts K-line (candlestick+
  volume dual-axis, 1d/1w/1m, drag-zoom, theme-aware) + fundamentals (PE/PB/PS).
- **Cross-mode links**: `[[quote:TICKER]]` chip click → market panel; "问 AI" button →
  chat mode with pre-filled "分析一下 {ticker} {name}".

## [v0.6] - 2026-06-19

### Added
- **Market data layer**: pluggable providers (akshare/baostock), SQLite TTL cache
  (quote 60s/kline 5min/fundamentals 24h), provider failover with stale-data fallback.
- **5 endpoints**: `/api/quote/{ticker}`, `/api/quote/{ticker}/kline`,
  `/api/fundamentals/{ticker}`, `/api/search`, `/api/data-providers`.
- **Chat enhancement**: `[[quote:TICKER]]` directive in system prompt; AI output
  auto-renders real-time price chips (`.quote-chip`).
- **Market query modal**: toolbar "行情" button opens search + quote popup.

### Optional dependencies
- `akshare>=1.13,<2` and `baostock>=0.8.11,<1` moved to `requirements-extra.txt`.

## [v0.5] - 2026-05-15

### Added
- Inline report viewer (sandboxed iframe).
- Recycle bin with 7-day soft delete and permanent delete.
- Batch delete conversations.
- Glossary panel: hover term → definition; click → AI deep explanation.
- Inline reference citations from WebSearch/WebFetch results.
- Per-conversation cancel/stop button.

### Fixed
- Historical conversation display during streaming (switching mid-stream).
- Toolbar visual separation from conversation content.
- Skill HTML report presentation (sandboxed iframe viewer).
