"""Fixed root-only, persistent system-bus references for synthetic test units.

The controller must independently prove the already running unit's complete
identity before acquire(), and retain this object until stop/kernel evidence is
persisted. RefUnit is not a start operation, stop proof, or BPF cleanup proof.
There is no CLI, caller-selected bus/library/method, reconnect, or retry.

Primary v249 ABI/semantics (not actual Linux acceptance):
https://github.com/systemd/systemd/blob/v249/src/systemd/sd-bus.h
https://github.com/systemd/systemd/blob/v249/src/core/dbus-manager.c#L687-L695
https://github.com/systemd/systemd/blob/v249/src/core/dbus-unit.c#L577
https://github.com/systemd/systemd/blob/v249/src/libsystemd/sd-bus/bus-convenience.c#L334-L375
https://github.com/systemd/systemd/blob/v249/src/core/unit.c#L343-L405
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import re
import stat
import struct
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_LIBRARY = Path("/usr/lib/x86_64-linux-gnu/libsystemd.so.0")
_SOCKET = Path("/run/dbus/system_bus_socket")
_ADDRESS = b"unix:path=/run/dbus/system_bus_socket"
_DESTINATION = b"org.freedesktop.systemd1"
_MANAGER_PATH = b"/org/freedesktop/systemd1"
_MANAGER_INTERFACE = b"org.freedesktop.systemd1.Manager"
_UNIT_INTERFACE = b"org.freedesktop.systemd1.Unit"
_SERVICE_INTERFACE = b"org.freedesktop.systemd1.Service"
_PROC = Path("/proc")
_UNIQUE = re.compile(r":[0-9]{1,20}\.[0-9]{1,20}")
_ERRORS = frozenset(
    {
        "reference_platform_rejected",
        "reference_library_rejected",
        "reference_bus_rejected",
        "reference_identity_rejected",
        "reference_refs_rejected",
        "reference_state_rejected",
        "reference_call_failed",
        "reference_close_failed",
    }
)


class UnitReferenceError(RuntimeError):
    """Only a fixed category and optional bounded errno; no bus error text."""

    def __init__(self, category, error_number=None):
        self.category = category if type(category) is str and category in _ERRORS else "reference_call_failed"
        self.error_number = error_number if type(error_number) is int and 0 <= error_number <= 4095 else None
        super().__init__(self.category)


def _require(condition, category="reference_bus_rejected"):
    if not condition:
        raise UnitReferenceError(category)


@dataclass(frozen=True)
class ExpectedIdentity:
    """Trusted controller facts, never request/agent supplied identity claims."""

    role: str
    candidate_prefix: str
    nonce: str
    invocation_id: str
    pid: int
    startticks: int
    uid: int

    def __post_init__(self):
        _require(
            type(self.role) is str and self.role in {"live", "live-worker", "market"},
            "reference_identity_rejected",
        )
        for value, length in ((self.candidate_prefix, 12), (self.nonce, 32), (self.invocation_id, 32)):
            _require(
                type(value) is str and re.fullmatch("[0-9a-f]{" + str(length) + "}", value),
                "reference_identity_rejected",
            )
        for value in (self.pid, self.startticks, self.uid):
            _require(type(value) is int and 0 < value < 2**63, "reference_identity_rejected")
        _require(self.pid > 1, "reference_identity_rejected")

    @property
    def unit(self):
        prefix = {
            "live": "prism-live-broker",
            "live-worker": "prism-live-worker",
            "market": "prism-market-probe",
        }[self.role]
        return f"{prefix}-{self.candidate_prefix}-{self.nonce}.service"

    @property
    def object_path(self):
        # All non-alphanumeric ASCII bytes in these fixed names use _xx.
        encoded = "".join(
            chr(value) if chr(value).isalnum() else "_" + format(value, "02x")
            for value in self.unit.encode("ascii")
        )
        return "/org/freedesktop/systemd1/unit/" + encoded


class _BusError(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char_p), ("message", ctypes.c_char_p), ("need_free", ctypes.c_int)]


class _MissingUnit(UnitReferenceError):
    def __init__(self):
        super().__init__("reference_identity_rejected")


class _MissingObject(UnitReferenceError):
    def __init__(self):
        super().__init__("reference_refs_rejected")


def _locked_path(path, *, directory):
    info = path.lstat()
    _require(
        (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
        and info.st_uid == 0
        and not info.st_mode & 0o022,
        "reference_library_rejected",
    )
    return info


def _trusted_library():
    _require(
        sys.platform == "linux"
        and platform.machine() == "x86_64"
        and sys.byteorder == "little"
        and struct.calcsize("P") == 8
        and os.geteuid() == 0
        and not any(name.startswith(("LD_", "DYLD_")) for name in os.environ),
        "reference_platform_rejected",
    )
    fd = None
    try:
        # Ubuntu's fixed SONAME is a root-owned symlink; validate the complete
        # canonical target path, then dlopen its still-open stable descriptor.
        for parent in (*reversed(_LIBRARY.parents),):
            _locked_path(parent, directory=True)
        link = _LIBRARY.lstat()
        _require(
            link.st_uid == 0 and (stat.S_ISLNK(link.st_mode) or stat.S_ISREG(link.st_mode)),
            "reference_library_rejected",
        )
        target = _LIBRARY.resolve(strict=True)
        _require(
            target.parent == _LIBRARY.parent and re.fullmatch(r"libsystemd\.so\.0(?:\.[0-9]+)*", target.name),
            "reference_library_rejected",
        )
        for parent in reversed(target.parents):
            _locked_path(parent, directory=True)
        expected = _locked_path(target, directory=False)
        fd = os.open(target, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        actual = os.fstat(fd)
        _require(
            (actual.st_dev, actual.st_ino, actual.st_size)
            == (expected.st_dev, expected.st_ino, expected.st_size),
            "reference_library_rejected",
        )
        header = os.read(fd, 64)
        _require(
            len(header) == 64
            and header[:7] == b"\x7fELF\x02\x01\x01"
            and struct.unpack_from("<HH", header, 16) == (3, 62),
            "reference_library_rejected",
        )
        library = ctypes.CDLL("/proc/self/fd/" + str(fd), mode=os.RTLD_LOCAL | os.RTLD_NOW)
        final = target.lstat()
        _require(
            (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns)
            == (actual.st_dev, actual.st_ino, actual.st_size, actual.st_mtime_ns, actual.st_ctime_ns),
            "reference_library_rejected",
        )
        return library
    except UnitReferenceError:
        raise
    except Exception:
        raise UnitReferenceError("reference_library_rejected") from None
    finally:
        if fd is not None:
            os.close(fd)


def _check_socket():
    try:
        for parent in reversed(_SOCKET.parents):
            info = parent.lstat()
            _require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022)
        info = _SOCKET.lstat()
        # A system-bus socket normally permits connecting users. Its containing
        # directories and ownership, fixed address, and bus authentication bind it.
        _require(stat.S_ISSOCK(info.st_mode) and info.st_uid == 0)
    except UnitReferenceError:
        raise
    except Exception:
        raise UnitReferenceError("reference_bus_rejected") from None


def _read_at(directory, name, limit):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    try:
        raw = os.read(fd, limit + 1)
        _require(len(raw) <= limit, "reference_identity_rejected")
        return raw.decode("ascii")
    finally:
        os.close(fd)


def _process_identity(expected, *, stopped):
    path = _PROC / str(expected.pid)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        _require(stopped, "reference_identity_rejected")
        return
    except Exception:
        raise UnitReferenceError("reference_identity_rejected") from None
    try:
        _require(not stopped, "reference_identity_rejected")
        before = os.fstat(fd)
        first = _read_at(fd, "stat", 4096)
        _require(first.split(" ", 1)[0] == str(expected.pid), "reference_identity_rejected")
        tail = first.rsplit(")", 1)[1].split()
        _require(int(tail[19]) == expected.startticks, "reference_identity_rejected")
        status = _read_at(fd, "status", 16384)
        uids = [row.split()[1:] for row in status.splitlines() if row.startswith("Uid:")]
        _require(uids == [[str(expected.uid)] * 4], "reference_identity_rejected")
        _require(
            _read_at(fd, "cgroup", 4096).splitlines() == ["0::/system.slice/" + expected.unit],
            "reference_identity_rejected",
        )
        second = _read_at(fd, "stat", 4096)
        after = path.lstat()
        _require(
            first.rsplit(")", 1)[1].split()[19] == second.rsplit(")", 1)[1].split()[19]
            and (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino),
            "reference_identity_rejected",
        )
    except UnitReferenceError:
        raise
    except Exception:
        raise UnitReferenceError("reference_identity_rejected") from None
    finally:
        os.close(fd)


class _NativeBus:
    """Small, explicitly prototyped v249 C ABI; all operation names are fixed."""

    def __init__(self):
        self.lib, self.bus, self.closed = None, ctypes.c_void_p(), False
        self.owner = (os.getpid(), threading.get_ident())
        self.initial_unique = None

    def _owner(self):
        _require((os.getpid(), threading.get_ident()) == self.owner and not self.closed)

    def _bind(self):
        pointer, string, integer = ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int
        error_pointer, message_pointer = ctypes.POINTER(_BusError), ctypes.POINTER(pointer)
        declarations = {
            "sd_bus_new": ([message_pointer], integer),
            "sd_bus_set_address": ([pointer, string], integer),
            "sd_bus_set_bus_client": ([pointer, integer], integer),
            "sd_bus_set_method_call_timeout": ([pointer, ctypes.c_uint64], integer),
            "sd_bus_start": ([pointer], integer),
            "sd_bus_wait": ([pointer, ctypes.c_uint64], integer),
            "sd_bus_process": ([pointer, message_pointer], integer),
            "sd_bus_is_open": ([pointer], integer),
            "sd_bus_is_ready": ([pointer], integer),
            "sd_bus_get_unique_name": ([pointer, ctypes.POINTER(string)], integer),
            "sd_bus_call_method": (
                [pointer, string, string, string, string, error_pointer, message_pointer, string],
                integer,
            ),
            "sd_bus_get_property": (
                [pointer, string, string, string, string, error_pointer, message_pointer, string],
                integer,
            ),
            "sd_bus_message_read_basic": ([pointer, ctypes.c_char, pointer], integer),
            "sd_bus_message_read_array": (
                [pointer, ctypes.c_char, ctypes.POINTER(pointer), ctypes.POINTER(ctypes.c_size_t)],
                integer,
            ),
            "sd_bus_message_enter_container": ([pointer, ctypes.c_char, string], integer),
            "sd_bus_message_exit_container": ([pointer], integer),
            "sd_bus_message_at_end": ([pointer, integer], integer),
            "sd_bus_message_unref": ([pointer], pointer),
            "sd_bus_error_free": ([error_pointer], None),
            "sd_bus_close": ([pointer], None),
            "sd_bus_unref": ([pointer], pointer),
        }
        for name, (arguments, result) in declarations.items():
            function = getattr(self.lib, name)
            function.argtypes, function.restype = arguments, result

    @staticmethod
    def _checked(result):
        if type(result) is not int or result < 0:
            number = -result if type(result) is int and -4095 <= result < 0 else None
            raise UnitReferenceError("reference_call_failed", number)
        return result

    def open(self):
        self._owner()
        _require(self.lib is None and not self.bus.value, "reference_state_rejected")
        try:
            self.lib = _trusted_library()
            self._bind()
            _check_socket()
            self._checked(self.lib.sd_bus_new(ctypes.byref(self.bus)))
            _require(bool(self.bus.value))
            self._checked(self.lib.sd_bus_set_address(self.bus, _ADDRESS))
            self._checked(self.lib.sd_bus_set_bus_client(self.bus, 1))
            self._checked(self.lib.sd_bus_set_method_call_timeout(self.bus, 2_000_000))
            self._checked(self.lib.sd_bus_start(self.bus))
            deadline = time.monotonic() + 2.0
            for _ in range(128):
                self._checked(self.lib.sd_bus_process(self.bus, None))
                if self.lib.sd_bus_is_ready(self.bus) > 0:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise UnitReferenceError("reference_call_failed", errno.ETIMEDOUT)
                self._checked(self.lib.sd_bus_wait(self.bus, min(50_000, max(1, int(remaining * 1_000_000)))))
            else:
                raise UnitReferenceError("reference_call_failed", errno.ETIMEDOUT)
            name = ctypes.c_char_p()
            self._checked(self.lib.sd_bus_get_unique_name(self.bus, ctypes.byref(name)))
            self.initial_unique = self._name(name.value)
            return self.unique()
        except Exception as error:
            self.close()
            if isinstance(error, UnitReferenceError):
                raise
            raise UnitReferenceError("reference_call_failed") from None

    @staticmethod
    def _name(raw):
        _require(type(raw) is bytes and 3 <= len(raw) <= 64)
        try:
            value = raw.decode("ascii")
        except UnicodeError:
            raise UnitReferenceError("reference_bus_rejected") from None
        _require(_UNIQUE.fullmatch(value))
        return value

    def unique(self):
        self._owner()
        _require(self.lib is not None and bool(self.bus.value))
        for _ in range(16):
            if self._checked(self.lib.sd_bus_process(self.bus, None)) == 0:
                break
        else:
            raise UnitReferenceError("reference_bus_rejected")
        _require(self.lib.sd_bus_is_open(self.bus) > 0 and self.lib.sd_bus_is_ready(self.bus) > 0)
        name = ctypes.c_char_p()
        self._checked(self.lib.sd_bus_get_unique_name(self.bus, ctypes.byref(name)))
        value = self._name(name.value)
        _require(value == self.initial_unique)
        return value

    def _method(self, member, expected):
        _require(member in {"GetUnit", "RefUnit", "UnrefUnit"})
        self.unique()
        error, reply = _BusError(), ctypes.c_void_p()
        try:
            result = self.lib.sd_bus_call_method(
                self.bus,
                _DESTINATION,
                _MANAGER_PATH,
                _MANAGER_INTERFACE,
                member.encode("ascii"),
                ctypes.byref(error),
                ctypes.byref(reply),
                b"s",
                ctypes.c_char_p(expected.unit.encode("ascii")),
            )
            if result < 0 and error.name == b"org.freedesktop.systemd1.NoSuchUnit":
                raise _MissingUnit()
            self._checked(result)
            _require(bool(reply.value))
            if member == "GetUnit":
                value = ctypes.c_char_p()
                _require(
                    self._checked(self.lib.sd_bus_message_read_basic(reply, b"o", ctypes.byref(value))) > 0
                )
                _require(value.value == expected.object_path.encode("ascii"))
            _require(self._checked(self.lib.sd_bus_message_at_end(reply, 1)) > 0)
        finally:
            if reply.value:
                self.lib.sd_bus_message_unref(reply)
            self.lib.sd_bus_error_free(ctypes.byref(error))

    def ref(self, expected):
        self._method("RefUnit", expected)

    def unref(self, expected):
        self._method("UnrefUnit", expected)

    def _property(self, expected, member, signature):
        unit_properties = {
            "Id": b"s",
            "LoadState": b"s",
            "ActiveState": b"s",
            "SubState": b"s",
            "InvocationID": b"ay",
            "Refs": b"as",
        }
        # v249 dbus.c registers bus_unit_cgroup_vtable on Service.
        service_properties = {"MainPID": b"u", "ExecMainPID": b"u", "ControlGroup": b"s"}
        interface = _UNIT_INTERFACE if member in unit_properties else _SERVICE_INTERFACE
        _require((unit_properties | service_properties).get(member) == signature)
        self.unique()
        error, reply = _BusError(), ctypes.c_void_p()
        try:
            result = self.lib.sd_bus_get_property(
                self.bus,
                _DESTINATION,
                expected.object_path.encode("ascii"),
                interface,
                member.encode("ascii"),
                ctypes.byref(error),
                ctypes.byref(reply),
                signature,
            )
            if result < 0 and error.name in {
                b"org.freedesktop.DBus.Error.UnknownObject",
                b"org.freedesktop.systemd1.NoSuchUnit",
            }:
                raise _MissingObject()
            self._checked(result)
            _require(bool(reply.value))
            if signature == b"as":
                _require(self._checked(self.lib.sd_bus_message_enter_container(reply, b"a", b"s")) > 0)
                value = []
                for _ in range(3):
                    item = ctypes.c_char_p()
                    result = self._checked(
                        self.lib.sd_bus_message_read_basic(reply, b"s", ctypes.byref(item))
                    )
                    if result == 0:
                        break
                    value.append(self._name(item.value))
                    _require(len(value) <= 1, "reference_refs_rejected")
                else:
                    raise UnitReferenceError("reference_refs_rejected")
                self._checked(self.lib.sd_bus_message_exit_container(reply))
            elif signature == b"ay":
                data, size = ctypes.c_void_p(), ctypes.c_size_t()
                _require(
                    self._checked(
                        self.lib.sd_bus_message_read_array(
                            reply, b"y", ctypes.byref(data), ctypes.byref(size)
                        )
                    )
                    > 0
                    and size.value == 16
                    and bool(data.value)
                )
                value = ctypes.string_at(data, 16).hex()
            else:
                item = ctypes.c_uint32() if signature == b"u" else ctypes.c_char_p()
                _require(
                    self._checked(self.lib.sd_bus_message_read_basic(reply, signature, ctypes.byref(item)))
                    > 0
                )
                if signature == b"u":
                    value = item.value
                else:
                    raw = item.value
                    _require(type(raw) is bytes and len(raw) <= 512)
                    value = raw.decode("ascii")
            _require(self._checked(self.lib.sd_bus_message_at_end(reply, 0)) > 0)
            self._checked(self.lib.sd_bus_message_exit_container(reply))
            _require(self._checked(self.lib.sd_bus_message_at_end(reply, 1)) > 0)
            return value
        except UnicodeError:
            raise UnitReferenceError("reference_bus_rejected") from None
        finally:
            if reply.value:
                self.lib.sd_bus_message_unref(reply)
            self.lib.sd_bus_error_free(ctypes.byref(error))

    def observe(self, expected):
        # GetUnit never loads an unknown unit; RefUnit is forbidden before this.
        self._method("GetUnit", expected)
        return {
            member: self._property(expected, member, signature)
            for member, signature in {
                "Id": b"s",
                "LoadState": b"s",
                "ActiveState": b"s",
                "SubState": b"s",
                "InvocationID": b"ay",
                "ControlGroup": b"s",
                "MainPID": b"u",
                "ExecMainPID": b"u",
            }.items()
        }

    def refs(self, expected):
        return self._property(expected, "Refs", b"as")

    def released_refs(self, expected):
        try:
            _require(self.refs(expected) == [], "reference_refs_rejected")
        except _MissingObject:
            # Properties may race GC after Unref. A fresh same-connection
            # non-loading GetUnit must independently prove exact NoSuchUnit.
            try:
                self._method("GetUnit", expected)
            except _MissingUnit:
                self.unique()
                return
            raise UnitReferenceError("reference_refs_rejected") from None

    def close(self):
        if self.closed:
            return
        self.closed = True
        bus, self.bus = self.bus, ctypes.c_void_p()
        if self.lib is not None and bus.value:
            try:
                self.lib.sd_bus_close(bus)
            finally:
                self.lib.sd_bus_unref(bus)


class UnitReference:
    """Single-use same-thread root supervisor object; no request/RPC surface."""

    def __init__(self, expected, *, _backend=None):
        _require(type(expected) is ExpectedIdentity, "reference_identity_rejected")
        self.expected, self.backend = expected, _backend
        self.state, self.unique_name = "new", None
        self.ref_attempted = self.unref_attempted = self.held = self.terminal_verified = False
        self.owner = (os.getpid(), threading.get_ident())

    def _owner(self):
        _require(self.owner == (os.getpid(), threading.get_ident()), "reference_state_rejected")

    def _observe(self, *, stopped):
        _process_identity(self.expected, stopped=stopped)
        observed = self.backend.observe(self.expected)
        expected = self.expected
        _require(
            type(observed) is dict
            and set(observed)
            == {
                "Id",
                "LoadState",
                "ActiveState",
                "SubState",
                "InvocationID",
                "ControlGroup",
                "MainPID",
                "ExecMainPID",
            },
            "reference_identity_rejected",
        )
        _require(
            observed["Id"] == expected.unit
            and observed["LoadState"] == "loaded"
            and observed["InvocationID"] == expected.invocation_id
            and type(observed["MainPID"]) is int
            and type(observed["ExecMainPID"]) is int
            and observed["ExecMainPID"] == expected.pid
            and observed["MainPID"] == (0 if stopped else expected.pid)
            and observed["ActiveState"] in ({"inactive", "failed"} if stopped else {"active"})
            and observed["ControlGroup"]
            in ({"", "/system.slice/" + expected.unit} if stopped else {"/system.slice/" + expected.unit}),
            "reference_identity_rejected",
        )
        _process_identity(expected, stopped=stopped)
        return observed

    def _refs(self, expected):
        _require(self.backend.unique() == self.unique_name)
        actual = self.backend.refs(self.expected)
        _require(type(actual) is list and actual == expected, "reference_refs_rejected")

    def _proof(self, state):
        expected = self.expected
        return {
            "schema": 1,
            "unit": expected.unit,
            "invocation_id": expected.invocation_id,
            "pid": expected.pid,
            "startticks": expected.startticks,
            "uid": expected.uid,
            "unique_name": self.unique_name,
            "state": state,
        }

    def acquire(self):
        self._owner()
        _require(self.state == "new", "reference_state_rejected")
        self.state = "acquiring"
        try:
            self.backend = _NativeBus() if self.backend is None else self.backend
            self.unique_name = self.backend.open()
            _require(type(self.unique_name) is str and _UNIQUE.fullmatch(self.unique_name))
            self._observe(stopped=False)
            self._refs([])
            self.ref_attempted = True  # Uncertain delivery is never retried.
            self.backend.ref(self.expected)
            self.held = True
            self._refs([self.unique_name])
            self._observe(stopped=False)
            self.state = "held"
            return self._proof("acquired")
        except Exception as error:
            self.state = "failed"
            if isinstance(error, UnitReferenceError):
                raise
            raise UnitReferenceError("reference_call_failed") from None

    def verify(self, *, stopped=False):
        self._owner()
        _require(
            type(stopped) is bool and self.state == "held" and self.held and not self.unref_attempted,
            "reference_state_rejected",
        )
        try:
            self._refs([self.unique_name])
            self._observe(stopped=stopped)
            self._refs([self.unique_name])
            self.terminal_verified = self.terminal_verified or stopped
            return self._proof("verified_stopped" if stopped else "verified_running")
        except Exception as error:
            self.state = "failed"
            if isinstance(error, UnitReferenceError):
                raise
            raise UnitReferenceError("reference_call_failed") from None

    def verify_held(self):
        """Worker reference continuity, including natural active-exited only.

        verified_exited proves neither an explicit stop nor BPF cleanup. It does
        not arm release(); actual own-unit stop and verify(stopped=True) remain
        mandatory. No unknown unit is loaded and the original sender is reused.
        """
        self._owner()
        _require(
            self.expected.role == "live-worker"
            and self.state == "held"
            and self.held
            and not self.unref_attempted,
            "reference_state_rejected",
        )
        try:
            self._refs([self.unique_name])
            observed = self.backend.observe(self.expected)
            if type(observed) is dict and observed.get("MainPID") == self.expected.pid:
                self._observe(stopped=False)
                state = "verified_running"
            else:
                identity = self.expected
                _require(
                    type(observed) is dict
                    and set(observed)
                    == {
                        "Id",
                        "LoadState",
                        "ActiveState",
                        "SubState",
                        "InvocationID",
                        "ControlGroup",
                        "MainPID",
                        "ExecMainPID",
                    }
                    and observed["Id"] == identity.unit
                    and observed["LoadState"] == "loaded"
                    and observed["ActiveState"] == "active"
                    and observed["SubState"] == "exited"
                    and observed["InvocationID"] == identity.invocation_id
                    and type(observed["MainPID"]) is int
                    and observed["MainPID"] == 0
                    and type(observed["ExecMainPID"]) is int
                    and observed["ExecMainPID"] == identity.pid
                    and observed["ControlGroup"] in {"", "/system.slice/" + identity.unit},
                    "reference_identity_rejected",
                )
                _process_identity(identity, stopped=True)
                _require(self.backend.observe(identity) == observed, "reference_identity_rejected")
                _process_identity(identity, stopped=True)
                state = "verified_exited"
            self._refs([self.unique_name])
            return self._proof(state)
        except Exception as error:
            self.state = "failed"
            if isinstance(error, UnitReferenceError):
                raise
            raise UnitReferenceError("reference_call_failed") from None

    def release(self):
        self._owner()
        _require(
            self.state == "held" and self.held and self.terminal_verified and not self.unref_attempted,
            "reference_state_rejected",
        )
        try:
            # A second terminal read follows the caller's durable kernel proof.
            self.verify(stopped=True)
            self.unref_attempted = True
            self.backend.unref(self.expected)
            self.held = False
            _require(self.backend.unique() == self.unique_name)
            self.backend.released_refs(self.expected)
            _require(self.backend.unique() == self.unique_name)
            self.backend.close()
            self.state = "released"
            return self._proof("released")
        except Exception as error:
            self.state = "failed"
            if isinstance(error, UnitReferenceError):
                raise
            raise UnitReferenceError("reference_call_failed") from None

    def close(self):
        """Containment only, never an accepted release proof or reconnect."""
        self._owner()
        failed = False
        if self.backend is not None:
            if self.held and not self.unref_attempted:
                self.unref_attempted = True
                try:
                    _require(self.backend.unique() == self.unique_name)
                    self.backend.unref(self.expected)
                except Exception:
                    failed = True
            try:
                self.backend.close()
            except Exception:
                failed = True
        self.held = False
        if self.state != "released":
            self.state = "closed_failed"
        if failed:
            raise UnitReferenceError("reference_close_failed") from None
