# Prism Evolution — Personal Research Workstation

**Date**: 2026-06-19
**Status**: Design approved, awaiting implementation
**Author**: brainstorming session

## Goal

Evolve Prism from a self-hosted single-user chat wrapper around Claude
Code into a complete personal investment research workstation. Three
top pain points are addressed: (1) real-time market data lookup,
(2) simple portfolio tracking with P&L, (3) AI-generated alert
conditions. Shipped in three independently usable releases.

## Non-Goals

- **Multi-tenant / team / RBAC.** Stays single-user self-hosted.
- **Order management / broker integration.** Read-only research, no
  trading. Adding broker connectivity would create a regulatory
  problem and a whole new product surface.
- **Backtesting engine.** Different problem (quant infrastructure,
  factor model, optimization). Out of scope until later.
- **Mobile native app.** Web responsive only.

## Architecture Overview

```
┌──────────────────────────────────────────────────────┐
│  Browser (vanilla JS + SSE)                           │
│  ┌──────┐ ┌────────┐ ┌────────┐ ┌────────┐           │
│  │ Chat │ │Reports │ │ 持仓   │ │ 警报   │           │
│  └──┬───┘ └───┬────┘ └────┬───┘ └────┬───┘           │
└─────┼─────────┼───────────┼──────────┼───────────────┘
      ▼         ▼           ▼          ▼
┌──────────────────────────────────────────────────────┐
│  FastAPI                                              │
│  /api/chat/* /api/reports/* /api/portfolio/* /api/alerts/*│
│  /api/quote/{ticker}                                 │
└─────┬─────────────────────────────────────┬──────────┘
      ▼                                     ▼
┌──────────────┐   ┌─────────────────┐  ┌──────────┐
│ Claude Code  │   │ MarketDataProvider│  │SQLite WAL│
│ CLI          │   │  ├ akshare       │  │ positions│
│              │   │  ├ baostock      │  │ alerts   │
│              │   │  ├ tushare(预留) │  │ market_  │
│              │   │  └ wind(预留)    │  │  cache   │
└──────────────┘   └─────────────────┘  └──────────┘
                         │
                  ┌──────┴────────┐
                  │  APScheduler  │  (v0.7+)
                  └───────────────┘
```

**Key design principles:**

1. **Data source is fully pluggable.** `MarketDataProvider` is a
   Protocol with `get_quote / get_kline / get_fundamentals / search`.
   Default: akshare + baostock (both free, no key). New providers
   (tushare, wind, choice) added by writing one class.
2. **Local cache layer.** All external calls go through a SQLite
   cache with TTL: 60s for quotes, 5min for K-line, 24h for
   fundamentals. Prevents rate-limiting and keeps latency low.
3. **AI-first, data-augmented.** Chat stays the primary surface. AI
   output embeds structured data tags (`[[quote:600519]]`) that
   the frontend renders as live price cards. User reads the
   analysis, the numbers are right there — no tab-switching.
4. **One-user, one-instance.** No multi-tenant DB, no team
   permissions, no SSO. Avoids a 10x complexity explosion.

## Releases

### v0.6 — Data Layer (target: 2-3 weeks)

**New module**: `server/market/`

```
server/market/
├── __init__.py
├── provider.py         # MarketDataProvider Protocol + base
├── cache.py            # SQLite-backed TTL cache
├── akshare_provider.py
├── baostock_provider.py
└── router.py           # FastAPI routes
```

**Provider Protocol** (the contract every provider implements):

```python
class MarketDataProvider(Protocol):
    name: str

    async def get_quote(self, ticker: str) -> Quote: ...
    async def get_kline(self, ticker: str, period: str = "1d",
                        limit: int = 120) -> list[KLine]: ...
    async def get_fundamentals(self, ticker: str) -> Fundamentals: ...
    async def search(self, query: str, limit: int = 10) -> list[SearchHit]: ...
    async def health(self) -> bool: ...
```

`Quote` / `KLine` / `Fundamentals` are Pydantic models defined in
`server/market/models.py`.

**Cache layer** (`server/market/cache.py`):

```python
class MarketCache:
    """SQLite-backed TTL cache. Keyed by (provider, method, ticker, params)."""
    async def get_or_fetch(self, key: str, ttl: int,
                           fetch_fn: Callable[[], Awaitable[T]]) -> T: ...
    async def invalidate(self, pattern: str) -> int: ...
```

TTLs (configurable):
- `QUOTE_TTL = 60` seconds
- `KLINE_TTL = 300` seconds
- `FUNDAMENTALS_TTL = 86400` seconds (24h)

**New database tables**:

```sql
CREATE TABLE market_cache (
    cache_key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX idx_market_cache_expires ON market_cache(expires_at);
```

Migrations added to `init_db()` in the same pattern as the existing
conversations/messages tables.

**New endpoints**:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/quote/{ticker}` | Latest price, change, volume |
| GET | `/api/quote/{ticker}/kline` | OHLCV bars (period + limit) |
| GET | `/api/fundamentals/{ticker}` | PE/PB/ROE/revenue/profit |
| GET | `/api/search` | Ticker / name search |
| GET | `/api/data-providers` | Active providers + health |

**Configuration** (`server/config.py` additions):

```python
MARKET_PROVIDERS = ["akshare", "baostock"]  # priority order
AKSHARE_TIMEOUT = 10
BAOSTOCK_TIMEOUT = 10
QUOTE_TTL = 60
KLINE_TTL = 300
FUNDAMENTALS_TTL = 86400
```

**Provider selection and failover**:

```python
class ProviderChain:
    """Tries providers in order, returns first success; falls back on
    failure. Caches the failure for 30s to avoid hammering a dead source."""
    def __init__(self, providers: list[MarketDataProvider]): ...
    async def get_quote(self, ticker: str) -> Quote: ...
```

**Frontend integration**:

- Add `[[quote:600519]]` tag parsing in `app.js` `render()`.
- Render price chip next to ticker mentions. Click opens a side
  drawer with quote + mini K-line (use a small canvas-based chart,
  no chart library to keep the bundle small).
- New toolbar button "行情" opens a ticker search modal.
- The chat system prompt is augmented with a one-liner: "When you
  mention a specific ticker, include `[[quote:TICKER]]` so the
  UI can display its real price."

**Error handling**:

- Provider throws → next provider in chain.
- All providers fail + cache miss → 503 with structured error
  `{"error": "no_provider_available", "tried": ["akshare", "baostock"]}`.
- All providers fail + cache hit (even stale) → return cached,
  flag `stale: true` in the response.
- Rate-limit detection (HTTP 429) → exponential backoff in cache
  layer; mark provider unhealthy for 5 min.

**Testing**:

- `test_provider_protocol.py` — interface conformance tests,
  applied to all providers.
- `test_akshare_provider.py` — mocked HTTP responses for happy
  path, rate limit, timeout, empty result.
- `test_baostock_provider.py` — same.
- `test_cache.py` — TTL expire, key collision, concurrent fetches.
- `test_provider_chain.py` — failover order, dead-provider skip.
- `test_endpoints.py` — happy + 503 + stale path.

**Migration**: existing `init_db()` gets the new table; no
backwards-incompat changes.

---

### v0.7 — Portfolio (target: 2-3 weeks)

**New database tables**:

```sql
CREATE TABLE positions (
    id TEXT PRIMARY KEY,                -- secrets.token_hex(12)
    ticker TEXT NOT NULL,
    shares REAL NOT NULL,               -- share count (supports fractional)
    cost REAL NOT NULL,                 -- cost basis per share
    account TEXT DEFAULT 'default',     -- multi-account slot
    notes TEXT,                         -- user notes
    opened_at INTEGER,                  -- unix seconds
    closed_at INTEGER,                  -- NULL = currently held
    created_at INTEGER DEFAULT (strftime('%s','now')),
    updated_at INTEGER DEFAULT (strftime('%s','now'))
);
CREATE INDEX idx_positions_ticker ON positions(ticker);
CREATE INDEX idx_positions_closed ON positions(closed_at);

CREATE TABLE portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at INTEGER NOT NULL,          -- unix seconds
    total_value REAL,
    total_cost REAL,
    unrealized_pnl REAL,
    pnl_pct REAL,
    detail_json TEXT NOT NULL           -- [{ticker, shares, cost, price, pnl}, ...]
);
CREATE INDEX idx_snapshots_taken_at ON portfolio_snapshots(taken_at);
```

**Position model** (single record = the user's current view of
"how much of X do I hold at what cost"). No trade ledger — a
later v0.9 could add one if the user wants.

**New endpoints**:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/portfolio` | Current open positions + computed P&L |
| POST | `/api/portfolio` | Add a new position |
| PATCH | `/api/portfolio/{id}` | Update (add/reduce/close/notes) |
| DELETE | `/api/portfolio/{id}` | Hard delete |
| GET | `/api/portfolio/summary` | Aggregate totals (cost/value/P&L) |
| GET | `/api/portfolio/history` | Snapshot history for chart |

**P&L calculation** (computed in router, not stored):

```python
def compute_pnl(position, current_price: float) -> dict:
    market_value = position.shares * current_price
    cost_basis = position.shares * position.cost
    unrealized = market_value - cost_basis
    pnl_pct = (unrealized / cost_basis) * 100 if cost_basis else 0
    return {
        "market_value": market_value,
        "cost_basis": cost_basis,
        "unrealized_pnl": unrealized,
        "pnl_pct": pnl_pct,
    }
```

**Daily snapshot job** — a lightweight cron-like task that runs
at 16:00 every trading day, queries all open positions and writes
a `portfolio_snapshots` row. Implemented with APScheduler (same
infrastructure that v0.8 needs anyway), so v0.7 brings in the
scheduler dependency.

**AI integration**:

- v0.7 takes the simpler approach: when a chat session starts,
  the system prompt is augmented with the user's open positions
  as a compact JSON block (typical N ≤ 几十, no perf concern).
  A tool-call `get_my_portfolio` could be added later if needed.
- New toolbar button "我的持仓" expands a sidebar with positions.
- Adding a position from the chat: user says "记录加仓
  600519 1000 股 @ 1680" → AI returns a `[[action:add_position]]`
  block → frontend shows confirmation dialog → on accept, posts
  to `/api/portfolio`.

**Frontend**:

- New sidebar tab "持仓".
- Table: code / name / shares / cost / current price / market
  value / P&L / P&L% / actions.
- "Add" form: ticker autocomplete (uses `/api/search` from v0.6),
  numeric inputs for shares and cost, optional notes.
- Manual "refresh prices" button — invalidates quote cache for
  held tickers and re-fetches.
- History chart (canvas): plot daily P&L over the last 30/90/365
  days. No chart library.

**Error handling**:

- Adding a position with unknown ticker → 422 with a hint to use
  search.
- Cost or shares negative → 422.
- Closing a position twice → 409 Conflict.
- Quote fetch fails for a held ticker → mark that row as "stale",
  show the last-known price with a warning icon, don't fail the
  whole response.

**Testing**:

- `test_positions_crud.py` — create / read / update / delete +
  invalid inputs.
- `test_pnl.py` — boundary cases: zero shares, negative cost
  (shorts, not supported — should reject), fractional shares.
- `test_snapshots.py` — daily job inserts the right row, doesn't
  double-write.
- `test_endpoints.py` — full HTTP integration.

---

### v0.8 — AI Alerts (target: 3-4 weeks)

**Core concept**: user describes an alert in natural language, AI
parses it to a structured condition expression, a scheduler
periodically evaluates the condition against fresh market data,
triggers an event when matched.

**Condition DSL** (JSON expression tree):

```json
{
  "type": "compound",
  "op": "and",
  "children": [
    {"type": "compare", "field": "pe_ttm", "op": "<", "value": 15},
    {"type": "indicator", "name": "rsi_14", "op": ">", "value": 70},
    {"type": "compare", "field": "close", "op": "crosses_above", "value": "ma_20"}
  ]
}
```

Supported `type`:
- `compare`: field/indicator vs literal (`<`, `>`, `<=`, `>=`, `==`, `!=`)
- `indicator`: technical indicators (`ma_N`, `rsi_N`, `macd`, `boll`)
- `compound`: `and` / `or` / `not`
- `event`: cross detection (`crosses_above`, `crosses_below`)

**Evaluator** (`server/alerts/evaluator.py`):

```python
class ConditionEvaluator:
    """Pure-function expression evaluator. No I/O. Takes a snapshot
    dict of {ticker: {field: value, indicator: value, ...}} and
    returns bool."""
    def evaluate(self, expr: dict, snapshot: dict) -> bool: ...
```

The evaluator is fully unit-testable without any market data.

**New database tables**:

```sql
CREATE TABLE alerts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    ticker TEXT,                        -- NULL = any ticker (screener)
    condition_json TEXT NOT NULL,
    generated_prompt TEXT,              -- original natural language
    status TEXT DEFAULT 'active',       -- active / paused / triggered
    schedule TEXT DEFAULT 'market_hours', -- market_hours / daily / hourly
    cooldown_minutes INTEGER DEFAULT 60, -- don't re-trigger within window
    webhook_url TEXT,                   -- optional, for v0.8
    last_evaluated INTEGER,
    last_triggered INTEGER,
    created_at INTEGER DEFAULT (strftime('%s','now')),
    updated_at INTEGER DEFAULT (strftime('%s','now'))
);
CREATE INDEX idx_alerts_status ON alerts(status);

CREATE TABLE alert_triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id TEXT NOT NULL,
    triggered_at INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,         -- market data at trigger time
    conv_id TEXT,                        -- auto-opened conversation
    acknowledged INTEGER DEFAULT 0,
    FOREIGN KEY (alert_id) REFERENCES alerts(id) ON DELETE CASCADE
);
CREATE INDEX idx_triggers_alert ON alert_triggers(alert_id);
```

**New endpoints**:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/alerts` | List alerts |
| POST | `/api/alerts/parse` | NL → JSON condition (via AI) |
| POST | `/api/alerts` | Create alert (with parsed JSON) |
| PATCH | `/api/alerts/{id}` | Update (status, name, etc.) |
| DELETE | `/api/alerts/{id}` | Delete |
| POST | `/api/alerts/{id}/test` | Evaluate once, return result |
| GET | `/api/alerts/triggers` | Trigger history |
| POST | `/api/alerts/triggers/{id}/ack` | Mark as seen |

**AI parsing flow** (v0.8 introduces a small AI-as-parser use case):

1. User clicks "New alert", types: "PB 小于 1.5，股息率大于
   3%，最近成交量放大到 20 日均量 1.5 倍"
2. POST `/api/alerts/parse` with the text + an explicit system
   prompt that includes the DSL schema.
3. Backend opens a short independent Claude Code session with a
   strict "JSON only, no commentary" prompt and the schema
   embedded; reads back the JSON via the existing JSONL parser.
   Model selection follows `MODELS[DEFAULT_MODEL]` (single
   configurable model — we don't switch mid-session).
4. Frontend shows: parsed condition tree + summary, lets user
   edit fields manually before saving.
5. On save, POST `/api/alerts` with the final condition.

**Scheduler** (`server/alerts/scheduler.py`):

Uses APScheduler with three trigger types:
- `market_hours`: cron expression that fires every 5 min between
  9:30 and 15:00 China time on weekdays. Holiday check via
  `akshare.tool_trade_date_hist_sina()`, cached 24h, prewarmed
  on startup with the last 365 days.
- `daily`: 16:00 every weekday.
- `hourly`: top of every hour.

For each active alert at each fire time:
1. Fetch current data (uses v0.6's `ProviderChain` + cache).
2. Evaluate condition.
3. If true AND `last_triggered + cooldown_minutes < now`:
   - Write `alert_triggers` row.
   - Open a new conversation (`/api/convs` with system prompt
     containing the alert name, the trigger snapshot, and a
     "please analyze this trigger" instruction).
   - Update `last_triggered`.
   - Fire webhook (if configured).
4. Update `last_evaluated`.

**Frontend**:

- New sidebar tab "警报".
- List of alerts with status pill (active/paused/triggered),
  last evaluated, last triggered.
- "New alert" wizard:
  1. Text input → "解析" button → shows parsed condition tree.
  2. User confirms or edits.
  3. Set ticker (or "any"), schedule, cooldown, optional webhook.
  4. Save.
- "Triggers" sub-tab: history list, click to open the
  auto-generated conversation.
- Browser Notification API opt-in: ask on first alert creation.
  If denied, the prompt is suppressed for 30 days; user can reset
  via a settings entry that clears the cooldown.

**Error handling**:

- Condition JSON malformed → 422 on create.
- AI parse fails (returns invalid JSON) → fall back to "manual
  mode" — show user the raw text and let them build the
  condition tree by hand.
- Provider fails for an alert evaluation → mark the alert's
  `last_evaluated` with an error suffix, don't crash the
  scheduler, log it.
- Webhook fails (non-2xx) → record the failure in the trigger
  row, don't retry (v0.8 keeps it simple).

**Testing**:

- `test_evaluator.py` — pure unit tests, ~20 cases per operator
  + compound + edge cases (empty children, NaN, missing field).
- `test_ai_parser.py` — round-trip a few example natural
  language inputs (uses a fixture prompt, doesn't call real
  Claude in CI).
- `test_scheduler.py` — mock time + mock provider, verify the
  scheduler fires the right actions at the right times.
- `test_endpoints.py` — HTTP integration.
- `test_trigger_lifecycle.py` — trigger → conv opened → ack.

---

## Cross-Cutting Concerns

| Dimension | Design |
| --- | --- |
| **Error handling** | Provider failover; AI parse fallback; no PII or stack traces in error responses (reuse existing global exception handler). |
| **Performance** | All external data goes through cache. Batch API: `/api/quote?a,b,c` for multi-ticker portfolio refresh. K-line cap at 500 bars. |
| **Security** | All new tables are user-local. Webhook URL stored as plain text in v0.8 (self-hosted single-user; never logged or echoed in errors); encryption-at-rest deferred to v0.9. Existing CSRF + Pydantic defense applies to new endpoints. |
| **Observability** | New structured log events: `market.fetch`, `market.cache_hit`, `market.provider_fail`, `alert.evaluated`, `alert.triggered`. Reuse the request-ID correlation from the existing middleware. |
| **Migrations** | Each release adds its tables to `init_db()` in the same pattern as existing ones. Zero destructive changes to existing tables. |
| **Testing strategy** | Unit-heavy. Mocked providers for unit tests, real (but free) providers for one integration test that runs only when `RUN_LIVE_TESTS=1`. Coverage floor: 60% per release. |
| **Documentation** | Each release updates `CHANGELOG.md`, `docs/API.md` with new endpoints, and adds a short user-facing guide section to README. |

## Open Questions Deferred to Future Specs

- **Backtesting engine.** Different problem entirely.
- **Trade ledger.** Could be a v0.9 if simple position records
  feel limiting. Not blocking.
- **Watchlist / screener UI.** "Any ticker" alerts in v0.8 scan
  the union of (a) all tickers ever opened in `positions` and
  (b) tickers the user has manually added via the upcoming
  watchlist UI (v0.8.1). v0.8 ships with universe = positions
  only; the UI for manual additions lands as a small follow-up.
- **Mobile.** Out of scope.
- **Mobile push notifications.** Web Notification API is the v0.8
  ceiling; native push is a future project.

## Release Sequencing

```
Week 1-3    v0.6 — Data layer
              ↓
Week 4-6    v0.7 — Portfolio (depends on v0.6 for prices)
              ↓
Week 7-10   v0.8 — Alerts (depends on v0.6 for prices, v0.7 for portfolio context)
```

Each release is independently shippable and adds value on its own.
A user who only wants the data layer can stop at v0.6.

## Success Criteria

- **v0.6**: User can ask "贵州茅台最新价" and get a real number from
  akshare within 500ms (cache hit) or 3s (miss). Cache hit rate
  ≥ 70% in normal use.
- **v0.7**: User can record a position and see live P&L update
  when the page refreshes. Daily snapshot generates without
  failure for 30 consecutive trading days.
- **v0.8**: User can create an alert from natural language with
  one click, sees it fire (or not) on schedule, gets a
  conversation opened with the analysis.
