"""Actual resource lifetimes for scanner/analysis/market with synthetic business data."""

import asyncio
import sys
import threading
import time
import types

import pandas as pd
import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from server import analysis_engine as analysis
from server.auth import create_session
from server.database import get_db
from server.indicators import router as indicators
from server.indicators import scanner
from server.market.cache import MarketCache
from server.market.chain import ProviderChain
from server.market.models import Quote
from server.market.provider import NoProviderAvailable, ProviderUnavailable
from server.work_adapters import WorkAdmissionMiddleware
from server.work_lifecycle import Manager, bind_context, bind_manager, get_manager


@pytest.fixture
def manager():
    value = Manager(max_workers=4)
    yield value
    # Fixture teardown only; blocked work is never cleared or made a drain proof.
    value._pool.shutdown(wait=True, cancel_futures=False)


async def until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    pytest.fail("actual resource state did not converge")


def frame():
    return pd.DataFrame(
        {
            "t": [1, 2],
            "open": [10.0, 11.0],
            "high": [11.0, 12.0],
            "low": [9.0, 10.0],
            "close": [10.0, 11.0],
            "volume": [100.0, 110.0],
            "amount": [1000.0, 1210.0],
        }
    )


def indicator():
    # Fundamentals disables the optional real HTTP fast path in these tests.
    return {
        "id": "lifecycle-indicator",
        "name": "synthetic PE",
        "kind": "signal",
        "dsl": {"lt": [{"fund": "pe"}, 20]},
    }


def make_job(owner, tickers=None):
    return scanner.create_job(
        owner,
        indicator(),
        {
            "scope": "watchlist",
            "tickers": tickers or ["600519", "000001"],
            "period": "1d",
            "max_results": 100,
        },
    )


async def test_wait_result_preserves_actual_child_completion_and_waiter_cancel(manager):
    started, release = threading.Event(), threading.Event()
    children = []

    def block():
        started.set()
        release.wait(timeout=5)

    async def owner(lease):
        children.append(asyncio.create_task(manager.run_blocking("market", block)))
        await until(started.is_set)
        return "business result"

    handle = manager.start("market", owner)
    callbacks = []
    handle.add_done_callback(callbacks.append)
    try:
        assert await handle.wait_result() == "business result"
        assert not handle.done() and not callbacks
        waiting = asyncio.create_task(handle.wait())
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        manager.begin_drain(manager.ticket())
        assert manager.observe()["pending_cf"] == 1
        assert not await manager.wait_drained(timeout=0.01)
        release.set()
        await asyncio.gather(*children)
        assert await handle.wait() == "business result"
        await until(lambda: callbacks == [handle])
        manager.close_development()
    finally:
        release.set()
        await asyncio.gather(*children, return_exceptions=True)


async def test_scanner_cooperative_cancel_retains_current_thread_partial_and_terminal(manager, test_user):
    started, release = threading.Event(), threading.Event()
    job = make_job(test_user["username"])

    def read(ticker):
        if ticker == "000001":
            started.set()
            release.wait(timeout=5)
        return frame(), {"pe": 10}

    async def build(ticker, *args, **kwargs):
        return await get_manager().run_blocking("scan", read, ticker)

    with bind_context(manager):
        scanner.start_job(job, indicator(), build)
    handle = scanner._TASKS[job["id"]]
    try:
        await until(lambda: started.is_set() and job["processed"] == 1)
        assert scanner.cancel_job(job["id"], test_user["username"])
        assert scanner.get_job(job["id"], test_user["username"])["status"] == "cancelling"
        assert not handle.done() and scanner._TASKS[job["id"]] is handle
        with pytest.raises(HTTPException) as denied:
            scanner.delete_job(job["id"], test_user["username"])
        assert denied.value.status_code == 409
        manager.begin_drain(manager.ticket())
        assert manager.observe()["pending_cf"] == 1
        assert not await manager.wait_drained(timeout=0.01)
        release.set()
        await handle.wait()
        await until(lambda: job["id"] not in scanner._TASKS)
        saved = scanner.get_job(job["id"], test_user["username"])
        assert saved["status"] == "cancelled" and saved["processed"] == 2
        assert saved["matched"] == len(saved["matches"]) == 2
        assert saved["completed_at"] and manager.observe()["blocked"] == 0
        assert await manager.wait_drained(timeout=2)
        assert scanner.delete_job(job["id"], test_user["username"])
        manager.close_development()
    finally:
        release.set()
        await until(lambda: manager.observe()["pending_cf"] == 0)


async def test_foreign_active_scan_delete_http_404_does_not_disclose_or_cancel(manager, test_user):
    release, started = asyncio.Event(), asyncio.Event()
    job = make_job(test_user["username"], ["600519"])

    async def build(*args, **kwargs):
        started.set()
        await release.wait()
        return frame(), {"pe": 10}

    with get_db() as db:
        db.execute("INSERT INTO users(username,password_hash) VALUES('foreign','synthetic')")
    foreign_cookie = {"token": create_session("foreign")}
    app = FastAPI()
    app.include_router(indicators.router)
    with bind_context(manager):
        scanner.start_job(job, indicator(), build)
    handle = scanner._TASKS[job["id"]]
    try:
        await started.wait()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.delete("/api/indicators/scan-jobs/" + job["id"], cookies=foreign_cookie)
        assert response.status_code == 404
        assert scanner.get_job(job["id"], test_user["username"])["status"] == "running"
        assert job["id"] not in scanner._CANCEL_REQUESTED
        release.set()
        await handle.wait()
    finally:
        release.set()
        await until(lambda: handle.done())


async def test_scan_progress_db_failure_waits_other_native_async_workers(manager, test_user):
    release, pending, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    tickers = [f"{index:06d}" for index in range(1, 12)]
    job = make_job(test_user["username"], tickers)
    with get_db() as db:
        db.execute(
            "CREATE TRIGGER reject_progress BEFORE UPDATE ON indicator_scan_jobs "
            "WHEN NEW.processed=10 AND NEW.status='running' "
            "BEGIN SELECT RAISE(FAIL,'synthetic progress write failure'); END"
        )

    async def build(ticker, *args, **kwargs):
        if ticker == "000011":
            pending.set()
            await release.wait()
            finished.set()
        return frame(), {"pe": 10}

    with bind_context(manager):
        scanner.start_job(job, indicator(), build)
    handle = scanner._TASKS[job["id"]]
    try:
        await pending.wait()
        await until(lambda: job["processed"] == 10)
        await asyncio.sleep(0.02)
        assert not handle.done() and job["id"] in scanner._TASKS
        manager.begin_drain(manager.ticket())
        assert not await manager.wait_drained(timeout=0.01)
        assert manager.observe()["pending_async"] >= 1
        release.set()
        await handle.wait()
        assert finished.is_set()
        saved = scanner.get_job(job["id"], test_user["username"])
        assert saved["status"] == "failed" and saved["processed"] == 11
        assert saved["matched"] == 11
        assert await manager.wait_drained(timeout=2)
        manager.close_development()
    finally:
        release.set()
        await until(finished.is_set)


async def test_unknown_scan_owner_cancel_does_not_cancel_workers_or_clear_registry(manager, test_user):
    release, pending, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    job = make_job(test_user["username"], ["600519"])

    async def build(*args, **kwargs):
        pending.set()
        try:
            await release.wait()
            return frame(), {"pe": 10}
        finally:
            finished.set()

    with bind_context(manager):
        scanner.start_job(job, indicator(), build)
    handle = scanner._TASKS[job["id"]]
    try:
        await pending.wait()
        # Deliberately inject an UNKNOWN owner cancellation, unlike cancel_job.
        handle._task.cancel()
        await until(lambda: manager.observe()["blocked"] > 0)
        assert not finished.is_set() and not handle.done()
        manager.begin_drain(manager.ticket())
        assert not await manager.wait_drained(timeout=0.01)
        release.set()
        await finished.wait()
        await asyncio.sleep(0.02)
        assert manager.observe()["blocked"] > 0 and not handle.done()
        assert not await manager.wait_drained(timeout=0.01)
    finally:
        release.set()
        await until(finished.is_set)


async def test_scan_terminal_db_failure_stays_blocked(manager, test_user):
    job = make_job(test_user["username"], ["600519"])
    with get_db() as db:
        db.execute(
            "CREATE TRIGGER reject_terminal BEFORE UPDATE ON indicator_scan_jobs "
            "WHEN NEW.status IN ('completed','failed','cancelled') "
            "BEGIN SELECT RAISE(FAIL,'synthetic terminal write failure'); END"
        )

    async def build(*args, **kwargs):
        return frame(), {"pe": 10}

    with bind_context(manager):
        scanner.start_job(job, indicator(), build)
    handle = scanner._TASKS[job["id"]]
    await until(lambda: manager.observe()["blocked"] > 0)
    assert not handle.done() and job["id"] in scanner._TASKS
    manager.begin_drain(manager.ticket())
    assert not await manager.wait_drained(timeout=0.01)


async def test_market_waiter_cancel_keeps_shared_real_thread_handle(manager):
    started, release = threading.Event(), threading.Event()

    class Provider:
        name = "synthetic"
        calls = 0

        async def get_quote(self, ticker):
            self.calls += 1

            def read():
                started.set()
                release.wait(timeout=5)
                return Quote(ticker=ticker, price=10, timestamp=1, source=self.name)

            return await get_manager().run_blocking("market", read)

    provider = Provider()
    chain = ProviderChain([provider], MarketCache())
    with bind_context(manager):
        first = asyncio.create_task(chain.get_quote("600519"))
        second = asyncio.create_task(chain.get_quote("600519"))
    try:
        await until(started.is_set)
        handle = chain._inflight["quote:600519"]
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        manager.begin_drain(manager.ticket())
        assert chain._inflight["quote:600519"] is handle and not handle.done()
        assert manager.observe()["pending_cf"] == 1 and provider.calls == 1
        assert not await manager.wait_drained(timeout=0.01)
        release.set()
        quote, stale = await second
        assert quote.price == 10 and stale is False and provider.calls == 1
        await until(lambda: not chain._inflight)
        assert await manager.wait_drained(timeout=2)
        manager.close_development()
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)


async def test_market_completed_unavailable_is_retryable_without_permanent_block(manager):
    class Provider:
        name = "synthetic"
        calls = 0

        async def get_quote(self, ticker):
            self.calls += 1
            if self.calls == 1:
                raise ProviderUnavailable("synthetic normal outage")
            return Quote(ticker=ticker, price=12, timestamp=1, source=self.name)

    provider = Provider()
    chain = ProviderChain([provider], MarketCache())
    with bind_context(manager):
        with pytest.raises(NoProviderAvailable):
            await chain.get_quote("600519")
        await until(lambda: not chain._inflight)
        quote, stale = await chain.get_quote("600519")
    assert quote.price == 12 and not stale and provider.calls == 2
    assert manager.observe()["blocked"] == 0


async def test_market_timeout_fallback_returns_before_cf_exit_and_reuses_owned_result(manager):
    started, release = threading.Event(), threading.Event()

    class TimedOut:
        name = "timeout"
        calls = 0

        async def get_fundamentals(self, ticker):
            self.calls += 1

            def read():
                started.set()
                release.wait(timeout=5)
                return None

            try:
                return await asyncio.wait_for(get_manager().run_blocking("market", read), timeout=0.02)
            except TimeoutError:
                raise ProviderUnavailable("synthetic budget elapsed") from None

    class Healthy:
        name = "healthy"
        calls = 0

        async def get_fundamentals(self, ticker):
            from server.market.models import Fundamentals

            self.calls += 1
            return Fundamentals(ticker=ticker, timestamp=1, source=self.name)

    slow, healthy = TimedOut(), Healthy()
    chain = ProviderChain([slow, healthy], MarketCache())
    try:
        with bind_context(manager):
            first = await asyncio.wait_for(chain.get_fundamentals("600519"), timeout=1)
            assert first[0].source == "healthy" and not first[1]
            handle = chain._inflight["fundamentals:600519"]
            assert not handle.done() and manager.observe()["pending_cf"] == 1
            again = await asyncio.wait_for(chain.get_fundamentals("600519"), timeout=0.5)
        assert again[0].source == "healthy" and slow.calls == healthy.calls == 1
        manager.begin_drain(manager.ticket())
        assert not await manager.wait_drained(timeout=0.01)
        release.set()
        await handle.wait()
        await until(lambda: not chain._inflight)
        assert await manager.wait_drained(timeout=2)
        manager.close_development()
    finally:
        release.set()
        await until(lambda: manager.observe()["pending_cf"] == 0)


@pytest.mark.parametrize("failure", ["cancel", "base"])
async def test_market_unknown_owner_termination_stays_blocked(manager, failure):
    class UnknownTermination(BaseException):
        pass

    async def runner():
        if failure == "cancel":
            raise asyncio.CancelledError()
        raise UnknownTermination()

    chain = ProviderChain([], MarketCache())
    with bind_context(manager):
        with pytest.raises((asyncio.CancelledError, UnknownTermination)):
            await chain._coalesce("synthetic", runner)
    await until(lambda: manager.observe()["blocked"] > 0)
    assert not chain._inflight["synthetic"].done()
    manager.begin_drain(manager.ticket())
    assert not await manager.wait_drained(timeout=0.01)


def setup_graph(monkeypatch, *, stream=None, close=None):
    defaults = types.ModuleType("tradingagents.default_config")
    defaults.DEFAULT_CONFIG = {}
    monkeypatch.setitem(sys.modules, defaults.__name__, defaults)
    monkeypatch.setattr(analysis, "_ensure_astock", lambda: None)
    monkeypatch.setattr(analysis, "ASTOCK_CACHE_TTL_SECONDS", 0)
    monkeypatch.setattr(analysis, "_ENGINE_RUN_LOCK", threading.Lock())
    monkeypatch.setattr(analysis, "_extract_structured", lambda *args: None)

    class Graph:
        def __init__(self, **kwargs):
            self.graph = self

        def prepare_graph_run(self, *args):
            return {"messages": []}, {}, None

        def stream(self, *args, **kwargs):
            if stream:
                stream()
            yield {"final_trade_decision": "public synthetic complete decision"}

        def finalize_graph_run(self, *args):
            return "synthetic"

        def close_graph_run(self):
            if close:
                close()

    module = types.ModuleType("tradingagents.graph.trading_graph")
    module.TradingAgentsGraph = Graph
    monkeypatch.setitem(sys.modules, module.__name__, module)


async def test_analysis_sse_disconnect_keeps_actual_executor_through_cleanup_and_persist(
    manager, test_user, monkeypatch
):
    started, release = threading.Event(), threading.Event()
    cleanup_started, cleanup_release = threading.Event(), threading.Event()

    def stream():
        started.set()
        release.wait(timeout=5)

    def close():
        cleanup_started.set()
        cleanup_release.wait(timeout=5)

    setup_graph(monkeypatch, stream=stream, close=close)
    with bind_context(manager):
        aid = await analysis.start_analysis("600519", username=test_user["username"], force_refresh=True)
    try:
        await until(started.is_set)
        events = analysis.stream_events(aid)
        await anext(events)
        await events.aclose()
        manager.begin_drain(manager.ticket())
        assert manager.observe()["pending_cf"] == 1
        assert not await manager.wait_drained(timeout=0.01)
        release.set()
        await until(cleanup_started.is_set)
        assert analysis.get_analysis_record(aid, test_user["username"])["status"] == "done"
        # A terminal business report is insufficient while actual cleanup runs.
        assert manager.observe()["pending_cf"] == 1
        assert manager.observe()["persist_pending"] == 1
        assert not await manager.wait_drained(timeout=0.01)
        cleanup_release.set()
        assert await manager.wait_drained(timeout=3)
        with get_db() as db:
            assert db.execute("SELECT status FROM analysis_runs WHERE id=?", [aid]).fetchone()[0] == "done"
        manager.close_development()
    finally:
        release.set()
        cleanup_release.set()
        await until(lambda: manager.observe()["pending_cf"] == 0)


@pytest.mark.parametrize("failure", ["cleanup", "terminal_writer"])
async def test_analysis_real_cleanup_or_terminal_db_writer_failure_blocks(
    manager, test_user, monkeypatch, failure
):
    def close():
        if failure == "cleanup":
            raise RuntimeError("synthetic cleanup failure")
        # The actual worker already saved done. Reject the terminal writer's
        # subsequent actual upsert, rather than mocking persistence as success.
        with get_db() as db:
            db.execute(
                "CREATE TRIGGER reject_analysis_terminal BEFORE UPDATE ON analysis_runs "
                "WHEN NEW.status IN ('done','error') "
                "BEGIN SELECT RAISE(FAIL,'synthetic terminal writer failure'); END"
            )

    setup_graph(monkeypatch, close=close)
    with bind_context(manager):
        aid = await analysis.start_analysis("600519", username=test_user["username"], force_refresh=True)
    await until(lambda: manager.observe()["blocked"] > 0)
    await until(lambda: manager.observe()["pending_cf"] == 0)
    manager.begin_drain(manager.ticket())
    assert not await manager.wait_drained(timeout=0.01)
    assert manager.observe()["blocked"] == 1
    if failure == "terminal_writer":
        assert manager.observe()["persist_blocked"] == 1
    with get_db() as db:
        assert db.execute("SELECT status FROM analysis_runs WHERE id=?", [aid]).fetchone()[0] == "done"


async def test_asgi_scope_binds_fenced_manager_for_history_and_unlisted_producer(manager):
    app = FastAPI()
    bind_manager(app, manager)
    app.add_middleware(WorkAdmissionMiddleware)
    called = {"universe": False, "producer": False}

    @app.get("/api/history")
    async def history():
        assert get_manager() is manager
        return {"history": []}

    @app.get("/api/indicators/universe/summary")
    async def universe():
        called["universe"] = True
        return {}

    @app.get("/api/unlisted-producer")
    async def producer():
        called["producer"] = True
        chain = ProviderChain([], MarketCache())
        await chain.get_quote("600519")
        return {}

    manager.begin_drain(manager.ticket())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/history")).status_code == 200
        assert (await client.get("/api/indicators/universe/summary")).status_code == 503
        assert (await client.get("/api/unlisted-producer")).status_code == 503
    assert called == {"universe": False, "producer": True}
    assert not sum(manager.observe()["leases"].values())
    manager.close_development()
