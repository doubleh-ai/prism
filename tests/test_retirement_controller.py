"""Offline controller plumbing; fake observations never certify retirement."""

import base64
import json
import os
import sys
import types
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pytest
import test_release_controller as existing

from scripts import retirement_probe as probe
from scripts import retirement_validation as validation

remote, controller = existing.remote, existing.controller


@pytest.fixture
def prepared():
    fixture = existing.Fixture()
    fixture.setUp()
    fixture.prepared()
    try:
        yield fixture
    finally:
        fixture.tearDown()


def sources():
    return {
        name: {
            "sha256": sha,
            "content": base64.b64encode((existing.ROOT / "scripts" / name).read_bytes()).decode(),
        }
        for name, sha in remote.RETIREMENT_HELPERS.items()
    }


def fake_summary(bindings):
    plan = probe.descriptor(bindings, nonce="c" * 32)
    cases = [
        {
            "case_id": case["case_id"],
            "passed": True,
            "clean_exit_zero": case["case_id"] != "term-control",
            "executor_results_preserved": case["expectation"] == "executor_preserved",
            "expected_counterexample": case["expectation"] != "executor_preserved",
            "production_quiescence_proven": False,
            "task_census_still_required": True,
        }
        for case in plan["cases"]
    ]
    result = probe.summarize(plan, cases)
    result.update(
        actual_linux_collection=True,
        synthetic_cleanup_verified=True,
        validates_single_main_signal_only=True,
        systemctl_stop_end_to_end_proven=False,
        wrapper_sha256=validation.wrapper_directory().name,
        probe_source_sha256=validation.PROBE_SOURCE_SHA256,
        python_minor="3.12",
        pinned_versions=dict(probe.VERSIONS),
        uvicorn_server_sha256=probe.UVICORN_SHA256,
        uvloop_binary_sha256=probe.UVLOOP_SHA256,
    )
    return result


def wire(prepared, monkeypatch, mode=None):
    engine = prepared.engine
    data = dict(prepared.data, action="validate_legacy_retirement", retirement_helpers=sources())
    baseline = {
        "production_pid": 111,
        "runtime_fingerprint": "f" * 64,
        "source_manifest_sha256": "e" * 64,
        "base_environment_sha256": remote.RETIREMENT_BASE_ENV_SHA256,
    }
    monkeypatch.setattr(engine, "retirement_baseline", lambda: dict(baseline))
    monkeypatch.setattr(engine, "runtime", lambda: {"fingerprint": baseline["runtime_fingerprint"]})
    seen = []

    def execute(bindings, *, boundary, run, record):
        result = fake_summary(bindings)
        boundary("retirement_copy_public_libraries")
        record(
            "public_bundles_verified",
            {"bundle_sha256": result["bundle_sha256"], "wrapper_sha256": result["wrapper_sha256"]},
        )
        for case in result["cases"]:
            boundary("retirement_start_" + case["case_id"])
            record("synthetic_start_intent", {"case_id": case["case_id"], "nonce": "c" * 32})
            events = (
                engine.record
                / "legacy-retirement"
                / data.get("retirement_attempt", existing.RELEASE)
                / "events.jsonl"
            )
            assert json.loads(events.read_bytes().splitlines()[-1])["event"] == "synthetic_start_intent"
            seen.append(case["case_id"])
            boundary("retirement_signal_" + case["case_id"])
            if mode == "unsafe_event":
                record(
                    "synthetic_signal_intent",
                    {
                        "case_id": case["case_id"],
                        "invocation_id": "c" * 32,
                        "signal": "SIGKILL",
                        "signal_at_monotonic_us": 1,
                    },
                )
            record("synthetic_case_result", case)
        if mode == "scope":
            result["production_quiescence_proven"] = True
        if mode == "failure":
            result["retirement_validation_passed"] = False
            result["synthetic_cleanup_verified"] = False
        if mode == "timeout":
            run(["/usr/bin/systemctl", "show"], timeout=1000)
        if mode != "unrecorded":
            record("retirement_validation_result", result)
        if mode == "after_summary":
            record("bundle_copy_intent", {"bundle_sha256": "a" * 64})
        return result

    @contextmanager
    def modules(directory):
        yield (
            probe,
            types.SimpleNamespace(run_validation=execute, wrapper_directory=validation.wrapper_directory),
        )

    monkeypatch.setattr(engine, "retirement_modules", modules)
    return data, seen, baseline


def test_explicit_fixed_helpers_only_and_no_candidate_trust(prepared):
    prepared.engine.request = dict(prepared.data, retirement_helpers=sources())
    content = prepared.engine.retirement_sources()
    assert set(content) == set(remote.RETIREMENT_HELPERS) == set(controller.RETIREMENT_HELPERS)
    assert all(remote.digest(value) == remote.RETIREMENT_HELPERS[name] for name, value in content.items())


@pytest.mark.parametrize("case", ["bytes", "digest", "extra", "missing"])
def test_unauthorized_helper_rejected_before_import(prepared, case):
    data = sources()
    if case == "bytes":
        data["retirement_probe.py"]["content"] = base64.b64encode(
            b"raise AssertionError('never execute')"
        ).decode()
    elif case == "digest":
        data["retirement_probe.py"]["sha256"] = "f" * 64
    elif case == "extra":
        data["candidate.py"] = data["retirement_probe.py"]
    else:
        data.pop("retirement_validation.py")
    prepared.engine.request = dict(prepared.data, retirement_helpers=data)
    with pytest.raises(remote.ReleaseError, match="approved_retirement_helpers_required"):
        prepared.engine.retirement_sources()


def test_sealed_helpers_compile_only_public_bytes_with_real_source_paths(prepared):
    engine = prepared.engine
    engine.request = dict(prepared.data, retirement_helpers=sources())
    directory = engine.install_retirement_helpers(engine.retirement_sources())
    assert directory.name == remote.digest(remote.encoded(remote.RETIREMENT_HELPERS))
    assert directory.stat().st_mode & 0o777 == 0o555
    assert (directory / "scripts").stat().st_mode & 0o777 == 0o555
    assert all(path.stat().st_mode & 0o777 == 0o444 for path in (directory / "scripts").iterdir())
    original_path = list(sys.path)
    with mock.patch.dict(sys.modules):
        for name in ("scripts", "scripts.retirement_probe", "scripts.retirement_validation"):
            sys.modules.pop(name, None)
        with engine.retirement_modules(directory) as (actual_probe, actual_validation):
            assert Path(actual_probe.__file__) == directory / "scripts/retirement_probe.py"
            assert Path(actual_validation.__file__) == directory / "scripts/retirement_validation.py"
            assert actual_validation.probe is actual_probe
            assert actual_probe.bundle_identity() == probe.bundle_identity()
            assert sys.path == original_path
        assert not any(
            name in sys.modules
            for name in ("scripts", "scripts.retirement_probe", "scripts.retirement_validation")
        )
    assert sys.path == original_path


@pytest.mark.parametrize("case", ["content", "hardlink", "mode", "extra", "namespace"])
def test_cache_tamper_or_ambient_namespace_fails_closed(prepared, case):
    engine = prepared.engine
    engine.request = dict(prepared.data, retirement_helpers=sources())
    directory = engine.install_retirement_helpers(engine.retirement_sources())
    path = directory / "scripts/retirement_probe.py"
    if case == "content":
        path.chmod(0o644)
        path.write_bytes(b"# changed\n")
        path.chmod(0o444)
    elif case == "hardlink":
        os.link(path, prepared.root / "additional-link")
    elif case == "mode":
        path.chmod(0o644)
    elif case == "extra":
        (directory / "scripts").chmod(0o755)
        (directory / "scripts/unknown.py").write_text("# not approved\n")
        (directory / "scripts").chmod(0o555)
    if case == "namespace":
        with pytest.raises(remote.ReleaseError, match="retirement_module_namespace_rejected"):
            with engine.retirement_modules(directory):
                pass
    else:
        with pytest.raises(remote.ReleaseError):
            engine.retirement_sealed_sources(directory)


def test_action_durable_safe_events_and_scope_no_switch_authority(prepared, monkeypatch):
    data, seen, _ = wire(prepared, monkeypatch)
    result = prepared.engine.execute(data)["legacy_retirement"]
    assert result["passed"] and result["baseline_verified_after"] and result["production_verified"] is False
    assert set(seen) == remote.RETIREMENT_CASE_IDS
    summary = result["summary"]
    assert (
        summary["production_quiescence_proven"] is False
        and summary["systemctl_stop_end_to_end_proven"] is False
    )
    assert summary["validates_single_main_signal_only"] is True
    directory = prepared.engine.record / "legacy-retirement" / existing.RELEASE
    lines = (directory / "events.jsonl").read_bytes().splitlines(keepends=True)
    previous = "0" * 64
    for index, raw in enumerate(lines, 1):
        row = json.loads(raw)
        assert row["sequence"] == index and row["previous_sha256"] == previous
        previous = remote.digest(raw)
    assert result["last_event_sha256"] == previous and result["events"] == len(lines)
    assert json.loads(lines[-1])["event"] == "retirement_validation_result"
    original = (directory / "receipt.json").read_bytes()
    with pytest.raises(remote.ReleaseError, match="retirement_attempt_already_exists"):
        prepared.engine.execute(data)
    assert (directory / "receipt.json").read_bytes() == original
    assert not any(args[:2] == ["/usr/bin/systemctl", "kill"] for args, _ in prepared.commands)


@pytest.mark.parametrize(
    "mode,code",
    [
        ("scope", "retirement_safe_result_rejected"),
        ("unsafe_event", "retirement_safe_event_rejected"),
        ("failure", "legacy_retirement_validation_failed"),
        ("unrecorded", "retirement_durable_summary_required"),
        ("after_summary", "retirement_events_after_summary_rejected"),
        ("timeout", "retirement_command_timeout_rejected"),
    ],
)
def test_failures_preserve_attempt_without_accepted_receipt(prepared, monkeypatch, mode, code):
    data, _, _ = wire(prepared, monkeypatch, mode)
    with pytest.raises(remote.ReleaseError, match=code):
        prepared.engine.execute(data)
    directory = prepared.engine.record / "legacy-retirement" / existing.RELEASE
    receipt = json.loads((directory / "receipt.json").read_bytes())
    assert receipt["passed"] is False and receipt["failure_code"] == code
    assert directory.is_dir() and (directory / "events.jsonl").exists()
    if mode == "unsafe_event":
        assert b"SIGKILL" not in (directory / "events.jsonl").read_bytes()


def test_post_validation_baseline_change_keeps_signal_result_but_fails_action(prepared, monkeypatch):
    data, _, baseline = wire(prepared, monkeypatch)
    calls = []

    def current():
        calls.append(1)
        return dict(baseline, production_pid=222) if len(calls) > 1 else dict(baseline)

    monkeypatch.setattr(prepared.engine, "retirement_baseline", current)
    with pytest.raises(remote.ReleaseError, match="retirement_original_baseline_changed"):
        prepared.engine.execute(data)
    receipt = json.loads(
        (prepared.engine.record / "legacy-retirement" / existing.RELEASE / "receipt.json").read_bytes()
    )
    assert receipt["summary"]["retirement_validation_passed"] is True
    assert receipt["passed"] is False and receipt["baseline_verified_after"] is False


@pytest.mark.parametrize("changed", ["runtime", "source", "environment", "valid"])
def test_baseline_binds_actual_original_source_runtime_and_environment(prepared, monkeypatch, changed):
    engine = prepared.engine
    engine.request = dict(prepared.data)
    engine.request["receipts"].update(
        expected_current_fingerprint="f" * 64, expected_source_manifest_sha256="e" * 64
    )
    engine.request["receipts"]["dependencies"]["base_environment_sha256"] = remote.RETIREMENT_BASE_ENV_SHA256
    monkeypatch.setattr(
        engine,
        "runtime",
        lambda: {
            "working_directory": str(engine.production),
            "fingerprint": "a" * 64 if changed == "runtime" else "f" * 64,
        },
    )
    monkeypatch.setattr(
        engine, "source_inventory", lambda *args: "a" * 64 if changed == "source" else "e" * 64
    )
    monkeypatch.setattr(
        remote,
        "tree_digest",
        lambda *args: ("a" * 64 if changed == "environment" else remote.RETIREMENT_BASE_ENV_SHA256, 0),
    )
    if changed == "valid":
        assert engine.retirement_baseline()["production_pid"] == 111
    else:
        with pytest.raises(remote.ReleaseError):
            engine.retirement_baseline()


@pytest.mark.parametrize(
    "unit,output,allowed",
    [
        (
            "prism-retirement-probe-" + "a" * 32 + "-int-executor.service",
            b"LoadState=not-found\nMainPID=0\n",
            True,
        ),
        ("prism-retirement-probe-" + "a" * 32 + "-unknown.service", b"LoadState=not-found\n", False),
        ("stock-app.service", b"LoadState=not-found\n", False),
        (
            "prism-retirement-probe-" + "a" * 32 + "-int-post.service",
            b"LoadState=not-found\nEnvironment=unsafe\n",
            False,
        ),
    ],
)
def test_only_fixed_stopped_synthetic_show_can_be_missing(monkeypatch, unit, output, allowed):
    monkeypatch.setattr(
        remote.subprocess, "run", lambda *args, **kwargs: types.SimpleNamespace(returncode=1, stdout=output)
    )
    argv = ["/usr/bin/systemctl", "show", "--property=" + remote.RETIREMENT_PROPERTIES, unit]
    if allowed:
        assert remote.run_command(argv, timeout=10) == output
    else:
        with pytest.raises(remote.ReleaseError, match="command_failed"):
            remote.run_command(argv, timeout=10)


def test_mac_requires_both_literal_pins(tmp_path):
    receipts = tmp_path / "receipts.json"
    receipts.write_text(json.dumps({"commit": existing.COMMIT}))
    args = types.SimpleNamespace(
        action="validate_legacy_retirement",
        commit=existing.COMMIT,
        release_id=existing.RELEASE,
        executor_sha256=controller.sha((controller.ROOT / "remote_release.py").read_bytes()),
        receipts=receipts,
        source_manifest=None,
        external_source_manifest=None,
        retirement_probe_sha256=None,
        retirement_validation_sha256=None,
    )
    with pytest.raises(controller.ControlError, match="approved_retirement_helpers_required"):
        controller.request_for(args)
    args.retirement_probe_sha256 = remote.RETIREMENT_HELPERS["retirement_probe.py"]
    with pytest.raises(controller.ControlError, match="approved_retirement_helpers_required"):
        controller.request_for(args)
    args.retirement_validation_sha256 = remote.RETIREMENT_HELPERS["retirement_validation.py"]
    request, _ = controller.request_for(args)
    assert request["retirement_helpers"] == sources()
    assert request["retirement_attempt"] == existing.RELEASE
