"""代理健康检测 — TCP ping + 智能 fallback"""
import asyncio
import logging
import os
from typing import Dict

log = logging.getLogger("stock-app")

_CACHE: Dict[str, dict] = {}
_CACHE_TTL = 30  # seconds between re-checks


async def _check_tcp(host: str, port: int, timeout: float = 2.0) -> bool:
    """Return True if TCP connection succeeds within timeout."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        writer.close()
        await asyncio.sleep(0)  # let close finish
        return True
    except Exception:
        return False


async def get_proxy_config(
    host: str = "127.0.0.1",
    port: int = 7890,
    timeout: float = 2.0,
) -> Dict[str, str]:
    """Return proxy env vars if reachable, empty dict otherwise.

    Results are cached for CACHE_TTL seconds to avoid repeated TCP pings.
    Set env PROXY_HOST / PROXY_PORT to override defaults.
    """
    host = os.environ.get("PROXY_HOST", host)
    try:
        port = int(os.environ.get("PROXY_PORT", str(port)))
    except ValueError:
        port = 7890

    cache_key = f"{host}:{port}"
    now = asyncio.get_event_loop().time() if asyncio.get_event_loop().is_running() else 0
    cached = _CACHE.get(cache_key)
    if cached and (now - cached.get("ts", 0)) < _CACHE_TTL:
        return cached.get("config", {})

    reachable = await _check_tcp(host, port, timeout)
    config: Dict[str, str] = {}
    if reachable:
        proxy_url = f"http://{host}:{port}"
        config = {
            "http_proxy": proxy_url,
            "https_proxy": proxy_url,
            "HTTP_PROXY": proxy_url,
            "HTTPS_PROXY": proxy_url,
        }
        log.info(f"Proxy {proxy_url} reachable")
    else:
        log.warning(f"Proxy {host}:{port} unreachable, using direct connection")

    _CACHE[cache_key] = {"ts": now, "config": config}
    return config


def clear_proxy_cache():
    """Force re-detection on next call."""
    _CACHE.clear()
