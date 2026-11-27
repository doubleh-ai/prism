"""SQLite-backed TTL cache for market data.

Stored as JSON. ``get_or_fetch`` semantics are NOT here — providers
call ``get`` / ``set`` and the chain decides when to fall back to
stale. This keeps the cache free of provider-specific logic.
"""

import json
import time
from typing import Any

from ..database import get_db


class MarketCache:
    """TTL cache keyed by string. Values are JSON-serializable."""

    def __init__(self, max_stale_seconds: int = 86400):
        # Retention is separate from freshness. Never return unbounded old data.
        self.max_stale_seconds = max(0, int(max_stale_seconds))

    def get(self, key: str) -> tuple[Any | None, bool]:
        """Return (value, stale).

        ``stale`` is always False here — use :meth:`get_stale` for the
        fallback path that bypasses TTL.
        """
        with get_db() as db:
            row = db.execute(
                "SELECT value_json, expires_at FROM market_cache WHERE cache_key=?",
                [key],
            ).fetchone()
        if not row:
            return None, False
        if time.time() > row["expires_at"]:
            return None, False
        return json.loads(row["value_json"]), False

    def get_stale(self, key: str) -> tuple[Any | None, bool]:
        """Return retained data after a provider failure, with its stale flag."""
        with get_db() as db:
            row = db.execute(
                "SELECT value_json, expires_at FROM market_cache WHERE cache_key=?",
                [key],
            ).fetchone()
        if not row or time.time() > row["expires_at"] + self.max_stale_seconds:
            return None, True
        return json.loads(row["value_json"]), True

    def set(self, key: str, value: Any, ttl: int) -> None:
        """Store ``value`` under ``key`` for ``ttl`` seconds.

        Prune only beyond stale retention. Writing another ticker must not
        erase a still usable fallback for this ticker.
        """
        now = time.time()
        with get_db() as db:
            db.execute("DELETE FROM market_cache WHERE expires_at < ?", [now - self.max_stale_seconds])
            db.execute(
                """
                INSERT INTO market_cache (cache_key, value_json, fetched_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    value_json=excluded.value_json,
                    fetched_at=excluded.fetched_at,
                    expires_at=excluded.expires_at
                """,
                [key, json.dumps(value, ensure_ascii=False), int(now), now + ttl],
            )

    def invalidate(self, key: str) -> int:
        """Delete a single key. Returns rows affected (0 or 1)."""
        with get_db() as db:
            cur = db.execute("DELETE FROM market_cache WHERE cache_key=?", [key])
        return cur.rowcount
