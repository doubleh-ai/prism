"""ProviderChain — failover order, dead-provider skip, stale fallback."""
import pytest
import asyncio
from server.market.chain import ProviderChain
from server.market.cache import MarketCache
from server.market.provider import (
    MarketDataProvider, ProviderUnavailable, NoProviderAvailable,
)
from server.market.models import Quote, KLine, Fundamentals, SearchHit


class FakeProvider:
    def __init__(self, name, quote=None, fail=False, raise_notfound=False):
        self.name = name
        self._quote = quote or Quote(
            ticker="X", price=1.0, timestamp=1, source=name,
        )
        self._fail = fail
        self._raise_notfound = raise_notfound
        self.calls = 0

    async def get_quote(self, ticker):
        self.calls += 1
        if self._raise_notfound:
            raise ProviderUnavailable(f"{self.name} simulated not-found")
        if self._fail:
            raise ProviderUnavailable(f"{self.name} down")
        return self._quote.model_copy(update={"source": self.name, "ticker": ticker})

    async def get_kline(self, ticker, period="1d", limit=120):
        if self._fail:
            raise ProviderUnavailable(f"{self.name} down")
        return []

    async def get_fundamentals(self, ticker):
        if self._fail:
            raise ProviderUnavailable(f"{self.name} down")
        return Fundamentals(ticker=ticker, timestamp=1, source=self.name)

    async def search(self, query, limit=10):
        if self._fail:
            raise ProviderUnavailable(f"{self.name} down")
        return []

    async def health(self):
        return not self._fail


@pytest.mark.asyncio
async def test_first_provider_succeeds():
    cache = MarketCache()
    a = FakeProvider("a")
    b = FakeProvider("b")
    chain = ProviderChain([a, b], cache=cache)
    quote, stale = await chain.get_quote("600519")
    assert quote.source == "a"
    assert stale is False
    assert a.calls == 1
    assert b.calls == 0


@pytest.mark.asyncio
async def test_failover_to_second_provider():
    cache = MarketCache()
    a = FakeProvider("a", fail=True)
    b = FakeProvider("b")
    chain = ProviderChain([a, b], cache=cache)
    quote, stale = await chain.get_quote("600519")
    assert quote.source == "b"
    assert a.calls == 1
    assert b.calls == 1


@pytest.mark.asyncio
async def test_stale_fallback_when_all_fail():
    cache = MarketCache()
    # Pre-seed cache with an EXPIRED entry so the chain treats it as stale
    # (chain.py checks fresh cache first and returns immediately; only when
    # cache.get() returns None does it try providers and fall back to get_stale).
    import time
    cache.set("quote:akshare:600519", {"ticker": "600519", "price": 999.0,
                                        "timestamp": 1, "source": "akshare"}, ttl=1)
    time.sleep(1.2)
    a = FakeProvider("akshare", fail=True)
    b = FakeProvider("baostock", fail=True)
    chain = ProviderChain([a, b], cache=cache)
    quote, stale = await chain.get_quote("600519")
    assert quote.price == 999.0
    assert stale is True


@pytest.mark.asyncio
async def test_no_provider_no_cache_raises():
    cache = MarketCache()
    a = FakeProvider("a", fail=True)
    b = FakeProvider("b", fail=True)
    chain = ProviderChain([a, b], cache=cache)
    with pytest.raises(NoProviderAvailable):
        await chain.get_quote("600519")


@pytest.mark.asyncio
async def test_health_reports_per_provider():
    cache = MarketCache()
    a = FakeProvider("a", fail=True)
    b = FakeProvider("b")
    chain = ProviderChain([a, b], cache=cache)
    h = await chain.health()
    assert h == {"a": False, "b": True}


@pytest.mark.asyncio
async def test_caches_successful_result():
    cache = MarketCache()
    a = FakeProvider("a")
    chain = ProviderChain([a], cache=cache, ttls={"quote": 60})
    await chain.get_quote("600519")
    # Second call: provider NOT called again, served from cache
    a.calls = 0
    quote, stale = await chain.get_quote("600519")
    assert quote.price == 1.0
    assert stale is False
    assert a.calls == 0


@pytest.mark.asyncio
async def test_concurrent_requests_coalesce_to_single_upstream_call():
    """When N callers hit a fresh-cache miss simultaneously for the same
    ticker, the chain must make exactly 1 upstream call, not N."""
    cache = MarketCache()

    class SlowProvider:
        name = "slow"

        def __init__(self):
            self.calls = 0

        async def get_quote(self, ticker):
            self.calls += 1
            # Hold the call long enough that concurrent callers pile up
            await asyncio.sleep(0.2)
            return Quote(ticker=ticker, price=10.0, timestamp=1, source=self.name)

        async def get_kline(self, *a, **k): return []
        async def get_fundamentals(self, *a, **k): return Fundamentals(ticker=a[0], timestamp=1, source=self.name)
        async def search(self, *a, **k): return []
        async def health(self): return True

    p = SlowProvider()
    chain = ProviderChain([p], cache=cache)
    results = await asyncio.gather(*[chain.get_quote("600519") for _ in range(10)])
    assert all(r[0].price == 10.0 for r in results)
    assert p.calls == 1, f"expected 1 upstream call, got {p.calls}"
