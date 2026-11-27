"""Actual local children and kernel birth identity; never a cgroup drain proof."""

import asyncio
import ctypes
import subprocess
import sys
from pathlib import Path

import pytest

from server.services import process_lease as adapter
from server.work_lifecycle import Manager, current_lease


def test_actual_child_birth_identity_stable_and_reaped():
    manager = Manager()
    lease = manager.admit("auxiliary")
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys;sys.stdin.buffer.read()"], stdin=subprocess.PIPE
    )
    try:
        birth = adapter.process_start_ticks(process.pid)
        assert birth > 0 and adapter.process_start_ticks(process.pid) == birth
        assert adapter.register_process(lease, process)
        lease.release()
        manager.begin_drain(manager.ticket())
        assert manager.observe()["pending_processes"] == 1
        process.stdin.close()
        assert lease.wait_process_sync(process) == 0
        manager.close_development()
    finally:
        process.wait()
        manager._pool.shutdown(wait=True, cancel_futures=False)


def test_linux_stat_parser_handles_embedded_parentheses_and_rejects_wrong_pid(monkeypatch):
    monkeypatch.setattr(adapter.sys, "platform", "linux")
    tail = ["S", *["0"] * 18, "4567", "0"]
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw: "234 (odd ) command) " + " ".join(tail))
    assert adapter.process_start_ticks(234) == 4567
    with pytest.raises(adapter.ProcessIdentityUnknown):
        adapter.process_start_ticks(235)


@pytest.mark.parametrize("failure", ["short", "wrong_pid", "invalid_usec"])
def test_macos_abi_exact_size_pid_and_birth_are_required(monkeypatch, failure):
    monkeypatch.setattr(adapter.sys, "platform", "darwin")

    class Function:
        def __call__(self, pid, flavor, arg, pointer, size):
            assert flavor == 3 and arg == 0 and size == 136
            info = ctypes.cast(pointer, ctypes.POINTER(adapter._ProcBSDInfo)).contents
            info.pid = pid + (failure == "wrong_pid")
            info.start_sec = 123
            info.start_usec = 1_000_000 if failure == "invalid_usec" else 4
            return size - 1 if failure == "short" else size

    class Library:
        proc_pidinfo = Function()

    monkeypatch.setattr(adapter.ctypes, "CDLL", lambda *a, **kw: Library())
    with pytest.raises(adapter.ProcessIdentityUnknown):
        adapter.process_start_ticks(234)


async def test_unknown_identity_keeps_blocked_after_actual_wait(monkeypatch):
    manager = Manager()
    lease = manager.admit("auxiliary")
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
    monkeypatch.setattr(
        adapter, "process_start_ticks", lambda pid: (_ for _ in ()).throw(adapter.ProcessIdentityUnknown())
    )
    try:
        assert not adapter.register_process(lease, process)
        assert await adapter.wait_process(lease, process, False) == 0
        lease.release()
        manager.begin_drain(manager.ticket())
        assert not await manager.wait_drained(timeout=0.01)
        assert manager.observe()["blocked"] == 1
        with pytest.raises(Exception, match="development_close_rejected"):
            manager.close_development()
    finally:
        await process.wait()
        manager._pool.shutdown(wait=True, cancel_futures=False)


async def test_cancelled_worker_keeps_real_child_until_actual_exit():
    manager = Manager(max_workers=2)
    root = manager.admit("request")
    processes = []

    def invocation():
        lease = current_lease()
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", "import sys;sys.stdin.buffer.read()"], stdin=subprocess.PIPE
        )
        processes.append(process)
        registered = adapter.register_process(lease, process)
        return adapter.wait_process_sync(lease, process, registered)

    with root:
        task = asyncio.create_task(manager.run_blocking("diagnostic", invocation))
        await asyncio.sleep(0)  # Child registration precedes releasing the root owner.
    try:
        for _ in range(200):
            if manager.observe()["pending_processes"] == 1:
                break
            await asyncio.sleep(0.005)
        assert manager.observe()["pending_processes"] == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        manager.begin_drain(manager.ticket())
        assert not await manager.wait_drained(timeout=0.01)
        assert manager.observe()["pending_cf"] == manager.observe()["pending_processes"] == 1
        processes[0].stdin.close()
        assert await manager.wait_drained(timeout=3)
        manager.close_development()
    finally:
        for process in processes:
            if not process.stdin.closed:
                process.stdin.close()
            process.wait()
        manager._pool.shutdown(wait=True, cancel_futures=False)


async def test_cancelled_async_waiter_keeps_registration_until_actual_wait_witness():
    manager = Manager()
    lease = manager.admit("diagnostic")
    spawned, release = asyncio.Event(), asyncio.Event()
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-c", "import sys;sys.stdin.buffer.read()", stdin=asyncio.subprocess.PIPE
    )
    real_wait = process.wait

    async def delayed_wait():
        result = await real_wait()
        spawned.set()
        await release.wait()
        return result

    process.wait = delayed_wait
    assert adapter.register_process(lease, process)
    task = asyncio.create_task(adapter.wait_process(lease, process, True))
    try:
        await asyncio.sleep(0)
        assert manager.observe()["pending_processes"] == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        lease.release()
        process.stdin.close()
        await asyncio.wait_for(spawned.wait(), 2)
        manager.begin_drain(manager.ticket())
        assert manager.observe()["pending_processes"] == 1
        assert not await manager.wait_drained(timeout=0.01)
        release.set()
        assert await manager.wait_drained(timeout=2)
        manager.close_development()
    finally:
        release.set()
        if process.returncode is None:
            process.kill()
        await process.wait()
        manager._pool.shutdown(wait=True, cancel_futures=False)


async def test_repeated_cancel_during_spawn_cannot_hide_real_child(monkeypatch):
    manager = Manager()
    lease = manager.admit("diagnostic")
    born, return_process = asyncio.Event(), asyncio.Event()
    processes = []
    real_spawn = asyncio.create_subprocess_exec

    async def delayed_spawn(*args, **kwargs):
        process = await real_spawn(*args, **kwargs)
        processes.append(process)
        born.set()
        await return_process.wait()
        return process

    monkeypatch.setattr(adapter.asyncio, "create_subprocess_exec", delayed_spawn)
    handle = adapter.start_process(lease, sys.executable, "-I", "-c", "import time;time.sleep(10)")
    task = asyncio.create_task(handle.wait_result())
    try:
        await asyncio.wait_for(born.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        lease.release()
        manager.begin_drain(manager.ticket())
        assert manager.observe()["pending_async"] == 1
        assert not await manager.wait_drained(timeout=0.01)
        return_process.set()
        for _ in range(200):
            if manager.observe()["pending_processes"] == 1:
                break
            await asyncio.sleep(0.005)
        assert manager.observe()["pending_processes"] == 1
        assert not await manager.wait_drained(timeout=0.01)
        # Explicit test cleanup is an actual wait witness, never a timeout/cancel
        # shortcut. The production drain path does not kill this child for us.
        processes[0].kill()
        await lease.wait_process(processes[0])
        assert await manager.wait_drained(timeout=2)
        manager.close_development()
    finally:
        return_process.set()
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.wait()
        manager._pool.shutdown(wait=True, cancel_futures=False)
