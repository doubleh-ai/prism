<p align="center">
  <img src="https://img.shields.io/badge/Prism-AI%20Research-3b5d8a?style=flat" alt="Prism">
  <img src="https://img.shields.io/badge/license-Apache%202.0-blue" alt="License">
  <img src="https://img.shields.io/badge/python-3.12+-blue" alt="Python">
</p>

# Prism

> AI-powered investment research platform for the Chinese A-share market.
> Multi-agent analysis, live market data, industry graph — all in one self-hosted app.

Prism combines **7-agent LangGraph debate pipelines**, **real-time market data** (quotes, K-line, fundamentals), **industry chain knowledge graphs**, and **technical indicators** into a clean, fast, single-user web application.

---

## Architecture

```
Browser (Vanilla JS + ECharts + SSE)
        │
   FastAPI (Python 3.12)
   ┌────┼────────────────────────────┐
   │    │  Market Providers          │
   │    ├── akshare (Sina/EE)        │
   │    ├── baostock (local TCP)     │
   │    └── tencent (fallback)       │
   │    │                            │
   │    ├── Astock Engine            │
   │    │   └── TradingAgentsGraph   │
   │    │       7-agent LangGraph    │
   │    │                            │
   │    ├── Indicators Engine        │
   │    │   └── pandas-ta + DSL      │
   │    │                            │
   │    └── SQLite (WAL, mmap)       │
   └─────────────────────────────────┘
```

- **Frontend** — Vanilla JS (modular: 9 files, ~3,500 lines), CSS custom-property dark/light themes, ECharts K-line + force-graph, SSE streaming, sessionStorage persistence, Service Worker offline cache
- **Backend** — FastAPI + SQLite WAL, bcrypt auth, Pydantic validation, CSRF/CSP/XSS defense, rate limiting, LRU caches, GZip + Cache-Control
- **AI Engine** — 7-agent LangGraph debate pipeline (Market/Social/News/Fundamentals/Policy/HotMoney/Lockup analysts → Bull/Bear debate → Research → Trader → Risk → Decision)
- **Market Data** — Pluggable provider chain (akshare → baostock → tencent) with SQLite TTL cache + stale-data fallback, quote/K-line/fundamentals/search
- **Deploy** — Nginx reverse proxy + systemd on Ubuntu, one-script deploy

## Quick Start

```bash
# Clone
git clone https://github.com/doubleh-ai/prism.git /home/ubuntu/prism
cd /home/ubuntu/prism

# Install
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Optional: market data + astock analysis deps
pip install -r requirements-extra.txt

# Configure
cp .env.example .env
# → Edit .env with your API keys

# Deploy
ADMIN_USER=admin ADMIN_PASS=your-password bash install.sh
```

### Environment Variables

| Variable | Description |
|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek platform API key |
| `MINIMAX_API_KEY` | MiniMax platform API key |
| `LLM_API_KEY` | Legacy fallback (used if per‑provider keys are unset) |
| `STOCK_DATA_DIR` | Data directory (default: `./data`) |
| `RATE_CHAT_PER_MIN` | Chat rate limit (default: 20) |

Full list in `.env.example`.

---

## Features

### 📊 Market Data (v0.6–v0.7)

- **Real-time quotes** — price, change%, OHLCV from akshare/baostock/tencent
- **K-line charts** — ECharts candlestick + volume, support 1d/1w/1m periods, drag-zoom
- **Fundamentals** — PE/PB/ROE/market-cap from akshare
- **Stock search** — fuzzy match by ticker or name
- **Watchlist** — localStorage-persisted, auto-refresh every 60s
- **`[[quote:TICKER]]` chips** — inline price cards in AI chat, click to jump to market panel

### 🧠 AI Analysis (v0.10 astock)

- **7-agent LangGraph pipeline** — Market/Social/News/Fundamentals/Policy/HotMoney/Lockup analysts → Bull/Bear debate → Research Manager → Trader → Risk debate (Aggressive/Conservative/Neutral) → Portfolio Manager decision
- **Real-time progress** — SSE streaming with stage timeline + agent grid + progress bar
- **Persistent state** — analysis survives page refresh via sessionStorage, auto-reconnects SSE
- **Structured report** — rating badge, analyst reports (foldable), debate summary, trading plan, risk assessment
- **Copy to chat** — one-click "add to conversation" pastes the final decision into the composer

### 🔗 Industry Chain Graph (v0.10 Phase 6)

- **Knowledge graph** — nodes (companies/technologies/resources) + edges (supplies/competes/depends_on)
- **Geo distribution** — 12-country seed data with key industries and policy summaries
- **Ticker chain** — upstream/downstream traversal for any stock
- **Search** — fuzzy search across all industry nodes
- **Accumulates over time** — each analysis can feed new nodes/edges into the graph

### 📈 Technical Indicators (v0.10)

- **TA-Lib-free** — powered by pandas-ta, computed server-side
- **30+ indicators** — SMA/EMA/MACD/RSI/BOLL/KDJ/ATR/OBV + more
- **DSL query** — `GET /api/indicators/{ticker}?expr=macd(12,26,9)|rsi(14)`
- **Cached** — results cached per-ticker for 60s

### 🛡️ Security (v0.8.2)

- CSRF double-submit cookie + `X-Requested-With` header
- CSP headers (script-src/style-src/img-src)
- XSS sanitization (HTML escape + `sanitizeHtml()` strip scripts/on* handlers/javascript: URLs)
- Path traversal prevention (resolve + relative_to)
- Rate limiting (chat 20/min, login 10/min, explain 10/min)
- Request ID correlation on every request

---

## Project Structure

```
prism/
├── server/
│   ├── main.py              # FastAPI app + middleware + all routes
│   ├── config.py            # App config, models, API keys
│   ├── database.py          # SQLite WAL + thread-local + migrations + seed
│   ├── auth.py              # Login, sessions, CSRF
│   ├── chat.py              # SSE chat stream + Claude CLI runner
│   ├── conversations.py     # CRUD + recycle bin
│   ├── middleware.py         # Request ID, error handlers, logging
│   ├── models.py            # Pydantic request/response schemas
│   ├── proxy.py             # Provider-aware LLM proxy
│   ├── analysis_engine.py   # 7-agent astock LangGraph wrapper
│   ├── analysis_graph.py    # Analysis node/edge persistence
│   ├── industry_router.py   # Industry chain graph API (Phase 6)
│   ├── industry_seed.py     # Industry seed data
│   ├── market/              # Market data layer
│   │   ├── router.py        # /api/quote|kline|fundamentals|search endpoints
│   │   ├── provider.py      # Abstract provider interface
│   │   ├── factory.py       # Provider chain builder
│   │   ├── cache.py         # SQLite TTL cache
│   │   ├── chain.py         # Failover chain logic
│   │   ├── models.py        # Market Pydantic models
│   │   ├── akshare_provider.py
│   │   ├── baostock_provider.py
│   │   └── tencent_provider.py
│   └── indicators/          # Technical indicators
│       ├── router.py        # /api/indicators endpoint
│       ├── dsl.py           # Expression parser (macd(12,26,9)|rsi(14))
│       ├── translator.py    # DSL → pandas-ta calls
│       └── models.py
├── static/
│   ├── css/app.css          # ~1800 lines CSS custom-property themes
│   ├── js/                  # Modular JS (~3,500 lines total)
│   │   ├── app.util.js      # escHtml, safeUrl, debounce, api wrapper
│   │   ├── app.core.js      # App init, mode switch, theme, sidebar
│   │   ├── app.chat.js      # Chat streaming, message rendering, skills
│   │   ├── app.chat-render.js # Markdown + quote-chip rendering
│   │   ├── app.market.js    # Market panel, watchlist, quote cards
│   │   ├── app.analysis.js  # Astock analysis UI (progress + report)
│   │   ├── app.industry.js  # Industry graph visualization
│   │   ├── app.indicators.js # Technical indicator charts
│   │   └── app.tour.js      # Onboarding tour
│   ├── lib/
│   │   ├── echarts.min.js   # ECharts 5 (CDN fallback: self-hosted)
│   │   ├── marked.min.js    # Markdown parser
│   │   └── world.json       # GeoJSON world map for geo-distribution
│   └── sw.js                # Service Worker (offline cache)
├── templates/
│   ├── app.html             # Main SPA shell
│   └── login.html           # Login page
├── tests/                   # 297 tests (pytest)
├── skills/CLAUDE.md         # Skill dependency declarations
├── config/
│   ├── stock-analyzer.service  # systemd unit
│   ├── nginx-site.conf         # Nginx reverse proxy config
│   └── claude-settings.example.json
├── scripts/
│   ├── create_user.py       # Admin user creation
│   └── verify.sh            # Health-check script
├── docs/
│   ├── API.md               # Full API reference
│   └── superpowers/         # Design specs + plans (historical)
├── requirements.txt         # Core dependencies
├── requirements-extra.txt   # Market data + astock deps
├── install.sh               # One-command production deploy
├── Makefile                 # test, lint, format, type-check, run-dev
├── Dockerfile               # Containerized deployment
├── pyproject.toml           # ruff + mypy config
└── .github/workflows/ci.yml # CI pipeline
```

## API Endpoints

| Group | Endpoints |
|---|---|
| **Auth** | `POST /api/login`, `POST /api/logout`, `GET /api/me` |
| **Conversations** | `GET/POST /api/convs`, `GET/DELETE /api/convs/{id}`, `POST restore`, `DELETE permanent`, `PUT skill`, `POST batch-delete`, `GET /api/trash` |
| **Chat** | `POST /api/chat/stream` (SSE), `POST /api/chat/respond`, `POST /api/chat/cancel` |
| **Analysis** | `POST /api/analysis/start`, `GET /api/analysis/{id}/stream` (SSE), `GET /api/analysis/{id}/report` |
| **Market** | `GET /api/quote/{ticker}`, `GET /api/quote/{ticker}/kline`, `GET /api/fundamentals/{ticker}`, `GET /api/search`, `GET /api/data-providers` |
| **Industry** | `GET /api/industry/graph`, `GET /api/industry/geo`, `GET /api/industry/search`, `GET /api/industry/ticker/{ticker}/chain` |
| **Indicators** | `GET /api/indicators/{ticker}?expr=macd(12,26,9)\|rsi(14)` |
| **Reports** | `GET /api/convs/{id}/reports`, `GET /api/reports/{id}/{filename}` |
| **Utility** | `GET /api/models`, `GET /api/skills`, `POST /api/explain-term` |

Full API reference: [`docs/API.md`](docs/API.md)

## Development

```bash
# Run tests (297 tests)
make test

# Run with coverage
make test-cov

# Lint + format
make lint
make format

# Type check
make type-check

# Dev server (port 8088)
make run-dev
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full development guide.

## Deployment

```bash
# One-script deploy (dev → prod)
bash /tmp/prism-deploy-p2.sh

# Or manual:
sudo cp -r /home/develop/projects/prism/* /home/ubuntu/stock-analyzer/
sudo systemctl restart stock-app.service
```

## License

Apache 2.0 — see [LICENSE](LICENSE)
