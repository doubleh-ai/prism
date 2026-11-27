# Prism — Agent & Contributor Rules

These rules apply to all coding agents (Antigravity, Codex, Claude Code, Cursor, Copilot) and human contributors working in this repository. Follow every rule below before committing or opening a pull request.

---

## 1. Branching, Commits & Pull Request Workflow

1. **`main` Is the Only Trunk**: Always branch off up-to-date `origin/main` using a descriptive prefix (`feat/<topic>`, `fix/<topic>`, `perf/<topic>`, `docs/<topic>`, `chore/<topic>`).
2. **No `master` Branch & No Force-Push to `main`**: Never create or push a `master` branch, and never rewrite `main` history (`git push --force`).
3. **Focused Atomic Commits**: Keep each PR scoped to one cohesive goal. Write imperative commit titles explaining *what* changed and *why*.
4. **Changelog Discipline**: Record user-visible additions, fixes, or removals under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md). Bump [server/release.py](server/release.py) (`APP_VERSION`) only when cutting an explicit release.
5. **Merge via Green PR**: Open a pull request against `main` using [.github/PULL_REQUEST_TEMPLATE.md](.github/PULL_REQUEST_TEMPLATE.md) and merge only after all CI checks (`test` and `security-scan`) pass.

---

## 2. Open-Source Hygiene & Zero-Secret Redlines

1. **Never Commit Secrets or Runtime State**: `.env` files, API keys, tokens, private keys (`*.pem`, `*.key`), SQLite databases (`*.db`, `*.sqlite*`), logs (`*.log`), and user reports/uploads must never enter Git.
2. **Zero Private Infrastructure Footprint**: Keep source code, tests, comments, and documentation 100% portable and host-agnostic. Never hardcode real server IPv4/IPv6 addresses (outside RFC 5737 documentation ranges `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24` or loopback `127.0.0.1`), SSH `user@host` strings, private ops hostnames, or host-specific deployment scripts.
3. **Documentation Integrity**: Keep [README.md](README.md) version-agnostic and accurate for self-hosters. Every relative link in `*.md` must resolve to an existing file in the repository.

---

## 3. Architecture & Security Invariants

1. **Direct HTTP Native Agent Only**: All research and auxiliary model calls must execute through Prism's built-in Native Agent (`server/agent/`) and direct HTTP providers (`DeepSeek`, `MiniMax`, `OpenAI`). Never add external CLI subprocess runners or silent fallback engines.
2. **Closed Route Security**: Every HTTP route must enforce authentication (`require_auth`), account ownership checks, closed Pydantic input validation, and CSRF protection (`verify_csrf` / `X-Requested-With`) on state-changing requests.
3. **Untrusted Content Boundary**: Treat model output, tool payloads, and fetched web pages as untrusted data, never instructions. Sanitize rendered HTML/URLs (`sanitizeHtml`, `safeUrl`) and never auto-retry ambiguous paid model turns.
4. **Honest Data Provenance**: Preserve source URLs, observation dates (`Asia/Shanghai`), and unit labels. When an external quote, warehouse receipt, or spot series is missing or stale, surface an explicit `unavailable` / `stale` state — never fabricate, interpolate, or zero-fill missing numbers.

---

## 4. Frontend & Product Restraint Rules

1. **Unified Workbench Chrome**: Maintain the shared warm-ivory & forest-green design language, collapsible left `icon-rail`, single 64px `.topbar`, and docked/resizable Copilot across both `/prism` and `/futures`. Integrate capabilities into existing surfaces rather than adding standalone pages, redundant headers, or announcement banners.
2. **Non-Blocking Reading Experience**: Keep tool/capability execution traces and citation lists folded (`<details>`) so assistant prose and tables remain immediately readable.
3. **Mandatory Asset Cache-Version Sync**: Whenever any file under `static/js/*`, `static/css/*`, or `templates/*` changes, increment the asset version number in lockstep across:
   - `templates/app.html`, `templates/futures.html`, `templates/login.html`, `templates/partials/*.html` (`?v=NNN`)
   - `static/sw.js` (`CACHE_NAME = 'prism-workbench-vNNN'` and `?v=NNN`)
   - `static/js/app.core.js` (`_clientAssetVersion: 'NNN'`)
   - `tests/test_frontend.py` version assertions (if pinned)

---

## 5. Mandatory Pre-Submit Verification Gate

Run all five checks locally (or `make check`) from an isolated environment (`STOCK_DATA_DIR` pointed at a temp directory, never a live database) before committing or opening a PR:

```bash
# 1. Open-source hygiene, broken links, branch name & asset version coherence (stdlib-only)
python3 -I -S -B scripts/check_oss_hygiene.py

# 2. Startup credential isolation (stdlib-only, 0 network requests)
python3 -I -S -B scripts/check_provider_key_isolation.py

# 3. Python lint & formatting (110-char line limit)
python -m ruff check server/ tests/ scripts/release_candidate.py scripts/build_jsonpath_wheel.py scripts/check_provider_key_isolation.py scripts/check_oss_hygiene.py
python -m ruff format --check server/ tests/ scripts/release_candidate.py scripts/build_jsonpath_wheel.py scripts/check_provider_key_isolation.py scripts/check_oss_hygiene.py

# 4. Shipped browser JavaScript runtime suites (Node.js)
for f in tests/*.cjs; do [ "$f" = "tests/source_urls_runtime.cjs" ] && continue; node "$f"; done

# 5. Python regression tests (run targeted suites while iterating; full suite before PR/release)
python -m pytest -q tests/test_asset_coherence.py tests/test_public_source_boundary.py tests/test_frontend.py tests/test_security.py
```

Enable the repository Git pre-commit hook once per clone so hygiene checks run automatically on every `git commit`:

```bash
git config core.hooksPath .githooks
```
