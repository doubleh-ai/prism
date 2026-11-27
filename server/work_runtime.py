"""Private Linux lifecycle control; never an HTTP route or a request-supplied proof.

Root must independently attest this code, service source, kernel identity and
artifact. The root-owned binding supplies an artifact digest; this module does
not prove that digest merely by repeating it in a response.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import stat
import struct
import sys
import time
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path

from .work_lifecycle import KINDS, LifecycleError, Manager, OSObservation, RuntimeIdentity, Ticket

_BINDING = Path("/run/prism-work-binding.json")
_RUN = Path("/run")
_CGROUP = Path("/sys/fs/cgroup")
_ARTIFACT = Path(__file__).resolve().parents[1]
_MAX_FRAME = 16 * 1024
_MAX_FILE = 128 * 1024
_MAX_GROUPS = 1024
_MAX_MEMBERS = 4096
_SAMPLE_NS = 2_000_000_000
_ERRORS = frozenset(
    {
        "binding_rejected",
        "platform_rejected",
        "runtime_identity_rejected",
        "kernel_unknown",
        "socket_rejected",
        "peer_rejected",
        "request_rejected",
        "request_timeout",
        "ticket_rejected",
        "identity_rejected",
        "quiescence_unproven",
        "state_rejected",
        "internal_unknown",
    }
)


class RuntimeControlError(RuntimeError):
    def __init__(self, code):
        self.code = code if code in _ERRORS else "internal_unknown"
        super().__init__("work_runtime_" + self.code)


def _fail(code):
    raise RuntimeControlError(code)


def _signature(value):
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read(path, limit=_MAX_FILE, *, dir_fd=None):
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=dir_fd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            _fail("kernel_unknown")
        chunks, count = [], 0
        while True:
            data = os.read(fd, min(8192, limit + 1 - count))
            if not data:
                break
            chunks.append(data)
            count += len(data)
            if count > limit:
                _fail("kernel_unknown")
        after = os.fstat(fd)
        named = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
        if _signature(before) != _signature(after) or _signature(after) != _signature(named):
            _fail("kernel_unknown")
        return b"".join(chunks), _signature(after)
    finally:
        os.close(fd)


def _json(raw):
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                _fail("request_rejected")
            result[key] = value
        return result

    return json.loads(
        raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=lambda _: _fail("request_rejected")
    )


def _binding():
    raw, signature = _read(_BINDING, 4096)
    try:
        value = _json(raw)
    except (RuntimeControlError, ValueError, UnicodeError):
        _fail("binding_rejected")
    if (
        type(value) is not dict
        or set(value) != {"schema", "commit", "artifact_sha256", "role", "unit", "nonce"}
        or type(value["schema"]) is not int
        or value["schema"] != 1
        or type(value["commit"]) is not str
        or not re.fullmatch("[0-9a-f]{40}", value["commit"])
        or type(value["artifact_sha256"]) is not str
        or not re.fullmatch("[0-9a-f]{64}", value["artifact_sha256"])
        or type(value["role"]) is not str
        or value["role"] not in {"main", "recovery"}
        or type(value["unit"]) is not str
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\.service", value["unit"])
        or type(value["nonce"]) is not str
        or not re.fullmatch("[0-9a-f]{32}", value["nonce"])
        or signature[3] != 0
        or signature[5] != 1
        or stat.S_IMODE(signature[2]) != 0o444
    ):
        _fail("binding_rejected")
    if value["unit"] not in {
        "stock-app.service",
        "prism-stage-" + value["commit"][:12] + ".service",
        "prism-new-runtime-stop-"
        + value["nonce"]
        + "-"
        + ("candidate" if value["role"] == "main" else "recovery")
        + ".service",
    }:
        _fail("binding_rejected")
    return value, signature


def _directory(path, *, uid, mode=None):
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        value = os.fstat(fd)
        named = os.stat(path, follow_symlinks=False)
        if (
            value.st_uid != uid
            or not stat.S_ISDIR(value.st_mode)
            or (mode is not None and stat.S_IMODE(value.st_mode) != mode)
            or (mode is None and value.st_mode & 0o022)
            or _signature(value) != _signature(named)
        ):
            _fail("socket_rejected")
        return fd, (value.st_dev, value.st_ino)
    except BaseException:
        os.close(fd)
        raise


def _ticks(pid):
    raw, _ = _read(f"/proc/{pid}/stat", 8192)
    text = raw.decode("ascii")
    left, separator, tail = text.rpartition(") ")
    if not separator or not left.startswith(str(pid) + " ("):
        _fail("kernel_unknown")
    columns = tail.split()
    if len(columns) < 20 or not columns[19].isdigit() or int(columns[19]) < 1:
        _fail("kernel_unknown")
    return int(columns[19])


def _process_group(pid):
    raw, _ = _read(f"/proc/{pid}/cgroup", 8192)
    lines = raw.decode("ascii").splitlines()
    if len(lines) != 1 or not lines[0].startswith("0::/"):
        _fail("kernel_unknown")
    group = lines[0][3:]
    if not re.fullmatch(r"/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", group) or any(
        item in {".", ".."} for item in group.split("/")
    ):
        _fail("kernel_unknown")
    return group


def _readonly_cgroup():
    raw, _ = _read("/proc/self/mountinfo")
    found = []
    for line in raw.decode("ascii").splitlines():
        before, separator, after = line.partition(" - ")
        left, right = before.split(), after.split()
        if not separator or len(left) < 6 or len(right) < 3:
            _fail("kernel_unknown")
        target = left[4]
        # Escaped alternatives cannot alias a hidden cgroup overlay unnoticed.
        if "\\" in target and ("cgroup" in target or target.startswith("/sys")):
            _fail("kernel_unknown")
        if target == "/sys/fs/cgroup":
            options = set(left[5].split(","))
            if left[3] != "/" or right[0] != "cgroup2" or "ro" not in options or "rw" in options:
                _fail("kernel_unknown")
            found.append((left[0], left[2], left[3], left[4], left[5], right[0]))
        elif target.startswith("/sys/fs/cgroup/"):
            # Even a read-only overlay could conceal part of the recursive tree.
            _fail("kernel_unknown")
    if len(found) != 1:
        _fail("kernel_unknown")
    return tuple(found)


class LinuxAdapter:
    """Read only fixed kernel paths. No request can choose a PID, path or proof."""

    def __init__(self):
        if sys.platform != "linux" or not hasattr(socket, "SO_PEERCRED") or os.geteuid() == 0:
            _fail("platform_rejected")
        run_fd, _ = _directory(_RUN, uid=0)
        os.close(run_fd)
        self.binding, self.binding_signature = _binding()
        self.revision, self.revision_signature = _read(_ARTIFACT / ".deploy-revision", 64)
        if (
            self.revision_signature[3] != 0
            or self.revision_signature[5] != 1
            or self.revision_signature[2] & 0o022
        ):
            _fail("binding_rejected")
        if self.revision not in {self.binding["commit"].encode(), (self.binding["commit"] + "\n").encode()}:
            _fail("binding_rejected")
        self.identity = self._identity()
        self.mounts = _readonly_cgroup()

    def _identity(self):
        pid = os.getpid()
        first = _ticks(pid)
        boot, _ = _read("/proc/sys/kernel/random/boot_id", 64)
        group = _process_group(pid)
        if group != "/system.slice/" + self.binding["unit"] or _ticks(pid) != first:
            _fail("runtime_identity_rejected")
        return RuntimeIdentity(
            boot.decode("ascii").strip(),
            pid,
            first,
            self.binding["commit"],
            self.binding["artifact_sha256"],
            group,
        )

    def verify(self):
        run_fd, _ = _directory(_RUN, uid=0)
        os.close(run_fd)
        binding, signature = _binding()
        revision, revision_signature = _read(_ARTIFACT / ".deploy-revision", 64)
        if (
            binding != self.binding
            or signature != self.binding_signature
            or revision != self.revision
            or revision_signature != self.revision_signature
            or self._identity() != self.identity
            or _readonly_cgroup() != self.mounts
        ):
            _fail("runtime_identity_rejected")

    def _inventory(self):
        fd = os.open(_CGROUP, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY)
        try:
            for component in self.identity.cgroup.strip("/").split("/"):
                following = os.open(
                    component, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=fd
                )
                os.close(fd)
                fd = following
            rows, directories, members = [], [], set()
            started = time.monotonic_ns()

            def visit(current, relative, depth):
                if (
                    len(directories) >= _MAX_GROUPS
                    or depth > 32
                    or time.monotonic_ns() - started > _SAMPLE_NS
                ):
                    _fail("kernel_unknown")
                before = os.fstat(current)
                directories.append((relative, before.st_dev, before.st_ino))
                raw, _ = _read("cgroup.procs", dir_fd=current)
                pids = raw.decode("ascii").splitlines()
                if any(not p.isdigit() or int(p) < 1 for p in pids):
                    _fail("kernel_unknown")
                processes = []
                group = self.identity.cgroup + ("/" + relative if relative != "." else "")
                for text in pids:
                    if time.monotonic_ns() - started > _SAMPLE_NS:
                        _fail("kernel_unknown")
                    pid = int(text)
                    if pid in members or len(members) >= _MAX_MEMBERS:
                        _fail("kernel_unknown")
                    ticks = _ticks(pid)
                    if _process_group(pid) != group or _ticks(pid) != ticks:
                        _fail("kernel_unknown")
                    processes.append((pid, ticks))
                    members.add(pid)
                rows.append((relative, tuple(sorted(processes))))
                names = sorted(os.listdir(current))
                for name in names:
                    item = os.stat(name, dir_fd=current, follow_symlinks=False)
                    if stat.S_ISLNK(item.st_mode):
                        _fail("kernel_unknown")
                    if not stat.S_ISDIR(item.st_mode):
                        if not stat.S_ISREG(item.st_mode):
                            _fail("kernel_unknown")
                        continue
                    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name in {".", ".."}:
                        _fail("kernel_unknown")
                    child = os.open(
                        name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=current
                    )
                    try:
                        opened = os.fstat(child)
                        if (opened.st_dev, opened.st_ino) != (item.st_dev, item.st_ino):
                            _fail("kernel_unknown")
                        visit(child, name if relative == "." else relative + "/" + name, depth + 1)
                        named = os.stat(name, dir_fd=current, follow_symlinks=False)
                        if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
                            _fail("kernel_unknown")
                    finally:
                        os.close(child)
                if names != sorted(os.listdir(current)) or (before.st_dev, before.st_ino) != (
                    os.fstat(current).st_dev,
                    os.fstat(current).st_ino,
                ):
                    _fail("kernel_unknown")

            visit(fd, ".", 0)
            if time.monotonic_ns() - started > _SAMPLE_NS:
                _fail("kernel_unknown")
            return tuple(rows), tuple(directories)
        finally:
            os.close(fd)

    def observe(self, manager):
        started = time.monotonic_ns()
        context = manager.observation_context()
        if context[0] != self.identity:
            _fail("runtime_identity_rejected")
        self.verify()
        first = self._inventory()
        second = self._inventory()
        self.verify()
        if (
            first != second
            or manager.observation_context() != context
            or time.monotonic_ns() - started > _SAMPLE_NS
        ):
            _fail("kernel_unknown")
        members = {item for _, processes in first[0] for item in processes}
        if members != {(self.identity.pid, self.identity.start_ticks)}:
            _fail("quiescence_unproven")
        return OSObservation(*context, time.monotonic_ns(), "yes", first[0])


def _expected(identity):
    return {key: getattr(identity, key) for key in ("commit", "boot_id", "pid", "start_ticks")}


def _peer_root(writer):
    endpoint = writer.get_extra_info("socket")
    if endpoint is None or not hasattr(socket, "SO_PEERCRED"):
        _fail("peer_rejected")
    pid, uid, gid = struct.unpack("3i", endpoint.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if pid < 1 or uid != 0 or gid < 0:
        _fail("peer_rejected")


class Runtime:
    def __init__(self, manager, adapter=None):
        self.manager, self.adapter = manager, adapter
        self.controlled = adapter is not None
        self.server = self.endpoint = self.endpoint_signature = None
        self.directory_fd = self.directory_identity = None
        self._writers = set()
        self._handlers = set()

    @property
    def identity(self):
        return self.manager.identity

    def _reply(self, error=None):
        return {
            "schema": 1,
            "ok": error is None,
            "error": error,
            "controlled": self.controlled,
            "identity": asdict(self.manager.identity) if self.manager.identity else None,
            "ticket": asdict(self.manager.ticket()),
            "snapshot": self.manager.observe(),
        }

    def dispatch(self, request):
        if not self.controlled:
            _fail("state_rejected")
        if type(request) is not dict or type(request.get("schema")) is not int or request["schema"] != 1:
            _fail("request_rejected")
        action = request.get("action")
        if type(action) is not str:
            _fail("request_rejected")
        keys = {"schema", "action", "expected"} | ({"ticket"} if action != "status" else set())
        if action not in {"status", "fence", "seal", "resume"} or set(request) != keys:
            _fail("request_rejected")
        expected = request["expected"]
        if (
            type(expected) is not dict
            or expected != _expected(self.manager.identity)
            or any(
                type(expected.get(key)) is not type(value)
                for key, value in _expected(self.manager.identity).items()
            )
        ):
            _fail("identity_rejected")
        try:
            self.adapter.verify()
        except RuntimeControlError:
            raise
        except Exception:
            _fail("kernel_unknown")
        if action == "status":
            return self._reply()
        ticket = request["ticket"]
        if (
            type(ticket) is not dict
            or set(ticket) != {"epoch", "nonce"}
            or type(ticket["epoch"]) is not int
            or type(ticket["nonce"]) is not str
            or ticket != asdict(self.manager.ticket())
        ):
            _fail("ticket_rejected")
        command = Ticket(**ticket)
        try:
            if action == "fence":
                self.manager.begin_drain(command)
            elif action == "seal":
                snapshot = self.manager.observe()
                if (
                    snapshot["state"] != "draining"
                    or snapshot["producer_unknown"]
                    or any(snapshot["leases"].values())
                ):
                    _fail("quiescence_unproven")
                self.manager.record_os_observation(self.adapter.observe(self.manager))
                self.manager.seal(command)
            else:
                self.manager.resume(command)
        except LifecycleError:
            _fail("quiescence_unproven")
        except RuntimeControlError:
            raise
        except Exception:
            _fail("kernel_unknown")
        return self._reply()

    async def _handle(self, reader, writer):
        task = asyncio.current_task()
        self._writers.add(writer)
        self._handlers.add(task)
        try:
            await self._serve(reader, writer)
        finally:
            writer.close()
            try:
                with suppress(Exception):
                    await asyncio.wait_for(writer.wait_closed(), 2)
            finally:
                self._writers.discard(writer)
                self._handlers.discard(task)

    async def _serve(self, reader, writer):
        try:
            _peer_root(writer)  # Authenticate before reading even the length header.
        except Exception:
            return
        try:
            header = await asyncio.wait_for(reader.readexactly(4), 2)
            length = struct.unpack("!I", header)[0]
            if not 1 <= length <= _MAX_FRAME:
                _fail("request_rejected")
            raw = await asyncio.wait_for(reader.readexactly(length), 2)
            result = self.dispatch(_json(raw))
        except RuntimeControlError as exc:
            result = self._reply(exc.code)
        except TimeoutError:
            result = self._reply("request_timeout")
        except (ValueError, UnicodeError, asyncio.IncompleteReadError):
            result = self._reply("request_rejected")
        except Exception:
            result = self._reply("internal_unknown")
        try:
            writer.write(json.dumps(result, sort_keys=True, separators=(",", ":")).encode() + b"\n")
            await asyncio.wait_for(writer.drain(), 2)
        except Exception:
            pass

    async def listen(self):
        directory = _RUN / ("prism-work-" + self.adapter.binding["nonce"])
        self.directory_fd, self.directory_identity = _directory(directory, uid=os.geteuid(), mode=0o700)
        if os.listdir(self.directory_fd):
            os.close(self.directory_fd)
            self.directory_fd = None
            _fail("socket_rejected")
        self.endpoint = directory / "control.sock"
        endpoint = None
        try:
            endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            # Native bind fails on an existing file; asyncio's path API may unlink it.
            endpoint.bind(str(self.endpoint))
            value = os.stat(self.endpoint, follow_symlinks=False)
            if not stat.S_ISSOCK(value.st_mode) or value.st_uid != os.geteuid() or value.st_nlink != 1:
                _fail("socket_rejected")
            self.endpoint_signature = (value.st_dev, value.st_ino)
            check_fd, check_identity = _directory(directory, uid=os.geteuid(), mode=0o700)
            try:
                if check_identity != self.directory_identity:
                    _fail("socket_rejected")
            finally:
                os.close(check_fd)
            endpoint.listen(8)
            endpoint.setblocking(False)
            self.server = await asyncio.start_unix_server(self._handle, sock=endpoint, limit=_MAX_FRAME + 4)
        except BaseException:
            if endpoint is not None:
                endpoint.close()
            self._unlink_owned()
            raise

    def _unlink_owned(self):
        if self.directory_fd is None:
            return
        try:
            directory = os.stat(self.endpoint.parent, follow_symlinks=False)
            if (directory.st_dev, directory.st_ino) != self.directory_identity:
                _fail("socket_rejected")
            if self.endpoint_signature is not None:
                value = os.stat("control.sock", dir_fd=self.directory_fd, follow_symlinks=False)
                if (
                    not stat.S_ISSOCK(value.st_mode)
                    or (value.st_dev, value.st_ino) != self.endpoint_signature
                ):
                    _fail("socket_rejected")
                os.unlink("control.sock", dir_fd=self.directory_fd)
                self.endpoint_signature = None
        finally:
            os.close(self.directory_fd)
            self.directory_fd = None

    async def aclose(self):
        try:
            if self.controlled:
                snapshot = self.manager.observe()
                if snapshot["state"] != "quiescent" or any(snapshot["leases"].values()):
                    _fail("quiescence_unproven")
                self.manager.record_os_observation(self.adapter.observe(self.manager))
                self.manager.close()
            else:
                if self.manager.identity is not None:
                    _fail("quiescence_unproven")
                if self.manager.observe()["state"] == "running":
                    self.manager.begin_drain(self.manager.ticket())
                # A timeout only checks for blocked work; it never ends ownership.
                # Cancelling this waiter leaves all actual work registered.
                while not await self.manager.wait_drained(timeout=0.05):
                    if self.manager.observe()["blocked"]:
                        _fail("quiescence_unproven")
                self.manager.close_development()
        except LifecycleError:
            _fail("quiescence_unproven")
        except RuntimeControlError:
            raise
        except Exception:
            _fail("kernel_unknown")
        try:
            if self.server is not None:
                self.server.close()
                for writer in tuple(self._writers):
                    writer.close()
                await asyncio.wait_for(self.server.wait_closed(), 2)
                if self._handlers:
                    _, pending = await asyncio.wait(tuple(self._handlers), timeout=2)
                    if pending:
                        _fail("socket_rejected")
                self.server = None
            self._unlink_owned()
        except RuntimeControlError:
            raise
        except Exception:
            _fail("socket_rejected")


async def open_runtime(*, producers):
    """Lifespan: declare exact producers, bind returned manager, then yield.

    Controlled startup is held (draining), so root must status -> seal -> resume.
    Shutdown requires a fresh explicit seal; unknown work never gets cancelled.
    Development shutdown awaits actual drain before closing, without Linux proof.
    """
    if (
        type(producers) is not dict
        or set(producers) != KINDS
        or any(type(v) is not bool for v in producers.values())
    ):
        _fail("binding_rejected")
    controlled = os.environ.get("PRISM_WORK_CONTROL") == "1"
    try:
        adapter = LinuxAdapter() if controlled else None
        manager = Manager(adapter.identity if adapter else None)
        for kind, enabled in producers.items():
            manager.declare(kind, enabled=enabled)
        runtime = Runtime(manager, adapter)
        if controlled:
            manager.begin_drain(manager.ticket())
            await runtime.listen()
        return runtime
    except RuntimeControlError:
        raise
    except Exception:
        _fail("binding_rejected")
