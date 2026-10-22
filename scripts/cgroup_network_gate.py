"""Candidate only: owned synthetic-unit connect/sendmsg gate, not wired to a controller.

No CLI, credential access, network probes, dependency installation, global BPF
pins, parent-cgroup changes, arbitrary bytecode loader, or caller-supplied FD.
The future reviewed controller must serialize ownership, hold the trusted
bootstrap after its authenticated Unix hello and BEFORE it creates any IP
socket, install(), verify IPv4/IPv6 negative canaries (strict EPERM/EACCES),
verify fixed DNS and pinned TLS, then verify() again BEFORE acquiring a key.
Retain existing systemd IPAddressDeny and the separate worker network namespace.
This helper does not certify those canaries or authorize any model request.

Linux 6.8 is the new audited profile; 5.15 remains source-audit compatibility.
Both require x86_64 / little-endian / root. Unsupported
operations fail closed. install failure retains owned descriptors: the caller
must stop its own unit, then cleanup(). Unknown identity/query results never
cause a sweep, a kill, a parent write, or a detach of an unknown program.
Cleanup requires a fresh exact four-program witness before the owned stop.
A collected cgroup requires the same stopped invocation, absent original PID,
matching retained cgroup2 FD and eight actual ENOENT query results. Offline
queries never pretend attachments are empty. Only the four known IDs are
polled after closing owned program FDs; ENOENT proves they are no longer
obtainable, not synchronous RCU memory freeing. Partial installs remain unknown.
The controller must still test this lifecycle on actual Linux.

Primary ABI/semantics (both fixed source versions, no installed BPF binding):
https://github.com/torvalds/linux/blob/v6.8/kernel/bpf/cgroup.c
  effective query flags=0; offline queries preserve ENOENT from the cgroup core.
https://github.com/torvalds/linux/blob/v6.8/kernel/cgroup/cgroup.c
  cgroup_get_from_fd/css_tryget_online reject an offline cgroup with ENOENT.
https://github.com/torvalds/linux/blob/v6.8/kernel/bpf/syscall.c
  exact BPF_PROG_GET_FD_BY_ID returns ENOENT after the final live reference.
https://github.com/torvalds/linux/blob/v6.8/include/uapi/linux/bpf.h
  the used 128-byte attributes and 80-byte program-info prefixes remain valid.
https://github.com/torvalds/linux/blob/v5.15/include/uapi/linux/bpf.h
  bpf_sock_addr offsets, enum IDs, bpf_attr, bpf_prog_info prefix.
https://github.com/torvalds/linux/blob/v5.15/kernel/bpf/cgroup.c
  __cgroup_bpf_run_filter_sock_addr returns EPERM for a rejected address.
https://github.com/torvalds/linux/blob/v5.15/net/ipv4/udp.c
  connected UDP can skip sendmsg hooks: connect MUST also check type/protocol.
https://github.com/torvalds/linux/blob/v5.15/arch/x86/entry/syscalls/syscall_64.tbl
  x86_64 bpf syscall 321.

bpf_prog_info in 5.15 has no expected_attach_type field. Hook binding is proved
by loading with that expected type, retaining the returned FD, reading its ID,
and querying that exact ID at that exact hook. Tags are stable metadata, not a
substitute for this producer binding. Offline tests are not a Linux receipt.
ALLOW_MULTI is used only for exact-FD detach: 5.15 find_detach_entry ignores the
FD with NONE/OVERRIDE. Both query modes must still show our single program;
unknown or inherited same-hook programs are rejected, never replaced.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import ipaddress
import os
import platform
import pwd
import re
import stat
import struct
import subprocess
import sys
import time
from dataclasses import asdict, dataclass

_HOOKS = (10, 11, 14, 15)  # INET4_CONNECT, INET6_CONNECT, UDP4_SENDMSG, UDP6_SENDMSG
_PROG_TYPE = 18  # BPF_PROG_TYPE_CGROUP_SOCK_ADDR
_CGROUP_ROOT = "/sys/fs/cgroup/system.slice"
_CGROUP2_MAGIC = 0x63677270
_RESOLVER = "127.0.0.53"
_ATTR_SIZE = 128  # v5.15 bpf_attr, through prog-load fd_array at offset 120.
_ALLOW_MULTI = 2  # Makes detach match the retained program FD, never "first program".
_PROFILES = frozenset({"linux-6.8", "linux-5.15-source-audit"})
_STOP_FRESH_NS = 45_000_000_000
_RELEASE_SECONDS = 5.0
_RELEASE_ROUNDS = 100
DIAGNOSTIC_STAGES = frozenset(
    {
        "not_started",
        "construct",
        "open_cgroup",
        "identity",
        "query_direct",
        "query_effective",
        "load",
        "info",
        "attach",
        "verify",
        "prepare_stop",
        "stop_identity",
        "offline_query",
        "detach",
        "close_owned",
        "object_release",
        "partial_cleanup",
        "complete",
        "unknown",
    }
)


class GateError(RuntimeError):
    """Fixed category plus bounded syscall metadata; never verifier logs/pointers."""

    def __init__(self, message, *, command=None, error_number=None):
        super().__init__(message)
        self.command = command if type(command) is int and command in {5, 8, 9, 13, 15, 16} else None
        self.error_number = error_number if type(error_number) is int and 0 <= error_number <= 4095 else None
        self.stage = "construct"


def safe_diagnostic(error):
    stage = getattr(error, "stage", "unknown")
    command, number = (
        getattr(error, "command", None),
        getattr(error, "error_number", getattr(error, "errno", None)),
    )
    return {
        "stage": stage if type(stage) is str and stage in DIAGNOSTIC_STAGES else "unknown",
        "command": command if type(command) is int and command in {5, 8, 9, 13, 15, 16} else None,
        "errno": number if type(number) is int and 0 <= number <= 4095 else None,
    }


def _require(value, message="network_gate_rejected"):
    if not value:
        raise GateError(message)


@dataclass(frozen=True)
class ControllerDescriptor:
    """Root-internal immutable input, built from the controller's actual identity.

    provider_ipv4 is the reviewed controller's already validated provider DNS
    result, not candidate/request/RPC input. It grants TCP 443 only. The market
    role has no DNS exception; live has the single fixed local resolver on 53.
    Unit/path are derived here, never accepted from an arbitrary caller string.
    """

    role: str
    candidate_prefix: str
    nonce: str
    invocation_id: str
    pid: int
    startticks: int
    uid: int
    cgroup_inode: int
    cgroup_device: int
    provider_ipv4: tuple[str, ...]

    def __post_init__(self):
        _require(type(self.role) is str and self.role in {"live", "market"})
        for value, length in (
            (self.candidate_prefix, 12),
            (self.nonce, 32),
            (self.invocation_id, 32),
        ):
            _require(type(value) is str and re.fullmatch(r"[0-9a-f]{" + str(length) + "}", value))
        for value in (self.pid, self.startticks, self.uid, self.cgroup_inode, self.cgroup_device):
            _require(type(value) is int and 0 < value < 2**63)
        _require(self.pid > 1 and type(self.provider_ipv4) is tuple and 1 <= len(self.provider_ipv4) <= 8)
        _require(all(type(value) is str for value in self.provider_ipv4))
        _require(len(set(self.provider_ipv4)) == len(self.provider_ipv4))
        for value in self.provider_ipv4:
            try:
                address = ipaddress.IPv4Address(value)
            except ipaddress.AddressValueError:
                raise GateError("network_gate_rejected") from None
            _require(
                str(address) == value
                and address.is_global
                and not address.is_multicast
                and not address.is_reserved
                and not address.is_unspecified
                and not address.is_loopback
                and not address.is_link_local
                and address not in ipaddress.IPv4Network("192.0.0.0/24")
            )

    @property
    def unit(self):
        prefix = "prism-live-broker" if self.role == "live" else "prism-market-probe"
        return f"{prefix}-{self.candidate_prefix}-{self.nonce}.service"

    @property
    def cgroup(self):
        return "/system.slice/" + self.unit

    @property
    def path(self):
        return _CGROUP_ROOT + "/" + self.unit

    @property
    def account(self):
        return "prism-integration-broker" if self.role == "live" else "prism-integration"


def _program(descriptor, hook):
    """Fixed no-map/no-helper address program. No caller-provided instructions.

    Loads are u32 at UAPI offsets: user_family=0, ip4=4, port=24,
    family=28, type=32, protocol=36. Network-order fields are compared using
    little-endian host representations of their network bytes. JEQ is JMP32:
    this avoids sign extension when an IPv4 immediate has its top bit set.
    """
    _require(type(descriptor) is ControllerDescriptor and type(hook) is int and hook in _HOOKS)
    # IPv6, including IPv4-mapped IPv6, is denied without reading its address.
    if hook in {11, 15} or (hook == 14 and descriptor.role == "market"):
        return struct.pack("<BBhiBBhi", 0xB7, 0, 0, 0, 0x95, 0, 0, 0)
    rows, labels, fixups = [], {}, []

    def emit(code, dst=0, src=0, offset=0, immediate=0):
        rows.append((code, dst | src << 4, offset, immediate))

    def load(offset):
        emit(0x61, 2, 1, offset)  # LDX W r2, [r1 + offset]

    def jump(equal, immediate, target):
        fixups.append((len(rows), target))
        emit(0x16 if equal else 0x56, 2, immediate=immediate)  # JMP32 JEQ/JNE K

    def mark(label):
        labels[label] = len(rows)

    load(0)
    jump(False, 2, "deny")  # user_family must be AF_INET
    load(28)
    jump(False, 2, "deny")  # socket family must also be AF_INET
    load(32)
    if hook == 14:
        jump(False, 2, "deny")  # SOCK_DGRAM
        load(36)
        jump(False, 17, "deny")  # IPPROTO_UDP
        jump(True, 17, "dns")
    else:
        jump(True, 1, "tcp")  # SOCK_STREAM
        if descriptor.role == "live":
            jump(False, 2, "deny")  # only DNS may use SOCK_DGRAM
            load(36)
            jump(False, 17, "deny")
            jump(True, 17, "dns")
        else:
            jump(False, 1, "deny")
        mark("tcp")
        load(36)
        jump(False, 6, "deny")  # IPPROTO_TCP
        load(24)
        jump(True, int.from_bytes(b"\x01\xbb", "little"), "provider")
        if descriptor.role == "live":
            jump(True, int.from_bytes(b"\x00\x35", "little"), "dns")
        jump(False, int.from_bytes(b"\x01\xbb", "little"), "deny")
        mark("provider")
        load(4)
        for value in descriptor.provider_ipv4:
            jump(True, int.from_bytes(ipaddress.IPv4Address(value).packed, "little"), "allow")
        # Last failed IP comparison always jumps to deny.
        jump(
            False,
            int.from_bytes(ipaddress.IPv4Address(descriptor.provider_ipv4[-1]).packed, "little"),
            "deny",
        )
    if descriptor.role == "live":
        mark("dns")
        load(24)
        jump(False, int.from_bytes(b"\x00\x35", "little"), "deny")
        load(4)
        jump(False, int.from_bytes(ipaddress.IPv4Address(_RESOLVER).packed, "little"), "deny")
        jump(True, int.from_bytes(ipaddress.IPv4Address(_RESOLVER).packed, "little"), "allow")
    mark("deny")
    emit(0xB7, immediate=0)
    emit(0x95)
    mark("allow")
    emit(0xB7, immediate=1)
    emit(0x95)
    for index, label in fixups:
        code, registers, _, immediate = rows[index]
        rows[index] = code, registers, labels[label] - index - 1, immediate
    return b"".join(
        struct.pack("<BBhi", code, registers, offset, ctypes.c_int32(immediate).value)
        for code, registers, offset, immediate in rows
    )


@dataclass(frozen=True)
class _ProgramInfo:
    identifier: int
    tag: bytes
    kind: int
    maps: int
    creator_uid: int


@dataclass(frozen=True)
class _OwnedProgram:
    hook: int
    fd: int
    info: _ProgramInfo | None
    producer_sha256: str


class _LinuxKernel:
    """Private fixed adapter; no arbitrary command, FD, path or bytecode API."""

    def __init__(self):
        release = platform.release()
        _require(
            sys.platform == "linux"
            and platform.machine() == "x86_64"
            and sys.byteorder == "little"
            and struct.calcsize("P") == 8
            and os.geteuid() == 0
            and re.match(r"(?:5\.15|6\.8)\.", release),
            "network_gate_unsupported",
        )
        self.profile = "linux-6.8" if release.startswith("6.8.") else "linux-5.15-source-audit"
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.syscall.restype = ctypes.c_long
        self.libc.fstatfs.argtypes = [ctypes.c_int, ctypes.c_void_p]
        self.libc.fstatfs.restype = ctypes.c_int

    def _call(self, command, attr):
        _require(
            type(command) is int
            and command in {5, 8, 9, 13, 15, 16}
            and type(attr) is bytearray
            and len(attr) == _ATTR_SIZE
        )
        buffer = (ctypes.c_ubyte * len(attr)).from_buffer(attr)
        ctypes.set_errno(0)
        result = self.libc.syscall(321, command, ctypes.byref(buffer), len(attr))
        if result < 0:
            raise GateError("network_gate_syscall_failed", command=command, error_number=ctypes.get_errno())
        return result

    def open_cgroup(self, descriptor):
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for component in ("sys", "fs", "cgroup", "system.slice", descriptor.unit):
                next_fd = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=fd
                )
                os.close(fd)
                fd = next_fd
                info = os.fstat(fd)
                _require(info.st_uid == 0 and not info.st_mode & 0o022)
            self._check_cgroup(fd, descriptor)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _check_cgroup(self, fd, descriptor):
        info = os.fstat(fd)
        _require(
            stat.S_ISDIR(info.st_mode)
            and info.st_uid == 0
            and info.st_ino == descriptor.cgroup_inode
            and info.st_dev == descriptor.cgroup_device
        )
        filesystem = ctypes.create_string_buffer(256)
        _require(self.libc.fstatfs(fd, filesystem) == 0)
        _require(struct.unpack_from("<q", filesystem.raw)[0] == _CGROUP2_MAGIC)

    @staticmethod
    def _read_at(fd, filename):
        child = os.open(filename, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=fd)
        try:
            raw = os.read(child, 8193)
            _require(len(raw) <= 8192)
            return raw.decode("ascii")
        finally:
            os.close(child)

    @staticmethod
    def _process_absent(descriptor):
        try:
            os.stat("/proc/" + str(descriptor.pid), follow_symlinks=False)
        except OSError as error:
            _require(error.errno == errno.ENOENT)
            return
        # A live original or a reused PID is unknown, never proof of shutdown.
        raise GateError("network_gate_rejected")

    def check_identity(self, fd, descriptor, *, stopped):
        self._check_cgroup(fd, descriptor)
        account = pwd.getpwnam(descriptor.account)
        _require(account.pw_uid == descriptor.uid and account.pw_uid != 0)
        result = subprocess.run(
            [
                "/usr/bin/systemctl",
                "show",
                "--all",
                "--property=Id,InvocationID,MainPID,ExecMainPID,ControlGroup,ActiveState,User",
                descriptor.unit,
            ],
            check=True,
            capture_output=True,
            timeout=3,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        )
        _require(len(result.stdout) <= 8192 and not result.stderr)
        lines = result.stdout.decode("ascii").splitlines()
        pairs = [line.split("=", 1) for line in lines]
        _require(all(len(row) == 2 for row in pairs))
        state = dict(pairs)
        _require(
            len(state) == len(pairs)
            and set(state)
            == {"Id", "InvocationID", "MainPID", "ExecMainPID", "ControlGroup", "ActiveState", "User"}
            and state["Id"] == descriptor.unit
            and state["InvocationID"] == descriptor.invocation_id
            and state["ExecMainPID"] == str(descriptor.pid)
            and state["User"] == descriptor.account
        )
        if stopped:
            _require(state["ActiveState"] in {"inactive", "failed"} and state["MainPID"] == "0")
            self._process_absent(descriptor)
        try:
            current = os.stat(descriptor.path, follow_symlinks=False)
        except OSError as error:
            _require(stopped and error.errno == errno.ENOENT and state["ControlGroup"] == "")
            self._check_cgroup(fd, descriptor)
            self._process_absent(descriptor)
            return "collected_offline"
        _require(
            stat.S_ISDIR(current.st_mode)
            and current.st_uid == 0
            and (current.st_dev, current.st_ino) == (descriptor.cgroup_device, descriptor.cgroup_inode)
            and state["ControlGroup"] == descriptor.cgroup
        )
        if stopped:
            events = dict(line.split() for line in self._read_at(fd, "cgroup.events").splitlines())
            _require(events.get("populated") == "0" and not self._read_at(fd, "cgroup.procs").strip())
            self._process_absent(descriptor)
            return "retained_empty"
        _require(state["ActiveState"] == "active" and state["MainPID"] == str(descriptor.pid))
        process = os.open(
            "/proc/" + str(descriptor.pid), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        )
        try:
            status = self._read_at(process, "status")
            uid_lines = [line.split()[1:] for line in status.splitlines() if line.startswith("Uid:")]
            _require(uid_lines == [[str(descriptor.uid)] * 4])
            _require(self._read_at(process, "cgroup").strip() == "0::" + descriptor.cgroup)
            proc_stat = self._read_at(process, "stat")
            _require(int(proc_stat[proc_stat.rindex(")") + 2 :].split()[19]) == descriptor.startticks)
        finally:
            os.close(process)
        return "running"

    def open_program(self, identifier):
        _require(type(identifier) is int and 0 < identifier < 2**32)
        attr = bytearray(_ATTR_SIZE)
        struct.pack_into("<I", attr, 0, identifier)
        return self._call(13, attr)  # BPF_PROG_GET_FD_BY_ID, exactly this known ID only.

    def query(self, fd, hook, *, effective):
        ids = (ctypes.c_uint32 * 64)()
        attr = bytearray(_ATTR_SIZE)
        struct.pack_into("<IIIIQI", attr, 0, fd, hook, int(effective), 0, ctypes.addressof(ids), 64)
        self._call(16, attr)  # BPF_PROG_QUERY; BPF_F_QUERY_EFFECTIVE = 1.
        flags = struct.unpack_from("<I", attr, 12)[0]
        count = struct.unpack_from("<I", attr, 24)[0]
        _require(count <= 64)
        return flags, tuple(ids[:count])

    def load(self, descriptor, hook):
        raw = _program(descriptor, hook)
        instructions = ctypes.create_string_buffer(raw)
        license_value = ctypes.create_string_buffer(b"GPL\0")
        attr = bytearray(_ATTR_SIZE)
        struct.pack_into(
            "<IIQQ",
            attr,
            0,
            _PROG_TYPE,
            len(raw) // 8,
            ctypes.addressof(instructions),
            ctypes.addressof(license_value),
        )
        struct.pack_into("<I", attr, 68, hook)  # expected_attach_type, no guessed info-field counterpart.
        name = ("prism_gate_" + str(hook)).encode("ascii")
        attr[48 : 48 + len(name)] = name
        return self._call(5, attr)  # BPF_PROG_LOAD; flags=0, no maps/helpers/BTF/logs.

    def info(self, fd):
        # The first 80 bytes are a stable v5.15 prefix through name[16].
        info = ctypes.create_string_buffer(80)
        attr = bytearray(_ATTR_SIZE)
        struct.pack_into("<IIQ", attr, 0, fd, 80, ctypes.addressof(info))
        self._call(15, attr)  # BPF_OBJ_GET_INFO_BY_FD
        _require(struct.unpack_from("<I", attr, 4)[0] >= 80)
        raw = info.raw
        kind, identifier = struct.unpack_from("<II", raw)
        creator_uid, maps = struct.unpack_from("<II", raw, 48)
        return _ProgramInfo(identifier, raw[8:16], kind, maps, creator_uid)

    def attach(self, cgroup_fd, hook, program_fd):
        attr = bytearray(_ATTR_SIZE)
        struct.pack_into("<IIIII", attr, 0, cgroup_fd, program_fd, hook, _ALLOW_MULTI, 0)
        self._call(8, attr)  # BPF_PROG_ATTACH, no override/replace; exact-ID queries still require singleton.

    def detach(self, cgroup_fd, hook, program_fd):
        attr = bytearray(_ATTR_SIZE)
        struct.pack_into("<IIIII", attr, 0, cgroup_fd, program_fd, hook, 0, 0)
        self._call(9, attr)  # BPF_PROG_DETACH, own retained FD only.

    @staticmethod
    def close(fd):
        os.close(fd)


class OwnedNetworkGate:
    """Single-use controller object. Keep it alive through owned-unit shutdown.

    _kernel exists solely for offline fixtures. Production callers use the
    default, which independently checks systemd/proc/cgroup identity. No public
    result produced here says actual canaries/TLS or credentials are approved.
    """

    def __init__(self, descriptor, *, _kernel=None):
        _require(type(descriptor) is ControllerDescriptor)
        self.descriptor = descriptor
        self._kernel = _LinuxKernel() if _kernel is None else _kernel
        self._cgroup_fd = None
        self._programs = []
        self._close_error = False
        self._stop_witness = None
        self._installed_verified = False
        self._stage = "construct"
        self._diagnostic = {"stage": "not_started", "command": None, "errno": None}
        _require(self._kernel.profile in _PROFILES)
        self.state = "new"

    def _mark(self, stage):
        _require(stage in DIAGNOSTIC_STAGES)
        self._stage = stage
        self._diagnostic = {"stage": stage, "command": None, "errno": None}

    def _capture(self, error):
        self._diagnostic = safe_diagnostic(error)
        self._diagnostic["stage"] = self._stage

    def diagnostic(self):
        return dict(self._diagnostic)

    def _identity(self, *, stopped=False):
        self._mark("stop_identity" if stopped else "identity")
        return self._kernel.check_identity(self._cgroup_fd, self.descriptor, stopped=stopped)

    def _query(self, hook, expected):
        for effective in (False, True):
            self._mark("query_effective" if effective else "query_direct")
            flags, identifiers = self._kernel.query(self._cgroup_fd, hook, effective=effective)
            expected_flags = (
                _ALLOW_MULTI if expected and not (effective and self._kernel.profile == "linux-6.8") else 0
            )
            _require(type(flags) is int and flags == expected_flags and identifiers == expected)

    @staticmethod
    def _valid_info(info):
        _require(
            type(info) is _ProgramInfo
            and type(info.identifier) is int
            and 0 < info.identifier < 2**32
            and type(info.tag) is bytes
            and len(info.tag) == 8
            and type(info.kind) is int
            and info.kind == _PROG_TYPE
            and type(info.maps) is int
            and info.maps == 0
            and type(info.creator_uid) is int
            and info.creator_uid == 0
        )

    def install(self):
        _require(self.state == "new")
        self.state = "installing"
        try:
            self._mark("open_cgroup")
            self._cgroup_fd = self._kernel.open_cgroup(self.descriptor)
            self._identity()
            for hook in _HOOKS:
                self._query(hook, ())
            for hook in _HOOKS:
                self._identity()
                self._query(hook, ())
                self._mark("load")
                fd = self._kernel.load(self.descriptor, hook)
                # Preserve every returned FD even if info/readback subsequently fails.
                record = _OwnedProgram(
                    hook, fd, None, hashlib.sha256(_program(self.descriptor, hook)).hexdigest()
                )
                self._programs.append(record)
                self._mark("info")
                info = self._kernel.info(fd)
                self._valid_info(info)
                _require(info.identifier not in {item.info.identifier for item in self._programs[:-1]})
                record = _OwnedProgram(hook, fd, info, record.producer_sha256)
                self._programs[-1] = record
                self._identity()
                self._mark("attach")
                self._kernel.attach(self._cgroup_fd, hook, fd)
                self._query(hook, (info.identifier,))
            self.state = "installed"
            result = self.verify()
            self._installed_verified = True
            return result
        except BaseException as error:
            self._capture(error)
            self.state = "failed"
            if not isinstance(error, Exception):
                raise
            raise GateError("network_gate_install_failed") from None

    def verify(self):
        _require(self.state == "installed" and len(self._programs) == 4)
        try:
            self._identity()
            for record in self._programs:
                self._mark("info")
                _require(self._kernel.info(record.fd) == record.info)
                self._query(record.hook, (record.info.identifier,))
            self._identity()
        except BaseException as error:
            self._capture(error)
            self.state = "failed"
            if not isinstance(error, Exception):
                raise
            raise GateError("network_gate_verification_failed") from None
        return tuple(
            (record.hook, record.info.identifier, record.info.tag.hex(), record.producer_sha256)
            for record in self._programs
        )

    def prepare_stop(self):
        """Fresh active identity + all four exact programs before the controller stops its unit."""
        self._mark("prepare_stop")
        programs = self.verify()
        checked = time.monotonic_ns()
        self._stop_witness = (self.descriptor, tuple(self._programs), checked)
        return {
            "schema": 1,
            "profile": self._kernel.profile,
            "identity": asdict(self.descriptor),
            "programs": programs,
            "verified_at_ns": checked,
        }

    def _offline_queries(self):
        results = []
        self._mark("offline_query")
        for hook in _HOOKS:
            for effective in (False, True):
                try:
                    self._kernel.query(self._cgroup_fd, hook, effective=effective)
                except GateError as error:
                    if error.command != 16 or error.error_number != errno.ENOENT:
                        raise
                    results.append({"hook": hook, "effective": effective, "errno": errno.ENOENT})
                else:
                    raise GateError("network_gate_offline_query_rejected")
        return results

    def _objects_released(self, records):
        """Only these immutable known IDs. ENOENT is not an RCU memory-free claim."""
        deadline, seen_gone = time.monotonic() + _RELEASE_SECONDS, set()
        for _ in range(_RELEASE_ROUNDS):
            self._mark("object_release")
            _require(time.monotonic() < deadline)
            gone = set()
            for record in records:
                _require(time.monotonic() < deadline)
                try:
                    fd = self._kernel.open_program(record.info.identifier)
                except GateError as error:
                    if error.command != 13 or error.error_number != errno.ENOENT:
                        raise
                    gone.add(record.info.identifier)
                else:
                    try:
                        _require(record.info.identifier not in seen_gone and fd != self._cgroup_fd)
                        _require(self._kernel.info(fd) == record.info)
                    finally:
                        self._kernel.close(fd)
            seen_gone |= gone
            if len(gone) == 4:
                _require(time.monotonic() < deadline)
                return True
            _require(time.monotonic() < deadline)
            time.sleep(0.05)
        raise GateError("network_gate_object_release_unknown")

    def cleanup(self):
        _require(self.state in {"installed", "failed", "cleaning"} and not self._close_error)
        if self._cgroup_fd is None:
            _require(not self._programs)
            self.state = "cleaned"
            return {
                "schema": 1,
                "mode": "partial_closed",
                "complete": False,
                "objects_released": False,
                "program_count": 0,
                "programs": [],
                "offline_queries": [],
                "detached_hooks": [],
                "stop_verified_at_ns": None,
                "profile": self._kernel.profile,
            }
        self.state = "cleaning"
        try:
            mode = self._identity(stopped=True)
            _require(mode in {"retained_empty", "collected_offline"})
            records = tuple(self._programs)
            complete = (
                self._installed_verified
                and len(records) == 4
                and tuple(row.hook for row in records) == _HOOKS
                and all(row.info is not None for row in records)
            )
            stop_time = None
            if complete:
                _require(self._stop_witness is not None)
                identity, witnessed, stop_time = self._stop_witness
                _require(
                    identity == self.descriptor
                    and witnessed == records
                    and 0 <= time.monotonic_ns() - stop_time <= _STOP_FRESH_NS
                )
            for row in records:
                self._mark("info")
                info = self._kernel.info(row.fd)
                self._valid_info(info)
                _require(row.info is None or info == row.info)
                _require(
                    row.producer_sha256 == hashlib.sha256(_program(self.descriptor, row.hook)).hexdigest()
                )
            offline, detached = [], []
            if mode == "collected_offline":
                offline = self._offline_queries()
            elif complete:
                attached = {}
                for row in records:
                    self._mark("query_direct")
                    flags, ids = self._kernel.query(self._cgroup_fd, row.hook, effective=False)
                    _require(ids in {(), (row.info.identifier,)} and flags == (_ALLOW_MULTI if ids else 0))
                    self._query(row.hook, ids)
                    attached[row.hook] = ids
                for row in records:
                    mode = self._identity(stopped=True)
                    if mode == "collected_offline":
                        offline = self._offline_queries()
                        break
                    self._query(row.hook, attached[row.hook])
                    if attached[row.hook]:
                        self._mark("detach")
                        self._kernel.detach(self._cgroup_fd, row.hook, row.fd)
                        detached.append(row.hook)
                    self._query(row.hook, ())
            # Partial install cannot certify four-hook cleanup or detach a partial policy.
            self._close_error = True
            self._mark("close_owned")
            while self._programs:
                row = self._programs.pop()
                info = self._kernel.info(row.fd)
                self._valid_info(info)
                _require(row.info is None or info == row.info)
                self._kernel.close(row.fd)
            final_mode = self._identity(stopped=True)
            if final_mode == "collected_offline" and not offline:
                offline = self._offline_queries()
            _require(mode != "collected_offline" or final_mode == "collected_offline")
            released = self._objects_released(records) if complete else False
            after_release = self._identity(stopped=True)
            if after_release == "collected_offline" and not offline:
                offline = self._offline_queries()
            _require(final_mode != "collected_offline" or after_release == "collected_offline")
            self._mark("close_owned")
            fd = self._cgroup_fd
            self._cgroup_fd = None
            self._kernel.close(fd)
            self._close_error = False
            self.state = "cleaned" if complete and released else "failed"
            self._mark("complete" if complete and released else "partial_cleanup")
            return {
                "schema": 1,
                "mode": after_release if complete else "partial_closed",
                "complete": complete,
                "objects_released": released,
                "program_count": len(records),
                "programs": [
                    (row.hook, row.info.identifier if row.info else None, row.producer_sha256)
                    for row in records
                ],
                "offline_queries": offline,
                "detached_hooks": detached,
                "stop_verified_at_ns": stop_time,
                "profile": self._kernel.profile,
            }
        except BaseException as error:
            self._capture(error)
            self.state = "failed"
            if not isinstance(error, Exception):
                raise
            raise GateError("network_gate_cleanup_failed") from None
