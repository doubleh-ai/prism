<p align="center">
  <img src="https://img.shields.io/badge/Prism-AI%20Research-3b5d8a?style=flat" alt="Prism">
  <img src="https://img.shields.io/badge/license-Apache%202.0-blue" alt="License">
  <img src="https://img.shields.io/badge/python-3.12+-blue" alt="Python">
</p>

# Prism

> AI-powered investment research platform for the Chinese A-share market.
> Multi-agent analysis, live market data, industry graph — all in one self-hosted app.

Prism combines **7-agent LangGraph debate pipelines**, **market data** (quotes, K-line, fundamentals), **industry chain knowledge graphs**, and **technical indicators** in an authenticated research workspace with per-user records.

## Research Workbench（开发中）

研究对话是统一入口，行情、个股分析、深度研究、产业关系、指标、全市场选股和网页研报仍有独立工作区。顶部“平台能力”展示后端实际提供的工具；Agent 的真实调用会在回复中形成结果卡，并保存到会话和对应工作区的“调用记录”。点击卡片打开已有记录，可以返回原会话继续讨论；打开后台任务不会重新启动一份研究。网页是主要研报阅读方式，已有导出功能仍可选用。

新会话默认使用仓库内置的 `prism-research` 研究方法，不要求本机安装外部 `bottleneck-hunter` 技能。既有会话保留原技能；技能缺失时会明确提示回退到内置方法。内置方法并不替代真实模型、Claude CLI、行情提供方、Astock 或 Vibe 引擎；相应依赖不可用时，能力会返回失败记录，不以示例结果替代。

每轮研究保存实际请求时间、`Asia/Shanghai` 时区和 `run_id`。界面将**研究截至**、来源**发布时间**、**采集时间**与行情本身的数据时间分开展示；未知时间不会用当前时间补造。来源“已采集”仅说明工具成功取得内容，不代表事实已经核验；外部网页、平台数据和历史研究分别标注。旧记录没有可靠时间或来源信息时保留“未提供 / 待核验”，产业关系也不能仅凭连接数或种子数据认定为真实供货或瓶颈。

能力目录及历史接口见 [API Reference](docs/API.md#platform-capabilities-and-call-history)。

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
| `PRISM_INTERNAL_API_URL` | Agent 工具桥访问同一 Prism 服务的内部地址，默认 `http://127.0.0.1:8088`；必须为带端口的数字回环 HTTP 地址 |

Full list in `.env.example`.

Agent 工具桥通过回环地址调用本实例，并按本轮用户、会话和能力范围执行校验。默认端口为 `8088`；若实际服务使用其他端口，必须同时配置 `PRISM_INTERNAL_API_URL`。例如本地服务使用 `9000`：

```bash
PRISM_INTERNAL_API_URL=http://127.0.0.1:9000 \
  .venv/bin/uvicorn server.main:app --host 127.0.0.1 --port 9000
```

systemd 部署时，将相同配置放入该服务的环境配置。此地址不是公网域名、反向代理入口或 Vibe sidecar 地址；不接受外部主机或重定向。短期调用认证由服务生成，无需在浏览器或仓库中放置认证值。

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

The current release workflow is **candidate-only**: prepare and check a
specific revision, then inspect actual GitHub workflow results in the admin
operations view. It does not publish to production, restart services, or
perform rollback. Unavailable or stale observations are shown explicitly;
a passing candidate is not evidence that production has changed.

`GET /health/ready` is a public, read-only application/database readiness
probe (200 or 503). It exposes only basic status, version/revision metadata,
and observation time. It does not verify model calls, live market data, or
deployment acceptance. The Docker healthcheck uses this endpoint.

Do not use the former `/tmp/prism-deploy-p2.sh`, recursive copies into the
live directory, `install.sh`, or `scripts/verify.sh` as a release executor.
The installer is for initial setup; the verification script can change
state and invoke external services. A GitHub workflow cancellation link
stops that workflow only when GitHub confirms cancellation; it does not
roll back production.

See [the release process and emergency-control boundaries](docs/release-process.md)
for what is available now and the required gates before production can be
connected. Existing Docker packaging also needs a separate full-feature
acceptance check before it can be used as a production artifact.

## License

Apache 2.0 — see [LICENSE](LICENSE)
