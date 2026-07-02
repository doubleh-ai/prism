"""LRU upper-bound for in-memory caches (proxy._CACHE, main._term_cache)."""
import pytest
from unittest.mock import patch, AsyncMock


class TestProxyLRUCap:
    @pytest.mark.asyncio
    async def test_proxy_cache_evicts_at_cap(self):
        from server import proxy
        proxy._CACHE.clear()

        with patch('server.proxy._check_tcp', new=AsyncMock(return_value=False)):
            for i in range(1100):
                await proxy.get_proxy_config(host=f"h{i}", port=9999, timeout=0.1)
        assert len(proxy._CACHE) <= 1024, \
            f"Expected ≤1024 entries, got {len(proxy._CACHE)}"


class TestTermCacheLRUCap:
    def test_term_cache_evicts_at_cap(self):
        from server import main
        main._term_cache.clear()

        for i in range(300):
            main._term_cache[f"term_{i}"] = f"explanation_{i}"
        assert len(main._term_cache) <= 256, \
            f"Expected ≤256 entries, got {len(main._term_cache)}"


class TestProxyLruIsLru:
    @pytest.mark.asyncio
    async def test_lru_evicts_oldest_unused(self):
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
