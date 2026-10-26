# Contributing to Prism

Start with the [Project Guide](docs/PROJECT_GUIDE.md) for local setup and the [architecture map](docs/ARCHITECTURE.md) for component entry points. These documents describe repository source, not any instance's deployment state. CI, tags and candidate packages do not establish that a release occurred.

## Development setup

Use Python 3.12+ and an isolated virtual environment, HOME and data directory. Install `requirements.txt`; install development tools such as pytest-cov and Ruff separately. Node.js is needed for browser-script regression tests. `install.sh` only prepares local Python dependencies; it does not start a service or perform a deployment.

The app can start without model credentials. Real research needs a configured DeepSeek or MiniMax key loaded into the process environment. `.env.example` is a template, not an automatically loaded configuration file. Keep credentials out of command history, commits and test output.

**No part of Prism may depend on or fall back to Claude Code CLI.** Do not install the CLI or external Skills for development, tests, candidate acceptance or recovery. Anthropic-compatible Messages denotes the direct HTTP protocol, not a Claude Code dependency. Application prompts belong in `server/agent/prompts.py`; auxiliary model tasks use `server/services/auxiliary_model.py`. Missing configuration or provider failure must remain an explicit failure.

## Validation and style

Use synthetic accounts, temporary data and mocked providers for offline tests. Set isolated paths before importing the application: importing it initializes the database. Never use a production database or inherited credentials for development checks.

```bash
python -m pytest -q tests/test_native_agent_api.py tests/test_native_agent_frontend.py tests/test_native_agent_http_journey.py
python -m pytest tests/ --cov=server --cov-report=term-missing
python -m ruff check server/ tests/
python -m ruff format --check server/ tests/
```

Test the changed behavior and relevant failure paths. Local socket tests need loopback access; real provider and data-source acceptance is separate, bounded work. Some Makefile targets ignore nonzero lint exits, so use direct commands and actual CI results when deciding whether validation passed. Format with Ruff and follow the 110-character line limit configured in `pyproject.toml`.

## Code map and extensions

| Component | Start here |
|---|---|
| Research execution, budgets, cancellation | `server/agent/api.py`, `runtime.py`, `store.py` |
| Direct model protocol and auxiliary tasks | `server/agent/provider.py`, `server/services/auxiliary_model.py` |
| Authorized tools and audit records | `server/agent/tools.py`, `server/capabilities.py` |
| Account-owned history and offline migration | `server/conversations.py`, `server/legacy_history.py`, `scripts/import_legacy_history.py` |
| Sources, evidence, reports | `server/agent/sources.py`, `server/research/`, `server/reports.py` |
| Work ownership and drain | `server/work_lifecycle.py`, `server/work_adapters.py` |
| Browser replay and rendering | `static/js/app.chat.js`, `app.chat-render.js`, `app.research.js` |

New routes require authentication, owner checks, closed input schemas and existing CSRF protection. New tools also require explicit Native allowlisting, real work ownership, cancellation, idempotency, safe error/result records and a link to the saved result. See the extension checklist in the architecture map.

Astock remains an optional third-party Python engine. Vibe execution is disabled with 503; only owned saved history is available. Do not describe either as a completed migration to the owned Agent.

## Pull requests and releases

Keep changes focused and reviewable, add meaningful regression coverage when behavior changes, update affected documentation and record user-visible changes in `CHANGELOG.md`. Be respectful and discuss technical tradeoffs clearly. Report security issues privately as described in [SECURITY.md](SECURITY.md).

Deployment owners must use a reviewed Native-only workflow with immutable candidates, an independent stop gate, drain, encrypted backup, recovery and real verification. Deployment and recovery contracts must bind exact artifacts, dependencies and data compatibility; recovery requires independent acceptance. Reject paths that depend on or fall back to Claude Code CLI. Publishing source, pushing a tag, running `install.sh` or passing CI cannot substitute for deployment evidence. Keep environment-specific records restricted. See [release-process.md](docs/release-process.md).
