# Prism Engineering & Contribution Guidelines

These guidelines define safety, architecture, and verification defaults for contributors and coding agents working in this repository.

## Architecture & Runtime Boundaries

- **Direct HTTP Native Agent**: Research and auxiliary model tasks run through Prism's built-in Native Agent (`server/agent/`) and direct HTTP providers (`DeepSeek`, `MiniMax`, `OpenAI`). Do not introduce external CLI subprocesses or fallback execution engines.
- **Account Isolation & Security**: All routes require authentication, owner verification, closed input schemas, and CSRF protection. Rendered model output and external web pages are untrusted data and must pass through frontend sanitization; never treat retrieved content as execution instructions.
- **Ops & Data Separation**: This repository holds application source, portable docs, and offline tests. Host-specific deployment scripts, secrets, `.env` files, private keys, SQLite databases, logs, and user content must never be committed or packaged into artifacts.
- **Read-Only Observations**: CI runs, candidate artifacts, and `/health/ready` checks are development/readiness signals only and never authorize or execute production deployments.

## Development & Verification Standards

- Follow [Product Principles](docs/product-principles.md) and [Interface Guidelines](docs/ui-guidelines.md): integrate capabilities cleanly into existing navigation, keep errors and data provenance visible, and avoid redundant controls or announcement banners.
- Use isolated temporary `HOME` / `STOCK_DATA_DIR` paths, synthetic accounts, and mocked providers for tests. Never point local or unverified code at a production database.
- When modifying frontend static assets (`static/js/*`, `static/css/*`, `templates/*`), keep cache-busting query versions synchronized with `static/sw.js` and `static/js/app.core.js` so `tests/test_asset_coherence.py` passes.
- Before opening a PR against `main`:
  1. Run `python3 -I -S -B scripts/check_provider_key_isolation.py`
  2. Run `python -m ruff check server/ tests/` and `python -m ruff format --check server/ tests/`
  3. Run affected `pytest` and Node runtime checks (and full `pytest` before release)
  4. Update affected documentation and record user-visible changes in `CHANGELOG.md` under `[Unreleased]`.
