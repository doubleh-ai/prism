"""MarketCache — TTL, hit/miss, stale-fallback semantics."""

import time

from server.market.cache import MarketCache


def test_set_and_get_within_ttl():
    c = MarketCache()
    c.set("k1", {"price": 100.0}, ttl=60)
    val, stale = c.get("k1")
    assert val == {"price": 100.0}
    assert stale is False


def test_get_missing_returns_none():
    c = MarketCache()
    val, stale = c.get("nope")
    assert val is None
    assert stale is False


def test_get_stale_returns_expired_value():
    c = MarketCache()
    c.set("k2", {"v": 1}, ttl=1)
    time.sleep(1.2)
    # get() returns None when expired (live call expected)
    val, _ = c.get("k2")
    assert val is None
    # get_stale() returns the value with stale=True
    val, stale = c.get_stale("k2")
    assert val == {"v": 1}
    assert stale is True


def test_invalidate_by_exact_key():
    c = MarketCache()
    c.set("a", 1, ttl=60)
    c.set("b", 2, ttl=60)
    n = c.invalidate("a")
    assert n == 1
    assert c.get("a")[0] is None
    assert c.get("b")[0] == 2


def test_value_types_preserved():
    c = MarketCache()
    c.set("list", [1, 2, 3], ttl=60)
    c.set("str", "hello", ttl=60)
    c.set("num", 42, ttl=60)
    c.set("nest", {"a": {"b": 1}}, ttl=60)
    assert c.get("list")[0] == [1, 2, 3]
    assert c.get("str")[0] == "hello"
    assert c.get("num")[0] == 42
    assert c.get("nest")[0] == {"a": {"b": 1}}


def test_set_cleans_up_expired_rows():
    """set() must drop expired rows before writing, so the table
    doesn't grow unbounded under repeated writes of fresh + expired keys."""
    from server.database import get_db

    c = MarketCache()
    # Seed: one fresh, one already-expired
    c.set("fresh", {"v": 1}, ttl=60)
    c.set("old", {"v": 2}, ttl=1)
    time.sleep(1.2)
    with get_db() as db:
        before = db.execute("SELECT COUNT(*) AS n FROM market_cache").fetchone()["n"]
    assert before == 2
    # Next set() must clean up "old"
    c.set("another", {"v": 3}, ttl=60)
    with get_db() as db:
        rows = db.execute("SELECT cache_key FROM market_cache ORDER BY cache_key").fetchall()
    keys = [r["cache_key"] for r in rows]
    assert "old" not in keys, f"expired row leaked: {keys}"
    assert "fresh" in keys
    assert "another" in keys
