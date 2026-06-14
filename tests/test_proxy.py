"""Test proxy detection module"""
import pytest
from unittest.mock import patch, AsyncMock
import asyncio


class TestProxyDetection:
    """Test proxy health check and caching"""

    @pytest.mark.asyncio
    async def test_proxy_unreachable_returns_empty(self):
        """When proxy is unreachable, return empty config."""
        from server.proxy import get_proxy_config, clear_proxy_cache
        clear_proxy_cache()

        with patch('server.proxy._check_tcp', new=AsyncMock(return_value=False)):
            config = await get_proxy_config(host="127.0.0.1", port=9999, timeout=0.1)
            assert config == {}

    @pytest.mark.asyncio
    async def test_proxy_reachable_returns_config(self):
        """When proxy is reachable, return http_proxy + https_proxy."""
        from server.proxy import get_proxy_config, clear_proxy_cache
        clear_proxy_cache()

        with patch('server.proxy._check_tcp', new=AsyncMock(return_value=True)):
            config = await get_proxy_config(host="127.0.0.1", port=7890, timeout=0.1)
            assert config["http_proxy"] == "http://127.0.0.1:7890"
            assert config["https_proxy"] == "http://127.0.0.1:7890"

    @pytest.mark.asyncio
    async def test_proxy_config_cached(self):
        """Proxy config is cached within TTL window."""
        from server.proxy import get_proxy_config, clear_proxy_cache, _CACHE
        clear_proxy_cache()

        call_count = 0

        async def check_tcp(host, port, timeout):
            nonlocal call_count
            call_count += 1
            return True

        with patch('server.proxy._check_tcp', new=check_tcp):
            config1 = await get_proxy_config(timeout=0.1)
            config2 = await get_proxy_config(timeout=0.1)
            assert config1 == config2
            # Second call should use cache; call_count should be 1
            assert call_count == 1

    @pytest.mark.asyncio
    async def test_clear_cache_forces_recheck(self):
        """Clear cache forces re-detection."""
        from server.proxy import get_proxy_config, clear_proxy_cache

        clear_proxy_cache()
        with patch('server.proxy._check_tcp', new=AsyncMock(return_value=False)):
            config = await get_proxy_config(timeout=0.1)
            assert config == {}

        clear_proxy_cache()
        with patch('server.proxy._check_tcp', new=AsyncMock(return_value=True)):
            config = await get_proxy_config(timeout=0.1)
            assert config != {}
