"""Actual SDK-worker lifetimes, with synthetic socket results and no network."""

import asyncio
import os
import threading
from types import SimpleNamespace

import pytest

from server import work_lifecycle as work


@pytest.fixture
def managed_baostock(monkeypatch):
    from server.market import baostock_provider as provider

    manager = work.Manager(max_workers=4)
    manager.declare("market")
    manager.declare("scan")
    monkeypatch.setattr(provider, "get_manager", lambda: manager)
    monkeypatch.setattr(provider, "_LOGIN_SDK", None)
    monkeypatch.setattr(provider, "_LOGIN_GENERATION", None)
    yield provider, manager
    manager._pool.shutdown(wait=True, cancel_futures=False)


async def wait_event(event):
    for _ in range(400):
        if event.is_set():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("synthetic worker did not reach its gate")


class GatedSDK:
    """A paginated SDK that detects a second query overwriting its socket."""

    def __init__(self, stage):
        self.stage = stage
        self.entered, self.release, self.second_entered = (threading.Event() for _ in range(3))
        self.mutex = threading.Lock()
        self.active, self.maximum = 0, 0
        self.socket_owner = None
        self.calls = []
        self.login_count = 0
        self.row_count = 0
        self.worker_threads = set()

    def step(self, name, code=None):
        with self.mutex:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.calls.append((name, code))
            self.worker_threads.add(threading.get_ident())
        try:
            if code == "B":
                self.second_entered.set()
            if name == self.stage and (code == "A" or name == "login" and self.login_count == 1):
                self.entered.set()
                assert self.release.wait(5), "test must release its SDK thread"
        finally:
            with self.mutex:
                self.active -= 1

    def login(self):
        self.login_count += 1
        self.step("login")
        return SimpleNamespace(error_code="0")

    def query_history_k_data_plus(self, code, fields, **kwargs):
        self.step("query", code)
        assert self.socket_owner is None, "the prior socket result was not drained"
        self.socket_owner = code
        sdk = self

        class Result:
            fields = ["code"]
            error_code = "0"
            consumed = False

            def next(self):
                if self.consumed:
                    assert sdk.socket_owner == code
                    sdk.socket_owner = None
                    sdk.calls.append(("drained", code))
                    return False
                sdk.step("drain", code)
                assert sdk.socket_owner == code
                self.consumed = True
                return True

            def get_row_data(self):
                assert sdk.socket_owner == code
                sdk.row_count += 1
                return [code]

        return Result()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["login", "query", "drain"])
@pytest.mark.parametrize("waiter_exit", ["timeout", "cancel"])
@pytest.mark.parametrize("second_instance", [False, True])
async def test_cancelled_or_timed_out_sdk_transaction_keeps_global_lock_and_original_future(
    managed_baostock, monkeypatch, stage, waiter_exit, second_instance
):
    provider, manager = managed_baostock
    sdk = GatedSDK(stage)
    monkeypatch.setattr(provider, "bs", sdk)
    monkeypatch.setattr(provider, "BAOSTOCK_TIMEOUT", 0.05 if waiter_exit == "timeout" else 2)
    first = provider.BaostockProvider()
    second = provider.BaostockProvider() if second_instance else first
    task_a = asyncio.create_task(first._query("A", "code", "2024-01-01", "d", "3"))
    task_b = None
    try:
        await wait_event(sdk.entered)
        if waiter_exit == "timeout":
            with pytest.raises(provider.ProviderUnavailable, match="timeout"):
                await task_a
        else:
            task_a.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task_a
        assert manager.observe()["pending_cf"] == 1
        monkeypatch.setattr(provider, "BAOSTOCK_TIMEOUT", 2)
        task_b = asyncio.create_task(second._query("B", "code", "2024-01-01", "d", "3"))
        for _ in range(100):
            if manager.observe()["pending_cf"] == 2:
                break
            await asyncio.sleep(0.005)
        assert manager.observe()["pending_cf"] == 2
        await asyncio.sleep(0.03)
        assert not sdk.second_entered.is_set()
        assert not task_b.done()
        manager.begin_drain(manager.ticket())
        assert not await manager.wait_drained(0.02)
        assert manager.observe()["leases"]["market"] == 2
        sdk.release.set()
        assert await task_b == [{"code": "B"}]
        assert await manager.wait_drained(1)
        assert manager.observe()["pending_cf"] == 0
        assert sdk.maximum == 1
        assert sdk.row_count == 2 and sdk.socket_owner is None
        assert sdk.calls.index(("drained", "A")) < sdk.calls.index(("query", "B"))
        assert threading.get_ident() not in sdk.worker_threads
        # Login is shared across provider instances, not an instance-local flag.
        assert sdk.login_count == 1
    finally:
        sdk.release.set()
        await asyncio.gather(*(task for task in (task_a, task_b) if task is not None), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_stage", ["login", "query", "drain"])
async def test_failed_transaction_forces_login_for_next_provider(managed_baostock, monkeypatch, fail_stage):
    provider, _ = managed_baostock
    sdk = GatedSDK("none")
    monkeypatch.setattr(provider, "bs", sdk)
    first = provider.BaostockProvider()
    assert await first._query("A", "code", "2024-01-01", "d", "3") == [{"code": "A"}]

    def fail(*args, **kwargs):
        raise provider.ProviderUnavailable("synthetic SDK failure")

    if fail_stage == "login":
        # An intervening SDK user invalidates the previously reusable login.
        provider.run_serial_transaction(lambda: None)
        original = sdk.login
        sdk.login = fail
    elif fail_stage == "query":
        original = sdk.query_history_k_data_plus
        sdk.query_history_k_data_plus = fail
    else:
        original = first._drain
        monkeypatch.setattr(provider.BaostockProvider, "_drain", staticmethod(fail))
    with pytest.raises(provider.ProviderUnavailable, match="synthetic SDK failure"):
        await first._query("B", "code", "2024-01-01", "d", "3")
    sdk.socket_owner = None
    if fail_stage == "login":
        sdk.login = original
    elif fail_stage == "query":
        sdk.query_history_k_data_plus = original
    else:
        monkeypatch.setattr(provider.BaostockProvider, "_drain", staticmethod(original))
    previous = sdk.login_count
    assert await provider.BaostockProvider()._query("C", "code", "2024-01-01", "d", "3") == [{"code": "C"}]
    assert sdk.login_count == previous + 1


def test_serial_transaction_does_not_strip_or_overwrite_proxy_environment(monkeypatch):
    from server.market.baostock_provider import run_serial_transaction

    names = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")
    for name in names:
        monkeypatch.setenv(name, "synthetic-proxy-before")
    entered, release = threading.Event(), threading.Event()
    observed, failed = [], []

    def sdk_call():
        observed.append({name: os.environ.get(name) for name in names})
        entered.set()
        assert release.wait(2)
        raise RuntimeError("synthetic SDK failure")

    def worker():
        try:
            run_serial_transaction(sdk_call)
        except RuntimeError:
            failed.append(True)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(2)
        assert all(os.environ[name] == "synthetic-proxy-before" for name in names)
        for name in names:
            monkeypatch.setenv(name, "synthetic-proxy-after")
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive() and failed == [True]
    assert observed == [dict.fromkeys(names, "synthetic-proxy-before")]
    assert all(os.environ[name] == "synthetic-proxy-after" for name in names)
