"""Offline fixed stop-controller tests; synthetic observations are not Linux proof."""

import base64
import copy
import json
import os
import sys
import types
from contextlib import contextmanager
from unittest import mock

import pytest
import test_release_controller as existing
import test_retirement_controller as signal

from scripts import retirement_probe as probe
from scripts import retirement_stop_validation as stop
from scripts import retirement_validation as validation

remote, controller = existing.remote, existing.controller
prepared = signal.prepared
NONCE = "c" * 32
PROOF = "retirement-proof-005"
ATTEMPT = "retirement-stop-001"


def sources():
    return {
        name: {
            "sha256": sha,
            "content": base64.b64encode((existing.ROOT / "scripts" / name).read_bytes()).decode(),
        }
        for name, sha in remote.RETIREMENT_STOP_HELPERS.items()
    }


def bindings():
    return {
        "commit": existing.COMMIT,
        "release_id": existing.RELEASE,
        "base_environment_sha256": remote.RETIREMENT_BASE_ENV_SHA256,
    }


def seed_proof(engine, baseline):
    """Explicit fake seven-case ledger; never used as an actual deployment receipt."""
    directory = engine.record / "legacy-retirement" / PROOF
    directory.mkdir(parents=True, mode=0o700)
    engine.write(
        directory / "identity.json",
        remote.encoded(
            {
                "commit": existing.COMMIT,
                "attempt": PROOF,
                "helpers": remote.RETIREMENT_HELPERS,
                "baseline": baseline,
            }
        ),
    )
    summary, entries, chain = signal.fake_summary(bindings()), [], "0" * 64

    def add(event, details):
        nonlocal chain
        line = (
            remote.encoded(
                {
                    "sequence": len(entries) + 1,
                    "at": existing.NOW,
                    "previous_sha256": chain,
                    "event": event,
                    "details": details,
                }
            )
            + b"\n"
        )
        entries.append(line)
        chain = remote.digest(line)

    plan = probe.descriptor(bindings(), nonce=NONCE)
    for case in summary["cases"]:
        case_id = case["case_id"]
        descriptor = probe._case(plan, case_id)
        add("synthetic_start_intent", {"case_id": case_id, "nonce": NONCE})
        add(
            "actual_isolation_verified",
            {
                "case_id": case_id,
                "invocation_id": "d" * 32,
                "main_pid": 222,
                "start_ticks": 123,
                "uid": 61234,
                "control_group": descriptor["control_group"],
                "actual_private_network_and_mounts": True,
                "runtime_bind_same_inode": True,
                "production_directory_access_denied": True,
                "inet_socket_creation_denied": True,
            },
        )
        add(
            "synthetic_signal_intent",
            {
                "case_id": case_id,
                "invocation_id": "d" * 32,
                "signal": descriptor["signal"],
                "signal_at_monotonic_us": 1000,
            },
        )
        add(
            "synthetic_exit_observed",
            {"case_id": case_id, "recursive_member_count": 0, "exit_code": 1, "exit_status": 0},
        )
        add("synthetic_case_result", case)
    add("retirement_validation_result", summary)
    engine.write(directory / "events.jsonl", b"".join(entries))
    engine.write(
        directory / "receipt.json",
        remote.encoded(
            {
                "attempt": PROOF,
                "summary": summary,
                "events": len(entries),
                "last_event_sha256": chain,
                "production_verified": False,
                "passed": True,
                "baseline_verified_after": True,
                "failure_code": None,
            }
        ),
    )
    return directory


def summary_value():
    plan = stop.descriptor(
        bindings(), expected_python_sha256=remote.RETIREMENT_SYSTEM_PYTHON_SHA256, nonce=NONCE
    )
    return {
        "schema": 1,
        "bindings": bindings(),
        "stop_binding_sha256": plan["binding_sha256"],
        "retirement_stop_validation_passed": True,
        "systemctl_stop_end_to_end_proven": True,
        "synthetic_cleanup_verified": True,
        "actual_linux_collection": True,
        "cases": [
            {
                "case_id": case,
                "passed": True,
                "clean_exit_zero": True,
                "executor_results_preserved": True,
                "expected_counterexample": False,
                "production_quiescence_proven": False,
                "task_census_still_required": True,
            }
            for case in stop.CASES
        ],
        "production_quiescence_proven": False,
        "all_production_memory_persisted": False,
        "task_census_still_required": True,
        "python_version": "3.12.3",
        "python_binary_sha256": remote.RETIREMENT_SYSTEM_PYTHON_SHA256,
        "probe_source_sha256": stop.PROBE_SOURCE_SHA256,
        "validation_source_sha256": stop.VALIDATION_SOURCE_SHA256,
        "bundle_sha256": probe.bundle_identity(),
        "uvicorn_server_sha256": probe.UVICORN_SHA256,
        "uvloop_binary_sha256": probe.UVLOOP_SHA256,
        "policy_sha256": probe.sha256(probe.encoded(stop.POLICY)),
    }


def wire(prepared, monkeypatch, mode=None):
    engine = prepared.engine
    baseline = {
        "production_pid": 111,
        "runtime_fingerprint": "f" * 64,
        "source_manifest_sha256": "e" * 64,
        "base_environment_sha256": remote.RETIREMENT_BASE_ENV_SHA256,
    }
    monkeypatch.setattr(engine, "retirement_baseline", lambda: dict(baseline))
    monkeypatch.setattr(
        engine,
        "live_original_state",
        lambda: {key: baseline[key] for key in ("production_pid", "runtime_fingerprint")},
    )
    data = copy.deepcopy(prepared.data)
    data.update(
        action="validate_legacy_stop",
        retirement_stop_helpers=sources(),
        retirement_stop_attempt=ATTEMPT,
        retirement_proof_attempt=PROOF,
    )
    data["receipts"]["system_python_sha256"] = remote.RETIREMENT_SYSTEM_PYTHON_SHA256
    existing.bind(data)
    proof = seed_proof(engine, baseline)
    seen = []

    def execute(request, *, expected_python_sha256, boundary, run, record):
        assert request == bindings() and expected_python_sha256 == remote.RETIREMENT_SYSTEM_PYTHON_SHA256
        result = summary_value()
        if mode in {"stock-stop", "kill", "restart", "wrong-nonce", "timeout", "unintended-python"}:
            argv = {
                "stock-stop": ["/usr/bin/systemctl", "stop", "--no-block", "stock-app.service"],
                "kill": ["/usr/bin/systemctl", "kill", "--kill-whom=main", "stock-app.service"],
                "restart": ["/usr/bin/systemctl", "restart", "stock-app.service"],
                "wrong-nonce": [
                    "/usr/bin/systemctl",
                    "show",
                    "--all",
                    "--property=" + stop.PROPERTIES,
                    "prism-retirement-probe-" + "f" * 32 + "-int-executor.service",
                ],
                "unintended-python": ["/usr/bin/python3.12", "-c", "print('unsafe')"],
                "timeout": stop.VERSION_COMMAND,
            }[mode]
            if mode == "wrong-nonce":
                record("synthetic_start_intent", {"case_id": "int-executor", "nonce": NONCE})
            run(argv, timeout=100 if mode == "timeout" else 10)
        run(stop.VERSION_COMMAND, timeout=10)
        run(stop.OBSERVER_INVENTORY, timeout=10)
        run(["/usr/bin/systemctl", "show", "--property=" + validation.PROPERTIES, remote.SERVICE], timeout=10)
        try:
            boundary("retirement_copy_public_libraries")
        except remote.ReleaseError:
            if mode != "caught-stop":
                raise
            result.update(
                retirement_stop_validation_passed=False,
                systemctl_stop_end_to_end_proven=False,
                synthetic_cleanup_verified=False,
                cases=[],
                error_code="retirement_stop_boundary_or_command_failed",
            )
            record("retirement_stop_validation_result", result)
            return result
        record(
            "public_bundles_verified",
            {"bundle_sha256": result["bundle_sha256"], "wrapper_sha256": validation.wrapper_directory().name},
        )
        plan = probe.descriptor(request, nonce=NONCE)
        for case in result["cases"]:
            case_id = case["case_id"]
            descriptor = probe._case(plan, case_id)
            boundary("retirement_stop_start_" + case_id)
            run(
                ["/usr/bin/systemctl", "show", "--property=LoadState", descriptor["unit"]],
                timeout=10,
            )
            record("synthetic_start_intent", {"case_id": case_id, "nonce": NONCE})
            run(validation.launch_command(plan, case_id), timeout=15)
            if mode == "duplicate-launch":
                run(validation.launch_command(plan, case_id), timeout=15)
            record(
                "synthetic_stop_actual_isolation",
                {
                    "case_id": case_id,
                    "invocation": "d" * 32,
                    "pid": 222,
                    "uid": 61234,
                    "start_ticks": 123,
                    "actual_kernel_verified": True,
                },
            )
            for argv in stop.exec_array_commands(descriptor["unit"]):
                run(argv, timeout=10)
            if mode != "missing-exec-proof":
                record(
                    "synthetic_stop_exec_arrays_observed",
                    {
                        "case_id": case_id,
                        "properties": {key: stop.EMPTY_EXEC_ARRAY for key in ("ExecStop", "ExecStopPost")},
                    },
                )
            boundary("retirement_stop_observer_" + case_id)
            record("synthetic_observer_start_intent", {"case_id": case_id})
            run(stop.observer_command(plan, case_id), timeout=15)
            boundary("retirement_stop_prepare_" + case_id)
            record(
                "synthetic_stop_intent",
                {
                    "case_id": case_id,
                    "invocation_id": "d" * 32,
                    "pid": 222,
                    "start_ticks": 123,
                    "stop_at_monotonic_us": 1000,
                },
            )
            boundary("retirement_stop_dispatch_" + case_id)
            if mode != "missing-commit":
                record("synthetic_stop_committed", {"case_id": case_id, "invocation_id": "d" * 32})
            ledger = engine.record / "legacy-stop" / ATTEMPT / "events.jsonl"
            seen.append(json.loads(ledger.read_bytes().splitlines()[-1])["event"])
            run(["/usr/bin/systemctl", "stop", "--no-block", descriptor["unit"]], timeout=10)
            if mode == "duplicate-stop":
                run(["/usr/bin/systemctl", "stop", "--no-block", descriptor["unit"]], timeout=10)
            record(
                "synthetic_stop_runner_join_observed",
                {"case_id": case_id, "stop_job_waited_with_executor_pending": True},
            )
            record(
                "synthetic_stop_case_result",
                {
                    **case,
                    "systemctl_stop_end_to_end_proven": True,
                    "stop_dispatches": 1,
                    "exit_monotonic_us": 1001,
                },
            )
            record("synthetic_observer_release_intent", {"case_id": case_id, "invocation_id": "e" * 32})
            run(["/usr/bin/systemctl", "stop", "--no-block", stop.observer_unit(plan, case_id)], timeout=10)
        if mode == "scope":
            result["production_quiescence_proven"] = True
        if mode == "failure":
            result.update(
                retirement_stop_validation_passed=False,
                systemctl_stop_end_to_end_proven=False,
                synthetic_cleanup_verified=False,
                error_code="retirement_stop_fixture_failed",
            )
        if mode == "post-baseline":
            baseline["production_pid"] = 333
        if mode != "unrecorded":
            record("retirement_stop_validation_result", result)
        if mode == "after-summary":
            record("bundle_copy_intent", {"bundle_sha256": result["bundle_sha256"]})
        return result

    @contextmanager
    def modules(directory, *, stop=False):
        assert stop
        facade = types.SimpleNamespace(
            **{k: v for k, v in vars(globals()["stop"]).items() if not k.startswith("__")}
        )
        facade.run_stop_validation = execute
        yield probe, validation, facade

    monkeypatch.setattr(engine, "retirement_modules", modules)
    return data, baseline, proof, seen


def test_three_literal_helpers_sealed_without_candidate_import(prepared):
    engine = prepared.engine
    engine.request = dict(prepared.data, retirement_stop_helpers=sources())
    content = engine.retirement_sources(stop=True)
    assert remote.RETIREMENT_STOP_HELPERS == controller.RETIREMENT_STOP_HELPERS
    assert all(remote.digest(raw) == remote.RETIREMENT_STOP_HELPERS[name] for name, raw in content.items())
    directory = engine.install_retirement_helpers(content, stop=True)
    with mock.patch.dict(sys.modules):
        for name in (
            "scripts",
            "scripts.retirement_probe",
            "scripts.retirement_validation",
            "scripts.retirement_stop_validation",
        ):
            sys.modules.pop(name, None)
        with engine.retirement_modules(directory, stop=True) as (p, v, s):
            assert s.probe is p and s.base is v
            assert s.PROBE_SOURCE_SHA256 == remote.RETIREMENT_HELPERS["retirement_probe.py"]
            assert s.VALIDATION_SOURCE_SHA256 == remote.RETIREMENT_HELPERS["retirement_validation.py"]
    assert {path.name for path in (directory / "scripts").iterdir()} == set(remote.RETIREMENT_STOP_HELPERS)


def test_stop_requires_prior_actual_seven_case_ledger_and_durable_intents(prepared, monkeypatch):
    data, _, proof, seen = wire(prepared, monkeypatch)
    result = prepared.engine.execute(data)["legacy_stop"]
    assert result["passed"] is True and result["baseline_verified_after"] is True
    assert result["summary"]["systemctl_stop_end_to_end_proven"] is True
    assert result["summary"]["production_quiescence_proven"] is False
    assert result["summary"]["all_production_memory_persisted"] is False
    assert result["production_verified"] is False
    assert seen == ["synthetic_stop_committed", "synthetic_stop_committed"]
    assert result["signal_proof"]["receipt_sha256"] == remote.digest((proof / "receipt.json").read_bytes())
    directory = prepared.engine.record / "legacy-stop" / ATTEMPT
    previous = "0" * 64
    for number, line in enumerate((directory / "events.jsonl").read_bytes().splitlines(keepends=True), 1):
        row = json.loads(line)
        assert row["sequence"] == number and row["previous_sha256"] == previous
        previous = remote.digest(line)
    assert previous == result["last_event_sha256"]
    original = (directory / "receipt.json").read_bytes()
    with pytest.raises(remote.ReleaseError, match="retirement_stop_attempt_already_exists"):
        prepared.engine.execute(data)
    assert (directory / "receipt.json").read_bytes() == original
    stops = [argv[-1] for argv, _ in prepared.commands if argv[:2] == ["/usr/bin/systemctl", "stop"]]
    assert len(stops) == 4 and all(unit.startswith("prism-retirement-") for unit in stops)
    assert not any(argv[:2] == ["/usr/bin/systemctl", "kill"] for argv, _ in prepared.commands)


@pytest.mark.parametrize(
    "change",
    ["missing", "baseline", "pins", "failed", "counterexample", "chain", "extra", "hardlink", "mode"],
)
def test_prior_signal_proof_fail_closed_before_any_stop(prepared, monkeypatch, change):
    data, _, directory, _ = wire(prepared, monkeypatch)
    path = directory / ("identity.json" if change in {"baseline", "pins"} else "receipt.json")
    value = json.loads(path.read_bytes())
    if change == "missing":
        data["retirement_proof_attempt"] = "nonexistent-proof"
    elif change == "baseline":
        value["baseline"]["source_manifest_sha256"] = "0" * 64
    elif change == "pins":
        value["helpers"]["retirement_validation.py"] = "0" * 64
    elif change == "failed":
        value["passed"] = False
    elif change == "counterexample":
        value["summary"]["cases"][-1]["executor_results_preserved"] = True
    elif change == "chain":
        (directory / "events.jsonl").write_bytes(b"{}\n")
    elif change == "extra":
        value["raw_log"] = "must-never-be-accepted"
    elif change == "hardlink":
        os.link(path, prepared.root / "additional-link")
    elif change == "mode":
        path.chmod(0o644)
    if change in {"baseline", "pins", "failed", "counterexample", "extra"}:
        path.write_bytes(remote.encoded(value))
    with pytest.raises(remote.ReleaseError):
        prepared.engine.execute(data)
    assert not (prepared.engine.record / "legacy-stop" / ATTEMPT).exists()
    assert not any(argv[:2] == ["/usr/bin/systemctl", "stop"] for argv, _ in prepared.commands)


@pytest.mark.parametrize(
    "mode",
    [
        "stock-stop",
        "kill",
        "restart",
        "wrong-nonce",
        "timeout",
        "unintended-python",
        "duplicate-launch",
        "duplicate-stop",
        "missing-commit",
        "scope",
        "failure",
        "unrecorded",
        "after-summary",
    ],
)
def test_stop_surface_failures_preserved_no_retry_or_production_mutation(prepared, monkeypatch, mode):
    data, _, _, _ = wire(prepared, monkeypatch, mode)
    with pytest.raises(remote.ReleaseError):
        prepared.engine.execute(data)
    directory = prepared.engine.record / "legacy-stop" / ATTEMPT
    receipt = json.loads((directory / "receipt.json").read_bytes())
    assert receipt["passed"] is False and receipt["failure_code"]
    assert not any(
        argv[:2] in (["/usr/bin/systemctl", "kill"], ["/usr/bin/systemctl", "restart"])
        or argv[:2] == ["/usr/bin/systemctl", "stop"]
        and argv[-1] == "stock-app.service"
        for argv, _ in prepared.commands
    )
    if mode == "duplicate-stop":
        stops = [argv for argv, _ in prepared.commands if argv[:2] == ["/usr/bin/systemctl", "stop"]]
        assert len(stops) == 1
    retry = dict(data, retirement_stop_attempt="retirement-stop-002")
    with pytest.raises(remote.ReleaseError, match="retirement_previous_stop_unreconciled"):
        prepared.engine.execute(retry)
    assert not (directory.parent / "retirement-stop-002").exists()


def test_system_python_must_be_private_approved_actual_digest(prepared, monkeypatch):
    data, _, _, _ = wire(prepared, monkeypatch)
    data["receipts"]["system_python_sha256"] = "a" * 64
    existing.bind(data)
    with pytest.raises(remote.ReleaseError, match="retirement_system_python_binding_required"):
        prepared.engine.execute(data)


def test_fresh_full_baseline_change_prevents_dispatch(prepared, monkeypatch):
    data, baseline, _, _ = wire(prepared, monkeypatch)

    def permit(nonce, label):
        prepared.boundaries.append(label)
        if label == "retirement_stop_dispatch_int-executor":
            baseline["production_pid"] = 333
        return nonce

    prepared.engine.permit = permit
    with pytest.raises(remote.ReleaseError, match="retirement_original_baseline_changed"):
        prepared.engine.execute(data)
    assert not any(argv[:2] == ["/usr/bin/systemctl", "stop"] for argv, _ in prepared.commands)
    receipt = json.loads((prepared.engine.record / "legacy-stop" / ATTEMPT / "receipt.json").read_bytes())
    assert receipt["baseline_verified_after"] is False and receipt["passed"] is False


def test_operator_stop_caught_by_helper_still_cancels_action(prepared, monkeypatch):
    data, _, _, _ = wire(prepared, monkeypatch, "caught-stop")
    original = prepared.engine.boundary

    def stopped(label):
        if label == "retirement_copy_public_libraries":
            raise remote.ReleaseError("operator_stop")
        return original(label)

    monkeypatch.setattr(prepared.engine, "boundary", stopped)
    with pytest.raises(remote.ReleaseError, match="^operator_stop$"):
        prepared.engine.execute(data)
    directory = prepared.engine.record / "legacy-stop" / ATTEMPT
    receipt = json.loads((directory / "receipt.json").read_bytes())
    assert receipt["failure_code"] == "operator_stop" and receipt["passed"] is False
    assert receipt["summary"]["retirement_stop_validation_passed"] is False
    assert json.loads((prepared.engine.record / "status.json").read_bytes())["outcome"] == "cancelled"


def test_successful_experiment_does_not_accept_changed_post_baseline(prepared, monkeypatch):
    data, _, _, _ = wire(prepared, monkeypatch, "post-baseline")
    with pytest.raises(remote.ReleaseError, match="retirement_original_baseline_changed"):
        prepared.engine.execute(data)
    receipt = json.loads((prepared.engine.record / "legacy-stop" / ATTEMPT / "receipt.json").read_bytes())
    assert receipt["summary"]["retirement_stop_validation_passed"] is True
    assert receipt["passed"] is False and receipt["baseline_verified_after"] is False


def test_stop_dispatch_has_actual_ledger_fsync_before_runner(prepared, monkeypatch):
    data, _, _, _ = wire(prepared, monkeypatch)
    synced = {}
    original_fsync, original_run = remote.os.fsync, prepared.engine.run

    def fsync(fd):
        result = original_fsync(fd)
        info = os.fstat(fd)
        synced[(info.st_dev, info.st_ino)] = info.st_size
        return result

    def run(argv, **kwargs):
        if argv[:2] == ["/usr/bin/systemctl", "stop"]:
            path = prepared.engine.record / "legacy-stop" / ATTEMPT / "events.jsonl"
            info = path.stat()
            assert synced[(info.st_dev, info.st_ino)] == info.st_size
            assert json.loads(path.read_bytes().splitlines()[-1])["event"] in {
                "synthetic_stop_committed",
                "synthetic_observer_release_intent",
            }
        return original_run(argv, **kwargs)

    monkeypatch.setattr(remote.os, "fsync", fsync)
    monkeypatch.setattr(prepared.engine, "run", run)
    assert prepared.engine.execute(data)["legacy_stop"]["passed"] is True


@pytest.mark.parametrize("change", ["boolean_uid", "raw_exception", "extra", "zero_pid", "counterexample"])
def test_stop_event_exact_safe_types_only(prepared, change):
    event = "synthetic_stop_actual_isolation"
    value = {
        "case_id": "int-executor",
        "invocation": "d" * 32,
        "pid": 222,
        "uid": 61234,
        "start_ticks": 123,
        "actual_kernel_verified": True,
    }
    if change == "boolean_uid":
        value["uid"] = True
    elif change == "raw_exception":
        value["invocation"] = "arbitrary exception must not persist"
    elif change == "extra":
        value["raw_log"] = "arbitrary content"
    elif change == "zero_pid":
        value["pid"] = 0
    else:
        event = "synthetic_stop_case_result"
        value = {
            **summary_value()["cases"][0],
            "expected_counterexample": True,
            "systemctl_stop_end_to_end_proven": True,
            "stop_dispatches": 1,
            "exit_monotonic_us": 1001,
        }
    with pytest.raises(remote.ReleaseError):
        prepared.engine.retirement_stop_event(event, value, bindings(), probe, validation, stop)


@pytest.mark.parametrize("kind", ["case", "observer", "version", "inventory", "unknown"])
def test_actual_command_capture_only_fixed_public_metadata(monkeypatch, kind):
    argv = {
        "case": [
            "/usr/bin/systemctl",
            "show",
            "--all",
            "--property=" + stop.PROPERTIES,
            "prism-retirement-probe-" + NONCE + "-int-executor.service",
        ],
        "observer": [
            "/usr/bin/systemctl",
            "show",
            "--all",
            "--property=" + stop.OBSERVER_PROPERTIES,
            "prism-retirement-stop-ref-" + NONCE + "-int-executor.service",
        ],
        "version": stop.VERSION_COMMAND,
        "inventory": stop.OBSERVER_INVENTORY,
        "unknown": [
            "/usr/bin/systemctl",
            "show",
            "--all",
            "--property=" + stop.PROPERTIES,
            "stock-app.service",
        ],
    }[kind]
    calls = []

    def run(args, **kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(
            returncode=1 if kind in {"case", "observer", "unknown"} else 0, stdout=b"LoadState=not-found\n"
        )

    monkeypatch.setattr(remote.subprocess, "run", run)
    if kind == "unknown":
        with pytest.raises(remote.ReleaseError, match="command_failed"):
            remote.run_command(argv, timeout=10)
    else:
        assert remote.run_command(argv, timeout=10) == b"LoadState=not-found\n"
    assert calls[0]["stdout"] == remote.subprocess.PIPE


def test_mac_requires_all_three_literal_pins_and_explicit_attempt_proof(tmp_path):
    receipts = tmp_path / "receipts.json"
    manifest = tmp_path / "source.json"
    manifest.write_text('{"files":[]}')
    values = {
        "commit": existing.COMMIT,
        "system_python_sha256": remote.RETIREMENT_SYSTEM_PYTHON_SHA256,
        "expected_source_manifest_sha256": controller.sha(remote.encoded({"files": []})),
    }
    receipts.write_text(json.dumps(values))
    args = types.SimpleNamespace(
        action="validate_legacy_stop",
        commit=existing.COMMIT,
        release_id=existing.RELEASE,
        executor_sha256=controller.sha((controller.ROOT / "remote_release.py").read_bytes()),
        receipts=receipts,
        source_manifest=manifest,
        external_source_manifest=None,
        retirement_probe_sha256=remote.RETIREMENT_HELPERS["retirement_probe.py"],
        retirement_validation_sha256=remote.RETIREMENT_HELPERS["retirement_validation.py"],
        retirement_stop_sha256=None,
        retirement_stop_attempt=ATTEMPT,
        retirement_proof_attempt=PROOF,
    )
    with pytest.raises(controller.ControlError, match="approved_retirement_helpers_required"):
        controller.request_for(args)
    args.retirement_stop_sha256 = remote.RETIREMENT_STOP_HELPERS["retirement_stop_validation.py"]
    args.retirement_proof_attempt = None
    with pytest.raises(controller.ControlError, match="explicit_retirement_stop_and_proof_attempts_required"):
        controller.request_for(args)
    args.retirement_proof_attempt = PROOF
    result, _ = controller.request_for(args)
    assert result["retirement_stop_helpers"] == sources()
    assert result["retirement_stop_attempt"] == ATTEMPT and result["retirement_proof_attempt"] == PROOF


def seed_reviewed_prestart(prepared, data, baseline, *, attempt=None, helper_sha=None):
    engine = prepared.engine
    engine.request = data
    proof = engine.retirement_signal_proof(baseline, bindings(), probe, validation)
    directory = engine.record / "legacy-stop" / (attempt or remote.RETIREMENT_PRESTART_ATTEMPT)
    directory.mkdir(parents=True, mode=0o700)
    identity = {
        "commit": existing.COMMIT,
        "attempt": directory.name,
        "helpers": {
            **remote.RETIREMENT_HELPERS,
            "retirement_stop_validation.py": helper_sha or remote.RETIREMENT_PRESTART_STOP_SHA256,
        },
        "baseline": dict(baseline),
        "signal_proof": proof,
        "system_python_sha256": remote.RETIREMENT_SYSTEM_PYTHON_SHA256,
    }
    engine.write(directory / "identity.json", remote.encoded(identity))
    summary = summary_value()
    summary.update(
        retirement_stop_validation_passed=False,
        systemctl_stop_end_to_end_proven=False,
        synthetic_cleanup_verified=True,
        cases=[],
        error_code="retirement_stop_systemd_properties_rejected",
    )
    chain, lines = "0" * 64, []
    for sequence, (event, details) in enumerate(
        (
            (
                "public_bundles_verified",
                {
                    "bundle_sha256": probe.bundle_identity(),
                    "wrapper_sha256": validation.wrapper_directory().name,
                },
            ),
            ("retirement_stop_validation_result", summary),
        ),
        1,
    ):
        line = (
            remote.encoded(
                {
                    "sequence": sequence,
                    "at": existing.NOW,
                    "previous_sha256": chain,
                    "event": event,
                    "details": details,
                }
            )
            + b"\n"
        )
        lines.append(line)
        chain = remote.digest(line)
    engine.write(directory / "events.jsonl", b"".join(lines))
    engine.write(
        directory / "receipt.json",
        remote.encoded(
            {
                "attempt": directory.name,
                "signal_proof": proof,
                "summary": summary,
                "events": 2,
                "last_event_sha256": chain,
                "production_verified": False,
                "passed": False,
                "baseline_verified_after": True,
                "failure_code": "legacy_stop_validation_failed",
            }
        ),
    )
    return directory


def test_only_reviewed_zero_actuation_failure_gets_new_durable_reconciliation(prepared, monkeypatch):
    data, baseline, _, _ = wire(prepared, monkeypatch)
    previous = seed_reviewed_prestart(prepared, data, baseline)
    before = {path.name: path.read_bytes() for path in previous.iterdir()}
    assert prepared.engine.execute(data)["legacy_stop"]["passed"] is True
    assert {path.name: path.read_bytes() for path in previous.iterdir()} == before
    current = prepared.engine.record / "legacy-stop" / ATTEMPT
    accepted = json.loads((current / "prestart-reconciliation.json").read_bytes())
    assert accepted["old_records_unchanged"] is True and len(accepted["records"]) == 1
    record = accepted["records"][0]
    assert record["attempt"] == remote.RETIREMENT_PRESTART_ATTEMPT
    assert record["no_synthetic_actuation"] is True
    assert record["receipt_sha256"] == remote.digest(before["receipt.json"])
    assert record["events_sha256"] == remote.digest(before["events.jsonl"])
    identity = json.loads((current / "identity.json").read_bytes())
    assert identity["prestart_reconciliations"] == accepted["records"]


@pytest.mark.parametrize(
    "change",
    [
        "start",
        "extra_event",
        "chain",
        "cases",
        "cleanup",
        "error",
        "baseline",
        "proof",
        "helper",
        "after",
        "other_attempt",
    ],
)
def test_any_unknown_or_actuated_failure_stays_blocked(prepared, monkeypatch, change):
    data, baseline, _, _ = wire(prepared, monkeypatch)
    directory = seed_reviewed_prestart(prepared, data, baseline)
    identity = json.loads((directory / "identity.json").read_bytes())
    receipt = json.loads((directory / "receipt.json").read_bytes())
    lines = (directory / "events.jsonl").read_bytes().splitlines(keepends=True)
    if change == "start":
        row = json.loads(lines[0])
        row.update(event="synthetic_start_intent", details={"case_id": "int-executor", "nonce": NONCE})
        lines[0] = remote.encoded(row) + b"\n"
    elif change == "extra_event":
        lines.insert(1, lines[0])
    elif change == "chain":
        receipt["last_event_sha256"] = "f" * 64
    elif change == "cases":
        receipt["summary"]["cases"] = [summary_value()["cases"][0]]
    elif change == "cleanup":
        receipt["summary"]["synthetic_cleanup_verified"] = False
    elif change == "error":
        receipt["summary"]["error_code"] = "retirement_stop_uncertain_dispatch"
    elif change == "baseline":
        identity["baseline"]["production_pid"] = 999
    elif change == "proof":
        receipt["signal_proof"]["receipt_sha256"] = "f" * 64
    elif change == "helper":
        identity["helpers"]["retirement_stop_validation.py"] = remote.RETIREMENT_STOP_HELPERS[
            "retirement_stop_validation.py"
        ]
    elif change == "after":
        receipt["baseline_verified_after"] = False
    else:
        other = directory.with_name("unapproved-prestart-attempt")
        directory.rename(other)
        directory = other
    (directory / "identity.json").write_bytes(remote.encoded(identity))
    (directory / "receipt.json").write_bytes(remote.encoded(receipt))
    (directory / "events.jsonl").write_bytes(b"".join(lines))
    with pytest.raises(remote.ReleaseError, match="^retirement_previous_stop_unreconciled$"):
        prepared.engine.execute(data)
    assert not (directory.parent / ATTEMPT).exists()
    assert not any(argv[:2] == ["/usr/bin/systemctl", "stop"] for argv, _ in prepared.commands)


def test_legacy_environment_is_fully_scanned_before_and_after_not_during_fixed_work_gate(
    prepared, monkeypatch
):
    data, baseline, _, _ = wire(prepared, monkeypatch)
    calls = []

    def full():
        calls.append("full_environment_source_runtime")
        return dict(baseline)

    monkeypatch.setattr(prepared.engine, "retirement_baseline", full)
    assert prepared.engine.execute(data)["legacy_stop"]["passed"] is True
    assert calls == ["full_environment_source_runtime", "full_environment_source_runtime"]


@pytest.mark.parametrize("drift", ["pid", "runtime", "source"])
def test_active_gate_still_requires_fresh_runtime_and_complete_production_source(
    prepared, monkeypatch, drift
):
    data, baseline, _, _ = wire(prepared, monkeypatch)
    original = prepared.engine.live_original_state
    phase = {"changed": False}

    def permit(nonce, label):
        if label == "retirement_stop_dispatch_int-executor":
            phase["changed"] = True
        return nonce

    def current():
        value = original()
        if phase["changed"]:
            if drift == "source":
                raise remote.ReleaseError("live_original_source_changed")
            value["production_pid" if drift == "pid" else "runtime_fingerprint"] = (
                999 if drift == "pid" else "f" * 63 + "0"
            )
        return value

    prepared.engine.permit = permit
    monkeypatch.setattr(prepared.engine, "live_original_state", current)
    with pytest.raises(remote.ReleaseError, match="retirement_original_baseline_changed"):
        prepared.engine.execute(data)
    assert not any(argv[:2] == ["/usr/bin/systemctl", "stop"] for argv, _ in prepared.commands)


@pytest.mark.parametrize(
    "kind",
    ["own-exec", "own-post", "new-runtime", "production", "other-property", "other-method", "extra-argument"],
)
def test_busctl_capture_is_exact_finite_and_returns_actual_stdout(monkeypatch, kind):
    unit = "prism-retirement-probe-" + NONCE + "-int-executor.service"
    argv = stop.exec_array_commands(unit)[0]
    expected = kind in {"own-exec", "own-post", "new-runtime"}
    if kind == "own-post":
        argv[-1] = "ExecStopPost"
    if kind == "new-runtime":
        argv[5] = (
            "/org/freedesktop/systemd1/unit/prism_2dnew_2druntime_2dstop_2d"
            + NONCE
            + "_2dcandidate_2eservice"
        )
    if kind == "production":
        argv[5] = "/org/freedesktop/systemd1/unit/stock_2dapp_2eservice"
    if kind == "other-property":
        argv[-1] = "Environment"
    if kind == "other-method":
        argv[3] = "call"
    if kind == "extra-argument":
        argv.append("extra")
    calls = []
    raw = remote.encoded(stop.EMPTY_EXEC_ARRAY) + b"\n"

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return types.SimpleNamespace(
            returncode=0, stdout=raw if kwargs["stdout"] == remote.subprocess.PIPE else None
        )

    monkeypatch.setattr(remote.subprocess, "run", run)
    assert remote.run_command(argv, timeout=10) == (raw if expected else b"")
    assert calls[0][1]["stdout"] == (remote.subprocess.PIPE if expected else remote.subprocess.DEVNULL)


def test_new_stop_requires_durable_typed_arrays_before_any_stop_intent(prepared, monkeypatch):
    data, _, _, _ = wire(prepared, monkeypatch, mode="missing-exec-proof")
    with pytest.raises(remote.ReleaseError, match="retirement_stop_durable_exec_arrays_required"):
        prepared.engine.execute(data)
    assert not any(argv[:2] == ["/usr/bin/systemctl", "stop"] for argv, _ in prepared.commands)


def approve_synthetic_second_prestart(prepared, data, baseline, monkeypatch):
    attempt = "prism-20261022-stop-e2e-002"
    approved = copy.deepcopy(remote.RETIREMENT_PRESTART_REVIEWED)
    first = prepared.engine.record / "legacy-stop" / remote.RETIREMENT_PRESTART_ATTEMPT
    if not first.exists():
        seed_reviewed_prestart(prepared, data, baseline)
    path = seed_reviewed_prestart(
        prepared, data, baseline, attempt=attempt, helper_sha=approved[attempt]["helper_sha256"]
    )
    # Reproduce the reviewed 002 writer's extra identity field using independent
    # hashes of the first private record, not the reconciliation under test.
    first_receipt = json.loads((first / "receipt.json").read_bytes())
    prior = {
        "attempt": first.name,
        "identity_sha256": remote.digest((first / "identity.json").read_bytes()),
        "receipt_sha256": remote.digest((first / "receipt.json").read_bytes()),
        "events_sha256": remote.digest((first / "events.jsonl").read_bytes()),
        "last_event_sha256": first_receipt["last_event_sha256"],
        "no_synthetic_actuation": True,
        "baseline_verified_after": True,
        "reason": "retirement_stop_systemd_properties_rejected",
    }
    identity = json.loads((path / "identity.json").read_bytes())
    identity["prestart_reconciliations"] = [prior]
    (path / "identity.json").write_bytes(remote.encoded(identity))
    for name in ("identity", "events", "receipt"):
        approved[attempt][name + "_sha256"] = remote.digest(
            (path / (name + (".jsonl" if name == "events" else ".json"))).read_bytes()
        )
    monkeypatch.setattr(remote, "RETIREMENT_PRESTART_REVIEWED", approved)
    return path


def test_two_exact_reviewed_no_actuation_failures_are_reconciled_read_only(prepared, monkeypatch):
    data, baseline, _, _ = wire(prepared, monkeypatch)
    first = seed_reviewed_prestart(prepared, data, baseline)
    second = approve_synthetic_second_prestart(prepared, data, baseline, monkeypatch)
    original = {
        str(path): {file.name: file.read_bytes() for file in path.iterdir()} for path in (first, second)
    }
    assert prepared.engine.execute(data)["legacy_stop"]["passed"] is True
    for path in (first, second):
        assert {file.name: file.read_bytes() for file in path.iterdir()} == original[str(path)]
    accepted = json.loads(
        (prepared.engine.record / "legacy-stop" / ATTEMPT / "prestart-reconciliation.json").read_bytes()
    )
    assert {row["attempt"] for row in accepted["records"]} == {first.name, second.name}
    assert all(row["no_synthetic_actuation"] is True for row in accepted["records"])


@pytest.mark.parametrize(
    "change",
    [
        "missing-first",
        "first-chain",
        "first-start",
        "missing-field",
        "empty-list",
        "extra-row",
        "row-attempt",
        "row-identity",
        "row-receipt",
        "row-events",
        "row-tail",
        "row-cleanup",
        "row-actuation",
        "row-reason",
        "extra-identity",
    ],
)
def test_second_reviewed_identity_requires_complete_independent_first_reconciliation(
    prepared, monkeypatch, change
):
    data, baseline, _, _ = wire(prepared, monkeypatch)
    second = approve_synthetic_second_prestart(prepared, data, baseline, monkeypatch)
    first = second.parent / remote.RETIREMENT_PRESTART_ATTEMPT
    identity = json.loads((second / "identity.json").read_bytes())
    if change == "missing-first":
        # Absence must fail even though the 002 row and its pinned bytes survive.
        for file in first.iterdir():
            file.unlink()
        first.rmdir()
    elif change in {"first-chain", "first-start"}:
        rows = (first / "events.jsonl").read_bytes().splitlines(keepends=True)
        row = json.loads(rows[0])
        if change == "first-chain":
            row["previous_sha256"] = "f" * 64
        else:
            row.update(event="synthetic_start_intent", details={"case_id": "int-executor", "nonce": NONCE})
        rows[0] = remote.encoded(row) + b"\n"
        (first / "events.jsonl").write_bytes(b"".join(rows))
        identity["prestart_reconciliations"][0]["events_sha256"] = remote.digest(b"".join(rows))
    elif change == "missing-field":
        del identity["prestart_reconciliations"]
    elif change == "empty-list":
        identity["prestart_reconciliations"] = []
    elif change == "extra-row":
        identity["prestart_reconciliations"] *= 2
    elif change == "extra-identity":
        identity["unreviewed"] = True
    else:
        key, value = {
            "row-attempt": ("attempt", second.name),
            "row-identity": ("identity_sha256", "f" * 64),
            "row-receipt": ("receipt_sha256", "f" * 64),
            "row-events": ("events_sha256", "f" * 64),
            "row-tail": ("last_event_sha256", "f" * 64),
            "row-cleanup": ("baseline_verified_after", False),
            "row-actuation": ("no_synthetic_actuation", False),
            "row-reason": ("reason", "retirement_stop_uncertain_dispatch"),
        }[change]
        identity["prestart_reconciliations"][0][key] = value
    (second / "identity.json").write_bytes(remote.encoded(identity))
    # Re-pin synthetic bytes so rejection exercises semantic/private-chain
    # validation rather than merely failing the immutable reviewed-byte digest.
    approved = copy.deepcopy(remote.RETIREMENT_PRESTART_REVIEWED)
    approved[second.name]["identity_sha256"] = remote.digest((second / "identity.json").read_bytes())
    monkeypatch.setattr(remote, "RETIREMENT_PRESTART_REVIEWED", approved)
    before = {str(p): p.read_bytes() for d in (first, second) if d.exists() for p in d.iterdir()}
    with pytest.raises(remote.ReleaseError, match="^retirement_previous_stop_unreconciled$"):
        prepared.engine.execute(data)
    assert {str(p): p.read_bytes() for d in (first, second) if d.exists() for p in d.iterdir()} == before
    assert not (second.parent / ATTEMPT).exists()
    assert not any(argv[:2] == ["/usr/bin/systemctl", "stop"] for argv, _ in prepared.commands)


def test_reconciliation_is_independent_of_filesystem_directory_order(prepared, monkeypatch):
    data, baseline, _, _ = wire(prepared, monkeypatch)
    second = approve_synthetic_second_prestart(prepared, data, baseline, monkeypatch)
    original = type(second).iterdir

    def reversed_entries(path):
        rows = list(original(path))
        return iter(sorted(rows, key=lambda item: item.name, reverse=True))

    monkeypatch.setattr(type(second), "iterdir", reversed_entries)
    assert prepared.engine.execute(data)["legacy_stop"]["passed"] is True
    result = json.loads((second.parent / ATTEMPT / "prestart-reconciliation.json").read_bytes())
    assert [row["attempt"] for row in result["records"]] == [remote.RETIREMENT_PRESTART_ATTEMPT, second.name]


@pytest.mark.parametrize("file", ["identity.json", "receipt.json", "events.jsonl"])
def test_second_prestart_requires_exact_pinned_private_bytes_even_if_schema_valid(
    prepared, monkeypatch, file
):
    data, baseline, _, _ = wire(prepared, monkeypatch)
    path = approve_synthetic_second_prestart(prepared, data, baseline, monkeypatch)
    # Harmless whitespace is still a change to the reviewed immutable bytes.
    with (path / file).open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(remote.ReleaseError, match="retirement_previous_stop_unreconciled"):
        prepared.engine.execute(data)
    assert not (path.parent / ATTEMPT).exists()
    assert not any(argv[:2] == ["/usr/bin/systemctl", "stop"] for argv, _ in prepared.commands)


def test_arbitrary_same_name_second_failure_is_not_the_reviewed_actual_receipt(prepared, monkeypatch):
    data, baseline, _, _ = wire(prepared, monkeypatch)
    attempt = "prism-20261022-stop-e2e-002"
    seed_reviewed_prestart(
        prepared,
        data,
        baseline,
        attempt=attempt,
        helper_sha=remote.RETIREMENT_PRESTART_REVIEWED[attempt]["helper_sha256"],
    )
    with pytest.raises(remote.ReleaseError, match="retirement_previous_stop_unreconciled"):
        prepared.engine.execute(data)
    assert not any(argv[:2] == ["/usr/bin/systemctl", "stop"] for argv, _ in prepared.commands)


@pytest.mark.parametrize(
    "unit",
    [
        "prism-retirement-probe-" + NONCE + "-int-executor.service",
        "prism-retirement-stop-ref-" + NONCE + "-int-post.service",
        "stock-app.service",
    ],
)
def test_prestart_loadstate_notfound_capture_never_synthesizes_missing_fields(monkeypatch, unit):
    calls = []

    def run(argv, **kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(returncode=4, stdout=b"LoadState=not-found\n")

    monkeypatch.setattr(remote.subprocess, "run", run)
    argv = ["/usr/bin/systemctl", "show", "--property=LoadState", unit]
    if unit == "stock-app.service":
        with pytest.raises(remote.ReleaseError, match="command_failed"):
            remote.run_command(argv, timeout=10)
    else:
        assert remote.run_command(argv, timeout=10) == b"LoadState=not-found\n"
    assert calls[0]["stdout"] == remote.subprocess.PIPE


def seed_retained_start(prepared, data, baseline, monkeypatch):
    second = approve_synthetic_second_prestart(prepared, data, baseline, monkeypatch)
    first = second.parent / remote.RETIREMENT_PRESTART_ATTEMPT
    prior = []
    for path in (first, second):
        receipt = json.loads((path / "receipt.json").read_bytes())
        prior.append(
            {
                "attempt": path.name,
                "identity_sha256": remote.digest((path / "identity.json").read_bytes()),
                "receipt_sha256": remote.digest((path / "receipt.json").read_bytes()),
                "events_sha256": remote.digest((path / "events.jsonl").read_bytes()),
                "last_event_sha256": receipt["last_event_sha256"],
                "no_synthetic_actuation": True,
                "baseline_verified_after": True,
                "reason": "retirement_stop_systemd_properties_rejected",
            }
        )
    approved = copy.deepcopy(remote.RETIREMENT_RETAINED_START)
    path = seed_reviewed_prestart(
        prepared, data, baseline, attempt=approved["attempt"], helper_sha=approved["helper_sha256"]
    )
    identity = json.loads((path / "identity.json").read_bytes())
    identity["prestart_reconciliations"] = prior
    (path / "identity.json").write_bytes(remote.encoded(identity))
    receipt = json.loads((path / "receipt.json").read_bytes())
    receipt["summary"].update(
        error_code="retirement_systemd_number_rejected", synthetic_cleanup_verified=False
    )
    first_event = json.loads((path / "events.jsonl").read_bytes().splitlines()[0])
    rows = [
        ("public_bundles_verified", first_event["details"]),
        ("synthetic_start_intent", {"case_id": approved["case_id"], "nonce": approved["nonce"]}),
        ("retirement_stop_validation_result", receipt["summary"]),
    ]
    lines, tail = [], "0" * 64
    for sequence, (event, details) in enumerate(rows, 1):
        line = (
            remote.encoded(
                {
                    "sequence": sequence,
                    "at": existing.NOW,
                    "previous_sha256": tail,
                    "event": event,
                    "details": details,
                }
            )
            + b"\n"
        )
        lines.append(line)
        tail = remote.digest(line)
    (path / "events.jsonl").write_bytes(b"".join(lines))
    receipt.update(events=3, last_event_sha256=tail)
    (path / "receipt.json").write_bytes(remote.encoded(receipt))
    for name in ("identity", "receipt", "events"):
        approved[name + "_sha256"] = remote.digest(
            (path / (name + (".jsonl" if name == "events" else ".json"))).read_bytes()
        )
    plan = probe.descriptor(bindings(), nonce=approved["nonce"])
    unit = probe._case(plan, approved["case_id"])["unit"]
    launch = validation.launch_command(plan, approved["case_id"])
    argv = launch[launch.index("--") + 1 :]
    approved["argv_sha256"] = remote.digest(remote.encoded(argv))
    monkeypatch.setattr(remote, "RETIREMENT_RETAINED_START", approved)
    state = {
        "LoadState": "loaded",
        "ActiveState": "active",
        "SubState": "exited",
        "MainPID": "0",
        "ExecMainPID": str(approved["exec_main_pid"]),
        "ExecMainCode": "1",
        "ExecMainStatus": "0",
        "Result": "success",
        "InvocationID": approved["invocation_id"],
        "ControlGroup": "",
        "Job": "",
        "Transient": "yes",
        "ExecMainExitTimestampMonotonic": "4",
    }
    facts = {
        "state": state,
        "job": {"type": "(uo)", "data": [0, "/"]},
        "start": {
            "type": "a(sasbttttuii)",
            "data": [[argv[0], argv, False, 1, 2, 3, 4, approved["exec_main_pid"], 1, 0]],
        },
        "observer": "not-found",
        "show_reads": 0,
        "drift": False,
    }
    cgroot = prepared.engine.root / "sys/fs/cgroup"
    cgroot.mkdir(parents=True)
    (cgroot / "cgroup.controllers").write_text("cpu memory\n")
    original = prepared.engine.run

    def run(command, *, timeout):
        if command == [
            "/usr/bin/systemctl",
            "show",
            "--all",
            "--property=" + remote.RETIREMENT_RETAINED_PROPERTIES,
            unit,
        ]:
            facts["show_reads"] += 1
            observed = dict(facts["state"])
            if facts["drift"] and facts["show_reads"] % 2 == 0:
                observed["InvocationID"] = "f" * 32
            return "".join(k + "=" + v + "\n" for k, v in observed.items()).encode()
        if command == stop.job_command(unit):
            return remote.encoded(facts["job"])
        start = stop.exec_array_commands(unit)[0]
        start[-1] = "ExecStart"
        if command == start:
            return remote.encoded(facts["start"])
        if command == [
            "/usr/bin/systemctl",
            "show",
            "--property=LoadState",
            stop.observer_unit(plan, approved["case_id"]),
        ]:
            return ("LoadState=" + facts["observer"] + "\n").encode()
        return original(command, timeout=timeout)

    monkeypatch.setattr(prepared.engine, "run", run)
    return path, plan, facts


def test_exact_started_failure_is_only_readonly_reconciled_and_retained(prepared, monkeypatch):
    data, baseline, _, _ = wire(prepared, monkeypatch)
    path, plan, facts = seed_retained_start(prepared, data, baseline, monkeypatch)
    before = {file.name: file.read_bytes() for file in path.iterdir()}
    assert prepared.engine.execute(data)["legacy_stop"]["passed"] is True
    assert {file.name: file.read_bytes() for file in path.iterdir()} == before
    record = json.loads((path.parent / ATTEMPT / "retained-start-reconciliation.json").read_bytes())
    assert record["previous_passed"] is record["previous_cleanup_verified"] is False
    assert record["failed_record_retained"] is record["no_stop_intent_in_complete_chain"] is True
    assert record["observation"]["typed_job"] == {"type": "(uo)", "data": [0, "/"]}
    assert facts["show_reads"] > 2
    retained = probe._case(plan, "int-executor")["unit"]
    assert not any(
        argv[:2]
        in (
            ["/usr/bin/systemctl", "stop"],
            ["/usr/bin/systemctl", "kill"],
            ["/usr/bin/systemctl", "reset-failed"],
        )
        and retained in argv
        for argv, _ in prepared.commands
    )


@pytest.mark.parametrize(
    "change",
    [
        "job",
        "job-path",
        "show-conflict",
        "invocation",
        "pid",
        "main",
        "exit",
        "state",
        "case-cgroup",
        "observer-cgroup",
        "observer",
        "argv",
        "argv-pin",
        "exec-shape",
        "show-drift",
        "passed",
        "stop-intent",
        "chain",
        "prestart-reference",
    ],
)
def test_started_failure_reconciliation_blocks_every_unknown_or_active_state(prepared, monkeypatch, change):
    data, baseline, _, _ = wire(prepared, monkeypatch)
    path, plan, facts = seed_retained_start(prepared, data, baseline, monkeypatch)
    if change == "job":
        facts["job"] = {"type": "(uo)", "data": [7, "/org/freedesktop/systemd1/job/7"]}
    elif change == "job-path":
        facts["job"]["data"][1] = "/org/freedesktop/systemd1/job/0"
    elif change == "show-conflict":
        facts["state"]["Job"] = "7"
    elif change == "invocation":
        facts["state"]["InvocationID"] = "f" * 32
    elif change == "pid":
        facts["state"]["ExecMainPID"] = "123"
    elif change == "main":
        facts["state"]["MainPID"] = "123"
    elif change == "exit":
        facts["state"]["ExecMainStatus"] = "1"
    elif change == "state":
        facts["state"]["SubState"] = "running"
    elif change in {"case-cgroup", "observer-cgroup"}:
        unit = (
            probe._case(plan, "int-executor")["unit"]
            if change == "case-cgroup"
            else stop.observer_unit(plan, "int-executor")
        )
        (prepared.engine.root / "sys/fs/cgroup/system.slice" / unit).mkdir(parents=True)
    elif change == "observer":
        facts["observer"] = "loaded"
    elif change == "argv":
        facts["start"]["data"][0][1][-1] = "changed-public-fixture"
    elif change == "argv-pin":
        monkeypatch.setitem(remote.RETIREMENT_RETAINED_START, "argv_sha256", "f" * 64)
    elif change == "exec-shape":
        facts["start"]["type"] = "as"
    elif change == "show-drift":
        facts["drift"] = True
    else:
        if change == "prestart-reference":
            value = json.loads((path / "identity.json").read_bytes())
            value["prestart_reconciliations"][0]["receipt_sha256"] = "f" * 64
            (path / "identity.json").write_bytes(remote.encoded(value))
        elif change == "passed":
            value = json.loads((path / "receipt.json").read_bytes())
            value["passed"] = True
            value["summary"]["synthetic_cleanup_verified"] = True
            (path / "receipt.json").write_bytes(remote.encoded(value))
        else:
            lines = (path / "events.jsonl").read_bytes().splitlines(keepends=True)
            row = json.loads(lines[1])
            row["event" if change == "stop-intent" else "previous_sha256"] = (
                "synthetic_stop_intent" if change == "stop-intent" else "f" * 64
            )
            lines[1] = remote.encoded(row) + b"\n"
            (path / "events.jsonl").write_bytes(b"".join(lines))
        # Exercise semantics too, beyond the fixed immutable digest check.
        for name in ("identity", "receipt", "events"):
            monkeypatch.setitem(
                remote.RETIREMENT_RETAINED_START,
                name + "_sha256",
                remote.digest((path / (name + (".jsonl" if name == "events" else ".json"))).read_bytes()),
            )
    before = {file.name: file.read_bytes() for file in path.iterdir()}
    with pytest.raises(remote.ReleaseError, match="retirement_previous_stop_unreconciled"):
        prepared.engine.execute(data)
    assert {file.name: file.read_bytes() for file in path.iterdir()} == before
    assert not (path.parent / ATTEMPT).exists()
    assert not any(argv[:2] == ["/usr/bin/systemctl", "stop"] for argv, _ in prepared.commands)


@pytest.mark.parametrize(
    "kind",
    [
        "old-main",
        "old-observer",
        "new-main",
        "new-observer",
        "production",
        "other-interface",
        "other-property",
        "extra",
    ],
)
def test_job_stdout_capture_is_finite_unit_property_and_real_pipe(monkeypatch, kind):
    unit = "prism-retirement-probe-" + NONCE + "-int-executor.service"
    if kind == "old-observer":
        unit = "prism-retirement-stop-ref-" + NONCE + "-int-post.service"
    argv = stop.job_command(unit)
    if kind.startswith("new"):
        argv[5] = (
            "/org/freedesktop/systemd1/unit/prism_2dnew_2druntime_2dstop_2d"
            + NONCE
            + "_2drecovery"
            + ("_2dref" if kind == "new-observer" else "")
            + "_2eservice"
        )
    if kind == "production":
        argv[5] = "/org/freedesktop/systemd1/unit/stock_2dapp_2eservice"
    if kind == "other-interface":
        argv[6] = "org.freedesktop.systemd1.Service"
    if kind == "other-property":
        argv[7] = "Environment"
    if kind == "extra":
        argv.append("extra")
    calls = []

    def run(command, **kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(
            returncode=0, stdout=b"typed" if kwargs["stdout"] == remote.subprocess.PIPE else None
        )

    monkeypatch.setattr(remote.subprocess, "run", run)
    expected = kind in {"old-main", "old-observer", "new-main", "new-observer"}
    assert remote.run_command(argv) == (b"typed" if expected else b"")
    assert calls[0]["stdout"] == (remote.subprocess.PIPE if expected else remote.subprocess.DEVNULL)


@pytest.mark.parametrize("change", ["identity.json", "receipt.json", "events.jsonl"])
def test_retained_started_failure_requires_exact_reviewed_immutable_bytes(prepared, monkeypatch, change):
    data, baseline, _, _ = wire(prepared, monkeypatch)
    path, _, _ = seed_retained_start(prepared, data, baseline, monkeypatch)
    with (path / change).open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(remote.ReleaseError, match="retirement_previous_stop_unreconciled"):
        prepared.engine.execute(data)
    assert not (path.parent / ATTEMPT).exists()
    assert not any(argv[:2] == ["/usr/bin/systemctl", "stop"] for argv, _ in prepared.commands)


@pytest.mark.parametrize("change", ["exact", "other-nonce", "production", "other-property"])
def test_retained_execstart_capture_is_only_one_fixed_readonly_public_command(monkeypatch, change):
    value = probe.descriptor(bindings(), nonce=remote.RETIREMENT_RETAINED_START["nonce"])
    unit = probe._case(value, "int-executor")["unit"]
    argv = stop.exec_array_commands(unit)[0]
    argv[-1] = "ExecStart"
    if change == "other-nonce":
        argv[5] = argv[5].replace(remote.RETIREMENT_RETAINED_START["nonce"], NONCE)
    if change == "production":
        argv[5] = "/org/freedesktop/systemd1/unit/stock_2dapp_2eservice"
    if change == "other-property":
        argv[7] = "Environment"
    calls = []

    def run(command, **kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(
            returncode=0, stdout=b"public" if kwargs["stdout"] == remote.subprocess.PIPE else None
        )

    monkeypatch.setattr(remote.subprocess, "run", run)
    assert remote.run_command(argv) == (b"public" if change == "exact" else b"")
    assert calls[0]["stdout"] == (remote.subprocess.PIPE if change == "exact" else remote.subprocess.DEVNULL)


def test_retained_invocation_is_rechecked_at_each_new_actuation_boundary(prepared, monkeypatch):
    data, baseline, _, _ = wire(prepared, monkeypatch)
    path, _, facts = seed_retained_start(prepared, data, baseline, monkeypatch)

    def permit(nonce, label):
        if label == "retirement_stop_dispatch_int-executor":
            facts["state"]["InvocationID"] = "f" * 32
        return nonce

    prepared.engine.permit = permit
    with pytest.raises(remote.ReleaseError, match="retirement_retained_start_unreconciled"):
        prepared.engine.execute(data)
    assert not any(argv[:2] == ["/usr/bin/systemctl", "stop"] for argv, _ in prepared.commands)
    assert json.loads((path / "receipt.json").read_bytes())["passed"] is False


@pytest.mark.parametrize("old_parent", [False, True])
def test_known_reviewed_release_cannot_lose_started_failure_and_resume_from_empty_inventory(
    prepared, monkeypatch, old_parent
):
    data, baseline, _, _ = wire(prepared, monkeypatch)
    if old_parent:
        approve_synthetic_second_prestart(prepared, data, baseline, monkeypatch)
    approved = copy.deepcopy(remote.RETIREMENT_RETAINED_START)
    approved.update(commit=existing.COMMIT, release_id=existing.RELEASE)
    monkeypatch.setattr(remote, "RETIREMENT_RETAINED_START", approved)
    with pytest.raises(remote.ReleaseError, match="retirement_previous_stop_unreconciled"):
        prepared.engine.execute(data)
    assert not (prepared.engine.record / "legacy-stop" / ATTEMPT).exists()
    assert not any(argv[:2] == ["/usr/bin/systemctl", "stop"] for argv, _ in prepared.commands)
