"""SQLite database — thread-local connection reuse + WAL + performance PRAGMAs."""
import sqlite3
import time
import secrets
import threading
from contextlib import contextmanager
from .config import DATA_DIR, SQLITE_MMAP_SIZE

DB_PATH = DATA_DIR / "chat.db"

# Performance PRAGMAs applied on every new connection.
# Tradeoffs vs. defaults:
#   synchronous=NORMAL  — under WAL, safe enough; may lose last transaction on power loss
#   cache_size=-64000   — 64 MB page cache (negative = KB)
#   mmap_size=268435456 — 256 MB mmap (read-heavy workloads benefit)
#   temp_store=MEMORY    — temp tables & indexes in RAM
#   busy_timeout=5000   — 5 s lock-wait before SQLITE_BUSY
_PRAGMAS = [
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA cache_size=-64000",
    f"PRAGMA mmap_size={SQLITE_MMAP_SIZE}",
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
        try:
            conn.execute(pragma)
        except Exception:
            if "mmap" in pragma.lower():
                # sqlite3 can fall back to regular I/O if mmap is unsupported.
                # Don't kill the connection for this — log and continue.
                import logging
                logging.getLogger("prism.db").warning(
                    "PRAGMA failed (non-fatal, falling back to regular I/O): %s", pragma
                )
            else:
                conn.close()
                raise
    return conn


def reset_connection() -> None:
    """Close and clear the thread-local connection.

    Tests use this in their fixtures to switch DB paths between tests
    without leaving a stale connection to the old path.

    Note: caller must ensure no active get_db() context exists on the
    current thread, otherwise the underlying connection will be closed
    mid-use.
    """
    conn = getattr(_LOCAL, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
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
        # ── Phase 6: Industry chain graph ──
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS industry_nodes (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                country TEXT,
                region TEXT,
                lat REAL,
                lng REAL,
                description TEXT,
                market_share REAL,
                concentration TEXT,
                tier INTEGER DEFAULT 0,
                source_ticker TEXT,
                source_date TEXT,
                confidence REAL DEFAULT 0.5,
                created_at INTEGER DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS industry_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_node_id TEXT NOT NULL REFERENCES industry_nodes(id),
                target_node_id TEXT NOT NULL REFERENCES industry_nodes(id),
                relation TEXT NOT NULL,
                strength REAL DEFAULT 0.5,
                evidence TEXT,
                source_ticker TEXT,
                source_date TEXT,
                created_at INTEGER DEFAULT (strftime('%s','now')),
                UNIQUE(source_node_id, target_node_id, relation)
            );
            CREATE TABLE IF NOT EXISTS geo_regions (
                country_code TEXT PRIMARY KEY,
                country_name TEXT NOT NULL,
                continent TEXT,
                key_industries TEXT,
                industrial_policy_summary TEXT,
                lat REAL,
                lng REAL,
                updated_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_industry_nodes_cat ON industry_nodes(category);
            CREATE INDEX IF NOT EXISTS idx_industry_nodes_country ON industry_nodes(country);
            CREATE INDEX IF NOT EXISTS idx_industry_edges_source ON industry_edges(source_node_id);
            CREATE INDEX IF NOT EXISTS idx_industry_edges_target ON industry_edges(target_node_id);
        """)
        # geo_regions lat/lng migration (added after the initial Phase 6 rollout)
        for _col in ["lat", "lng"]:
            try:
                conn.execute(f"ALTER TABLE geo_regions ADD COLUMN {_col} REAL")
            except sqlite3.OperationalError:
                pass
        # Per-user indicator library (v0.11)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS indicators (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                dsl_json TEXT NOT NULL DEFAULT '{}',
                kind TEXT NOT NULL DEFAULT 'plot',
                created_at INTEGER DEFAULT (strftime('%s','now')),
                updated_at INTEGER DEFAULT (strftime('%s','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_indicators_user ON indicators(username);
        """)
        # Seed geo data + curated industry chains
        _seed_geo_regions(conn)
        from .industry_seed import seed_all as _seed_industry
        _seed_industry(conn)
    finally:
        conn.close()


# ── User / session helpers (unchanged) ──

def create_user(username: str, password_hash: str):
    with get_db() as db:
        db.execute("INSERT OR REPLACE INTO users (username, password_hash) VALUES (?,?)",
                   [username, password_hash])


def check_rate_limit(key: str, max_req: int = 20, window: int = 60) -> bool:
    """Return True if request is allowed, False if rate limited.

    The check-then-update is performed atomically using a single
    ``INSERT ... ON CONFLICT ... RETURNING`` so two concurrent requests
    cannot both pass when only one slot remains. The whole
    transaction holds an exclusive lock for its duration; with WAL
    that means a single writer at a time, which is fine for this
    workload.
    """
    now = time.time()
    with get_db() as db:
        # Single round-trip: try to claim a slot. If the row exists and
        # the window is still open, increment; if it's expired, reset
        # to (1, now); if no row, insert (1, now). The RETURNING gives
        # us the post-update count to compare against the cap.
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
        # Allow the request iff the count after the increment is ≤ cap.
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


# ── Phase 6: Industry chain seed data ──
_GEO_SEED = [
    ("CN", "中国", "Asia", '["稀土","光伏","锂电池","电动车","5G","高铁"]', "新能源补贴退坡，芯片自主化政策"),
    ("TW", "台湾", "Asia", '["先进制程芯片","封装测试","电子代工","PCB"]', "芯片法案限制对华出口"),
    ("KR", "韩国", "Asia", '["存储芯片","显示面板","造船","动力电池"]', "CHIPS法案补贴，对华设备出口管制"),
    ("JP", "日本", "Asia", '["半导体材料","精密设备","汽车","工业机器人"]', "半导体材料出口管制"),
    ("US", "美国", "North America", '["芯片设计","航空航天","制药","AI","云计算"]', "CHIPS法案，对华技术封锁"),
    ("DE", "德国", "Europe", '["汽车","工业自动化","化工","精密机械"]', "能源转型，供应链多元化"),
    ("AU", "澳大利亚", "Oceania", '["锂矿","铁矿石","煤炭","天然气"]', "关键矿产战略，限制中资收购"),
    ("CL", "智利", "South America", '["锂矿","铜矿"]', "锂矿国有化讨论"),
    ("SA", "沙特", "Middle East", '["石油","石化","氢能"]', "愿景2030，新能源转型"),
    ("IN", "印度", "Asia", '["仿制药","IT服务","稀土替代","电子组装"]', "PLI制造补贴，对华替代战略"),
    ("VN", "越南", "Asia", '["电子组装","纺织","家具"]', "承接中国产业转移"),
    ("MY", "马来西亚", "Asia", '["芯片封装测试","棕榈油","石油天然气"]', "半导体供应链枢纽"),
]


def _seed_geo_regions(conn):
    now = int(__import__("time").time())
    for code, name, continent, industries, policy in _GEO_SEED:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO geo_regions (country_code, country_name, continent, key_industries, industrial_policy_summary, updated_at) VALUES (?,?,?,?,?,?)",
                [code, name, continent, industries, policy, now],
            )
        except Exception:
            pass
