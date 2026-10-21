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


class _FakeStockFrame:
    empty = False

    def __init__(self, rows):
        self.rows = rows

    def iterrows(self):
        for idx, row in enumerate(self.rows):
            yield idx, row


@pytest.mark.asyncio
async def test_akshare_search_ranks_exact_code_and_initials(monkeypatch):
    from server.market import akshare_provider as provider_mod

    monkeypatch.setattr(provider_mod, "_AKSHARE_AVAILABLE", True)
    monkeypatch.setattr(
        provider_mod,
        "_stock_list_cache",
        lambda: _FakeStockFrame([
            {"code": "300600", "name": "国瑞科技"},
            {"code": "300601", "name": "康泰生物"},
            {"code": "300607", "name": "拓斯达"},
            {"code": "600519", "name": "贵州茅台"},
        ]),
    )

    provider = provider_mod.AkshareProvider()

    hits = await provider.search("300607", limit=5)
    assert hits[0].ticker == "300607"
    assert hits[0].name == "拓斯达"

    hits = await provider.search("sz300607", limit=5)
    assert hits[0].ticker == "300607"

    hits = await provider.search("tsd", limit=5)
    assert hits[0].ticker == "300607"

    hits = await provider.search("gzm", limit=5)
    assert hits[0].ticker == "600519"
