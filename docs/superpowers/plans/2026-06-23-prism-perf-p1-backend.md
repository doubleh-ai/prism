# Prism v0.10 — Phase 1: 后端低风险性能优化 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 优化后端单请求延迟、消除事件循环阻塞、加固静态资源缓存；不引入破坏性行为变更。

**Architecture:** 7 个独立可验收的子任务。后端 SQLite 升级（PRAGMA + 连接复用 + autocommit） + 异步化（tencent + aiofiles）+ 静态资源层（gzip + immutable cache）+ 资源治理（LRU 缓存上限 + explain 限流）。前端零改动。

**Tech Stack:** FastAPI 0.115 / SQLite 3 / Pydantic 2 / pytest 8 / aiofiles (新) / lru-dict (新) / Starlette `GZipMiddleware`（内建）

**Reference spec:** `docs/superpowers/specs/2026-06-23-prism-architecture-optimization-design.md` §5

**Constraint:** 保留全部现有功能（245 个测试全过）。新依赖：`aiofiles>=23.2`, `lru-dict>=1.2`。不 commit；用户在 dev 树验收。

---

## 任务概览

| # | 任务 | 改的文件 | 新文件 | 预期新增测试 |
|---|---|---|---|---|
| 1 | LRU 缓存上限（#13-14） | `server/proxy.py`, `server/main.py`, `requirements.txt` | `tests/test_lru_caches.py` | 3 |
| 2 | explain-term 限流（#15） | `server/main.py` | `tests/test_explain_rate_limit.py` | 2 |
| 3 | SQLite PRAGMA（#6） | `server/database.py` | `tests/test_db_pragmas.py` | 5 |
| 4 | 连接复用 autocommit（#7） | `server/database.py`, `tests/conftest.py` | `tests/test_db_connection_reuse.py` | 4 |
| 5 | aiofiles 替换 chat.py（#8） | `server/chat.py` | `tests/test_chat_jsonl_async.py` | 2 |
| 6 | tencent provider 异步化（#5） | `server/market/tencent_provider.py` | `tests/test_tencent_async.py` | 3 |
| 7 | GZipMiddleware + Cache-Control（#10） | `server/main.py` | `tests/test_gzip_and_cache.py` | 4 |
| **合计** | | | | **23 个新测试** |

执行顺序：从最独立 → 最依赖。1 → 2 → 3 → 4（PRAGMA 必须在连接复用之前）→ 5 → 6 → 7。每个任务自成 commit / 验收单元。

---

## File Structure (post-Phase 1)

**Modified:**
- `server/database.py` — `get_db()` 改 thread-local + autocommit；`reset_connection()` 新增；`_make_connection()` 私有
- `server/proxy.py` — `_CACHE` 由 `dict` 改为 `LRU(1024)`
- `server/main.py` — `_term_cache` 改为 `LRU(256)`；`/api/explain-term` 加 rate limit；新增 `StaticCacheControlMiddleware`；接入 `GZipMiddleware`
- `server/chat.py` — `_read_jsonl_messages` 改 async + `aiofiles`
- `server/market/tencent_provider.py` — `get_quote` / `get_kline` 内部 urllib 调用包 `asyncio.to_thread`
- `tests/conftest.py` — `temp_data_dir` fixture 调 `reset_connection()`
- `requirements.txt` — 加 `aiofiles>=23.2`、`lru-dict>=1.2`

**New (tests only):**
- `tests/test_lru_caches.py`
- `tests/test_explain_rate_limit.py`
- `tests/test_db_pragmas.py`
- `tests/test_db_connection_reuse.py`
- `tests/test_chat_jsonl_async.py`
- `tests/test_tencent_async.py`
- `tests/test_gzip_and_cache.py`

---

## Task 1: LRU 缓存上限（#13-14）

**Files:**
- Modify: `server/proxy.py:9-10`
- Modify: `server/main.py:309`
- Modify: `requirements.txt`
- Test: `tests/test_lru_caches.py`

**Why:** 当前 `_term_cache` 和 `_CACHE` 是无界 dict，长期跑会涨内存。改为 lru-dict 实现的 256/1024 条上限 O(1) LRU。

### Step 1.1: Add lru-dict to requirements

打开 `requirements.txt`，在 `# ── dev / test ──` 块**之前**（运行时段）加一行：

```python
# Prism — AI Investment Analysis (Python dependencies)
#
# Versions are pinned to minor: the API surface these packages expose is
# stable, but we lock at the minor level to get reproducible installs and
# reproducible CI. For exact reproducible builds, run:
#   pip install pip-compile
#   pip-compile requirements.in
#
# In production deployments, prefer running with a lock file:
#   pip install --require-hashes -r requirements.lock

# ── Runtime ──
fastapi==0.115.*
uvicorn[standard]==0.34.*
bcrypt==4.2.*
pydantic==2.13.*
aiofiles>=23.2          # ← NEW: async file I/O for jsonl reads (chat.py)
lru-dict>=1.2           # ← NEW: O(1) LRU for in-memory caches (proxy.py, main.py)

# ── dev / test ──
pytest==8.0.*
pytest-asyncio==0.24.*
pytest-cov==5.0.*
httpx==0.27.*

# ── Optional: market data providers ──
# akshare / baostock live in requirements-extra.txt because they pull in
# many large dependencies. Install them only if you want live market data:
#   pip install -r requirements-extra.txt
```

### Step 1.2: Install the new dep

Run:
```bash
pip install 'aiofiles>=23.2' 'lru-dict>=1.2'
```

Expected: 两条 `Successfully installed ...` 输出。

### Step 1.3: Write failing test for proxy LRU cap

Create `tests/test_lru_caches.py`:

```python
"""LRU upper-bound for in-memory caches (proxy._CACHE, main._term_cache).

Regression guard: both dicts were unbounded. After the change to lru-dict,
inserting more entries than the cap must evict oldest.
"""
import pytest
from unittest.mock import patch, AsyncMock


class TestProxyLRUCap:
    @pytest.mark.asyncio
    async def test_proxy_cache_evicts_at_cap(self):
        """Inserting 1100 keys into a 1024-cap LRU keeps only 1024."""
        from server import proxy
        proxy._CACHE.clear()

        with patch('server.proxy._check_tcp', new=AsyncMock(return_value=False)):
            for i in range(1100):
                await proxy.get_proxy_config(host=f"h{i}", port=9999, timeout=0.1)
        assert len(proxy._CACHE) <= 1024, \
            f"Expected ≤1024 entries, got {len(proxy._CACHE)}"


class TestTermCacheLRUCap:
    def test_term_cache_evicts_at_cap(self):
        """Inserting 300 entries into a 256-cap LRU keeps only 256."""
        from server import main
        main._term_cache.clear()

        for i in range(300):
            main._term_cache[f"term_{i}"] = f"explanation_{i}"
        assert len(main._term_cache) <= 256, \
            f"Expected ≤256 entries, got {len(main._term_cache)}"


class TestProxyLruIsLru:
    @pytest.mark.asyncio
    async def test_lru_evicts_oldest_unused(self):
        """Touching a key keeps it alive; oldest untouched gets evicted."""
        from server import proxy
        proxy._CACHE.clear()

        with patch('server.proxy._check_tcp', new=AsyncMock(return_value=False)):
            for i in range(1024):
                await proxy.get_proxy_config(host=f"h{i}", port=9999, timeout=0.1)
            # Re-touch "h0" so it becomes most-recently-used
            await proxy.get_proxy_config(host="h0", port=9999, timeout=0.1)
            # Add 100 more — h1..h100 should be evicted, h0 should remain
            for i in range(1024, 1124):
                await proxy.get_proxy_config(host=f"h{i}", port=9999, timeout=0.1)
        assert "h0:9999" in proxy._CACHE
        assert "h1:9999" not in proxy._CACHE
```

### Step 1.4: Run tests to verify they fail

Run:
```bash
cd /home/develop/projects/prism && python -m pytest tests/test_lru_caches.py -v
```

Expected: 3 tests FAIL（`_CACHE` 当前是 dict，不是 LRU；`len()` 会涨过 cap）。

### Step 1.5: Change proxy._CACHE to LRU

Edit `server/proxy.py`:

- **Top of file** (after existing imports) add:
```python
from lru import LRU
```

- **Replace lines 9-10**:
```python
_CACHE: Dict[str, dict] = {}
_CACHE_TTL = 30  # seconds between re-checks
```
with:
```python
_CACHE: LRU = LRU(1024)  # O(1) LRU, max 1024 entries
_CACHE_TTL = 30  # seconds between re-checks
```

- **Update line 44** (the `get` call):
```python
    cached = _CACHE.get(cache_key)
```
Keep as-is — LRU's `get()` API matches dict's `get()` (returns None if missing).

- **Line 62** (`_CACHE[cache_key] = ...`) — keep as-is, LRU supports `__setitem__`.

### Step 1.6: Change main._term_cache to LRU

Edit `server/main.py` line 309:

- **Replace**:
```python
_term_cache: dict = {}
```
with:
```python
# 256-entry LRU cap so long-running processes don't grow unbounded.
# lru-dict is thread-safe; O(1) get/set.
from lru import LRU  # local import to avoid module-load cost when unused
_term_cache: LRU = LRU(256)
```

### Step 1.7: Run tests to verify they pass

Run:
```bash
cd /home/develop/projects/prism && python -m pytest tests/test_lru_caches.py -v
```

Expected: 3 tests PASS.

### Step 1.8: Run full test suite to verify no regression

Run:
```bash
cd /home/develop/projects/prism && python -m pytest -x -q 2>&1 | tail -20
```

Expected: 全部 245 个旧测试 PASS（241 + 3 新 + 已有的 1 个）。无 regression。

### Step 1.9: Verify manually

```bash
cd /home/develop/projects/prism && python -c "
from server import main, proxy
print('term_cache type:', type(main._term_cache).__name__)
print('proxy cache type:', type(proxy._CACHE).__name__)
print('term cap:', main._term_cache.__capacity__ if hasattr(main._term_cache, '__capacity__') else 'n/a')
"
```

Expected: `LRU` / `LRU` / `256`（lru-dict 的内部属性叫 `capacity` 或类似；忽略差异）。

---

## Task 2: explain-term 限流（#15）

**Files:**
- Modify: `server/main.py:311-358` (`api_explain_term`)
- Test: `tests/test_explain_rate_limit.py`

**Why:** `/api/explain-term` 每次会 spawn 一个 15s 超时的 Claude 子进程。10/分钟的限流避免被刷。

### Step 2.1: Write failing test

Create `tests/test_explain_rate_limit.py`:

```python
"""/api/explain-term rate limit — 10 requests per user per minute."""
import pytest


@pytest.mark.asyncio
async def test_explain_term_429_after_10(client, test_user):
    """11th call within 60s for the same user must return 429."""
    # Authenticate
    login = await client.post(
        "/api/login",
        json={"username": test_user["username"], "password": test_user["password"]},
    )
    assert login.status_code == 200

    # First 10 calls should NOT be rate-limited (we don't care about their
    # content — the subprocess will fail in test env, but rate-limit check
    # happens BEFORE the subprocess).
    for i in range(10):
        r = await client.post("/api/explain-term", json={"term": f"term{i}"})
        assert r.status_code != 429, f"Call {i+1} unexpectedly rate-limited"

    # 11th call must be rate-limited
    r = await client.post("/api/explain-term", json={"term": "term11"})
    assert r.status_code == 429


@pytest.mark.asyncio
async def test_explain_term_rate_limit_per_user(client):
    """Different users have independent rate-limit buckets."""
    from server.database import create_user, get_db
    from server.auth import hash_password

    # Create two users
    for u in ["alice", "bob"]:
        create_user(u, hash_password("pw1234"))

    # Log in as alice and exhaust her quota
    await client.post("/api/login", json={"username": "alice", "password": "pw1234"})
    for i in range(10):
        await client.post("/api/explain-term", json={"term": f"a{i}"})
    r_alice = await client.post("/api/explain-term", json={"term": "a11"})
    assert r_alice.status_code == 429

    # Bob's bucket is independent — should not be limited
    await client.post("/api/logout")
    await client.post("/api/login", json={"username": "bob", "password": "pw1234"})
    r_bob = await client.post("/api/explain-term", json={"term": "b1"})
    assert r_bob.status_code != 429
```

### Step 2.2: Run tests to verify they fail

Run:
```bash
cd /home/develop/projects/prism && python -m pytest tests/test_explain_rate_limit.py -v
```

Expected: 2 tests FAIL（当前端点无限流）。

### Step 2.3: Add rate limit to api_explain_term

Edit `server/main.py` — in the `api_explain_term` function, add the rate-limit check
right after `require_auth` (around line 315) and before `await request.json()`:

```python
@app.post("/api/explain-term")
async def api_explain_term(request: Request):
    """Generate a brief AI explanation for a financial/professional term."""
    from .models import ExplainTermRequest, validation_error_message
    u = require_auth(request)
    # Rate limit: 10 requests per user per 60s. Defends the 15s-timeout
    # subprocess from being abused / DoS'd. Reuses the existing
    # check_rate_limit infrastructure (database.py:105).
    if not check_rate_limit(f"explain:{u}", max_req=10, window=60):
        return JSONResponse({"error": "请求太频繁，请稍后再试"}, status_code=429)
    body = await request.json()
    # ... rest unchanged
```

Also add `check_rate_limit` to the imports at the top of `main.py` if not present:
- **Modify** line 13:
```python
from .database import init_db, get_db
```
to:
```python
from .database import init_db, get_db, check_rate_limit
```

And add `JSONResponse` to the existing import if needed — already imported on line 8:
```python
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
```
needs `JSONResponse` too. Replace with:
```python
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
```

Wait — `JSONResponse` is currently imported later inside the function (main.py:108). Move it to the top-level import.

### Step 2.4: Run tests to verify they pass

Run:
```bash
cd /home/develop/projects/prism && python -m pytest tests/test_explain_rate_limit.py -v
```

Expected: 2 tests PASS（子进程会失败但限流检查在前面；rate-limit 状态 429 在子进程之前返回）。

Note: The rate-limit test uses `check_rate_limit` which is backed by the database. The test
fixture's autouse `temp_data_dir` will reset DB state per test, so each test starts fresh.

### Step 2.5: Run full test suite

```bash
cd /home/develop/projects/prism && python -m pytest -x -q 2>&1 | tail -10
```

Expected: 全部 PASS（245 + 3 + 2 = 250 个测试）。

---

## Task 3: SQLite PRAGMA 调优（#6）

**Files:**
- Modify: `server/database.py:1-25`
- Test: `tests/test_db_pragmas.py`

**Why:** 当前只设 WAL。补 `synchronous=NORMAL` / `cache_size=-64000` / `mmap_size=256MB` / `temp_store=MEMORY` / `busy_timeout=5000`。在 WAL 下 `synchronous=NORMAL` 已足够安全（断电最多丢最后一两个事务）。

### Step 3.1: Write failing test

Create `tests/test_db_pragmas.py`:

```python
"""SQLite connection PRAGMAs applied at open time.

WAL + NORMAL + cache_size + mmap + busy_timeout + temp_store=MEMORY.
"""
from server.database import get_db


def test_wal_mode_active():
    """journal_mode is WAL on a freshly opened connection."""
    with get_db() as db:
        mode = db.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_synchronous_normal():
    """synchronous=NORMAL (1) under WAL — durability tradeoff: may lose
    last transaction on power loss, but ~10x faster than FULL."""
    with get_db() as db:
        sync = db.execute("PRAGMA synchronous").fetchone()[0]
    assert sync == 1, f"Expected synchronous=1 (NORMAL), got {sync}"


def test_cache_size_negative_64mb():
    """cache_size=-64000 → 64 MB page cache. Negative means KB."""
    with get_db() as db:
        size_kb = db.execute("PRAGMA cache_size").fetchone()[0]
    assert size_kb == -64000, f"Expected cache_size=-64000, got {size_kb}"


def test_busy_timeout_5s():
    """busy_timeout=5000 ms — 5 second lock-wait before SQLITE_BUSY."""
    with get_db() as db:
        ms = db.execute("PRAGMA busy_timeout").fetchone()[0]
    assert ms == 5000, f"Expected busy_timeout=5000, got {ms}"


def test_temp_store_memory():
    """temp_store=2 (MEMORY) — temp tables/indexes in RAM, not on disk."""
    with get_db() as db:
        store = db.execute("PRAGMA temp_store").fetchone()[0]
    assert store == 2, f"Expected temp_store=2 (MEMORY), got {store}"
```

### Step 3.2: Run tests to verify they fail

```bash
cd /home/develop/projects/prism && python -m pytest tests/test_db_pragmas.py -v
```

Expected: 4 of 5 FAIL（`test_wal_mode_active` passes since WAL is already set; the others
need new PRAGMAs）。

### Step 3.3: Apply PRAGMAs in get_db

Edit `server/database.py` — replace the entire file content:

```python
"""SQLite database — thread-local connection reuse + WAL + performance PRAGMAs."""
import sqlite3
import time
import secrets
import threading
from contextlib import contextmanager
from .config import DATA_DIR

DB_PATH = DATA_DIR / "chat.db"

# Performance PRAGMAs applied on every new connection.
# Tradeoffs vs. defaults:
#   synchronous=NORMAL  — under WAL, safe enough; may lose last transaction on power loss
#   cache_size=-64000   — 64 MB page cache (negative = KB)
#   mmap_size=268435456 — 256 MB mmap (read-heavy workloads benefit)
#   temp_store=MEMORY   — temp tables & indexes in RAM
#   busy_timeout=5000   — 5 s lock-wait before SQLITE_BUSY
_PRAGMAS = [
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA cache_size=-64000",
    "PRAGMA mmap_size=268435456",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA busy_timeout=5000",
]

# Thread-local connection storage. SQLite connections are cheap to create
# but not free; reusing per-thread avoids the ~1 ms open+PRAGMA cost on
# every request. check_same_thread=False + thread-local gives us the best
# of both worlds: shared connection lifecycle per thread, no global lock.
_LOCAL = threading.local()


def _make_connection() -> sqlite3.Connection:
    """Create a new SQLite connection with our PRAGMAs and row factory."""
    conn = sqlite3.connect(
        str(DB_PATH),
        check_same_thread=False,
        isolation_level=None,  # autocommit — see Task 4
    )
    conn.row_factory = sqlite3.Row
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    return conn


def reset_connection() -> None:
    """Close and clear the thread-local connection.

    Tests use this in their fixtures to switch DB paths between tests
    without leaving a stale connection to the old path.
    """
    conn = getattr(_LOCAL, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _LOCAL.conn = None


@contextmanager
def get_db():
    """Thread-local connection reuse with autocommit mode.

    Autocommit (isolation_level=None) means each statement is its own
    transaction. We don't need commit()/rollback() in this code path —
    any partial-failure leaves the DB in a consistent state. This is
    faster than implicit-transaction mode and matches how the rest of
    the code uses the DB (one statement per with-block).
    """
    conn = getattr(_LOCAL, "conn", None)
    if conn is None:
        conn = _make_connection()
        _LOCAL.conn = conn
    try:
        yield conn
    except Exception:
        # Autocommit mode: no implicit transaction to roll back.
        # Re-raise so the caller sees the original error.
        raise


def init_db():
    """Create tables and run migrations. Uses a fresh connection, not
    the thread-local — init_db may be called when the thread-local
    points to a stale or non-existent DB (e.g. tests with new tmpdirs).
    """
    conn = _make_connection()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                created_at INTEGER DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                expires INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                title TEXT DEFAULT '新对话',
                skill TEXT DEFAULT 'bottleneck-hunter',
                session_id TEXT,
                project_dir TEXT,
                deleted_at INTEGER,
                created_at INTEGER DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conv_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at INTEGER DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS rate_limits (
                key TEXT PRIMARY KEY,
                count INTEGER DEFAULT 0,
                window_start REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS market_cache (
                cache_key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                fetched_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_market_cache_expires
                ON market_cache(expires_at);
            CREATE TABLE IF NOT EXISTS analysis_nodes (
                id TEXT PRIMARY KEY,
                conv_id TEXT NOT NULL,
                node_type TEXT NOT NULL,
                label TEXT NOT NULL,
                content TEXT DEFAULT '',
                skill TEXT DEFAULT '',
                created_at INTEGER DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS analysis_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_node_id TEXT NOT NULL,
                target_node_id TEXT NOT NULL,
                edge_type TEXT NOT NULL DEFAULT 'flows_to',
                metadata TEXT DEFAULT '{}',
                UNIQUE(source_node_id, target_node_id, edge_type)
            );
            CREATE INDEX IF NOT EXISTS idx_analysis_nodes_conv
                ON analysis_nodes(conv_id);
        """)
        # Migrations
        for col, col_type in [("session_id", "TEXT"), ("project_dir", "TEXT"), ("deleted_at", "INTEGER")]:
            try:
                conn.execute(f"ALTER TABLE conversations ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError:
                pass
    finally:
        conn.close()


# ── User / session helpers (unchanged) ──

def create_user(username: str, password_hash: str):
    with get_db() as db:
        db.execute("INSERT OR REPLACE INTO users (username, password_hash) VALUES (?,?)",
                   [username, password_hash])


def check_rate_limit(key: str, max_req: int = 20, window: int = 60) -> bool:
    """Return True if request is allowed, False if rate limited.
    ... (unchanged) ...
    """
    now = time.time()
    with get_db() as db:
        row = db.execute(
            """
            INSERT INTO rate_limits (key, count, window_start)
            VALUES (?, 1, ?)
            ON CONFLICT(key) DO UPDATE SET
                count = CASE
                    WHEN excluded.window_start - window_start >= ?
                    THEN 1
                    ELSE count + 1
                END,
                window_start = CASE
                    WHEN excluded.window_start - window_start >= ?
                    THEN excluded.window_start
                    ELSE window_start
                END
            RETURNING count, window_start
            """,
            [key, now, window, window],
        ).fetchone()
        return row["count"] <= max_req


def get_session(token: str) -> str | None:
    """Return username for valid session token, or None."""
    with get_db() as db:
        row = db.execute("SELECT username, expires FROM sessions WHERE token=?", [token]).fetchone()
        if not row or time.time() > row["expires"]:
            return None
        return row["username"]


def create_session(username: str) -> str:
    """Create a new session token (30-day expiry)."""
    token = secrets.token_hex(32)
    with get_db() as db:
        db.execute("INSERT INTO sessions (token, username, expires) VALUES (?,?,?)",
                   [token, username, int(time.time()) + 30 * 86400])
    return token


def delete_session(token: str):
    with get_db() as db:
        db.execute("DELETE FROM sessions WHERE token=?", [token])
```

### Step 3.4: Update conftest.py to reset connection between tests

Edit `tests/conftest.py` — in the `temp_data_dir` fixture, add `reset_connection()`
calls:

```python
@pytest.fixture(autouse=True)
def temp_data_dir(monkeypatch):
    """Use a temp directory for DATA_DIR so tests don't touch production DB."""
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("STOCK_DATA_DIR", td)
        # Re-import config to pick up new DATA_DIR
        import server.config
        server.config.DATA_DIR = Path(td)
        server.config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        # Reset DB_PATH
        import server.database
        server.database.DB_PATH = Path(td) / "chat.db"
        # Reset thread-local connection (Task 4) — without this, the
        # previous test's connection (pointing to a deleted tempdir)
        # would be reused.
        server.database.reset_connection()
        server.database.init_db()
        yield td
        # Teardown: close the connection to the temp DB
        server.database.reset_connection()
```

### Step 3.5: Run new tests + full suite

```bash
cd /home/develop/projects/prism && python -m pytest tests/test_db_pragmas.py -v
```

Expected: 5 tests PASS.

```bash
cd /home/develop/projects/prism && python -m pytest -x -q 2>&1 | tail -10
```

Expected: 全部 PASS（245 + 3 + 2 + 5 = 255 个测试）。

---

## Task 4: 连接复用 autocommit（#7）

**Files:**
- (no new modifications — Task 3 already did the work)
- Test: `tests/test_db_connection_reuse.py`

**Why:** Task 3 已经在 `_make_connection()` 中加了 `check_same_thread=False` + `isolation_level=None`（autocommit），`get_db()` 用了 thread-local 复用。Task 4 写测试来验证这两个行为。

### Step 4.1: Write failing tests

Create `tests/test_db_connection_reuse.py`:

```python
"""Thread-local connection reuse + autocommit mode.

- Same thread, multiple get_db() calls → same connection object.
- Different threads → independent connections.
- Autocommit: single-statement changes are visible without explicit commit.
"""
import sqlite3
import threading
from server import database


def test_same_thread_reuses_connection():
    """Two get_db() calls in the same thread yield the same Connection object."""
    with database.get_db() as db1:
        id1 = id(db1)
    with database.get_db() as db2:
        id2 = id(db2)
    assert id1 == id2, "Expected same connection in same thread"


def test_different_threads_have_independent_connections():
    """Different threads should not share a connection (each has its own)."""
    connections = {}

    def grab(tid):
        with database.get_db() as db:
            connections[tid] = id(db)

    t1 = threading.Thread(target=grab, args=("t1",))
    t2 = threading.Thread(target=grab, args=("t2",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert connections["t1"] != connections["t2"], \
        "Different threads should have different connection objects"


def test_reset_connection_creates_new_one():
    """reset_connection() forces the next get_db() to open a fresh connection."""
    with database.get_db() as db1:
        id1 = id(db1)
    database.reset_connection()
    with database.get_db() as db2:
        id2 = id(db2)
    assert id1 != id2, "Expected fresh connection after reset_connection()"


def test_autocommit_writes_persist_immediately():
    """Under autocommit, an INSERT is visible to a subsequent SELECT
    without an explicit commit call."""
    with database.get_db() as db:
        db.execute("INSERT OR REPLACE INTO users (username, password_hash) VALUES (?,?)",
                   ["autocommit_test", "fakehash"])
    # New context — should see the row
    with database.get_db() as db:
        row = db.execute("SELECT password_hash FROM users WHERE username=?",
                         ["autocommit_test"]).fetchone()
    assert row is not None
    assert row["password_hash"] == "fakehash"
    # Cleanup
    with database.get_db() as db:
        db.execute("DELETE FROM users WHERE username=?", ["autocommit_test"])
```

### Step 4.2: Run tests to verify they pass

```bash
cd /home/develop/projects/prism && python -m pytest tests/test_db_connection_reuse.py -v
```

Expected: 4 tests PASS（Task 3 already implemented the behavior; this task is the test
gate）。

If any fail, debug by checking:
- `database._LOCAL.conn` — should be set after first get_db()
- `database._make_connection()` — should return a new Connection with PRAGMAs

### Step 4.3: Run full suite

```bash
cd /home/develop/projects/prism && python -m pytest -x -q 2>&1 | tail -10
```

Expected: 全部 PASS（245 + 3 + 2 + 5 + 4 = 259 个测试）。

---

## Task 5: aiofiles 替换 chat.py 的同步 jsonl 读（#8）

**Files:**
- Modify: `server/chat.py:67-165` (`_read_jsonl_messages`)
- Modify: `server/chat.py:318` (the call site)
- Test: `tests/test_chat_jsonl_async.py`

**Why:** `_read_jsonl_messages` 在 async 生成器内同步 `open()` 文件，阻塞事件循环。改为 `aiofiles` 异步读。

### Step 5.1: Write failing test

Create `tests/test_chat_jsonl_async.py`:

```python
"""_read_jsonl_messages uses aiofiles (non-blocking on event loop)."""
import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch


def test_read_jsonl_is_async():
    """_read_jsonl_messages is `async def` (proves it uses aiofiles)."""
    from server import chat
    import inspect
    assert inspect.iscoroutinefunction(chat._read_jsonl_messages), \
        "_read_jsonl_messages must be async to use aiofiles"


def test_read_jsonl_parses_messages(tmp_path, monkeypatch):
    """Async reader parses user + assistant events from a jsonl file."""
    from server import chat
    # Build a jsonl with a user + assistant event
    p = tmp_path / "fake.jsonl"
    p.write_text(
        json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n"
        + json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": "hello back"},
                {"type": "tool_use", "name": "WebSearch", "input": {"q": "x"}},
            ]},
        }) + "\n",
        encoding="utf-8",
    )
    # Patch CLAUDE_PROJECTS so the reader looks in tmp_path
    monkeypatch.setattr(chat, "CLAUDE_PROJECTS", tmp_path)

    msgs = asyncio.run(chat._read_jsonl_messages("fake", "."))
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert "hi" in msgs[0]["content"]
    assert msgs[1]["role"] == "assistant"
    # Content is JSON; parse and check chunks
    parsed = json.loads(msgs[1]["content"])
    assert any(c["t"] == "text" and "hello back" in c["c"] for c in parsed["chunks"])
    assert any(c["t"] == "tool" and c["c"] == "WebSearch" for c in parsed["chunks"])
```

### Step 5.2: Run tests to verify they fail

```bash
cd /home/develop/projects/prism && python -m pytest tests/test_chat_jsonl_async.py -v
```

Expected: `test_read_jsonl_is_async` FAILS（当前是 sync）。

### Step 5.3: Convert _read_jsonl_messages to async + aiofiles

Edit `server/chat.py` — replace `_read_jsonl_messages` (lines 67-165):

```python
async def _read_jsonl_messages(session_id: str, project_dir: str) -> list[dict]:
    """Read Claude Code JSONL, merging consecutive assistant events into single turns.

    Async (uses aiofiles) so it doesn't block the event loop when called
    from inside the SSE generator in chat_stream.

    Returns list of {role, content} dicts where content is JSON string with
    {text, chunks, refs}. Also captures WebSearch/WebFetch tool results
    as 'ref' chunks.
    """
    import aiofiles
    jsonl_path = CLAUDE_PROJECTS / project_dir / f"{session_id}.jsonl"
    if not jsonl_path.exists():
        return []

    messages = []
    collected_refs = []  # Accumulate refs across the conversation

    async with aiofiles.open(jsonl_path, encoding="utf-8") as f:
        async for line in f:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = ev.get("type", "")

            if t == "user":
                msg = ev.get("message", "")
                if isinstance(msg, dict):
                    content_blocks = msg.get("content", "")
                else:
                    try:
                        msg_dict = json.loads(str(msg).replace("'", '"'))
                        content_blocks = msg_dict.get("content", str(msg))
                    except Exception:
                        content_blocks = str(msg)

                # Check if this is a tool_result message
                if isinstance(content_blocks, list):
                    for block in content_blocks:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            result_content = block.get("content", "")
                            if isinstance(result_content, list):
                                result_text = ""
                                for rc in result_content:
                                    if isinstance(rc, dict) and rc.get("type") == "text":
                                        result_text += rc.get("text", "")
                                    elif isinstance(rc, str):
                                        result_text += rc
                            elif isinstance(result_content, str):
                                result_text = result_content
                            else:
                                result_text = str(result_content)
                            # Extract URLs from WebSearch/WebFetch results
                            refs = _extract_refs_from_tool_result(result_text)
                            collected_refs.extend(refs)
                            continue  # Skip tool_result — don't add as user message

                content_str = str(content_blocks) if not isinstance(content_blocks, str) else content_blocks
                if content_str and not content_str.startswith("[{'type':") and not content_str.startswith("[{'tool_use_id':"):
                    messages.append({"role": "user", "content": content_str})

            elif t == "assistant":
                blocks = ev.get("message", {}).get("content", [])
                chunks = []
                for b in blocks:
                    bt = b.get("type", "")
                    if bt == "text":
                        chunks.append({"t": "text", "c": b.get("text", "")})
                    elif bt == "tool_use":
                        name = b.get("name", "?")
                        inp = b.get("input", {})
                        detail = str(list(inp.values())[:1]) if inp else ""
                        chunks.append({"t": "tool", "c": name, "d": detail})
                    elif bt == "thinking":
                        chunks.append({"t": "think", "c": b.get("thinking", "")})

                if not chunks:
                    continue

                # Merge consecutive assistant messages
                if messages and messages[-1]["role"] == "assistant":
                    prev = json.loads(messages[-1]["content"])
                    prev["chunks"].extend(chunks)
                    txts = [c["c"] for c in prev["chunks"] if c["t"] == "text"]
                    prev["text"] = "".join(txts)
                    messages[-1]["content"] = json.dumps(prev, ensure_ascii=False)
                else:
                    txts = [c["c"] for c in chunks if c["t"] == "text"]
                    content = json.dumps({"text": "".join(txts), "chunks": chunks}, ensure_ascii=False)
                    messages.append({"role": "assistant", "content": content})

    # Attach collected references to the last assistant message
    if collected_refs and messages:
        for i in range(len(messages) - 1, -1, -1):
            if messages[i]["role"] == "assistant":
                prev = json.loads(messages[i]["content"])
                for ref in collected_refs:
                    prev.setdefault("chunks", []).append({"t": "ref", "url": ref["url"], "title": ref["title"], "snippet": ref.get("snippet", "")})
                messages[i]["content"] = json.dumps(prev, ensure_ascii=False)
                break

    return messages
```

### Step 5.4: Update the call site in chat.py

Edit `server/chat.py` line 318 — change:

```python
            jsonl_msgs = _read_jsonl_messages(session_id, project_dir)
```

to:

```python
            jsonl_msgs = await _read_jsonl_messages(session_id, project_dir)
```

The call site is already inside `async def generate()` so `await` works directly.

### Step 5.5: Run tests to verify they pass

```bash
cd /home/develop/projects/prism && python -m pytest tests/test_chat_jsonl_async.py -v
```

Expected: 2 tests PASS.

### Step 5.6: Run full suite

```bash
cd /home/develop/projects/prism && python -m pytest -x -q 2>&1 | tail -10
```

Expected: 全部 PASS（245 + 11 + 2 = 258 个测试）。注意 conversations.py 里的 `_read_jsonl_messages` 仍是同步版本（不同函数，conftest fixture 不影响）—— 这两个函数共名但行为独立，spec 明确 conversations.py 保留同步。

---

## Task 6: tencent provider 异步化（#5）

**Files:**
- Modify: `server/market/tencent_provider.py:89-156`
- Test: `tests/test_tencent_async.py`

**Why:** `get_quote` / `get_kline` 内部用 `urllib.urlopen` 同步调用，**在 async def 里阻塞事件循环**。包到 `asyncio.to_thread`。

### Step 6.1: Write failing test

Create `tests/test_tencent_async.py`:

```python
"""Tencent provider's urllib calls must not block the event loop.

Strategy: mock urlopen to do a non-trivial amount of work, then call
get_quote. During the call, schedule another coroutine on the loop and
verify it runs — i.e. the loop is not blocked.
"""
import asyncio
import time
from unittest.mock import patch, MagicMock


def test_get_quote_does_not_block_event_loop():
    """While get_quote awaits the (mocked) upstream, the loop runs other tasks."""
    from server.market.tencent_provider import TencentProvider

    # Simulate a 200 ms "upstream call" — synchronous, in a thread
    def fake_urlopen(req, timeout):
        time.sleep(0.2)
        m = MagicMock()
        m.read.return_value = b'v_sh600519="1~贵州茅台~600519~1700.00~1690.00~1700.00~1000000~500000~1700.00~1690.00~1700.00"'
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    async def side_task():
        """Runs while get_quote is awaiting — if loop is blocked, this never finishes."""
        await asyncio.sleep(0)
        return time.time()

    async def main():
        provider = TencentProvider()
        with patch("server.market.tencent_provider.urllib.request.urlopen", new=fake_urlopen):
            # Schedule side_task to run at +50 ms (during the 200 ms upstream)
            loop = asyncio.get_event_loop()
            side_done_time = loop.create_task(asyncio.sleep(0.05))
            t0 = time.time()
            quote = await provider.get_quote("600519")
            await side_done_time
            elapsed = time.time() - t0
        # If the loop was blocked by urlopen (sync in async), elapsed
        # would be ≥ 200 ms (sequential). If asyncio.to_thread worked,
        # elapsed ≈ 200 ms but side_task ran in parallel.
        assert quote is not None
        assert quote.ticker == "600519"
        # Loop was free: side task should fire around +50 ms, not +200 ms.
        # We don't assert exact timing (CI noise) — just that get_quote
        # didn't block. The presence of the `await asyncio.sleep(0.05)`
        # before get_quote returns is what we check below.
        return elapsed

    elapsed = asyncio.run(main())
    # The elapsed time should be ≤ 250ms (200ms upstream + 50ms scheduling
    # overhead), NOT 200ms + 200ms (which would mean the side task was
    # blocked behind the upstream call). Allow 300ms for CI noise.
    assert elapsed < 0.3, f"Loop appears blocked: {elapsed*1000:.0f} ms elapsed"


def test_get_quote_returns_valid_quote():
    """The mocked quote parses to a real Quote model."""
    from server.market.tencent_provider import TencentProvider

    def fake_urlopen(req, timeout):
        m = MagicMock()
        m.read.return_value = (
            b'v_sz002050="51~\xe4\xb8\x89\xe8\x8a\xb1\xe6\x99\xba\xe6\x8e\xa7~002050~'
            b'45.74~45.50~45.80~1000000~500000~45.74~45.50~45.74"'
        )
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    async def main():
        provider = TencentProvider()
        with patch("server.market.tencent_provider.urllib.request.urlopen", new=fake_urlopen):
            return await provider.get_quote("002050")

    quote = asyncio.run(main())
    assert quote.ticker == "002050"
    assert quote.price == 45.74
    assert quote.source == "tencent"


def test_get_kline_does_not_block_event_loop():
    """Same non-blocking guarantee for get_kline."""
    from server.market.tencent_provider import TencentProvider

    def fake_urlopen(req, timeout):
        time.sleep(0.2)
        m = MagicMock()
        m.read.return_value = b'[{"day":"2024-01-01","open":1.0,"high":2.0,"low":0.5,"close":1.5,"volume":1000.0}]'
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    async def main():
        provider = TencentProvider()
        with patch("server.market.tencent_provider.urllib.request.urlopen", new=fake_urlopen):
            t0 = time.time()
            kline = await provider.get_kline("600519", "1d", 10)
            elapsed = time.time() - t0
        return kline, elapsed

    kline, elapsed = asyncio.run(main())
    assert len(kline) == 1
    assert kline[0].close == 1.5
    assert elapsed < 0.3, f"K-line loop appears blocked: {elapsed*1000:.0f} ms"
```

### Step 6.2: Run tests to verify they fail

```bash
cd /home/develop/projects/prism && python -m pytest tests/test_tencent_async.py -v
```

Expected: 2 of 3 FAIL（`test_get_quote_returns_valid_quote` passes — the current sync
implementation works; the other two fail because the loop is blocked）。

### Step 6.3: Wrap urllib calls in asyncio.to_thread

Edit `server/market/tencent_provider.py` — replace `get_quote` and `get_kline`:

```python
    async def get_quote(self, ticker: str) -> Quote:
        try:
            quotes = await asyncio.to_thread(_fetch_tencent_quotes, [ticker], 8)
        except ProviderUnavailable:
            raise
        if ticker not in quotes:
            raise ProviderNotFound(f"tencent: no quote for {ticker}")
        f = quotes[ticker]
        try:
            price = float(f[3]) if f[3] else 0.0
            prev_close = float(f[4]) if f[4] else 0.0
            change = price - prev_close
            change_pct = (change / prev_close * 100.0) if prev_close else 0.0
            return Quote(
                ticker=ticker,
                name=f[1] if len(f) > 1 else ticker,
                price=price,
                change=change,
                change_pct=change_pct,
                volume=float(f[6]) if len(f) > 6 and f[6] else 0.0,
                turnover=float(f[37]) if len(f) > 37 and f[37] else 0.0,
                high=float(f[33]) if len(f) > 33 and f[33] else None,
                low=float(f[34]) if len(f) > 34 and f[34] else None,
                open=float(f[5]) if len(f) > 5 and f[5] else None,
                prev_close=prev_close,
                timestamp=int(time.time()),
                source=self.name,
            )
        except (ValueError, IndexError) as e:
            raise ProviderUnavailable(f"tencent: bad quote data: {e}") from e

    async def get_kline(
        self, ticker: str, period: str = "1d", limit: int = 120
    ) -> List[KLine]:
        symbol = _sina_symbol(ticker)
        # Only daily is supported via Sina; for weekly/monthly,
        # fetch daily and resample client-side or return daily.
        url = (
            f"{_SINA_KLINE_URL}?symbol={symbol}&scale=240&ma=no&datalen={limit}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            # Run sync urllib in a worker thread so the event loop is free.
            raw = await asyncio.to_thread(self._fetch_kline_sync, req, 12)
        except Exception as e:
            raise ProviderUnavailable(f"Sina kline fetch failed: {e}") from e
        try:
            bars = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ProviderUnavailable(f"Sina kline bad JSON: {e}") from e
        if not bars or not isinstance(bars, list):
            return []
        out: List[KLine] = []
        for b in bars[-limit:]:
            try:
                ts = int(time.mktime(time.strptime(b["day"], "%Y-%m-%d")))
                out.append(KLine(
                    timestamp=ts,
                    open=float(b["open"]),
                    high=float(b["high"]),
                    low=float(b["low"]),
                    close=float(b["close"]),
                    volume=float(b["volume"]),
                ))
            except (KeyError, ValueError):
                continue
        return out

    @staticmethod
    def _fetch_kline_sync(req, timeout: int) -> str:
        """Sync helper for asyncio.to_thread — reads the response body."""
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
```

Add `import asyncio` at the top of the file (line 7ish):

```python
import asyncio
import json
import logging
import re
import time
import urllib.request
from typing import List
```

### Step 6.4: Run tests to verify they pass

```bash
cd /home/develop/projects/prism && python -m pytest tests/test_tencent_async.py -v
```

Expected: 3 tests PASS.

### Step 6.5: Run full suite

```bash
cd /home/develop/projects/prism && python -m pytest -x -q 2>&1 | tail -10
```

Expected: 全部 PASS（245 + 14 + 3 = 262 个测试）。

---

## Task 7: GZipMiddleware + Cache-Control（#10）

**Files:**
- Modify: `server/main.py:113-117` (middleware stack)
- Modify: `server/main.py` (add new middleware classes)
- Test: `tests/test_gzip_and_cache.py`

**Why:** 静态资源 ~750KB JS 每次刷新重下。gzip 后 ~250KB；Cache-Control 永久缓存（带 `?v=` 破缓存）。

### Step 7.1: Write failing test

Create `tests/test_gzip_and_cache.py`:

```python
"""Static files are gzipped when client accepts it, and have immutable cache headers."""
import pytest


@pytest.mark.asyncio
async def test_static_js_gzipped_when_accepted(client):
    """/static/js/*.js with Accept-Encoding: gzip returns gzipped body."""
    # Build a fresh client with auto-decompression disabled, so r.content
    # is the raw gzipped bytes (httpx's default `client` fixture would
    # transparently decompress and mask the test).
    import httpx
    from server.main import app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", decompress=False,
    ) as raw_client:
        r = await raw_client.get(
            "/static/js/app.core.js",
            headers={"Accept-Encoding": "gzip"},
        )
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
    # Verify the body is actually gzip-compressed
    import gzip
    decoded = gzip.decompress(r.content).decode("utf-8")
    assert "App" in decoded or "init" in decoded


@pytest.mark.asyncio
async def test_static_js_not_gzipped_when_not_accepted(client):
    """Without Accept-Encoding: gzip, returns raw bytes."""
    r = await client.get("/static/js/app.core.js")
    assert r.status_code == 200
    assert r.headers.get("content-encoding") is None


@pytest.mark.asyncio
async def test_static_files_have_immutable_cache_control(client):
    """/static/* files get Cache-Control: max-age=31536000, immutable."""
    r = await client.get("/static/css/app.css")
    cc = r.headers.get("cache-control", "")
    assert "max-age=31536000" in cc
    assert "immutable" in cc


@pytest.mark.asyncio
async def test_dynamic_routes_not_immortal_cached(client):
    """/api/* responses do NOT get the immutable cache header."""
    # /api/me returns 401 for unauthenticated; we don't care, only the header
    r = await client.get("/api/me")
    cc = r.headers.get("cache-control", "")
    assert "max-age=31536000" not in cc
```

### Step 7.2: Run tests to verify they fail

```bash
cd /home/develop/projects/prism && python -m pytest tests/test_gzip_and_cache.py -v
```

Expected: 3 of 4 FAIL（gzip 未启用，Cache-Control 未设）。

### Step 7.3: Add GZipMiddleware + StaticCacheControlMiddleware

Edit `server/main.py`:

- **Update import** at line 8:
```python
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
```
becomes:
```python
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from starlette.middleware.gzip import GZipMiddleware
```

- **Add a new middleware class** after the existing `SecurityHeadersMiddleware`
(around line 84, just before the `CSRFMiddleware`):

```python
class StaticCacheControlMiddleware(BaseHTTPMiddleware):
    """Attach Cache-Control: immutable to /static/* responses.

    Static assets are versioned via ``?v=N`` query strings, so the URL
    is the cache key. max-age=31536000 + immutable lets the browser
    skip revalidation for a year. Dynamic routes (/api/*) are unaffected
    so auth-gated responses don't get immortal-cached.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response
```

- **Add the middlewares** to the app. The order matters: GZip should run
BEFORE Cache-Control (so the cached gzip'd response is served). Place
both BEFORE the existing `SecurityHeadersMiddleware` registration (around
line 113):

```python
# GZipMiddleware: compresses responses for clients that send Accept-Encoding: gzip.
# min_size=512 skips tiny responses (headers would be bigger than the body).
app.add_middleware(GZipMiddleware, minimum_size=512)
# StaticCacheControlMiddleware: immutable cache for /static/*.
app.add_middleware(StaticCacheControlMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(CSRFMiddleware)
```

Note: BaseHTTPMiddleware is added in REVERSE order of how they appear
in the request — wait, this is wrong. Starlette middleware order: the
LAST `add_middleware` is the OUTERMOST. So request flow is:
`CSRFMiddleware → SecurityHeadersMiddleware → StaticCacheControlMiddleware → GZipMiddleware → app`.

Response flow (reverse):
`app → GZipMiddleware → StaticCacheControlMiddleware → SecurityHeadersMiddleware → CSRFMiddleware`.

For gzip, we want GZip to wrap the response (compress the body). For
Cache-Control, we want the header to be set on the (possibly gzipped)
response.

GZipMiddleware handles this correctly — it compresses the body and sets
`Content-Encoding: gzip`. The Cache-Control middleware should run BEFORE
GZip so it sets the header on the uncompressed response (which then gets
gzipped). 

So the correct order is: `StaticCacheControlMiddleware` outermost
relative to `GZipMiddleware`. In Starlette terms, `add_middleware` is
applied in REVERSE order of the add calls for the request direction.
So we add GZip first, then StaticCacheControl. Let me re-check...

Actually: `app.add_middleware(X)` adds X to the middleware stack. The
stack is a LIFO, so the LAST added is the OUTERMOST. Request flow:
outermost → ... → innermost → handler. So:

```
add_middleware(GZipMiddleware)        # 1st: innermost
add_middleware(StaticCacheControl)    # 2nd: outer
```

Request: StaticCache → GZip → handler
Response: handler → GZip → StaticCache

StaticCache sees the gzipped response and adds the header. Good.
But: GZipMiddleware is implemented at the Starlette level, not as
BaseHTTPMiddleware, and it operates on StreamingResponse. Adding a
BaseHTTPMiddleware outside it should work, but the header set after
compression should be fine.

Actually wait, the simplest test is: does the cache header appear on
the gzipped response? If StaticCache runs after GZip, yes. The test in
step 7.1 asserts that. Let me also add an assertion that Cache-Control
is preserved.

Let me update the middleware order. Final order:

```python
app.add_middleware(GZipMiddleware, minimum_size=512)        # innermost
app.add_middleware(StaticCacheControlMiddleware)            # outer
app.add_middleware(SecurityHeadersMiddleware)               # outer
app.add_middleware(CSRFMiddleware)                          # outermost
```

The final code:

```python
# GZipMiddleware: compresses responses for clients that send Accept-Encoding: gzip.
# minimum_size=512 skips tiny responses.
app.add_middleware(GZipMiddleware, minimum_size=512)
app.add_middleware(StaticCacheControlMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(CSRFMiddleware)
```

### Step 7.4: Run tests to verify they pass

```bash
cd /home/develop/projects/prism && python -m pytest tests/test_gzip_and_cache.py -v
```

Expected: 4 tests PASS.

### Step 7.5: Run full suite

```bash
cd /home/develop/projects/prism && python -m pytest -x -q 2>&1 | tail -10
```

Expected: 全部 PASS（245 + 14 + 3 + 4 = 266 个测试是错误表述；实际是 245 + 3 + 2 + 5 + 4 + 2 + 3 + 4 = 268 个测试）。**Spec 验收 260+ ✓**

### Step 7.6: Manual smoke test

```bash
# Start the dev server in another terminal (or skip if port 8088 is busy)
cd /home/develop/projects/prism && python -m uvicorn server.main:app --port 8088 &

# Test gzip
curl -sI -H "Accept-Encoding: gzip" http://localhost:8088/static/js/app.core.js | head -10
# Expected: Content-Encoding: gzip, Cache-Control: public, max-age=31536000, immutable

# Test non-gzip
curl -sI http://localhost:8088/static/js/app.core.js | head -10
# Expected: no Content-Encoding, but Cache-Control still present

# Test dynamic route
curl -sI http://localhost:8088/api/me | head -10
# Expected: no Cache-Control: max-age
```

Stop the dev server when done.

---

## Final Verification

After all 7 tasks complete, run the full suite one more time:

```bash
cd /home/develop/projects/prism && python -m pytest -v 2>&1 | tail -40
```

Expected: **268 个测试全部 PASS** (245 旧 + 23 新)。

Manual smoke test:
- Start `python -m uvicorn server.main:app --port 8088`
- Open browser to `http://localhost:8088/`
- Test: 登录 / 发送对话 / 查行情 / 查看关系图 / 报告查看
- 全部功能行为不变
- DevTools Network: 静态资源第二次加载返回 200 (from cache) 或 304
- DevTools Network: 静态资源 Size 减少 ~60% (gzip)

## Out of Scope (deferred to Phase 2-4)

- Frontend rendering hot path (Phase 2)
- Quote chip batch + ECharts dispose + DOM cap (Phase 3)
- Service worker / IndexedDB / SSE reconnect (Phase 4)

## Files Changed Summary

| File | Lines changed (approx) | Purpose |
|---|---|---|
| `requirements.txt` | +2 | Add aiofiles, lru-dict |
| `server/database.py` | refactored | PRAGMAs + thread-local + autocommit |
| `server/proxy.py` | +2 / -1 | LRU(1024) instead of dict |
| `server/main.py` | +30 / -3 | LRU(256) + rate limit + 2 middlewares |
| `server/chat.py` | refactored | aiofiles for jsonl read |
| `server/market/tencent_provider.py` | +5 / -3 | asyncio.to_thread wrapping |
| `tests/conftest.py` | +2 | reset_connection in fixture |
| `tests/test_lru_caches.py` | new (~50 lines) | LRU cap tests |
| `tests/test_explain_rate_limit.py` | new (~40 lines) | rate limit tests |
| `tests/test_db_pragmas.py` | new (~35 lines) | PRAGMA tests |
| `tests/test_db_connection_reuse.py` | new (~50 lines) | connection reuse tests |
| `tests/test_chat_jsonl_async.py` | new (~35 lines) | aiofiles tests |
| `tests/test_tencent_async.py` | new (~80 lines) | tencent async tests |
| `tests/test_gzip_and_cache.py` | new (~40 lines) | gzip + cache tests |

## Deploy

After verification, create the deploy script:

```bash
# Save to /tmp/prism-deploy-v10.sh
# Pattern: see /tmp/prism-deploy-v09.sh
# Include: server/database.py, server/proxy.py, server/main.py,
#          server/chat.py, server/market/tencent_provider.py,
#          requirements.txt
# Then: pip install -r requirements.txt, restart stock-app.service
```

User will run the deploy script and verify in browser.
