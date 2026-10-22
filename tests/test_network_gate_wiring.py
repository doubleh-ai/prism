"""Sealed-source and actual-bootstrap composition; all kernel/network I/O is fake."""

import ast
import base64
import hashlib
import os
import sys
import types
from pathlib import Path

import pytest

from scripts import live_integration_validation as live
from scripts import market_live_validation as market


class Proxy:
    def __init__(self, original, **changes):
        self.original, self.changes = original, changes

    def __getattr__(self, name):
        return self.changes.get(name, getattr(self.original, name))


def root_metadata(info):
    return types.SimpleNamespace(
        st_uid=0,
        st_gid=0,
        st_mode=info.st_mode & ~0o022,
        st_ino=info.st_ino,
        st_dev=info.st_dev,
        st_nlink=info.st_nlink,
        st_size=info.st_size,
        st_mtime_ns=info.st_mtime_ns,
        st_ctime_ns=info.st_ctime_ns,
    )


def sealed_loader(module, path, monkeypatch):
    # Ownership is an explicit fixture; file descriptors, source bytes, SHA and
    # sys.modules registration are exercised by the real loader.
    real_fstat = os.fstat
    with monkeypatch.context() as patch:
        patch.setattr(module, "root_locked" if module is live else "locked", lambda path: None)
        patch.setattr(module, "os", Proxy(os, fstat=lambda fd: root_metadata(real_fstat(fd))))
        return module.load_network_gate(path)


@pytest.mark.parametrize("module", [live, market])
def test_sealed_helper_loader_registers_dataclasses_restores_namespace_and_checks_sha(
    tmp_path, monkeypatch, module
):
    source = Path(module.__file__).with_name("cgroup_network_gate.py").read_bytes()
    assert hashlib.sha256(source).hexdigest() == module.NETWORK_GATE_SHA256
    path = tmp_path / "sealed" / "network-gate.py"
    path.parent.mkdir()
    path.write_bytes(source)
    name = "_prism_owned_network_gate_" + module.NETWORK_GATE_SHA256
    sentinel = types.ModuleType(name)
    monkeypatch.setitem(sys.modules, name, sentinel)
    loaded = sealed_loader(module, path, monkeypatch)
    assert loaded.DIAGNOSTIC_STAGES == module.NETWORK_DIAGNOSTIC_STAGES
    assert loaded.__file__ == str(path) and sys.modules[name] is sentinel
    config = loaded.ControllerDescriptor(
        role="live",
        candidate_prefix="a" * 12,
        nonce="b" * 32,
        invocation_id="c" * 32,
        pid=1234,
        startticks=55,
        uid=2001,
        cgroup_inode=91,
        cgroup_device=31,
        provider_ipv4=("1.1.1.1",),
    )
    assert config.path.endswith("/" + config.unit)
    path.write_bytes(source + b"\n# unreviewed change\n")
    with pytest.raises(module.ValidationError, match="network_gate_rejected"):
        sealed_loader(module, path, monkeypatch)
    assert sys.modules[name] is sentinel


class ProofGate:
    """Typed supervisor fixture; it is not an actual Linux/kernel proof."""

    programs = tuple((hook, 500 + hook, "7461676279746573", "a" * 64) for hook in (10, 11, 14, 15))

    def verify(self):
        return self.programs

    def prepare_stop(self):
        return {
            "schema": 1,
            "profile": "linux-6.8",
            "identity": {"fixture": True},
            "programs": self.programs,
            "verified_at_ns": 1234,
        }

    def cleanup(self):
        return {
            "schema": 1,
            "mode": "retained_empty",
            "complete": True,
            "objects_released": True,
            "program_count": 4,
            "programs": [[hook, identifier, source] for hook, identifier, _, source in self.programs],
            "offline_queries": [],
            "detached_hooks": [10, 11, 14, 15],
            "stop_verified_at_ns": 1234,
            "profile": "linux-6.8",
        }

    def diagnostic(self):
        return {"stage": "complete", "command": None, "errno": None}


class ProofReference:
    """Explicit offline C-bus fixture, never a real Linux unit reference."""

    def __init__(self, identity):
        self.identity = identity
        self.events = []

    def proof(self, state):
        return {"schema": 1, **self.identity, "unique_name": ":1.234", "state": state}

    def acquire(self):
        self.events.append("acquire")
        return self.proof("acquired")

    def verify(self, *, stopped=False):
        self.events.append("stopped" if stopped else "running")
        return self.proof("verified_stopped" if stopped else "verified_running")

    def verify_held(self):
        exited = getattr(self, "exited", False)
        self.events.append("held_exited" if exited else "held_running")
        return self.proof("verified_exited" if exited else "verified_running")

    def release(self):
        self.events.append("release")
        return self.proof("released")

    def close(self):
        self.events.append("close")


def bind_reference(value):
    live_value = hasattr(value, "units")
    roles = ("broker", "worker") if live_value else ("market",)
    value.reference_roles = roles
    value.reference_identities = {}
    value.reference_unique_names = {}
    value.unit_references = {}
    for role in roles:
        identity = {
            "unit": value.units[role] if live_value else value.unit,
            "invocation_id": "c" * 32 if live_value else value.invocation,
            "pid": value.pids[role] if live_value else value.pid,
            "startticks": 55 if live_value else value.ticks,
            "uid": value.accounts[role].pw_uid if live_value else value.account.pw_uid,
        }
        value.reference_identities[role] = identity
        value.reference_unique_names[role] = ":1.234"
        value.unit_references[role] = ProofReference(identity)
    value.report["unit_reference_acquire_sha256"] = "a" * 64


class MemoryKernel:
    profile = "linux-6.8"

    def __init__(self, module, events):
        self.module, self.events = module, events
        self.programs, self.attached = {}, {}
        self.stopped = False

    def open_cgroup(self, descriptor):
        self.events.append("owned_cgroup_open")
        return 40

    def check_identity(self, fd, descriptor, *, stopped):
        assert fd == 40 and stopped == self.stopped
        self.events.append("kernel_stopped" if stopped else "kernel_running")
        return "retained_empty" if stopped else None

    def query(self, fd, hook, *, effective):
        identifiers = self.attached.get(hook, ())
        return (2 if identifiers and not effective else 0), identifiers

    def load(self, descriptor, hook):
        fd = 100 + len(self.programs)
        self.programs[fd] = self.module._ProgramInfo(500 + len(self.programs), b"tagbytes", 18, 0, 0)
        return fd

    def info(self, fd):
        return self.programs[fd]

    def attach(self, fd, hook, program_fd):
        self.attached[hook] = (self.programs[program_fd].identifier,)
        self.events.append("attach_" + str(hook))

    def detach(self, fd, hook, program_fd):
        assert self.stopped and self.attached[hook] == (self.programs[program_fd].identifier,)
        self.attached[hook] = ()
        self.events.append("detach_" + str(hook))

    def open_program(self, identifier):
        assert identifier in {500, 501, 502, 503}
        raise self.module.GateError("released_program", command=13, error_number=2)

    def close(self, fd):
        self.events.append("close_owned_fd")


def test_market_actual_bootstrap_hello_sealed_attach_permit_canaries_tls_then_no_http(tmp_path, monkeypatch):
    source_path = Path(market.__file__).with_name("cgroup_network_gate.py")
    sealed = tmp_path / "sealed-helper.py"
    sealed.write_bytes(source_path.read_bytes())
    probe_source = Path(market.__file__).with_name("market_live_probe.py").read_bytes()
    request = {
        "schema": 1,
        "release_id": "network-preflight-only",
        "candidate": "a" * 40,
        "production_pid": 123,
        "probe_source": {"sha256": market.PROBE_SHA256, "content": base64.b64encode(probe_source).decode()},
    }
    events = []
    value = market.Validation(
        request,
        boundary=events.append,
        run=lambda *args, **kwargs: pytest.fail("no real command"),
        record=lambda result: None,
        network_gate_path=sealed,
        preflight_only=True,
    )
    value.pid, value.invocation, value.ticks = 1234, "c" * 32, 55
    value.account = types.SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
    assert value.account.pw_uid > 0  # This test must never run as root.
    value.work = tmp_path / "work"
    value.work.mkdir()
    (value.work / "output").mkdir()
    (value.work / "data").mkdir()
    (value.work / "worker.py").write_bytes(market.WORKER_SOURCE.encode())
    (value.work / "probe.py").write_bytes(probe_source)
    value.pins = {host: ["1.1.1.1"] for host in market.HOSTS}
    value.negative = "9.9.9.9"
    groups = tmp_path / "cgroups"
    cgroup = groups / "system.slice" / value.unit
    cgroup.mkdir(parents=True)
    monkeypatch.setattr(market, "CGROUP", groups)
    monkeypatch.setattr(value, "identity", lambda: events.append("actual_identity_fixture") or {"fixture": 1})
    actual_loader = market.load_network_gate
    holder = {}

    def load(path):
        # Temporarily expose the real loader to the fixture wrapper.
        with monkeypatch.context() as patch:
            patch.setattr(market, "load_network_gate", actual_loader)
            module = sealed_loader(market, path, patch)
        kernel = MemoryKernel(module, events)
        module._LinuxKernel = lambda: kernel
        holder["kernel"] = kernel
        return module

    monkeypatch.setattr(market, "load_network_gate", load)
    monkeypatch.setattr(
        market,
        "load_unit_reference",
        lambda path: types.SimpleNamespace(
            ExpectedIdentity=lambda **kw: types.SimpleNamespace(unit=value.unit, **kw),
            UnitReference=lambda expected: ProofReference(
                {
                    "unit": value.unit,
                    **{
                        name: getattr(expected, name)
                        for name in ("invocation_id", "pid", "startticks", "uid")
                    },
                }
            ),
        ),
    )
    parsed = ast.parse(market.WORKER_SOURCE)
    parsed.body = [node for node in parsed.body if not isinstance(node, ast.Try)]
    namespace = {}
    exec(compile(parsed, "actual-market-bootstrap-offline", "exec"), namespace)

    class ChildPath:
        def __init__(self, path):
            text = os.fspath(path)
            if text.startswith("/sys/fs/cgroup"):
                text = str(groups) + text[len("/sys/fs/cgroup") :]
            self.path = Path(text)

        def __fspath__(self):
            return str(self.path)

        def __str__(self):
            return str(self.path)

        def __truediv__(self, name):
            return ChildPath(self.path / name)

        def __eq__(self, other):
            return isinstance(other, ChildPath) and self.path == other.path

        @property
        def parents(self):
            return tuple(ChildPath(path) for path in self.path.parents)

        def lstat(self):
            return root_metadata(self.path.lstat())

        def stat(self):
            return self.path.stat()

        def exists(self):
            return self.path.exists()

        def iterdir(self):
            return self.path.iterdir()

        def read_text(self):
            assert str(self.path) == "/proc/self/cgroup"
            return "0::/system.slice/" + value.unit

    class Channel:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, value):
            pass

        def connect_ex(self, target):
            events.append("strict_negative")
            return 13

        def sendto(self, data, target):
            assert data == b""
            events.append("strict_udp_negative")
            raise PermissionError(13, "offline")

        def connect(self, target):
            assert target == ("1.1.1.1", 443)
            events.append("positive_tls")

        def getpeername(self):
            return "1.1.1.1", 443

    class TLSContext:
        verify_mode, check_hostname = 2, True

        def wrap_socket(self, channel, *, server_hostname):
            assert server_hostname in market.HOSTS
            return channel

    namespace["Path"] = ChildPath
    namespace["self_ticks"] = lambda: value.ticks
    namespace["denied"] = lambda *args: True  # No credential-path read attempts.
    namespace["pwd"] = types.SimpleNamespace(
        getpwnam=lambda name: types.SimpleNamespace(
            pw_uid=value.account.pw_uid,
            pw_gid=value.account.pw_gid,
            pw_shell="/usr/sbin/nologin",
        )
    )
    real_fstat = os.fstat
    namespace["os"] = Proxy(
        os,
        getuid=lambda: value.account.pw_uid,
        getpid=lambda: value.pid,
        getgroups=lambda: [value.account.pw_gid],
        environ={},
        fstat=lambda fd: root_metadata(real_fstat(fd)),
    )
    config = {
        **{key: request[key] for key in ("candidate", "release_id", "production_pid")},
        "work": str(value.work),
        "nonce": value.nonce,
        "issued_at": value.issued,
        "worker_sha256": market.WORKER_SHA256,
        "probe_sha256": market.PROBE_SHA256,
        "pins": value.pins,
        "negative_public": value.negative,
        "preflight_only": True,
        "network_gate_sha256": market.NETWORK_GATE_SHA256,
        "network_role": "market",
        "network_pins": ["1.1.1.1"],
    }
    namespace["sys"] = types.SimpleNamespace(
        platform="linux",
        flags=types.SimpleNamespace(isolated=True, no_site=True, dont_write_bytecode=True),
        executable="/opt/prism/releases/" + request["candidate"] + "/.venv/bin/python",
        argv=["worker", market.encoded(config).decode()],
    )
    namespace["socket"] = types.SimpleNamespace(
        AF_INET=2,
        AF_INET6=10,
        SOCK_STREAM=1,
        SOCK_DGRAM=2,
        socket=lambda *args: Channel(),
    )
    namespace["ssl"] = types.SimpleNamespace(
        CERT_REQUIRED=2,
        TLSVersion=types.SimpleNamespace(TLSv1_2=1),
        create_default_context=TLSContext,
    )
    namespace["importlib"] = types.SimpleNamespace(
        util=types.SimpleNamespace(
            module_from_spec=lambda *args: pytest.fail("preflight must not import candidate/probe"),
        )
    )
    actual_write = namespace["write"]

    def write(path, document):
        actual_write(path, document)
        if str(path).endswith("/hello.json"):
            events.append("hello")
            value.install_network_gate()
        elif str(path).endswith("/bootstrap.json"):
            value.verify_network_gate()
            proof, _ = value.proof()
            assert all(proof["checks"].values())
            (value.work / "preflight-finish.json").write_bytes(b"offline root stop fixture")

    namespace["write"] = write
    actual_permit = namespace["network_permit"]

    def permit(*args):
        result = actual_permit(*args)
        events.append("permit_consumed")
        return result

    namespace["network_permit"] = permit
    with pytest.raises(SystemExit) as stopped:
        namespace["main"]()
    assert stopped.value.code == 0
    assert events.index("hello") < events.index("attach_10") < events.index("attach_15")
    assert events.index("attach_15") < events.index("permit_consumed") < events.index("strict_negative")
    assert events.index("strict_udp_negative") < events.index("positive_tls")
    assert not (value.work / "root-allow.json").exists()
    assert not (value.work / "output/market-result.json").exists()
    value.prepare_network_stop()
    events.append("stop_owned_unit")
    holder["kernel"].stopped = True
    value.cleanup_network_gate()
    assert value.report["network_gate_objects_released"] is True
    assert value.report["network_gate_cleanup_mode"] == "retained_empty"
    assert events.index("stop_owned_unit") < events.index("detach_10")
