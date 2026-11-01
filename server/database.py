"""SQLite database — thread-local connection reuse + WAL + performance PRAGMAs."""

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from .config import DATA_DIR, SQLITE_MMAP_SIZE

DB_PATH = DATA_DIR / "chat.db"
_ACTIVITY_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
_ACTIVITY_RETENTION_SECONDS = 180 * 86400
_last_activity_prune = 0

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
        with suppress(sqlite3.Error):
            conn.close()
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
                created_at INTEGER DEFAULT (strftime('%s','now')),
                disabled_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                expires INTEGER NOT NULL,
                created_at INTEGER DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS admin_recovery_challenges (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                binding TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL
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
            CREATE TABLE IF NOT EXISTS saved_reports (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual',
                source_id TEXT,
                conv_id TEXT,
                ticker TEXT DEFAULT '',
                name TEXT DEFAULT '',
                title TEXT NOT NULL,
                signal TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                report_json TEXT NOT NULL,
                created_at INTEGER DEFAULT (strftime('%s','now')),
                updated_at INTEGER DEFAULT (strftime('%s','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_saved_reports_user_updated
                ON saved_reports(username, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_saved_reports_ticker
                ON saved_reports(username, ticker);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_saved_reports_source
                ON saved_reports(username, source, source_id)
                WHERE source_id IS NOT NULL;
            CREATE TABLE IF NOT EXISTS analysis_cache (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                ticker TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'deep',
                name TEXT DEFAULT '',
                signal TEXT DEFAULT '',
                report_json TEXT NOT NULL,
                timings_json TEXT DEFAULT '{}',
                elapsed REAL DEFAULT 0,
                created_at INTEGER DEFAULT (strftime('%s','now')),
                updated_at INTEGER DEFAULT (strftime('%s','now')),
                UNIQUE(username, ticker, trade_date, mode)
            );
            CREATE INDEX IF NOT EXISTS idx_analysis_cache_lookup
                ON analysis_cache(username, ticker, trade_date, mode, updated_at DESC);
            CREATE TABLE IF NOT EXISTS activity_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER DEFAULT (strftime('%s','now')),
                username TEXT DEFAULT '',
                feature TEXT NOT NULL,
                action TEXT NOT NULL,
                target_type TEXT DEFAULT '',
                target_id TEXT DEFAULT '',
                target_label TEXT DEFAULT '',
                status TEXT DEFAULT 'success',
                duration_ms INTEGER DEFAULT 0,
                meta_json TEXT DEFAULT '{}',
                request_id TEXT DEFAULT '',
                ip_hash TEXT DEFAULT '',
                user_agent_hash TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_activity_events_created
                ON activity_events(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_activity_events_user_created
                ON activity_events(username, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_activity_events_feature_created
                ON activity_events(feature, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_activity_events_action_created
                ON activity_events(action, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_activity_events_status_created
                ON activity_events(status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_activity_events_target_created
                ON activity_events(target_type, target_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS diagnostic_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT DEFAULT '',
                kind TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'error',
                component TEXT DEFAULT '',
                page TEXT DEFAULT '',
                message TEXT NOT NULL,
                stack TEXT DEFAULT '',
                fingerprint TEXT NOT NULL,
                version TEXT DEFAULT '',
                context_json TEXT DEFAULT '{}',
                occurrences INTEGER NOT NULL DEFAULT 1,
                first_seen INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                last_seen INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                resolved_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_diagnostic_open_last_seen
                ON diagnostic_events(resolved_at, last_seen DESC);
            CREATE INDEX IF NOT EXISTS idx_diagnostic_fingerprint
                ON diagnostic_events(fingerprint, version, username);
        """)
        # Migrations
        with suppress(sqlite3.OperationalError):
            conn.execute("ALTER TABLE users ADD COLUMN disabled_at INTEGER")
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN created_at INTEGER")
            conn.execute("UPDATE sessions SET created_at=strftime('%s','now') WHERE created_at IS NULL")
        except sqlite3.OperationalError:
            pass
        for col, col_type in [("session_id", "TEXT"), ("project_dir", "TEXT"), ("deleted_at", "INTEGER")]:
            with suppress(sqlite3.OperationalError):
                conn.execute(f"ALTER TABLE conversations ADD COLUMN {col} {col_type}")
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
            with suppress(sqlite3.OperationalError):
                conn.execute(f"ALTER TABLE geo_regions ADD COLUMN {_col} REAL")
        # Per-user indicator library (v0.11)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS indicators (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                dsl_json TEXT NOT NULL DEFAULT '{}',
                params_json TEXT NOT NULL DEFAULT '{}',
                plan_json TEXT NOT NULL DEFAULT '{}',
                visual_json TEXT NOT NULL DEFAULT '{}',
                source TEXT DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'plot',
                created_at INTEGER DEFAULT (strftime('%s','now')),
                updated_at INTEGER DEFAULT (strftime('%s','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_indicators_user ON indicators(username);
            CREATE TABLE IF NOT EXISTS indicator_seed_versions (
                username TEXT PRIMARY KEY,
                version INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS indicator_scan_jobs (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                indicator_id TEXT NOT NULL,
                indicator_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                config_json TEXT NOT NULL DEFAULT '{}',
                results_json TEXT NOT NULL DEFAULT '[]',
                total INTEGER NOT NULL DEFAULT 0,
                processed INTEGER NOT NULL DEFAULT 0,
                matched INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                created_at INTEGER DEFAULT (strftime('%s','now')),
                updated_at INTEGER DEFAULT (strftime('%s','now')),
                completed_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_indicator_scan_jobs_user_created
                ON indicator_scan_jobs(username, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_indicator_scan_jobs_status
                ON indicator_scan_jobs(status, updated_at DESC);
            CREATE TABLE IF NOT EXISTS vibe_plans (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                prompt TEXT NOT NULL,
                market TEXT DEFAULT 'A股',
                horizon TEXT DEFAULT 'swing',
                risk_level TEXT DEFAULT 'balanced',
                plan_json TEXT NOT NULL,
                created_at INTEGER DEFAULT (strftime('%s','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_vibe_plans_user_created
                ON vibe_plans(username, created_at DESC);
            CREATE TABLE IF NOT EXISTS capability_runs (
                run_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                conv_id TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at INTEGER NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS capability_calls (
                call_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                username TEXT NOT NULL,
                conv_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                capability_id TEXT NOT NULL,
                arguments_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                input_summary TEXT NOT NULL DEFAULT '',
                result_summary TEXT NOT NULL DEFAULT '',
                target_json TEXT NOT NULL DEFAULT '{}',
                response_json TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL,
                completed_at INTEGER,
                UNIQUE(run_id, request_id)
            );
            CREATE INDEX IF NOT EXISTS idx_capability_calls_user_conv
                ON capability_calls(username,conv_id,created_at);
            CREATE TABLE IF NOT EXISTS analysis_runs (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                ticker TEXT NOT NULL,
                name TEXT DEFAULT '',
                mode TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                status TEXT NOT NULL,
                report_json TEXT,
                signal TEXT DEFAULT '',
                error TEXT DEFAULT '',
                started_at REAL NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_analysis_runs_user_started
                ON analysis_runs(username,started_at DESC);
            CREATE TABLE IF NOT EXISTS owned_report_files (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                conv_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                filename TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_owned_report_files_owner
                ON owned_report_files(username,conv_id,created_at DESC);
            CREATE TABLE IF NOT EXISTS user_watchlist (
                username TEXT NOT NULL,
                ticker TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                position INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY(username,ticker)
            );
            CREATE TABLE IF NOT EXISTS retired_usernames (
                username TEXT PRIMARY KEY,
                retired_at INTEGER NOT NULL
            );
        """)
        from .legacy_history import init_legacy_history_db

        init_legacy_history_db(conn)
        # Preserve legacy orphaned data without assigning it to a newly created
        # account with the same name. No historical records are deleted.
        for table in (
            "conversations",
            "saved_reports",
            "indicators",
            "vibe_plans",
            "indicator_scan_jobs",
            "analysis_runs",
            "capability_runs",
            "capability_calls",
            "owned_report_files",
            "user_watchlist",
            "research_runs",
            "native_agent_runs",
            "native_agent_dialogues",
            "legacy_conversation_history",
        ):
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", [table]
            ).fetchone():
                continue
            conn.execute(
                f"INSERT OR IGNORE INTO retired_usernames(username,retired_at) "
                f"SELECT DISTINCT username,strftime('%s','now') FROM {table} t "
                "WHERE username<>'' AND NOT EXISTS(SELECT 1 FROM users u WHERE u.username=t.username)"
            )
        # Keep account isolation when an older compatible program is restored:
        # legacy code cannot silently recreate a retired username either.
        conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS users_retire_after_delete
            AFTER DELETE ON users
            BEGIN
                INSERT OR IGNORE INTO retired_usernames(username,retired_at)
                    VALUES(OLD.username,strftime('%s','now'));
                DELETE FROM sessions WHERE username=OLD.username;
                UPDATE capability_runs SET revoked=1 WHERE username=OLD.username;
            END;
            CREATE TRIGGER IF NOT EXISTS users_deny_retired_insert
            BEFORE INSERT ON users
            WHEN EXISTS(SELECT 1 FROM retired_usernames r WHERE r.username=NEW.username)
            BEGIN
                SELECT RAISE(ABORT,'retired_username');
            END;
        """)
        with suppress(sqlite3.OperationalError):
            conn.execute("ALTER TABLE capability_calls ADD COLUMN arguments_json TEXT NOT NULL DEFAULT '{}'")
        for column in ("evaluated", "insufficient", "excluded"):
            with suppress(sqlite3.OperationalError):
                conn.execute(
                    f"ALTER TABLE indicator_scan_jobs ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                )
        for _col, _spec in {
            "params_json": "TEXT NOT NULL DEFAULT '{}'",
            "plan_json": "TEXT NOT NULL DEFAULT '{}'",
            "visual_json": "TEXT NOT NULL DEFAULT '{}'",
            "source": "TEXT DEFAULT ''",
        }.items():
            with suppress(sqlite3.OperationalError):
                conn.execute(f"ALTER TABLE indicators ADD COLUMN {_col} {_spec}")
        # Seed geo data + curated industry chains
        _seed_geo_regions(conn)
        from .industry_seed import seed_all as _seed_industry

        _seed_industry(conn)
    finally:
        conn.close()


# ── User / session helpers (unchanged) ──


def create_user(username: str, password_hash: str):
    with get_db() as db:
        db.execute("SAVEPOINT create_account")
        try:
            if db.execute("SELECT 1 FROM retired_usernames WHERE username=?", [username]).fetchone():
                raise HTTPException(409, "该用户名已停用保留，不能重新创建；请使用新用户名")
            cur = db.execute(
                "INSERT OR IGNORE INTO users(username,password_hash) VALUES(?,?)", [username, password_hash]
            )
            if not cur.rowcount:
                raise HTTPException(409, "用户名已存在")
            db.execute("RELEASE create_account")
        except BaseException:
            db.execute("ROLLBACK TO create_account")
            db.execute("RELEASE create_account")
            raise


def is_username_retired(username: str) -> bool:
    with get_db() as db:
        return (
            db.execute("SELECT 1 FROM retired_usernames WHERE username=?", [username]).fetchone() is not None
        )


def list_users() -> list[dict]:
    now = int(time.time())
    with get_db() as db:
        rows = db.execute(
            """
            SELECT
                u.username,
                u.created_at,
                u.disabled_at,
                (SELECT COUNT(*) FROM sessions s WHERE s.username = u.username AND s.expires > ?) AS active_sessions,
                (SELECT COUNT(*) FROM conversations c WHERE c.username = u.username) AS conversation_count
            FROM users u
            ORDER BY u.created_at DESC, u.username ASC
            """,
            [now],
        ).fetchall()
    return [dict(row) for row in rows]


def user_exists(username: str) -> bool:
    with get_db() as db:
        row = db.execute("SELECT 1 FROM users WHERE username=?", [username]).fetchone()
    return row is not None


def set_user_password(username: str, password_hash: str) -> bool:
    with get_db() as db:
        cur = db.execute(
            "UPDATE users SET password_hash=?, disabled_at=NULL WHERE username=?", [password_hash, username]
        )
        if cur.rowcount:
            db.execute("DELETE FROM sessions WHERE username=?", [username])
        return cur.rowcount > 0


def set_user_disabled(username: str, disabled: bool) -> bool:
    disabled_at = int(time.time()) if disabled else None
    with get_db() as db:
        cur = db.execute("UPDATE users SET disabled_at=? WHERE username=?", [disabled_at, username])
        if cur.rowcount and disabled:
            db.execute("DELETE FROM sessions WHERE username=?", [username])
        return cur.rowcount > 0


def delete_user_account(username: str) -> bool:
    with get_db() as db:
        db.execute("SAVEPOINT retire_account")
        try:
            if not db.execute("SELECT 1 FROM users WHERE username=?", [username]).fetchone():
                db.execute("RELEASE retire_account")
                return False
            db.execute(
                "INSERT OR IGNORE INTO retired_usernames(username,retired_at) VALUES(?,?)",
                [username, int(time.time())],
            )
            db.execute("DELETE FROM sessions WHERE username=?", [username])
            db.execute("UPDATE capability_runs SET revoked=1 WHERE username=?", [username])
            db.execute("DELETE FROM users WHERE username=?", [username])
            db.execute("RELEASE retire_account")
            return True
        except BaseException:
            db.execute("ROLLBACK TO retire_account")
            db.execute("RELEASE retire_account")
            raise


def admin_usage_stats() -> dict:
    """Aggregate lightweight user-behavior stats for the admin console."""
    now = int(time.time())
    day_ago = now - 86400
    with get_db() as db:
        totals = db.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM users) AS users,
                (SELECT COUNT(*) FROM users WHERE disabled_at IS NULL) AS enabled_users,
                (SELECT COUNT(*) FROM users WHERE disabled_at IS NOT NULL) AS disabled_users,
                (SELECT COUNT(*) FROM sessions WHERE expires > ?) AS active_sessions,
                (SELECT COUNT(*) FROM sessions WHERE created_at >= ?) AS logins_24h,
                (SELECT COUNT(*) FROM conversations) AS conversations,
                (SELECT COUNT(*) FROM conversations WHERE deleted_at IS NULL) AS active_conversations,
                (SELECT COUNT(*) FROM conversations WHERE deleted_at IS NOT NULL) AS deleted_conversations,
                (SELECT COUNT(*) FROM conversations WHERE created_at >= ?) AS conversations_24h,
                (SELECT COUNT(*) FROM messages) AS messages,
                (SELECT COUNT(*) FROM messages WHERE role='user') AS user_messages,
                (SELECT COUNT(*) FROM messages WHERE role='assistant') AS assistant_messages,
                (SELECT COUNT(*) FROM messages WHERE created_at >= ?) AS messages_24h
            """,
            [now, day_ago, day_ago, day_ago],
        ).fetchone()
        by_user = db.execute(
            """
            SELECT
                u.username,
                u.created_at,
                u.disabled_at,
                COALESCE(s.active_sessions, 0) AS active_sessions,
                COALESCE(s.last_login_at, 0) AS last_login_at,
                COALESCE(c.conversation_count, 0) AS conversation_count,
                COALESCE(c.deleted_count, 0) AS deleted_count,
                COALESCE(m.message_count, 0) AS message_count,
                COALESCE(m.user_message_count, 0) AS user_message_count,
                COALESCE(m.assistant_message_count, 0) AS assistant_message_count,
                MAX(COALESCE(m.last_message_at, 0), COALESCE(c.last_conversation_at, 0), COALESCE(s.last_login_at, 0)) AS last_activity_at
            FROM users u
            LEFT JOIN (
                SELECT username,
                       SUM(CASE WHEN expires > ? THEN 1 ELSE 0 END) AS active_sessions,
                       MAX(created_at) AS last_login_at
                FROM sessions GROUP BY username
            ) s ON s.username = u.username
            LEFT JOIN (
                SELECT username,
                       COUNT(*) AS conversation_count,
                       SUM(CASE WHEN deleted_at IS NOT NULL THEN 1 ELSE 0 END) AS deleted_count,
                       MAX(created_at) AS last_conversation_at
                FROM conversations GROUP BY username
            ) c ON c.username = u.username
            LEFT JOIN (
                SELECT c.username,
                       COUNT(m.id) AS message_count,
                       SUM(CASE WHEN m.role='user' THEN 1 ELSE 0 END) AS user_message_count,
                       SUM(CASE WHEN m.role='assistant' THEN 1 ELSE 0 END) AS assistant_message_count,
                       MAX(m.created_at) AS last_message_at
                FROM conversations c
                LEFT JOIN messages m ON m.conv_id = c.id
                GROUP BY c.username
            ) m ON m.username = u.username
            ORDER BY last_activity_at DESC, u.created_at DESC, u.username ASC
            """,
            [now],
        ).fetchall()
    return {"totals": dict(totals), "users": [dict(row) for row in by_user]}


def _clean_activity_text(value: object, limit: int = 160) -> str:
    text = "" if value is None else str(value)
    return text.replace("\x00", "").strip()[:limit]


def _hash_activity_value(value: object) -> str:
    text = _clean_activity_text(value, 512)
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def username_from_request_token(token: str | None) -> str | None:
    """Return the user bound to a session token without raising HTTP errors."""
    if not token:
        return None
    return get_session(token)


def record_activity(
    *,
    username: str = "",
    feature: str,
    action: str,
    target_type: str = "",
    target_id: str = "",
    target_label: str = "",
    status: str = "success",
    duration_ms: int = 0,
    meta: dict | None = None,
    request=None,
    created_at: int | None = None,
) -> None:
    """Persist one coarse user activity event for admin analytics.

    ``meta`` should stay small and must not contain request bodies or secrets.
    The optional request is used only for request-id and hashed client markers.
    """
    global _last_activity_prune
    created = int(created_at or time.time())
    safe_meta = meta or {}
    try:
        meta_json = json.dumps(safe_meta, ensure_ascii=False, separators=(",", ":"))[:2000]
    except Exception:
        meta_json = "{}"
    request_id = ""
    ip_hash = ""
    ua_hash = ""
    if request is not None:
        try:
            request_id = _clean_activity_text(request.headers.get("X-Request-ID") or "", 80)
            ip_hash = _hash_activity_value(getattr(request.client, "host", "") if request.client else "")
            ua_hash = _hash_activity_value(request.headers.get("user-agent") or "")
        except Exception:
            pass
    with get_db() as db:
        if created - _last_activity_prune >= 21600:
            db.execute(
                "DELETE FROM activity_events WHERE created_at < ?",
                [created - _ACTIVITY_RETENTION_SECONDS],
            )
            _last_activity_prune = created
        db.execute(
            """
            INSERT INTO activity_events (
                created_at, username, feature, action, target_type, target_id,
                target_label, status, duration_ms, meta_json, request_id,
                ip_hash, user_agent_hash
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                created,
                _clean_activity_text(username, 80),
                _clean_activity_text(feature, 40) or "api",
                _clean_activity_text(action, 60) or "open",
                _clean_activity_text(target_type, 40),
                _clean_activity_text(target_id, 120),
                _clean_activity_text(target_label, 160),
                _clean_activity_text(status, 20) or "success",
                max(0, int(duration_ms or 0)),
                meta_json,
                request_id,
                ip_hash,
                ua_hash,
            ],
        )


def _activity_days(range_key: str | None) -> int:
    mapping = {"24h": 1, "7d": 7, "14d": 14, "30d": 30, "90d": 90}
    return mapping.get((range_key or "7d").lower(), 7)


def _activity_cutoff(range_key: str | None) -> int:
    return int(time.time()) - _activity_days(range_key) * 86400


def _row_to_activity_event(row: sqlite3.Row) -> dict:
    out = dict(row)
    try:
        out["meta"] = json.loads(out.pop("meta_json") or "{}")
    except Exception:
        out["meta"] = {}
    return out


def _percentile(values: list[int], ratio: float) -> int:
    if not values:
        return 0
    ordered = sorted(max(0, int(value or 0)) for value in values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return ordered[index]


def _activity_day_key(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, _ACTIVITY_TZ).strftime("%Y-%m-%d")


def _complete_activity_daily(
    rows: list[sqlite3.Row],
    new_user_rows: list[sqlite3.Row],
    *,
    days: int,
    now: int,
) -> list[dict]:
    values = {row["day"]: dict(row) for row in rows}
    new_users = {row["day"]: int(row["new_users"] or 0) for row in new_user_rows}
    today = datetime.fromtimestamp(now, _ACTIVITY_TZ).date()
    output = []
    for offset in range(days - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        item = values.get(day, {"day": day, "events": 0, "active_users": 0, "failed_events": 0})
        item["new_users"] = new_users.get(day, 0)
        item["success_rate"] = round(
            (int(item.get("events") or 0) - int(item.get("failed_events") or 0))
            * 100
            / max(1, int(item.get("events") or 0)),
            1,
        )
        output.append(item)
    return output


def admin_activity_overview(range_key: str = "7d") -> dict:
    """Return high-level behavior aggregates for the admin console."""
    days = _activity_days(range_key)
    now = int(time.time())
    cutoff = now - days * 86400
    prev_cutoff = cutoff - days * 86400
    day_ago = now - 86400
    with get_db() as db:
        totals = dict(
            db.execute(
                """
            SELECT
                COUNT(*) AS events,
                COUNT(DISTINCT NULLIF(username, '')) AS active_users,
                COUNT(DISTINCT feature) AS feature_count,
                SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END) AS failed_events,
                ROUND(AVG(CASE WHEN duration_ms > 0 THEN duration_ms END)) AS avg_duration_ms,
                MAX(created_at) AS last_event_at,
                SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS events_24h,
                COUNT(DISTINCT CASE WHEN created_at >= ? THEN NULLIF(username, '') END) AS active_users_24h
            FROM activity_events
            WHERE created_at >= ?
            """,
                [day_ago, day_ago, cutoff],
            ).fetchone()
        )
        previous = dict(
            db.execute(
                """
            SELECT
                COUNT(*) AS events,
                COUNT(DISTINCT NULLIF(username, '')) AS active_users
            FROM activity_events
            WHERE created_at >= ? AND created_at < ?
            """,
                [prev_cutoff, cutoff],
            ).fetchone()
        )
        feature_rows = db.execute(
            """
            SELECT
                feature,
                COUNT(*) AS events,
                COUNT(DISTINCT NULLIF(username, '')) AS active_users,
                COUNT(DISTINCT date(created_at, 'unixepoch', '+8 hours')) AS active_days,
                SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END) AS failed_events,
                ROUND(AVG(CASE WHEN duration_ms > 0 THEN duration_ms END)) AS avg_duration_ms,
                MAX(created_at) AS last_event_at
            FROM activity_events
            WHERE created_at >= ?
            GROUP BY feature
            ORDER BY events DESC, active_users DESC, feature ASC
            LIMIT 20
            """,
            [cutoff],
        ).fetchall()
        feature_duration_rows = db.execute(
            """
            SELECT feature, duration_ms
            FROM activity_events
            WHERE created_at >= ? AND duration_ms > 0
            """,
            [cutoff],
        ).fetchall()
        feature_daily_rows = db.execute(
            """
            SELECT feature, date(created_at, 'unixepoch', '+8 hours') AS day,
                   COUNT(DISTINCT NULLIF(username, '')) AS active_users
            FROM activity_events
            WHERE created_at >= ?
            GROUP BY feature, day
            """,
            [cutoff],
        ).fetchall()
        action_rows = db.execute(
            """
            SELECT feature, action, COUNT(*) AS events,
                   COUNT(DISTINCT NULLIF(username, '')) AS active_users,
                   SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END) AS failed_events,
                   ROUND(AVG(CASE WHEN duration_ms > 0 THEN duration_ms END)) AS avg_duration_ms,
                   MAX(created_at) AS last_event_at
            FROM activity_events
            WHERE created_at >= ?
            GROUP BY feature, action
            ORDER BY events DESC, active_users DESC
            LIMIT 30
            """,
            [cutoff],
        ).fetchall()
        daily_rows = db.execute(
            """
            SELECT date(created_at, 'unixepoch', '+8 hours') AS day,
                   COUNT(*) AS events,
                   COUNT(DISTINCT NULLIF(username, '')) AS active_users,
                   SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END) AS failed_events
            FROM activity_events
            WHERE created_at >= ?
            GROUP BY day
            ORDER BY day ASC
            """,
            [cutoff],
        ).fetchall()
        new_user_rows = db.execute(
            """
            SELECT date(created_at, 'unixepoch', '+8 hours') AS day, COUNT(*) AS new_users
            FROM users
            WHERE created_at >= ?
            GROUP BY day
            ORDER BY day ASC
            """,
            [cutoff],
        ).fetchall()

    totals["failed_events"] = totals.get("failed_events") or 0
    totals["events_24h"] = totals.get("events_24h") or 0
    totals["active_users_24h"] = totals.get("active_users_24h") or 0
    totals["avg_duration_ms"] = totals.get("avg_duration_ms") or 0
    previous["events"] = previous.get("events") or 0
    previous["active_users"] = previous.get("active_users") or 0
    duration_map: dict[str, list[int]] = {}
    for row in feature_duration_rows:
        duration_map.setdefault(row["feature"], []).append(int(row["duration_ms"] or 0))
    feature_dau_map: dict[str, list[int]] = {}
    for row in feature_daily_rows:
        feature_dau_map.setdefault(row["feature"], []).append(int(row["active_users"] or 0))
    feature_items = []
    for row in feature_rows:
        item = dict(row)
        daily_active = feature_dau_map.get(item["feature"], [])
        item["failed_events"] = item.get("failed_events") or 0
        item["avg_duration_ms"] = item.get("avg_duration_ms") or 0
        item["p95_duration_ms"] = _percentile(duration_map.get(item["feature"], []), 0.95)
        item["avg_dau"] = round(sum(daily_active) / max(1, days), 1)
        item["peak_dau"] = max(daily_active) if daily_active else 0
        item["event_share"] = round(int(item["events"] or 0) * 100 / max(1, int(totals["events"] or 0)), 1)
        item["events_per_user"] = round(int(item["events"] or 0) / max(1, int(item["active_users"] or 0)), 1)
        feature_items.append(item)
    return {
        "range": range_key,
        "days": days,
        "timezone": "Asia/Shanghai",
        "totals": totals,
        "previous": previous,
        "features": feature_items,
        "actions": [dict(row) for row in action_rows],
        "daily": _complete_activity_daily(daily_rows, new_user_rows, days=days, now=now),
    }


def admin_activity_events(
    *,
    range_key: str = "7d",
    username: str = "",
    feature: str = "",
    action: str = "",
    status: str = "",
    query: str = "",
    limit: int = 80,
    offset: int = 0,
) -> dict:
    """Return a filtered activity event timeline."""
    cutoff = _activity_cutoff(range_key)
    clauses = ["created_at >= ?"]
    params: list[object] = [cutoff]
    if username:
        clauses.append("username = ?")
        params.append(_clean_activity_text(username, 80))
    if feature:
        clauses.append("feature = ?")
        params.append(_clean_activity_text(feature, 40))
    if action:
        clauses.append("action = ?")
        params.append(_clean_activity_text(action, 60))
    if status:
        clauses.append("status = ?")
        params.append(_clean_activity_text(status, 20))
    if query:
        pattern = "%" + _clean_activity_text(query, 100) + "%"
        clauses.append(
            "(username LIKE ? OR feature LIKE ? OR action LIKE ? OR target_id LIKE ? OR target_label LIKE ?)"
        )
        params.extend([pattern, pattern, pattern, pattern, pattern])
    limit = max(1, min(int(limit or 80), 300))
    offset = max(0, min(int(offset or 0), 10000))
    where_sql = " AND ".join(clauses)
    with get_db() as db:
        total = int(
            db.execute(
                f"SELECT COUNT(*) FROM activity_events WHERE {where_sql}",
                params,
            ).fetchone()[0]
        )
        rows = db.execute(
            f"""
            SELECT id, created_at, username, feature, action, target_type,
                   target_id, target_label, status, duration_ms, meta_json, request_id
            FROM activity_events
            WHERE {where_sql}
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    return {
        "events": [_row_to_activity_event(row) for row in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(rows) < total,
    }


def admin_activity_users(range_key: str = "7d") -> dict:
    """Return per-user feature usage and activity recency."""
    cutoff = _activity_cutoff(range_key)
    with get_db() as db:
        rows = db.execute(
            """
            WITH filtered AS (
                SELECT * FROM activity_events WHERE created_at >= ? AND username != ''
            ),
            top_feature AS (
                SELECT username, feature, COUNT(*) AS events,
                       ROW_NUMBER() OVER (
                            PARTITION BY username
                            ORDER BY COUNT(*) DESC, feature ASC
                        ) AS rn
                FROM filtered
                GROUP BY username, feature
            ),
            top_action AS (
                SELECT username, feature, action, COUNT(*) AS events,
                       ROW_NUMBER() OVER (
                            PARTITION BY username
                            ORDER BY COUNT(*) DESC, feature ASC, action ASC
                        ) AS rn
                FROM filtered
                GROUP BY username, feature, action
            ),
            ordered AS (
                SELECT username, created_at,
                       LAG(created_at) OVER (PARTITION BY username ORDER BY created_at, id) AS prev_at
                FROM filtered
            ),
            session_counts AS (
                SELECT username,
                       SUM(CASE WHEN prev_at IS NULL OR created_at - prev_at > 1800 THEN 1 ELSE 0 END) AS sessions
                FROM ordered
                GROUP BY username
            )
            SELECT
                u.username,
                u.created_at,
                u.disabled_at,
                COUNT(e.id) AS events,
                SUM(CASE WHEN e.status = 'success' THEN 1 ELSE 0 END) AS successful_events,
                COUNT(DISTINCT e.feature) AS feature_count,
                COUNT(DISTINCT date(e.created_at, 'unixepoch', '+8 hours')) AS active_days,
                SUM(CASE WHEN e.status != 'success' THEN 1 ELSE 0 END) AS failed_events,
                ROUND(AVG(CASE WHEN e.duration_ms > 0 THEN e.duration_ms END)) AS avg_duration_ms,
                MIN(e.created_at) AS first_event_at,
                MAX(e.created_at) AS last_event_at,
                COALESCE(tf.feature, '-') AS top_feature,
                COALESCE(ta.feature || ':' || ta.action, '-') AS top_action,
                COALESCE(sc.sessions, 0) AS sessions
            FROM users u
            LEFT JOIN filtered e ON e.username = u.username
            LEFT JOIN top_feature tf
                ON tf.username = u.username AND tf.rn = 1
            LEFT JOIN top_action ta
                ON ta.username = u.username AND ta.rn = 1
            LEFT JOIN session_counts sc ON sc.username = u.username
            GROUP BY u.username, u.created_at, u.disabled_at, tf.feature, ta.feature, ta.action, sc.sessions
            ORDER BY events DESC, last_event_at DESC, u.username ASC
            """,
            [cutoff],
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["failed_events"] = item.get("failed_events") or 0
        item["successful_events"] = item.get("successful_events") or 0
        item["avg_duration_ms"] = item.get("avg_duration_ms") or 0
        item["success_rate"] = round(
            int(item["successful_events"]) * 100 / max(1, int(item.get("events") or 0)), 1
        )
        item["events_per_active_day"] = round(
            int(item.get("events") or 0) / max(1, int(item.get("active_days") or 0)), 1
        )
        out.append(item)
    return {"users": out}


def _activity_step_matches(event: dict, step: dict) -> bool:
    return (event.get("feature"), event.get("action")) in step["matches"]


def admin_activity_insights(range_key: str = "7d") -> dict:
    """Return retention, funnel, target, and latency insights for administrators."""
    days = _activity_days(range_key)
    now = int(time.time())
    cutoff = now - days * 86400
    with get_db() as db:
        rows = db.execute(
            """
            SELECT id, created_at, username, feature, action, target_type,
                   target_id, target_label, status, duration_ms, meta_json
            FROM activity_events
            WHERE created_at >= ?
            ORDER BY created_at ASC, id ASC
            """,
            [cutoff],
        ).fetchall()
        user_rows = db.execute(
            "SELECT username, created_at, disabled_at FROM users ORDER BY created_at ASC"
        ).fetchall()

    events = [dict(row) for row in rows]
    users = [dict(row) for row in user_rows]
    registered = {row["username"] for row in users}
    enabled = {row["username"] for row in users if row.get("disabled_at") is None}
    active_users: set[str] = set()
    user_days: dict[str, set[str]] = {}
    duration_values: list[int] = []
    hourly = [{"hour": hour, "events": 0, "failed_events": 0, "users": set()} for hour in range(24)]
    action_stats: dict[tuple[str, str], dict] = {}
    target_stats: dict[tuple[str, str, str], dict] = {}
    error_stats: dict[tuple[str, str, str], dict] = {}
    current_sessions: dict[str, dict] = {}
    sessions: list[dict] = []

    for event in events:
        username = event.get("username") or ""
        status = event.get("status") or "success"
        created_at = int(event.get("created_at") or 0)
        duration = max(0, int(event.get("duration_ms") or 0))
        hour = datetime.fromtimestamp(created_at, _ACTIVITY_TZ).hour
        hourly[hour]["events"] += 1
        if status != "success":
            hourly[hour]["failed_events"] += 1
        if username:
            hourly[hour]["users"].add(username)
        if duration > 0:
            duration_values.append(duration)

        if username in registered and status == "success":
            active_users.add(username)
            user_days.setdefault(username, set()).add(_activity_day_key(created_at))
            session = current_sessions.get(username)
            if session is None or created_at - session["last"] > 1800:
                session = {"username": username, "start": created_at, "last": created_at, "events": 1}
                current_sessions[username] = session
                sessions.append(session)
            else:
                session["last"] = created_at
                session["events"] += 1

        action_key = (event.get("feature") or "api", event.get("action") or "open")
        action_item = action_stats.setdefault(
            action_key,
            {
                "feature": action_key[0],
                "action": action_key[1],
                "events": 0,
                "failed_events": 0,
                "users": set(),
                "durations": [],
                "last_event_at": 0,
            },
        )
        action_item["events"] += 1
        action_item["last_event_at"] = max(action_item["last_event_at"], created_at)
        if username:
            action_item["users"].add(username)
        if duration > 0:
            action_item["durations"].append(duration)
        if status != "success":
            action_item["failed_events"] += 1
            try:
                status_code = str(json.loads(event.get("meta_json") or "{}").get("status_code") or "-")
            except Exception:
                status_code = "-"
            error_key = (action_key[0], action_key[1], status_code)
            error_item = error_stats.setdefault(
                error_key,
                {
                    "feature": action_key[0],
                    "action": action_key[1],
                    "status_code": status_code,
                    "events": 0,
                    "last_event_at": 0,
                },
            )
            error_item["events"] += 1
            error_item["last_event_at"] = max(error_item["last_event_at"], created_at)

        target_type = event.get("target_type") or ""
        target_id = event.get("target_id") or ""
        target_label = event.get("target_label") or target_id
        if target_type and target_id and target_type not in {"path", "user", "conversation", "analysis"}:
            target_key = (action_key[0], target_type, target_id)
            target_item = target_stats.setdefault(
                target_key,
                {
                    "feature": action_key[0],
                    "target_type": target_type,
                    "target_id": target_id,
                    "target_label": target_label,
                    "events": 0,
                    "users": set(),
                    "last_event_at": 0,
                },
            )
            target_item["events"] += 1
            target_item["last_event_at"] = max(target_item["last_event_at"], created_at)
            if username:
                target_item["users"].add(username)

    returning_users = {username for username, value in user_days.items() if len(value) >= 2}
    new_users = {row["username"] for row in users if int(row.get("created_at") or 0) >= cutoff}
    session_durations = [max(0, session["last"] - session["start"]) for session in sessions]
    successful_events = sum(1 for event in events if event.get("status") == "success")

    hourly_output = []
    for item in hourly:
        hourly_output.append(
            {
                "hour": item["hour"],
                "events": item["events"],
                "failed_events": item["failed_events"],
                "active_users": len(item["users"]),
            }
        )

    top_targets = []
    for item in target_stats.values():
        output = {key: value for key, value in item.items() if key != "users"}
        output["active_users"] = len(item["users"])
        top_targets.append(output)
    top_targets.sort(key=lambda item: (-item["events"], -item["active_users"], item["target_label"]))

    slow_actions = []
    for item in action_stats.values():
        durations = item["durations"]
        output = {
            "feature": item["feature"],
            "action": item["action"],
            "events": item["events"],
            "active_users": len(item["users"]),
            "failed_events": item["failed_events"],
            "failure_rate": round(item["failed_events"] * 100 / max(1, item["events"]), 1),
            "avg_duration_ms": round(sum(durations) / max(1, len(durations))) if durations else 0,
            "p95_duration_ms": _percentile(durations, 0.95),
            "max_duration_ms": max(durations) if durations else 0,
            "last_event_at": item["last_event_at"],
        }
        slow_actions.append(output)
    slow_actions.sort(key=lambda item: (-item["failure_rate"], -item["p95_duration_ms"], -item["events"]))

    funnel_specs = [
        {
            "id": "market_research",
            "label": "行情研究到分析",
            "steps": [
                {"label": "搜索标的", "matches": {("market", "search")}},
                {
                    "label": "查看行情",
                    "matches": {("market", "quote"), ("market", "kline"), ("market", "fundamentals")},
                },
                {"label": "启动分析", "matches": {("analysis", "start")}},
            ],
        },
        {
            "id": "analysis_delivery",
            "label": "A股分析交付",
            "steps": [
                {"label": "启动分析", "matches": {("analysis", "start")}},
                {"label": "打开报告", "matches": {("analysis", "open_report")}},
                {"label": "保存报告", "matches": {("report", "save_analysis_report")}},
            ],
        },
        {
            "id": "vibe_followup",
            "label": "Vibe 计划跟进",
            "steps": [
                {"label": "生成计划", "matches": {("vibe", "create_plan")}},
                {"label": "回看计划", "matches": {("vibe", "open_plan")}},
                {"label": "查看候选", "matches": {("market", "quote"), ("analysis", "start")}},
            ],
        },
    ]
    funnels = []
    successful = [event for event in events if event.get("status") == "success" and event.get("username")]
    for spec in funnel_specs:
        reached = [set() for _ in spec["steps"]]
        state: dict[str, int] = {}
        stage_events = [0 for _ in spec["steps"]]
        for event in successful:
            for index, step in enumerate(spec["steps"]):
                if _activity_step_matches(event, step):
                    stage_events[index] += 1
            username = event["username"]
            index = state.get(username, 0)
            if index < len(spec["steps"]) and _activity_step_matches(event, spec["steps"][index]):
                reached[index].add(username)
                state[username] = index + 1
        base = len(reached[0])
        funnels.append(
            {
                "id": spec["id"],
                "label": spec["label"],
                "steps": [
                    {
                        "label": step["label"],
                        "users": len(reached[index]),
                        "events": stage_events[index],
                        "conversion": round(len(reached[index]) * 100 / max(1, base), 1),
                    }
                    for index, step in enumerate(spec["steps"])
                ],
            }
        )

    error_breakdown = sorted(
        error_stats.values(),
        key=lambda item: (-item["events"], -item["last_event_at"]),
    )[:12]
    active_count = len(active_users)
    avg_daily_active = round(sum(len(value) for value in user_days.values()) / max(1, days), 1)
    return {
        "range": range_key,
        "days": days,
        "timezone": "Asia/Shanghai",
        "engagement": {
            "registered_users": len(users),
            "active_users": active_count,
            "new_users": len(new_users),
            "returning_users": len(returning_users),
            "returning_rate": round(len(returning_users) * 100 / max(1, active_count), 1),
            "dormant_users": len(enabled - active_users),
            "sessions": len(sessions),
            "events_per_active_user": round(len(events) / max(1, active_count), 1),
            "events_per_session": round(len(events) / max(1, len(sessions)), 1),
            "avg_session_duration_sec": round(sum(session_durations) / max(1, len(session_durations))),
            "avg_active_days": round(
                sum(len(value) for value in user_days.values()) / max(1, active_count), 1
            ),
            "avg_daily_active": avg_daily_active,
            "stickiness": round(avg_daily_active * 100 / max(1, active_count), 1),
            "success_rate": round(successful_events * 100 / max(1, len(events)), 1),
            "p95_duration_ms": _percentile(duration_values, 0.95),
            "slow_events": sum(1 for value in duration_values if value >= 3000),
        },
        "hourly": hourly_output,
        "funnels": funnels,
        "top_targets": top_targets[:15],
        "slow_actions": slow_actions[:15],
        "errors": error_breakdown,
    }


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
    """Return username for a valid session token, or None."""
    with get_db() as db:
        row = db.execute(
            """
            SELECT s.username, s.expires
            FROM sessions s
            JOIN users u ON u.username = s.username
            WHERE s.token=? AND u.disabled_at IS NULL
            """,
            [token],
        ).fetchone()
        if not row or time.time() > row["expires"]:
            return None
        return row["username"]


def create_session(username: str) -> str:
    """Create a new session token (30-day expiry)."""
    token = secrets.token_hex(32)
    with get_db() as db:
        now = int(time.time())
        db.execute(
            "INSERT INTO sessions (token, username, expires, created_at) VALUES (?,?,?,?)",
            [token, username, now + 30 * 86400, now],
        )
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
    (
        "US",
        "美国",
        "North America",
        '["芯片设计","航空航天","制药","AI","云计算"]',
        "CHIPS法案，对华技术封锁",
    ),
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
        with suppress(Exception):
            conn.execute(
                "INSERT OR IGNORE INTO geo_regions (country_code, country_name, continent, key_industries, industrial_policy_summary, updated_at) VALUES (?,?,?,?,?,?)",
                [code, name, continent, industries, policy, now],
            )
