"""Explicit fake C ABI/OS fixtures; never a real Linux system-bus receipt."""

import ctypes
import errno
import os
import stat
import struct
import types
from pathlib import Path

import pytest

from scripts import systemd_unit_reference as reference

REAL_PROCESS_IDENTITY = reference._process_identity


def expected(role="live"):
    return reference.ExpectedIdentity(role, "a" * 12, "b" * 32, "c" * 32, 12345, 7788, 2002)


class Function:
    def __init__(self, function):
        self.function = function
        self.argtypes = self.restype = None

    def __call__(self, *args):
        return self.function(*args)


class FakeLibrary:
    """Exercises actual ctypes out-pointers, cursor/container and cleanup code."""

    def __init__(self, identity):
        self.expected, self.calls = identity, []
        self.unique_name = b":1.234"
        self.ready, self.connected, self.running, self.loaded = True, True, True, True
        self.exited = False
        self.refs, self.failures, self.messages, self.buffers = [], {}, {}, []
        self.next_message = 1000
        self.property_changes = {}
        self.after_ref = None
        self.trailing = self.wrong_signature = False
        self.gc_after_unref = False
        self.bus = 777
        names = [
            "new",
            "set_address",
            "set_bus_client",
            "set_method_call_timeout",
            "start",
            "wait",
            "process",
            "is_open",
            "is_ready",
            "get_unique_name",
            "call_method",
            "get_property",
            "message_read_basic",
            "message_read_array",
            "message_enter_container",
            "message_exit_container",
            "message_at_end",
            "message_unref",
            "error_free",
            "close",
            "unref",
        ]
        for name in names:
            setattr(self, "sd_bus_" + name, Function(lambda *args, _name=name: self.call(_name, *args)))

    def message(self, output, kind, value):
        number = self.next_message
        self.next_message += 1
        output._obj.value = number
        self.messages[number] = {
            "kind": kind,
            "value": value,
            "index": 0,
            "variant": kind != "method",
            "array": False,
            "consumed": False,
        }
        return 0

    def snapshot(self):
        identity = self.expected
        return {
            "Id": identity.unit,
            "LoadState": "loaded",
            "ActiveState": "active" if self.running or self.exited else "inactive",
            "SubState": "running" if self.running else "exited" if self.exited else "dead",
            "InvocationID": identity.invocation_id,
            "ControlGroup": "/system.slice/" + identity.unit if self.running else "",
            "MainPID": identity.pid if self.running else 0,
            "ExecMainPID": identity.pid,
            **self.property_changes,
        }

    def call(self, name, *args):
        self.calls.append((name, args))
        if name in self.failures:
            return self.failures[name]
        if name == "new":
            args[0]._obj.value = self.bus
        elif name == "set_address":
            assert args[1] == b"unix:path=/run/dbus/system_bus_socket"
        elif name == "set_bus_client":
            assert args[1] == 1
        elif name == "set_method_call_timeout":
            assert args[1] == 2_000_000
        elif name == "wait":
            assert 1 <= args[1] <= 50_000
        elif name == "is_open":
            return int(self.connected)
        elif name == "is_ready":
            return int(self.ready)
        elif name == "get_unique_name":
            args[1]._obj.value = self.unique_name
        elif name == "call_method":
            bus, destination, path, interface, member, error, output, signature, unit = args
            assert bus.value == self.bus
            assert destination == b"org.freedesktop.systemd1"
            assert path == b"/org/freedesktop/systemd1"
            assert interface == b"org.freedesktop.systemd1.Manager"
            assert signature == b"s" and unit.value == self.expected.unit.encode()
            if member == b"GetUnit" and not self.loaded:
                error._obj.name = b"org.freedesktop.systemd1.NoSuchUnit"
                error._obj.message = b"RAW_NOT_FOR_LOGS"
                return -errno.ENOENT
            if member == b"RefUnit":
                self.refs.append(self.unique_name.decode())
                if self.after_ref:
                    self.after_ref()
            elif member == b"UnrefUnit":
                assert self.refs.count(self.unique_name.decode()) == 1
                self.refs.remove(self.unique_name.decode())
                if self.gc_after_unref:
                    self.loaded = False
            else:
                assert member == b"GetUnit"
            result = self.failures.get(member, 0)
            if result < 0:
                error._obj.name, error._obj.message = b"org.example.Unknown", b"RAW_NOT_FOR_LOGS"
                return result
            return self.message(
                output, "method", self.expected.object_path.encode() if member == b"GetUnit" else None
            )
        elif name == "get_property":
            bus, destination, path, interface, member, error, output, signature = args
            assert bus.value == self.bus and destination == b"org.freedesktop.systemd1"
            assert path == self.expected.object_path.encode()
            assert interface == (
                b"org.freedesktop.systemd1.Service"
                if member in {b"MainPID", b"ExecMainPID", b"ControlGroup"}
                else b"org.freedesktop.systemd1.Unit"
            )
            key = member.decode()
            if not self.loaded:
                error._obj.name = b"org.freedesktop.DBus.Error.UnknownObject"
                error._obj.message = b"RAW_NOT_FOR_LOGS"
                return -errno.ENOENT
            kind = (
                b"as"
                if key == "Refs"
                else b"ay"
                if key == "InvocationID"
                else b"u"
                if key in {"MainPID", "ExecMainPID"}
                else b"s"
            )
            assert signature == kind
            if self.wrong_signature:
                error._obj.name, error._obj.message = b"org.example.BadType", b"RAW_NOT_FOR_LOGS"
                return -errno.ENXIO
            return self.message(output, kind, list(self.refs) if key == "Refs" else self.snapshot()[key])
        elif name.startswith("message_"):
            message = self.messages[args[0].value]
            if name == "message_read_basic":
                _, signature, output = args
                if message["array"]:
                    assert signature == b"s"
                    if message["index"] == len(message["value"]):
                        return 0
                    value = message["value"][message["index"]].encode()
                    message["index"] += 1
                else:
                    if message["consumed"]:
                        return 0
                    value = message["value"]
                    assert signature == (b"o" if message["kind"] == "method" else message["kind"])
                    message["consumed"] = True
                    if isinstance(value, str):
                        value = value.encode()
                output._obj.value = value
                return 1
            if name == "message_read_array":
                _, signature, output, size = args
                assert signature == b"y" and message["kind"] == b"ay"
                data = bytes.fromhex(message["value"])
                buffer = ctypes.create_string_buffer(data)
                self.buffers.append(buffer)
                output._obj.value, size._obj.value = ctypes.addressof(buffer), len(data)
                message["consumed"] = True
                return 1
            if name == "message_enter_container":
                assert args[1:] == (b"a", b"s") and message["kind"] == b"as"
                message["array"] = True
                return 1
            if name == "message_exit_container":
                if message["array"]:
                    message["array"], message["consumed"] = False, True
                else:
                    assert message["variant"]
                    message["variant"] = False
                return 1
            if name == "message_at_end":
                complete = args[1]
                if self.trailing:
                    return 0
                if message["kind"] == "method":
                    return int(message["consumed"] or message["value"] is None)
                return int(message["consumed"] and (not complete or not message["variant"]))
            if name == "message_unref":
                del self.messages[args[0].value]
                return None
        elif name == "close":
            self.connected = False
        return 0

    def methods(self, member):
        return [args for name, args in self.calls if name == "call_method" and args[4] == member.encode()]


@pytest.fixture
def fixture(monkeypatch):
    identity = expected()
    library = FakeLibrary(identity)
    monkeypatch.setattr(reference, "_trusted_library", lambda: library)
    monkeypatch.setattr(reference, "_check_socket", lambda: None)

    def process(bound, *, stopped):
        assert bound == identity
        if library.running is stopped:
            raise reference.UnitReferenceError("reference_identity_rejected")

    monkeypatch.setattr(reference, "_process_identity", process)
    value = reference.UnitReference(identity)
    return identity, library, value


@pytest.mark.parametrize(
    "role,prefix",
    [("live", "prism-live-broker"), ("live-worker", "prism-live-worker"), ("market", "prism-market-probe")],
)
def test_unit_name_is_exact_controller_derivation_without_request_path(role, prefix):
    identity = expected(role)
    assert identity.unit == prefix + "-" + "a" * 12 + "-" + "b" * 32 + ".service"
    assert identity.object_path.endswith(identity.unit.replace("-", "_2d").replace(".", "_2e"))
    assert reference.UnitReference(identity).state == "new"


@pytest.mark.parametrize(
    "change",
    [
        dict(role="production"),
        dict(candidate_prefix="../production"),
        dict(nonce="x" * 32),
        dict(invocation_id="c" * 31),
        dict(pid=True),
        dict(pid=1),
        dict(startticks=0),
        dict(uid=0),
    ],
)
def test_invalid_identity_cannot_construct_or_open_a_bus(change):
    values = vars(expected()) | change
    with pytest.raises(reference.UnitReferenceError, match="reference_identity_rejected"):
        reference.ExpectedIdentity(**values)
    with pytest.raises(reference.UnitReferenceError):
        reference.UnitReference("stock-app.service")


@pytest.mark.parametrize("collected", [False, True])
def test_actual_cabi_lifecycle_single_connection_ref_hold_terminal_unref_and_close(fixture, collected):
    identity, library, value = fixture
    proof = value.acquire()
    assert proof == {
        "schema": 1,
        "unit": identity.unit,
        "invocation_id": identity.invocation_id,
        "pid": identity.pid,
        "startticks": identity.startticks,
        "uid": identity.uid,
        "unique_name": ":1.234",
        "state": "acquired",
    }
    assert value.verify()["state"] == "verified_running"
    assert len(library.methods("RefUnit")) == 1 and not library.methods("UnrefUnit")
    library.running = False
    assert value.verify(stopped=True)["state"] == "verified_stopped"
    library.gc_after_unref = collected
    assert value.release()["state"] == "released"
    assert len(library.methods("UnrefUnit")) == 1 and value.state == "released"
    assert len([1 for name, _ in library.calls if name == "new"]) == 1
    assert len([1 for name, _ in library.calls if name == "close"]) == 1
    assert not library.messages
    value.close()
    assert value.state == "released" and len(library.methods("UnrefUnit")) == 1
    with pytest.raises(reference.UnitReferenceError):
        value.acquire()


@pytest.mark.parametrize(
    "change",
    [
        "foreign_ref",
        "notloaded",
        "mainpid",
        "execpid",
        "invocation",
        "inactive",
        "cgroup",
        "disconnected",
        "notready",
        "signature",
        "trailing",
    ],
)
def test_acquire_rejects_wrong_unit_identity_refs_connection_and_types_before_ref(fixture, change):
    _, library, value = fixture
    if change == "foreign_ref":
        library.refs = [":1.999"]
    elif change == "notloaded":
        library.loaded = False
    elif change == "mainpid":
        library.property_changes["MainPID"] = 12
    elif change == "execpid":
        library.property_changes["ExecMainPID"] = 12
    elif change == "invocation":
        library.property_changes["InvocationID"] = "d" * 32
    elif change == "inactive":
        library.property_changes["ActiveState"] = "inactive"
    elif change == "cgroup":
        library.property_changes["ControlGroup"] = "/system.slice/stock-app.service"
    elif change == "disconnected":
        library.connected = False
    elif change == "notready":
        library.ready = False
    elif change == "signature":
        library.wrong_signature = True
    else:
        library.trailing = True
    with pytest.raises(reference.UnitReferenceError) as error:
        value.acquire()
    assert "RAW_NOT_FOR_LOGS" not in str(error.value)
    assert not library.methods("RefUnit")
    assert value.state == "failed"
    value.close()
    assert value.state == "closed_failed"


@pytest.mark.parametrize(
    "change", ["foreign_ref", "duplicate_ref", "empty_refs", "disconnect", "unique_changed", "pid_changed"]
)
def test_held_reference_cannot_be_replaced_reconnected_or_reused(fixture, change):
    _, library, value = fixture
    value.acquire()
    if change == "foreign_ref":
        library.refs = [":1.999"]
    elif change == "duplicate_ref":
        library.refs *= 2
    elif change == "empty_refs":
        library.refs = []
    elif change == "disconnect":
        library.connected = False
    elif change == "unique_changed":
        library.unique_name = b":1.235"
    else:
        library.property_changes["MainPID"] = 12
    with pytest.raises(reference.UnitReferenceError):
        value.verify()
    assert value.state == "failed" and len(library.methods("RefUnit")) == 1
    with pytest.raises(reference.UnitReferenceError):
        value.acquire()
    try:
        value.close()
    except reference.UnitReferenceError:
        pass
    assert len([1 for name, _ in library.calls if name == "new"]) == 1


def test_releasing_while_running_or_without_fresh_terminal_proof_is_rejected(fixture):
    _, library, value = fixture
    value.acquire()
    with pytest.raises(reference.UnitReferenceError):
        value.release()
    with pytest.raises(reference.UnitReferenceError):
        value.verify(stopped=True)
    assert not library.methods("UnrefUnit")
    value.close()
    assert value.state == "closed_failed" and len(library.methods("UnrefUnit")) == 1


@pytest.mark.parametrize("member", ["RefUnit", "UnrefUnit"])
def test_uncertain_native_method_delivery_is_never_retried_and_raw_bus_text_is_discarded(fixture, member):
    _, library, value = fixture
    if member == "UnrefUnit":
        value.acquire()
        library.running = False
        value.verify(stopped=True)
    library.failures[member.encode()] = -errno.ETIMEDOUT
    with pytest.raises(reference.UnitReferenceError) as error:
        value.acquire() if member == "RefUnit" else value.release()
    assert error.value.error_number == errno.ETIMEDOUT and str(error.value) == "reference_call_failed"
    assert "RAW_NOT_FOR_LOGS" not in repr(error.value)
    value.close()
    assert len(library.methods(member)) == 1
    assert value.state == "closed_failed"


def test_terminal_identity_must_still_match_on_second_read_before_unref(fixture):
    _, library, value = fixture
    value.acquire()
    library.running = False
    value.verify(stopped=True)
    library.property_changes["InvocationID"] = "d" * 32
    with pytest.raises(reference.UnitReferenceError):
        value.release()
    assert not library.methods("UnrefUnit")
    value.close()


def test_cross_thread_or_fork_identity_never_operates_the_existing_native_connection(fixture, monkeypatch):
    _, library, value = fixture
    value.acquire()
    monkeypatch.setattr(reference.threading, "get_ident", lambda: -1)
    with pytest.raises(reference.UnitReferenceError):
        value.verify()
    assert len(library.methods("RefUnit")) == 1


@pytest.mark.parametrize("operation", ["process", "call_method", "get_property"])
def test_native_failure_unrefs_every_message_and_returns_bounded_errno(fixture, operation):
    _, library, value = fixture
    library.failures[operation] = -errno.ECONNRESET
    with pytest.raises(reference.UnitReferenceError) as error:
        value.acquire()
    assert error.value.error_number == errno.ECONNRESET
    value.close()
    assert not library.messages


def test_cabi_has_explicit_pointer_and_variadic_fixed_prefix_prototypes(fixture):
    _, library, value = fixture
    value.acquire()
    assert library.sd_bus_new.argtypes == [ctypes.POINTER(ctypes.c_void_p)]
    assert library.sd_bus_new.restype is ctypes.c_int
    assert library.sd_bus_call_method.argtypes[-1] is ctypes.c_char_p
    assert len(library.sd_bus_call_method.argtypes) == 8
    assert library.sd_bus_message_unref.restype is ctypes.c_void_p
    assert library.sd_bus_error_free.restype is None
    assert ctypes.sizeof(reference._BusError) == 24
    value.close()


def test_real_native_backend_is_blocked_on_mac_without_loading_a_library(monkeypatch):
    monkeypatch.setattr(reference.sys, "platform", "darwin")
    monkeypatch.setattr(reference.ctypes, "CDLL", lambda *a, **k: pytest.fail("never dlopen"))
    with pytest.raises(reference.UnitReferenceError, match="reference_platform_rejected"):
        reference.UnitReference(expected()).acquire()


def stat_text(identity):
    return str(identity.pid) + " (public synthetic worker) S " + "0 " * 18 + str(identity.startticks) + "\n"


def make_process(tmp_path, identity):
    proc = tmp_path / "proc"
    process = proc / str(identity.pid)
    process.mkdir(parents=True)
    (process / "stat").write_text(stat_text(identity))
    (process / "status").write_text("Name:\tpublic\nUid:\t" + "\t".join([str(identity.uid)] * 4) + "\n")
    (process / "cgroup").write_text("0::/system.slice/" + identity.unit + "\n")
    return proc, process


def test_actual_dirfd_process_sampling_cannot_equate_cancel_or_timeout_with_exit(tmp_path, monkeypatch):
    identity = expected()
    proc, process = make_process(tmp_path, identity)
    monkeypatch.setattr(reference, "_PROC", proc)
    reference._process_identity(identity, stopped=False)
    with pytest.raises(reference.UnitReferenceError):
        reference._process_identity(identity, stopped=True)
    for file in process.iterdir():
        file.unlink()
    process.rmdir()
    reference._process_identity(identity, stopped=True)
    with pytest.raises(reference.UnitReferenceError):
        reference._process_identity(identity, stopped=False)


@pytest.mark.parametrize("change", ["ticks", "uid", "cgroup", "stat_pid", "symlink", "oversize"])
def test_actual_dirfd_process_sampling_rejects_reuse_foreign_user_or_unified_cgroup_drift(
    tmp_path, monkeypatch, change
):
    identity = expected()
    proc, process = make_process(tmp_path, identity)
    monkeypatch.setattr(reference, "_PROC", proc)
    if change == "ticks":
        (process / "stat").write_text(stat_text(identity).replace(str(identity.startticks), "9988"))
    elif change == "uid":
        (process / "status").write_text("Uid:\t1\t1\t1\t1\n")
    elif change == "cgroup":
        (process / "cgroup").write_text("0::/system.slice/stock-app.service\n")
    elif change == "stat_pid":
        (process / "stat").write_text(stat_text(identity).replace(str(identity.pid), "9999", 1))
    elif change == "oversize":
        (process / "status").write_text("x" * 16385)
    else:
        (process / "cgroup").unlink()
        (process / "cgroup").symlink_to(process / "status")
    with pytest.raises(reference.UnitReferenceError, match="reference_identity_rejected"):
        reference._process_identity(identity, stopped=False)


def test_authentication_loop_is_bounded_before_get_unique_name(fixture):
    _, library, value = fixture
    library.ready = False
    with pytest.raises(reference.UnitReferenceError) as error:
        value.acquire()
    assert error.value.error_number == errno.ETIMEDOUT
    assert not [1 for name, _ in library.calls if name == "get_unique_name"]
    assert len([1 for name, _ in library.calls if name == "wait"]) <= 128


def test_control_group_uses_primary_v249_service_interface_and_refs_unit_interface(fixture):
    _, library, value = fixture
    value.acquire()
    properties = [args for name, args in library.calls if name == "get_property"]
    assert {args[3] for args in properties if args[4] == b"ControlGroup"} == {
        b"org.freedesktop.systemd1.Service"
    }
    assert {args[3] for args in properties if args[4] == b"Refs"} == {b"org.freedesktop.systemd1.Unit"}
    value.close()


def elf_bytes(machine=62):
    header = bytearray(64)
    header[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HH", header, 16, 3, machine)
    return bytes(header)


@pytest.mark.parametrize(
    "change", ["none", "bad_elf", "wrong_arch", "outside_target", "writable", "replaced"]
)
def test_root_trusted_native_library_uses_fixed_soname_stable_fd_and_actual_elf_header(
    tmp_path, monkeypatch, change
):
    library_path = tmp_path / "native/libsystemd.so.0"
    library_path.parent.mkdir()
    target = library_path.with_name("libsystemd.so.0.32.0")
    target.write_bytes(
        b"not an ELF library" if change == "bad_elf" else elf_bytes(183 if change == "wrong_arch" else 62)
    )
    library_path.symlink_to(target.name)
    if change == "outside_target":
        outside = tmp_path / "libsystemd.so.0.32.0"
        outside.write_bytes(elf_bytes())
        library_path.unlink()
        library_path.symlink_to(outside)
    real_lstat = Path.lstat

    def root_fixture(path):
        actual = real_lstat(path)
        mode = actual.st_mode & ~0o022
        if path == target and change == "writable":
            mode |= stat.S_IWGRP
        return types.SimpleNamespace(
            st_uid=0,
            st_mode=mode,
            st_dev=actual.st_dev,
            st_ino=actual.st_ino,
            st_size=actual.st_size,
            st_mtime_ns=actual.st_mtime_ns,
            st_ctime_ns=actual.st_ctime_ns,
        )

    monkeypatch.setattr(reference, "_LIBRARY", library_path)
    monkeypatch.setattr(Path, "lstat", root_fixture)
    monkeypatch.setattr(reference.sys, "platform", "linux")
    monkeypatch.setattr(reference.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(reference.os, "geteuid", lambda: 0)
    monkeypatch.setattr(reference.os, "environ", {})
    sentinel, opened = object(), []

    def dlopen(path, *, mode):
        assert path.startswith("/proc/self/fd/")
        fd = int(path.rsplit("/", 1)[1])
        assert os.fstat(fd).st_ino == real_lstat(target).st_ino
        assert mode == os.RTLD_LOCAL | os.RTLD_NOW
        opened.append(fd)
        if change == "replaced":
            replacement = target.with_name("replacement")
            replacement.write_bytes(elf_bytes())
            replacement.replace(target)
        return sentinel

    monkeypatch.setattr(reference.ctypes, "CDLL", dlopen)
    if change == "none":
        assert reference._trusted_library() is sentinel
    else:
        with pytest.raises(reference.UnitReferenceError, match="reference_library_rejected"):
            reference._trusted_library()
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize(
    "environment",
    [
        {"LD_PRELOAD": ""},
        {"LD_LIBRARY_PATH": "/public-fixture"},
        {"DYLD_INSERT_LIBRARIES": "/public-fixture"},
    ],
)
def test_native_loading_rejects_inherited_loader_environment_before_any_dlopen(monkeypatch, environment):
    monkeypatch.setattr(reference.sys, "platform", "linux")
    monkeypatch.setattr(reference.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(reference.os, "geteuid", lambda: 0)
    monkeypatch.setattr(reference.os, "environ", environment)
    monkeypatch.setattr(reference.ctypes, "CDLL", lambda *a, **k: pytest.fail("must not dlopen"))
    with pytest.raises(reference.UnitReferenceError, match="reference_platform_rejected"):
        reference._trusted_library()


def test_unknown_python_or_native_error_text_never_escapes_safe_category(fixture):
    _, library, value = fixture
    library.sd_bus_set_address.function = lambda *a: (_ for _ in ()).throw(RuntimeError("RAW_NOT_FOR_LOGS"))
    with pytest.raises(reference.UnitReferenceError, match="^reference_call_failed$"):
        value.acquire()
    assert not library.connected


def test_partial_open_failure_still_closes_exact_own_bus_once(fixture):
    _, library, value = fixture
    library.failures["set_address"] = -errno.EINVAL
    with pytest.raises(reference.UnitReferenceError):
        value.acquire()
    value.close()
    assert len([1 for name, _ in library.calls if name == "close"]) == 1
    assert not library.methods("RefUnit")


@pytest.mark.parametrize(
    "case", ["gc_race", "unknown_object_but_present", "unknown_error", "foreign_ref_after_unref"]
)
def test_unref_postcondition_accepts_only_typed_empty_refs_or_confirmed_nonloading_gc(fixture, case):
    _, library, value = fixture
    value.acquire()
    library.running = False
    value.verify(stopped=True)
    original = library.sd_bus_get_property.function

    def postcondition(*args):
        if args[4] == b"Refs" and library.methods("UnrefUnit"):
            if case == "foreign_ref_after_unref":
                library.refs = [":1.999"]
            else:
                args[5]._obj.name = (
                    b"org.example.Unknown"
                    if case == "unknown_error"
                    else b"org.freedesktop.DBus.Error.UnknownObject"
                )
                args[5]._obj.message = b"RAW_NOT_FOR_LOGS"
                if case == "gc_race":
                    library.loaded = False
                return -errno.ENOENT
        return original(*args)

    library.sd_bus_get_property.function = postcondition
    if case == "gc_race":
        assert value.release()["state"] == "released"
        assert len(library.methods("GetUnit")) >= 2
    else:
        with pytest.raises(reference.UnitReferenceError):
            value.release()
        assert value.state == "failed"
    value.close()
    assert len(library.methods("UnrefUnit")) == 1


def test_native_terminal_release_does_not_reread_gc_prone_service_metadata_after_unref(fixture):
    _, library, value = fixture
    value.acquire()
    library.running = False
    value.verify(stopped=True)
    value.release()
    index = next(
        i for i, (name, args) in enumerate(library.calls) if name == "call_method" and args[4] == b"UnrefUnit"
    )
    assert [args[4] for name, args in library.calls[index + 1 :] if name == "get_property"] == [b"Refs"]


@pytest.mark.parametrize("change", ["running_after_verified", "lost_reference", "notloaded"])
def test_release_rechecks_every_terminal_fact_before_the_one_unref(fixture, change):
    _, library, value = fixture
    value.acquire()
    library.running = False
    value.verify(stopped=True)
    if change == "running_after_verified":
        library.running = True
    elif change == "lost_reference":
        library.refs = []
    else:
        library.loaded = False
    with pytest.raises(reference.UnitReferenceError):
        value.release()
    assert not library.methods("UnrefUnit")
    try:
        value.close()
    except reference.UnitReferenceError:
        pass


def test_root_system_socket_path_is_fixed_and_unknown_endpoint_rejected(tmp_path, monkeypatch):
    path = tmp_path / "system_bus_socket"
    path.write_bytes(b"not a socket")
    monkeypatch.setattr(reference, "_SOCKET", path)
    with pytest.raises(reference.UnitReferenceError, match="reference_bus_rejected"):
        reference._check_socket()


@pytest.mark.parametrize("role", ["live-worker", "market"])
def test_worker_and_market_each_keep_their_own_connection_reference_and_typed_terminal_identity(
    monkeypatch, role
):
    identity = expected(role)
    library = FakeLibrary(identity)
    monkeypatch.setattr(reference, "_trusted_library", lambda: library)
    monkeypatch.setattr(reference, "_check_socket", lambda: None)
    observed = []

    def process(bound, *, stopped):
        assert bound == identity
        observed.append((bound.unit, stopped))
        assert library.running is not stopped

    monkeypatch.setattr(reference, "_process_identity", process)
    value = reference.UnitReference(identity)
    assert value.acquire()["unit"] == identity.unit
    assert value.verify()["state"] == "verified_running"
    library.running = False
    assert value.verify(stopped=True)["unit"] == identity.unit
    assert value.release()["state"] == "released"
    assert len(library.methods("RefUnit")) == len(library.methods("UnrefUnit")) == 1
    assert {unit for unit, _ in observed} == {identity.unit}
    assert (identity.unit, False) in observed and (identity.unit, True) in observed


@pytest.mark.parametrize(
    "role", ["worker", "broker", "production", "live-worker-extra", "live/worker", "stock-app.service"]
)
def test_role_cannot_expand_fixed_synthetic_unit_prefixes(role):
    with pytest.raises(reference.UnitReferenceError, match="reference_identity_rejected"):
        expected(role)


@pytest.fixture
def worker_fixture(monkeypatch):
    identity = expected("live-worker")
    library = FakeLibrary(identity)
    monkeypatch.setattr(reference, "_trusted_library", lambda: library)
    monkeypatch.setattr(reference, "_check_socket", lambda: None)

    def process(bound, *, stopped):
        assert bound == identity
        if library.running is stopped:
            raise reference.UnitReferenceError("reference_identity_rejected")

    monkeypatch.setattr(reference, "_process_identity", process)
    return identity, library, reference.UnitReference(identity)


def test_worker_held_running_and_natural_exited_are_separate_from_actual_stop_and_release(worker_fixture):
    _, library, value = worker_fixture
    value.acquire()
    assert value.verify_held()["state"] == "verified_running"
    library.running, library.exited = False, True
    proof = value.verify_held()
    assert proof["state"] == "verified_exited" and not value.terminal_verified
    assert set(proof) == {
        "schema",
        "unit",
        "invocation_id",
        "pid",
        "startticks",
        "uid",
        "unique_name",
        "state",
    }
    with pytest.raises(reference.UnitReferenceError, match="reference_state_rejected"):
        value.release()
    assert not library.methods("UnrefUnit")
    library.exited = False  # Explicit synthetic own-unit stop fixture.
    assert value.verify(stopped=True)["state"] == "verified_stopped"
    assert value.release()["state"] == "released"
    assert len(library.methods("RefUnit")) == len(library.methods("UnrefUnit")) == 1


@pytest.mark.parametrize("control_group", ["", "original"])
def test_natural_exited_allows_only_empty_or_original_cgroup(worker_fixture, control_group):
    identity, library, value = worker_fixture
    value.acquire()
    library.running, library.exited = False, True
    library.property_changes["ControlGroup"] = "" if not control_group else "/system.slice/" + identity.unit
    assert value.verify_held()["state"] == "verified_exited"
    value.close()


@pytest.mark.parametrize(
    "change",
    [
        "substate",
        "inactive",
        "unknown_main",
        "foreign_ref",
        "empty_refs",
        "duplicate_refs",
        "disconnected",
        "changed_sender",
        "unknown_unit",
        "invocation",
        "exec_pid",
        "foreign_cgroup",
    ],
)
def test_worker_natural_exit_rejects_unknown_or_changed_kernel_bus_identity(worker_fixture, change):
    _, library, value = worker_fixture
    value.acquire()
    library.running, library.exited = False, True
    if change == "substate":
        library.property_changes["SubState"] = "dead"
    elif change == "inactive":
        library.exited = False
    elif change == "unknown_main":
        library.property_changes["MainPID"] = 9999
    elif change == "foreign_ref":
        library.refs = [":1.999"]
    elif change == "empty_refs":
        library.refs = []
    elif change == "duplicate_refs":
        library.refs *= 2
    elif change == "disconnected":
        library.connected = False
    elif change == "changed_sender":
        library.unique_name = b":1.235"
    elif change == "unknown_unit":
        library.loaded = False
    elif change == "invocation":
        library.property_changes["InvocationID"] = "d" * 32
    elif change == "exec_pid":
        library.property_changes["ExecMainPID"] = 9999
    else:
        library.property_changes["ControlGroup"] = "/system.slice/stock-app.service"
    with pytest.raises(reference.UnitReferenceError):
        value.verify_held()
    assert value.state == "failed" and not value.terminal_verified
    assert len(library.methods("RefUnit")) == 1
    try:
        value.close()
    except reference.UnitReferenceError:
        pass


@pytest.mark.parametrize("role", ["live", "market"])
def test_natural_exit_mode_is_never_available_for_broker_or_market(monkeypatch, role):
    identity = expected(role)
    value = reference.UnitReference(identity, _backend=object())
    value.state, value.held = "held", True
    with pytest.raises(reference.UnitReferenceError, match="reference_state_rejected"):
        value.verify_held()


@pytest.mark.parametrize("stopped", [False, True])
def test_natural_exit_does_not_loosen_existing_running_or_stopped_modes(worker_fixture, stopped):
    _, library, value = worker_fixture
    value.acquire()
    library.running, library.exited = False, True
    with pytest.raises(reference.UnitReferenceError, match="reference_identity_rejected"):
        value.verify(stopped=stopped)
    value.close()


@pytest.mark.parametrize("pid_state", ["gone", "still_alive", "reused"])
def test_worker_natural_exit_checks_actual_private_proc_dir_not_only_mainpid_zero(
    worker_fixture, tmp_path, monkeypatch, pid_state
):
    identity, library, value = worker_fixture
    proc, process = make_process(tmp_path, identity)
    monkeypatch.setattr(reference, "_PROC", proc)
    monkeypatch.setattr(reference, "_process_identity", REAL_PROCESS_IDENTITY)
    value.acquire()
    library.running, library.exited = False, True
    if pid_state == "gone":
        for file in process.iterdir():
            file.unlink()
        process.rmdir()
    elif pid_state == "reused":
        (process / "stat").write_text(stat_text(identity).replace(str(identity.startticks), "99999"))
    if pid_state == "gone":
        assert value.verify_held()["state"] == "verified_exited"
    else:
        with pytest.raises(reference.UnitReferenceError, match="reference_identity_rejected"):
            value.verify_held()
    value.close()


def test_substate_uses_actual_primary_unit_interface_in_cabi(worker_fixture):
    _, library, value = worker_fixture
    value.acquire()
    library.running, library.exited = False, True
    value.verify_held()
    values = [args for name, args in library.calls if name == "get_property" and args[4] == b"SubState"]
    assert values and all(args[3] == b"org.freedesktop.systemd1.Unit" and args[-1] == b"s" for args in values)
    value.close()
