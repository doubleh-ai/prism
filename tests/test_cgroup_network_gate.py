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
    assert instance.state == "cleaned"
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
    instance.cleanup()
    assert instance.state == "cleaned"


def test_uncertain_close_is_never_retried_on_possibly_reused_descriptor():
    kernel = KernelFixture()
    instance = gate.OwnedNetworkGate(descriptor(), _kernel=kernel)
    instance.install()
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
