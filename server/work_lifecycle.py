"""Application work ownership and a fail-closed, in-process drain barrier.

This module starts no command, cancels no work and exposes no management route.
Only reviewed application adapters may create work or supply OS observations.
Its snapshots contain control identities and counts, never business arguments.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import re
import secrets
import subprocess
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass

KINDS = frozenset(
    {"request", "agent", "analysis", "scan", "market", "cli", "auxiliary", "capability", "vibe", "diagnostic"}
)
DURABLE_KINDS = frozenset({"agent", "analysis", "scan", "capability", "vibe"})
OS_FRESH_NS = 10_000_000_000
_CURRENT = contextvars.ContextVar("prism_work_lease", default=None)
_SCOPE_MANAGER = contextvars.ContextVar("prism_work_scope_manager", default=None)
_BINDINGS = contextvars.ContextVar("prism_work_bindings", default=())
_DEFAULTS = weakref.WeakKeyDictionary()
_DEFAULT_LOCK = threading.RLock()
_SYNC_DEFAULT = threading.local()


class LifecycleError(RuntimeError):
    """Only finite, non-business failure categories cross this boundary."""


def fail(code):
    raise LifecycleError("work_lifecycle_" + code)


def _uint(value, minimum=0):
    return type(value) is int and value >= minimum


@dataclass(frozen=True)
class Ticket:
    epoch: int
    nonce: str


@dataclass(frozen=True)
class RuntimeIdentity:
    boot_id: str
    pid: int
    start_ticks: int
    commit: str
    artifact_sha256: str
    cgroup: str

    def __post_init__(self):
        if (
            not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", self.boot_id)
            or not _uint(self.pid, 2)
            or not _uint(self.start_ticks, 1)
            or not re.fullmatch(r"[0-9a-f]{40}", self.commit)
            or not re.fullmatch(r"[0-9a-f]{64}", self.artifact_sha256)
            or not re.fullmatch(r"/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", self.cgroup)
            or any(part in {".", ".."} for part in self.cgroup.split("/"))
        ):
            fail("runtime_identity_rejected")


@dataclass(frozen=True)
class OSObservation:
    """Input from a trusted local adapter, never a socket client's JSON flag.

    groups is the complete recursive cgroup inventory: (relative group,
    ((pid, start_ticks), ...)). The controller independently checks its adapter
    and the actual kernel. Constructing this object is not OS authentication.
    """

    identity: RuntimeIdentity
    epoch: int
    generation: int
    nonce: str
    checked_at_ns: int
    protect_control_groups: str
    groups: tuple


@dataclass(frozen=True)
class DrainProof:
    epoch: int
    generation: int
    nonce: str


class Lease:
    def __init__(self, manager, kind, parent, persist_required):
        self.manager, self.kind, self.parent = manager, kind, parent
        self.id = secrets.token_hex(16)
        self.epoch = manager._epoch
        self._children = set()
        self._blocking = set()
        self._processes = {}
        self._owner_done = False
        self._persist = "required" if persist_required else "none"
        self._blocked = set()
        self._handle = None
        self._async_pending = False

    def __enter__(self):
        self.manager._check_lease(self)
        _BINDINGS.set((*_BINDINGS.get(), (self, _CURRENT.set(self))))
        return self

    def __exit__(self, exc_type, exc, traceback):
        stack = _BINDINGS.get()
        if not stack or stack[-1][0] is not self:
            fail("lease_context_rejected")
        _CURRENT.reset(stack[-1][1])
        _BINDINGS.set(stack[:-1])
        if exc_type is not None and self.kind in DURABLE_KINDS:
            self.block("owner_failed")
        self.release()

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, exc_type, exc, traceback):
        self.__exit__(exc_type, exc, traceback)

    def release(self):
        """Owner returned; descendants and real resources retain this lease."""
        self.manager._release(self)

    def block(self, category="owner_failed"):
        if category not in {"owner_failed", "persistence_failed", "process_unknown", "owned_task_cancelled"}:
            fail("failure_category_rejected")
        with self.manager._lock:
            self.manager._check_lease(self)
            self._blocked.add(category)
            self.manager._changed()

    async def finish(self, persist):
        """Run a trusted terminal writer; failure leaves a blocked live lease."""
        with self.manager._lock:
            self.manager._check_lease(self)
            if self._persist in {"pending", "ok", "failed"} or not callable(persist):
                fail("persistence_state_rejected")
            if self.manager._pending(self, include_owner=False):
                fail("persistence_resources_pending")
            self._persist = "pending"
            self.manager._changed()
        try:
            result = persist()
            if inspect.isawaitable(result):
                result = await result
            if result is not None:
                fail("persistence_result_rejected")
        except BaseException:
            with self.manager._lock:
                self._persist = "failed"
                self._blocked.add("persistence_failed")
                self._owner_done = True
                self.manager._changed()
            raise LifecycleError("work_lifecycle_persistence_failed") from None
        with self.manager._lock:
            self._persist = "ok"
            self._owner_done = True
            self.manager._changed()
            self.manager._collect(self)

    async def wait_owned(self, timeout=None):
        started = time.monotonic()
        while True:
            with self.manager._lock:
                self.manager._check_lease(self)
                if not self.manager._pending(self, include_owner=False):
                    return True
            if timeout is not None and time.monotonic() - started >= timeout:
                return False
            await asyncio.sleep(0.01)

    def attach_process(self, process, *, start_ticks):
        return self.manager._attach_process(self, process, start_ticks)

    async def wait_process(self, process):
        return await self.manager._wait_process(self, process)

    def wait_process_sync(self, process):
        """Witness an attached Popen actual wait in this calling thread."""
        return self.manager._wait_process_sync(self, process)


class RequestObservation:
    """Observe an HTTP scope without granting ancestry or business admission."""

    def __init__(self, manager):
        self._manager = manager

    def release(self):
        self._manager._release_request(self)


class WorkHandle:
    """Callbacks receive this handle after actual lease completion, on its loop.

    done() includes descendants, real resources and persistence, rather than
    merely the async owner's return. A blocked lease never announces success.
    """

    def __init__(self, lease, task):
        self.lease, self._task = lease, task
        self._loop = asyncio.get_running_loop()
        self._completed = False
        self._callbacks = []
        with lease.manager._lock:
            lease._handle = self
            if lease.id not in lease.manager._leases:
                self._complete()

    async def wait_result(self):
        """Return the business result; descendants/persistence still govern done()."""
        return await asyncio.shield(self._task)

    async def wait(self):
        result = await self.wait_result()
        while not self.done():
            await asyncio.sleep(0.01)
        return result

    def done(self):
        return self._completed and self._task.done()

    def add_done_callback(self, callback):
        if not callable(callback):
            fail("completion_callback_rejected")
        with self.lease.manager._lock:
            if self._completed:
                self._schedule(callback)
            else:
                self._callbacks.append(callback)

    def _schedule(self, callback):
        def invoke():
            try:
                callback(self)
            except Exception:
                self._loop.call_exception_handler({"message": "work_lifecycle_completion_callback_failed"})

        if not self._loop.is_closed():
            self._loop.call_soon_threadsafe(invoke)

    def _complete(self):
        if self._completed:
            return
        self._completed = True
        for callback in self._callbacks:
            self._schedule(callback)
        self._callbacks.clear()


class Manager:
    def __init__(self, identity=None, *, max_workers=8):
        if identity is not None and type(identity) is not RuntimeIdentity:
            fail("runtime_identity_rejected")
        if not _uint(max_workers, 1) or max_workers > 64:
            fail("worker_limit_rejected")
        self.identity = identity
        self._lock = threading.RLock()
        self._leases = {}
        self._requests = set()
        self._declared = {}
        self._epoch, self._generation = 0, 0
        self._nonce = secrets.token_hex(16)
        self._state = "running"
        self._os = None
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="prism-work")

    def declare(self, kind, *, enabled=True):
        """Reviewed integration declares every producer enabled or hard-disabled."""
        with self._lock:
            if (
                kind not in KINDS
                or type(enabled) is not bool
                or kind in self._declared
                or self._leases
                or self._requests
            ):
                fail("producer_declaration_rejected")
            if self._state != "running" or self._epoch:
                fail("producer_declaration_rejected")
            self._declared[kind] = enabled
            self._changed()

    def _changed(self):
        self._generation += 1
        self._os = None

    def ticket(self):
        with self._lock:
            return Ticket(self._epoch, self._nonce)

    def _command(self, ticket):
        if (
            type(ticket) is not Ticket
            or not _uint(ticket.epoch)
            or type(ticket.nonce) is not str
            or not re.fullmatch(r"[0-9a-f]{32}", ticket.nonce)
            or ticket != Ticket(self._epoch, self._nonce)
        ):
            fail("stale_control_ticket")
        self._epoch += 1
        self._nonce = secrets.token_hex(16)
        self._changed()

    def _check_lease(self, lease):
        if type(lease) is not Lease or lease.manager is not self or self._leases.get(lease.id) is not lease:
            fail("foreign_or_finished_parent")

    def admit(self, kind, *, parent=None, persist_required=None):
        with self._lock:
            if kind not in KINDS or self._declared.get(kind) is False:
                fail("work_kind_rejected")
            if self._state in {"quiescent", "closing", "closed"}:
                fail("admission_closed")
            if parent is None:
                parent = current_lease()
            if parent is not None:
                self._check_lease(parent)
                if parent._persist in {"pending", "failed"} or parent._blocked:
                    fail("parent_not_active")
                if parent._owner_done and not self._pending(parent):
                    fail("parent_not_active")
            elif self._state != "running":
                fail("admission_closed")
            if persist_required is None:
                persist_required = kind in DURABLE_KINDS
            if type(persist_required) is not bool:
                fail("persistence_policy_rejected")
            lease = Lease(self, kind, parent, persist_required)
            self._leases[lease.id] = lease
            if parent is not None:
                parent._children.add(lease.id)
            self._changed()
            return lease

    def track_request(self):
        """Count even history/auth responses and their outer middleware writes.

        During drain these observations do not become current leases or valid
        parents. Only already admitted business owners may start descendants.
        Once sealed, every new HTTP scope must stay outside application code.
        """
        with self._lock:
            if self._state not in {"running", "draining"} or self._declared.get("request") is False:
                fail("admission_closed")
            observation = RequestObservation(self)
            self._requests.add(observation)
            self._changed()
            return observation

    def _release_request(self, observation):
        with self._lock:
            if type(observation) is not RequestObservation or observation._manager is not self:
                fail("foreign_or_finished_parent")
            if observation in self._requests:
                self._requests.remove(observation)
                self._changed()

    def _pending(self, lease, *, include_owner=True):
        return (
            include_owner
            and lease._async_pending
            or bool(lease._children)
            or any(not future.done() for future in lease._blocking)
            or any(not row["reaped"] for row in lease._processes.values())
        )

    def _collect(self, lease):
        if (
            not lease._owner_done
            or self._pending(lease)
            or lease._persist not in {"none", "ok"}
            or lease._blocked
        ):
            return
        if self._leases.pop(lease.id, None) is lease:
            self._changed()
            if lease._handle is not None:
                lease._handle._complete()
            if lease.parent is not None:
                lease.parent._children.discard(lease.id)
                self._collect(lease.parent)

    def _release(self, lease):
        with self._lock:
            if self._leases.get(lease.id) is not lease:
                return
            lease._owner_done = True
            self._changed()
            self._collect(lease)

    def _blocking_done(self, lease, future):
        with self._lock:
            if self._leases.get(lease.id) is lease:
                if future.cancelled():
                    lease._blocked.add("owner_failed")
                self._changed()
                self._collect(lease)

    async def run_blocking(self, kind, function, *args, parent=None, **kwargs):
        """Retain the original Future; a cancelled waiter cannot stop its thread."""
        lease = self.admit(kind, parent=parent, persist_required=False)
        token = _CURRENT.set(lease)
        try:
            context = contextvars.copy_context()
            with self._lock:
                future = self._pool.submit(context.run, function, *args, **kwargs)
                lease._blocking.add(future)
                self._changed()
                future.add_done_callback(lambda value: self._blocking_done(lease, value))
            return await asyncio.shield(asyncio.wrap_future(future))
        finally:
            _CURRENT.reset(token)
            lease.release()

    def start(self, kind, function, *, parent=None, persist=None):
        """Own the background task independently of its HTTP waiters."""
        if not callable(function) or (kind in DURABLE_KINDS and not callable(persist)):
            fail("owned_work_contract_required")
        lease = self.admit(kind, parent=parent, persist_required=persist is not None)

        async def run():
            token = _CURRENT.set(lease)
            try:
                result = await function(lease)
                if persist is not None:
                    await lease.wait_owned()
                    await lease.finish(lambda: persist(result))
                return result
            except asyncio.CancelledError:
                lease.block("owned_task_cancelled")
                raise
            except BaseException:
                if lease._persist != "failed":
                    lease.block("owner_failed")
                raise
            finally:
                _CURRENT.reset(token)
                lease.release()

        with self._lock:
            lease._async_pending = True
            self._changed()
        try:
            task = asyncio.create_task(run())
        except BaseException:
            lease._async_pending = False
            lease.block()
            lease.release()
            raise

        def completed(value):
            # Retrieve exceptions even when the HTTP waiter has disconnected.
            if not value.cancelled():
                value.exception()
            with self._lock:
                lease._async_pending = False
                self._changed()
                self._collect(lease)

        task.add_done_callback(completed)
        return WorkHandle(lease, task)

    def _attach_process(self, lease, process, start_ticks):
        with self._lock:
            self._check_lease(lease)
            if (
                not isinstance(process, (subprocess.Popen, asyncio.subprocess.Process))
                or not _uint(process.pid, 2)
                or not _uint(start_ticks, 1)
                or process.pid in lease._processes
            ):
                fail("process_registration_rejected")
            lease._processes[process.pid] = {
                "process": process,
                "start_ticks": start_ticks,
                "reaped": False,
                "waiter": None,
            }
            self._changed()

    def _wait_process_sync(self, lease, process):
        with self._lock:
            self._check_lease(lease)
            row = lease._processes.get(getattr(process, "pid", None))
            if (
                not isinstance(process, subprocess.Popen)
                or row is None
                or row["process"] is not process
                or row["waiter"] is not None
                or row.get("sync_waiting")
            ):
                fail("process_registration_required")
            row["sync_waiting"] = True
            self._changed()
        try:
            # Never submit another job to the pool that may be running this call.
            result = process.wait()
            if type(result) is not int or type(process.returncode) is not int or result != process.returncode:
                fail("process_wait_unknown")
        except BaseException:
            with self._lock:
                row["sync_waiting"] = False
                lease._blocked.add("process_unknown")
                self._changed()
            raise LifecycleError("work_lifecycle_process_wait_unknown") from None
        with self._lock:
            row["sync_waiting"] = False
            row["reaped"] = True
            self._changed()
            self._collect(lease)
        return result

    async def _wait_process(self, lease, process):
        with self._lock:
            self._check_lease(lease)
            row = lease._processes.get(process.pid)
            if row is None or row["process"] is not process or row.get("sync_waiting"):
                fail("process_registration_required")
            if row["waiter"] is None:
                if isinstance(process, subprocess.Popen):
                    future = self._pool.submit(process.wait)
                    lease._blocking.add(future)
                    row["waiter"] = future
                    future.add_done_callback(lambda value: self._process_done(lease, row, value))
                else:
                    row["waiter"] = asyncio.create_task(process.wait())
                    row["waiter"].add_done_callback(lambda value: self._process_done(lease, row, value))
                self._changed()
            waiter = row["waiter"]
        if isinstance(waiter, asyncio.Future):
            return await asyncio.shield(waiter)
        return await asyncio.shield(asyncio.wrap_future(waiter))

    def _process_done(self, lease, row, future):
        with self._lock:
            if future.cancelled() or future.exception() is not None or type(future.result()) is not int:
                lease._blocked.add("process_unknown")
            else:
                row["reaped"] = True
            self._changed()
            self._collect(lease)

    def begin_drain(self, ticket):
        with self._lock:
            if self._state != "running":
                fail("drain_state_rejected")
            self._command(ticket)
            self._state = "draining"
            return self.observe()

    def observation_context(self):
        with self._lock:
            return self.identity, self._epoch, self._generation, self._nonce

    def record_os_observation(self, observation):
        """Called only by the reviewed local OS adapter, not a remote request."""
        with self._lock:
            if (
                type(observation) is not OSObservation
                or self.identity is None
                or observation.identity != self.identity
                or not _uint(observation.epoch)
                or not _uint(observation.generation)
                or type(observation.nonce) is not str
                or (observation.epoch, observation.generation, observation.nonce)
                != (self._epoch, self._generation, self._nonce)
                or observation.protect_control_groups != "yes"
                or not _uint(observation.checked_at_ns, 1)
                or not 0 <= time.monotonic_ns() - observation.checked_at_ns <= OS_FRESH_NS
                or type(observation.groups) is not tuple
            ):
                fail("os_observation_rejected")
            groups, members = set(), set()
            for row in observation.groups:
                if type(row) is not tuple or len(row) != 2:
                    fail("os_observation_rejected")
                relative, processes = row
                if (
                    type(relative) is not str
                    or relative in groups
                    or relative != "."
                    and (
                        not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", relative)
                        or any(part in {".", ".."} for part in relative.split("/"))
                    )
                    or type(processes) is not tuple
                ):
                    fail("os_observation_rejected")
                groups.add(relative)
                for member in processes:
                    if (
                        type(member) is not tuple
                        or len(member) != 2
                        or not all(_uint(value, 1) for value in member)
                    ):
                        fail("os_observation_rejected")
                    if member[0] in {item[0] for item in members}:
                        fail("os_observation_rejected")
                    members.add(member)
            if "." not in groups or (self.identity.pid, self.identity.start_ticks) not in members:
                fail("os_observation_rejected")
            self._os = (observation, len(members) - 1)

    def observe(self):
        with self._lock:
            counts = {kind: 0 for kind in sorted(KINDS)}
            for lease in self._leases.values():
                counts[lease.kind] += 1
            os_current = (
                self._os is not None and 0 <= time.monotonic_ns() - self._os[0].checked_at_ns <= OS_FRESH_NS
            )
            return {
                "schema": 1,
                "state": self._state,
                "epoch": self._epoch,
                "generation": self._generation,
                "leases": counts,
                "pending_async": sum(lease._async_pending for lease in self._leases.values()),
                "pending_requests": len(self._requests),
                "pending_cf": sum(
                    not value.done() for lease in self._leases.values() for value in lease._blocking
                ),
                "pending_processes": sum(
                    not row["reaped"] for lease in self._leases.values() for row in lease._processes.values()
                ),
                "persist_pending": sum(
                    lease._persist in {"required", "pending"} for lease in self._leases.values()
                ),
                "persist_blocked": sum(lease._persist == "failed" for lease in self._leases.values()),
                "blocked": sum(bool(lease._blocked) for lease in self._leases.values()),
                "producer_unknown": len(KINDS - self._declared.keys()),
                "os_unknown": 0 if os_current else 1,
                "os_descendants": self._os[1] if os_current else None,
            }

    def _empty(self):
        return not self._leases and not self._requests

    async def wait_drained(self, timeout=None):
        started = time.monotonic()
        while True:
            with self._lock:
                if self._state not in {"draining", "quiescent"}:
                    fail("drain_state_rejected")
                if self._empty():
                    return True
            if timeout is not None and time.monotonic() - started >= timeout:
                return False
            await asyncio.sleep(0.01)

    def seal(self, ticket):
        with self._lock:
            value = self.observe()
            if (
                self._state != "draining"
                or not self._empty()
                or value["producer_unknown"]
                or value["os_unknown"]
                or value["os_descendants"]
            ):
                fail("quiescence_unproven")
            witnessed = self._os
            self._command(ticket)
            self._os = witnessed
            self._state = "quiescent"
            return DrainProof(self._epoch, self._generation, self._nonce)

    def resume(self, ticket):
        with self._lock:
            if self._state != "quiescent" or not self._empty():
                fail("quiescence_unproven")
            self._command(ticket)
            self._state = "running"
            return self.observe()

    def close(self):
        with self._lock:
            if self._state != "quiescent" or not self._empty():
                fail("quiescence_unproven")
            self._state = "closing"
            self._changed()
        # Future callbacks may need the manager lock while their threads exit.
        self._pool.shutdown(wait=True, cancel_futures=False)
        with self._lock:
            self._state = "closed"
            self._changed()

    def close_development(self):
        """Close an identity-free development pool, without certifying quiescence."""
        with self._lock:
            if self.identity is not None or self._state != "draining" or not self._empty():
                fail("development_close_rejected")
            self._state = "closing"
            self._changed()
        self._pool.shutdown(wait=True, cancel_futures=False)
        with self._lock:
            self._state = "closed"
            self._changed()


def current_lease():
    return _CURRENT.get()


def bind_manager(app, manager):
    """A lifespan restart may replace only a closed, empty prior manager."""
    if type(manager) is not Manager or getattr(app, "state", None) is None:
        fail("bound_manager_rejected")
    with _DEFAULT_LOCK:
        previous = getattr(app.state, "work_manager", None)
        if previous is not None and previous is not manager:
            if type(previous) is not Manager:
                fail("bound_manager_rejected")
            with previous._lock:
                if previous._state != "closed" or not previous._empty():
                    fail("prior_manager_not_closed")
        app.state.work_manager = manager
    return manager


@contextmanager
def bind_context(manager):
    """Bind all nested producers in an ASGI scope, including history-only routes."""
    if type(manager) is not Manager:
        fail("bound_manager_rejected")
    token = _SCOPE_MANAGER.set(manager)
    try:
        yield manager
    finally:
        _SCOPE_MANAGER.reset(token)


def get_manager(scope=None):
    """Prefer the app-bound manager; lazy direct-call managers never bypass hold."""
    if scope is not None:
        app = scope.get("app")
        manager = getattr(getattr(app, "state", None), "work_manager", None)
        if manager is not None:
            if type(manager) is not Manager:
                fail("bound_manager_rejected")
            return manager
    lease = current_lease()
    if lease is not None:
        return lease.manager
    scoped = _SCOPE_MANAGER.get()
    if scoped is not None:
        return scoped
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        manager = getattr(_SYNC_DEFAULT, "manager", None)
        if manager is None or manager._state == "closed" and manager._empty():
            manager = Manager()
            _SYNC_DEFAULT.manager = manager
        return manager
    with _DEFAULT_LOCK:
        manager = _DEFAULTS.get(loop)
        if manager is None or manager._state == "closed" and manager._empty():
            manager = Manager()
            _DEFAULTS[loop] = manager
        return manager
