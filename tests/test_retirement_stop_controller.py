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
                ["/usr/bin/systemctl", "show", "--property=" + stop.PROPERTIES, descriptor["unit"]],
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
            "--property=" + stop.PROPERTIES,
            "prism-retirement-probe-" + NONCE + "-int-executor.service",
        ],
        "observer": [
            "/usr/bin/systemctl",
            "show",
            "--property=" + stop.OBSERVER_PROPERTIES,
            "prism-retirement-stop-ref-" + NONCE + "-int-executor.service",
        ],
        "version": stop.VERSION_COMMAND,
        "inventory": stop.OBSERVER_INVENTORY,
        "unknown": ["/usr/bin/systemctl", "show", "--property=" + stop.PROPERTIES, "stock-app.service"],
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
