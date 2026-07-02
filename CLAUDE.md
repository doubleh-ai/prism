# CLAUDE.md — Prism

Project overview and conventions for Claude Code agents working on this codebase.

## What is Prism?

Prism is a **self-hosted AI investment research platform** for the Chinese A-share market.
It's a single-user web app: FastAPI backend + SQLite + vanilla JS frontend + ECharts.

Key capabilities:
- **AI Chat** — streaming SSE chat with Claude Code CLI subprocess, multi-skill support
- **Astock Analysis** — 7-agent LangGraph debate pipeline (Market → Debate → Research → Trader → Risk → Decision)
- **Market Data** — real-time quotes/K-line/fundamentals from akshare/baostock/tencent
- **Industry Graph** — knowledge graph of companies/technologies/resources with geo-distribution
- **Technical Indicators** — 30+ indicators via pandas-ta DSL

## Where things are

```
server/
  main.py           — FastAPI app, all route registration, middleware stack
  config.py         — ALL config: env vars, model definitions, API keys, paths
  database.py       — SQLite WAL, thread-local connections, migrations, seed data
  auth.py           — bcrypt login, session tokens, CSRF verify
  chat.py           — SSE chat stream: subprocess → queue → SSE events
  conversations.py  — CRUD + soft-delete recycle bin
  middleware.py      — request ID, error handlers, rotating file log
  models.py          — Pydantic schemas for all request/response validation
  proxy.py           — provider-aware LLM API proxy
  analysis_engine.py — 7-agent astock wrapper: ThreadPoolExecutor + asyncio.Queue → SSE
  analysis_graph.py  — persist analysis nodes/edges to SQLite
  industry_router.py — Phase 6 industry chain graph (4 API endpoints)
  market/            — Pluggable market data layer (akshare/baostock/tencent)
  indicators/        — Technical indicators (pandas-ta + DSL parser)
  services/          — Claude Code subprocess wrapper

static/js/ (modular, mounted on global `App` object)
  app.util.js        — escHtml, safeUrl, debounce, api() wrapper, base64
  app.core.js        — App.init(), switchMode(), theme, sidebar, refresh restore
  app.chat.js        — streaming chat, skill drawer, AskUserQuestion, composer
  app.chat-render.js — Markdown rendering + [[quote:TICKER]] chip injection
  app.market.js      — market panel, watchlist, quote cards, K-line chart
  app.analysis.js    — astock analysis UI: progress bar, agent grid, final report
  app.industry.js    — industry graph visualization (ECharts force graph)
  app.indicators.js  — technical indicator charts
  app.tour.js        — onboarding tour

templates/
  app.html           — main SPA shell (sidebar + icon rail + main area)
  login.html         — standalone login page

tests/               — 297 tests (pytest + httpx TestClient)
  conftest.py        — fixtures: temp_data_dir, client, auth_user
```

## Conventions

### Python
- Line length cap: **110 characters** (ruff config in pyproject.toml)
- Format with `ruff format`, lint with `ruff check`
- Type-check with `mypy` (loose mode; lean on Pydantic for runtime validation)
- All JSON request bodies MUST go through Pydantic models (defined in `models.py`)
- Database access: use `with get_db() as db:` context manager (thread-local, autocommit)
- Rate limiting: `check_rate_limit(key, max_req, window_seconds)` returns bool
- Auth: `require_auth(request)` returns username or raises 401; `optional=True` for public pages

### JavaScript
- No framework. Vanilla JS. Everything on global `App` object.
- Use `App.api(url, options)` for all fetch calls (auto-adds CSRF headers)
- Use `escHtml(str)` for XSS-safe text insertion
- Use `safeUrl(str)` before setting href/src attributes
- CSS themes: `[data-theme="dark"]` / `[data-theme="light"]` on `<html>`
- CSS variables defined in `:root` and overridden in theme selectors
- All CSS in `static/css/app.css` (~1,800 lines, single file)

### Testing
- Tests live in `tests/`, using `httpx.AsyncClient` with `ASGITransport`
- `conftest.py` provides `temp_data_dir` (tempfile) and `client` (authenticated)
- Run: `python3 -m pytest tests/ -x --tb=short` (297 tests, ~60s)
- All tests are offline (no real LLM calls, no real network to market data)
- New endpoints MUST have at least happy-path + one error-case test

### Git
- NO git commit without explicit user request
- Deploy script: `/tmp/prism-deploy-p2.sh` (copy files + restart systemd)
- Dev: `/home/develop/projects/prism/`, Prod: `/home/ubuntu/stock-analyzer/`
- Server name: `stock-app.service` (systemd), runs on port 8088

## How to run locally

```bash
cd /home/develop/projects/prism

# Dev server
python3 -m uvicorn server.main:app --host 127.0.0.1 --port 8088 --reload

# Tests
python3 -m pytest tests/ -x --tb=short

# Single test file
python3 -m pytest tests/test_api.py -x --tb=short
```

## Key gotchas

1. **pip needs `--break-system-packages`** — this machine uses system Python 3.12
2. **Tsinghua mirror**: `-i https://pypi.tuna.tsinghua.edu.cn/simple` for fast downloads
3. **TradingAgents-astock** at `/home/develop/projects/TradingAgents-astock/` — install with `pip install -e`
4. **No headless browser** in dev env — verify UI changes via static review + pytest + user's browser
5. **Prod egress**: Sina ✓, baostock:10087/eastmoney ✗, local proxy at 127.0.0.1:7890 for LLMs
6. **`static/js/app.js`** no longer exists — it was split into 9 modules in v0.8
7. **Analysis state** persists in `sessionStorage['prism_analysis']` — check `_restoreAnalysisState()`
8. **CSRF**: all POST/PUT/DELETE need `X-Requested-With: XMLHttpRequest` header
9. **SSE events**: format is `data: {json}\n\n`, NOT `event: type\ndata: ...`
10. **Cache-Control**: `/static/*` gets `immutable` — always bump `?v=N` in templates after CSS/JS changes

## Active decision log

- **JS framework**: deliberately vanilla — no React/Vue build step, keeps deploy simple
- **SQLite over Postgres**: single-user app, WAL mode is fast enough, zero ops overhead
- **ECharts self-hosted**: `static/lib/echarts.min.js` (~1MB) avoids CDN dependency in prod
- **Provider chain order**: akshare first (most stable via Sina), baostock/tencent as fallback
- **Analysis in thread**: LangGraph runs in `ThreadPoolExecutor` to not block the event loop
- **Indicators server-side**: computed in Python (pandas-ta), not in browser, for consistency
