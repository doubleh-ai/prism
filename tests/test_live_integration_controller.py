"""Offline wiring and synthetic credential-boundary fixtures, never live receipts."""

import base64
import copy
import json
import os
import stat
import types
from contextlib import contextmanager
from pathlib import Path

import pytest
import test_release_controller as existing

from scripts import live_integration_validation as helper

remote, controller = existing.remote, existing.controller
NOW, COMMIT = existing.NOW, existing.COMMIT
ATTEMPT = "public-synthetic-live-20261022"
SYNTHETIC = "synthetic-deepseek-token-0123456789"


@pytest.fixture
def prepared(monkeypatch):
    fixture = existing.Fixture()
    fixture.setUp()
    fixture.prepared()
    engine = fixture.engine
    production = engine.production / "app.py"
    production.write_bytes(b"# public synthetic baseline\n")
    production.chmod(0o644)
    environment = engine.production / ".env"
    environment.write_bytes(f"MINIMAX_API_KEY=must-not-be-selected\nDEEPSEEK_API_KEY={SYNTHETIC}\n".encode())
    environment.chmod(0o600)
    manifest = {
        "files": [
            {
                "path": "app.py",
                "sha256": remote.digest(production.read_bytes()),
                "size": production.stat().st_size,
                "mode": "0644",
            }
        ]
    }
    data = copy.deepcopy(fixture.data)
    data.update(
        action="integration_live",
        integration_attempt=ATTEMPT,
        source_manifest=manifest,
        live_helpers={
            name: {
                "sha256": approved,
                "content": base64.b64encode((existing.ROOT / "scripts" / name).read_bytes()).decode(),
            }
            for name, approved in remote.LIVE_INTEGRATION_HELPERS.items()
        },
    )
    data["receipts"].update(
        expected_source_manifest_sha256=remote.digest(remote.encoded(manifest)),
        expected_current_fingerprint="f" * 64,
        model_environment={
            "sha256": remote.digest(environment.read_bytes()),
            "size": environment.stat().st_size,
            "mode": "0600",
        },
    )
    existing.bind(data)
    engine.request = data
    monkeypatch.setattr(
        engine, "runtime", lambda: {"working_directory": str(engine.production), "fingerprint": "f" * 64}
    )
    try:
        yield fixture, data
    finally:
        fixture.tearDown()


def test_four_source_pins_match_public_bytes_and_both_sides():
    assert controller.LIVE_INTEGRATION_HELPERS == remote.LIVE_INTEGRATION_HELPERS
    assert "integration_live" in controller.ACTIONS and "integration_live" in remote.ACTIONS
    assert helper.SAFE_ERRORS == remote.LIVE_ERRORS and helper.SOURCE_PINS == remote.LIVE_SOURCE_PINS
    for name, approved in remote.LIVE_INTEGRATION_HELPERS.items():
        assert remote.digest((existing.ROOT / "scripts" / name).read_bytes()) == approved


@pytest.mark.parametrize("change", ["missing", "digest", "source", "extra"])
def test_remote_rejects_any_unapproved_helper_bytes_before_loading(prepared, change):
    fixture, data = prepared
    if change == "missing":
        data["live_helpers"].pop("live_integration_policy.py")
    elif change == "digest":
        data["live_helpers"]["live_integration_worker.py"]["sha256"] = "d" * 64
    elif change == "source":
        data["live_helpers"]["live_integration_transport.py"]["content"] = base64.b64encode(
            b"raise RuntimeError('never executed')"
        ).decode()
    else:
        data["live_helpers"]["arbitrary.py"] = {"sha256": "c" * 64, "content": ""}
    with pytest.raises(remote.ReleaseError, match="approved_live_integration_helpers_required"):
        fixture.engine.live_helper_sources()


def test_root_only_loads_sealed_approved_supervisor_not_worker_or_candidate(prepared):
    fixture, data = prepared
    contents = fixture.engine.live_helper_sources()
    with fixture.engine.live_module(contents) as module:
        assert module.SOURCE_PINS == helper.SOURCE_PINS
        directory = Path(module.__file__).parent
        assert module.__file__ != str(fixture.engine.release / "scripts/live_integration_validation.py")
        assert stat.S_IMODE(directory.stat().st_mode) == 0o555
        assert set(path.name for path in directory.iterdir()) == set(remote.LIVE_INTEGRATION_HELPERS)
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o444 for path in directory.iterdir())
        assert not hasattr(module, "run_worker") and not hasattr(module, "Broker")
    assert fixture.engine._live_module is None


def mac_arguments(fixture, data, tmp_path):
    receipts = tmp_path / "reviewed-receipts.json"
    receipts.write_bytes(remote.encoded(data["receipts"]))
    source = tmp_path / "source-manifest.json"
    source.write_bytes(remote.encoded(data["source_manifest"]))
    values = {
        "action": "integration_live",
        "commit": COMMIT,
        "release_id": existing.RELEASE,
        "executor_sha256": remote.digest((existing.ROOT / "scripts/remote_release.py").read_bytes()),
        "receipts": str(receipts),
        "source_manifest": str(source),
        "external_source_manifest": None,
        "integration_attempt": ATTEMPT,
    }
    for name, approved in remote.LIVE_INTEGRATION_HELPERS.items():
        values[name.removesuffix(".py").replace("live_integration_", "live_") + "_sha256"] = approved
    return types.SimpleNamespace(**values)


def test_mac_requires_all_four_explicit_pins_new_attempt_and_opaque_receipt(prepared, tmp_path):
    fixture, data = prepared
    args = mac_arguments(fixture, data, tmp_path)
    request, _ = controller.request_for(args)
    assert request["live_helpers"] == data["live_helpers"] and request["integration_attempt"] == ATTEMPT
    assert request["receipts"]["model_environment"] == data["receipts"]["model_environment"]
    for name in (
        "live_validation_sha256",
        "live_worker_sha256",
        "live_transport_sha256",
        "live_policy_sha256",
    ):
        saved = getattr(args, name)
        setattr(args, name, None)
        with pytest.raises(controller.ControlError, match="approved_live_integration_helpers_required"):
            controller.request_for(args)
        setattr(args, name, saved)
    args.integration_attempt = None
    with pytest.raises(controller.ControlError, match="explicit_live_integration_attempt_required"):
        controller.request_for(args)
    args.integration_attempt = "a" * 41
    with pytest.raises(controller.ControlError, match="explicit_live_integration_attempt_required"):
        controller.request_for(args)


def gate_fixture(prepared, monkeypatch):
    fixture, data = prepared
    engine = fixture.engine
    engine._live_module, engine._live_attempt = helper, ATTEMPT
    monkeypatch.setattr(remote, "seal_executor_memory", lambda: True)  # No offline process limits change.
    work = engine.operations / "integration" / ATTEMPT / "live"
    work.mkdir(parents=True, mode=0o755)
    (work / "output").mkdir(mode=0o700)
    for name in remote.LIVE_SOURCE_PINS:
        (work / (name + ".py")).write_bytes(
            (existing.ROOT / "scripts" / ("live_integration_" + name + ".py")).read_bytes()
        )
        (work / (name + ".py")).chmod(0o644)
    nonce = "d" * 32
    accounts = {
        name: types.SimpleNamespace(pw_name=name, pw_uid=uid, pw_gid=uid, pw_shell="/usr/sbin/nologin")
        for name, uid in (
            ("prism-integration", 2001),
            ("prism-integration-broker", 2002),
            ("ubuntu", os.getuid()),
            ("prism-stage", 2004),
        )
    }
    monkeypatch.setattr(remote.pwd, "getpwnam", accounts.__getitem__)
    processes = {}
    for pid, uid in ((1, 0), (111, os.getuid()), (321, 2001), (654, 2002)):
        process = fixture.root / "proc" / str(pid)
        process.mkdir(parents=True, exist_ok=True)
        (process / "status").write_text("Name:\tpublic synthetic\nUid:\t" + "\t".join([str(uid)] * 4) + "\n")
        (process / "stat").write_text(
            f"{pid} (public synthetic proc) S " + "0 " * 18 + str(500 if pid == 321 else 501)
        )
        (process / "ns").mkdir()
        (process / "limits").write_text("Max core file size 0 0 bytes\n")
        processes[pid] = process
    (processes[1] / "ns/net").touch()
    (processes[321] / "ns/net").touch()
    for pid in (111, 654):
        os.link(processes[1] / "ns/net", processes[pid] / "ns/net")
    binding = {
        "candidate": COMMIT,
        "cli_sha256": fixture.cli_digest,
        "probe_sha256": remote.LIVE_SOURCE_PINS["worker"],
        "nonce": nonce,
        "worker_pid": 321,
        "broker_pid": 654,
    }
    kernel = {
        "worker_uids": [2001] * 4,
        "broker_uids": [2002] * 4,
        "worker_netns_inode": (processes[321] / "ns/net").stat().st_ino,
        "broker_netns_inode": (processes[654] / "ns/net").stat().st_ino,
        "host_netns_inode": (processes[1] / "ns/net").stat().st_ino,
        "worker_startticks": 500,
        "broker_startticks": 501,
    }
    states = {}
    for role, pid, uid in (("worker", 321, 2001), ("broker", 654, 2002)):
        unit = "prism-live-" + role + "-" + COMMIT[:12] + "-" + nonce + ".service"
        (processes[pid] / "cgroup").write_text("0::/system.slice/" + unit + "\n")
        states[unit] = {
            "MainPID": str(pid),
            "ExecMainPID": str(pid),
            "InvocationID": "e" * 32,
            "User": "prism-integration" + ("-broker" if role == "broker" else ""),
            "Group": "prism-integration" + ("-broker" if role == "broker" else ""),
            "ControlGroup": "/system.slice/" + unit,
            "NoNewPrivileges": "yes",
            "ProtectSystem": "strict",
            "ProtectHome": "yes",
            "PrivateNetwork": "yes" if role == "worker" else "no",
            "KillMode": "control-group",
            "LimitCORE": "0",
        }
    original = engine.run
    engine.run = lambda argv, **kwargs: (
        "\n".join(key + "=" + value for key, value in states[argv[-1]].items()).encode()
        if argv[-1] in states
        else original(argv, **kwargs)
    )
    monkeypatch.setattr(engine, "stage_runtime_mount", lambda process: True)  # Explicit synthetic OS witness.
    reader = engine.integration_read
    monkeypatch.setattr(
        engine, "integration_read", lambda path, uid: reader(path, os.getuid())
    )  # Test files cannot chown to Linux UID.
    proof = {
        "schema": 1,
        "binding": binding,
        "production_pid": 111,
        "issued_at": NOW,
        "checked_at": NOW,
        "sources": remote.LIVE_SOURCE_PINS,
        "checks": dict.fromkeys(helper.BOOT_CHECKS, True),
    }
    (work / "output/bootstrap-proof.json").write_bytes(remote.encoded(proof))
    (work / "output/bootstrap-proof.json").chmod(0o600)
    allow = {
        "schema": 1,
        "binding": binding,
        "policy_sha256": remote.LIVE_SOURCE_PINS["policy"],
        "broker_memory_sealed": True,
        "bootstrap_proof_sha256": remote.digest(remote.encoded(proof)),
        "allowed_at": NOW,
        "witness": {"binding": binding, "checked_at": NOW, "checks": dict.fromkeys(helper.WITNESSES, True)},
        "kernel": kernel,
        "sources": remote.LIVE_SOURCE_PINS,
    }
    (work / "root-allow.json").write_bytes(remote.encoded(allow))
    (work / "root-allow.json").chmod(0o444)
    return engine, data, work, binding, allow, states, processes


def test_reader_has_no_credential_open_without_real_gate_even_when_action_and_permit_present(
    prepared, monkeypatch
):
    fixture, data = prepared
    engine = fixture.engine
    opened = []
    original = remote.os.open
    monkeypatch.setattr(
        remote.os,
        "open",
        lambda path, *args, **kwargs: opened.append(Path(path)) or original(path, *args, **kwargs),
    )
    with pytest.raises(remote.ReleaseError, match="^live_model_authorization_rejected$"):
        engine.read_bound_model_key(engine.operations / "integration" / ATTEMPT / "live", {})
    assert engine.production / ".env" not in opened


def test_executor_memory_guard_failure_is_before_any_env_open(prepared, monkeypatch):
    engine, data, work, binding, *_ = gate_fixture(prepared, monkeypatch)
    monkeypatch.setattr(remote, "seal_executor_memory", lambda: False)
    original, opened = remote.os.open, []
    monkeypatch.setattr(
        remote.os,
        "open",
        lambda path, *args, **kwargs: opened.append(Path(path)) or original(path, *args, **kwargs),
    )
    with pytest.raises(remote.ReleaseError, match="live_executor_memory_unsealed"):
        engine.read_bound_model_key(work, binding)
    assert engine.production / ".env" not in opened


def test_valid_gate_reads_only_synthetic_single_key_and_excludes_env_from_source_inventory(
    prepared, monkeypatch
):
    engine, data, work, binding, *_ = gate_fixture(prepared, monkeypatch)
    assert engine.live_model_gate(work) == binding
    assert engine.read_bound_model_key(work, binding) == SYNTHETIC
    with pytest.raises(remote.ReleaseError, match="live_model_credential_unavailable"):
        engine.read_bound_model_key(work, binding)
    assert all(item["path"] != ".env" for item in data["source_manifest"]["files"])
    with pytest.raises(remote.ReleaseError, match="hidden_path_forbidden"):
        remote.safe_path(".env")
    assert prepared[0].boundaries[-2:] == ["live_credential_access", "live_credential_reconcile"]


@pytest.mark.parametrize("failure", [None, "rlimit", "setdumpable", "getdumpable"])
def test_executor_memory_guard_sets_and_reads_only_current_process(failure, monkeypatch):
    calls = []
    monkeypatch.setattr(remote.sys, "platform", "linux")
    monkeypatch.setattr(remote.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        remote.resource, "setrlimit", lambda kind, value: calls.append(("limit", kind, value))
    )
    monkeypatch.setattr(remote.resource, "getrlimit", lambda kind: (0, 1) if failure == "rlimit" else (0, 0))

    def prctl(*args):
        calls.append(args)
        return 1 if failure == ("setdumpable" if args[0] == 4 else "getdumpable") else 0

    monkeypatch.setattr(remote.ctypes, "CDLL", lambda path, **kwargs: types.SimpleNamespace(prctl=prctl))
    if failure:
        with pytest.raises(remote.ReleaseError, match="live_executor_memory_unsealed"):
            remote.seal_executor_memory()
    else:
        assert remote.seal_executor_memory() is True
        assert calls == [("limit", remote.resource.RLIMIT_CORE, (0, 0)), (4, 0, 0, 0, 0), (3, 0, 0, 0, 0)]


@pytest.mark.parametrize(
    "change",
    [
        "allow_missing",
        "allow_mode",
        "allow_hardlink",
        "expired",
        "nonce",
        "proof",
        "uid",
        "startticks",
        "ns",
        "run",
        "core",
        "source",
        "core_kernel",
        "broker_memory",
    ],
)
def test_bad_gate_stops_before_any_env_open(prepared, monkeypatch, change):
    engine, data, work, binding, allow, states, processes = gate_fixture(prepared, monkeypatch)
    path = work / "root-allow.json"
    if change == "allow_missing":
        path.unlink()
    elif change == "allow_mode":
        path.chmod(0o644)
    elif change == "allow_hardlink":
        os.link(path, work / "other")
    elif change in {"expired", "nonce", "broker_memory"}:
        if change == "expired":
            allow["allowed_at"] = NOW - 121
        elif change == "nonce":
            allow["binding"]["nonce"] = "f" * 32
        else:
            allow["broker_memory_sealed"] = False
        path.chmod(0o600)
        path.write_bytes(remote.encoded(allow))
        path.chmod(0o444)
    elif change == "proof":
        (work / "output/bootstrap-proof.json").write_bytes(b"{}")
    elif change == "uid":
        (processes[321] / "status").write_text("Uid:\t2001\t0\t2001\t2001\n")
    elif change == "startticks":
        (processes[321] / "stat").write_text("321 (public synthetic) S " + "0 " * 18 + "999")
    elif change == "ns":
        (processes[321] / "ns/net").unlink()
        os.link(processes[1] / "ns/net", processes[321] / "ns/net")
    elif change == "run":
        monkeypatch.setattr(engine, "stage_runtime_mount", lambda process: False)
    elif change == "core":
        next(iter(states.values()))["LimitCORE"] = "infinity"
    elif change == "core_kernel":
        (processes[321] / "limits").write_text("Max core file size 0 unlimited bytes\n")
    else:
        (work / "worker.py").write_bytes(b"# unapproved source\n")
    opened = []
    original = remote.os.open
    monkeypatch.setattr(
        remote.os,
        "open",
        lambda path, *args, **kwargs: opened.append(Path(path)) or original(path, *args, **kwargs),
    )
    with pytest.raises(remote.ReleaseError, match="^live_model_authorization_rejected$"):
        engine.read_bound_model_key(work, binding)
    assert engine.production / ".env" not in opened


@pytest.mark.parametrize(
    "change", ["hash", "size", "mode", "hardlink", "replace_after_parse", "stop_before", "stop_after"]
)
def test_reader_preserves_fd_binding_and_operator_boundaries(prepared, monkeypatch, change):
    engine, data, work, binding, *_ = gate_fixture(prepared, monkeypatch)
    path = engine.production / ".env"
    if change in {"hash", "size", "mode"}:
        data["receipts"]["model_environment"][{"hash": "sha256", "size": "size", "mode": "mode"}[change]] = {
            "hash": "c" * 64,
            "size": 1,
            "mode": "0644",
        }[change]
    elif change == "hardlink":
        os.link(path, engine.production / "synthetic-env-link")
    elif change == "replace_after_parse":
        original = remote.selected_model_key

        def replace(raw):
            value = original(raw)
            replacement = engine.production / "synthetic-replacement"
            replacement.write_bytes(raw)
            replacement.chmod(0o600)
            replacement.replace(path)
            return value

        monkeypatch.setattr(remote, "selected_model_key", replace)
    else:
        original = engine.boundary

        def boundary(label):
            if label == (
                "live_credential_access" if change == "stop_before" else "live_credential_reconcile"
            ):
                raise remote.ReleaseError("operator_stop")
            original(label)

        monkeypatch.setattr(engine, "boundary", boundary)
    with pytest.raises(remote.ReleaseError) as error:
        engine.read_bound_model_key(work, binding)
    assert str(error.value) in {"live_model_credential_unavailable", "operator_stop"}
    assert SYNTHETIC not in str(error.value)


def safe_summary(data):
    return {
        "schema": 1,
        "action": "integration_live",
        "candidate": COMMIT,
        "release_id": ATTEMPT,
        "source_sha256": remote.LIVE_SOURCE_PINS,
        "started_at": NOW,
        "finished_at": NOW,
        "stage": "complete",
        "passed": True,
        "error_class": None,
        "live_model_verified": True,
        "native_search_verified": True,
        "owned_mcp_verified": True,
        "server_clock_verified": True,
        "native_fetch_verified": False,
        "market_provider_verified": False,
        "research_quality_verified": False,
        "production_verified": False,
        "units_stopped": True,
        "cleanup_error_class": None,
        "broker_negative_errno": dict.fromkeys([*helper.NEGATIVE_TARGETS, "disallowed_public"], 13),
        "broker_filesystem_checks": dict.fromkeys(helper.BROKER_FILESYSTEM_CHECKS, True),
        "broker_preflight_facts": dict.fromkeys(
            ("public_dns_pinned", "upstream_tls_verified", "broker_memory_sealed"), True
        ),
        "broker_memory_sealed": True,
        "provider_peer_count": 1,
        "bootstrap_proof_sha256": "c" * 64,
        "worker_result_sha256": "d" * 64,
        "blocked_provider_header_names": [],
        "official_source_count": 1,
        "broker_summary_sha256": "e" * 64,
        "requests": {"messages": 2, "count_tokens": 0, "quote": 0, "kline": 0, "public_fetch": 0},
    }


@pytest.mark.parametrize(
    "change", ["raw", "quality", "fetch", "market", "production", "count", "cleanup", "error"]
)
def test_safe_journal_rejects_raw_fields_or_inconsistent_success(prepared, change):
    fixture, data = prepared
    value = safe_summary(data)
    if change == "raw":
        value["raw_response"] = SYNTHETIC
    elif change in {"quality", "fetch", "market", "production"}:
        value[
            {
                "quality": "research_quality_verified",
                "fetch": "native_fetch_verified",
                "market": "market_provider_verified",
                "production": "production_verified",
            }[change]
        ] = True
    elif change == "count":
        value["requests"]["messages"] = 7
    elif change == "cleanup":
        value["units_stopped"] = False
    else:
        value["error_class"] = SYNTHETIC
    with pytest.raises(remote.ReleaseError, match="^live_safe_record_rejected$"):
        fixture.engine.live_safe_record(value, ATTEMPT)


@pytest.mark.parametrize("mode", ["pass", "fail", "stop", "missing_record", "unsafe_record", "bad_command"])
def test_fixed_orchestration_persists_safe_failure_and_never_calls_key_early(prepared, monkeypatch, mode):
    fixture, data = prepared
    engine = fixture.engine
    callback_calls = []

    def run_validation(bindings, *, boundary, run, record, key_provider):
        assert bindings["candidate"] == COMMIT and bindings["release_id"] == ATTEMPT
        assert set(bindings["sources"]) == set(remote.LIVE_SOURCE_PINS)
        if mode == "stop":
            with pytest.raises(helper.ValidationError, match="operator_stop"):
                boundary("live_accounts")
        if mode == "bad_command":
            with pytest.raises(remote.ReleaseError, match="live_command_rejected"):
                run(["/usr/bin/systemctl", "restart", "stock-app.service"], timeout=1)
        if mode == "unsafe_record":
            with pytest.raises(remote.ReleaseError, match="live_safe_record_rejected"):
                record(dict(safe_summary(data), raw_secret=SYNTHETIC))
        if mode in {"pass", "unsafe_record", "bad_command", "missing_record"}:
            # Explicit synthetic Linux facts; no real credentials or network.
            gate_fixture(prepared, monkeypatch)
            assert key_provider() == SYNTHETIC
        value = safe_summary(data)
        if mode in {"fail", "stop"}:
            value.update(passed=False, error_class="worker_failed", stage="public_qa")
        if mode != "missing_record":
            record(value)
        callback_calls.append("returned")
        return value

    @contextmanager
    def module(contents):
        assert set(contents) == set(remote.LIVE_INTEGRATION_HELPERS)
        yield types.SimpleNamespace(
            run_validation=run_validation,
            ValidationError=helper.ValidationError,
            RESOLVE_CODE=helper.RESOLVE_CODE,
        )

    monkeypatch.setattr(engine, "live_module", module)
    monkeypatch.setattr(remote, "seal_executor_memory", lambda: True)
    if mode == "stop":
        original = engine.boundary

        def stopped(label):
            if label == "live_accounts":
                raise remote.ReleaseError("operator_stop")
            original(label)

        monkeypatch.setattr(engine, "boundary", stopped)
    if mode in {"fail", "stop", "missing_record"}:
        with pytest.raises((remote.ReleaseError, helper.ValidationError)):
            engine.execute(data)
    else:
        result = engine.execute(data)
        assert result["integration"]["summary"]["live_model_verified"]
        assert result["production_verified"] is False
    directory = engine.record / ("integration-live-" + ATTEMPT)
    receipt = json.loads((directory / "receipt.json").read_bytes())
    raw = (directory / "receipt.json").read_text() + (directory / "events.jsonl").read_text()
    assert SYNTHETIC not in raw and "raw_secret" not in raw
    assert receipt["production_verified"] is False
    assert not any("restart" in argv for argv, _ in fixture.commands)
    original_receipt = (directory / "receipt.json").read_bytes()
    with pytest.raises(remote.ReleaseError, match="live_integration_attempt_already_exists"):
        engine.execute(data)
    assert (directory / "receipt.json").read_bytes() == original_receipt


@pytest.mark.parametrize(
    "kind", ["resolve", "worker", "broker", "other_python", "other_unit", "glob", "query_flag"]
)
def test_actual_runner_captures_only_fixed_live_dns_and_two_exact_unit_inventories(monkeypatch, kind):
    unit = "prism-live-worker-" + COMMIT[:12] + "-" + "a" * 32 + ".service"
    inventories = ["/usr/bin/systemctl", "list-units", "--all", "--plain", "--no-legend"]
    argv = {
        "resolve": remote.LIVE_RESOLVE_COMMAND,
        "worker": inventories + [unit],
        "broker": inventories + [unit.replace("-worker-", "-broker-")],
        "other_python": ["/usr/bin/python3", "-I", "-B", "-S", "-c", "print('untrusted')"],
        "other_unit": inventories + ["stock-app.service"],
        "glob": inventories + ["prism-live-*.service"],
        "query_flag": inventories + [unit, "--full"],
    }[kind]
    calls = []

    def subprocess_run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return types.SimpleNamespace(
            returncode=0,
            stdout=b"actual-public-output\n" if kwargs["stdout"] == remote.subprocess.PIPE else None,
        )

    monkeypatch.setattr(remote.subprocess, "run", subprocess_run)
    actual = remote.run_command(argv, timeout=8)
    captured = kind in {"resolve", "worker", "broker"}
    assert actual == (b"actual-public-output\n" if captured else b"")
    assert calls[0][1]["stdout"] == (remote.subprocess.PIPE if captured else remote.subprocess.DEVNULL)
    assert calls[0][1]["stderr"] == remote.subprocess.DEVNULL
    assert remote.LIVE_RESOLVE_CODE == helper.RESOLVE_CODE


@pytest.mark.parametrize("code", [0, 11, 110, 111, 4095])
def test_safe_failed_preflight_keeps_bounded_evidence_but_cannot_pass(prepared, code):
    fixture, data = prepared
    value = safe_summary(data)
    value.update(passed=False, error_class="broker_preflight_rejected", stage="preflight")
    value["broker_negative_errno"]["production"] = code
    value["broker_preflight_facts"]["upstream_tls_verified"] = False
    value["broker_filesystem_checks"]["config_write_denied"] = False
    actual = fixture.engine.live_safe_record(value, ATTEMPT)
    assert actual["broker_negative_errno"]["production"] == code
    assert actual["broker_preflight_facts"]["upstream_tls_verified"] is False
    value.update(passed=True, error_class=None, stage="complete")
    with pytest.raises(remote.ReleaseError, match="live_safe_record_rejected"):
        fixture.engine.live_safe_record(value, ATTEMPT)


@pytest.mark.parametrize(
    "change", ["negative", "overflow", "boolean", "raw_fact", "extra_target", "extra_fact"]
)
def test_safe_preflight_failure_never_persists_arbitrary_diagnostics(prepared, change):
    fixture, data = prepared
    value = safe_summary(data)
    value.update(passed=False, error_class="broker_preflight_rejected", stage="preflight")
    if change == "negative":
        value["broker_negative_errno"]["production"] = -1
    elif change == "overflow":
        value["broker_negative_errno"]["production"] = 4096
    elif change == "boolean":
        value["broker_negative_errno"]["production"] = True
    elif change == "raw_fact":
        value["broker_preflight_facts"]["upstream_tls_verified"] = SYNTHETIC
    elif change == "extra_target":
        value["broker_negative_errno"][SYNTHETIC] = 13
    else:
        value["broker_preflight_facts"][SYNTHETIC] = True
    with pytest.raises(remote.ReleaseError, match="live_safe_record_rejected"):
        fixture.engine.live_safe_record(value, ATTEMPT)
