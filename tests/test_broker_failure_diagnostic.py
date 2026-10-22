"""Real private-file I/O with synthetic identities; no Linux/provider evidence."""

import ast
import errno
import hashlib
import json
import os
import socket
import ssl
import threading
import types
from pathlib import Path

import pytest

from scripts import live_integration_transport as transport
from scripts import live_integration_validation as supervisor


def bootstrap_functions():
    parsed = ast.parse(supervisor.BROKER_BOOTSTRAP)
    parsed.body = [node for node in parsed.body if not isinstance(node, ast.Try)]
    namespace = {}
    exec(compile(parsed, "sealed-broker-functions-only", "exec"), namespace)
    return namespace


@pytest.fixture
def diagnostic_writer(tmp_path):
    namespace = bootstrap_functions()
    work = Path("/var/lib/prism-operations/integration/public-diagnostic-20261022/live")
    output = tmp_path / "broker"
    output.mkdir(mode=0o700)

    # Keep fixed-path validation intact while mapping only final I/O into this
    # public fixture. Native os.open/fsync/exclusive/no-follow remain real.
    def fixed_path(value):
        assert value == str(work)

        class Work:
            name, parent = work.name, work.parent

            def __truediv__(self, name):
                assert name == "broker/preflight-failure.json"
                return tmp_path / name

        return Work()

    def path_factory(value):
        return Path(value) if value != str(work) else fixed_path(value)

    config = {
        "work": str(work),
        "candidate": "a" * 40,
        "nonce": "b" * 32,
        "broker_uid": os.getuid(),
        "root_pid": 1234,
        "issued_at": 123456,
        "transport_sha256": supervisor.SOURCE_PINS["transport"],
        "bootstrap_sha256": supervisor.BROKER_BOOTSTRAP_SHA256,
        "network_gate_sha256": supervisor.NETWORK_GATE_SHA256,
        "preflight_only": True,
    }
    assert os.getuid() != 0, "These private-file fixtures must never run as root"
    namespace.update(
        Path=path_factory,
        _diagnostic_context=config,
        _diagnostic_binding={"provider_pins": ["1.1.1.1", "8.8.8.8"]},
        _diagnostic_broker=types.SimpleNamespace(
            upstream=types.SimpleNamespace(
                phase="tcp_connect", resolved_peers=("1.1.1.1", "8.8.8.8"), selected_peer="1.1.1.1"
            )
        ),
    )
    namespace["mark"]("upstream_preflight")
    return namespace, config, output / "preflight-failure.json"


@pytest.mark.parametrize(
    ("error", "category", "code", "number"),
    [
        (PermissionError(13, "PUBLIC_DIAGNOSTIC_SECRET 1.2.3.4"), "permission_denied", None, 13),
        (socket.gaierror(-2, "PUBLIC_DIAGNOSTIC_SECRET"), "dns_error", None, None),
        (OSError(101, "PUBLIC_DIAGNOSTIC_SECRET"), "os_error", None, 101),
        (ssl.SSLError(1, "PUBLIC_DIAGNOSTIC_SECRET"), "tls_error", None, 1),
        (transport.TransportError("upstream_dns_rejected"), "transport_error", "upstream_dns_rejected", None),
        (transport.TransportError("PUBLIC_DIAGNOSTIC_SECRET"), "transport_error", "transport_error", None),
        (RuntimeError("network_permit_rejected"), "bootstrap_error", "network_permit_rejected", None),
        (RuntimeError("PUBLIC_DIAGNOSTIC_SECRET"), "unexpected_error", None, None),
    ],
)
def test_real_private_file_keeps_only_bounded_safe_facts(diagnostic_writer, error, category, code, number):
    namespace, config, target = diagnostic_writer
    namespace["failure_diagnostic"](error)
    raw = target.read_bytes()
    assert b"PUBLIC_DIAGNOSTIC_SECRET" not in raw and b"1.1.1.1" not in raw and b"1.2.3.4" not in raw
    value = json.loads(raw)
    assert value["binding"] == {**{k: v for k, v in config.items() if k != "work"}, "broker_pid": os.getpid()}
    diagnostic = value["diagnostic"]
    assert supervisor.valid_broker_diagnostic_fields(diagnostic)
    assert (
        diagnostic.items()
        >= {
            "stage": "upstream_tcp_connect",
            "error_class": category,
            "error_code": code,
            "errno": number,
            "dns_peer_count": 2,
            "dns_snapshot_matches_parent": True,
            "selected_peer_in_parent": True,
        }.items()
    )
    assert target.stat().st_mode & 0o777 == 0o600 and target.stat().st_nlink == 1
    before = raw
    with pytest.raises(FileExistsError):
        namespace["failure_diagnostic"](RuntimeError("ipc_rejected"))
    assert target.read_bytes() == before


def test_certificate_failure_keeps_only_integer_verify_code(diagnostic_writer):
    namespace, _, target = diagnostic_writer
    error = ssl.SSLCertVerificationError(1, "PUBLIC_DIAGNOSTIC_SECRET")
    error.verify_code = 18
    namespace["failure_diagnostic"](error)
    data = json.loads(target.read_bytes())["diagnostic"]
    assert data["error_class"] == "tls_certificate_error" and data["tls_verify_code"] == 18
    assert supervisor.valid_broker_diagnostic_fields(data)


def test_dns_facts_observe_original_state_without_resolving_or_selecting(diagnostic_writer, monkeypatch):
    namespace, _, target = diagnostic_writer
    calls = []

    class OriginalUpstream:
        phase = "tls_handshake"

        @property
        def resolved_peers(self):
            calls.append("original_peers")
            return ("8.8.4.4",)

        @property
        def selected_peer(self):
            calls.append("original_selection")
            return "8.8.4.4"

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("No second DNS resolution"))
    namespace["_diagnostic_broker"] = types.SimpleNamespace(upstream=OriginalUpstream())
    namespace["failure_diagnostic"](PermissionError(1, "public denied fixture"))
    value = json.loads(target.read_bytes())["diagnostic"]
    assert value["stage"] == "upstream_tls_handshake" and value["dns_peer_count"] == 1
    assert value["dns_snapshot_matches_parent"] is False and value["selected_peer_in_parent"] is False
    assert calls == ["original_peers", "original_selection"]


@pytest.mark.parametrize("phase", ["not_started", "dns_resolve", "dns_validate"])
def test_unresolved_upstream_does_not_invent_dns_evidence(diagnostic_writer, phase):
    namespace, _, target = diagnostic_writer
    namespace["_diagnostic_broker"] = types.SimpleNamespace(
        upstream=types.SimpleNamespace(phase=phase, resolved_peers=(), selected_peer=None)
    )
    namespace["failure_diagnostic"](OSError(errno.ENETUNREACH, "public fixture"))
    value = json.loads(target.read_bytes())["diagnostic"]
    assert value["stage"] == "upstream_" + phase
    assert (
        value["dns_peer_count"],
        value["dns_snapshot_matches_parent"],
        value["selected_peer_in_parent"],
    ) == (
        0,
        None,
        None,
    )


def test_disabled_post_allow_diagnostic_writes_no_file(diagnostic_writer):
    namespace, _, target = diagnostic_writer
    namespace["_diagnostic_enabled"] = False
    namespace["failure_diagnostic"](RuntimeError("PUBLIC_DIAGNOSTIC_SECRET"))
    assert not target.exists()


def test_real_symlink_target_rejected_without_overwriting_public_fixture(diagnostic_writer, tmp_path):
    namespace, _, target = diagnostic_writer
    witness = tmp_path / "public-witness"
    witness.write_bytes(b"PUBLIC_UNCHANGED")
    target.symlink_to(witness)
    with pytest.raises(FileExistsError):
        namespace["failure_diagnostic"](OSError(13, "fixture"))
    assert witness.read_bytes() == b"PUBLIC_UNCHANGED"


def test_broker_failure_wrapper_exits_even_when_diagnostic_io_fails():
    namespace = bootstrap_functions()
    parsed = ast.parse(supervisor.BROKER_BOOTSTRAP)
    terminal = [node for node in parsed.body if isinstance(node, ast.Try)]
    assert len(terminal) == 1
    calls = []

    def fail():
        calls.append("main_failed")
        raise OSError(errno.EPERM, "public denied fixture")

    def diagnostic(error):
        assert isinstance(error, PermissionError)
        calls.append("diagnostic_failed")
        raise PermissionError(errno.EACCES, "public read-only fixture")

    namespace.update(main=fail, failure_diagnostic=diagnostic)
    with pytest.raises(SystemExit) as error:
        exec(compile(ast.Module(body=terminal, type_ignores=[]), "sealed-failure-wrapper", "exec"), namespace)
    assert error.value.code == 1 and calls == ["main_failed", "diagnostic_failed"]


def test_real_unix_eof_is_after_private_diagnostic_fsync(diagnostic_writer, tmp_path):
    namespace, _, target = diagnostic_writer
    endpoint = str(tmp_path / "diagnostic.sock")
    errors = []

    def worker():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                channel.connect(endpoint)
                with namespace["diagnostic_before_channel_close"]():
                    raise PermissionError(errno.EPERM, "public denied fixture")
        except PermissionError:
            pass
        except Exception as error:
            errors.append(type(error).__name__)

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(endpoint)
        listener.listen(1)
        listener.settimeout(5)
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            channel, _ = listener.accept()
            with channel:
                channel.settimeout(5)
                assert channel.recv(1) == b""
                # EOF cannot race ahead of the guard's real file write/fsync.
                value = json.loads(target.read_bytes())
                assert value["diagnostic"]["error_class"] == "permission_denied"
        finally:
            thread.join(5)
    assert not thread.is_alive() and errors == []


def test_diagnostic_io_failure_never_swallows_original_broker_failure():
    namespace = bootstrap_functions()
    calls = []

    def denied(error):
        calls.append(type(error))
        raise PermissionError(errno.EACCES, "public denied diagnostic fixture")

    namespace["failure_diagnostic"] = denied
    original = OSError(errno.ENETUNREACH, "public denied transport fixture")
    with pytest.raises(OSError) as observed, namespace["diagnostic_before_channel_close"]():
        raise original
    assert observed.value is original and calls == [OSError]


def test_embedded_enums_match_trusted_transport_and_parent_validation():
    namespace = bootstrap_functions()
    assert namespace["_DIAGNOSTIC_STAGES"] == supervisor.BROKER_DIAGNOSTIC_STAGES
    assert namespace["_DIAGNOSTIC_TRANSPORT_CODES"] == transport.TRANSPORT_ERROR_CODES
    assert supervisor.BROKER_DIAGNOSTIC_TRANSPORT_CODES == transport.TRANSPORT_ERROR_CODES
    assert {"upstream_" + phase for phase in transport.UPSTREAM_PHASES} <= supervisor.BROKER_DIAGNOSTIC_STAGES
    assert namespace["_DIAGNOSTIC_BOOTSTRAP_CODES"] == supervisor.BROKER_DIAGNOSTIC_BOOTSTRAP_CODES
    assert (
        hashlib.sha256(supervisor.BROKER_BOOTSTRAP.encode()).hexdigest() == supervisor.BROKER_BOOTSTRAP_SHA256
    )
