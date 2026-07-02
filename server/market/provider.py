"""Provider Protocol — the contract every data source must satisfy.

Provider implementations live in sibling modules (``akshare_provider``,
``baostock_provider``). They may be sync or async internally; the
public surface here is async, and the chain handles mixing.
"""
from typing import Protocol, runtime_checkable, List
from .models import Quote, KLine, Fundamentals, SearchHit


class ProviderError(Exception):
    """Base class for all provider errors."""


class ProviderNotFound(ProviderError):
    """The provider has no data for the requested ticker (404 equivalent)."""


class ProviderUnavailable(ProviderError):
    """The provider is up but failed (timeout, network, rate limit, ImportError)."""


class NoProviderAvailable(ProviderError):
    """All providers in the chain failed and no cache fallback exists."""


@runtime_checkable
class MarketDataProvider(Protocol):
    """The contract every market data provider must satisfy."""
    name: str

    async def get_quote(self, ticker: str) -> Quote: ...
    async def get_kline(
        self, ticker: str, period: str = "1d", limit: int = 120
    ) -> List[KLine]: ...
    async def get_fundamentals(self, ticker: str) -> Fundamentals: ...
    async def search(self, query: str, limit: int = 10) -> List[SearchHit]: ...
    async def health(self) -> bool: ...
