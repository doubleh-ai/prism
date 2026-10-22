"""Offline specification and ownership tests; never a real Linux/BPF receipt."""

import ctypes
import dataclasses
import ipaddress
import itertools
import struct
import types

import pytest

from scripts import cgroup_network_gate as gate


def descriptor(role="live", **changes):
    values = {
        "role": role,
        "candidate_prefix": "a" * 12,
        "nonce": "b" * 32,
        "invocation_id": "c" * 32,
        "pid": 1234,
        "startticks": 4567,
        "uid": 1002,
        "cgroup_inode": 9001,
        "cgroup_device": 31,
        # Second address exercises unsigned JMP32 with a signed instruction immediate.
        "provider_ipv4": ("1.1.1.1", "200.1.2.3"),
    }
    return gate.ControllerDescriptor(**(values | changes))


def execute(program, *, address, port, family=2, user_family=2, socket_type=1, protocol=6):
    """Independent interpreter for the emitted eBPF subset, not builder logic.

    This implements instruction semantics and UAPI context bytes only. Test
    expectations below are a separate target policy specification.
    """
    context = bytearray(72)
    struct.pack_into("<I", context, 0, user_family)
    context[4:8] = ipaddress.IPv4Address(address).packed
    context[24:26] = port.to_bytes(2, "big")
    struct.pack_into("<III", context, 28, family, socket_type, protocol)
    registers = [0] * 11
    pc = 0
    for _ in range(256):
        assert 0 <= pc * 8 < len(program), "invalid jump"
        code, fields, offset, immediate = struct.unpack_from("<BBhi", program, pc * 8)
        dst, src = fields & 15, fields >> 4
        pc += 1
        if code == 0x61:
            assert src == 1 and 0 <= offset <= len(context) - 4
            registers[dst] = int.from_bytes(context[offset : offset + 4], "little")
        elif code == 0xB7:
            registers[dst] = immediate & (2**64 - 1)
        elif code in (0x16, 0x56):
            match = (registers[dst] & 0xFFFFFFFF) == (immediate & 0xFFFFFFFF)
            if match == (code == 0x16):
                pc += offset
        elif code == 0x95:
            return registers[0]
        else:
            raise AssertionError("unexpected opcode/helper/map: " + hex(code))
    raise AssertionError("program did not terminate")


@pytest.mark.parametrize("role", ["live", "market"])
def test_independent_complete_target_matrix(role):
    config = descriptor(role)
    addresses = [
        "1.1.1.1",
        "200.1.2.3",
        "8.8.8.8",
        "127.0.0.53",
        "127.0.0.1",
        "169.254.169.254",
        "10.0.0.1",
        "0.0.0.0",
        "255.255.255.255",
    ]
    # Covers positive provider TLS; DNS TCP/UDP; five existing negative classes;
    # port confusion, raw/type confusion, IPv6-in-v4 hooks, and protocol mismatch.
    for hook, address, port, socket_type, protocol, family, user_family in itertools.product(
        (10, 11, 14, 15),
        addresses,
        (0, 53, 80, 443, 8088, 8899, 65535),
        (1, 2, 3),
        (0, 6, 17, 255),
        (2, 10),
        (2, 10),
    ):
        expected = (
            family == user_family == 2
            and hook == 10
            and socket_type == 1
            and protocol == 6
            and address in {"1.1.1.1", "200.1.2.3"}
            and port == 443
        ) or (
            role == "live"
            and family == user_family == 2
            and address == "127.0.0.53"
            and port == 53
            and (
                (hook == 10 and (socket_type, protocol) in {(1, 6), (2, 17)})
                or (hook == 14 and (socket_type, protocol) == (2, 17))
            )
        )
        actual = execute(
            gate._program(config, hook),
            address=address,
            port=port,
            socket_type=socket_type,
            protocol=protocol,
            family=family,
            user_family=user_family,
        )
        assert actual == int(expected), (
            role,
            hook,
            address,
            port,
            socket_type,
            protocol,
            family,
            user_family,
        )


def test_connected_udp_provider_bypass_is_rejected_at_connect_and_sendmsg():
    config = descriptor()
    for hook in (10, 14):
        assert (
            execute(gate._program(config, hook), address="1.1.1.1", port=443, socket_type=2, protocol=17) == 0
        )
    # Unix is not among the attachment hooks; these are exclusively INET hooks.
    assert gate._HOOKS == (10, 11, 14, 15)


@pytest.mark.parametrize("role", ["live", "market"])
def test_program_control_flow_has_no_unreachable_or_backward_instructions(role):
    # Linux's verifier rejects unreachable instructions, unlike an interpreter
    # that merely executes the chosen input path.
    for hook in (10, 11, 14, 15):
        program = gate._program(descriptor(role), hook)
        rows = list(struct.iter_unpack("<BBhi", program))
        visited, pending = set(), [0]
        while pending:
            index = pending.pop()
            if index in visited:
                continue
            assert 0 <= index < len(rows)
            visited.add(index)
            opcode, _, offset, _ = rows[index]
            if opcode != 0x95:
                pending.append(index + 1)
            if opcode in (0x16, 0x56):
                assert offset >= 0
                pending.append(index + 1 + offset)
        assert visited == set(range(len(rows)))


@pytest.mark.parametrize(
    "changes",
    [
        {"role": "worker"},
        {"role": "../production"},
        {"candidate_prefix": "../etcpasswd"},
        {"nonce": "A" * 32},
        {"invocation_id": "x" * 32},
        {"pid": True},
        {"pid": 1},
        {"uid": 0},
        {"startticks": 0},
        {"cgroup_inode": -1},
        {"cgroup_device": "31"},
        {"provider_ipv4": ["1.1.1.1"]},
        {"provider_ipv4": ()},
        {"provider_ipv4": ("1.1.1.1",) * 2},
        {"provider_ipv4": tuple(f"8.8.8.{i}" for i in range(1, 10))},
        *(
            {"provider_ipv4": (value,)}
            for value in (
                "127.0.0.53",
                "10.0.0.1",
                "169.254.169.254",
                "224.0.0.1",
                "240.0.0.1",
                "0.0.0.0",
                "100.64.0.1",
                "192.0.2.1",
                "192.0.0.8",
                "::1",
                "::ffff:1.1.1.1",
                "1.1.1.1/32",
                "01.1.1.1",
                123,
            )
        ),
    ],
)
def test_descriptor_rejects_untrusted_shape_and_nonpublic_destinations(changes):
    with pytest.raises(gate.GateError):
        descriptor(**changes)


class KernelFixture:
    """In-memory object/FD lifecycle only. Never creates an OS FD or socket."""

    def __init__(self):
        self.profile = "linux-5.15-source-audit"
        self.attached = {}
        self.inherited = {}
        self.infos = {}
        self.closed = []
        self.events = []
        self.stopped = False
        self.identity_ok = True
        self.fail_attach = None
        self.fail_info = None

    def open_cgroup(self, config):
        self.events.append(("open", config.path))
        return 40

    def check_identity(self, fd, config, *, stopped):
        assert fd == 40 and type(config) is gate.ControllerDescriptor
        self.events.append(("identity", stopped))
        if not self.identity_ok or stopped != self.stopped:
            raise gate.GateError("identity_rejected")
        return "retained_empty" if stopped else "running"

    def query(self, fd, hook, *, effective):
        assert fd == 40
        ids = self.attached.get(hook, ())
        flags = 2 if ids else 0
        if effective:
            ids += self.inherited.get(hook, ())
        self.events.append(("query", hook, effective, ids))
        return flags, ids

    def load(self, config, hook):
        fd = 100 + len(self.infos)
        self.infos[fd] = gate._ProgramInfo(500 + len(self.infos), b"tagbytes", 18, 0, 0)
        self.events.append(("load", hook, fd))
        return fd

    def info(self, fd):
        self.events.append(("info", fd))
        if self.fail_info == fd:
            raise gate.GateError("info_rejected")
        return self.infos[fd]

    def attach(self, fd, hook, program_fd):
        assert fd == 40 and not self.attached.get(hook)
        self.events.append(("attach", hook, program_fd))
        if self.fail_attach == hook:
            raise gate.GateError("attach_rejected")
        self.attached[hook] = (self.infos[program_fd].identifier,)

    def open_program(self, identifier):
        matches = [(fd, info) for fd, info in self.infos.items() if info.identifier == identifier]
        if all(fd in self.closed for fd, info in matches) and not any(
            identifier in ids for ids in self.attached.values()
        ):
            raise gate.GateError("network_gate_syscall_failed", command=13, error_number=2)
        fd = max(self.infos) + 1
        self.infos[fd] = matches[0][1]
        return fd

    def detach(self, fd, hook, program_fd):
        assert fd == 40 and self.stopped
        assert self.attached[hook] == (self.infos[program_fd].identifier,)
        self.events.append(("detach", hook, program_fd))
        self.attached[hook] = ()

    def close(self, fd):
        assert fd not in self.closed
        self.events.append(("close", fd))
        self.closed.append(fd)


def test_install_reverify_and_cleanup_only_after_actual_stopped_identity():
    kernel = KernelFixture()
    instance = gate.OwnedNetworkGate(descriptor(), _kernel=kernel)
    receipt = instance.install()
    assert [row[:2] for row in receipt] == [(10, 500), (11, 501), (14, 502), (15, 503)]
    assert instance.verify() == receipt and kernel.closed == []
    instance.prepare_stop()
    with pytest.raises(gate.GateError, match="cleanup_failed"):
        instance.cleanup()
    assert not any(row[0] == "detach" for row in kernel.events)
    kernel.stopped = True
    instance.cleanup()
    assert instance.state == "cleaned" and set(kernel.closed) == {40, 100, 101, 102, 103}
    with pytest.raises(gate.GateError):
        instance.install()


@pytest.mark.parametrize("inherited", [False, True])
def test_existing_program_prevents_any_load_or_attach(inherited):
    kernel = KernelFixture()
    (kernel.inherited if inherited else kernel.attached)[14] = (777,)
    instance = gate.OwnedNetworkGate(descriptor(), _kernel=kernel)
    with pytest.raises(gate.GateError, match="install_failed"):
        instance.install()
    assert not any(row[0] in {"load", "attach", "detach"} for row in kernel.events)


@pytest.mark.parametrize("mutation", ["identity", "effective", "replacement", "metadata"])
def test_changed_kernel_evidence_fails_closed_without_detaching_unknown(mutation):
    kernel = KernelFixture()
    instance = gate.OwnedNetworkGate(descriptor(), _kernel=kernel)
    instance.install()
    if mutation == "identity":
        kernel.identity_ok = False
    elif mutation == "effective":
        kernel.inherited[15] = (777,)
    elif mutation == "replacement":
        kernel.attached[11] = (777,)
    else:
        kernel.infos[100] = dataclasses.replace(kernel.infos[100], maps=1)
    with pytest.raises(gate.GateError, match="verification_failed"):
        instance.verify()
    kernel.stopped = True
    with pytest.raises(gate.GateError, match="cleanup_failed"):
        instance.cleanup()
    assert kernel.closed == [] and not any(row[0] == "detach" for row in kernel.events)


@pytest.mark.parametrize("boundary", ["info", "attach"])
def test_partial_install_holds_descriptors_until_owned_stopped_cleanup(boundary):
    kernel = KernelFixture()
    if boundary == "info":
        kernel.fail_info = 100
    else:
        kernel.fail_attach = 14
    instance = gate.OwnedNetworkGate(descriptor(), _kernel=kernel)
    with pytest.raises(gate.GateError, match="install_failed"):
        instance.install()
    assert instance.state == "failed" and kernel.closed == []
    kernel.stopped = True
    kernel.fail_info = None
    instance.cleanup()
    assert instance.state == "failed"
    assert set(kernel.closed) == {40, *kernel.infos}


def test_readback_rejection_prevents_attach():
    kernel = KernelFixture()
    original = kernel.info

    def invalid(fd):
        return dataclasses.replace(original(fd), maps=1)

    kernel.info = invalid
    instance = gate.OwnedNetworkGate(descriptor(), _kernel=kernel)
    with pytest.raises(gate.GateError):
        instance.install()
    assert not any(row[0] == "attach" for row in kernel.events)


def test_interruption_retains_failed_state_for_owned_cleanup():
    kernel = KernelFixture()

    def interrupted(*args):
        raise KeyboardInterrupt

    kernel.attach = interrupted
    instance = gate.OwnedNetworkGate(descriptor(), _kernel=kernel)
    with pytest.raises(KeyboardInterrupt):
        instance.install()
    assert instance.state == "failed" and kernel.closed == []
    kernel.stopped = True
    proof = instance.cleanup()
    assert instance.state == "failed" and proof["complete"] is False


def test_uncertain_close_is_never_retried_on_possibly_reused_descriptor():
    kernel = KernelFixture()
    instance = gate.OwnedNetworkGate(descriptor(), _kernel=kernel)
    instance.install()
    instance.prepare_stop()
    kernel.stopped = True

    def uncertain(fd):
        kernel.closed.append(fd)
        raise OSError("synthetic uncertain close")

    kernel.close = uncertain
    with pytest.raises(gate.GateError):
        instance.cleanup()
    with pytest.raises(gate.GateError):
        instance.cleanup()
    assert kernel.closed == [103]


def test_linux_515_uapi_frames_without_executing_a_syscall():
    kernel = object.__new__(gate._LinuxKernel)
    observed = []

    def call(command, attr):
        assert len(attr) == 128
        observed.append(command)
        if command == 5:
            kind, count, instructions, license_pointer = struct.unpack_from("<IIQQ", attr)
            assert kind == 18 and count > 2
            assert ctypes.string_at(instructions, count * 8) == gate._program(descriptor(), 10)
            assert ctypes.string_at(license_pointer) == b"GPL"
            assert struct.unpack_from("<I", attr, 68) == (10,)
            assert attr[24:48] == bytes(24) and attr[72:] == bytes(56)
            return 123
        if command == 15:
            fd, info_length, pointer = struct.unpack_from("<IIQ", attr)
            assert (fd, info_length) == (123, 80)
            raw = bytearray(80)
            struct.pack_into("<II8s", raw, 0, 18, 999, b"12345678")
            struct.pack_into("<II", raw, 48, 0, 0)
            ctypes.memmove(pointer, bytes(raw), 80)
            return 0
        if command == 16:
            fd, hook, query_flags, attach_flags, pointer, count = struct.unpack_from("<IIIIQI", attr)
            assert (fd, hook, query_flags, attach_flags, count) == (40, 10, 1, 0, 64)
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))[0] = 999
            struct.pack_into("<I", attr, 24, 1)
            return 0
        assert command in {8, 9}
        assert struct.unpack_from("<IIIII", attr) == (40, 123, 10, 2 if command == 8 else 0, 0)
        assert attr[20:] == bytes(108)
        return 0

    kernel._call = call
    assert kernel.load(descriptor(), 10) == 123
    assert kernel.info(123) == gate._ProgramInfo(999, b"12345678", 18, 0, 0)
    assert kernel.query(40, 10, effective=True) == (0, (999,))
    kernel.attach(40, 10, 123)
    kernel.detach(40, 10, 123)
    assert observed == [5, 15, 16, 8, 9]


@pytest.mark.parametrize(
    "mutation",
    [None, "invocation", "path", "inode", "uid", "startticks", "membership", "stopped_populated"],
)
def test_actual_identity_parser_rejects_mismatched_evidence(monkeypatch, mutation):
    config = descriptor()
    kernel = object.__new__(gate._LinuxKernel)

    def statfs(fd, buffer):
        assert fd == 40
        ctypes.memmove(buffer, struct.pack("<q", 0x63677270), 8)
        return 0

    kernel.libc = types.SimpleNamespace(fstatfs=statfs)
    inode = config.cgroup_inode + int(mutation == "inode")
    filesystem = types.SimpleNamespace(st_mode=0o40755, st_uid=0, st_ino=inode, st_dev=config.cgroup_device)
    # Replace this module's adapters, not shared os/subprocess functions used by
    # pytest's own temporary-directory teardown.
    monkeypatch.setattr(
        gate,
        "os",
        types.SimpleNamespace(
            O_RDONLY=gate.os.O_RDONLY,
            O_DIRECTORY=gate.os.O_DIRECTORY,
            O_CLOEXEC=gate.os.O_CLOEXEC,
            O_NOFOLLOW=gate.os.O_NOFOLLOW,
            fstat=lambda fd: filesystem,
            stat=lambda *args, **kwargs: filesystem,
            open=lambda *args, **kwargs: 42,
            close=lambda fd: None,
        ),
    )
    monkeypatch.setattr(
        gate, "pwd", types.SimpleNamespace(getpwnam=lambda name: types.SimpleNamespace(pw_uid=config.uid))
    )
    state = {
        "Id": config.unit,
        "InvocationID": "d" * 32 if mutation == "invocation" else config.invocation_id,
        "MainPID": str(config.pid),
        "ExecMainPID": str(config.pid),
        "ControlGroup": "/system.slice/other.service" if mutation == "path" else config.cgroup,
        "ActiveState": "active",
        "User": config.account,
    }
    if mutation == "stopped_populated":
        state.update(MainPID="0", ActiveState="inactive")
    monkeypatch.setattr(
        gate,
        "subprocess",
        types.SimpleNamespace(
            run=lambda *args, **kwargs: types.SimpleNamespace(
                stdout="".join(f"{key}={value}\n" for key, value in state.items()).encode(), stderr=b""
            )
        ),
    )
    texts = {
        "status": "Uid:\t" + "\t".join([str(config.uid + int(mutation == "uid"))] * 4) + "\n",
        "cgroup": "0::" + ("/system.slice/other.service" if mutation == "membership" else config.cgroup),
        "stat": "1234 (trusted bootstrap) "
        + " ".join(["0"] * 19 + [str(config.startticks + int(mutation == "startticks"))]),
        "cgroup.events": "populated 1\nfrozen 0\n",
        "cgroup.procs": "",
    }
    kernel._read_at = lambda fd, name: texts[name]
    if mutation is None:
        kernel.check_identity(40, config, stopped=False)
    else:
        with pytest.raises(gate.GateError):
            kernel.check_identity(40, config, stopped=mutation == "stopped_populated")


class CollectedKernel(KernelFixture):
    """Synthetic 6.8 offline/async release; no actual syscall or kernel claim."""

    def __init__(self):
        super().__init__()
        self.profile = "linux-6.8"
        self.query_error = 2
        self.release_delay = 0
        self.lookup_counts = {}
        self.lookup_fds = []
        self.bad_lookup_info = False
        self.release_error = 2

    def check_identity(self, fd, config, *, stopped):
        super().check_identity(fd, config, stopped=stopped)
        return "collected_offline" if stopped else "running"

    def query(self, fd, hook, *, effective):
        if self.stopped:
            self.events.append(("offline_query", hook, effective, self.query_error))
            if self.query_error is None:
                return 0, ()
            raise gate.GateError("network_gate_syscall_failed", command=16, error_number=self.query_error)
        flags, ids = super().query(fd, hook, effective=effective)
        return (0 if effective else flags), ids

    def open_program(self, identifier):
        assert all(fd in self.closed for fd in (100, 101, 102, 103)), "lookup cannot precede owned close"
        self.events.append(("get_known_id", identifier))
        count = self.lookup_counts.get(identifier, 0)
        self.lookup_counts[identifier] = count + 1
        if count < self.release_delay:
            info = next(info for fd, info in self.infos.items() if info.identifier == identifier)
            fd = 700 + len(self.lookup_fds)
            self.lookup_fds.append(fd)
            self.infos[fd] = dataclasses.replace(info, maps=1) if self.bad_lookup_info else info
            return fd
        raise gate.GateError("network_gate_syscall_failed", command=13, error_number=self.release_error)


@pytest.mark.parametrize("profile", ["linux-6.8", "linux-5.15-source-audit"])
@pytest.mark.parametrize("effective", [False, True])
def test_kernel_profiles_require_distinct_exact_query_flags(profile, effective):
    kernel = KernelFixture()
    kernel.profile = profile
    instance = gate.OwnedNetworkGate(descriptor(), _kernel=kernel)
    instance._cgroup_fd = 40
    expected = 0 if profile == "linux-6.8" and effective else 2

    def correct(fd, hook, *, effective):
        return (0 if profile == "linux-6.8" and effective else 2), (500,)

    kernel.query = correct
    instance._query(10, (500,))

    def wrong(fd, hook, *, effective):
        current = 0 if profile == "linux-6.8" and effective else 2
        return (2 if expected == 0 else 0) if effective == test_effective else current, (500,)

    test_effective = effective
    kernel.query = wrong
    with pytest.raises(gate.GateError):
        instance._query(10, (500,))


def test_collected_cgroup_requires_eight_actual_errno_queries_and_no_detach():
    kernel = CollectedKernel()
    instance = gate.OwnedNetworkGate(descriptor(), _kernel=kernel)
    programs = instance.install()
    stop = instance.prepare_stop()
    assert stop["programs"] == programs and stop["profile"] == "linux-6.8"
    kernel.stopped = True
    result = instance.cleanup()
    assert result["complete"] is True and result["objects_released"] is True
    assert result["mode"] == "collected_offline" and result["detached_hooks"] == []
    assert result["offline_queries"] == [
        {"hook": hook, "effective": effective, "errno": 2}
        for hook in (10, 11, 14, 15)
        for effective in (False, True)
    ]
    assert not any(row[0] == "detach" for row in kernel.events)
    assert {row[1] for row in kernel.events if row[0] == "get_known_id"} == {500, 501, 502, 503}
    assert set(kernel.closed) == {40, 100, 101, 102, 103}


@pytest.mark.parametrize("query_error", [None, 0, 1, 5, 9, 13, 22])
def test_collected_query_success_or_non_enoent_never_becomes_empty_success(query_error):
    kernel = CollectedKernel()
    instance = gate.OwnedNetworkGate(descriptor(), _kernel=kernel)
    instance.install()
    instance.prepare_stop()
    kernel.stopped, kernel.query_error = True, query_error
    with pytest.raises(gate.GateError, match="cleanup_failed"):
        instance.cleanup()
    assert kernel.closed == [] and not any(row[0] == "detach" for row in kernel.events)
    assert instance.diagnostic()["stage"] == "offline_query"
    if query_error is not None:
        assert instance.diagnostic()["command"] == 16 and instance.diagnostic()["errno"] == query_error


def test_stop_witness_must_precede_stop_and_match_all_programs(monkeypatch):
    kernel = CollectedKernel()
    instance = gate.OwnedNetworkGate(descriptor(), _kernel=kernel)
    instance.install()
    kernel.stopped = True
    with pytest.raises(gate.GateError, match="cleanup_failed"):
        instance.cleanup()
    assert kernel.closed == []
    other = CollectedKernel()
    instance = gate.OwnedNetworkGate(descriptor(), _kernel=other)
    instance.install()
    instance.prepare_stop()
    other.stopped = True
    monkeypatch.setattr(gate, "_STOP_FRESH_NS", 0)
    with pytest.raises(gate.GateError, match="cleanup_failed"):
        instance.cleanup()
    assert other.closed == []


def test_async_object_release_closes_every_lookup_fd_and_only_polls_known_ids(monkeypatch):
    kernel = CollectedKernel()
    kernel.release_delay = 2
    instance = gate.OwnedNetworkGate(descriptor(), _kernel=kernel)
    instance.install()
    instance.prepare_stop()
    kernel.stopped = True
    monkeypatch.setattr(gate.time, "sleep", lambda _: None)
    result = instance.cleanup()
    assert result["objects_released"] is True
    assert len(kernel.lookup_fds) == 8
    assert all(fd in kernel.closed for fd in kernel.lookup_fds)
    assert all(count == 3 for count in kernel.lookup_counts.values())
    assert set(kernel.lookup_counts) == {500, 501, 502, 503}


@pytest.mark.parametrize("mutation", ["identity", "not_released", "wrong_errno"])
def test_release_timeout_changed_object_or_unexpected_errno_is_unknown_and_not_retried(monkeypatch, mutation):
    kernel = CollectedKernel()
    kernel.release_delay = 10 if mutation != "wrong_errno" else 0
    kernel.bad_lookup_info = mutation == "identity"
    kernel.release_error = 13 if mutation == "wrong_errno" else 2
    instance = gate.OwnedNetworkGate(descriptor(), _kernel=kernel)
    instance.install()
    instance.prepare_stop()
    kernel.stopped = True
    monkeypatch.setattr(gate, "_RELEASE_ROUNDS", 2)
    monkeypatch.setattr(gate.time, "sleep", lambda _: None)
    with pytest.raises(gate.GateError, match="cleanup_failed"):
        instance.cleanup()
    assert instance.state == "failed"
    assert all(fd in kernel.closed for fd in kernel.lookup_fds)
    assert 40 not in kernel.closed
    before = list(kernel.closed)
    with pytest.raises(gate.GateError):
        instance.cleanup()
    assert kernel.closed == before
    assert instance.diagnostic()["stage"] == "object_release"


def test_partial_four_loaded_but_failed_attach_is_not_complete_policy_cleanup():
    kernel = CollectedKernel()
    kernel.fail_attach = 15
    instance = gate.OwnedNetworkGate(descriptor(), _kernel=kernel)
    with pytest.raises(gate.GateError, match="install_failed"):
        instance.install()
    assert len(instance._programs) == 4
    kernel.stopped = True
    result = instance.cleanup()
    assert result["mode"] == "partial_closed" and result["complete"] is False
    assert result["objects_released"] is False and instance.state == "failed"
    assert set(kernel.closed) == {40, 100, 101, 102, 103}
    assert not kernel.lookup_counts and not any(row[0] == "detach" for row in kernel.events)


def test_syscall_errno_diagnostic_is_bounded_and_contains_no_raw_error_or_pointer():
    kernel = object.__new__(gate._LinuxKernel)

    def denied(*args):
        ctypes.set_errno(13)
        return -1

    kernel.libc = types.SimpleNamespace(syscall=denied)
    with pytest.raises(gate.GateError) as error:
        kernel._call(5, bytearray(128))
    assert gate.safe_diagnostic(error.value) == {"stage": "construct", "command": 5, "errno": 13}
    assert gate.safe_diagnostic(
        types.SimpleNamespace(stage="private/raw", command=12345, error_number="secret")
    ) == {
        "stage": "unknown",
        "command": None,
        "errno": None,
    }


def test_get_program_fd_uses_only_known_identifier_uapi_prefix_without_real_syscall():
    kernel = object.__new__(gate._LinuxKernel)

    def call(command, attr):
        assert command == 13 and struct.unpack_from("<I", attr)[0] == 501
        assert attr[4:] == bytes(124)
        return 71

    kernel._call = call
    assert kernel.open_program(501) == 71


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "live_pid",
        "reused_pid",
        "proc_permission",
        "path_permission",
        "path_symlink",
        "retained_inode",
        "retained_filesystem",
        "invocation",
        "exec_pid",
        "main_pid",
        "active",
        "group_not_cleared",
        "user",
        "missing_property",
        "duplicate_property",
    ],
)
def test_collected_identity_requires_actual_missing_path_pid_and_retained_cgroup2_fd(monkeypatch, mutation):
    config = descriptor()
    kernel = object.__new__(gate._LinuxKernel)
    observations = []

    def statfs(fd, buffer):
        assert fd == 40
        magic = 0 if mutation == "retained_filesystem" else 0x63677270
        ctypes.memmove(buffer, struct.pack("<q", magic), 8)
        return 0

    kernel.libc = types.SimpleNamespace(fstatfs=statfs)
    filesystem = types.SimpleNamespace(
        st_mode=0o40755,
        st_uid=0,
        st_ino=config.cgroup_inode + int(mutation == "retained_inode"),
        st_dev=config.cgroup_device,
    )

    def actual_stat(path, *, follow_symlinks):
        assert follow_symlinks is False
        observations.append(path)
        if path == "/proc/" + str(config.pid):
            if mutation in {"live_pid", "reused_pid"}:
                return filesystem
            if mutation == "proc_permission":
                raise PermissionError(13, "synthetic")
            raise FileNotFoundError(2, "synthetic")
        assert path == config.path
        if mutation == "path_permission":
            raise PermissionError(13, "synthetic")
        if mutation == "path_symlink":
            return types.SimpleNamespace(
                st_mode=0o120777, st_uid=0, st_ino=config.cgroup_inode, st_dev=config.cgroup_device
            )
        raise FileNotFoundError(2, "synthetic")

    monkeypatch.setattr(gate, "os", types.SimpleNamespace(fstat=lambda fd: filesystem, stat=actual_stat))
    monkeypatch.setattr(
        gate, "pwd", types.SimpleNamespace(getpwnam=lambda name: types.SimpleNamespace(pw_uid=config.uid))
    )
    state = {
        "Id": config.unit,
        "InvocationID": config.invocation_id,
        "MainPID": "0",
        "ExecMainPID": str(config.pid),
        "ControlGroup": "",
        "ActiveState": "inactive",
        "User": config.account,
    }
    replacements = {
        "invocation": ("InvocationID", "d" * 32),
        "exec_pid": ("ExecMainPID", "9999"),
        "main_pid": ("MainPID", str(config.pid)),
        "active": ("ActiveState", "active"),
        "group_not_cleared": ("ControlGroup", config.cgroup),
        "user": ("User", "root"),
    }
    if mutation in replacements:
        key, value = replacements[mutation]
        state[key] = value
    if mutation == "missing_property":
        state.pop("ExecMainPID")

    def show(argv, **kwargs):
        assert argv == [
            "/usr/bin/systemctl",
            "show",
            "--all",
            "--property=Id,InvocationID,MainPID,ExecMainPID,ControlGroup,ActiveState,User",
            config.unit,
        ]
        raw = "".join(f"{key}={value}\n" for key, value in state.items())
        if mutation == "duplicate_property":
            raw += "MainPID=0\n"
        return types.SimpleNamespace(stdout=raw.encode(), stderr=b"")

    monkeypatch.setattr(gate, "subprocess", types.SimpleNamespace(run=show))
    if mutation is None:
        assert kernel.check_identity(40, config, stopped=True) == "collected_offline"
        assert observations.count("/proc/" + str(config.pid)) >= 2
    else:
        with pytest.raises(gate.GateError):
            kernel.check_identity(40, config, stopped=True)


def test_known_id_cannot_reappear_after_enoent_even_with_identical_metadata(monkeypatch):
    kernel = CollectedKernel()
    kernel.release_delay = 2
    original = kernel.open_program

    def lookup(identifier):
        if identifier == 500 and not kernel.lookup_counts.get(identifier):
            kernel.lookup_counts[identifier] = 1
            raise gate.GateError("network_gate_syscall_failed", command=13, error_number=2)
        return original(identifier)

    kernel.open_program = lookup
    instance = gate.OwnedNetworkGate(descriptor(), _kernel=kernel)
    instance.install()
    instance.prepare_stop()
    kernel.stopped = True
    monkeypatch.setattr(gate.time, "sleep", lambda _: None)
    with pytest.raises(gate.GateError, match="cleanup_failed"):
        instance.cleanup()
    assert all(fd in kernel.closed for fd in kernel.lookup_fds)
    assert instance.state == "failed" and 40 not in kernel.closed


@pytest.mark.parametrize(
    ("release", "expected"),
    [
        ("5.15.0-164-generic", "linux-5.15-source-audit"),
        ("6.8.0-87-generic", "linux-6.8"),
        ("6.9.0", None),
        ("6.80.0", None),
        ("6.8x", None),
        ("5.150.0", None),
    ],
)
def test_only_exact_source_audited_kernel_profiles_are_selectable_without_syscalls(
    monkeypatch, release, expected
):
    monkeypatch.setattr(gate, "sys", types.SimpleNamespace(platform="linux", byteorder="little"))
    monkeypatch.setattr(
        gate, "platform", types.SimpleNamespace(machine=lambda: "x86_64", release=lambda: release)
    )
    monkeypatch.setattr(gate, "os", types.SimpleNamespace(geteuid=lambda: 0))

    def forbidden(*args):
        pytest.fail("constructor must never issue a BPF syscall")

    libc = types.SimpleNamespace(syscall=forbidden, fstatfs=lambda *args: 0)
    monkeypatch.setattr(gate.ctypes, "CDLL", lambda *args, **kwargs: libc)
    if expected is None:
        with pytest.raises(gate.GateError, match="unsupported"):
            gate._LinuxKernel()
    else:
        assert gate._LinuxKernel().profile == expected


def test_even_all_enoent_must_arrive_within_the_release_deadline(monkeypatch):
    kernel = CollectedKernel()
    instance = gate.OwnedNetworkGate(descriptor(), _kernel=kernel)
    instance.install()
    instance.prepare_stop()
    kernel.stopped = True
    clock = iter((1.0, 1.0, 7.0))
    monkeypatch.setattr(
        gate,
        "time",
        types.SimpleNamespace(
            monotonic=lambda: next(clock, 7.0), monotonic_ns=lambda: 1, sleep=lambda _: None
        ),
    )
    # Keep the existing witness inside its separate stop freshness window.
    old = instance._stop_witness
    instance._stop_witness = (old[0], old[1], 1)
    with pytest.raises(gate.GateError, match="cleanup_failed"):
        instance.cleanup()
    assert not kernel.lookup_counts and instance.diagnostic()["stage"] == "object_release"
