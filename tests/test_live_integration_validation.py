"""Offline supervisor fixtures, never real Linux/BPF/model/search evidence."""

import ast
import base64
import contextlib
import hashlib
import io
import json
import os
import socket
import stat
import struct
import types
from pathlib import Path

import pytest

from scripts import live_integration_validation as supervisor


def request():
    scripts = Path(supervisor.__file__).parent
    return {
        "schema": 1,
        "release_id": "public-synthetic-20261022",
        "candidate": "a" * 40,
        "cli_sha256": "b" * 64,
        "production_pid": 123,
        "sources": {
            name: base64.b64encode((scripts / ("live_integration_" + name + ".py")).read_bytes()).decode()
            for name in supervisor.SOURCE_PINS
        },
    }


def account(name, uid):
    return types.SimpleNamespace(pw_name=name, pw_uid=uid, pw_gid=uid, pw_shell="/usr/sbin/nologin")


@pytest.fixture
def validation(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor, "OPERATIONS", tmp_path / "integration")
    monkeypatch.setattr(supervisor, "CGROUP", tmp_path / "cgroups")
    log, calls = [], []
    value = supervisor.Validation(
        request(),
        boundary=lambda stage: log.append(stage),
        run=lambda argv, **kw: calls.append((argv, kw)) or b"",
        record=log.append,
        key_provider=lambda: "public-synthetic-key-only",
    )
    value.accounts = {
        "worker": account("prism-integration", 2001),
        "broker": account("prism-integration-broker", 2002),
    }
    value.bridge_gid, value.pids = 2003, {"worker": 321, "broker": 654}
    value.binding = {
        "candidate": value.request["candidate"],
        "cli_sha256": value.request["cli_sha256"],
        "probe_sha256": supervisor.SOURCE_PINS["worker"],
        "nonce": value.nonce,
        "worker_pid": 321,
        "broker_pid": 654,
    }
    value.test_log, value.test_calls = log, calls
    return value


class Channel:
    def __init__(self, incoming=b"", credentials=(654, 2002, 2002)):
        self.incoming, self.outgoing = io.BytesIO(incoming), bytearray()
        self.credentials = credentials

    def recv(self, count):
        return self.incoming.read(min(count, 7))

    def sendall(self, value):
        self.outgoing.extend(value)

    def getsockopt(self, *args):
        return struct.pack("3i", *self.credentials)


def frame(value):
    raw = supervisor.encoded(value)
    return struct.pack("!I", len(raw)) + raw


def test_request_pins_actual_three_frozen_sources_and_rejects_extra_paths():
    source = supervisor.validate_request(request())
    assert {key: hashlib.sha256(value).hexdigest() for key, value in source.items()} == supervisor.SOURCE_PINS
    value = request()
    value["credential_path"] = "/home/ubuntu/stock-analyzer/.env"
    with pytest.raises(supervisor.ValidationError, match="request_rejected"):
        supervisor.validate_request(value)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda req: req.update(schema=True),
        lambda req: req.update(production_pid=True),
        lambda req: req.update(release_id="../../production"),
        lambda req: req.update(candidate="a" * 39),
        lambda req: req["sources"].update(worker=base64.b64encode(b"print('not approved')").decode()),
        lambda req: req["sources"].update(worker="not base64"),
    ],
)
def test_request_never_executes_unapproved_source(mutate):
    value = request()
    mutate(value)
    with pytest.raises(supervisor.ValidationError):
        supervisor.validate_request(value)


def test_framing_partial_reads_peer_binding_and_no_arbitrary_json(monkeypatch):
    monkeypatch.setattr(socket, "SO_PEERCRED", 17, raising=False)
    channel = Channel(frame({"schema": 1, "kind": "public"}))
    assert supervisor.receive(channel) == {"schema": 1, "kind": "public"}
    supervisor.send(channel, {"public": True})
    assert supervisor.receive(Channel(channel.outgoing)) == {"public": True}
    supervisor.peer(channel, 654, 2002)
    with pytest.raises(supervisor.ValidationError, match="ipc_rejected"):
        supervisor.peer(channel, 655, 2002)
    for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b"[]", b"\xff"):
        with pytest.raises(supervisor.ValidationError, match="ipc_rejected"):
            supervisor.receive(Channel(struct.pack("!I", len(raw)) + raw))
    for raw in (struct.pack("!I", 16385), struct.pack("!I", 0), b"\0\0"):
        with pytest.raises(supervisor.ValidationError, match="ipc_rejected"):
            supervisor.receive(Channel(raw))


@pytest.mark.parametrize(
    "value",
    ["127.0.0.1", "169.254.169.254", "10.0.0.1", "::1", "64:ff9b::808:808", "8.8.8.8%scope", 134744072],
)
def test_dns_rejects_private_metadata_translation_and_non_string(value):
    with pytest.raises(supervisor.ValidationError, match="dns_rejected"):
        supervisor.public_ip(value)


def test_dns_exact_resolver_stub_not_loopback_range(tmp_path, monkeypatch):
    path = tmp_path / "resolv.conf"
    monkeypatch.setattr(supervisor, "RESOLV", path)
    path.write_text("nameserver 127.0.0.53\n")
    pins, resolvers, negative = supervisor.network_policy(lambda: ["1.1.1.1", "8.8.8.8"])
    assert pins == ["1.1.1.1", "8.8.8.8"] and resolvers == ["127.0.0.53"]
    assert negative["production"] == ("127.0.0.1", 8088)
    assert negative["disallowed_public"][0] not in pins + resolvers
    for text in ("nameserver 127.0.0.1\n", "nameserver 10.0.0.53\n", "nameserver invalid\n", "x" * 16385):
        path.write_text(text)
        with pytest.raises(supervisor.ValidationError, match="dns_rejected"):
            supervisor.network_policy(lambda: ["1.1.1.1"])
    path.write_text("nameserver 127.0.0.53\n")
    with pytest.raises(supervisor.ValidationError, match="dns_rejected"):
        supervisor.network_policy(lambda: ["1.1.1.1", "169.254.169.254"])


def test_write_honors_requested_allow_mode_even_private_root_umask(tmp_path):
    before = os.umask(0o077)
    try:
        path = tmp_path / "root-allow.json"
        supervisor.write(path, b'{"public":true}', 0o444)
    finally:
        os.umask(before)
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    with pytest.raises(FileExistsError):
        supervisor.write(path, b"replacement")
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(FileExistsError):
        supervisor.write(link, b"replacement")
    assert path.read_bytes() == b'{"public":true}'


def test_private_record_rejects_public_files_hardlinks_and_duplicate_json(tmp_path):
    path = tmp_path / "result.json"
    supervisor.write(path, b'{"public":true}', 0o600)
    assert supervisor.read_private(path, os.getuid())[0] == {"public": True}
    path.chmod(0o644)
    with pytest.raises(supervisor.ValidationError, match="worker_result_rejected"):
        supervisor.read_private(path, os.getuid())
    path.chmod(0o600)
    os.link(path, tmp_path / "other")
    with pytest.raises(supervisor.ValidationError, match="worker_result_rejected"):
        supervisor.read_private(path, os.getuid())


def test_units_are_two_nonroot_distinct_env_i_no_secrets_and_exact_bpf_ips(validation):
    worker = validation.unit_command("worker", ["1.1.1.1"], ["127.0.0.53"])
    broker = validation.unit_command("broker", ["1.1.1.1"], ["127.0.0.53"])
    assert "--property=PrivateNetwork=yes" in worker
    assert "--property=IPAddressDeny=any" in broker
    assert "--property=IPAddressAllow=1.1.1.1 127.0.0.53" in broker
    assert not any("127.0.0.0/8" in item for item in broker)
    assert "--property=User=prism-integration" in worker
    assert "--property=User=prism-integration-broker" in broker
    for command in (worker, broker):
        assert "/usr/bin/env" in command and command[command.index("/usr/bin/env") + 1] == "-i"
        assert "--property=TemporaryFileSystem=/run:ro" in command
        assert "--property=LimitCORE=0" in command
        assert "--property=KillMode=control-group" in command and "--property=RuntimeMaxSec=180" in command
        assert "-I" in command and "-B" in command and "-S" in command
        assert not any(value.startswith(("DEEPSEEK_", "LLM_", "ANTHROPIC_")) for value in command)
    assert validation.nonce in validation.units["worker"] and len(validation.nonce) == 32


def preflight(validation):
    return {
        "schema": 1,
        "kind": "preflight",
        "binding": validation.binding,
        "facts": {"public_dns_pinned": True, "upstream_tls_verified": True},
        "filesystem_checks": dict.fromkeys(supervisor.BROKER_FILESYSTEM_CHECKS, True),
        "negative_errno": dict.fromkeys([*supervisor.NEGATIVE_TARGETS, "disallowed_public"], 13),
        "positive_peers": ["1.1.1.1"],
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["negative_errno"].update(production=111),
        lambda value: value["negative_errno"].update(metadata=0),
        lambda value: value["negative_errno"].update(vibe=True),
        lambda value: value["facts"].update(upstream_tls_verified=1),
        lambda value: value["filesystem_checks"].update(config_write_denied=False),
        lambda value: value.update(positive_peers=["10.0.0.1"]),
        lambda value: value.update(positive_peers=["1.1.1.1", "1.1.1.1"]),
    ],
)
def test_preflight_requires_actual_bpf_denial_not_connection_refused(validation, mutate):
    value = preflight(validation)
    validation.preflight(value, ["1.1.1.1"])
    mutate(value)
    with pytest.raises(supervisor.ValidationError, match="broker_preflight_rejected"):
        validation.preflight(value, ["1.1.1.1"])


def key_ready(validation, monkeypatch):
    events = []
    validation.kernel, validation.proof_sha = {"actual": "synthetic"}, "c" * 64
    monkeypatch.setattr(supervisor, "peer", lambda *args: events.append("peer"))
    monkeypatch.setattr(validation, "kernel_identity", lambda: events.append("kernel") or validation.kernel)
    monkeypatch.setattr(validation, "proof", lambda: events.append("proof") or ({}, validation.proof_sha))
    monkeypatch.setattr(validation, "candidate_identity", lambda: events.append("candidate"))
    monkeypatch.setattr(validation, "source_identity", lambda: events.append("sources"))
    validation.boundary = lambda stage: events.append(stage)
    validation.key_provider = lambda: events.append("key_provider") or "public-synthetic-key-only"
    return events


def test_key_only_after_actual_rechecks_and_second_stop_gate_before_send(validation, monkeypatch):
    events = key_ready(validation, monkeypatch)
    channel = Channel()
    validation.release_key(channel)
    index = events.index("key_provider")
    assert events[:index] == ["live_key_release", "peer", "kernel", "proof", "candidate", "sources"]
    assert events[index + 1 :] == ["live_key_send", "peer", "kernel", "proof", "candidate", "sources"]
    assert supervisor.receive(Channel(channel.outgoing))["key"] == "public-synthetic-key-only"
    assert "public-synthetic-key-only" not in json.dumps(validation.test_log)
    with pytest.raises(supervisor.ValidationError, match="key_rejected"):
        validation.release_key(Channel())
    assert events.count("key_provider") == 1


@pytest.mark.parametrize("failure", ["before", "after", "kernel", "source"])
def test_stop_identity_source_failures_never_send_a_key(validation, monkeypatch, failure):
    events = key_ready(validation, monkeypatch)
    if failure in {"before", "after"}:

        def boundary(stage):
            events.append(stage)
            if stage == ("live_key_release" if failure == "before" else "live_key_send"):
                raise supervisor.ValidationError("operator_stop")

        validation.boundary = boundary
    elif failure == "kernel":
        monkeypatch.setattr(validation, "kernel_identity", lambda: {"actual": "changed"})
    else:

        def rejected():
            raise supervisor.ValidationError("source_rejected")

        monkeypatch.setattr(validation, "source_identity", rejected)
    channel = Channel()
    with pytest.raises(supervisor.ValidationError):
        validation.release_key(channel)
    assert channel.outgoing == b""
    assert events.count("key_provider") == (1 if failure == "after" else 0)


def worker_result(validation):
    def phase(search):
        return {
            "model_completed": True,
            "chat_http_verified": True,
            "chat_sse_sha256": "d" * 64,
            "chat_sse_ledger_match": True,
            "server_clock_verified": True,
            "model_date_echo_verified": True,
            "native_search_frames_observed": search,
            "search_call_count": int(search),
            "server_search_frames_observed": False,
            "official_source_urls": ["https://www.sse.com.cn/disclosure/listedinfo/announcement/"]
            if search
            else [],
            "source_ledger_persisted": True,
            "server_search_unhandled_by_candidate": False,
            "run_id": ("e" if search else "f") * 32,
        }

    return {
        "schema": 1,
        "probe": "bounded_live_integration",
        "binding": validation.binding,
        "checked_at": validation.issued,
        "passed": True,
        "live_model_verified": True,
        "native_search_verified": True,
        "native_fetch_verified": False,
        "market_provider_verified": False,
        "production_verified": False,
        "research_quality_verified": False,
        "checks": {
            "phases": [phase(False), phase(True)],
            "owned_mcp_roundtrip_verified": True,
            "foreign_owner_http_404_verified": True,
            "real_runner_invocations": 2,
            "private_event_bytes": 1024,
        },
        "error_class": None,
        "stage": "complete",
        "blocked_provider_header_names": [],
        "probe_mutations": [
            "broker origin and synthetic token",
            "bounded turns",
            "two read-only MCP tools",
            "synthetic data and HOME",
        ],
    }


def test_successful_result_separates_real_required_claims_and_unsupported_capabilities(
    validation, monkeypatch
):
    value = worker_result(validation)
    monkeypatch.setattr(supervisor, "read_private", lambda *args, **kwargs: (value, "c" * 64))
    validation.result()
    assert validation.report["owned_mcp_verified"] and validation.report["server_clock_verified"]
    assert validation.report["native_search_verified"] and validation.report["live_model_verified"]
    assert not any(
        validation.report[name]
        for name in (
            "native_fetch_verified",
            "market_provider_verified",
            "research_quality_verified",
            "production_verified",
        )
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(native_search_verified=False),
        lambda value: value.update(native_fetch_verified=True),
        lambda value: value.update(error_class="live_acceptance_incomplete"),
        lambda value: value.update(blocked_provider_header_names=["x-unknown-sdk"]),
        lambda value: value.update(probe_mutations=[]),
        lambda value: value["checks"].update(owned_mcp_roundtrip_verified=1),
        lambda value: value["checks"]["phases"][1].update(server_search_unhandled_by_candidate=True),
        lambda value: value["checks"]["phases"][1].update(source_ledger_persisted=False),
        lambda value: value["checks"]["phases"][1].update(
            official_source_urls=["https://evil.example/source"]
        ),
        lambda value: value["checks"]["phases"][1].update(
            official_source_urls=["https://www.sse.com.cn/a?token=synthetic"]
        ),
        lambda value: value["checks"]["phases"][1].update(
            official_source_urls=["https://user@www.sse.com.cn/a"]
        ),
    ],
)
def test_inconsistent_unhandled_source_and_nonofficial_results_cannot_pass(validation, monkeypatch, mutate):
    value = worker_result(validation)
    mutate(value)
    monkeypatch.setattr(supervisor, "read_private", lambda *args, **kwargs: (value, "c" * 64))
    with pytest.raises(supervisor.ValidationError, match="worker_result_rejected"):
        validation.result()


def broker_summary(validation):
    return {
        "schema": 1,
        "kind": "summary",
        "binding": validation.binding,
        "summary": {
            "requests": {"messages": 2, "count_tokens": 0, "quote": 0, "kline": 0, "public_fetch": 0},
            "request_bytes": 2048,
            "response_bytes": 4096,
            "completed": 2,
            "failed": 0,
            "unknown_header_names": [],
            "native_fetch_verified": False,
            "research_quality_verified": False,
        },
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(failed=False),
        lambda value: value.update(completed=1),
        lambda value: value.update(request_bytes=2097153),
        lambda value: value.update(response_bytes=8388609),
        lambda value: value["requests"].update(messages=7),
        lambda value: value["requests"].update(count_tokens=3),
        lambda value: value["requests"].update(public_fetch=1),
        lambda value: value.update(unknown_header_names=["x-unexpected"]),
    ],
)
def test_broker_receipt_requires_matching_hard_budget_and_no_failed_or_bypassed_requests(validation, mutate):
    value = broker_summary(validation)
    validation.broker_result(value)
    mutate(value["summary"])
    with pytest.raises(supervisor.ValidationError, match="broker_failed"):
        validation.broker_result(value)


def test_existing_work_is_preserved_without_result_write_or_key_read(validation, monkeypatch):
    validation.work.mkdir(parents=True)
    marker = validation.work / "previous-private-receipt"
    marker.write_bytes(b"must remain byte-identical")
    monkeypatch.setattr(supervisor.sys, "platform", "linux")
    monkeypatch.setattr(supervisor.os, "geteuid", lambda: 0)
    monkeypatch.setattr(supervisor, "root_locked", lambda path: None)
    monkeypatch.setattr(validation, "candidate_identity", lambda: None)
    monkeypatch.setattr(validation, "accounts_ready", lambda: None)
    monkeypatch.setattr(supervisor, "network_policy", lambda *args: (["1.1.1.1"], ["127.0.0.53"], {}))
    validation.key_provider = lambda: pytest.fail("key must never be read")
    result = validation.execute()
    assert not result["passed"] and result["error_class"] == "workspace_exists"
    assert marker.read_bytes() == b"must remain byte-identical"
    assert list(validation.work.iterdir()) == [marker]


@pytest.mark.parametrize("drift", ["missing", "invocation", "pid", "startticks", "descendant"])
def test_cleanup_never_stops_unowned_reused_instance_or_accepts_populated_descendants(
    validation, monkeypatch, drift
):
    role, pid, invocation = "worker", 321, "c" * 32
    validation.launched = {role}
    validation.invocations[role] = (invocation, pid, 500)
    state = {"InvocationID": invocation, "ExecMainPID": str(pid), "MainPID": str(pid)}
    if drift == "missing":
        validation.invocations.clear()
    if drift == "invocation":
        state["InvocationID"] = "d" * 32
    if drift == "pid":
        state["MainPID"] = "322"
    monkeypatch.setattr(validation, "show", lambda *args, **kwargs: state)
    monkeypatch.setattr(supervisor, "startticks", lambda path: 501 if drift == "startticks" else 500)
    calls = []
    validation._run = lambda argv, **kwargs: calls.append(argv) or b"MainPID=0\n"
    if drift == "descendant":
        group = supervisor.CGROUP / "system.slice" / validation.units[role]
        group.mkdir(parents=True)
        (group / "cgroup.procs").write_text("")
        (group / "cgroup.events").write_text("populated 1\nfrozen 0\n")
    validation.cleanup()
    assert not validation.report["units_stopped"] and validation.report["error_class"] == "cleanup_failed"
    assert any("stop" in call for call in calls) is (drift == "descendant")
    assert not any("stock-app.service" in call for call in calls)


def test_cleanup_stops_only_exact_attempt_and_requires_kernel_empty(validation, monkeypatch):
    validation.launched = {"worker", "broker"}
    validation.invocations = {role: ("c" * 32, pid, 500) for role, pid in validation.pids.items()}
    monkeypatch.setattr(
        validation,
        "show",
        lambda role, **kw: {
            "InvocationID": "c" * 32,
            "ExecMainPID": str(validation.pids[role]),
            "MainPID": "0",
        },
    )
    calls = []
    validation._run = lambda argv, **kwargs: calls.append(argv) or b"MainPID=0\n"
    validation.cleanup()
    assert validation.report["units_stopped"]
    assert [call for call in calls if "stop" in call] == [
        ["/usr/bin/systemctl", "stop", validation.units[role]] for role in ("broker", "worker")
    ]


def test_existing_unit_rejected_before_launch_or_cleanup_ownership(validation):
    validation._run = lambda *args, **kwargs: b"prism-existing.service loaded active running\n"
    with pytest.raises(supervisor.ValidationError, match="unit_rejected"):
        validation.start("worker", ["1.1.1.1"], ["127.0.0.53"])
    assert not validation.launched and not validation.invocations


@pytest.mark.parametrize("actual", ["infinity", "18446744073709551615", "", "1024"])
def test_actual_unit_core_limit_must_be_zero_not_only_a_launch_flag(validation, monkeypatch, actual):
    validation.invocations["worker"] = ("c" * 32, 321, 500)
    state = {
        "MainPID": "321",
        "ExecMainPID": "321",
        "InvocationID": "c" * 32,
        "User": "prism-integration",
        "Group": "prism-integration",
        "NoNewPrivileges": "yes",
        "ProtectSystem": "strict",
        "ProtectHome": "yes",
        "KillMode": "control-group",
        "RuntimeMaxUSec": "3min",
        "PrivateNetwork": "yes",
        "LimitCORE": actual,
        "ControlGroup": "/system.slice/" + validation.units["worker"],
    }
    monkeypatch.setattr(validation, "show", lambda role: state)
    monkeypatch.setattr(supervisor, "startticks", lambda path: 500)
    with pytest.raises(supervisor.ValidationError, match="unit_rejected"):
        validation.identity("worker")


@pytest.mark.parametrize(
    "limits,expected",
    [
        ("Max core file size         0 0 bytes\n", True),
        ("Max core file size 0 unlimited bytes\n", False),
        ("Max core file size 1 0 bytes\n", False),
        ("Max open files 0 0 files\n", False),
    ],
)
def test_actual_kernel_core_limit_requires_both_soft_and_hard_zero(tmp_path, limits, expected):
    (tmp_path / "limits").write_text(limits)
    assert supervisor.core_disabled(tmp_path) is expected


def test_failed_broker_exit_is_not_accepted_from_its_summary(validation, monkeypatch):
    monkeypatch.setattr(
        validation, "show", lambda role: {"MainPID": "0", "ExecMainStatus": "1", "Result": "exit-code"}
    )
    with pytest.raises(supervisor.ValidationError, match="broker_failed"):
        validation.completed("broker")


def test_unsafe_exception_text_is_never_recorded_and_no_key_called(validation, monkeypatch):
    monkeypatch.setattr(supervisor.sys, "platform", "linux")
    monkeypatch.setattr(supervisor.os, "geteuid", lambda: 0)

    def rejected(path):
        raise RuntimeError("PUBLIC_SYNTHETIC_SECRET_SENTINEL")

    monkeypatch.setattr(supervisor, "root_locked", rejected)
    validation.key_provider = lambda: pytest.fail("must not acquire key")
    result = validation.execute()
    assert result["error_class"] == "supervision_failed"
    assert "PUBLIC_SYNTHETIC_SECRET_SENTINEL" not in json.dumps(validation.test_log)
    assert not validation.test_calls


def test_private_run_mount_label_alone_is_not_evidence(tmp_path, monkeypatch):
    process = tmp_path / "proc/321"
    process.mkdir(parents=True)
    (process / "mountinfo").write_text("1 0 0:1 / /run ro - tmpfs tmpfs ro\n")
    runtime = process / "root/run"
    runtime.mkdir(parents=True)
    # Offline synthetic stats prove the helper must additionally ask the kernel
    # for ST_RDONLY; a fake mountinfo string cannot pass this test.
    original = supervisor.os.fstat
    monkeypatch.setattr(
        supervisor.os,
        "fstat",
        lambda fd: types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0, st_dev=1, st_ino=2),
    )
    monkeypatch.setattr(supervisor.os, "fstatvfs", lambda fd: types.SimpleNamespace(f_flag=0))
    monkeypatch.setattr(
        Path,
        "stat",
        lambda self, *args, **kw: types.SimpleNamespace(
            st_dev=1 if self == runtime else 3, st_ino=2 if self == runtime else 4
        ),
    )
    with pytest.raises(supervisor.ValidationError, match="private_run_rejected"):
        supervisor.private_run(process)
    monkeypatch.setattr(supervisor.os, "fstatvfs", lambda fd: types.SimpleNamespace(f_flag=os.ST_RDONLY))
    supervisor.private_run(process)
    monkeypatch.setattr(supervisor.os, "fstat", original)


def test_bootstrap_is_compileable_stdlib_only_and_key_not_config_or_environment():
    compile(supervisor.BROKER_BOOTSTRAP, "sealed-broker-bootstrap", "exec")
    assert '"key"' not in supervisor.BROKER_BOOTSTRAP.split("config=json.loads(sys.argv[1])", 1)[0]
    assert 'root_source(source,config["transport_sha256"])' in supervisor.BROKER_BOOTSTRAP
    assert "sys.flags.no_site" in supervisor.BROKER_BOOTSTRAP and "SO_PEERCRED" in supervisor.BROKER_BOOTSTRAP


def test_complete_offline_state_machine_preserves_private_receipt_and_single_key_boundary(
    validation, monkeypatch
):
    """All OS/model witnesses below are explicitly fixtures, never live claims."""
    monkeypatch.setattr(supervisor.sys, "platform", "linux")
    monkeypatch.setattr(supervisor.os, "geteuid", lambda: 0)
    monkeypatch.setattr(supervisor.os, "chown", lambda *args: None)
    monkeypatch.setattr(supervisor, "root_locked", lambda path: None)
    monkeypatch.setattr(supervisor, "peer", lambda *args: None)
    monkeypatch.setattr(validation, "candidate_identity", lambda: None)
    monkeypatch.setattr(validation, "source_identity", lambda: None)
    monkeypatch.setattr(validation, "accounts_ready", lambda: None)
    monkeypatch.setattr(supervisor, "network_policy", lambda *args: (["1.1.1.1"], ["127.0.0.53"], {}))
    validation.accounts["worker"].pw_uid = os.getuid()
    kernel = {
        "worker_uids": [os.getuid()] * 4,
        "broker_uids": [2002] * 4,
        "worker_netns_inode": 11,
        "broker_netns_inode": 12,
        "host_netns_inode": 12,
        "worker_startticks": 101,
        "broker_startticks": 102,
    }
    monkeypatch.setattr(validation, "kernel_identity", lambda: kernel)
    key_calls = []
    validation.key_provider = lambda: key_calls.append("once") or "public-synthetic-key-only"

    def workspace():
        validation.work.mkdir(parents=True)
        validation.workspace_created = True
        (validation.work / "output").mkdir()
        proof = {
            "schema": 1,
            "binding": validation.binding,
            "production_pid": 123,
            "issued_at": validation.issued,
            "checked_at": validation.issued,
            "sources": supervisor.SOURCE_PINS,
            "checks": dict.fromkeys(supervisor.BOOT_CHECKS, True),
        }
        supervisor.write(validation.work / "output/bootstrap-proof.json", supervisor.encoded(proof), 0o600)
        supervisor.write(
            validation.work / "output/live-result.json", supervisor.encoded(worker_result(validation)), 0o600
        )

    channel = Channel()

    class Listener:
        def bind(self, path):
            Path(path).touch()

        def listen(self, count):
            pass

        def settimeout(self, timeout):
            pass

        def accept(self):
            return channel, "synthetic-peer"

        def close(self):
            pass

    channel.settimeout = lambda timeout: None
    channel.close = lambda: None
    monkeypatch.setattr(supervisor.socket, "socket", lambda *args: Listener())
    monkeypatch.setattr(validation, "workspace", workspace)
    monkeypatch.setattr(validation, "start", lambda role, *args: {"worker": 321, "broker": 654}[role])
    monkeypatch.setattr(
        validation, "show", lambda role: {"MainPID": "0", "ExecMainStatus": "0", "Result": "success"}
    )
    incoming = iter(
        [
            {"schema": 1, "kind": "hello", "nonce": validation.nonce, "pid": 654},
            preflight(validation),
            {"schema": 1, "kind": "key", "binding": validation.binding},
            {"schema": 1, "kind": "authorized", "binding": validation.binding},
            broker_summary(validation),
        ]
    )
    monkeypatch.setattr(supervisor, "receive", lambda channel: next(incoming))
    monkeypatch.setattr(validation, "cleanup", lambda: validation.report.update(units_stopped=True))
    result = validation.execute()
    assert result["passed"] and result["error_class"] is None and result["stage"] == "complete"
    assert key_calls == ["once"]
    assert (
        not result["production_verified"]
        and not result["native_fetch_verified"]
        and not result["market_provider_verified"]
    )
    stored = json.loads((validation.work / "validation-result.json").read_bytes())
    assert (
        stored == result
        and stat.S_IMODE((validation.work / "validation-result.json").stat().st_mode) == 0o600
    )
    permit = json.loads((validation.work / "root-allow.json").read_bytes())
    assert permit["binding"] == validation.binding and permit["kernel"] == kernel
    assert permit["witness"]["checked_at"] <= permit["allowed_at"]
    assert stat.S_IMODE((validation.work / "root-allow.json").stat().st_mode) == 0o444
    assert "public-synthetic-key-only" not in json.dumps(validation.test_log) + json.dumps(result)


@pytest.mark.parametrize("failed_canary", [False, True])
def test_actual_bootstrap_protocol_orders_canaries_tls_root_allow_then_single_key(validation, failed_canary):
    # Execute the actual stdlib bootstrap functions with explicit offline I/O
    # fixtures. This validates protocol order, not real UID/BPF/TLS evidence.
    parsed = ast.parse(supervisor.BROKER_BOOTSTRAP)
    parsed.body = [node for node in parsed.body if not isinstance(node, ast.Try)]
    namespace = {}
    exec(compile(parsed, "offline-sealed-bootstrap", "exec"), namespace)
    events = []
    binding = validation.binding
    incoming = b"".join(
        frame(value)
        for value in [
            {
                "schema": 1,
                "kind": "binding",
                "binding": binding,
                "negative_targets": {**supervisor.NEGATIVE_TARGETS, "disallowed_public": ("9.9.9.9", 443)},
                "provider_pins": ["1.1.1.1"],
            },
            {"schema": 1, "kind": "allowed", "nonce": validation.nonce},
            {"schema": 1, "kind": "key", "key": "public-synthetic-key-only"},
            {"schema": 1, "kind": "finish", "nonce": validation.nonce},
        ]
    )
    channel = Channel(incoming, credentials=(900, 0, 0))
    channel.settimeout = lambda value: None
    channel.connect = lambda path: events.append("root_ipc")
    channel.__class__.__enter__ = lambda self: self
    channel.__class__.__exit__ = lambda self, *args: None

    class Negative:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, value):
            pass

        def connect_ex(self, target):
            events.append("bpf_negative")
            return 13

    class SyntheticPath:
        def __init__(self, value):
            self.value = str(value)

        def __truediv__(self, name):
            return SyntheticPath(self.value + "/" + name)

        def __str__(self):
            return self.value

        def lstat(self):
            return types.SimpleNamespace(st_mode=stat.S_IFSOCK | 0o660, st_uid=0, st_gid=2002)

    class Upstream:
        def connect(self, deadline):
            events.append("tls_public_peer")
            return types.SimpleNamespace(getpeername=lambda: ("1.1.1.1", 443))

        def preflight(self, deadline):
            self.connect(deadline)
            return {"public_dns_pinned": True, "upstream_tls_verified": True}

    class Broker:
        def __init__(self, *args):
            self.upstream, self.deadline = Upstream(), supervisor.time.monotonic() + 180

        def authorize(self, *, gatekeeper, key_provider):
            facts = self.upstream.preflight(1)
            gatekeeper(binding, facts)
            events.append("root_allow_received")
            assert key_provider() == "public-synthetic-key-only"
            events.append("key_received")

        def summary(self):
            return broker_summary(validation)["summary"]

    module = types.SimpleNamespace(Broker=Broker, serve_broker=lambda *args: contextlib.nullcontext())
    namespace["importlib"] = types.SimpleNamespace(
        util=types.SimpleNamespace(
            module_from_spec=lambda spec: module, spec_from_loader=lambda *args, **kw: None
        )
    )
    namespace["Path"] = SyntheticPath
    namespace["root_source"] = lambda *args: events.append("source_verified") or b"pass\n"
    namespace["denied_open"] = lambda path, **kw: events.append("filesystem_canary") or not failed_canary
    namespace["os"] = types.SimpleNamespace(getuid=lambda: 2002, getpid=lambda: 654)
    namespace["sys"] = types.SimpleNamespace(
        platform="linux",
        flags=types.SimpleNamespace(isolated=True, no_site=True, dont_write_bytecode=True),
        argv=[
            "probe",
            json.dumps(
                {
                    "work": str(validation.work),
                    "broker_uid": 2002,
                    "broker_gid": 2002,
                    "root_pid": 900,
                    "transport_sha256": "a" * 64,
                    "nonce": validation.nonce,
                    "bridge_gid": 2003,
                    "issued_at": validation.issued,
                    "production_pid": 123,
                }
            ),
        ],
    )
    namespace["socket"] = types.SimpleNamespace(
        AF_UNIX=1,
        AF_INET=2,
        SOCK_STREAM=1,
        SOL_SOCKET=1,
        SO_PEERCRED=17,
        socket=lambda family, kind: channel if family == 1 else Negative(),
    )
    if failed_canary:
        with pytest.raises(RuntimeError, match="broker_filesystem_rejected"):
            namespace["main"]()
        assert events == ["filesystem_canary"] * 4 and not channel.outgoing
    else:
        namespace["main"]()
        stream = Channel(channel.outgoing)
        messages = [supervisor.receive(stream) for _ in range(5)]
        assert [value["kind"] for value in messages] == ["hello", "preflight", "key", "authorized", "summary"]
        assert events[:5] == ["filesystem_canary"] * 4 + ["source_verified"]
        assert (
            events.index("tls_public_peer")
            < events.index("root_allow_received")
            < events.index("key_received")
        )
        assert messages[1]["filesystem_checks"] == dict.fromkeys(supervisor.BROKER_FILESYSTEM_CHECKS, True)
        assert all(number == 13 for number in messages[1]["negative_errno"].values())
        assert "public-synthetic-key-only" not in json.dumps(messages)
