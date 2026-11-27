# Prism API Reference

## Portfolio preview and bounded linear futures risk (candidate)

`GET/PUT /api/preferences/portfolio` keep owner binding, CSRF and optimistic
`expected_version`. Optional `futures_equity` is a decimal string distinct from
cash. `share_with_agent` defaults to false; when enabled, only the current risk
summary is frozen for new owned Native runs, without per-position rows.

Security positions retain their existing contract. Futures positions require
`asset_class: futures`, a concrete `SHFE/INE/DCE:PRODUCTYYMM` code, integral
positive `quantity`, decimal `price` and `multiplier`, CNY, an explicit
`price_as_of` instant, and `side: long|short`. Optional
`broker_margin_rate_pct`, `terms_as_of`, public HTTPS `source_url` and ISO
`last_trading_date` support margin/date checks; missing inputs remain unassessed.
Responses include existing rules plus optional bounded `scenarios`.

`POST /api/preferences/portfolio/import-preview` accepts closed JSON fields
`csv_text`, `currency`, and optional explicit `price_as_of` fallback. It requires
the same authentication, account binding and CSRF, parses at most 64KiB/100 rows
in memory, and returns `positions`, `counts`, `ignored_columns`, and
`input_basis: user_supplied_preview`. No file or portfolio is saved. The caller
must preview and then use the existing versioned PUT to persist. Unsupported
columns are excluded; malformed or ambiguous rows reject the entire preview.
Full field names, meanings and limits are in [the portfolio guide](portfolio-risk.md).

## DoubleH

Signed-in users enter at `/` and open research at `/prism`.
`GET /api/platform` lists Prism as the available product.
`/knowledge?id=...` opens the claim in the Prism research library.
`/preferences` opens research preferences and portfolio checks within Prism.

## Native research chat

Research chat uses Prism's owned direct HTTP runtime. The entire application prohibits Claude Code CLI execution or fallback; external Skills and retired MCP adapters are not execution backends. This reference describes repository source, not an instance's deployment state. Implementation and migration limits are in [Native Agent](native-agent-framework.md) and [Project Guide](PROJECT_GUIDE.md).

- `GET /api/agent/workflows` and `GET /api/agent/tools`: authenticated native method/tool catalogs.
- `POST /api/agent/chat/stream`: authenticated, CSRF-protected research start. Provide `question` and an existing owned `conv_id`; a stable `request_id` deduplicates the same input. Optional `model` and one `workflow` select the configured model and method. Reusing an identifier with different input returns 409.
- Conversation creation and Native research start also accept `X-Prism-Account` as an optional browser context fence. A value that differs from the authenticated account returns 409 before creation or model admission. It is not a login credential; clients without the header still require the same authentication and record ownership.
- `GET /api/agent/runs/{run_id}` and `/events?after_seq=N`: owned status and ordered persisted events.
- `GET /api/agent/runs/{run_id}/stream?after_seq=N`: observe/replay only; this does not start or repeat model work.
- `POST /api/agent/chat/cancel`: provide owned `conv_id` and optional `run_id`. `cancel_requested` is intent; `stopped` is true only after a terminal state.

SSE frames carry `agent.run_id`, `agent.seq`, `agent.status` and `agent.event`. Readers deduplicate sequence numbers and reconnect with GET. Browser refresh checks owned history before attaching to the existing run. Execution `completed` is distinct from research `unreviewed`; collection and historical reuse do not certify facts. `/api/chat/stream` and `/api/chat/cancel` only alias Native behavior. No environment flag enables the old runner. `/api/chat/respond` returns 409 and asks the user to send a new message.

Owned conversation history reads platform SQLite only. `history_migration.status` distinguishes `imported`, `pending_import` and `not_required`; pending import still exposes existing platform messages. The explicit offline importer is not an API or startup job and never restores a CLI runtime dependency. The retired Vibe feature has been removed from the application. Its routes (including the former `GET /api/vibe/plans` history endpoints), frontend assets and write paths no longer exist. Historical `vibe_plans` rows are retained in the database for compatibility and are no longer read or written by the code.

Endpoints are JSON over HTTP unless a stream or file is documented.
Authentication is via a session
cookie (`token`) issued by `/api/login`. The cookie is `HttpOnly`
and `Secure` when configured; `SameSite=strict` by default.

## Feedback (candidate)

Private user feedback and administrator review, not an execution/deployment API.
All writes require the existing authenticated CSRF protections; unknown JSON
fields and duplicate keys are rejected, with a 32 KiB request-body limit.

| Endpoint | Access and behavior |
|---|---|
| `POST /api/feedback` | Signed-in user; `category` (`bug` / `suggestion`), `title` (1–120), `description` (1–6000), optional local `page`, closed-enum `feature`, `repair_scope` and UUID `request_id`. Scope defaults to `unknown`; known scopes must match the feature. Returns `ok`, `reused`, `feedback`. |
| `GET /api/feedback` | Own records only; optional `category`, `status`, `feature`, `before_id`, `limit` (default 50, capped at 100). Returns `items`, `next_before_id`. |
| `GET /api/feedback/{id}` | Own detail and user-visible update history, or 404. |
| `GET /api/admin/feedback` | Administrator queue; same filters plus Shanghai `review_batch_date` (`YYYY-MM-DD`). |
| `GET /api/admin/feedback/{id}` | Administrator detail, including owner and review actors. |
| `POST /api/admin/feedback/{id}/update` | Administrator; `status`, nonempty `public_reply`, optional `verification`, `target_version`, full-40-hex `target_revision`, `expected_revision`. Appends history; stale expected revision returns 409. |

Bug submissions start at `triaging`; suggestions at `pending_review` with a
Shanghai-day batch. Category-specific status sets are defined in
`server/feedback.py`. `fixed` requires at least 12 characters of verification
and a version such as `v0.21.0`; this records the maintainer's claim, not proof
of deployment. Replies are visible to the submitting user, not a public issue.

Stored page context contains only the site path, without query/fragment.
Fixed credential patterns are redacted and common private-key blocks rejected;
this is not exhaustive secret detection. No conversation body or attachment
is collected automatically. Creation is limited to six requests/minute/user;
administrator updates to 60/minute. Same owner, UUID and cleaned content reuse
one record; conflicting content returns 409 without replacing the original.
Feedback content never grants tool, code execution or release authority.

### Restricted feedback worker

These candidate endpoints are disabled unless a dedicated
`PRISM_FEEDBACK_WORKER_TOKEN` is configured. They authenticate only the bounded
`X-Prism-Feedback-Token` header; browser cookies and administrator sessions do
not grant this identity. Keep its value out of source, prompts and logs.

- `GET /api/internal/feedback`: queue filters as above, plus `review_batch_date`;
  owner and request identifiers are removed.
- `GET /api/internal/feedback/{id}`: limited detail/history, with review actors
  represented as worker or administrator rather than account names.
- `GET /api/internal/feedback/diagnostics`: finite derived diagnostic fields;
  no raw messages, stacks, account names or request context.
- `POST /api/internal/feedback/{id}/update`: same bounded update fields, mandatory
  `expected_revision` and fixed actor `system:feedback-worker`. `fixed` also
  requires `target_version` to equal the running `APP_VERSION` and
  `target_revision` to equal its full deployed revision, plus verification text.

Only the exact numeric-ID worker POST route uses this dedicated authentication
instead of browser CSRF. Authentication is rechecked after reading the body;
rotation/disable during a request rejects its write. The worker cannot access
other APIs, execute a repair or deploy a release. Matching release fields are
not proof of real verification; task activation and production acceptance are
separate from implementing these endpoints.

### Suggestion-only review candidate

All four internal feedback roles also support fixed private token files selected
by the service operator through `PRISM_INTERNAL_TOKEN_DIRECTORY`. This explicit
mode has no environment fallback; roles and HTTP permissions are unchanged.
See `server/internal_tokens.py` for filenames, permissions and rotation. Request parameters cannot select a credential path.

`/api/internal/feedback-review` requires a distinct `PRISM_FEEDBACK_REVIEW_TOKEN`
via `X-Prism-Review-Token` (32–512 printable ASCII characters), with exactly one
`X-Prism-Expected-Revision` matching the full served revision. Monitor, worker and
review secrets must differ; shared values fail closed. Cookie-bearing requests
are rejected. Responses are `no-store`, authentication is limited per direct peer,
and mutating requests require the ordinary `X-Requested-With: XMLHttpRequest`.
No role is configured or cloud consumer started by default.

- `GET /queue?batch_date=YYYY-MM-DD&after_id=0&limit=10`: current Shanghai date
  only, due from 09:30; at most 10 pending suggestions from enabled accounts.
  Returns bounded redacted title/description, feature and item revision, without
  user identity, paths, history, bugs or diagnostics. Earlier pending batches
  are included. `next_after_id` paginates oldest first.
- `POST /{id}/decision`: strict JSON containing canonical UUID `request_id`,
  integer `expected_revision`, `batch_date`, `status` (`accepted`, `deferred`,
  `declined`, `needs_info`), concrete `reason` (4–1200 characters), `priority`
  (`low`, `normal`, `high`), up to 8 short `dependencies`, `risk`
  (`ordinary`, `owner_decision`) and a closed `topic`. Adoption for owner-only
  impact, email/SMS activation or cross-product private-content linkage is
  rejected. Fixed/in-progress/release status cannot be written by this identity.
- `GET /receipts/{request_id}`: exact durable decision receipt. A 404 cannot
  establish that an earlier POST never happened and must not cause automatic
  replay. Existing receipts can be read or replayed idempotently after their
  original batch; changed payloads using the same identifier conflict.

The item update, owner-visible history and receipt commit atomically after
rechecking active ownership, pending status, item version, due date and runtime
binding. New writes are capped at 200 per Shanghai day. Accepted replies state
that adoption enters planning and is not deployment. Deleted accounts/feedback
purge their receipts. No code, spending, sharing or release right follows from
an accepted suggestion. See the product principles for the user workflow;
the independently configured scheduler and model routing remain private ops.

### Restricted bug progress

`POST /api/internal/feedback-progress/{id}/update` uses a separate
`PRISM_FEEDBACK_PROGRESS_TOKEN` in `X-Prism-Progress-Token`, the current full
`X-Prism-Expected-Revision`, and `X-Requested-With: XMLHttpRequest`. Browser
cookies and tokens shared with monitoring, suggestion review or the legacy
feedback worker are rejected. The identity cannot read report text or accounts,
review suggestions, call models, mark a fix or deploy code.

The closed body contains canonical UUID `request_id`, integer CAS
`expected_revision`, the report's 12-hex `reported_revision`, and `stage`:
`triaging`, `reproducing`, `candidate_validated`, `validating`, `awaiting_review`,
`awaiting_release`, `needs_info`, `not_reproduced`, `blocked`, or `validation_failed`.
Public messages are fixed by the server; caller text and verification claims are
not accepted. Only active bugs from the current reported version are writable.
Closed, paused, disabled-account, stale-CAS and regressing stages are rejected.

`GET /api/internal/feedback-progress/receipts/{request_id}` returns an immutable,
bounded receipt for reconciliation. A 404 never proves that an uncertain POST
was not applied. Identical requests return the same receipt; the same unchanged
stage under a new UUID does not duplicate history or increment the item version.
Receipts include `changed`, `stage`, `revision_number`, `fingerprint`, and the
applied runtime revision; `fixed` and `deployed` are always false. Deleting the
account or feedback removes related progress and receipts. Actual fix closure
requires separate deployment and user-flow verification.

### Read-only feedback monitoring candidate

`GET /api/internal/feedback-monitor/queue` and
`GET /api/internal/feedback-monitor/diagnostics` use a separate
`X-Prism-Monitor-Token` header and `PRISM_FEEDBACK_MONITOR_TOKEN` configuration.
They are disabled by default. The token must be 32–512 characters without
whitespace, distinct from the feedback worker token. If both configured values
are equal, both identities fail closed. Cookies do not grant monitoring access;
the monitoring token cannot read full feedback, update its status, or access
administrator APIs. Responses are `no-store`. Authentication attempts are
bounded to 60/minute per direct peer; forwarded headers do not set this identity.

The queue exposes only numeric IDs/revision counters/times and validated
category, feature, status and release metadata. It does not select titles,
descriptions, user names, paths, replies, verification text or history. Diagnostic
signals contain fixed error codes/classes, counts and version scope; arbitrary
method names, raw errors, stacks and request context are omitted. Anonymous
access refusals, unmatched paths and explicit cancellation records are not in
the diagnostic kind allowlist. Signals, including those with the current version
label, still require reproduction; a version label does not certify an exact
deployed revision. These endpoints never change feedback or resolve diagnostics.

Both streams may include a `repair_scope` from the closed map in
[`server/repair_scopes.py`](../server/repair_scopes.py). Unknown scopes are omitted;
queue scopes must match `feature`. Diagnostics may additionally include a short
origin `revision`; receiving-server metadata does not establish that origin.
Legacy records and deferred outcomes without commit provenance are not backfilled.
A matching client-reported revision remains an untrusted reproduction hint, not
proof of loaded assets or permission to modify code.

Both accept `since` (inclusive Unix seconds, default 0), `after_id` (default 0)
and `limit` (1–100, default 50). Queue additionally accepts `category=bug` or
`suggestion` (default bug). Unknown, duplicate, invalid or future-time parameters
return 422 without echoing input. The response includes `schema`, `kind`,
`poll_started_at`, `next_after_id`, `items`, release metadata, `read_only: true`,
`signals_verified: false` and `user_text_included: false`.

To poll safely, retain the **first page's** `poll_started_at`, keep `since`
unchanged while following `next_after_id`, and advance the checkpoint only after
all pages succeed. The next cycle resets `after_id` to 0 and uses the retained
start time inclusively (optionally with an overlap). Deduplicate queue items by
`(id, revision_number)` and diagnostics by `(id, occurrences, last_seen)`; a
changed old item or a same-second update must be reconsidered. Do not advance on
an incomplete pass. Server-clock rollback requires replay from an earlier
checkpoint. This is a current-state feed, not a durable every-transition log;
deleted or resolved records need not appear, and a missing record does not prove
a fix. Large backlogs can be processed in bounded passes with checkpoints.

This is an application interface candidate, not an installed cloud consumer.
Production configuration, private client integration, identity provisioning and
deployed contract verification remain separate controlled work. Metadata alone
cannot support a reasoned review of a user's suggestion; do not claim a review
or an empty queue without authorized content access and a completed read.

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
  "version": "unknown",
  "deployed_revision": "unknown",
  "checked_at": 0,
  "tz": "Asia/Shanghai"
}
```

The example is illustrative, not an observed instance. `checked_at` is a Unix timestamp in seconds. The revision is a full
40-character hexadecimal commit identifier or `unknown`; missing or
invalid version metadata is also `unknown`. Unknown deployment identity
does not imply an unavailable database. Conversely, `ready` does not
verify model access, market data, authenticated user flows, production
identity, or a completed release. Deployment acceptance must separately
match the observed revision to the immutable candidate and verify the
required features. The Docker healthcheck uses this endpoint.

### `GET /api/admin/health`

Requires an administrator session. Returns local Native configuration
checks for the database, Native runtime entry points, tool catalog and
configured model endpoints/credentials, without their secret values.
It does not start a model, scan Skills or invoke a CLI. This snapshot is
not a live integration or release acceptance test.

### `GET /api/admin/operations`

Requires an administrator session. Returns read-only release observations
for the configured Prism repository: actual GitHub workflow states and
links, observation time/cache freshness, candidate-only delivery limits,
and metadata for this application instance. GitHub observations may be
cached for 180 seconds; failures or stale data must remain visible rather
than becoming a passing result. No GitHub token is exposed or required by
the public-repository observer.

This observation endpoint cannot authorize a deployment or recovery.
CI and candidate workflow results are not deployment records.
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

- 200: `{"user": "alice"}` + sets the `token` cookie.
- 403: wrong credentials.
- 422: malformed body.
- 429: rate-limited.

### `POST /api/logout`

- 200: `{"ok": true}`. Clears the `token` cookie.

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

Change the stored built-in research-method alias for a conversation; this does not load an external Skill.

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

Body: `{"question": "...", "workflow": "prism-research",
"conv_id": "...", "model": "minimax-m3", "request_id": "stable-request-id"}`

- 200: `Content-Type: text/event-stream`
- 422: empty question.
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
operational notices; it is not model reasoning. Methods are versioned built-in
prompts, with no external Skill loading or execution fallback.

### Platform capabilities and call history

These read endpoints require the normal authenticated session and return
only records owned by the current user.

| Endpoint | Result |
|---|---|
| `GET /api/capabilities` | `{"capabilities":[...]}` — platform catalog; the Agent allowlist is `/api/agent/tools` |
| `GET /api/convs/{conv_id}/capability-calls` | `{"calls":[record]}` for an owned conversation |
| `GET /api/capability-calls?view=market&limit=20` | `{"calls":[record]}` filtered by workspace; `view` is optional |

Each catalog entry includes `id`, `label`, `description`, `input_schema`,
`source_kind`, `read_only`, and `target_view`.

Each call record includes `call_id`, `run_id`, `capability_id`, `label`,
`status` (`running`, `completed`, or `failed`), `conv_id`, `created_at`,
`completed_at`, `input_summary`, `result_summary`, `source_kind`, and
`target: {"view": "...", "id": "...", "ticker": "..."}`. Optional target
fields are omitted when they do not apply. Views are `market`, `industry`,
`indicators`, `screener`, `reports`, and `analysis`.

Capability results link to existing records; **navigation does not execute
the capability again**. `scan_start` creates a task and advertises
`read_only: false`. `analysis_start`
remains a catalog entry with `available: false` and an `unavailable_reason`;
it cannot create tasks. A completed *start call*
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

Compatibility alias for the built-in Native workflow catalog. It does not scan disk or enumerate external Skills; use `GET /api/agent/workflows` for new clients.

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
(default `1d`); `limit` 1..500 (default `120`);
`adjustment` ∈ `none|qfq|hfq` (default `none`, unadjusted).
Provider fallbacks and caches must match the requested adjustment. Unsupported
adjusted sources fail explicitly rather than returning a different series.

- 200: `{"ticker": "...", "period": "1d", "stale": false, "adjustment": "none",
  "kline": [{"timestamp": ..., "open": ..., "high": ..., "low": ...,
  "close": ..., "volume": ..., "turnover": null, "adjustment": "none"}, ...]}`
- Missing turnover remains `null`, distinct from zero; it is not estimated from volume and close.
- 400: invalid period or limit.
- 422: invalid adjustment.
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

## Research preferences and futures

All endpoints require the signed-in owner. Writes retain CSRF checks and a
32 KiB closed JSON schema; unknown keys and duplicate fields fail. Optional
`X-Prism-Account` binds the page to its initial account and rejects a mismatch.

| Endpoint | Behavior |
|---|---|
| `GET/PUT /api/preferences/research` | Own explicit profile or `configured:false`; writes require `expected_version` |
| `GET /api/futures/products` | Fixed supported product catalog |
| `GET /api/futures/{symbol}/snapshot` | Bounded public daily observation with dates, calculations and gaps; 12 reads/minute/account |

On the JM, RB, M and EC detail pages, the signed-in account can research a question,
save a choice and return to its evidence. The following routes use the same
symbol throughout. Sessions are `current` or a fixed historical replay
(`2026-11-19` for JM, `2026-11-16` for RB and M, and `2026-11-17` for EC).

| Endpoint | Request and result |
|---|---|
| `POST /api/futures/{symbol}/research` | Send `question` and optional `session`. The result includes dated evidence and available choices. A draft with choices can be saved for one hour. |
| `POST /api/futures/{symbol}/judgments` | Send the returned `analysis_id` and `choice_id` to freeze a judgment. Repeating the same choice returns the saved record. |
| `GET /api/futures/{symbol}/judgments` | Lists this account's saved judgments for the selected `session`, with cursor pagination. |
| `GET /api/futures/{symbol}/judgments/{id}/review` | Uses the saved rule and evidence baseline. A pending result can be checked again when a matching observation becomes available. A final result is retained. |
| `DELETE /api/futures/{symbol}/judgments/{id}` | Removes the account's saved judgment for this symbol. |

JM records saved before the shared routes retain their original review rules.
RB inventory comparisons use successive weekly observations for the same survey
and sample. Terminal purchases are a separate series with a Shanghai scope.
M distinguishes soybean stocks, meal stocks and planned versus actual crushing.
EC tracks the published SCFIS Europe index in points. A futures contract's daily
settlement and its final settlement window are reported separately.

Profile fields: `style` conservative/balanced/aggressive, `horizon`
short/medium/long, integer `max_position_pct` 1-100, integer
`max_drawdown_pct` 1-50 and boolean `allow_leverage`. Each Native run captures a
stable profile version; current preferences never rewrite existing runs. Actual
holdings are not assumed and `portfolio_risk_status` remains `not_evaluated`.

The research workspace and Native `futures_snapshot`, `futures_inventory`,
`futures_basis` and `futures_spread` accept the same product codes.
Use `GET /api/futures/products` for the catalog and
`GET /api/futures/panel/catalog` for supported dimensions. DCE continuous
series come from the existing Sina daily K-line path because the DCE site
rejects automated requests; M/C/I/P warehouse inventory comes from a public
99期货 series (see `futures_inventory`). Its public cache retains
collection time. Continuous series are not executable contracts; missing
fundamentals and capital flow are explicit. The research Agent has no trading tools. Feedback accepts `futures` and
`preferences` among its registered feature names.

The read-only `futures_inventory` tool accepts all registered
products plus an optional `date` (`YYYY-MM-DD`). It returns the product's
warrant quantity, daily change, source URL and data date from the official
exchange warrant report (SHFE/INE 2016 onwards, CZCE 2017 onwards). DCE
M/C/I/P instead read the public 99期货 warrant series, which is labelled as a
secondary source, keeps the disclosed lot unit (`unit: "手"`) without tonne
conversion, and publishes no same-day change, so `change` is the difference to
the previous disclosed row (`change_basis: delta_vs_previous_disclosed_row`).
Products without a mapped physical warrant series return an explicit
`inventory_unavailable` with a reason; quantities are never estimated,
aggregated or filled with zero. Collected
observations become an `original_obtained` market-data snapshot document
through the existing evidence ledger, keyed by product and data date.

`futures_spread` takes one covered product and one trading day and returns that
day's nearest and farthest priced listed delivery months, their settlement
prices and `far_minus_near`, together with the listed-contract calendar. It only
works for the SHFE/INE products with a mapped listed-contract calendar; other products, missing calendars, missing settlement reports or
fewer than two priced months return `status: spread_unavailable` with a
`reason_code` and no number. DCE M/C/I/P explicitly return
`contract_months_not_covered`: the DCE site answers 412 and the alternative
sources do not publish a verifiable complete listed-contract calendar, so no
near/far settlement pair is fabricated from same-day quotes. The observation is rendered into owned pages and
recorded as an original market-data document, so a reported spread figure goes
through the existing claim locator. It is a same-day settlement difference, not
an executable spread quote, a spot basis or a trading recommendation.

The read-only `futures_basis` tool takes one registered product and an optional
`date` (`YYYY-MM-DD`) and returns that day's spot price, main-contract futures
price and `basis` (`spot_price - futures_price`) from the public 100ppi
spot-versus-futures table, with the data date, source URL, `basis_direction`
and the source's published difference (used only as a consistency check). The
first response is a `HW_CHECK` JavaScript check page; its cookie value is
extracted and the same address is requested again. An unsolvable check page
returns `reason_code: challenge_unsolved`, a missing data date returns
`report_not_found`, a product absent from that date returns
`product_not_listed`, and a spot/futures pair whose quoted units disagree
returns `source_price_mismatch`; glass is currently in that last state because
its spot is quoted per square metre while its futures price is per tonne. Products without a mapped comparable row return `symbol_not_covered`. An
unavailable result carries no prices, is never estimated, unit-converted or
substituted from another date. Collected observations become original
market-data documents through the existing evidence ledger with access method
`futures_basis`, keyed by product and data date.

The read-only `futures_spot_history` tool takes a product code or continuous
contract plus required `start_date`/`end_date` (`YYYY-MM-DD`) and returns the
daily spot-price series that the same 100ppi spot-versus-futures table printed
for that product, reusing the `futures_basis` `HW_CHECK` flow and parser. Each
observation is exactly `{date, price, unit, source, access_method}` in the
product's futures quote unit; only dates the source actually lists are
returned, so non-trading days, unpublished dates and unrecognised pages are
omitted rather than interpolated or zero-filled. Units are explicit per
product (gold 元/克, silver 元/千克, glass 元/平方米 converted by the
source-page rule `元/平方米*80=元/吨`); every converted price is checked
against the same row's main-contract futures price and a ratio outside
`[0.5, 2]` fails as `unit_unverified` instead of mixing units silently. The
measured coverage is 24 of 33 products (2013-01-04 onward; longest series
3,347 observations), and a series with fewer than 250 observations or a
first-to-last span under 365 days returns `backtest_ready: false` with
`insufficient_observations`/`span_too_short`. Ranges longer than 1,000 days or
720 weekdays return `range_too_large`; before 2013-01-04 returns
`range_before_source_history`; a dated contract such as `CU2701` returns
`contract_not_supported`; uncovered products return `symbol_not_covered`; a
failed day aborts the whole read as `spot_history_unavailable` with no partial
series. Collected series are rendered into owned pages and recorded as an
original market-data document with access method `futures_spot_history`, so a
reported spot figure goes through the existing claim locator.

Futures sources are independent: unavailable continuous prices leave `latest`
and technical values null, without discarding collected exchange or warehouse
reports. `coverage` describes the market-data components, `status: partial`
retains usable components for 30 seconds, and fully collected market components
use a 300-second cache. This status does not certify fundamental research or
portfolio suitability. No source collected returns 503 with
`X-Prism-Data-Status: unavailable` and `Retry-After: 30`, without caching a result.

For RB and SC, `fundamental_supply` reads at most three fixed NBS release-index
pages and two distinct monthly industrial releases, within six seconds and a
1 MiB limit per body, without authentication, redirects or automatic retries.
RB selects national pig iron, crude steel and steel products; SC selects crude
oil production and refinery throughput. `observations` retains each release's
period, publication date, body digest, monthly/combined-period amounts,
year-to-date amounts where reported and official year-on-year percentages.
Identical desktop/mobile tables count once; conflicting tables, units, dates
or missing metrics reject that release. The series describes national
enterprises above designated size, not deliverable output, inventory, operating
rates or global supply; throughput is not refined-product output. Missing
coverage is not filled from aggregates or other products. Monthly
periods remain separate from the daily `date_alignment` comparison.

For AU, the same `fundamental_supply` component discovers up to two distinct
cumulative periods from at most three fixed China Gold Association statistics
index pages. Requests share a six-second deadline, 1 MiB bodies and the same
no-authentication, no-redirect and no-retry boundaries. The parser verifies the
article's source credit, publication date, period and headline/body agreement.
`series_kind: gold_year_to_date_production_consumption` retains national domestic
and imported raw-material production, their reported total, consumption and its
three reported categories in tonnes, with original signed year-on-year changes.
`period_kind: year_to_date` always starts on January 1; figures are neither
differenced into quarters nor forced to balance after rounding. Imported
raw-material production is not customs bullion imports; these statistics do not
establish global supply, deliverable gold, inventory, basis or capital flows.
Each article's digest and original URL remain available. Cumulative periods do
not enter the daily date-alignment comparison. CU fundamentals remain uncovered.

Official reports start from the server's Shanghai calendar date, independently
of a missing or stale secondary price feed. Only an actual dated HTTP 404 permits
a query for the preceding date, at most three distinct calendar dates per
source within the shared 15-second budget. Network, TLS, access, rate-limit and
schema errors do not trigger earlier-date queries. This bounded window is not
a trading calendar or a guarantee of the latest published report. SHFE/INE
parsers check the report's actual date; collected reports are retained while
another source checks an older date. `source_attempts`, `observation_dates`,
`report_date_basis: bounded_calendar_window` and `date_alignment` preserve this
distinction. A mixed-date snapshot must not be treated as a same-day comparison.

For M, the DCE public protocol uses fixed HTTP POST endpoints with no credentials,
cookies, redirects, access-challenge handling or automatic retries. This path is
retained only to report the challenge: M warehouse inventory now reads the
public 99期货 series (unit `手`, `source.kind: secondary_warehouse_series`). The
protocol's metadata retains `transport_authenticated: false` and
`original_verified: false`. Echoed
dates must match; when none is provided, `date_provenance: request_parameter`
identifies a query date rather than a confirmed report date. Those components
appear in `observation_date_gaps`, are excluded from `observation_dates`, and
cannot establish `date_alignment: same_day`. HTTP 412 is
`automated_access_restricted`, not a missing report or permission to query an
older date. The 412 observed on 2026-11-22 from two independent networks is an
interactive anti-automation challenge page; the adapter inspects only a bounded
prefix of that response and reports `blocked_by: anti_automation_challenge`
without echoing upstream script, HTML or cookies. It does not execute or solve
the challenge, retry, or substitute unverified data. Warehouse totals retain the
reported lot unit; member rankings are not covered by this protocol. This
adapter does not expand portfolio repricing coverage or certify live DCE data
availability. Per-variety source availability is recorded in
[futures data sources](FUTURES_DATA_SOURCES.md).

For the supported SHFE/INE calendars, `contract_dates` and `exchange_parameters` independently read
SHFE's combined dated business reports. The first returns per-contract
`listed_on`, `listed_expiry_date`, `delivery_start` and `delivery_end`; the
second returns separately named speculative/hedge long/short margin percentages
and upper/lower limit percentages. Missing ratios remain null; special limits
set `additional_limit_notice_required`. Both retain source URL, body hash and
actual report/update dates. These are exchange observations, not confirmed
current broker requirements or delivery eligibility. `calendar_adjustments_status`
remains unassessed: the published calendar excludes holiday adjustments. Neither
source updates portfolio records. New CZCE/CFFEX/GFEX products do not read
contract-date or exchange-parameter reports; those components stay `not_covered`
for them. Details are in [futures research](futures-research.md).

Error metadata is a finite code and optional HTTP status, never upstream error
text, response bodies or headers. Cancellation propagates without starting more
source requests or caching an unfinished result.

## v0.23.0 completion work (unreleased)

### Portfolio repricing (unreleased)

`PortfolioUpdate` adds `allow_market_pricing` (default false) and
`futures_equity_basis` (`unverified` or `marked_to_reference_prices`). The marked
basis requires futures equity and reference prices at the snapshot instant.
Mainland code aliases cannot be used to split the same security into separate
concentration buckets; new duplicate holdings are rejected.

`POST /api/preferences/portfolio/reprice` takes only `expected_version` and needs
session, CSRF and matching account identity. The saved permission is checked
before any source request and again before returning; portfolio/profile changes
conflict. It returns a no-store, read-only estimate with `positions`, actual
market dates/bases, missing coverage, preserved assumptions and deterministic
`risk`. It does not replace saved prices, balances or history. It uses at most
four concurrent source tasks and ten seconds of caller waiting per batch, at most six batches per
account per minute. Already-started blocking work remains owned by the lifecycle
manager after a caller timeout; timeout is not proof that it stopped. Missing/expired/ambiguous prices are unavailable, not zero;
partial coverage cannot silently mix new quotes and old inputs into a passed
portfolio assessment. Unknown broker rules remain not evaluated.

The Native-only `portfolio_risk` tool takes no arguments. Both the run's initial
sharing permission and the current matching portfolio version are required,
in addition to market-pricing permission. It returns financial summary amounts
and rules without asset IDs, quantities or per-position URLs. The first result,
including failed/unknown acquisition, is fixed for the run; a new call ID cannot
restart it. Cancellation and current authorization are checked before starting
additional sources and before returning. See [portfolio risk](portfolio-risk.md).

### Research evidence

Native runs may include one isolated-context excerpt review within the existing
research model-call, output, time and daily quotas. `model_review` on the run,
research read and frozen revision records finite status, packet/method digests,
assessments bound to real claim/passage IDs, usage completeness and unreviewed
claim count. Knowledge details retain an advisory per claim without changing
human review state. No browser/model tool can invoke the review writer or grant
it permissions. See [excerpt review](research-quality.md) for bounds and semantics.

Research reads, claim registration, structured historical reads and knowledge
details include live `history_checks`. It compares explicit owner-scoped numeric
signatures before the bounded conflicting-result window: at most 60 current
claims, 80 historical differing claims and 20 conflict groups, with a scan-time
limit. Matching subjects, metric, accounting basis, closed period, dimension and
currency are required. Ineligible reviews and deleted conversations are excluded;
unreadable data, timeout and truncation keep `coverage_incomplete` visible.
`conflicts` contains current/history claim IDs, normalized values and original
research targets. It never certifies meaning, source independence or correctness,
and never rewrites a frozen revision. Same values do not constitute verification.

Industrial `evidence_graph.synthesis` supplies a five-dimension collection and
analysis brief for the existing Native tools. It covers the current relation
window only, retains missing dimensions and never reports research complete.
Only eligible supply hypotheses form material-flow paths; commercial stages and
invalidators remain explicit, and substitution/cost/certification/demand relations
are not material-flow edges. Historical numeric differences block reuse of the
affected support until the evidence is reconciled; incomplete comparisons stay
visible as review status, without inventing a conflict.

Graph evidence retains each claim's `model_review` advisory. Relation summaries
include reviewed, unreviewed, incomplete and flagged support IDs, even when the
compact response omits that claim's detail. Negative or partial support prevents
automatic path reuse and directs the existing Agent to check the cited excerpts;
it does not change human review state. Missing/unfinished reviews remain in
`review_gaps`, including on path hypotheses. Positive advisory never establishes
semantic verification, source independence or a commercial relationship.

These additions are development contracts, not proof of a deployed version.
All private results are owner-scoped and must not be cached. Mutations require
the existing CSRF protection and reject account switches.

| Endpoint | Behavior |
|---|---|
| `GET/PUT /api/preferences/portfolio` | Own explicit security positions, currency, cash/debt and observation times; writes compare `expected_version` and return deterministic risk rules |
| `GET /api/knowledge/items?query=...&before=...&limit=30` | Search all accessible persisted structured claims with bounded cursor pagination |
| `GET /api/knowledge/items/{id}` | Claim, source passages, review history and affected owned research |
| `PUT /api/knowledge/items/{id}/review` | `expected_version`, state, note, optional correction link; reviewed claims require a future validity time, incomplete citations cannot be promoted |
| `GET/PUT /api/admin/model-routes[/{purpose}]` | Administrator-only `research` model selection and bounded request/token/time quotas; only registered model IDs, no key material |
| `GET /api/industry/research?q=...&before=...&limit=20` | Owned structured relationship candidates, original source excerpts, supporting/opposing claim review state, finite transmission hypotheses and cursor pagination; background seed edges excluded |

Application model settings cover investment research. Historical
`work_planning`, `feedback_triage`, `suggestion_review` and `validation` settings
and usage records are retained. These purposes cannot be saved, resolved or
newly reserved. Editing a model route does not refund unknown, failed or
reserved calls. Cloud triage, suggestion review and engineering use the private
operations console and its own model budget. Native release model acceptance
requires its separately reviewed finite permit.

Portfolio decimals are strings, not binary floats. At most 100 unique securities
are accepted. Quotes older than seven days, future observations and mixed
currencies without FX are unevaluated. Optional `equity_peak`, `peak_as_of` and
signed `net_flows_since_peak` enable drawdown against a supplied high-water mark;
these user-supplied values are not independently verified prices or NAV history.
The profile endpoint's older `portfolio_risk_status` is not a risk certificate;
the portfolio response contains actual version-bound rule results.

Public PDF reads retain the same DNS/TLS/host policy. Bodies are capped at 8 MiB;
an isolated Python parser has CPU/memory/time limits, no inherited credentials,
and is killed and reaped on timeout while its parent auxiliary work remains
owned. The default excerpt covers at most ten pages and 32,768 characters. Page
numbers, total pages and body hash accompany the excerpt. Encrypted, scanned,
invalid and timed-out documents retain explicit gaps; PDF metadata is not used
to invent a publication date. This parser is not a CLI Agent or a model fallback.

Futures snapshots include `exchange_daily` for mapped SHFE, INE, CZCE, CFFEX and GFEX products. Exact-date official contract
reports include `contracts`, `active_contract` (largest daily reported volume),
quote unit, a two-contract settlement `curve`, and separately ranked
`member_positioning` columns for the SHFE and INE adapters. Their original report
URLs and body hashes are retained. The CZCE text report, CFFEX GBK CSV and GFEX
POST JSON adapters are fixed-address and bounded; CFFEX is reachable over plain
http only, GFEX needs the adapter's application User-Agent and an EdgeOne
challenge page is reported as `automated_access_restricted` without executing or
echoing it. The three new adapters cover daily contracts only: member rankings,
warehouse receipts, contract dates/parameters and production fundamentals stay
`not_covered`. Missing/invalid data stays unavailable; it does not invalidate
the continuous bars. A far-minus-near price difference is not spot basis. The
ranking samples differ across columns and dates; they do not establish net
capital flow, whole-market direction or personal portfolio suitability.

Relationship traversal forms at most 30 acyclic two/three-link hypotheses per
bounded page. Product/period overlap, capacity, substitution and downstream
orders still need semantic review. Revoked/expired/disputed/replaced supports,
missing source passages and opposing evidence block automatic path reuse. The
Agent receives a compact summary; original owned research retains full detail.
Legacy history retrieval filters all owned rows before a maximum matching window
of 200 reports and 200 research versions. It ranks only visible public-answer
text and preserves owner/deleted-conversation checks; it is not an unlimited
ranking index or external fact certification.
