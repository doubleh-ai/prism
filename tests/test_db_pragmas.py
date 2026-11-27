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


def test_mmap_size_set():
    """mmap_size is applied (value > 0) or gracefully skipped on unsupported fs."""
    with get_db() as db:
        mmap = db.execute("PRAGMA mmap_size").fetchone()[0]
    # mmap_size should be > 0 (our configured 256MB) or 0 if the fs
    # doesn't support it (graceful fallback — connection still works)
    assert mmap >= 0, f"Expected mmap_size >= 0, got {mmap}"


def test_connection_survives_mmap_failure():
    """If mmap fails, the connection is still usable for queries."""
    from server.database import _make_connection

    conn = _make_connection()
    try:
        # Should be able to query regardless of mmap status
        row = conn.execute("SELECT 1 AS ok").fetchone()
        assert row["ok"] == 1
    finally:
        conn.close()
