"""market_cache table exists with the expected schema."""

from server.database import get_db


def test_market_cache_table_exists():
    with get_db() as db:
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='market_cache'"
        ).fetchall()
    assert len(rows) == 1


def test_market_cache_columns():
    with get_db() as db:
        rows = db.execute("PRAGMA table_info(market_cache)").fetchall()
    cols = {r["name"] for r in rows}
    assert {"cache_key", "value_json", "fetched_at", "expires_at"} <= cols


def test_market_cache_index_exists():
    with get_db() as db:
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_market_cache_expires'"
        ).fetchall()
    assert len(rows) == 1
