"""ProviderChain — try providers in order, fall back to stale cache.

On a successful provider call the result is written to :class:`MarketCache`
under a stable key. On a provider failure, the chain moves to the next
provider. If every provider fails, the chain returns the most recent
cached value (even if expired) and flags ``stale=True``. If no cache
exists either, raises :class:`NoProviderAvailable`.
"""
import logging
import json
import time
from typing import List, Dict, Tuple, Any
from .provider import (
    MarketDataProvider, ProviderUnavailable, ProviderNotFound, NoProviderAvailable,
)
from .cache import MarketCache
from .models import Quote, KLine, Fundamentals, SearchHit

log = logging.getLogger("stock-app")


class ProviderChain:
    """Wraps an ordered list of providers with cache-backed failover."""

    def __init__(
        self,
        providers: List[MarketDataProvider],
        cache: MarketCache,
        ttls: Dict[str, int] | None = None,
    ):
        self.providers = providers
        self.cache = cache
        self.ttls = ttls or {}  # {"quote": 60, "kline": 300, "fundamentals": 86400}
        # In-flight request coalescing: concurrent callers for the same
        # coalescing_key share a single upstream task instead of all
        # hitting the provider. Without this, N concurrent users for
        # ticker 600519 would all burn provider quota in parallel.
        self._inflight: Dict[str, "asyncio.Task"] = {}

    def _cache_key(self, method: str, provider_name: str, *parts: str) -> str:
        return f"{method}:{provider_name}:" + ":".join(parts)

    async def _coalesce(self, key: str, runner):
        """Run ``runner()``; if another caller is already running for ``key``,
        await its result instead of starting a duplicate call."""
        import asyncio
        existing = self._inflight.get(key)
        if existing is not None and not existing.done():
            return await existing
        task = asyncio.create_task(runner())
        self._inflight[key] = task
        try:
            return await task
        finally:
            # Only drop the entry if it's still ours; another call may
            # have re-registered while we were awaiting.
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)

    async def _with_failover(
        self,
        method: str,
        cache_parts: tuple,
        invoke,  # async callable: (provider) -> result
        ttl_attr: str,
    ) -> Tuple[Any, bool]:
        """Run ``invoke(p)`` over each provider; cache success; fall back to stale.

        ``invoke`` is called as ``await invoke(provider)``.
        """
        last_err: Exception | None = None

        for p in self.providers:
            try:
                result = await invoke(p)
            except Exception as e:
                # Failover must survive ANY provider failure — a buggy or
                # network-blocked provider raising ValidationError /
                # ProviderNotFound / timeout must not abort the chain and
                # starve the remaining (healthy) providers.
                log.warning(f"market provider {p.name} failed: {type(e).__name__}: {e}")
                last_err = e
                continue
            # Success — cache the JSON-shaped value
            key = self._cache_key(method, p.name, *cache_parts)
            ttl = self.ttls.get(ttl_attr, 60)
            try:
                payload = json.loads(result.model_dump_json())
            except Exception:
                payload = result
            self.cache.set(key, payload, ttl=ttl)
            # Pydantic re-validate so we return the typed model with stale=False
            if isinstance(result, Quote):
                return Quote(**{**payload, "stale": False, "source": p.name}), False
            if isinstance(result, Fundamentals):
                return Fundamentals(**{**payload, "stale": False, "source": p.name}), False
            return result, False

        # All providers failed — try stale cache (any provider that had a value)
        for p in self.providers:
            key = self._cache_key(method, p.name, *cache_parts)
            val, _ = self.cache.get_stale(key)
            if val is not None:
                if method == "quote":
                    return Quote(**{**val, "stale": True}), True
                if method == "fundamentals":
                    return Fundamentals(**{**val, "stale": True}), True
                return val, True
        raise NoProviderAvailable(
            f"all providers failed: {[p.name for p in self.providers]}"
        ) from last_err

    async def get_quote(self, ticker: str) -> Tuple[Quote, bool]:
        # Fresh cache check before trying providers
        for p in self.providers:
            key = self._cache_key("quote", p.name, ticker)
            val, _ = self.cache.get(key)
            if val is not None:
                return Quote(**{**val, "stale": False}), False
        # Coalesce concurrent identical requests through one upstream call.
        co_key = f"quote:{ticker}"
        try:
            return await self._coalesce(
                co_key,
                lambda: self._with_failover(
                    "quote", (ticker,),
                    lambda p: p.get_quote(ticker), "quote",
                ),
            )
        except NoProviderAvailable:
            # All providers failed — try stale cache, marking stale=True
            for p in self.providers:
                key = self._cache_key("quote", p.name, ticker)
                val, _ = self.cache.get_stale(key)
                if val is not None:
                    return Quote(**{**val, "stale": True}), True
            raise

    async def get_kline(
        self, ticker: str, period: str = "1d", limit: int = 120
    ) -> Tuple[List[KLine], bool]:
        # Note: KLine is a list; cache the list of dicts, return as KLine list.
        async def _invoke(p):
            kls = await p.get_kline(ticker, period=period, limit=limit)
            return [KLine(**k.model_dump()) for k in kls]
        # Custom path — bypass _with_failover's list handling
        last_err = None

        # Check cache first
        for p in self.providers:
            key = self._cache_key("kline", p.name, ticker, period, str(limit))
            val, _ = self.cache.get(key)
            if val is not None:
                return [KLine(**k) for k in val], False

        # Coalesce concurrent identical requests
        co_key = f"kline:{ticker}:{period}:{limit}"

        async def _runner():
            nonlocal last_err
            for p in self.providers:
                try:
                    kls = await _invoke(p)
                except Exception as e:
                    last_err = e
                    log.warning(f"market provider {p.name} kline failed: {type(e).__name__}: {e}")
                    continue
                if not kls:
                    # An empty list is NOT a success — it means this provider
                    # has no bars for the ticker. Fall through to the next
                    # provider instead of caching/returning an empty chart.
                    last_err = ProviderNotFound(f"{p.name}: no kline for {ticker}")
                    continue
                key = self._cache_key("kline", p.name, ticker, period, str(limit))
                self.cache.set(key, [k.model_dump() for k in kls], ttl=self.ttls.get("kline", 300))
                return kls, False
            # All providers failed — fall back to stale
            for p in self.providers:
                key = self._cache_key("kline", p.name, ticker, period, str(limit))
                val, _ = self.cache.get_stale(key)
                if val is not None:
                    return [KLine(**k) for k in val], True
            raise NoProviderAvailable(
                f"all providers failed: {[p.name for p in self.providers]}"
            ) from last_err

        return await self._coalesce(co_key, _runner)

    async def get_fundamentals(self, ticker: str) -> Tuple[Fundamentals, bool]:
        co_key = f"fundamentals:{ticker}"
        return await self._coalesce(
            co_key,
            lambda: self._with_failover(
                "fundamentals", (ticker,),
                lambda p: p.get_fundamentals(ticker), "fundamentals",
            ),
        )

    async def search(self, query: str, limit: int = 10) -> List[SearchHit]:
        """Search is best-effort across all providers; returns first non-empty list."""
        for p in self.providers:
            try:
                hits = await p.search(query, limit=limit)
                if hits:
                    return hits
            except ProviderUnavailable as e:
                log.warning(f"search provider {p.name} failed: {e}")
                continue
        return []

    async def health(self) -> Dict[str, bool]:
        return {p.name: await p.health() for p in self.providers}
