"""Offline controller wiring on disposable shipped scripts; no real acceptance.

The fixture's systemd, kernel results and upstream summaries are synthetic. Public
helpers stay byte-pinned; no SSH, production, DNS, HTTP, model or account creation.
"""

import base64
import copy
import io
import json
import os
import stat
import types
from contextlib import contextmanager
from pathlib import Path

import pytest
import test_release_controller as existing

ROOT = Path(__file__).resolve().parents[1]
ATTEMPT = "synthetic-market-20261022"
NOW, COMMIT = existing.NOW, existing.COMMIT


@pytest.fixture
def modules(tmp_path):
    destination = tmp_path / "candidate"
    scripts = destination / "scripts"
    scripts.mkdir(parents=True)
    before = {}
    for name in (
        "remote_release.py",
        "release_controller.py",
        "market_live_probe.py",
        "market_live_validation.py",
        "cgroup_network_gate.py",
    ):
        raw = (ROOT / "scripts" / name).read_bytes()
        before[name] = raw
        (scripts / name).write_bytes(raw)
    # This wiring is now shipped. Exercise the exact copied scripts rather than
    # reversing an archived candidate patch, which would erase later controls.
    assert "MARKET_INTEGRATION_HELPERS" in (scripts / "remote_release.py").read_text()
    loaded = []
    for name in ("remote_release", "release_controller"):
        module = types.ModuleType("_market_candidate_" + name)
        module.__file__ = str(scripts / (name + ".py"))
        exec(compile(Path(module.__file__).read_bytes(), module.__file__, "exec"), module.__dict__)
        loaded.append(module)
    yield tuple(loaded)
    assert all((ROOT / "scripts" / name).read_bytes() == raw for name, raw in before.items())


@pytest.fixture
def prepared(modules, monkeypatch):
    remote, controller = modules
    monkeypatch.setattr(existing, "remote", remote)
    fixture = existing.Fixture()
    fixture.setUp()
    fixture.prepared()
    engine = fixture.engine
    source = engine.production / "app.py"
    source.write_bytes(b"# public synthetic market baseline\n")
    source.chmod(0o644)
    manifest = {
        "files": [
            {
                "path": "app.py",
                "sha256": remote.digest(source.read_bytes()),
                "size": source.stat().st_size,
                "mode": "0644",
            }
        ]
    }
    data = copy.deepcopy(fixture.data)
    data.update(
        action="integration_market",
        integration_attempt=ATTEMPT,
        source_manifest=manifest,
        market_helpers={
            name: {
                "sha256": pin,
                "content": base64.b64encode((ROOT / "scripts" / name).read_bytes()).decode(),
            }
            for name, pin in remote.MARKET_INTEGRATION_HELPERS.items()
        },
    )
    data["receipts"].update(
        expected_current_fingerprint="f" * 64,
        expected_source_manifest_sha256=remote.digest(remote.encoded(manifest)),
    )
    existing.bind(data)
    engine.request = data
    monkeypatch.setattr(
        engine, "runtime", lambda: {"working_directory": str(engine.production), "fingerprint": "f" * 64}
    )
    try:
        yield types.SimpleNamespace(
            remote=remote, controller=controller, fixture=fixture, engine=engine, data=data
        )
    finally:
        fixture.tearDown()


def receipt(context):
    path = (
        context.engine.record / ("integration-market-" + context.data["integration_attempt"]) / "receipt.json"
    )
    return json.loads(path.read_bytes())


def synthetic_result(context, module, *, accepted=False):
    binding = {
        "candidate": COMMIT,
        "release_id": ATTEMPT,
        "production_pid": 111,
        "pid": 321,
        "nonce": "d" * 32,
        "issued_at": NOW,
        "probe_sha256": context.remote.MARKET_INTEGRATION_HELPERS["market_live_probe.py"],
        "worker_sha256": context.remote.MARKET_WORKER_SHA256,
    }
    checks = dict.fromkeys(module.CHECKS, True)
    checks["unauthenticated_api_denied"] = accepted
    worker = {
        "schema": 1,
        "binding": binding,
        "checked_at": NOW,
        "passed": accepted,
        "market_provider_verified": True,
        "production_verified": False,
        "research_quality_verified": False,
        "live_model_verified": False,
        "native_search_verified": False,
        "native_fetch_verified": False,
        "error_class": None if accepted else "market_acceptance_incomplete",
        "checks": checks,
        "request_counts": {"quote": 1, "kline": 1},
        "https": {
            kind: {
                "https_verified": True,
                "redirect_followed": False,
                "response_sha256": "a" * 64,
                "response_bytes": 100,
            }
            for kind in ("quote", "kline")
        },
        "cached_result_sha256": {kind: "b" * 64 for kind in ("quote", "kline")},
        "kline_provenance_missing": [] if accepted else ["source", "retrieved_at", "data_timestamp"],
        "kline_adapter_is_tencent_actual_endpoint_is_sina": True,
        "failure_injection_disclosed": True,
        "fetched_at_is_not_trade_date": True,
    }
    return {
        "schema": 1,
        "probe": "credential_free_market_linux",
        "scope": "market_public_https_only",
        "candidate": COMMIT,
        "release_id": ATTEMPT,
        "started_at": NOW,
        "finished_at": NOW,
        "passed": accepted,
        "actual_linux_collection": True,
        "original_runtime_unchanged": True,
        "market_provider_verified": True,
        "production_verified": False,
        "research_quality_verified": False,
        "live_model_verified": False,
        "native_search_verified": False,
        "native_fetch_verified": False,
        "error_class": None if accepted else "market_acceptance_incomplete",
        "unit_stopped": True,
        "preflight_only": False,
        "network_gate_verified": True,
        "network_gate_verified_before_cleanup": True,
        "network_gate_cleaned": True,
        "negative_errno": dict.fromkeys(
            (
                "production_8088_denied",
                "vibe_8899_denied",
                "metadata_denied",
                "private_ip_denied",
                "other_public_denied",
            ),
            13,
        ),
        "additional_network_errno": dict.fromkeys(module.EXTRA_NEGATIVES, 13),
        "cleanup_error_class": None,
        "identity": {
            "pid": 321,
            "startticks": 123,
            "invocation": "e" * 32,
            "uids": [2001] * 4,
            "host_network_namespace": True,
            "network_namespace_inode": 999,
            "private_readonly_run": True,
        },
        "bootstrap_sha256": "c" * 64,
        "os_denial_controls": dict.fromkeys(module.BOOT_CHECKS, True),
        "worker_result_sha256": "d" * 64,
        "worker_result": worker,
    }


def synthetic_command(context, module, bindings):
    validation = object.__new__(module.Validation)
    validation.request = bindings
    validation.work = context.engine.operations / "integration" / ATTEMPT / "market"
    validation.candidate = context.engine.release
    validation.nonce, validation.issued = "d" * 32, NOW
    validation.preflight_only = False
    validation.unit = "prism-market-probe-" + COMMIT[:12] + "-" + validation.nonce + ".service"
    validation.pins = {"qt.gtimg.cn": ["1.1.1.1"], "money.finance.sina.com.cn": ["8.8.8.8"]}
    validation.negative = "9.9.9.9"
    return validation


def install_synthetic_driver(context, monkeypatch, driver):
    original = context.engine.market_module

    @contextmanager
    def load(contents):
        with original(contents) as module:
            monkeypatch.setattr(
                module, "run_validation", lambda bindings, **callbacks: driver(module, bindings, **callbacks)
            )
            yield module

    monkeypatch.setattr(context.engine, "market_module", load)


def successful_driver(context, *, accepted=False, mutate=None):
    def driver(module, bindings, *, boundary, run, record, network_gate_path):
        assert set(bindings) == {"schema", "release_id", "candidate", "production_pid", "probe_source"}
        assert bindings["probe_source"]["sha256"] == module.PROBE_SHA256
        boundary("market_start")
        record({"stage": "start", "candidate": COMMIT, "release_id": ATTEMPT, "at": NOW})
        validation = synthetic_command(context, module, bindings)
        run(validation.inventory_command(), timeout=5)
        run(validation.command(), timeout=15)
        run(
            [
                "/usr/bin/systemctl",
                "show",
                "--property=" + context.remote.MARKET_UNIT_PROPERTIES,
                validation.unit,
            ],
            timeout=5,
        )
        boundary("market_allow_public_http")
        run(["/usr/bin/systemctl", "stop", validation.unit], timeout=15)
        result = synthetic_result(context, module, accepted=accepted)
        record(result)
        if mutate:
            mutate()
        return result

    return driver


def test_two_frozen_helpers_are_exactly_pinned_on_both_sides(modules):
    remote, controller = modules
    assert remote.MARKET_INTEGRATION_HELPERS == controller.MARKET_INTEGRATION_HELPERS
    assert set(remote.MARKET_INTEGRATION_HELPERS) == {
        "market_live_probe.py",
        "market_live_validation.py",
        "cgroup_network_gate.py",
    }
    assert "integration_market" in remote.ACTIONS and "integration_market" in controller.ACTIONS
    for name, pin in remote.MARKET_INTEGRATION_HELPERS.items():
        assert remote.digest((ROOT / "scripts" / name).read_bytes()) == pin


@pytest.mark.parametrize("change", ["missing", "digest", "bytes", "extra", "base64"])
def test_bad_helper_bytes_rejected_before_any_import(prepared, change):
    supplied = prepared.data["market_helpers"]
    first = supplied["market_live_probe.py"]
    if change == "missing":
        supplied.pop("market_live_probe.py")
    elif change == "digest":
        first["sha256"] = "a" * 64
    elif change == "bytes":
        first["content"] = base64.b64encode(b"raise RuntimeError('never imported')").decode()
    elif change == "extra":
        supplied["candidate.py"] = dict(first)
    else:
        first["content"] = "!invalid!"
    with pytest.raises(prepared.remote.ReleaseError, match="approved_market_integration_helpers_required"):
        prepared.engine.market_helper_sources()


def test_only_root_sealed_fixed_supervisor_is_imported(prepared):
    contents = prepared.engine.market_helper_sources()
    with prepared.engine.market_module(contents) as module:
        directory = Path(module.__file__).parent
        assert stat.S_IMODE(directory.stat().st_mode) == 0o555
        assert module.PROBE_SHA256 == prepared.remote.MARKET_INTEGRATION_HELPERS["market_live_probe.py"]
        assert module.WORKER_SHA256 == prepared.remote.MARKET_WORKER_SHA256
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o444 for path in directory.iterdir())
        assert not hasattr(module, "exercise")  # Probe/candidate code is not imported in root.


@pytest.mark.parametrize("change", ["writable", "hardlink", "symlink", "content", "extra"])
def test_sealed_helper_mutation_is_rejected(prepared, change):
    contents = prepared.engine.market_helper_sources()
    with prepared.engine.market_module(contents) as module:
        directory = Path(module.__file__).parent
    directory.chmod(0o700)
    path = directory / "market_live_validation.py"
    if change == "writable":
        path.chmod(0o644)
    elif change == "hardlink":
        os.link(path, directory.parent / "outside-hardlink")
    elif change == "symlink":
        raw = path.read_bytes()
        path.unlink()
        target = directory.parent / "outside-source"
        target.write_bytes(raw)
        target.chmod(0o444)
        path.symlink_to(target)
    elif change == "content":
        path.chmod(0o600)
        path.write_bytes(b"# synthetic altered source\n")
        path.chmod(0o444)
    else:
        (directory / "unexpected.py").write_bytes(b"# never imported\n")
    directory.chmod(0o555)
    with pytest.raises(
        prepared.remote.ReleaseError, match="market_helper_source_rejected|symlink_path_rejected"
    ):
        with prepared.engine.market_module(contents):
            pytest.fail("altered helper was imported")


def mac_arguments(context, tmp_path):
    receipts = tmp_path / "receipts.json"
    receipts.write_bytes(context.remote.encoded(context.data["receipts"]))
    source = tmp_path / "source.json"
    source.write_bytes(context.remote.encoded(context.data["source_manifest"]))
    values = {
        "action": "integration_market",
        "commit": COMMIT,
        "release_id": existing.RELEASE,
        "executor_sha256": context.remote.digest(Path(context.remote.__file__).read_bytes()),
        "receipts": str(receipts),
        "source_manifest": str(source),
        "external_source_manifest": None,
        "integration_attempt": ATTEMPT,
    }
    for name, pin in context.remote.MARKET_INTEGRATION_HELPERS.items():
        values[name.removesuffix(".py").replace("market_live_", "market_") + "_sha256"] = pin
    return types.SimpleNamespace(**values)


def test_mac_requires_explicit_pins_manifest_fingerprint_and_short_attempt(prepared, tmp_path):
    args = mac_arguments(prepared, tmp_path)
    request, _ = prepared.controller.request_for(args)
    assert request["market_helpers"] == prepared.data["market_helpers"]
    assert request["integration_attempt"] == ATTEMPT and "live_helpers" not in request
    for name in ("market_probe_sha256", "market_validation_sha256"):
        saved = getattr(args, name)
        setattr(args, name, None)
        with pytest.raises(
            prepared.controller.ControlError, match="approved_market_integration_helpers_required"
        ):
            prepared.controller.request_for(args)
        setattr(args, name, saved)
    for attempt in (None, "a" * 41, "../outside", "short"):
        args.integration_attempt = attempt
        with pytest.raises(
            prepared.controller.ControlError, match="explicit_market_integration_attempt_required"
        ):
            prepared.controller.request_for(args)
    args.integration_attempt = "a" * 40
    assert prepared.controller.request_for(args)[0]["integration_attempt"] == "a" * 40
    args.source_manifest = None
    with pytest.raises(
        prepared.controller.ControlError, match="market_integration_private_bindings_required"
    ):
        prepared.controller.request_for(args)


@pytest.mark.parametrize("attempt", [None, "a" * 41, "../outside", "short"])
def test_remote_rejects_missing_or_oversize_attempt(prepared, attempt):
    prepared.data["integration_attempt"] = attempt
    with pytest.raises(prepared.remote.ReleaseError, match="explicit_market_integration_attempt_required"):
        prepared.engine.integration_market()


def test_real_provider_subcheck_never_turns_missing_auth_provenance_into_success(prepared, monkeypatch):
    install_synthetic_driver(prepared, monkeypatch, successful_driver(prepared))
    with pytest.raises(prepared.remote.ReleaseError, match="^market_acceptance_incomplete$"):
        prepared.engine.execute(prepared.data)
    evidence = receipt(prepared)
    assert evidence["passed"] is False and evidence["summary"]["market_provider_verified"] is True
    assert evidence["summary"]["passed"] is False
    assert evidence["summary"]["worker_result"]["checks"]["unauthenticated_api_denied"] is False
    assert evidence["summary"]["worker_result"]["kline_provenance_missing"]
    assert evidence["candidate_unchanged"] is True and evidence["original_runtime_unchanged"] is True
    assert json.loads((prepared.engine.record / "status.json").read_bytes())["outcome"] == "failed"
    assert (
        "market_allow_public_http" in prepared.fixture.boundaries
        and "market_reconcile" in prepared.fixture.boundaries
    )
    journal = prepared.engine.record / ("integration-market-" + ATTEMPT) / "events.jsonl"
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600
    entries = [json.loads(line) for line in journal.read_bytes().splitlines()]
    previous = prepared.remote.digest(b"")
    for number, entry in enumerate(entries, 1):
        assert entry["sequence"] == number and entry["previous_sha256"] == previous
        previous = prepared.remote.digest(prepared.remote.encoded(entry) + b"\n")
    assert evidence["last_event_sha256"] == previous
    with pytest.raises(prepared.remote.ReleaseError, match="market_integration_attempt_already_exists"):
        prepared.engine.integration_market()


def test_synthetic_complete_contract_records_success_only_with_observed_cleanup(prepared, monkeypatch):
    install_synthetic_driver(prepared, monkeypatch, successful_driver(prepared, accepted=True))
    result = prepared.engine.execute(prepared.data)
    assert result["integration"]["passed"] is True and result["production_verified"] is False
    assert receipt(prepared)["passed"] is True
    assert all(
        command[-1] != prepared.remote.SERVICE
        for command, _ in prepared.fixture.commands
        if command[:2] == ["/usr/bin/systemctl", "stop"]
    )


@pytest.mark.parametrize("mode", ["no_cleanup", "different_return", "post_summary", "unsafe_event"])
def test_missing_cleanup_or_non_durable_results_cannot_complete(prepared, monkeypatch, mode):
    raw = "synthetic-private-value-never-journaled"

    def driver(module, bindings, *, boundary, run, record, network_gate_path):
        validation = synthetic_command(prepared, module, bindings)
        result = synthetic_result(prepared, module, accepted=True)
        if mode == "unsafe_event":
            record({"stage": "start", "candidate": COMMIT, "release_id": ATTEMPT, "at": NOW, "password": raw})
        if mode != "no_cleanup":
            run(validation.inventory_command(), timeout=5)
            run(validation.command(), timeout=15)
            run(["/usr/bin/systemctl", "stop", validation.unit], timeout=15)
        record(result)
        if mode == "different_return":
            result = {**result, "worker_result_sha256": "a" * 64}
        elif mode == "post_summary":
            record({"stage": "start", "candidate": COMMIT, "release_id": ATTEMPT, "at": NOW})
        return result

    install_synthetic_driver(prepared, monkeypatch, driver)
    with pytest.raises(
        prepared.remote.ReleaseError,
        match="market_cleanup_not_observed|market_durable_result_required|market_event_order_rejected|market_safe_record_rejected",
    ):
        prepared.engine.integration_market()
    evidence = receipt(prepared)
    assert evidence["passed"] is False
    journal = prepared.engine.record / ("integration-market-" + ATTEMPT) / "events.jsonl"
    assert raw.encode() not in journal.read_bytes() and raw not in json.dumps(evidence)


@pytest.mark.parametrize(
    "change", ["password", "price", "url", "identity_uid", "passed", "checks", "false_count"]
)
def test_raw_or_false_claims_never_reach_private_event_log(prepared, change):
    with prepared.engine.market_module(prepared.engine.market_helper_sources()) as module:
        result = synthetic_result(prepared, module)
        if change in {"password", "price", "url"}:
            result[change] = "synthetic-private-raw-value"
        elif change == "identity_uid":
            result["identity"]["uids"] = [0] * 4
        elif change == "passed":
            result["passed"] = True
            result["error_class"] = None
        elif change == "checks":
            result["worker_result"]["checks"]["undeclared"] = True
        else:
            result["worker_result"]["request_counts"]["quote"] = True
        with pytest.raises(prepared.remote.ReleaseError, match="market_safe_record_rejected"):
            prepared.engine.market_safe_record(
                result, ATTEMPT, module, prepared.engine.market_original_state()
            )


def test_append_journal_survives_short_writes_and_fsyncs_each_event(prepared, monkeypatch):
    original_write, original_fsync = prepared.remote.os.write, prepared.remote.os.fsync
    written, synced = [], []

    def short_write(descriptor, value):
        written.append(len(value))
        return original_write(descriptor, value[: max(1, len(value) // 2)])

    def fsync(descriptor):
        synced.append(os.fstat(descriptor).st_ino)
        return original_fsync(descriptor)

    monkeypatch.setattr(prepared.remote.os, "write", short_write)
    monkeypatch.setattr(prepared.remote.os, "fsync", fsync)
    install_synthetic_driver(prepared, monkeypatch, successful_driver(prepared))
    with pytest.raises(prepared.remote.ReleaseError, match="market_acceptance_incomplete"):
        prepared.engine.integration_market()
    journal = prepared.engine.record / ("integration-market-" + ATTEMPT) / "events.jsonl"
    rows = [json.loads(line) for line in journal.read_bytes().splitlines()]
    assert len(rows) == receipt(prepared)["events"] == 2
    assert len(written) > 2 and synced.count(journal.stat().st_ino) == 2


@pytest.mark.parametrize(
    "kind",
    [
        "service",
        "shell",
        "uid",
        "ip",
        "unit",
        "second_launch",
        "stop_other",
        "show_other",
        "timeout",
        "resume",
    ],
)
def test_only_exact_nonce_command_and_own_cleanup_are_executable(prepared, monkeypatch, kind):
    def driver(module, bindings, *, boundary, run, record, network_gate_path):
        validation = synthetic_command(prepared, module, bindings)
        argv = validation.command()
        run(validation.inventory_command(), timeout=5)
        timeout = 15
        if kind == "service":
            argv = ["/usr/bin/systemctl", "restart", "stock-app.service"]
        elif kind == "shell":
            argv[argv.index("/usr/bin/env")] = "/bin/sh"
        elif kind == "uid":
            argv = [item.replace("User=prism-integration", "User=root") for item in argv]
        elif kind == "ip":
            config = json.loads(argv[-1])
            config["pins"]["qt.gtimg.cn"] = ["127.0.0.1"]
            argv[-1] = module.encoded(config).decode()
        elif kind == "unit":
            argv = ["--unit=stock-app.service" if item.startswith("--unit=") else item for item in argv]
        elif kind == "second_launch":
            run(argv, timeout=15)
        elif kind == "stop_other":
            run(argv, timeout=15)
            argv = [
                "/usr/bin/systemctl",
                "stop",
                "prism-market-probe-" + COMMIT[:12] + "-" + "e" * 32 + ".service",
            ]
        elif kind == "show_other":
            argv = ["/usr/bin/systemctl", "show", "--property=Environment", "stock-app.service"]
        elif kind == "timeout":
            timeout = 16
        else:
            argv = ["/usr/sbin/useradd", "prism-integration"]
        run(argv, timeout=timeout)
        pytest.fail("arbitrary command escaped the fixed wrapper")

    install_synthetic_driver(prepared, monkeypatch, driver)
    with pytest.raises(prepared.remote.ReleaseError, match="^market_command_rejected$"):
        prepared.engine.integration_market()
    assert receipt(prepared)["passed"] is False
    assert receipt(prepared)["summary"] is None


def test_stop_boundary_keeps_only_own_nonce_cleanup_available(prepared, monkeypatch):
    stopped = []

    def driver(module, bindings, *, boundary, run, record, network_gate_path):
        validation = synthetic_command(prepared, module, bindings)
        run(validation.inventory_command(), timeout=5)
        run(validation.command(), timeout=15)
        monkeypatch.setattr(prepared.engine, "control", lambda: True)
        with pytest.raises(module.ValidationError, match="operator_stop"):
            boundary("market_wait_result")
        run(["/usr/bin/systemctl", "stop", validation.unit], timeout=15)
        stopped.append(validation.unit)
        raise prepared.remote.ReleaseError("operator_stop")

    install_synthetic_driver(prepared, monkeypatch, driver)
    with pytest.raises(prepared.remote.ReleaseError, match="^operator_stop$"):
        prepared.engine.integration_market()
    assert len(stopped) == 1 and receipt(prepared)["passed"] is False


@pytest.mark.parametrize("mode", ["missing", "occupied", "different_nonce"])
def test_launch_requires_empty_inventory_for_exact_same_nonce(prepared, monkeypatch, mode):
    original = prepared.engine.run

    def observations(argv, **kwargs):
        if mode == "occupied" and argv[:2] == ["/usr/bin/systemctl", "list-units"]:
            return b"synthetic-existing-unit loaded active running\n"
        return original(argv, **kwargs)

    monkeypatch.setattr(prepared.engine, "run", observations)

    def driver(module, bindings, *, boundary, run, record, network_gate_path):
        value = synthetic_command(prepared, module, bindings)
        if mode != "missing":
            run(value.inventory_command(), timeout=5)
        if mode == "different_nonce":
            value.nonce = "e" * 32
            value.unit = "prism-market-probe-" + COMMIT[:12] + "-" + value.nonce + ".service"
        run(value.command(), timeout=15)
        pytest.fail("no empty same-instance inventory was proven")

    install_synthetic_driver(prepared, monkeypatch, driver)
    with pytest.raises(prepared.remote.ReleaseError, match="^market_command_rejected$"):
        prepared.engine.integration_market()
    assert receipt(prepared)["passed"] is False
    assert not any(
        argv[0] == "/usr/bin/systemd-run"
        for argv, _ in prepared.fixture.commands
        if any("prism-market-probe" in item for item in argv)
    )


@pytest.mark.parametrize("kind", ["market", "glob", "other_service", "extra_flag"])
def test_market_inventory_real_subprocess_stdout_capture_is_narrow(modules, monkeypatch, kind):
    remote, _ = modules
    unit = "prism-market-probe-" + COMMIT[:12] + "-" + "d" * 32 + ".service"
    prefix = ["/usr/bin/systemctl", "list-units", "--all", "--plain", "--no-legend"]
    argv = {
        "market": prefix + [unit],
        "glob": prefix + ["prism-market-probe-*.service"],
        "other_service": prefix + ["stock-app.service"],
        "extra_flag": prefix + [unit, "--full"],
    }[kind]
    calls = []

    def invoke(args, **kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(
            returncode=0,
            stdout=b"occupied-public-unit\n" if kwargs["stdout"] == remote.subprocess.PIPE else None,
        )

    monkeypatch.setattr(remote.subprocess, "run", invoke)
    captured = kind == "market"
    assert remote.run_command(argv, timeout=5) == (b"occupied-public-unit\n" if captured else b"")
    assert calls[0]["stdout"] == (remote.subprocess.PIPE if captured else remote.subprocess.DEVNULL)
    assert calls[0]["stderr"] == remote.subprocess.DEVNULL


@pytest.mark.parametrize("target", ["production", "candidate"])
def test_source_drift_after_worker_cannot_complete_action(prepared, monkeypatch, target):
    path = (
        prepared.engine.production / "app.py"
        if target == "production"
        else prepared.engine.release / "server/main.py"
    )
    install_synthetic_driver(
        prepared,
        monkeypatch,
        successful_driver(prepared, accepted=True, mutate=lambda: path.write_bytes(b"# synthetic drift\n")),
    )
    with pytest.raises(
        prepared.remote.ReleaseError,
        match="market_original_source_changed|production_source_drift|prepared_source_drift",
    ):
        prepared.engine.integration_market()
    evidence = receipt(prepared)
    assert evidence["passed"] is False
    assert evidence["original_runtime_unchanged"] is (target != "production")
    assert evidence["candidate_unchanged"] is (target != "candidate")


def test_final_reconciliation_drift_cannot_be_hidden_by_earlier_success(prepared, monkeypatch):
    install_synthetic_driver(prepared, monkeypatch, successful_driver(prepared, accepted=True))
    original = prepared.engine.market_original_state
    calls = []

    def final_drift():
        calls.append(None)
        result = original()
        # baseline + two actual helper boundaries + reconcile are unchanged;
        # drift appears only at the mandatory finally reconciliation.
        return {**result, "production_pid": 999} if len(calls) == 5 else result

    monkeypatch.setattr(prepared.engine, "market_original_state", final_drift)
    with pytest.raises(prepared.remote.ReleaseError, match="market_final_reconciliation_failed"):
        prepared.engine.integration_market()
    assert receipt(prepared)["passed"] is False


def test_every_remote_permit_rechecks_issue9_and_local_stop(modules, monkeypatch, tmp_path):
    _, controller = modules
    gates = []
    nonce = "a" * 32
    process = types.SimpleNamespace(
        stdin=io.BytesIO(),
        stdout=io.BytesIO(
            (
                json.dumps({"permit_required": nonce, "boundary": "market_start"})
                + "\n"
                + json.dumps({"result": {"production_verified": False}})
                + "\n"
            ).encode()
        ),
        poll=lambda: 0,
        wait=lambda **kwargs: 0,
    )
    monkeypatch.setattr(controller.select, "select", lambda *_args: ([process.stdout], [], []))
    result = controller.execute(
        {"action": "integration_market"},
        b"# synthetic executor, never run\n",
        tmp_path / "STOP",
        popen=lambda *args, **kwargs: process,
        gate=lambda path: gates.append(path),
    )
    assert result["production_verified"] is False and len(gates) == 2
