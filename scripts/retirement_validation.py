"""Root-only orchestration for isolated synthetic retirement validation.

Not an entry point. A reviewed fixed controller supplies fresh boundary permits,
a bounded command runner, and its private append-only journal. Only randomly
named synthetic units receive one signal. Production is inspected for identity
only. This proves a signal path, never systemctl-stop or production quiescence.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import socket
import stat
import struct
import time
from contextlib import suppress
from pathlib import Path

from scripts import retirement_probe as probe

PROBE_SOURCE_SHA256 = "1e70d84879e9b217bb7b83ba5ece8947b4f11b201a48ef33976ed6207c423971"
PRODUCTION_UNIT = "stock-app.service"
PROPERTIES = (
    "LoadState,ActiveState,SubState,MainPID,ExecMainPID,InvocationID,ControlGroup,"
    "ExecMainCode,ExecMainStatus,ExecMainExitTimestampMonotonic,Result,Transient"
)
NATURAL_DEADLINE_SECONDS = 45
CLEANUP_SECONDS = 55


class RetirementValidationError(RuntimeError):
    """Only fixed non-sensitive codes may cross the controller boundary."""


def reject(code):
    raise RetirementValidationError(code)


WRAPPER_TEMPLATE = r"""
import asyncio
import errno
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import runpy
import socket
import stat
import sys
import time

def main():
    if len(sys.argv)!=6 or sys.argv[1]!='serve':raise SystemExit(2)
    _,_,profile,nonce,binding,bundle=sys.argv
    if not re.fullmatch('[0-9a-f]{32}',nonce) or not re.fullmatch('[0-9a-f]{64}',binding) or bundle!='__BUNDLE__':raise SystemExit(2)
    root=Path.cwd()
    if sys.platform!='linux' or sys.version_info[:2]!=(3,12) or root.parent!=Path('/run') or not root.name.startswith('prism-retirement-probe-'+nonce+'-'):raise SystemExit(2)
    uid=os.geteuid()
    forbidden={0}
    for name in ('ubuntu','prism-stage','prism-integration'):
        try:forbidden.add(pwd.getpwnam(name).pw_uid)
        except KeyError:pass
    info=root.lstat()
    if uid in forbidden or not stat.S_ISDIR(info.st_mode) or info.st_uid!=uid or stat.S_IMODE(info.st_mode)!=0o700:raise SystemExit(2)
    os.umask(0o077)
    os.environ.clear()
    os.environ.update({'PATH':'/usr/bin:/bin','HOME':str(root),'TMPDIR':str(root),'LANG':'C.UTF-8'})
    def mark(name,**fields):
        value={'nonce':nonce,'binding_sha256':binding,'case_profile':profile,'at_ns':time.monotonic_ns(),**fields}
        target=root/(name+'.json'); pending=root/(name+'.pending')
        if target.exists() or target.is_symlink():raise RuntimeError('duplicate_validation_marker')
        fd=os.open(pending,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as stream:
            json.dump(value,stream,sort_keys=True);stream.flush();os.fsync(stream.fileno())
        os.rename(pending,target)
    hidden=True
    for path in ('/home/ubuntu/stock-analyzer','/home/ubuntu/Vibe-Trading','/root','/var/lib/prism','/etc/prism','/opt/prism/releases','/var/lib/prism-operations'):
        try:
            fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        except OSError as exc:
            hidden &= exc.errno in (errno.EACCES,errno.EPERM,errno.ENOENT)
        else:
            os.close(fd);hidden=False
    sockets_denied=True
    for family in (socket.AF_INET,socket.AF_INET6):
        try:connection=socket.socket(family,socket.SOCK_STREAM)
        except OSError as exc:sockets_denied &= exc.errno in (errno.EACCES,errno.EPERM,errno.EAFNOSUPPORT)
        else:connection.close();sockets_denied=False
    readonly=False
    forbidden_write=Path('/run')/('prism-retirement-denied-'+nonce)
    try:fd=os.open(forbidden_write,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    except OSError as exc:readonly=exc.errno==errno.EROFS
    else:os.close(fd);forbidden_write.unlink()
    endpoints_hidden=not any(Path(path).exists() for path in ('/run/systemd/private','/run/dbus/system_bus_socket'))
    mark('validation_isolation',uid_nonroot=True,production_paths_hidden=hidden,inet_sockets_denied=sockets_denied,run_readonly=readonly,runtime_endpoints_hidden=endpoints_hidden)
    if not all((hidden,sockets_denied,readonly,endpoints_hidden)):raise SystemExit(2)
    fixture=Path('/opt/prism/validation')/bundle/'fixture.py'
    fd=os.open(fixture,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as stream:
        st=os.fstat(stream.fileno())
        if st.st_uid!=0 or st.st_mode&0o222 or st.st_nlink!=1 or hashlib.file_digest(stream,'sha256').hexdigest()!='__FIXTURE__':raise SystemExit(2)
    sys.path.insert(0,str(fixture.parent/'vendor'))
    import uvicorn
    original=uvicorn.Server.serve
    async def bounded_serve(server,*args,**kwargs):
        def natural_deadline():
            mark('validation_deadline',expired=True)
            server.should_exit=True
        timer=asyncio.get_running_loop().call_later(45,natural_deadline)
        try:return await original(server,*args,**kwargs)
        finally:
            timer.cancel()
            mark('validation_server_finished',finished=True)
    uvicorn.Server.serve=bounded_serve
    sys.argv[0]=str(fixture)
    runpy.run_path(str(fixture),run_name='__main__')

if __name__=='__main__':main()
"""


def wrapper_source():
    return (
        WRAPPER_TEMPLATE.replace("__BUNDLE__", probe.bundle_identity())
        .replace("__FIXTURE__", probe.sha256(probe.FIXTURE_SOURCE.encode()))
        .encode()
    )


def wrapper_directory():
    return probe.BUNDLE_ROOT / probe.sha256(wrapper_source())


def _stable(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _parent(path, *, trusted=False):
    """Open every ancestor without following links, including inherited sources."""
    path = Path(path)
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        reject("retirement_path_rejected")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory
            )
            os.close(directory)
            directory = child
            info = os.fstat(directory)
            if trusted and (info.st_uid != 0 or info.st_mode & 0o022):
                reject("retirement_target_ancestor_rejected")
        return directory, path.name
    except BaseException:
        os.close(directory)
        raise


def read_data(path, *, maximum=32 * 1024 * 1024, trusted=False):
    directory, name = _parent(path, trusted=trusted)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        first = os.fstat(fd)
        if not stat.S_ISREG(first.st_mode) or first.st_nlink != 1 or first.st_size > maximum:
            reject("retirement_data_file_rejected")
        if trusted and (first.st_uid != 0 or first.st_mode & 0o022):
            reject("retirement_data_owner_rejected")
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum or _stable(first) != _stable(os.fstat(fd)):
            reject("retirement_data_changed")
        return raw, first
    finally:
        os.close(fd)


def _trusted_directory(path, mode):
    """Create only the fixed root-controlled validation hierarchy."""
    directory, name = _parent(path, trusted=True)
    try:
        with suppress(FileExistsError):
            os.mkdir(name, mode=mode, dir_fd=directory)
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if info.st_uid != 0 or info.st_mode & 0o022:
                reject("retirement_target_owner_rejected")
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def _new_file(path, content):
    directory, name = _parent(path, trusted=True)
    try:
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory,
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o444)
    finally:
        os.close(directory)


def _seal(directory):
    for root, folders, _ in os.walk(directory, topdown=False, followlinks=False):
        for path in [Path(root), *(Path(root) / part for part in folders)]:
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
                reject("retirement_bundle_directory_rejected")
            path.chmod(0o555)


def install_bundles(plan, boundary, record):
    """Copy only pinned public bytes. Never execute inherited packages as root."""
    probe._require_linux()
    probe._case(plan, probe.CASES[0][0])
    raw, _ = read_data(Path(probe.__file__), trusted=True)
    if probe.sha256(raw) != PROBE_SOURCE_SHA256:
        reject("retirement_probe_source_changed")
    boundary("retirement_copy_public_libraries")
    for directory in (Path("/opt/prism"), probe.BUNDLE_ROOT):
        _trusted_directory(directory, 0o755)
    destination = Path(plan["bundle_directory"])
    if destination.exists() or destination.is_symlink():
        probe.verify_bundle(plan)
    else:
        staging = probe.BUNDLE_ROOT / (".building-" + plan["bundle_sha256"] + "-" + plan["nonce"])
        _trusted_directory(staging, 0o700)
        if any(staging.iterdir()):
            reject("retirement_partial_bundle_exists")
        record("bundle_copy_intent", {"bundle_sha256": plan["bundle_sha256"]})
        _trusted_directory(staging / "vendor", 0o700)
        for item in probe.package_manifest():
            source = probe.SOURCE_ROOT / item["path"]
            data, _ = read_data(source)
            if len(data) != item["size"] or probe.sha256(data) != item["sha256"]:
                reject("retirement_inherited_source_changed")
            target = staging / "vendor" / item["path"]
            for parent in reversed(target.parent.parents):
                if parent != staging and staging in parent.parents:
                    _trusted_directory(parent, 0o700)
            _trusted_directory(target.parent, 0o700)
            _new_file(target, data)
        _new_file(staging / "fixture.py", probe.FIXTURE_SOURCE.encode())
        _seal(staging)
        if destination.exists() or destination.is_symlink():
            reject("retirement_bundle_appeared")
        os.rename(staging, destination)
        probe.verify_bundle(plan)
    helper = wrapper_directory()
    if helper.exists() or helper.is_symlink():
        _verify_wrapper()
    else:
        _trusted_directory(helper, 0o700)
        _new_file(helper / "wrapper.py", wrapper_source())
        _seal(helper)
        _verify_wrapper()
    record("public_bundles_verified", {"bundle_sha256": plan["bundle_sha256"], "wrapper_sha256": helper.name})


def _verify_wrapper():
    helper = wrapper_directory()
    for path in (helper, *helper.parents):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            reject("retirement_wrapper_directory_rejected")
    if set(path.name for path in helper.iterdir()) != {"wrapper.py"}:
        reject("retirement_wrapper_files_rejected")
    info = helper.lstat()
    raw, file_info = read_data(helper / "wrapper.py", trusted=True)
    if (
        stat.S_IMODE(info.st_mode) != 0o555
        or stat.S_IMODE(file_info.st_mode) != 0o444
        or raw != wrapper_source()
    ):
        reject("retirement_wrapper_source_changed")


def launch_command(plan, case_id):
    case = probe._case(plan, case_id)
    properties = dict(case["properties"])
    runtime = case["runtime_directory"]
    properties.update(
        RemainAfterExit="yes",
        TemporaryFileSystem="/run:ro",
        BindPaths=runtime + ":" + runtime,
        ReadOnlyPaths=properties["ReadOnlyPaths"] + " " + str(wrapper_directory()),
        InaccessiblePaths=properties["InaccessiblePaths"] + " /var/lib/prism-operations",
    )
    command = list(case["command"])
    expected = str(Path(plan["bundle_directory"]) / "fixture.py")
    if command.count(expected) != 1:
        reject("retirement_fixed_command_rejected")
    command[command.index(expected)] = str(wrapper_directory() / "wrapper.py")
    return [
        "/usr/bin/systemd-run",
        "--quiet",
        "--unit=" + case["unit"],
        *("--property=" + key + "=" + value for key, value in sorted(properties.items())),
        "--",
        *command,
    ]


def _expected_argv(plan, case_id):
    command = launch_command(plan, case_id)
    payload = command[command.index("--") + 1 :]
    python = payload.index("/usr/bin/python3")
    return b"\0".join(argument.encode() for argument in payload[python:]) + b"\0"


def no_existing_synthetic_workloads(plan):
    """An interrupted prior run is observed, never silently replaced or killed."""
    probe._require_linux()
    if not (probe.CGROUP_ROOT / "cgroup.controllers").is_file():
        reject("retirement_cgroup_v2_required")
    for path in (probe.CGROUP_ROOT / "system.slice").iterdir():
        if not path.name.startswith("prism-retirement-probe-"):
            continue
        match = re.fullmatch(r"prism-retirement-probe-([0-9a-f]{32})-([a-z-]+)\.service", path.name)
        if not match or match[2] not in {case[0] for case in probe.CASES}:
            reject("retirement_unknown_synthetic_group")
        previous = probe.descriptor(plan["bindings"], nonce=match[1])
        state = probe.recursive_cgroup_state(previous, match[2])
        if not state["observed"] or not state["empty"]:
            reject("retirement_previous_workload_active")
    return True


def _number(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value):
        reject("retirement_systemd_number_rejected")
    return int(value)


def _uids(pid):
    raw, _ = probe._read_regular(Path("/proc") / str(pid) / "status", 128 * 1024)
    rows = [line for line in raw.splitlines() if line.startswith(b"Uid:")]
    if len(rows) != 1 or not re.fullmatch(rb"Uid:[ \t]+[0-9]+[ \t]+[0-9]+[ \t]+[0-9]+[ \t]+[0-9]+", rows[0]):
        reject("retirement_uid_status_rejected")
    values = tuple(int(value) for value in rows[0].split()[1:])
    if len(set(values)) != 1:
        reject("retirement_uid_mismatch")
    return values[0]


def _isolation(plan, case_id):
    case = probe._case(plan, case_id)
    root = Path(case["runtime_directory"])
    info = root.lstat()
    forbidden = {0}
    for name in probe.FORBIDDEN_USERS:
        with suppress(KeyError):
            forbidden.add(pwd.getpwnam(name).pw_uid)
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid in forbidden:
        reject("retirement_runtime_owner_rejected")
    raw, marker = probe._read_regular(root / "validation_isolation.json", 8192)
    data = json.loads(raw)
    required = {
        "nonce",
        "binding_sha256",
        "case_profile",
        "at_ns",
        "uid_nonroot",
        "production_paths_hidden",
        "inet_sockets_denied",
        "run_readonly",
        "runtime_endpoints_hidden",
    }
    if (
        type(data) is not dict
        or set(data) != required
        or marker.st_uid != info.st_uid
        or stat.S_IMODE(marker.st_mode) != 0o600
        or data["nonce"] != plan["nonce"]
        or data["binding_sha256"] != plan["binding_sha256"]
        or data["case_profile"] != case["profile"]
        or type(data["at_ns"]) is not int
        or data["at_ns"] <= 0
        or any(
            data[name] is not True for name in required - {"nonce", "binding_sha256", "case_profile", "at_ns"}
        )
    ):
        reject("retirement_actual_isolation_rejected")
    return info.st_uid


def _mounts(pid, runtime):
    raw, _ = probe._read_regular(Path("/proc") / str(pid) / "mountinfo", 1024 * 1024)
    mounts = {}
    for line in raw.splitlines():
        head, separator, tail = line.partition(b" - ")
        before, after = head.split(), tail.split()
        if not separator or len(before) < 6 or len(after) < 3:
            reject("retirement_mountinfo_rejected")
        if before[4] in {b"/run", runtime.encode()}:
            mounts[before[4]] = (set(before[5].split(b",")), after[0])
    if (
        b"/run" not in mounts
        or b"ro" not in mounts[b"/run"][0]
        or mounts[b"/run"][1] != b"tmpfs"
        or runtime.encode() not in mounts
        or b"rw" not in mounts[runtime.encode()][0]
        or os.stat(f"/proc/{pid}/ns/mnt").st_ino == os.stat("/proc/self/ns/mnt").st_ino
    ):
        reject("retirement_actual_mounts_rejected")
    host, inside = Path(runtime).stat(), (Path("/proc") / str(pid) / "root" / runtime.lstrip("/")).stat()
    if (host.st_dev, host.st_ino, host.st_uid) != (inside.st_dev, inside.st_ino, inside.st_uid):
        reject("retirement_runtime_bind_mismatch")


def _deadline(case):
    path = Path(case["runtime_directory"]) / "validation_deadline.json"
    return path.exists() or path.is_symlink()


def server_finished(plan, case_id, signal_at):
    """Serve must unwind before opening work: otherwise executor-join is unproven."""
    case = probe._case(plan, case_id)
    marker = Path(case["runtime_directory"]) / "validation_server_finished.json"
    if not marker.exists() and not marker.is_symlink():
        return False
    uid = _isolation(plan, case_id)
    raw, info = probe._read_regular(marker, 8192)
    data = json.loads(raw)
    if (
        type(data) is not dict
        or set(data) != {"nonce", "binding_sha256", "case_profile", "at_ns", "finished"}
        or data["nonce"] != plan["nonce"]
        or data["binding_sha256"] != plan["binding_sha256"]
        or data["case_profile"] != case["profile"]
        or data["finished"] is not True
        or type(data["at_ns"]) is not int
        or data["at_ns"] < signal_at * 1000
        or info.st_uid != uid
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        reject("retirement_server_unwind_rejected")
    return True


def _open_gate(plan, case_id):
    """No permit is needed to release only this run's synthetic work on cleanup."""
    case = probe._case(plan, case_id)
    _isolation(plan, case_id)
    directory = os.open(case["runtime_directory"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fd = os.open(
                "release-work", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
            )
        except FileExistsError:
            info = os.stat("release-work", dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1:
                reject("retirement_gate_rejected")
        else:
            os.fsync(fd)
            os.close(fd)
    finally:
        os.close(directory)


def _request_fixture(case, uid, pid):
    path = Path(case["socket_path"])
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != uid:
        reject("retirement_socket_rejected")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        connection.connect(str(path))
        peer_pid, peer_uid, _ = struct.unpack(
            "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        )
        if peer_pid != pid or peer_uid != uid:
            reject("retirement_fixture_peer_rejected")
        connection.sendall(b"GET /fixture HTTP/1.1\r\nHost: fixture\r\nConnection: close\r\n\r\n")
        body = b""
        expected = b"data: done\n\n" if case["profile"] == "post_complete" else b"data: started\n\n"
        while expected not in body:
            chunk = connection.recv(4096)
            if not chunk or len(body) + len(chunk) > 16384:
                reject("retirement_fixture_response_rejected")
            body += chunk
        if not body.startswith(b"HTTP/1.1 200 "):
            reject("retirement_fixture_http_rejected")


class Validation:
    def __init__(self, request, *, boundary, run, record, monotonic=time.monotonic, sleep=time.sleep):
        self.plan = probe.descriptor(request)
        self.boundary, self.run, self.record = boundary, run, record
        self.monotonic, self.sleep = monotonic, sleep
        self.production = None
        self.active_case = None
        self.signals = set()

    def fixed_case(self, case):
        if type(case) is not dict or case != probe._case(self.plan, case.get("case_id")):
            reject("retirement_case_descriptor_changed")
        return case

    def show(self, unit):
        if unit != PRODUCTION_UNIT and unit not in {case["unit"] for case in self.plan["cases"]}:
            reject("retirement_host_unit_rejected")
        raw = self.run(["/usr/bin/systemctl", "show", "--property=" + PROPERTIES, unit], timeout=10)
        if not isinstance(raw, bytes) or len(raw) > 16384:
            reject("retirement_systemd_output_rejected")
        rows = [line.split("=", 1) for line in raw.decode("ascii").splitlines() if "=" in line]
        if len({row[0] for row in rows}) != len(rows):
            reject("retirement_systemd_properties_rejected")
        return dict(rows)

    def production_identity(self):
        state = self.show(PRODUCTION_UNIT)
        pid = _number(state.get("MainPID"))
        if (
            state.get("ActiveState") != "active"
            or state.get("SubState") != "running"
            or pid <= 1
            or not re.fullmatch(r"[0-9a-f]{32}", state.get("InvocationID", ""))
            or state.get("ControlGroup") != "/system.slice/stock-app.service"
            or _uids(pid) != pwd.getpwnam("ubuntu").pw_uid
        ):
            reject("retirement_production_identity_rejected")
        raw, _ = probe._read_regular(Path("/proc") / str(pid) / "stat", 8192)
        return {"pid": pid, "start_ticks": probe._start_ticks(raw, pid), "invocation": state["InvocationID"]}

    def check_production(self):
        if self.production_identity() != self.production:
            reject("retirement_production_identity_changed")

    def poll(self, function, seconds):
        deadline = self.monotonic() + seconds
        while True:
            value = function()
            if value:
                return value
            if self.monotonic() >= deadline:
                reject("retirement_observation_deadline")
            self.sleep(0.05)

    def live(self, case):
        state = self.show(case["unit"])
        pid = _number(state.get("MainPID"))
        if pid <= 1:
            return None
        if (
            state.get("Transient") != "yes"
            or state.get("ControlGroup") != case["control_group"]
            or not re.fullmatch(r"[0-9a-f]{32}", state.get("InvocationID", ""))
            or state.get("ActiveState") != "active"
            or state.get("SubState") != "running"
        ):
            reject("retirement_fixture_unit_rejected")
        identity = probe.linux_process_identity(self.plan, case["case_id"], pid)
        if not all(identity[key] for key in ("uid_isolated", "private_network", "cgroup_bound")):
            reject("retirement_fixture_identity_rejected")
        uid = _uids(pid)
        arguments, _ = probe._read_regular(Path("/proc") / str(pid) / "cmdline", 8192)
        if arguments != _expected_argv(self.plan, case["case_id"]):
            reject("retirement_fixture_argv_rejected")
        if uid != _isolation(self.plan, case["case_id"]):
            reject("retirement_fixture_uid_changed")
        for member in identity["cgroup"]["members"]:
            if _uids(member) != uid:
                reject("retirement_descendant_uid_rejected")
        _mounts(pid, case["runtime_directory"])
        binary = Path("/usr/bin/python3.12").stat()
        actual = (Path("/proc") / str(pid) / "exe").stat()
        if (
            (binary.st_dev, binary.st_ino) != (actual.st_dev, actual.st_ino)
            or binary.st_uid != 0
            or binary.st_mode & 0o022
        ):
            reject("retirement_system_python_rejected")
        return {
            "pid": pid,
            "start_ticks": identity["start_ticks"],
            "invocation": state["InvocationID"],
            "uid": uid,
        }

    def _ready(self, case):
        root = Path(case["runtime_directory"])
        needed = ("validation_isolation.json", "fixture_ready.json", "server.sock")
        if not all((root / name).exists() for name in needed):
            return None
        return self.live(case)

    def exit_state(self, case, live, signal_at):
        state = self.show(case["unit"])
        if (
            state.get("InvocationID") != live["invocation"]
            or _number(state.get("ExecMainPID")) != live["pid"]
        ):
            reject("retirement_invocation_changed")
        if _number(state.get("MainPID")) != 0:
            return None
        if state.get("ActiveState") not in {"active", "inactive", "failed"} or state.get("SubState") not in {
            "exited",
            "dead",
            "failed",
        }:
            reject("retirement_exit_state_rejected")
        exit_us = _number(state.get("ExecMainExitTimestampMonotonic"))
        if exit_us < signal_at:
            reject("retirement_exit_before_signal")
        group = probe.recursive_cgroup_state(
            self.plan, case["case_id"], stopped=True, previously_observed=True
        )
        return state, group

    def _empty(self, case):
        group = probe.recursive_cgroup_state(
            self.plan, case["case_id"], stopped=True, previously_observed=True
        )
        return group if group["empty"] else None

    def _retire_empty_unit(self, case, invocation):
        self.fixed_case(case)
        state = self.show(case["unit"])
        if (
            state.get("InvocationID") != invocation
            or _number(state.get("MainPID")) != 0
            or (state.get("ActiveState"), state.get("SubState"))
            not in {("active", "exited"), ("inactive", "dead"), ("failed", "failed")}
            or not self._empty(case)
        ):
            reject("retirement_empty_unit_required")
        # MainPID is zero and recursively no process exists: this cannot send a
        # second workload signal. Never use stop to clean an unknown/live unit.
        self.run(["/usr/bin/systemctl", "stop", "--no-block", case["unit"]], timeout=10)

        def inactive():
            state = self.show(case["unit"])
            if state.get("LoadState") == "not-found":
                return True
            return state.get("ActiveState") == "inactive" and _number(state.get("MainPID")) == 0

        return self.poll(inactive, 5)

    def case(self, case):
        self.fixed_case(case)
        case_id = case["case_id"]
        if case_id in self.signals:
            reject("retirement_signal_already_sent")
        self.check_production()
        self.boundary("retirement_start_" + case_id)
        runtime = Path(case["runtime_directory"])
        if runtime.exists() or runtime.is_symlink():
            reject("retirement_runtime_already_exists")
        self.record("synthetic_start_intent", {"case_id": case_id, "nonce": self.plan["nonce"]})
        self.active_case = case
        self.run(launch_command(self.plan, case_id), timeout=15)
        live = self.poll(lambda: self._ready(case), 5)
        self.record(
            "actual_isolation_verified",
            {
                "case_id": case_id,
                "invocation_id": live["invocation"],
                "main_pid": live["pid"],
                "start_ticks": live["start_ticks"],
                "uid": live["uid"],
                "control_group": case["control_group"],
                "actual_private_network_and_mounts": True,
                "runtime_bind_same_inode": True,
                "production_directory_access_denied": True,
                "inet_socket_creation_denied": True,
            },
        )
        _request_fixture(case, live["uid"], live["pid"])

        def phase_ready():
            markers = probe.read_markers(self.plan, case_id)
            return (
                markers
                if all(
                    markers[key] for key in ("fixture_ready", "stream_finished", case["start_phase_marker"])
                )
                else None
            )

        before = self.poll(phase_ready, 5)
        gate = runtime / "release-work"
        if gate.exists() or gate.is_symlink() or _deadline(case):
            reject("retirement_gate_not_closed")
        self.boundary("retirement_signal_" + case_id)
        self.check_production()
        if self.live(case) != live:
            reject("retirement_fixture_changed_before_signal")
        before = probe.read_markers(self.plan, case_id)
        unfinished = {
            "async_task": "async_saved",
            "daemon_thread": "daemon_saved",
            "orphan_child": "child_finished",
        }.get(case["profile"], "post_saved")
        if (
            gate.exists()
            or gate.is_symlink()
            or _deadline(case)
            or before.get("fixture_gate_expired", False)
            or before.get(unfinished, False)
        ):
            reject("retirement_work_changed_before_signal")
        if case_id in self.signals:
            reject("retirement_signal_already_sent")
        signal_at = int(self.monotonic() * 1_000_000)
        self.record(
            "synthetic_signal_intent",
            {
                "case_id": case_id,
                "invocation_id": live["invocation"],
                "signal": case["signal"],
                "signal_at_monotonic_us": signal_at,
            },
        )
        self.signals.add(case_id)  # Uncertain dispatch is never retried.
        self.run(
            ["/usr/bin/systemctl", "kill", "--kill-whom=main", "--signal=" + case["signal"], case["unit"]],
            timeout=10,
        )
        if case["expectation"] == "executor_preserved":
            self.poll(lambda: server_finished(self.plan, case_id, signal_at), case["observe_seconds"])
            if (
                self.live(case) != live
                or _deadline(case)
                or probe.read_markers(self.plan, case_id).get("post_saved", False)
            ):
                reject("retirement_executor_exited_before_gate")
            self.record(
                "synthetic_runner_join_observed",
                {"case_id": case_id, "server_unwound_with_executor_pending": True},
            )
            _open_gate(self.plan, case_id)
        exited, at_exit = self.poll(lambda: self.exit_state(case, live, signal_at), case["observe_seconds"])
        self.record(
            "synthetic_exit_observed",
            {
                "case_id": case_id,
                "recursive_member_count": at_exit["member_count"],
                "exit_code": _number(exited.get("ExecMainCode")),
                "exit_status": _number(exited.get("ExecMainStatus")),
            },
        )
        _open_gate(self.plan, case_id)
        self.poll(lambda: self._empty(case), case["natural_cleanup_seconds"])
        markers = probe.read_markers(self.plan, case_id)
        inactive = self._retire_empty_unit(case, live["invocation"])
        observation = {
            "linux": True,
            "source_bound": True,
            "uid_isolated": True,
            "private_network": True,
            "cgroup_bound": True,
            "same_invocation": True,
            "exit_after_signal": True,
            "inactive": inactive,
            "result_success": exited.get("Result") == "success",
            "exec_main_code": _number(exited.get("ExecMainCode")),
            "exec_main_status": _number(exited.get("ExecMainStatus")),
            "signals_sent": 1,
            "deadline_expired": _deadline(case),
            "cgroup_observed": at_exit["observed"],
            "cgroup_empty": at_exit["empty"],
            "cgroup_member_count": at_exit["member_count"],
            "cleanup_cgroup_empty": True,
            "markers": markers,
            "markers_before_signal": before,
            "gate_closed_before_signal": True,
        }
        result = probe.evaluate_case(self.plan, case_id, observation)
        self.check_production()
        self.record("synthetic_case_result", result)
        self.active_case = None
        return result

    def cleanup(self):
        """Release synthetic work; never kill, restart, or retry a signal."""
        case = self.active_case
        if case is None:
            return True
        try:
            self.fixed_case(case)
            # Startup can fail before publishing its isolation marker. Do not
            # abandon read-only observation of the bounded wrapper.
            with suppress(Exception):
                _open_gate(self.plan, case["case_id"])
            deadline = self.monotonic() + CLEANUP_SECONDS
            while self.monotonic() < deadline:
                state = self.show(case["unit"])
                if state.get("LoadState") == "not-found":
                    group = probe.CGROUP_ROOT / case["control_group"].lstrip("/")
                    return not group.exists() and not group.is_symlink()
                if _number(state.get("MainPID")) == 0 and self._empty(case):
                    return self._retire_empty_unit(case, state["InvocationID"])
                self.sleep(0.1)
        except Exception:
            return False
        return False


def run_validation(request, *, boundary, run, record):
    """Fixed controller interface. Bytes runner accepts ``timeout=`` and suppresses logs.

    ``record(event, safe_dict)`` must durably journal before returning. Boundary
    denial stops remaining work; own-workspace gate release and natural cleanup
    still run. The caller must serialize root operations and bind this module's
    public source digest. This function does not inspect or change real data.

    Not plug-and-play for stdin-exec controllers: before importing, their
    trusted_source path must bind both public helper SHA-256 values, obtain a
    fresh write permit, and install only those sources below the fixed root
    /opt/prism/validation/helpers/<combined-source-hash>/scripts/ directory.
    Ancestors/files must be root-owned, no links, directories 0555, files 0444.
    System Python 3.12 -I -S -B may add only that helper root to sys.path; do not
    import these helpers from an unverified candidate or inherited environment.
    The controller pins this module; install_bundles additionally rechecks the
    real retirement_probe.__file__ against PROBE_SOURCE_SHA256. Caller-held root
    serialization and rejection of unresolved earlier validation are required.
    """
    probe._require_linux()
    validator = Validation(request, boundary=boundary, run=run, record=record)
    results = []
    error = None
    cleanup = True
    try:
        validator.production = validator.production_identity()
        no_existing_synthetic_workloads(validator.plan)
        install_bundles(validator.plan, boundary, record)
        for case in validator.plan["cases"]:
            result = validator.case(case)
            results.append(result)
            if not result["passed"]:
                reject("retirement_case_failed")
    except Exception as exc:
        error = (
            str(exc)
            if isinstance(exc, (RetirementValidationError, probe.RetirementProbeError))
            and re.fullmatch(r"retirement_[a-z0-9_]{1,80}", str(exc))
            else "retirement_boundary_or_command_failed"
        )
    finally:
        cleanup = validator.cleanup()
        if error in {"retirement_previous_workload_active", "retirement_unknown_synthetic_group"}:
            cleanup = False
    if error is None and cleanup:
        summary = probe.summarize(validator.plan, results)
    else:
        summary = {
            "schema": 1,
            "bindings": validator.plan["bindings"],
            "bundle_sha256": validator.plan["bundle_sha256"],
            "retirement_validation_passed": False,
            "cases": results,
            "error_code": error or "retirement_cleanup_unverified",
            "production_quiescence_proven": False,
            "task_census_still_required": True,
        }
    summary.update(
        actual_linux_collection=True,
        synthetic_cleanup_verified=cleanup,
        validates_single_main_signal_only=True,
        systemctl_stop_end_to_end_proven=False,
        wrapper_sha256=wrapper_directory().name,
        probe_source_sha256=PROBE_SOURCE_SHA256,
        python_minor="3.12",
        pinned_versions=dict(probe.VERSIONS),
        uvicorn_server_sha256=probe.UVICORN_SHA256,
        uvloop_binary_sha256=probe.UVLOOP_SHA256,
    )
    record("retirement_validation_result", summary)
    return summary
