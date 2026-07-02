# Prism — developer Makefile
# Run `make help` for available targets.

# ── Config ──
PYTHON ?= python3
PIP ?= $(PYTHON) -m pip
HOST ?= 127.0.0.1
PORT ?= 8088

.PHONY: help install install-dev test test-cov test-fast lint format type-check \
        run run-dev clean db-reset coverage coverage-open pre-commit

help: ## Show this help.
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ── Install ──
install: ## Install runtime dependencies.
	$(PIP) install -r requirements.txt

install-dev: ## Install dev/test/lint dependencies.
	$(PIP) install -r requirements.txt
	$(PIP) install pytest-cov ruff mypy

# ── Tests ──
test: ## Run full test suite.
	$(PYTHON) -m pytest tests/ -v --tb=short

test-fast: ## Run fast tests only (skip integration).
	$(PYTHON) -m pytest tests/ -v --tb=short -m "not integration"

test-cov: ## Run tests with coverage report.
	$(PYTHON) -m pytest tests/ --cov=server --cov-report=term-missing --cov-report=html

coverage: test-cov ## Alias for test-cov.
	@echo "Coverage report at htmlcov/index.html"

coverage-open: coverage ## Open the HTML coverage report in a browser.
	@xdg-open htmlcov/index.html 2>/dev/null || open htmlcov/index.html 2>/dev/null || \
		echo "Open htmlcov/index.html in your browser."

# ── Lint / type / format ──
lint: ## Run ruff linter.
	$(PYTHON) -m ruff check server/ tests/ || true

format: ## Run ruff formatter (safe; check-only by default).
	$(PYTHON) -m ruff format --check server/ tests/ || true

type-check: ## Run mypy on the server package.
	$(PYTHON) -m mypy server/ --ignore-missing-imports --no-strict-optional

pre-commit: ## Run pre-commit hooks on staged files.
	$(PYTHON) -m pre_commit run --all-files || true

# ── Run ──
run: ## Run the production server (no reload).
	$(PYTHON) -m uvicorn server.main:app --host $(HOST) --port $(PORT) --workers 1

run-dev: ## Run the dev server with auto-reload.
	$(PYTHON) -m uvicorn server.main:app --host $(HOST) --port $(PORT) --reload

# ── DB ──
db-reset: ## Reset the local SQLite database (DESTRUCTIVE).
	rm -f data/chat.db data/chat.db-wal data/chat.db-shm

# ── Cleanup ──
clean: ## Remove caches and build artifacts.
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage coverage.xml
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
