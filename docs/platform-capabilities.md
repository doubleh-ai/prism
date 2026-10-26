# Native platform capabilities

Prism Native dispatches tools in-process through `server/agent/tools.py` and `server/capabilities.py`. The former CLI/MCP execution protocol is retired. No part of the application may depend on or fall back to Claude Code CLI, external Skills or a loopback bridge.

## Catalogs and permissions

| Endpoint | Scope |
|---|---|
| `GET /api/agent/tools` | Actual Native allowlist for authenticated research |
| `GET /api/capabilities` | Platform catalog; listing does not grant Agent access |
| `GET /api/convs/{id}/capability-calls` | Current owner's saved call records for a conversation |
| `GET /api/capability-calls?view=market&limit=20` | Current owner's calls, optionally filtered by workspace |

Native tools can read market data, indicators, industry context, owned reports, watchlists and existing task status. Source and knowledge tools have their own bounded readers. A run can record its own research scope, claims and relation candidates. Starting Astock, Vibe or scans and translating arbitrary indicator code are not in the Native allowlist.

Astock remains a third-party Python engine outside the owned research runtime. Vibe new research and follow-up execution return 503 pending independent isolation acceptance; only owned saved history is available.

## Execution contract

Each tool has a closed input schema, explicit read/write semantics and real account, conversation and Native parent-run authorization. Model arguments cannot select another user or provide arbitrary Shell, SQL, filesystem paths, administrator or deployment commands. Runtime budgets, per-run call identifiers and parameter-conflict checks bound execution. New platform entries do not automatically become Agent tools.

Background work must retain its real Lease until derived resources finish and required results are saved. Cancellation or a returned HTTP response does not prove that a thread or resource has stopped. Tool failures remain explicit; neither tools nor model transport may switch to the retired CLI backend.

## Saved results and evidence

Call records carry `call_id`, `run_id`, `conv_id`, capability, status, times, safe input/result summaries and a target page. Updates replace the same running card; opening a card reads the saved result without repeating the action. Owner checks apply to both the call and target record.

Research time, source publication time, retrieval time and data period remain distinct. Curated graph links and historical reports are unverified context until independently checked. A successful tool response proves collection/execution, not factual accuracy or investment quality.

See [Native Agent](native-agent-framework.md) for exact budgets, protocols and API routes, [Architecture](ARCHITECTURE.md) for extension steps, and [Work lifecycle](work-lifecycle.md) for drain and completion requirements. Historical CLI design is available in Git history and must not be used as current setup or recovery instructions.
