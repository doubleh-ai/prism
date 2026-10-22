"""Real isolated threads/process waits; synthetic OS adapter data is not Linux acceptance."""

import asyncio
import contextvars
import json
import subprocess
import sys
import threading
import time
import types
from dataclasses import replace

import pytest

from server import work_lifecycle as work


@pytest.fixture(autouse=True)
def temp_data_dir():
    """The lifecycle core never imports application configuration or databases."""
    yield


@pytest.fixture
def manager():
    value = work.Manager(
        work.RuntimeIdentity(
            "12345678-1234-1234-1234-123456789abc",
            90001,
            77,
            "a" * 40,
            "b" * 64,
            "/system.slice/stock-app.service",
        ),
        max_workers=4,
    )
    for kind in work.KINDS:
        value.declare(kind)
    yield value
    # Test cleanup only: never changes blocked leases or returns a drain proof.
    value._pool.shutdown(wait=True, cancel_futures=False)


def observation(manager, *, extra=()):
    identity, epoch, generation, nonce = manager.observation_context()
    return work.OSObservation(
        identity,
        epoch,
        generation,
        nonce,
        time.monotonic_ns(),
        "yes",
        ((".", ((identity.pid, identity.start_ticks), *extra)),),
    )


def seal(manager):
    manager.record_os_observation(observation(manager))
    return manager.seal(manager.ticket())


def test_root_and_child_capabilities_survive_fence_but_foreign_parents_do_not(manager):
    root = manager.admit("request")
    child = manager.admit("market", parent=root)
    root.release()
    manager.begin_drain(manager.ticket())
    with pytest.raises(work.LifecycleError, match="admission_closed"):
        manager.admit("request")
    grandchild = manager.admit("auxiliary", parent=child)
    with pytest.raises(work.LifecycleError, match="foreign_or_finished_parent"):
        manager.admit("market", parent={"id": child.id})
    other = work.Manager()
    try:
        with pytest.raises(work.LifecycleError, match="foreign_or_finished_parent"):
            other.admit("market", parent=child)
    finally:
        other._pool.shutdown(wait=True)
    child.release()
    assert sum(manager.observe()["leases"].values()) == 3
    grandchild.release()
    assert sum(manager.observe()["leases"].values()) == 0
    with pytest.raises(work.LifecycleError, match="foreign_or_finished_parent"):
        manager.admit("market", parent=child)
    seal(manager)


def test_admission_and_fence_are_atomic_between_actual_threads(manager):
    barrier = threading.Barrier(13)
    admitted, refused = [], []
    result_lock = threading.Lock()

    def enter():
        barrier.wait()
        try:
            value = manager.admit("request")
        except work.LifecycleError:
            with result_lock:
                refused.append(1)
        else:
            with result_lock:
                admitted.append(value)

    threads = [threading.Thread(target=enter) for _ in range(12)]
    for thread in threads:
        thread.start()
    barrier.wait()
    manager.begin_drain(manager.ticket())
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert len(admitted) + len(refused) == 12
    assert manager.observe()["leases"]["request"] == len(admitted)
    for lease in admitted:
        lease.release()
    seal(manager)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_mode", ["cancel", "timeout"])
async def test_cancelled_waiter_does_not_cancel_or_unregister_actual_executor(manager, cancel_mode):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    secret = "synthetic-prompt-must-not-appear"

    def blocking(argument):
        assert argument == secret and work.get_manager() is manager
        entered.set()
        release.wait(3)
        finished.set()
        return 17

    root = manager.admit("request")
    task = asyncio.create_task(manager.run_blocking("market", blocking, secret, parent=root))
    try:
        for _ in range(200):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set()
        if cancel_mode == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(task, 0.01)
        root.release()
        manager.begin_drain(manager.ticket())
        value = manager.observe()
        assert value["pending_cf"] == 1 and value["leases"]["market"] == 1
        assert not finished.is_set() and not await manager.wait_drained(0.02)
        assert secret not in json.dumps(value)
        with pytest.raises(work.LifecycleError, match="quiescence_unproven"):
            seal(manager)
    finally:
        release.set()
    assert await manager.wait_drained(2)
    assert finished.is_set() and manager.observe()["pending_cf"] == 0
    seal(manager)


@pytest.mark.asyncio
async def test_worker_copy_context_can_admit_completion_children_after_fence(manager):
    entered, proceed = threading.Event(), threading.Event()

    def blocking():
        entered.set()
        proceed.wait(3)
        parent = work.current_lease()
        child = work.get_manager().admit("auxiliary")
        assert child.parent is parent and child.manager is manager
        child.release()

    task = asyncio.create_task(manager.run_blocking("market", blocking))
    try:
        while not entered.is_set():
            await asyncio.sleep(0.005)
        manager.begin_drain(manager.ticket())
        proceed.set()
        await task
        assert await manager.wait_drained(1)
        seal(manager)
    finally:
        proceed.set()


@pytest.mark.asyncio
async def test_owned_async_task_finishes_persistence_after_its_http_waiter_is_cancelled(manager, tmp_path):
    release = asyncio.Event()
    target = tmp_path / "terminal-result.txt"

    async def job(lease):
        await release.wait()
        return "synthetic-complete-result"

    def persist(value):
        target.write_text(value)

    handle = manager.start("analysis", job, persist=persist)
    waiter = asyncio.create_task(handle.wait())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    manager.begin_drain(manager.ticket())
    assert manager.observe()["persist_pending"] == 1 and not target.exists()
    release.set()
    assert await handle.wait() == "synthetic-complete-result"
    assert target.read_text() == "synthetic-complete-result"
    assert await manager.wait_drained(1)
    seal(manager)


@pytest.mark.asyncio
async def test_persistence_failure_keeps_a_blocked_lease_and_does_not_publish_payload(manager):
    lease = manager.admit("analysis")

    def persist():
        raise OSError("synthetic-user-content-not-exported")

    with pytest.raises(work.LifecycleError, match="persistence_failed") as error:
        await lease.finish(persist)
    lease.release()
    manager.begin_drain(manager.ticket())
    value = manager.observe()
    assert value["persist_blocked"] == value["blocked"] == 1
    assert "synthetic-user-content" not in str(error.value) + json.dumps(value)
    assert not await manager.wait_drained(0.02)
    with pytest.raises(work.LifecycleError, match="quiescence_unproven"):
        seal(manager)


@pytest.mark.asyncio
async def test_persistence_cannot_run_before_owned_children_finish(manager):
    parent = manager.admit("analysis")
    child = manager.admit("market", parent=parent)
    wrote = []
    with pytest.raises(work.LifecycleError, match="persistence_resources_pending"):
        await parent.finish(lambda: wrote.append(True))
    assert wrote == []
    child.release()
    await parent.finish(lambda: wrote.append(True))
    manager.begin_drain(manager.ticket())
    seal(manager)


@pytest.mark.asyncio
async def test_actual_subprocess_wait_survives_cancel_and_still_needs_recursive_os_evidence(manager):
    lease = manager.admit("cli")
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", "import time; time.sleep(0.3)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    lease.attach_process(process, start_ticks=123)
    task = asyncio.create_task(lease.wait_process(process))
    try:
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        lease.release()
        manager.begin_drain(manager.ticket())
        assert manager.observe()["pending_processes"] == 1
        assert manager.observe()["pending_cf"] == 1
        assert await manager.wait_drained(2)
        assert process.poll() == 0
        with pytest.raises(work.LifecycleError, match="quiescence_unproven"):
            manager.seal(manager.ticket())
        manager.record_os_observation(observation(manager, extra=((81234, 99),)))
        with pytest.raises(work.LifecycleError, match="quiescence_unproven"):
            manager.seal(manager.ticket())
        seal(manager)
    finally:
        process.wait(timeout=3)


def test_replayed_epoch_nonce_and_stale_os_inventory_are_rejected(manager):
    ticket = manager.ticket()
    manager.begin_drain(ticket)
    with pytest.raises(work.LifecycleError, match="stale_control_ticket"):
        manager._command(ticket)
    stale = observation(manager)
    # Mutation invalidates an inventory even when its monotonic timestamp is fresh.
    manager._changed()
    with pytest.raises(work.LifecycleError, match="os_observation_rejected"):
        manager.record_os_observation(stale)
    before_seal = manager.ticket()
    proof = seal(manager)
    assert proof.epoch > before_seal.epoch
    with pytest.raises(work.LifecycleError, match="stale_control_ticket"):
        manager.resume(before_seal)
    manager.resume(manager.ticket())
    assert manager.observe()["state"] == "running"


@pytest.mark.parametrize(
    "change", ["boolean", "wrong_identity", "duplicate_pid", "missing_root", "stale", "not_protected"]
)
def test_os_adapter_contract_does_not_accept_a_boolean_or_ambiguous_inventory(manager, change):
    manager.begin_drain(manager.ticket())
    value = observation(manager)
    if change == "boolean":
        value = True
    elif change == "wrong_identity":
        value = replace(value, identity=replace(manager.identity, pid=90002))
    elif change == "duplicate_pid":
        value = replace(value, groups=((".", ((90001, 77),)), ("child", ((90001, 77),))))
    elif change == "missing_root":
        value = replace(value, groups=(("child", ((90001, 77),)),))
    elif change == "stale":
        value = replace(value, checked_at_ns=time.monotonic_ns() - work.OS_FRESH_NS - 1)
    else:
        value = replace(value, protect_control_groups="no")
    with pytest.raises(work.LifecycleError, match="os_observation_rejected"):
        manager.record_os_observation(value)


def test_unknown_producer_and_lazy_identity_cannot_be_a_release_proof():
    manager = work.Manager()
    try:
        lease = manager.admit("request")
        lease.release()
        manager.begin_drain(manager.ticket())
        assert manager.observe()["producer_unknown"] == len(work.KINDS)
        with pytest.raises(work.LifecycleError, match="quiescence_unproven"):
            manager.seal(manager.ticket())
    finally:
        manager._pool.shutdown(wait=True)


@pytest.mark.asyncio
async def test_scope_manager_precedes_context_and_lazy_getter_does_not_bypass_drain(manager):
    fallback = work.get_manager()
    root = manager.admit("request")
    with root:
        assert work.get_manager() is manager
        app = types.SimpleNamespace(state=types.SimpleNamespace(work_manager=fallback))
        assert work.get_manager({"app": app}) is fallback
    fallback.begin_drain(fallback.ticket())
    assert work.get_manager() is fallback
    with pytest.raises(work.LifecycleError, match="admission_closed"):
        work.get_manager().admit("request")


def test_sync_direct_getter_does_not_replace_a_draining_instance():
    context = contextvars.Context()
    manager = context.run(work.get_manager)
    if manager.observe()["state"] == "running":
        manager.begin_drain(manager.ticket())
    assert context.run(work.get_manager) is manager
    manager.close_development()  # Test cleanup through the identity-free public close contract.


@pytest.mark.asyncio
async def test_closed_empty_app_manager_can_be_replaced_explicitly_without_reopening_old_one(manager):
    manager.begin_drain(manager.ticket())
    seal(manager)
    manager.close()
    app = types.SimpleNamespace(state=types.SimpleNamespace(work_manager=manager))
    assert work.get_manager({"app": app}) is manager
    with pytest.raises(work.LifecycleError, match="admission_closed"):
        work.get_manager({"app": app}).admit("request")
    replacement = work.Manager()
    try:
        assert work.bind_manager(app, replacement) is replacement
        assert work.get_manager({"app": app}) is replacement
    finally:
        replacement._pool.shutdown(wait=True)


def test_binding_a_new_manager_cannot_bypass_a_prior_running_or_draining_gate(manager):
    app = types.SimpleNamespace(state=types.SimpleNamespace())
    work.bind_manager(app, manager)
    replacement = work.Manager()
    try:
        with pytest.raises(work.LifecycleError, match="prior_manager_not_closed"):
            work.bind_manager(app, replacement)
        manager.begin_drain(manager.ticket())
        with pytest.raises(work.LifecycleError, match="prior_manager_not_closed"):
            work.bind_manager(app, replacement)
        assert work.get_manager({"app": app}) is manager
    finally:
        replacement._pool.shutdown(wait=True)


@pytest.mark.asyncio
async def test_request_disconnect_does_not_poison_scope_and_its_child_still_owns_work(manager):
    request = manager.admit("request")
    child = manager.admit("market", parent=request)
    with pytest.raises(asyncio.CancelledError):
        async with request:
            raise asyncio.CancelledError
    manager.begin_drain(manager.ticket())
    assert manager.observe()["blocked"] == 0 and manager.observe()["leases"]["request"] == 1
    child.release()
    assert await manager.wait_drained(1)
    seal(manager)


@pytest.mark.asyncio
async def test_handle_done_and_callbacks_wait_for_descendants_not_owner_return(manager):
    ready = asyncio.Event()
    state = {}
    calls = []

    async def job(lease):
        state["child"] = manager.admit("market", parent=lease)
        ready.set()
        return 42

    handle = manager.start("request", job)
    handle.add_done_callback(lambda completed: calls.append((completed, completed.done())))
    await ready.wait()
    await asyncio.sleep(0)
    assert not handle.done() and calls == []
    manager.begin_drain(manager.ticket())
    state["child"].release()
    assert await handle.wait() == 42
    await asyncio.sleep(0)
    assert calls == [(handle, True)]
    handle.add_done_callback(lambda completed: calls.append((completed, completed.done())))
    await asyncio.sleep(0)
    assert len(calls) == 2
    seal(manager)


@pytest.mark.parametrize("epoch", [True, False, -1, "0"])
def test_control_ticket_requires_real_integer_epoch(manager, epoch):
    with pytest.raises(work.LifecycleError, match="stale_control_ticket"):
        manager.begin_drain(work.Ticket(epoch, manager.ticket().nonce))


@pytest.mark.asyncio
async def test_owner_release_cannot_hide_a_still_running_owned_async_task(manager):
    entered, finish = asyncio.Event(), asyncio.Event()

    async def job(lease):
        lease.release()
        entered.set()
        await finish.wait()

    handle = manager.start("request", job)
    await entered.wait()
    manager.begin_drain(manager.ticket())
    assert manager.observe()["pending_async"] == 1
    with pytest.raises(work.LifecycleError, match="quiescence_unproven"):
        seal(manager)
    finish.set()
    await handle.wait()
    assert manager.observe()["pending_async"] == 0
    seal(manager)


@pytest.mark.asyncio
async def test_real_asyncio_process_is_reaped_after_waiter_disconnect(manager):
    lease = manager.admit("cli")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        "import time; time.sleep(0.15)",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    lease.attach_process(process, start_ticks=123)
    waiter = asyncio.create_task(lease.wait_process(process))
    try:
        await asyncio.sleep(0.01)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        lease.release()
        manager.begin_drain(manager.ticket())
        assert manager.observe()["pending_processes"] == 1
        assert await manager.wait_drained(2)
        assert process.returncode == 0
        seal(manager)
    finally:
        await process.wait()


def test_empty_registry_cannot_hide_an_actual_unregistered_process(manager):
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", "import time; time.sleep(0.15)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        manager.begin_drain(manager.ticket())
        assert sum(manager.observe()["leases"].values()) == 0 and process.poll() is None
        # The tested adapter schema uses synthetic start ticks, but this PID
        # belongs to a real, still-running isolated child process.
        manager.record_os_observation(observation(manager, extra=((process.pid, 123),)))
        assert manager.observe()["os_descendants"] == 1
        with pytest.raises(work.LifecycleError, match="quiescence_unproven"):
            manager.seal(manager.ticket())
    finally:
        process.wait(timeout=3)
    seal(manager)


@pytest.mark.asyncio
async def test_serialized_success_bool_is_not_a_terminal_writer(manager):
    lease = manager.admit("analysis")
    with pytest.raises(work.LifecycleError, match="persistence_failed"):
        await lease.finish(lambda: True)
    assert manager.observe()["persist_blocked"] == 1


def test_recursive_os_groups_allow_real_unit_names_but_reject_dot_segments(manager):
    manager.begin_drain(manager.ticket())
    value = observation(manager)
    manager.record_os_observation(replace(value, groups=(*value.groups, ("child.scope", ()))))
    with pytest.raises(work.LifecycleError, match="os_observation_rejected"):
        manager.record_os_observation(replace(value, groups=(*value.groups, ("child/../escape", ()))))
    manager.seal(manager.ticket())


def test_sync_wait_requires_same_real_attached_popen_and_reaps(manager):
    lease = manager.admit("auxiliary")
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        with pytest.raises(work.LifecycleError, match="registration_required"):
            lease.wait_process_sync(process)
        lease.attach_process(process, start_ticks=123)
        assert manager.observe()["pending_processes"] == 1
        assert lease.wait_process_sync(process) == 0
        assert manager.observe()["pending_processes"] == 0
        lease.release()
        assert not sum(manager.observe()["leases"].values())
    finally:
        process.wait()


def test_sync_wait_mismatched_returncode_stays_unknown(manager, monkeypatch):
    lease = manager.admit("auxiliary")
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    lease.attach_process(process, start_ticks=123)
    monkeypatch.setattr(process, "wait", lambda: True)
    with pytest.raises(work.LifecycleError, match="wait_unknown"):
        lease.wait_process_sync(process)
    lease.release()
    assert manager.observe()["pending_processes"] == 1
    assert manager.observe()["blocked"] == 1


async def test_all_pool_workers_sync_reap_without_nested_submit_or_cancel_bypass():
    manager = work.Manager(max_workers=8)
    ready = threading.Barrier(8)
    proceed = threading.Event()
    processes, lock = [], threading.Lock()

    def blocking_child():
        lease = manager.admit("auxiliary")
        with lease:
            process = subprocess.Popen(
                [sys.executable, "-c", "import sys;sys.stdin.buffer.read(1)"], stdin=subprocess.PIPE
            )
            with lock:
                processes.append(process)
            lease.attach_process(process, start_ticks=123)
            ready.wait(timeout=5)
            proceed.wait(timeout=5)
            process.stdin.close()
            assert lease.wait_process_sync(process) == 0

    tasks = [asyncio.create_task(manager.run_blocking("auxiliary", blocking_child)) for _ in range(8)]
    try:
        for _ in range(500):
            if manager.observe()["pending_processes"] == 8:
                break
            await asyncio.sleep(0.01)
        assert manager.observe()["pending_processes"] == manager.observe()["pending_cf"] == 8
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        manager.begin_drain(manager.ticket())
        assert not await manager.wait_drained(timeout=0.02)
        assert manager.observe()["pending_processes"] == manager.observe()["pending_cf"] == 8
        proceed.set()
        assert await manager.wait_drained(timeout=5)
        manager.close_development()
        assert manager.observe()["state"] == "closed"
    finally:
        proceed.set()
        manager._pool.shutdown(wait=True, cancel_futures=False)
        for process in processes:
            process.wait()


def test_development_close_requires_identity_free_draining_empty(manager):
    with pytest.raises(work.LifecycleError, match="development_close_rejected"):
        manager.close_development()
    development = work.Manager()
    try:
        with pytest.raises(work.LifecycleError, match="development_close_rejected"):
            development.close_development()
        lease = development.admit("request")
        development.begin_drain(development.ticket())
        with pytest.raises(work.LifecycleError, match="development_close_rejected"):
            development.close_development()
        lease.release()
        assert development.close_development() is None
        assert development.observe()["state"] == "closed"
        with pytest.raises(work.LifecycleError, match="quiescence_unproven"):
            development.seal(development.ticket())
        manager.begin_drain(manager.ticket())
        with pytest.raises(work.LifecycleError, match="development_close_rejected"):
            manager.close_development()
    finally:
        development._pool.shutdown(wait=True, cancel_futures=False)


async def test_scope_context_keeps_read_route_producers_on_fenced_manager():
    manager = work.Manager()
    lazy = work.get_manager()
    try:
        with work.bind_context(manager):
            assert work.get_manager() is manager
            manager.begin_drain(manager.ticket())
            assert work.get_manager() is manager
            with pytest.raises(work.LifecycleError, match="admission_closed"):
                work.get_manager().admit("market")
            manager.close_development()
            assert work.get_manager() is manager
            with pytest.raises(work.LifecycleError):
                work.get_manager().admit("request")
        assert work.get_manager() is lazy
    finally:
        manager._pool.shutdown(wait=True, cancel_futures=False)


def test_scope_binding_nested_reset_and_existing_lease_priority():
    first, second, explicit = work.Manager(), work.Manager(), work.Manager()
    lazy = work.get_manager()
    app = types.SimpleNamespace(state=types.SimpleNamespace(work_manager=explicit))
    try:
        with work.bind_context(first):
            assert work.get_manager() is first
            with pytest.raises(ValueError), work.bind_context(second):
                assert work.get_manager() is second
                raise ValueError("synthetic")
            assert work.get_manager() is first
            root = first.admit("request")
            with root, work.bind_context(second):
                assert work.get_manager() is first
                assert work.get_manager({"app": app}) is explicit
        assert work.get_manager() is lazy
        with pytest.raises(work.LifecycleError, match="bound_manager_rejected"):
            with work.bind_context({"manager": first}):
                pytest.fail("serialized manager cannot bind")
    finally:
        for manager in (first, second, explicit):
            manager._pool.shutdown(wait=True, cancel_futures=False)


async def test_scope_manager_flows_to_worker_and_does_not_leak_between_tasks():
    first, second = work.Manager(), work.Manager()

    async def scoped(manager):
        with work.bind_context(manager):
            await asyncio.sleep(0)
            assert work.get_manager() is manager
            assert await manager.run_blocking("auxiliary", work.get_manager) is manager

    try:
        await asyncio.gather(scoped(first), scoped(second))
    finally:
        first._pool.shutdown(wait=True, cancel_futures=False)
        second._pool.shutdown(wait=True, cancel_futures=False)
