"""Recovery admits only observed history and real owned market work."""

import asyncio
import threading
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from server import recovery
from server.market import factory
from server.market.cache import MarketCache
from server.market.chain import ProviderChain
from server.market.models import Quote
from server.work_adapters import WorkAdmissionMiddleware
from server.work_lifecycle import KINDS, LifecycleError, Manager, get_manager


@pytest.mark.asyncio
async def test_recovery_lifespan_disables_every_unexposed_producer(monkeypatch):
    monkeypatch.delenv("PRISM_WORK_CONTROL", raising=False)
    application = SimpleNamespace(state=SimpleNamespace())
    async with recovery.lifespan(application):
        manager = application.state.work_manager
        assert manager is application.state.work_runtime.manager
        assert manager.observe()["producer_unknown"] == 0
        for kind in KINDS - {"request", "market"}:
            with pytest.raises(LifecycleError, match="work_kind_rejected"):
                manager.admit(kind)
        market = manager.admit("market")
        market.release()
    assert manager.observe()["state"] == "closed"


@pytest.mark.asyncio
async def test_recovery_fence_keeps_actual_market_thread_until_result_is_cached(monkeypatch, auth_cookies):
    manager = Manager()
    for kind in KINDS:
        manager.declare(kind, enabled=kind in {"request", "market"})
    monkeypatch.setattr(recovery.app.state, "work_manager", manager, raising=False)
    entered, release = threading.Event(), threading.Event()

    class Provider:
        name = "synthetic"

        async def get_quote(self, ticker):
            def read():
                entered.set()
                assert release.wait(5)
                return Quote(ticker=ticker, price=10, timestamp=1, source=self.name)

            return await get_manager().run_blocking("market", read)

    cache = MarketCache()
    chain = ProviderChain([Provider()], cache)
    monkeypatch.setattr(factory, "_chain", chain)
    async with AsyncClient(transport=ASGITransport(app=recovery.app), base_url="http://test") as client:
        request = asyncio.create_task(client.get("/api/quote/600519", cookies=auth_cookies))
        try:
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            assert entered.is_set() and manager.observe()["pending_cf"] == 1
            manager.begin_drain(manager.ticket())
            assert await manager.wait_drained(0.01) is False
            denied = await client.get("/api/quote/600519", cookies=auth_cookies)
            assert denied.status_code == 503
            history = await client.get("/api/convs", cookies=auth_cookies)
            assert history.status_code == 200
            assert not request.done() and manager.observe()["pending_cf"] == 1
        finally:
            release.set()
        result = await request
        assert result.status_code == 200 and result.json()["price"] == 10
        assert await manager.wait_drained(1) is True
        saved, _ = cache.get("quote:synthetic:600519")
        assert saved["price"] == 10 and saved["source"] == "synthetic"
        manager.close_development()
        held = await client.get("/health/ready")
        assert held.status_code == 503 and held.json()["error"] == "maintenance_hold"


def test_recovery_observer_wraps_the_existing_boundary():
    classes = [item.cls for item in recovery.app.user_middleware]
    assert classes.index(WorkAdmissionMiddleware) < classes.index(recovery.RecoveryBoundary)


@pytest.mark.asyncio
async def test_recovery_http_disconnect_preserves_shared_read_and_cache(monkeypatch, auth_cookies):
    manager = Manager()
    for kind in KINDS:
        manager.declare(kind, enabled=kind in {"request", "market"})
    monkeypatch.setattr(recovery.app.state, "work_manager", manager, raising=False)
    entered, release = threading.Event(), threading.Event()

    class Provider:
        name = "synthetic"

        async def get_quote(self, ticker):
            def read():
                entered.set()
                assert release.wait(5)
                return Quote(ticker=ticker, price=13, timestamp=1, source=self.name)

            return await get_manager().run_blocking("market", read)

    cache = MarketCache()
    chain = ProviderChain([Provider()], cache)
    monkeypatch.setattr(factory, "_chain", chain)
    async with AsyncClient(transport=ASGITransport(app=recovery.app), base_url="http://test") as client:
        request = asyncio.create_task(client.get("/api/quote/000001", cookies=auth_cookies))
        try:
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            assert entered.is_set()
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            manager.begin_drain(manager.ticket())
            assert manager.observe()["pending_cf"] == 1
            assert chain._inflight["quote:000001"].done() is False
            assert await manager.wait_drained(0.01) is False
        finally:
            release.set()
        assert await manager.wait_drained(1) is True
        saved, _ = cache.get("quote:synthetic:000001")
        assert saved["price"] == 13
        manager.close_development()
