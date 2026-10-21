# Prism API Reference

Endpoints are JSON over HTTP unless a stream or file is documented.
Authentication is via a session
cookie (`token`) issued by `/api/login`. The cookie is `HttpOnly`
and (in production) `Secure`; `SameSite=strict` by default.

## Cross-cutting

- **Request ID**: every response carries `X-Request-ID`. Clients
  may supply their own; the server uses it for log correlation.
- **CSRF**: every state-changing request (POST/PUT/DELETE/PATCH)
  must include `X-Requested-With: XMLHttpRequest`. The frontend
  `App.api()` wrapper does this automatically.
- **Validation errors**: 422 with `{"detail": "...", "errors": [...]}`.
- **Rate limit**: 429 on `chat` (20/min/user) and `login`
  (10/min/IP).

## Health and release observations

### `GET /health/ready`

Public, read-only basic application/database readiness. No session, CLI,
model call, skill scan, or third-party probe is required. The database
check performs `SELECT 1`.

- 200 when `status` is `ready`; 503 when it is `unready`.
- Only these fields are returned:

```json
{
  "status": "ready",
  "version": "v0.19.7",
  "deployed_revision": "unknown",
  "checked_at": 1792540800,
  "tz": "Asia/Shanghai"
}
```

`checked_at` is a Unix timestamp in seconds. The revision is a full
40-character hexadecimal commit identifier or `unknown`; missing or
invalid version metadata is also `unknown`. Unknown deployment identity
does not imply an unavailable database. Conversely, `ready` does not
verify model access, market data, authenticated user flows, production
identity, or a completed release. Deployment acceptance must separately
match the observed revision to the immutable candidate and verify the
required features. The Docker healthcheck uses this endpoint.

### `GET /api/admin/health`

Requires an administrator session. Returns the existing on-demand
diagnostic snapshot, including the database check, CLI version check,
skill availability, and whether model credentials are configured (not
their values). The built-in `prism-research` method is required for the
aggregate result; missing optional legacy methods do not fail it. This
snapshot is not a real model call or release acceptance test.

### `GET /api/admin/operations`

Requires an administrator session. Returns read-only release observations
for the configured Prism repository: actual GitHub workflow states and
links, observation time/cache freshness, candidate-only delivery limits,
and metadata for this application instance. GitHub observations may be
cached for 180 seconds; failures or stale data must remain visible rather
than becoming a passing result. No GitHub token is exposed or required by
the public-repository observer.

The current controller has no production adapter: `can_deploy` and
`can_rollback` are false, and the local runtime is not verified as
production. CI and candidate workflow results are not deployment records.
GitHub links open the real workflow page; cancellation there requires
GitHub confirmation and does not roll back any environment. There is no
deployment, restart, cancellation, or rollback mutation on this endpoint.

### `GET /api/releases`

Requires a normal authenticated session. Returns application version,
declared deployment revision, and entries parsed from `CHANGELOG.md`.
These are descriptive metadata, not proof that a production release
completed. See [release-process.md](release-process.md) for the current
scope and requirements for a future production controller.

## Auth

### `POST /api/login`

Body: `{"username": "...", "password": "..."}`

- 200: `{"user": "alice", "csrf_token": "..."}` + sets `token` and
  `csrf_token` cookies.
- 403: wrong credentials.
- 422: malformed body.
- 429: rate-limited.

### `POST /api/logout`

- 200: `{"ok": true}`. Clears both `token` and `csrf_token` cookies.

### `GET /api/me`

- 200: `{"user": "alice"}` if authenticated.
- 401: no valid session.

## Conversations

### `GET /api/convs`

List active (non-deleted) conversations for the current user.

- 200: `[{"id": "...", "title": "...", "skill": "..."}]`

### `POST /api/convs`

Create a new conversation.

Body: `{"skill": "prism-research"}` (optional; the built-in default).

- 200: `{"id": "..."}`

### `GET /api/convs/{cid}`

Get a conversation and its message history.

- 200: `{"id": "...", "title": "...", "skill": "...",
  "messages": [{"role": "user|assistant", "content": "..."}]}`
- 404: not found / not yours / in trash.

### `DELETE /api/convs/{cid}`

Soft delete (move to trash).

- 200: `{"ok": true}`

### `POST /api/convs/{cid}/restore`

Restore from trash.

- 200: `{"ok": true}`

### `DELETE /api/convs/{cid}/permanent`

Hard delete — also removes all messages.

- 200: `{"ok": true}`

### `PUT /api/convs/{cid}/skill`

Change the active skill for a conversation.

Body: `{"skill": "serenity"}`

- 200: `{"ok": true}`

### `POST /api/convs/batch-delete`

Soft delete multiple at once.

Body: `{"ids": ["id1", "id2", ...]}` (max 200)

- 200: `{"ok": true, "count": N}`

### `GET /api/trash`

List soft-deleted conversations.

- 200: `[{"id": "...", "title": "...", "skill": "...",
  "deleted_at": 1717100000}]`

## Chat

### `POST /api/chat/stream`

Stream an AI response using Server-Sent Events. A per-run `research` event
precedes `{"pulse": "start"}`, followed by `text`, `tool`, `think`, `ref`,
optional `status` and `platform_call` events, and finally `[DONE]`.

Body: `{"question": "...", "skill": "prism-research",
"conv_id": "...", "model": "minimax-m3", "extra_skills": []}`

- 200: `Content-Type: text/event-stream`
- 400: empty question.
- 422: malformed body.
- 429: rate-limited.

`{"research": context}` carries `schema_version`, `run_id`, `as_of` (the
actual request timestamp with a UTC offset), `timezone: "Asia/Shanghai"`,
`date`, and `engine`. The assistant message persists both `research` and a
`{"t":"research","research":context}` chunk. This is the research cutoff,
not a claim that all underlying data is current to that time.

Sources remain individual `{"ref": source}` events / `{"t":"ref", ...source}`
chunks. In addition to `url`, `title`, and `snippet`, they may include
`run_id`, `tool`, `tool_call_id`, `source_kind`, `evidence_status`,
`retrieved_at`, and `published_at`. `external_web`, `platform_data`, and
`historical_research` distinguish provenance. `retrieved` means successful
collection, **not verified factual accuracy**. Unknown publication or
collection dates remain null / absent; do not substitute the current date.
Historical sources without reliable run information are not reassigned to
the latest response.

`{"platform_call": record}` reports actual platform tool execution. Updates
with the same `call_id` replace the previous running state. Persisted chunks
use `{"t":"platform_call","call":record}`. `{"status":"..."}` carries visible
operational notices, including fallback from a missing external skill to
the built-in `prism-research` method; it is not model reasoning.

### Platform capabilities and call history

These read endpoints require the normal authenticated session and return
only records owned by the current user.

| Endpoint | Result |
|---|---|
| `GET /api/capabilities` | `{"capabilities":[...]}` — actual Agent tool catalog |
| `GET /api/convs/{conv_id}/capability-calls` | `{"calls":[record]}` for an owned conversation |
| `GET /api/capability-calls?view=market&limit=20` | `{"calls":[record]}` filtered by workspace; `view` is optional |

Each catalog entry includes `id`, `label`, `description`, `input_schema`,
`source_kind`, `read_only`, and `target_view`.

Each call record includes `call_id`, `run_id`, `capability_id`, `label`,
`status` (`running`, `completed`, or `failed`), `conv_id`, `created_at`,
`completed_at`, `input_summary`, `result_summary`, `source_kind`, and
`target: {"view": "...", "id": "...", "ticker": "..."}`. Optional target
fields are omitted when they do not apply. Views are `market`, `industry`,
`indicators`, `screener`, `reports`, `analysis`, and `vibe`.

Capability results link to existing records; **navigation does not execute
the capability again**. `analysis_start`, `vibe_start`, and `scan_start`
create tasks and advertise `read_only: false`. A completed *start call*
means that a task was accepted, not that the underlying research finished;
open the task or use its status capability to inspect progress. Platform
call records are not original external evidence.

The Agent uses a per-run stdio MCP bridge to the authenticated internal
capability endpoints. These are not browser-facing execution APIs. The
bridge base is `PRISM_INTERNAL_API_URL` (default
`http://127.0.0.1:8088`), which must point to the same Prism instance using
a numeric loopback host and explicit port. Custom service ports require
updating this variable; external hosts and redirects are rejected.

### `POST /api/chat/respond`

Answer a Claude `AskUserQuestion` prompt.

Body: `{"conv_id": "...", "answer": "..."}`

- 200: `{"ok": true, "message": "回答已发送"}`
- 404: no process running for that conversation.

### `POST /api/chat/cancel`

Kill the running Claude process for a conversation.

Body: `{"conv_id": "..."}`

- 200: `{"ok": true|false}` (false if no process was running).

## Reports

### `GET /api/convs/{cid}/reports`

List all generated report files for a conversation.

- 200: `{"reports": [{"filename": "...", "size": N, "mtime": N,
  "kind": "html|pdf|csv|md|json"}]}`

### `GET /api/reports/{cid}/{filename:path}`

Serve a single report file. Filename must be a plain basename
(no `..`, no `/`, no NUL). File is capped at 50 MB.

- 200: file content with appropriate MIME type.
- 400: bad filename or unsupported extension.
- 401: redirect to `/login` if no session.
- 404: not found.
- 413: file too large.

## Analysis (v0.10+)

Astock 7-agent LangGraph pipeline. Launched from the "一键分析" button in
the market panel.

### `POST /api/analysis/start`

Launch a new analysis for a ticker.

Body: `{"ticker": "600519", "name": "贵州茅台"}`

- 200: `{"analysis_id": "a1b2c3d4e5f6", "status": "started"}`
- 400: ticker missing.
- 500: engine startup failure.

### `GET /api/analysis/{analysis_id}/stream`

SSE event stream for analysis progress. Each event is `data: {json}\n\n`.

Event types:

| Event | Shape |
|---|---|
| `heartbeat` | `{"type": "heartbeat"}` (every 500ms while running) |
| `stage` | `{"type": "stage", "stage": "analysts", "progress_pct": 40, "elapsed": 12.3}` |
| `agent_done` | `{"type": "agent_done", "agent": "market", "node": "Market Analyst", "elapsed": 15.1}` |
| `done` | `{"type": "done", "report": {...}, "signal": "BUY", "elapsed": 180.5}` |
| `error` | `{"type": "error", "message": "...", "elapsed": 45.0}` |

Stages (in order): `analysts` → `debate` → `research` → `trader` → `risk` → `decision`.

### `GET /api/analysis/{analysis_id}/report`

Fetch the completed analysis report.

- 200: `{"id": "...", "ticker": "600519", "name": "贵州茅台", "status": "done",
  "signal": "BUY", "report": {"analyst_reports": {...}, "investment_debate": {...},
  "trader_plan": "...", "risk_debate": {...}, "final_decision": "..."},
  "elapsed": 180.5}`
- 404: analysis not found or still running.

## Industry Graph (v0.10+)

Industry chain knowledge graph with geo-distribution. Accumulates over time
as each analysis feeds new nodes/edges.

### `GET /api/industry/graph`

Query nodes + edges with optional filters.

Params: `ticker`, `category`, `country`, `q` (search), `depth` (1-5, default 2),
`min_confidence` (0.0-1.0, default 0.2).

- 200: `{"nodes": [...], "edges": [...], "total_nodes": N, "total_edges": M}`

### `GET /api/industry/geo`

Geo-region data for world-map rendering. Returns 12 countries with key industries
and per-country node counts.

- 200: `{"regions": [{"country_code": "CN", "country_name": "中国", "continent": "Asia",
  "key_industries": ["稀土","光伏",...], "node_count": 42}, ...]}`

### `GET /api/industry/search?q={query}&limit={n}`

Fuzzy search on industry node names. `q` required, `limit` 1-100 (default 20).

- 200: `{"results": [{"id": "...", "name": "...", "category": "...", "confidence": 0.8}, ...]}`

### `GET /api/industry/ticker/{ticker}/chain`

Upstream/downstream chain for a specific ticker.

- 200: `{"ticker": "600519", "nodes": [...], "edges": [...]}`

## Technical Indicators (v0.10+)

Computed server-side via pandas-ta. No TA-Lib required.

### `GET /api/indicators/{ticker}`

Params:
- `expr` (required): pipe-separated indicator expressions. Example:
  `macd(12,26,9)|rsi(14)|sma(20)|boll(20,2)`
- `period` (optional): `1d|1w|1m`, default `1d`.
- `limit` (optional): 1-500, default 120.

- 200: `{"ticker": "600519", "period": "1d",
  "results": {"macd(12,26,9)": {"macd": [...], "signal": [...], "histogram": [...]},
  "rsi(14)": {"rsi": [...]}}, "timestamps": [...]}`
- 400: invalid expression or period.

Supported indicators: `sma`, `ema`, `macd`, `rsi`, `boll` (Bollinger Bands),
`kdj`, `atr`, `obv`, `cci`, `willr`, `mfi`, `adx`, `stoch`, `vwap`, `ichimoku`, etc.

Results are cached per-ticker for 60s.

## Utility

### `GET /api/models`

- 200: `[{"id": "...", "name": "..."}]`

### `GET /api/skills`

- 200: `[{"id": "...", "name": "...", "shortcut": "..."}]`

### `POST /api/explain-term`

Generate a brief AI explanation for a term.

Body: `{"term": "HBM4", "context": "optional, ≤ 200 chars"}`

- 200: `{"term": "...", "explanation": "...", "cached": true|false}`

## Market data (v0.6+)

Quotes, K-line, fundamentals, and search are powered by a pluggable
provider chain (akshare / baostock) with a SQLite TTL cache. All
endpoints require authentication and are GET-only.

Ticker format: `^[A-Za-z0-9.\-]{1,16}$` (6-digit A-share, 5-digit
HK, US alpha, etc.). Providers reject truly bogus tickers with 404.

### `GET /api/quote/{ticker}`

Real-time quote.

- 200: `{"ticker": "...", "price": 1680.5, "change": 12.3,
  "change_pct": 0.74, "open": ..., "high": ..., "low": ...,
  "prev_close": ..., "volume": ..., "turnover": ...,
  "timestamp": 1718700000, "source": "akshare", "stale": false}`
- 404: ticker not found by any provider.
- 503: `{"error": "no_provider_available", "detail": "..."}` when
  all providers fail and no cached value is usable.

### `GET /api/quote/{ticker}/kline`

K-line bars. Query params: `period` ∈ `1d|1w|1m|5m|15m|30m|60m`
(default `1d`); `limit` 1..500 (default `120`).

- 200: `{"ticker": "...", "period": "1d", "stale": false,
  "kline": [{"ts": ..., "open": ..., "high": ..., "low": ...,
  "close": ..., "volume": ...}, ...]}`
- 400: invalid period or limit.
- 503: no provider available.

### `GET /api/fundamentals/{ticker}`

Fundamental metrics (PE/PB/ROE/market cap, etc.).

- 200: `{"ticker": "...", "pe": ..., "pb": ..., "roe": ...,
  "market_cap": ..., "...": ..., "timestamp": ..., "source": "...",
  "stale": false}`
- 404: ticker not found.
- 503: no provider available.

### `GET /api/search?q={query}&limit={n}`

Search tickers by code or name. `q` is required (≤ 32 chars);
`limit` 1..50 (default 10).

- 200: `{"hits": [{"ticker": "600519", "name": "贵州茅台",
  "market": "SH"}, ...]}`
- No match: `{"hits": []}`.

### `GET /api/data-providers`

Health check for the provider chain.

- 200: `{"providers": [{"name": "akshare", "healthy": true}, ...]}`

### Cache & failover semantics

- TTL: `quote=60s`, `kline=300s`, `fundamentals=86400s` (configurable).
- Each cache key is per-provider: `quote:akshare:600519`,
  `quote:baostock:600519`, etc. — one provider's failure doesn't
  poison another's cache.
- On all-provider failure: chain returns the most-recent expired
  value with `"stale": true`. The router returns 200 with stale
  data, not 503, so the UI can degrade gracefully.
- Failed provider calls are themselves cached for
  `PROVIDER_FAIL_CACHE_SECONDS=30` to avoid hammering a broken
  endpoint.

## SSE event shapes (chat stream)

Each event line is `data: {json}\n\n`:

| Event | Shape |
| --- | --- |
| `pulse:start` | `{"pulse": "start"}` |
| text chunk | `{"text": "..."}` |
| tool call | `{"tool": "Bash", "n": 1, "d": "..."}` |
| tool = AskUserQuestion | `{"tool": "AskUserQuestion", "n": 1, "ask": "...", "opts": [...]}` |
| thinking | `{"think": "..."}` |
| reference | `{"ref": {"url": "...", "title": "...", "snippet": "..."}}` |
| error | `{"error": "..."}` |
| done | `data: [DONE]\n\n` |
