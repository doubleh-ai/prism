"""Linux-shaped kernel fixtures are not Linux attestation; Unix IO is actual local IO."""

import asyncio
import json
import os
import stat
import struct
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from server import work_lifecycle as core
from server import work_runtime as runtime


@pytest.fixture(autouse=True)
def temp_data_dir():
    # The runtime and lifecycle core import no application configuration or DB.
    yield


IDENTITY = core.RuntimeIdentity(
    "12345678-1234-1234-1234-123456789abc",
    2300,
    77,
    "a" * 40,
    "b" * 64,
    "/system.slice/stock-app.service",
)
BINDING = {
    "schema": 1,
    "commit": "a" * 40,
    "artifact_sha256": "b" * 64,
    "role": "main",
    "unit": "stock-app.service",
    "nonce": "c" * 32,
}
SIGNATURE = (1, 2, stat.S_IFREG | 0o444, 0, 0, 1, 200, 3, 4)
MOUNT = "20 1 0:28 / /sys/fs/cgroup ro,nosuid,nodev,noexec - cgroup2 cgroup rw\n"


@pytest.fixture
def manager():
    value = core.Manager(IDENTITY)
    for kind in core.KINDS:
        value.declare(kind)
    yield value
    value._pool.shutdown(wait=True, cancel_futures=False)


def fake_adapter(manager):
    def observe(value):
        return core.OSObservation(
            *value.observation_context(),
            time.monotonic_ns(),
            "yes",
            ((".", ((IDENTITY.pid, IDENTITY.start_ticks),)),),
        )

    return SimpleNamespace(identity=IDENTITY, binding=BINDING.copy(), verify=lambda: None, observe=observe)


def request(manager, action="status"):
    value = {"schema": 1, "action": action, "expected": runtime._expected(IDENTITY)}
    if action != "status":
        value["ticket"] = asdict(manager.ticket())
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [None, "", "0", "true", "yes", "2"])
async def test_disabled_runtime_never_reads_binding_or_kernel_and_closes_development(monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv("PRISM_WORK_CONTROL", raising=False)
    else:
        monkeypatch.setenv("PRISM_WORK_CONTROL", flag)
    monkeypatch.setattr(runtime, "LinuxAdapter", lambda: pytest.fail("must not read Linux binding"))
    value = await runtime.open_runtime(producers=dict.fromkeys(core.KINDS, True))
    assert not value.controlled and value.identity is None and value.server is None
    assert value.manager.observe()["state"] == "running"
    assert value.manager.observe()["producer_unknown"] == 0
    await value.aclose()
    assert value.manager.observe()["state"] == "closed"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_close_waiter", [False, True])
async def test_development_close_with_pending_work_does_not_cancel_or_certify(
    monkeypatch, cancel_close_waiter
):
    monkeypatch.delenv("PRISM_WORK_CONTROL", raising=False)
    value = await runtime.open_runtime(producers=dict.fromkeys(core.KINDS, True))
    manager = value.manager
    entered, release = threading.Event(), threading.Event()
    threads, futures = [], []
    submit = manager._pool.submit

    def capture_submit(*args, **kwargs):
        future = submit(*args, **kwargs)
        futures.append(future)
        return future

    def actual_work():
        threads.append(threading.current_thread())
        entered.set()
        assert release.wait(5), "test did not release its actual worker"
        return "actual-completion"

    async def owner(_lease):
        return await manager.run_blocking("market", actual_work)

    def reject_proof(*args, **kwargs):
        pytest.fail("development shutdown must not fabricate an OS or drain proof")

    monkeypatch.setattr(manager._pool, "submit", capture_submit)
    monkeypatch.setattr(manager, "seal", reject_proof)
    monkeypatch.setattr(manager, "record_os_observation", reject_proof)
    handle = manager.start("market", owner)
    closing = None
    try:
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(0.01)
        assert len(futures) == 1
        actual_future = futures[0]
        closing = asyncio.create_task(value.aclose())
        # Cross the shutdown polling interval while the real CF remains pending.
        await asyncio.sleep(0.08)
        assert not closing.done()
        snapshot = manager.observe()
        assert snapshot["state"] == "draining" and manager.identity is None
        assert snapshot["pending_cf"] == 1 and snapshot["pending_async"] == 1
        assert snapshot["leases"]["market"] == 2 and snapshot["os_unknown"] == 1
        assert not actual_future.done() and not actual_future.cancelled()
        assert threads[0].is_alive() and not handle.done()

        if cancel_close_waiter:
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
            assert manager.observe()["state"] == "draining"
            assert manager.observe()["pending_cf"] == 1
            assert not actual_future.done() and not actual_future.cancelled()
            assert threads[0].is_alive() and not handle.done()
            closing = asyncio.create_task(value.aclose())
            await asyncio.sleep(0.08)
            assert not closing.done() and manager.observe()["pending_cf"] == 1

        release.set()
        assert await asyncio.wait_for(handle.wait(), 2) == "actual-completion"
        assert await asyncio.wait_for(closing, 2) is None
        assert actual_future.done() and not actual_future.cancelled()
        assert not threads[0].is_alive() and handle.done()
        snapshot = manager.observe()
        assert snapshot["state"] == "closed" and snapshot["pending_cf"] == 0
        assert not any(snapshot["leases"].values()) and snapshot["os_unknown"] == 1
    finally:
        release.set()
        if closing is not None and not closing.done():
            closing.cancel()
            await asyncio.gather(closing, return_exceptions=True)
        await asyncio.gather(handle.wait_result(), return_exceptions=True)
        manager._pool.shutdown(wait=True, cancel_futures=False)


@pytest.mark.asyncio
async def test_development_close_does_not_discard_blocked_work(monkeypatch):
    monkeypatch.delenv("PRISM_WORK_CONTROL", raising=False)
    value = await runtime.open_runtime(producers=dict.fromkeys(core.KINDS, True))
    lease = value.manager.admit("auxiliary")
    lease.block("process_unknown")
    lease.release()
    try:
        with pytest.raises(runtime.RuntimeControlError, match="quiescence_unproven"):
            await asyncio.wait_for(value.aclose(), 1)
        snapshot = value.manager.observe()
        assert snapshot["state"] == "draining" and snapshot["blocked"] == 1
        assert snapshot["leases"]["auxiliary"] == 1 and snapshot["os_unknown"] == 1
    finally:
        value.manager._pool.shutdown(wait=True, cancel_futures=False)


@pytest.mark.asyncio
async def test_identity_bearing_runtime_cannot_use_development_close(manager):
    value = runtime.Runtime(manager)
    with pytest.raises(runtime.RuntimeControlError, match="quiescence_unproven"):
        await value.aclose()
    assert manager.observe()["state"] == "running"


@pytest.mark.asyncio
async def test_development_close_waits_for_final_request_observation(monkeypatch):
    monkeypatch.delenv("PRISM_WORK_CONTROL", raising=False)
    value = await runtime.open_runtime(producers=dict.fromkeys(core.KINDS, True))
    observation = value.manager.track_request()
    closing = asyncio.create_task(value.aclose())
    try:
        await asyncio.sleep(0.08)
        snapshot = value.manager.observe()
        assert not closing.done() and snapshot["state"] == "draining"
        assert snapshot["pending_requests"] == 1 and not any(snapshot["leases"].values())
        observation.release()
        await asyncio.wait_for(closing, 2)
        assert value.manager.observe()["state"] == "closed"
    finally:
        observation.release()
        if not closing.done():
            closing.cancel()
            await asyncio.gather(closing, return_exceptions=True)
        value.manager._pool.shutdown(wait=True, cancel_futures=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("producers", [{}, {"request": True}, dict.fromkeys(core.KINDS, 1)])
async def test_all_producers_must_be_explicit_booleans(producers):
    with pytest.raises(runtime.RuntimeControlError, match="binding_rejected"):
        await runtime.open_runtime(producers=producers)


def test_controlled_mode_is_linux_nonroot_and_peercred_only(monkeypatch):
    monkeypatch.setattr(runtime, "sys", SimpleNamespace(platform="darwin"))
    with pytest.raises(runtime.RuntimeControlError, match="platform_rejected"):
        runtime.LinuxAdapter()
    monkeypatch.setattr(runtime, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(runtime.socket, "SO_PEERCRED", 17, raising=False)
    monkeypatch.setattr(runtime.os, "geteuid", lambda: 0)
    with pytest.raises(runtime.RuntimeControlError, match="platform_rejected"):
        runtime.LinuxAdapter()


@pytest.mark.parametrize("role", ["main", "recovery"])
@pytest.mark.parametrize("kind", ["production", "stage", "stop"])
def test_fixed_role_unit_allowlist_is_derived_from_root_binding(monkeypatch, role, kind):
    binding = {**BINDING, "role": role}
    binding["unit"] = {
        "production": "stock-app.service",
        "stage": "prism-stage-" + "a" * 12 + ".service",
        "stop": "prism-new-runtime-stop-"
        + "c" * 32
        + "-"
        + ("candidate" if role == "main" else "recovery")
        + ".service",
    }[kind]
    monkeypatch.setattr(runtime, "_read", lambda path, limit: (json.dumps(binding).encode(), SIGNATURE))
    assert runtime._binding()[0] == binding


@pytest.mark.parametrize(
    "change",
    [
        {"schema": True},
        {"role": "candidate"},
        {"role": []},
        {"commit": "a" * 39},
        {"artifact_sha256": "b" * 63},
        {"nonce": "../x"},
        {"unit": "ssh.service"},
        {"unit": "prism-stage-" + "d" * 12 + ".service"},
        {"unit": "prism-new-runtime-stop-" + "c" * 32 + "-recovery.service"},
        {"unit": "prism-new-runtime-stop-" + "c" * 32 + "-candidate-ref.service"},
        {"path": "/tmp/elsewhere"},
    ],
)
def test_binding_rejects_unknown_fields_types_and_nonfixed_units(monkeypatch, change):
    monkeypatch.setattr(
        runtime, "_read", lambda path, limit: (json.dumps({**BINDING, **change}).encode(), SIGNATURE)
    )
    with pytest.raises(runtime.RuntimeControlError, match="binding_rejected"):
        runtime._binding()


@pytest.mark.parametrize(("index", "value"), [(2, stat.S_IFREG | 0o644), (3, 501), (5, 2)])
def test_binding_requires_real_root_mode_and_single_link_metadata(monkeypatch, index, value):
    signature = list(SIGNATURE)
    signature[index] = value
    monkeypatch.setattr(
        runtime, "_read", lambda path, limit: (json.dumps(BINDING).encode(), tuple(signature))
    )
    with pytest.raises(runtime.RuntimeControlError, match="binding_rejected"):
        runtime._binding()


@pytest.mark.parametrize("raw", [b'{"schema":1,"schema":1}', b"NaN", b"{} trailing"])
def test_ambiguous_binding_json_is_rejected(monkeypatch, raw):
    monkeypatch.setattr(runtime, "_read", lambda path, limit: (raw, SIGNATURE))
    with pytest.raises(runtime.RuntimeControlError, match="binding_rejected"):
        runtime._binding()


def test_fixed_reader_refuses_symlink_and_oversized_actual_files(tmp_path):
    actual = tmp_path / "binding"
    actual.write_bytes(b"abcd")
    link = tmp_path / "alias"
    link.symlink_to(actual)
    with pytest.raises(OSError):
        runtime._read(link)
    with pytest.raises(runtime.RuntimeControlError, match="kernel_unknown"):
        runtime._read(actual, 3)
    assert runtime._read(actual, 4)[0] == b"abcd"


def test_fixed_reader_detects_path_replacement_during_read(tmp_path, monkeypatch):
    actual, replacement = tmp_path / "binding", tmp_path / "replacement"
    actual.write_bytes(b"old")
    replacement.write_bytes(b"new")
    original = runtime.os.read
    replaced = False

    def read(fd, count):
        nonlocal replaced
        value = original(fd, count)
        if not replaced:
            replaced = True
            replacement.replace(actual)
        return value

    monkeypatch.setattr(runtime.os, "read", read)
    with pytest.raises(runtime.RuntimeControlError, match="kernel_unknown"):
        runtime._read(actual)


@pytest.mark.parametrize(
    "mount",
    [
        MOUNT.replace(" ro,", " rw,"),
        MOUNT.replace("cgroup2", "tmpfs"),
        MOUNT.replace("0:28 / /sys", "0:28 /partial /sys"),
        "",
        MOUNT + MOUNT,
        MOUNT + "21 20 0:28 / /sys/fs/cgroup/system.slice ro - cgroup2 cgroup rw\n",
        MOUNT + "21 20 0:28 / /sys/fs/cgroup/system.slice rw - cgroup2 cgroup rw\n",
    ],
)
def test_readonly_cgroup_requires_actual_full_uncovered_mount(monkeypatch, mount):
    monkeypatch.setattr(runtime, "_read", lambda path: (mount.encode(), SIGNATURE))
    with pytest.raises(runtime.RuntimeControlError, match="kernel_unknown"):
        runtime._readonly_cgroup()


def test_mount_readonly_uses_mount_flags_not_superblock_rw_flag(monkeypatch):
    monkeypatch.setattr(runtime, "_read", lambda path: (MOUNT.encode(), SIGNATURE))
    assert runtime._readonly_cgroup()[0][-2:] == ("ro,nosuid,nodev,noexec", "cgroup2")


@pytest.mark.parametrize(
    "raw", [b"1:cpu:/system.slice/stock-app.service\n", b"0::/a/../b\n", b"0::/a\n1:cpu:/a\n"]
)
def test_only_unified_nontraversing_actual_cgroup_is_accepted(monkeypatch, raw):
    monkeypatch.setattr(runtime, "_read", lambda *args: (raw, SIGNATURE))
    with pytest.raises(runtime.RuntimeControlError, match="kernel_unknown"):
        runtime._process_group(2300)


def test_proc_stat_handles_parentheses_in_command_name(monkeypatch):
    raw = ("2300 (synthetic ) name) " + " ".join(["S", *(["0"] * 18), "77", "0"]) + "\n").encode()
    monkeypatch.setattr(runtime, "_read", lambda *args: (raw, SIGNATURE))
    assert runtime._ticks(2300) == 77


@pytest.fixture
def cgroup_fixture(tmp_path, monkeypatch):
    root = tmp_path / "cgroup"
    group = root / "system.slice/stock-app.service"
    group.mkdir(parents=True)
    (group / "cgroup.procs").write_text("2300\n")
    empty = group / "empty"
    empty.mkdir()
    (empty / "cgroup.procs").write_text("")
    monkeypatch.setattr(runtime, "_CGROUP", root)
    monkeypatch.setattr(runtime, "_ticks", lambda pid: 77)
    monkeypatch.setattr(runtime, "_process_group", lambda pid: IDENTITY.cgroup)
    adapter = object.__new__(runtime.LinuxAdapter)
    adapter.identity = IDENTITY
    adapter.verify = lambda: None
    return adapter, group


def test_actual_recursive_dirfd_inventory_preserves_empty_subgroups(cgroup_fixture):
    adapter, _ = cgroup_fixture
    rows, identities = adapter._inventory()
    assert rows == ((".", ((2300, 77),)), ("empty", ()))
    assert [row[0] for row in identities] == [".", "empty"]


@pytest.mark.parametrize("mutation", ["symlink", "duplicate", "unknown_process", "missing", "limit"])
def test_recursive_inventory_rejects_ambiguous_or_incomplete_kernel_fixture(
    cgroup_fixture, monkeypatch, mutation
):
    adapter, group = cgroup_fixture
    if mutation == "symlink":
        (group / "alias").symlink_to(group / "empty", target_is_directory=True)
    elif mutation == "duplicate":
        (group / "cgroup.procs").write_text("2300\n2300\n")
    elif mutation == "unknown_process":
        monkeypatch.setattr(runtime, "_process_group", lambda pid: "/elsewhere/other.service")
    elif mutation == "missing":
        (group / "empty/cgroup.procs").unlink()
    else:
        monkeypatch.setattr(runtime, "_MAX_GROUPS", 1)
    with pytest.raises((runtime.RuntimeControlError, OSError)):
        adapter._inventory()


def test_double_sampling_rejects_tree_or_manager_generation_drift(cgroup_fixture, manager, monkeypatch):
    adapter, _ = cgroup_fixture
    stable = adapter._inventory()
    calls = [stable, (stable[0], (*stable[1], ("unseen", 1, 2)))]
    monkeypatch.setattr(adapter, "_inventory", lambda: calls.pop(0))
    with pytest.raises(runtime.RuntimeControlError, match="kernel_unknown"):
        adapter.observe(manager)

    def changed():
        lease = manager.admit("request")
        lease.release()
        return stable

    monkeypatch.setattr(adapter, "_inventory", changed)
    with pytest.raises(runtime.RuntimeControlError, match="kernel_unknown"):
        adapter.observe(manager)


def test_stable_kernel_fixture_builds_internal_os_observation(cgroup_fixture, manager):
    adapter, _ = cgroup_fixture
    value = adapter.observe(manager)
    assert type(value) is core.OSObservation and value.groups[0] == (".", ((2300, 77),))
    assert value.protect_control_groups == "yes"


def test_initial_fence_status_seal_resume_and_stale_ticket_rejection(manager):
    value = runtime.Runtime(manager, fake_adapter(manager))
    manager.begin_drain(manager.ticket())
    status = value.dispatch(request(manager))
    assert status["snapshot"]["state"] == "draining"
    assert status["ticket"] == asdict(manager.ticket())
    stale = request(manager, "seal")
    result = value.dispatch(stale)
    assert result["snapshot"]["state"] == "quiescent"
    with pytest.raises(runtime.RuntimeControlError, match="ticket_rejected"):
        value.dispatch(stale)
    assert value.dispatch(request(manager, "resume"))["snapshot"]["state"] == "running"
    assert value.dispatch(request(manager, "fence"))["snapshot"]["state"] == "draining"


@pytest.mark.parametrize(
    "change",
    [
        {"action": "stop"},
        {"action": []},
        {"schema": True},
        {"path": "/proc/1"},
        {"command": "stop"},
        {"OSObservation": {}},
        {"descendants_empty": True},
        {"expected": {**runtime._expected(IDENTITY), "pid": True}},
        {"expected": {**runtime._expected(IDENTITY), "pid": 2301}},
        {"ticket": {"epoch": 0, "nonce": "x"}},
    ],
)
def test_fixed_control_protocol_cannot_supply_observation_or_execution_targets(manager, change):
    value = runtime.Runtime(manager, fake_adapter(manager))
    before = manager.observe()
    with pytest.raises(runtime.RuntimeControlError):
        value.dispatch({**request(manager), **change})
    assert manager.observe() == before


def test_seal_requires_real_adapter_sampling_and_no_registered_work(manager):
    value = runtime.Runtime(manager, fake_adapter(manager))
    lease = manager.admit("request")
    manager.begin_drain(manager.ticket())
    with pytest.raises(runtime.RuntimeControlError, match="quiescence_unproven"):
        value.dispatch(request(manager, "seal"))
    lease.release()
    value.adapter.observe = lambda _: runtime._fail("kernel_unknown")
    with pytest.raises(runtime.RuntimeControlError, match="kernel_unknown"):
        value.dispatch(request(manager, "seal"))
    assert manager.observe()["state"] == "draining"


@pytest.mark.asyncio
async def test_close_requires_explicit_seal_and_fresh_observation(manager):
    value = runtime.Runtime(manager, fake_adapter(manager))
    manager.begin_drain(manager.ticket())
    with pytest.raises(runtime.RuntimeControlError, match="quiescence_unproven"):
        await value.aclose()
    value.dispatch(request(manager, "seal"))
    value.adapter.observe = lambda _: runtime._fail("kernel_unknown")
    with pytest.raises(runtime.RuntimeControlError, match="kernel_unknown"):
        await value.aclose()
    assert manager.observe()["state"] == "quiescent"
    value.adapter = fake_adapter(manager)
    await value.aclose()
    assert manager.observe()["state"] == "closed"


class FakeWriter:
    def __init__(self, uid):
        self.uid, self.output, self.closed = uid, [], False

    def get_extra_info(self, name):
        return SimpleNamespace(getsockopt=lambda *args: struct.pack("3i", 123, self.uid, 0))

    def write(self, value):
        self.output.append(value)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


@pytest.mark.asyncio
async def test_nonroot_peer_is_refused_before_any_payload_read(manager, monkeypatch):
    monkeypatch.setattr(runtime.socket, "SO_PEERCRED", 17, raising=False)
    writer = FakeWriter(501)
    reader = SimpleNamespace(readexactly=lambda _: pytest.fail("nonroot must never be parsed"))
    await runtime.Runtime(manager, fake_adapter(manager))._handle(reader, writer)
    assert writer.closed and not writer.output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema":1,"schema":1}',
        b'{"schema":NaN}',
        b"{} trailing",
        b"\xff",
    ],
)
async def test_authenticated_json_frame_rejects_ambiguous_payload(manager, monkeypatch, payload):
    monkeypatch.setattr(runtime.socket, "SO_PEERCRED", 17, raising=False)
    writer, reader = FakeWriter(0), asyncio.StreamReader()
    reader.feed_data(struct.pack("!I", len(payload)) + payload)
    await runtime.Runtime(manager, fake_adapter(manager))._handle(reader, writer)
    result = json.loads(b"".join(writer.output))
    assert not result["ok"] and result["error"] == "request_rejected"


async def listen_fixture(value, tmp_path, monkeypatch):
    # AF_UNIX has a short kernel pathname limit; pytest's descriptive path is longer.
    root = Path(tempfile.mkdtemp(prefix="u", dir=os.environ.get("TMPDIR") or tempfile.gettempdir()))
    monkeypatch.setattr(runtime, "_RUN", root)
    directory = root / ("prism-work-" + BINDING["nonce"])
    directory.mkdir(mode=0o700)
    try:
        await value.listen()
    except PermissionError:
        pytest.skip("filesystem AF_UNIX is unavailable in this host sandbox")
    return directory


@pytest.mark.asyncio
async def test_real_filesystem_unix_transport_with_explicit_simulated_root_credential(
    manager, tmp_path, monkeypatch
):
    # This is actual AF_UNIX IO; this patched root credential is NOT Linux authentication evidence.
    value = runtime.Runtime(manager, fake_adapter(manager))
    manager.begin_drain(manager.ticket())
    monkeypatch.setattr(runtime, "_peer_root", lambda _: None)
    await listen_fixture(value, tmp_path, monkeypatch)
    try:
        reader, writer = await asyncio.open_unix_connection(str(value.endpoint))
        body = json.dumps(request(manager)).encode()
        writer.write(struct.pack("!I", len(body)) + body)
        await writer.drain()
        reply = json.loads(await asyncio.wait_for(reader.readline(), 2))
        assert reply["ok"] and reply["snapshot"]["state"] == "draining"
        writer.close()
        await writer.wait_closed()
        value.dispatch(request(manager, "seal"))
        await value.aclose()
        assert not value.endpoint.exists()
    finally:
        if value.server is not None:
            value.server.close()
            await value.server.wait_closed()
        if value.directory_fd is not None:
            value._unlink_owned()


@pytest.mark.asyncio
async def test_actual_nonroot_or_nonlinux_unix_peer_is_not_authenticated(manager, tmp_path, monkeypatch):
    if sys.platform == "linux" and os.geteuid() == 0:
        pytest.skip("this negative control requires an actual nonroot or nonlinux peer")
    value = runtime.Runtime(manager, fake_adapter(manager))
    await listen_fixture(value, tmp_path, monkeypatch)
    try:
        reader, writer = await asyncio.open_unix_connection(str(value.endpoint))
        assert await asyncio.wait_for(reader.read(), 2) == b""
        writer.close()
        await writer.wait_closed()
    finally:
        value.server.close()
        await value.server.wait_closed()
        value._unlink_owned()


@pytest.mark.asyncio
async def test_existing_endpoint_is_not_unlinked(manager, tmp_path, monkeypatch):
    value = runtime.Runtime(manager, fake_adapter(manager))
    monkeypatch.setattr(runtime, "_RUN", tmp_path)
    directory = tmp_path / ("prism-work-" + BINDING["nonce"])
    directory.mkdir(mode=0o700)
    existing = directory / "control.sock"
    existing.write_text("unowned evidence")
    with pytest.raises(runtime.RuntimeControlError, match="socket_rejected"):
        await value.listen()
    assert existing.read_text() == "unowned evidence"


@pytest.mark.asyncio
async def test_endpoint_replacement_is_preserved_during_cleanup(manager, tmp_path, monkeypatch):
    value = runtime.Runtime(manager, fake_adapter(manager))
    await listen_fixture(value, tmp_path, monkeypatch)
    value.server.close()
    await value.server.wait_closed()
    value.endpoint.unlink()
    value.endpoint.write_text("replacement must remain")
    with pytest.raises(runtime.RuntimeControlError, match="socket_rejected"):
        value._unlink_owned()
    assert value.endpoint.read_text() == "replacement must remain"


@pytest.mark.asyncio
async def test_enabled_startup_declares_then_fences_before_opening_control_endpoint(monkeypatch):
    monkeypatch.setenv("PRISM_WORK_CONTROL", "1")
    constructed = []

    def adapter():
        value = SimpleNamespace(identity=IDENTITY)
        constructed.append(value)
        return value

    async def listen(value):
        assert value.manager.observe()["producer_unknown"] == 0
        assert value.manager.observe()["state"] == "draining"

    monkeypatch.setattr(runtime, "LinuxAdapter", adapter)
    monkeypatch.setattr(runtime.Runtime, "listen", listen)
    value = await runtime.open_runtime(producers=dict.fromkeys(core.KINDS, True))
    assert len(constructed) == 1 and value.controlled
    value.adapter = fake_adapter(value.manager)
    value.dispatch(request(value.manager, "seal"))
    await value.aclose()


@pytest.mark.asyncio
async def test_seal_does_not_sample_os_until_all_producers_declared_and_work_empty(manager):
    adapter = fake_adapter(manager)
    adapter.observe = lambda _: pytest.fail("pending work cannot request an OS proof")
    value = runtime.Runtime(manager, adapter)
    lease = manager.admit("request")
    manager.begin_drain(manager.ticket())
    with pytest.raises(runtime.RuntimeControlError, match="quiescence_unproven"):
        value.dispatch(request(manager, "seal"))
    lease.release()
    missing = core.Manager(IDENTITY)
    try:
        missing.begin_drain(missing.ticket())
        value = runtime.Runtime(missing, adapter)
        with pytest.raises(runtime.RuntimeControlError, match="quiescence_unproven"):
            value.dispatch(request(missing, "seal"))
    finally:
        missing._pool.shutdown(wait=True, cancel_futures=False)


@pytest.fixture
def linux_kernel_fixture(tmp_path, monkeypatch):
    # All kernel/root ownership values in this fixture are synthetic, not host authentication.
    kernel = {
        str(runtime._BINDING): (json.dumps(BINDING).encode(), SIGNATURE),
        str(runtime._ARTIFACT / ".deploy-revision"): (b"a" * 40 + b"\n", SIGNATURE),
        "/proc/sys/kernel/random/boot_id": (IDENTITY.boot_id.encode() + b"\n", SIGNATURE),
        "/proc/2300/cgroup": (b"0::/system.slice/stock-app.service\n", SIGNATURE),
        "/proc/2300/stat": (
            ("2300 (app) " + " ".join(["S", *(["0"] * 18), "77", "0"]) + "\n").encode(),
            SIGNATURE,
        ),
        "/proc/self/mountinfo": (MOUNT.encode(), SIGNATURE),
    }
    monkeypatch.setattr(runtime, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(runtime.socket, "SO_PEERCRED", 17, raising=False)
    monkeypatch.setattr(
        runtime, "os", SimpleNamespace(geteuid=lambda: 12345, getpid=lambda: 2300, close=os.close)
    )
    monkeypatch.setattr(
        runtime, "_directory", lambda *args, **kwargs: (os.open(tmp_path, os.O_RDONLY), (1, 2))
    )
    monkeypatch.setattr(runtime, "_read", lambda path, *args, **kwargs: kernel[str(path)])
    return kernel


def test_linux_fixture_reads_own_identity_and_revalidates_root_binding(linux_kernel_fixture):
    adapter = runtime.LinuxAdapter()
    assert adapter.identity == IDENTITY
    adapter.verify()
    raw, signature = linux_kernel_fixture[str(runtime._BINDING)]
    linux_kernel_fixture[str(runtime._BINDING)] = (raw, (signature[0], 999, *signature[2:]))
    with pytest.raises(runtime.RuntimeControlError, match="runtime_identity_rejected"):
        adapter.verify()


@pytest.mark.parametrize("drift", ["boot", "ticks", "cgroup", "revision", "mount"])
def test_linux_fixture_rejects_actual_identity_source_or_mount_drift(linux_kernel_fixture, drift):
    adapter = runtime.LinuxAdapter()
    key, raw = {
        "boot": ("/proc/sys/kernel/random/boot_id", b"87654321-1234-1234-1234-123456789abc\n"),
        "ticks": (
            "/proc/2300/stat",
            ("2300 (app) " + " ".join(["S", *(["0"] * 18), "78", "0"]) + "\n").encode(),
        ),
        "cgroup": ("/proc/2300/cgroup", b"0::/user.slice/stock-app.service\n"),
        "revision": (str(runtime._ARTIFACT / ".deploy-revision"), b"d" * 40 + b"\n"),
        "mount": ("/proc/self/mountinfo", MOUNT.replace(" ro,", " rw,").encode()),
    }[drift]
    linux_kernel_fixture[key] = (raw, SIGNATURE)
    with pytest.raises(runtime.RuntimeControlError):
        adapter.verify()


@pytest.mark.parametrize(("index", "value"), [(2, stat.S_IFREG | 0o666), (3, 12345), (5, 2)])
def test_artifact_revision_must_be_single_link_root_owned_not_group_writable(
    linux_kernel_fixture, index, value
):
    key = str(runtime._ARTIFACT / ".deploy-revision")
    raw, signature = linux_kernel_fixture[key]
    changed = list(signature)
    changed[index] = value
    linux_kernel_fixture[key] = (raw, tuple(changed))
    with pytest.raises(runtime.RuntimeControlError, match="binding_rejected"):
        runtime.LinuxAdapter()


def test_actual_child_process_in_kernel_fixture_cannot_be_sealed(cgroup_fixture, manager, monkeypatch):
    adapter, group = cgroup_fixture
    (group / "empty/cgroup.procs").write_text("2400\n")
    monkeypatch.setattr(
        runtime, "_process_group", lambda pid: IDENTITY.cgroup + ("/empty" if pid == 2400 else "")
    )
    with pytest.raises(runtime.RuntimeControlError, match="quiescence_unproven"):
        adapter.observe(manager)


@pytest.mark.asyncio
@pytest.mark.parametrize("length", [0, runtime._MAX_FRAME + 1, 0xFFFFFFFF])
async def test_frame_length_is_rejected_before_payload_allocation(manager, monkeypatch, length):
    monkeypatch.setattr(runtime.socket, "SO_PEERCRED", 17, raising=False)
    writer, reader = FakeWriter(0), asyncio.StreamReader()
    reader.feed_data(struct.pack("!I", length))
    await runtime.Runtime(manager, fake_adapter(manager))._handle(reader, writer)
    result = json.loads(b"".join(writer.output))
    assert not result["ok"] and result["error"] == "request_rejected"


@pytest.mark.asyncio
async def test_socket_constructor_failure_closes_only_opened_directory_fd(manager, tmp_path, monkeypatch):
    value = runtime.Runtime(manager, fake_adapter(manager))
    monkeypatch.setattr(runtime, "_RUN", tmp_path)
    directory = tmp_path / ("prism-work-" + BINDING["nonce"])
    directory.mkdir(mode=0o700)
    original_directory = runtime._directory
    opened = []

    def track(*args, **kwargs):
        result = original_directory(*args, **kwargs)
        opened.append(result[0])
        return result

    def refuse(*args):
        raise OSError("synthetic socket construction failure")

    monkeypatch.setattr(runtime, "_directory", track)
    monkeypatch.setattr(runtime.socket, "socket", refuse)
    with pytest.raises(OSError):
        await value.listen()
    assert len(opened) == 1 and value.directory_fd is None
    with pytest.raises(OSError):
        os.fstat(opened[0])
    assert list(directory.iterdir()) == []


@pytest.mark.asyncio
async def test_closing_runtime_waits_for_only_its_active_control_handlers(manager, tmp_path, monkeypatch):
    value = runtime.Runtime(manager, fake_adapter(manager))
    manager.begin_drain(manager.ticket())
    monkeypatch.setattr(runtime, "_peer_root", lambda _: None)
    await listen_fixture(value, tmp_path, monkeypatch)
    reader, writer = await asyncio.open_unix_connection(str(value.endpoint))
    try:
        for _ in range(20):
            if value._handlers:
                break
            await asyncio.sleep(0.005)
        assert value._handlers
        value.dispatch(request(manager, "seal"))
        await value.aclose()
        assert not value._handlers and not value._writers and not value.endpoint.exists()
        await asyncio.wait_for(reader.read(), 2)
        assert manager.observe()["state"] == "closed"
    finally:
        writer.close()
        await writer.wait_closed()
        if value.server is not None:
            value.server.close()
            await value.server.wait_closed()
        if value.directory_fd is not None:
            value._unlink_owned()


def test_pid_sampling_deadline_cannot_be_refreshed_after_slow_kernel_reads(cgroup_fixture, monkeypatch):
    adapter, _ = cgroup_fixture
    clock = iter((1, 1, runtime._SAMPLE_NS + 2))
    monkeypatch.setattr(
        runtime, "time", SimpleNamespace(monotonic_ns=lambda: next(clock, runtime._SAMPLE_NS + 2))
    )
    with pytest.raises(runtime.RuntimeControlError, match="kernel_unknown"):
        adapter._inventory()


def test_entire_double_sample_has_one_bounded_observation_window(cgroup_fixture, manager, monkeypatch):
    adapter, _ = cgroup_fixture
    stable = adapter._inventory()
    monkeypatch.setattr(adapter, "_inventory", lambda: stable)
    clock = iter((1, runtime._SAMPLE_NS + 2))
    monkeypatch.setattr(
        runtime, "time", SimpleNamespace(monotonic_ns=lambda: next(clock, runtime._SAMPLE_NS + 2))
    )
    with pytest.raises(runtime.RuntimeControlError, match="kernel_unknown"):
        adapter.observe(manager)


@pytest.mark.asyncio
async def test_directory_identity_is_rechecked_after_actual_socket_bind(manager, tmp_path, monkeypatch):
    value = runtime.Runtime(manager, fake_adapter(manager))
    original = runtime._directory
    calls = 0

    def changed(*args, **kwargs):
        nonlocal calls
        fd, identity = original(*args, **kwargs)
        calls += 1
        return fd, (identity[0], identity[1] + 1) if calls == 2 else identity

    monkeypatch.setattr(runtime, "_directory", changed)
    with pytest.raises(runtime.RuntimeControlError, match="socket_rejected"):
        await listen_fixture(value, tmp_path, monkeypatch)
    assert calls == 2 and value.directory_fd is None and value.server is None
    assert not value.endpoint.exists()


@pytest.mark.asyncio
async def test_aclose_preserves_replaced_endpoint_and_returns_only_finite_error(
    manager, tmp_path, monkeypatch
):
    value = runtime.Runtime(manager, fake_adapter(manager))
    manager.begin_drain(manager.ticket())
    await listen_fixture(value, tmp_path, monkeypatch)
    value.dispatch(request(manager, "seal"))
    value.endpoint.unlink()
    value.endpoint.write_text("unknown replacement")
    with pytest.raises(runtime.RuntimeControlError, match="socket_rejected"):
        await value.aclose()
    assert manager.observe()["state"] == "closed"
    assert value.endpoint.read_text() == "unknown replacement"
