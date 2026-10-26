# Contributing to Prism

> Current Native Agent development instructions are in [Project Guide](docs/PROJECT_GUIDE.md).
> Research chat uses Prism's direct API runtime; the Claude CLI instructions below apply to retained legacy adapters.
> CI, tags and candidate packages do not deploy production. Use the reviewed private release process and its actual evidence.

Thanks for your interest in improving Prism! This document covers the
basics: how to set up a dev environment, run tests, and submit changes.

## Code of Conduct

This project follows the spirit of the standard open-source CoC: be
respectful, assume good faith, focus on technical merit. Report
unacceptable behavior to the maintainers privately.

## Development setup

Prism targets **Python 3.12+** on Linux. macOS works but isn't tested
in CI; Windows is unsupported.

```bash
# 1. Clone and install
git clone https://github.com/your-org/prism.git
cd prism
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest-cov ruff mypy pre-commit

# 2. Set up pre-commit hooks
pre-commit install

# 3. Run the dev server
make run-dev

# 4. In another terminal, run tests
make test
```

### Required environment variables

For local development, the app runs without any LLM keys but you won't
get useful responses. Set at least one provider key:

```bash
read -rs DEEPSEEK_API_KEY
export DEEPSEEK_API_KEY
# or
read -rs MINIMAX_API_KEY
export MINIMAX_API_KEY
```

The Claude Code CLI must be installed and on `$PATH`. See
[`install.sh`](./install.sh).

## Running tests

```bash
make test           # full suite
make test-fast      # skip integration tests
make test-cov       # with coverage report
```

Tests use a temporary SQLite database (the `temp_data_dir` fixture
points `STOCK_DATA_DIR` at a `tempfile.TemporaryDirectory()`), so
running tests will never touch your real data.

### Integration tests

A few tests require a running Claude CLI to spawn a subprocess. They
are skipped automatically when the CLI isn't available. The rest of
the suite runs offline.

## Coding style

- Format with `ruff format` (enforced by pre-commit).
- Lint with `ruff check` — config in [`pyproject.toml`](./pyproject.toml).
- Type-check with `mypy` (loose mode; we lean on Pydantic for runtime
  validation).
- Line length cap is **110 characters**. Strings/docstrings may exceed.

## Pull request process

1. Open an issue first for non-trivial changes — the maintainers may
   want to weigh in on the design before you invest time.
2. Keep PRs focused: one feature or fix per PR. Squash commits to a
   single reviewable change.
3. Add tests for new behavior. We target ≥70% coverage on touched
   files; the global floor is 45% (low because the streaming chat
   path is hard to test offline).
4. Update [`CHANGELOG.md`](./CHANGELOG.md) under the "Unreleased"
   section with a brief description.
5. Make sure CI is green: `make test` + `make lint` + `make type-check`.

## Security

Report security issues via the private channel described in
[`SECURITY.md`](./SECURITY.md). Please do not open public GitHub
issues for suspected vulnerabilities.

## Project layout

```
prism/
├── server/               # FastAPI application
│   ├── auth.py           # login, sessions, CSRF
│   ├── chat.py           # streaming chat endpoint + Claude CLI runner
│   ├── conversations.py  # CRUD + recycle bin
│   ├── database.py       # SQLite + WAL + migrations + seed
│   ├── main.py           # FastAPI app + middleware + all routes
│   ├── middleware.py      # request ID, error handlers, logging
│   ├── models.py          # Pydantic request/response schemas
│   ├── proxy.py           # Provider-aware LLM API proxy
│   ├── analysis_engine.py # 7-agent astock LangGraph wrapper
│   ├── analysis_graph.py  # Analysis node/edge persistence
│   ├── industry_router.py # Industry chain graph API (Phase 6)
│   ├── config.py          # App config, model definitions, env vars
│   ├── market/            # Pluggable market data providers
│   │   ├── router.py, provider.py, factory.py, cache.py, chain.py, models.py
│   │   ├── akshare_provider.py, baostock_provider.py, tencent_provider.py
│   ├── indicators/        # Technical indicators (pandas-ta + DSL)
│   │   ├── router.py, dsl.py, translator.py, models.py
│   └── services/
│       └── claude_runner.py  # Claude Code subprocess wrapper
├── static/               # frontend assets
│   ├── css/app.css       # ~1,800 lines, custom-property themes
│   ├── js/               # modular JS (9 files, ~3,500 lines)
│   │   ├── app.util.js, app.core.js, app.chat.js, app.chat-render.js
│   │   ├── app.market.js, app.analysis.js, app.industry.js
│   │   ├── app.indicators.js, app.tour.js
│   ├── lib/              # echarts.min.js, marked.min.js, world.json
│   └── sw.js             # Service Worker
├── templates/            # app.html (main SPA), login.html
├── tests/                # pytest suite (297 tests)
├── skills/CLAUDE.md      # bundled skill dependency declarations
├── docs/
│   ├── API.md            # full API reference
│   └── superpowers/      # historical design specs + plans
├── config/               # systemd unit, nginx config, example settings
├── scripts/              # create_user.py, verify.sh
├── CLAUDE.md             # project overview for Claude Code agents
├── install.sh            # production one-command deploy
├── Makefile              # test, lint, format, type-check, run-dev
├── Dockerfile            # containerized deployment
├── pyproject.toml        # ruff + mypy config
└── .github/workflows/ci.yml
```

## Adding a new API endpoint

1. Define a Pydantic schema in `server/models.py`.
2. Add the route in the appropriate module (`auth.py`, `chat.py`, …).
3. Use `require_auth(request)` to gate access.
4. If the endpoint mutates state, the CSRF middleware will require
   `X-Requested-With: XMLHttpRequest` (added by the frontend
   `App.api()` wrapper).
5. Add a test under `tests/` covering at least the happy path and one
   failure case.

## Release process

Maintainers only: tag a release with `git tag vX.Y.Z` and push. The
deployment script reads the version from the git tag.
