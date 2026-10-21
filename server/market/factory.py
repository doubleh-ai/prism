"""Build the default provider chain based on ``MARKET_PROVIDERS`` config."""

import logging

from ..config import FUNDAMENTALS_TTL, KLINE_TTL, MARKET_PROVIDERS, QUOTE_TTL
from .cache import MarketCache
from .chain import ProviderChain

log = logging.getLogger("stock-app")

_chain: ProviderChain | None = None


def _try_build_provider(name: str):
    """Instantiate a provider by name; return None if the library is missing."""
    try:
        if name == "akshare":
            from .akshare_provider import AkshareProvider

            return AkshareProvider()
        if name == "baostock":
            from .baostock_provider import BaostockProvider

            return BaostockProvider()
        if name == "tencent":
            from .tencent_provider import TencentProvider

            return TencentProvider()
        log.warning(f"Unknown market provider: {name}")
        return None
    except Exception as e:
        log.warning(f"Failed to construct provider {name}: {e}")
        return None


def get_chain() -> ProviderChain:
    """Return a process-wide singleton chain; build it lazily on first call."""
    global _chain
    if _chain is not None:
        return _chain
    cache = MarketCache()
    providers = []
    for name in MARKET_PROVIDERS:
        p = _try_build_provider(name)
        if p is not None:
            providers.append(p)
    if not providers:
        log.warning("No market providers available — endpoints will return 503")
    _chain = ProviderChain(
        providers,
        cache=cache,
        ttls={"quote": QUOTE_TTL, "kline": KLINE_TTL, "fundamentals": FUNDAMENTALS_TTL},
    )
    return _chain


def reset_chain_for_tests() -> None:
    """Reset the singleton — used by tests that mock providers."""
    global _chain
    _chain = None
