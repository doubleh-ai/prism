"""Candidate only: owned synthetic-unit connect/sendmsg gate, not wired to a controller.

No CLI, credential access, network probes, dependency installation, global BPF
pins, parent-cgroup changes, arbitrary bytecode loader, or caller-supplied FD.
The future reviewed controller must serialize ownership, hold the trusted
bootstrap after its authenticated Unix hello and BEFORE it creates any IP
socket, install(), verify IPv4/IPv6 negative canaries (strict EPERM/EACCES),
verify fixed DNS and pinned TLS, then verify() again BEFORE acquiring a key.
Retain existing systemd IPAddressDeny and the separate worker network namespace.
This helper does not certify those canaries or authorize any model request.

Only Linux 5.15 / x86_64 / little-endian / root is implemented. Unsupported
operations fail closed. install failure retains owned descriptors: the caller
must stop its own unit, then cleanup(). Unknown identity/query results never
cause a sweep, a kill, a parent write, or a detach of an unknown program.
cleanup requires the same unit/invocation and retained cgroup to remain
inspectable after stop; a missing/collected cgroup is deliberately not guessed
to be a successful cleanup. The adapter must test that lifecycle on Linux.

Primary ABI/semantics (v5.15, not inferred from modern bindings):
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
from dataclasses import dataclass

_HOOKS = (10, 11, 14, 15)  # INET4_CONNECT, INET6_CONNECT, UDP4_SENDMSG, UDP6_SENDMSG
_PROG_TYPE = 18  # BPF_PROG_TYPE_CGROUP_SOCK_ADDR
_CGROUP_ROOT = "/sys/fs/cgroup/system.slice"
_CGROUP2_MAGIC = 0x63677270
_RESOLVER = "127.0.0.53"
_ATTR_SIZE = 128  # v5.15 bpf_attr, through prog-load fd_array at offset 120.
_ALLOW_MULTI = 2  # Makes detach match the retained program FD, never "first program".


class GateError(RuntimeError):
    """Fixed, non-secret failure category; no raw syscall/output details."""


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
        _require(
            sys.platform == "linux"
            and platform.machine() == "x86_64"
            and sys.byteorder == "little"
            and struct.calcsize("P") == 8
            and os.geteuid() == 0
            and re.match(r"5\.15\.", platform.release()),
            "network_gate_unsupported",
        )
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.syscall.restype = ctypes.c_long
        self.libc.fstatfs.argtypes = [ctypes.c_int, ctypes.c_void_p]
        self.libc.fstatfs.restype = ctypes.c_int

    def _call(self, command, attr):
        _require(type(attr) is bytearray and len(attr) == _ATTR_SIZE)
        buffer = (ctypes.c_ubyte * len(attr)).from_buffer(attr)
        result = self.libc.syscall(321, command, ctypes.byref(buffer), len(attr))
        if result < 0:
            raise GateError("network_gate_syscall_failed")
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

    def check_identity(self, fd, descriptor, *, stopped):
        self._check_cgroup(fd, descriptor)
        current = os.stat(descriptor.path, follow_symlinks=False)
        _require((current.st_dev, current.st_ino) == (descriptor.cgroup_device, descriptor.cgroup_inode))
        account = pwd.getpwnam(descriptor.account)
        _require(account.pw_uid == descriptor.uid and account.pw_uid != 0)
        result = subprocess.run(
            [
                "/usr/bin/systemctl",
                "show",
                "--property=Id,InvocationID,MainPID,ControlGroup,ActiveState,User",
                descriptor.unit,
            ],
            check=True,
            capture_output=True,
            timeout=3,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        )
        _require(len(result.stdout) <= 8192 and not result.stderr)
        state = dict(line.split("=", 1) for line in result.stdout.decode("ascii").splitlines())
        _require(
            set(state) == {"Id", "InvocationID", "MainPID", "ControlGroup", "ActiveState", "User"}
            and state["Id"] == descriptor.unit
            and state["InvocationID"] == descriptor.invocation_id
            and state["ControlGroup"] == descriptor.cgroup
            and state["User"] == descriptor.account
        )
        if stopped:
            _require(state["ActiveState"] in {"inactive", "failed"} and state["MainPID"] == "0")
            events = dict(line.split() for line in self._read_at(fd, "cgroup.events").splitlines())
            _require(events.get("populated") == "0" and not self._read_at(fd, "cgroup.procs").strip())
            return
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
        self.state = "new"

    def _identity(self, *, stopped=False):
        self._kernel.check_identity(self._cgroup_fd, self.descriptor, stopped=stopped)

    def _query(self, hook, expected):
        for effective in (False, True):
            flags, identifiers = self._kernel.query(self._cgroup_fd, hook, effective=effective)
            _require(
                type(flags) is int and flags == (_ALLOW_MULTI if expected else 0) and identifiers == expected
            )

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
            self._cgroup_fd = self._kernel.open_cgroup(self.descriptor)
            self._identity()
            for hook in _HOOKS:
                self._query(hook, ())
            for hook in _HOOKS:
                self._identity()
                self._query(hook, ())
                fd = self._kernel.load(self.descriptor, hook)
                # Preserve every returned FD even if info/readback subsequently fails.
                record = _OwnedProgram(
                    hook, fd, None, hashlib.sha256(_program(self.descriptor, hook)).hexdigest()
                )
                self._programs.append(record)
                info = self._kernel.info(fd)
                self._valid_info(info)
                _require(info.identifier not in {item.info.identifier for item in self._programs[:-1]})
                record = _OwnedProgram(hook, fd, info, record.producer_sha256)
                self._programs[-1] = record
                self._identity()
                self._kernel.attach(self._cgroup_fd, hook, fd)
                self._query(hook, (info.identifier,))
            self.state = "installed"
            return self.verify()
        except BaseException as error:
            self.state = "failed"
            if not isinstance(error, Exception):
                raise
            raise GateError("network_gate_install_failed") from None

    def verify(self):
        _require(self.state == "installed" and len(self._programs) == 4)
        try:
            self._identity()
            for record in self._programs:
                _require(self._kernel.info(record.fd) == record.info)
                self._query(record.hook, (record.info.identifier,))
            self._identity()
        except BaseException as error:
            self.state = "failed"
            if not isinstance(error, Exception):
                raise
            raise GateError("network_gate_verification_failed") from None
        return tuple(
            (record.hook, record.info.identifier, record.info.tag.hex(), record.producer_sha256)
            for record in self._programs
        )

    def cleanup(self):
        _require(self.state in {"installed", "failed", "cleaning"} and not self._close_error)
        if self._cgroup_fd is None:
            _require(not self._programs)
            self.state = "cleaned"
            return
        self.state = "cleaning"
        try:
            self._identity(stopped=True)
            by_hook = {record.hook: record for record in self._programs}
            # Establish the whole ownership set BEFORE any detach.
            attached = {}
            for hook in _HOOKS:
                record = by_hook.get(hook)
                expected = ()
                if record is not None and record.info is not None:
                    _require(self._kernel.info(record.fd) == record.info)
                    self._valid_info(record.info)
                    flags, ids = self._kernel.query(self._cgroup_fd, hook, effective=False)
                    _require(ids in {(), (record.info.identifier,)} and flags == (_ALLOW_MULTI if ids else 0))
                    expected = ids
                self._query(hook, expected)
                attached[hook] = expected
            for hook in _HOOKS:
                self._identity(stopped=True)
                self._query(hook, attached[hook])
                if attached[hook]:
                    self._kernel.detach(self._cgroup_fd, hook, by_hook[hook].fd)
                self._query(hook, ())
            self._identity(stopped=True)
            for hook in _HOOKS:
                self._query(hook, ())
            # Do not retry an uncertain close: the number could have been reused.
            self._close_error = True
            while self._programs:
                self._kernel.close(self._programs.pop().fd)
            fd = self._cgroup_fd
            self._cgroup_fd = None
            self._kernel.close(fd)
            self._close_error = False
            self.state = "cleaned"
        except BaseException as error:
            self.state = "failed"
            if not isinstance(error, Exception):
                raise
            raise GateError("network_gate_cleanup_failed") from None
