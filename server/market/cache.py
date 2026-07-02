"""SQLite-backed TTL cache for market data.

Stored as JSON. ``get_or_fetch`` semantics are NOT here — providers
call ``get`` / ``set`` and the chain decides when to fall back to
stale. This keeps the cache free of provider-specific logic.
"""
import json
import time
from typing import Any, Optional
from ..database import get_db


class MarketCache:
    """TTL cache keyed by string. Values are JSON-serializable."""

    def get(self, key: str) -> tuple[Optional[Any], bool]:
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

    def get_stale(self, key: str) -> tuple[Optional[Any], bool]:
        """Return (value, True) even if expired, for fallback after provider failure."""
        with get_db() as db:
            row = db.execute(
                "SELECT value_json FROM market_cache WHERE cache_key=?",
                [key],
            ).fetchone()
        if not row:
            return None, True
        return json.loads(row["value_json"]), True

    def set(self, key: str, value: Any, ttl: int) -> None:
        """Store ``value`` under ``key`` for ``ttl`` seconds.

        Performs a lazy cleanup pass first: deletes every row whose
        ``expires_at`` is in the past. :meth:`get` skips expired rows
        so they're effectively dead weight; :meth:`get_stale` is the
        only reader of expired data and is bounded by recent misses,
        so the cleanup is safe to run on every write. Without it the
        table grows unbounded under moderate traffic.

        ``now`` uses float seconds (not int-truncated) so cleanup is
        consistent with the sub-second precision in :meth:`get`.
        """
        now = time.time()
        with get_db() as db:
            # Lazy cleanup: drop expired rows before writing the new one.
            db.execute("DELETE FROM market_cache WHERE expires_at < ?", [now])
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
