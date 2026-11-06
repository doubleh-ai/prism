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

## DoubleH and personal Work

`/` is the shared authenticated product entrance; `/prism` opens research. Work,
projects, calendar and reminders use `/work`, `/projects`, `/calendar` and
`/notifications`. `GET /api/platform` returns product availability only, with no
research or personal Work payload. Existing accounts remain valid.

- `GET/POST /api/work/projects`: list owned and accepted shared projects, or create an owned project. Creation requires
  a UUID `request_id`, `name` and optional `description`.
- `PATCH /api/work/projects/{id}`: update with a new `request_id`,
  `expected_version`, `name`, `description` and `active/completed/archived` status.
  Stale versions return 409. Repeated identical request IDs replay the result.
- `GET/POST /api/work/projects/{id}/tasks`: list project tasks or create one using
  the regular Work task schema. Creation requires an active project and provisions the owner workspace if needed; task and project association commit atomically.
- `GET /api/work/projects/{id}` and `/{id}/tasks/{task_id}`: current permitted
  project and linked task only. `PATCH /{id}/tasks/{task_id}` uses `UpdateTask`
  with `expected_version`; it does not accept a mutation UUID. Owners and accepted
  editors can edit linked tasks, viewers can read. Task list uses `limit` (up to
  200), `before`, `next_cursor` and `truncated` rather than silently dropping rows.
- `GET /api/work/projects/{id}/members`, `POST /{id}/invitations`,
  `POST /{id}/invitations/{invitation_id}/revoke`, `PUT /{id}/members/{username}`,
  `POST /{id}/leave` and `GET /{id}/activity`: in-app project collaboration.
  `GET /api/work/invitations` and `POST /api/work/invitations/{id}/respond`
  belong to the recipient. Invites expire after seven days and grant no access
  before acceptance. Roles are `viewer` or `editor`; member removal uses
  `role: removed`. Writes use UUID `request_id`; existing invitation/member
  mutations require their current `expected_version`. Version numbers remain
  monotonic across leaving and rejoining.
- `GET/PUT /api/work/projects/{id}/schedule`: versioned project calendar. PUT
  requires `request_id`, current schedule `expected_version`, explicit `start_at`,
  IANA `timezone`, `work_start` / `work_end` (local minutes), weekday indexes
  (Monday 0) and canonical holiday dates. An unconfigured schedule may already
  have a nonzero version. GET returns bounded tasks, links, assumptions, gaps,
  resource-aware critical/float calculations and `can_edit`.
- `PUT /api/work/projects/{id}/tasks/{task_id}/plan`: estimate, `assignee`,
  `not_before`, `milestone`, UUID and current plan `expected_version` (0 if absent).
  Only accepted active editors or the owner may be assigned. Ordinary duration
  is positive or null/default; milestones use zero. Null clears an optional field.
- `PUT /api/work/projects/{id}/dependencies`: replace links using the current
  schedule version and a UUID. Links use `predecessor_id`, `successor_id` and
  nonnegative working-minute `lag_minutes`. Maximum 1000 tasks / 4000 links;
  cycles, self-links and cross-project references fail closed. This one bounded
  request accepts 512 KiB; other Work writes retain the 32 KiB limit.
- `GET/POST /api/work/projects/{id}/attachments`: list current project metadata
  or upload raw `application/octet-stream` bytes. `X-Prism-Upload` contains
  percent-encoded JSON with UUID `request_id`, plain `filename`, optional
  canonical `task_id`. Maximum 5 MiB/file, 30 active files/project and 100 MiB
  across the owner's projects. Only UTF-8 TXT/MD/CSV, PDF, PNG and JPEG are accepted.
  Bytes stay in the business SQLite store; never in a public tree or model input.
  `GET /{id}/attachments/{attachment_id}/download` rechecks membership and forces
  an uncached download. `POST /{id}/attachments/{attachment_id}/remove` requires
  UUID and expected attachment version; only uploader/editor or owner can remove.
  Upload replay returns the current removed state instead of recreating a file.
- `GET/POST /api/work/projects/{id}/approvals`: list bounded requests or create
  a pending request with UUID, `title`, `description`, explicit `reviewer`, optional
  project `task_id` and up to ten `attachment_ids`. GET supports `limit`/`before`,
  returns `next_cursor`, eligible `reviewers`, `can_submit`, and independently
  bounded `pending_for_me` / `pending_for_me_truncated` so old pending decisions
  are not hidden by newer history. No self approval. Snapshot pins exact task,
  selected attachment versions/hashes and requester/reviewer membership versions.
  `POST /{id}/approvals/{approval_id}/decision` requires UUID, current version,
  `decision: approved|rejected` and optional comment (required for rejection).
  Only the designated reviewer can decide. `POST /.../{approval_id}/cancel`
  uses UUID/version and permits requester or owner to withdraw a pending request.
  Stale contents/roles cannot be approved; decided requests cannot be undone.
- `GET /api/work/notifications`: read the current account's in-app reminders.
  `POST /api/work/notifications/{id}/read` marks an owned reminder read.
- `GET /api/work/notifications/health`: administrator-only scheduler observation.
  A recent tick does not prove external mail delivery or device push.

Writes require the existing CSRF contract. `X-Prism-Account` detects a changed
account before a write. The server checks due tasks and upcoming calendar events
every 30 seconds, starting 15 minutes before their scheduled time. Duplicate
ticks deduplicate by owner, record and version; cancelled or changed records
invalidate old displayed reminders. Scheduling participates in maintenance drain.
This channel is in-app only; it does not send mail, SMS or user text to a model.
The same scheduler handles pending approvals, results and assigned project-task
deadlines. Reads recheck current project membership. Assigned-task notices bind
both task and plan versions, so changing task content or reassignment invalidates
the old notice without altering unrelated personal notifications.

## Native research chat

Research chat uses Prism's owned direct HTTP runtime. The entire application prohibits Claude Code CLI execution or fallback; external Skills and retired MCP adapters are not execution backends. This reference describes repository source, not an instance's deployment state. Implementation and migration limits are in [Native Agent](native-agent-framework.md) and [Project Guide](PROJECT_GUIDE.md).

- `GET /api/agent/workflows` and `GET /api/agent/tools`: authenticated native method/tool catalogs.
- `POST /api/agent/chat/stream`: authenticated, CSRF-protected research start. Provide `question` and an existing owned `conv_id`; a stable `request_id` deduplicates the same input. Optional `model` and one `workflow` select the configured model and method. Reusing an identifier with different input returns 409.
- `GET /api/agent/runs/{run_id}` and `/events?after_seq=N`: owned status and ordered persisted events.
- `GET /api/agent/runs/{run_id}/stream?after_seq=N`: observe/replay only; this does not start or repeat model work.
- `POST /api/agent/chat/cancel`: provide owned `conv_id` and optional `run_id`. `cancel_requested` is intent; `stopped` is true only after a terminal state.

SSE frames carry `agent.run_id`, `agent.seq`, `agent.status` and `agent.event`. Readers deduplicate sequence numbers and reconnect with GET. Browser refresh checks owned history before attaching to the existing run. Execution `completed` is distinct from research `unreviewed`; collection and historical reuse do not certify facts. `/api/chat/stream` and `/api/chat/cancel` only alias Native behavior. No environment flag enables the old runner. `/api/chat/respond` returns 409 and asks the user to send a new message.

Owned conversation history reads platform SQLite only. `history_migration.status` distinguishes `imported`, `pending_import` and `not_required`; pending import still exposes existing platform messages. The explicit offline importer is not an API or startup job and never restores a CLI runtime dependency. Vibe execution remains disabled with 503; owned saved history remains available.

Endpoints are JSON over HTTP unless a stream or file is documented.
Authentication is via a session
cookie (`token`) issued by `/api/login`. The cookie is `HttpOnly`
and `Secure` when configured; `SameSite=strict` by default.

## Feedback (candidate)

Private user feedback and administrator review, not an execution/deployment API.
Usage, status meanings and scheduling limits are in [Feedback](FEEDBACK.md).
All writes require the existing authenticated CSRF protections; unknown JSON
fields and duplicate keys are rejected, with a 32 KiB request-body limit.

| Endpoint | Access and behavior |
|---|---|
| `POST /api/feedback` | Signed-in user; `category` (`bug` / `suggestion`), `title` (1–120), `description` (1–6000), optional local `page`, closed-enum `feature` and UUID `request_id`. Returns `ok`, `reused`, `feedback`. |
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
See [restricted service access](FEEDBACK.md#定期任务的受限接入) for filenames,
permissions and rotation. Request parameters cannot select a credential path.

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

## Work, calendar and preferences (v0.22.0 candidate)

All endpoints require the signed-in owner. Writes retain CSRF checks and a
32 KiB closed JSON schema; unknown keys and duplicate fields fail. Optional
`X-Prism-Account` binds the page to its initial account and rejects a mismatch.
Work defaults enabled; `PRISM_WORK_ENABLED=0` explicitly disables tasks/calendar.

| Endpoint | Behavior |
|---|---|
| `GET /api/work/overview` | Personal workspace and first task page; does not create a workspace |
| `POST /api/work/spaces` | Explicit personal workspace creation with UUID `request_id` and timezone |
| `GET/POST /api/work/spaces/{id}/tasks` | Owned tasks; bounded pagination and stable creation request ID |
| `PATCH /api/work/spaces/{id}/tasks/{task_id}` | Owned update with `expected_version` and UUID |
| `GET /api/work/calendar?start=...&end=...` | At most 31 days, overlapping noncancelled events and open task deadlines; maximum 200 of each with `truncated` |
| `POST /api/work/calendar` | UUID, title, description, ISO start/end instants and IANA timezone; returns event and bounded overlap list |
| `PUT /api/work/calendar/{id}` | Full fields plus `expected_version` and `cancelled`; stale writes 409, repeated request/body replays original response |
| `GET /api/work/calendar/{id}.ics` | One owned event, escaped/folded UTF-8, UTC timestamps, revision sequence, no-store |
| `GET/PUT /api/preferences/research` | Own explicit profile or `configured:false`; writes require `expected_version` |
| `GET /api/futures/products` | Fixed supported product catalog |
| `GET /api/futures/{symbol}/snapshot` | Bounded public daily observation with dates, calculations and gaps; 12 reads/minute/account |

Profile fields: `style` conservative/balanced/aggressive, `horizon`
short/medium/long, integer `max_position_pct` 1-100, integer
`max_drawdown_pct` 1-50 and boolean `allow_leverage`. Each Native run captures a
stable profile version; current preferences never rewrite existing runs. Actual
holdings are not assumed and `portfolio_risk_status` remains `not_evaluated`.

`futures_snapshot` accepts RB/CU/AU/M/SC only. Its public cache retains
collection time. Continuous series are not executable contracts; missing
fundamentals and capital flow are explicit. Trading and Work write tools are not
in the research Agent catalog. Native feedback accepts `work`, `calendar`,
`futures` and `preferences` as additional closed-enum feature names.

Futures sources are independent: unavailable continuous prices leave `latest`
and technical values null, without discarding collected exchange or warehouse
reports. `coverage` describes the market-data components, `status: partial`
retains usable components for 30 seconds, and fully collected market components
use a 300-second cache. This status does not certify fundamental research or
portfolio suitability. No source collected returns 503 with
`X-Prism-Data-Status: unavailable` and `Retry-After: 30`, without caching a result.

Official reports start from the server's Shanghai calendar date, independently
of a missing or stale secondary price feed. Only an actual dated HTTP 404 permits
a query for the preceding date, at most three distinct calendar dates per
source within the shared 15-second budget. Network, TLS, access, rate-limit and
schema errors do not trigger earlier-date queries. This bounded window is not
a trading calendar or a guarantee of the latest published report. Each parser
checks the report's actual date; collected reports are retained while another
source checks an older date. `source_attempts`, `observation_dates`,
`report_date_basis: bounded_calendar_window` and `date_alignment` preserve this
distinction. A mixed-date snapshot must not be treated as a same-day comparison.

For RB/CU/AU/SC, `contract_dates` and `exchange_parameters` independently read
SHFE's combined dated business reports. The first returns per-contract
`listed_on`, `listed_expiry_date`, `delivery_start` and `delivery_end`; the
second returns separately named speculative/hedge long/short margin percentages
and upper/lower limit percentages. Missing ratios remain null; special limits
set `additional_limit_notice_required`. Both retain source URL, body hash and
actual report/update dates. These are exchange observations, not confirmed
current broker requirements or delivery eligibility. `calendar_adjustments_status`
remains unassessed: the published calendar excludes holiday adjustments. Neither
source updates portfolio records. Details are in [futures research](futures-research.md).

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
| `POST /api/work/assistant/proposals` | UUID `request_id`, `text`, IANA `timezone`, explicit `allow_model` (default false); returns a validated preview or clarification, with no business write |
| `GET /api/work/assistant/proposals/by-request/{request_id}` | Reconcile a lost response using the original request, without resending a model call |
| `GET /api/work/assistant/proposals/{id}` | Read the original proposal, execution state and receipt |
| `POST /api/work/assistant/proposals/{id}/confirm` | Exact preview `digest`, live owner and 15-minute validity; creates/updates proposed objects and records the whole result in one transaction |
| `POST /api/work/assistant/proposals/{id}/undo` | Atomically restore original values or cancel/archive newly created objects in reverse order; refuses later edits |
| `GET /api/work/calendar/{id}` | Original series and recurrence rule for whole-series editing |
| `PUT /api/work/calendar/{id}/occurrences/{occurrence_id}` | UUID, instance `expected_version`, optional `expected_series_version`; changes `cancelled` and/or an explicit `start_at`/`end_at` pair, preserving other dates; returns actual times and bounded overlaps |
| `GET/PUT /api/preferences/portfolio` | Own explicit security positions, currency, cash/debt and observation times; writes compare `expected_version` and return deterministic risk rules |
| `GET /api/knowledge/items?query=...&before=...&limit=30` | Search all accessible persisted structured claims with bounded cursor pagination |
| `GET /api/knowledge/items/{id}` | Claim, source passages, review history and affected owned research |
| `PUT /api/knowledge/items/{id}/review` | `expected_version`, state, note, optional correction link; reviewed claims require a future validity time, incomplete citations cannot be promoted |
| `GET/PUT /api/admin/model-routes[/{purpose}]` | Administrator-only purpose selection and bounded request/token/time quotas; only registered model IDs, no key material |
| `GET /api/industry/research?q=...&before=...&limit=20` | Owned structured relationship candidates, original source excerpts, supporting/opposing claim review state, finite transmission hypotheses and cursor pagination; background seed edges excluded |

Calendar create/update accepts optional `recurrence`: `frequency` daily, weekly
or monthly; `interval` 1–12; weekly `weekdays` 0 (Monday) through 6; exactly one of
`count` or inclusive local `until`. Rules are finite (at most 366 occurrences and
ten years). Invalid month dates/DST gaps are skipped; the explicit DTSTART fold
selects repeated local times. Agenda returns original `events` and actual
`occurrences`; the latter have separate instance and series versions. ICS uses
the finite UTC occurrence set and cancellation exceptions. Independently moved
instances retain the series UID with their original DTSTART as RECURRENCE-ID and
their actual DTSTART/DTEND; they do not inherit a wrong master duration. Whole
series edits preserve existing exceptions, refusing silent removal; explicit
`reset_exceptions: true` resets them. Omitted new fields preserve old mutation
fingerprints for same-request reconciliation.

Portfolio decimals are strings, not binary floats. At most 100 unique securities
are accepted. Quotes older than seven days, future observations and mixed
currencies without FX are unevaluated. Optional `equity_peak`, `peak_as_of` and
signed `net_flows_since_peak` enable drawdown against a supplied high-water mark;
these user-supplied values are not independently verified prices or NAV history.
The profile endpoint's older `portfolio_risk_status` is not a risk certificate;
the portfolio response contains actual version-bound rule results.

Work planning never receives investment history or other Work records. Routine
instructions stay local. A model is used only after explicit selection and a
configured Work route permitting that data purpose. A model proposal cannot
execute itself. Unknown, failed and reserved model use is not refunded or reset
by route edits. Costs and subscription coverage stay unknown until verified.

Planner actions may create or update tasks, projects and events. Update selectors
use `match_title`, and event selectors optionally use local `on_date` with
`scope: occurrence` (default) or explicit `series`. The server alone resolves
owned IDs, current versions and before-images. Missing/duplicate targets clarify
instead of guessing. Previewed changes are pinned to those versions; confirm and
inverse actions commit with their receipts in one transaction. Later edits are
never overwritten. Task/project/calendar changes can be combined, but duplicate
changes to one object must be merged. A `can_use_model: false` clarification does
not offer another redundant model request.

Project planner actions `create_project_task`, `update_project_task`, `plan_project_task`,
`set_project_dependencies` and `set_project_calendar` use `project_name`, exact
task names and typed business fields. Dependency proposals add/remove only named
links; the server constructs the complete replacement locally. Existing project
records are never sent to the provider. Preview pins current project and member
versions, schedule version and before-images; one transactional preflight checks
external changes before sibling writes. Undo also checks the last completed
schedule version. Saved proposals stop disclosing project content after access
is revoked. None of these actions grant sharing or research access.

`read_project` resolves a local project name, returns bounded current tasks,
attachment metadata and approval summaries, and completes immediately when the
proposal contains only reads; `proposal.can_undo` is false. No provider receives
those records. `create_project_approval` accepts names from the submitted text,
resolves exact task/attachment names locally, pins readable snapshots for preview,
and creates a pending request after confirmation. Undo only withdraws a still
pending request. Approval decisions are human-only business operations, never
model-generated approval authority.

Public PDF reads retain the same DNS/TLS/host policy. Bodies are capped at 8 MiB;
an isolated Python parser has CPU/memory/time limits, no inherited credentials,
and is killed and reaped on timeout while its parent auxiliary work remains
owned. The default excerpt covers at most ten pages and 32,768 characters. Page
numbers, total pages and body hash accompany the excerpt. Encrypted, scanned,
invalid and timed-out documents retain explicit gaps; PDF metadata is not used
to invent a publication date. This parser is not a CLI Agent or a model fallback.

Futures snapshots add `exchange_daily` for SHFE RB/CU/AU and INE SC. Exact-date
official contract reports include `contracts`, `active_contract` (largest daily
reported volume), quote unit, a two-contract settlement `curve`, and separately
ranked `member_positioning` columns. Their original report URLs and body hashes
are retained. Missing/invalid data stays unavailable; it does not invalidate the
continuous bars. A far-minus-near price difference is not spot basis. The
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
