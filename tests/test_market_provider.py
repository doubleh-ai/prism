"""Provider Protocol + error types — interface contract."""
import asyncio
import pytest
from server.market.provider import (
    MarketDataProvider,
    ProviderError,
    ProviderNotFound,
    ProviderUnavailable,
    NoProviderAvailable,
)
from server.market.models import Quote, KLine, Fundamentals, SearchHit


def test_provider_error_inherits_exception():
    assert issubclass(ProviderError, Exception)


def test_provider_not_found_inherits_provider_error():
    assert issubclass(ProviderNotFound, ProviderError)


def test_provider_unavailable_inherits_provider_error():
    assert issubclass(ProviderUnavailable, ProviderError)


def test_no_provider_available_inherits_provider_error():
    assert issubclass(NoProviderAvailable, ProviderError)


def test_protocol_is_runtime_checkable():
    class Fake:
        name = "fake"
        async def get_quote(self, ticker): return Quote(
            ticker=ticker, price=1.0, timestamp=1, source="fake"
        )
        async def get_kline(self, ticker, period="1d", limit=120): return []
        async def get_fundamentals(self, ticker): return Fundamentals(
            ticker=ticker, timestamp=1, source="fake"
        )
        async def search(self, query, limit=10): return []
        async def health(self): return True

    f = Fake()
    assert isinstance(f, MarketDataProvider)
