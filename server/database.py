"""SQLite 数据库 — context manager + WAL 模式 + 软删除"""
import sqlite3
import time
import secrets
from contextlib import contextmanager
from .config import DATA_DIR

DB_PATH = DATA_DIR / "chat.db"


@contextmanager
def get_db():
    """Context manager yielding a sqlite3.Connection with row_factory set."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create tables and run migrations."""
    with get_db() as db:
        db.executescript("""
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
        """)
        # Migrations
        for col, col_type in [("session_id", "TEXT"), ("project_dir", "TEXT"), ("deleted_at", "INTEGER")]:
            try:
                db.execute(f"ALTER TABLE conversations ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError:
                pass


def create_user(username: str, password_hash: str):
    with get_db() as db:
        db.execute("INSERT OR REPLACE INTO users (username, password_hash) VALUES (?,?)",
                   [username, password_hash])


def check_rate_limit(key: str, max_req: int = 20, window: int = 60) -> bool:
    """Return True if request is allowed, False if rate limited."""
    now = time.time()
    with get_db() as db:
        row = db.execute("SELECT count, window_start FROM rate_limits WHERE key=?", [key]).fetchone()
        if row and (now - row["window_start"]) < window:
            if row["count"] >= max_req:
                return False
            db.execute("UPDATE rate_limits SET count=count+1 WHERE key=?", [key])
        else:
            db.execute("INSERT OR REPLACE INTO rate_limits (key, count, window_start) VALUES (?,1,?)",
                       [key, now])
    return True


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
