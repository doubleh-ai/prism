"""Promoted fixed 004 readonly reconciliation; archival patch is never Linux evidence."""

import copy
import json
import subprocess
import types
from contextlib import contextmanager
from pathlib import Path

import pytest
import test_retirement_controller as signal
import test_retirement_stop_controller as prior

from scripts import retirement_probe as probe
from scripts import retirement_stop_validation as stop
from scripts import retirement_validation as validation

ROOT = Path(__file__).resolve().parents[1]
prepared = signal.prepared


@pytest.fixture
def promoted(tmp_path, monkeypatch):
    destination = tmp_path / "candidate"
    (destination / "scripts").mkdir(parents=True)
    original = {}
    for name in ("remote_release.py", "release_controller.py"):
        original[name] = (ROOT / "scripts" / name).read_bytes()
        (destination / "scripts" / name).write_bytes(original[name])
    patch = (ROOT / "scripts/patches/stop004-reconciliation-candidate.patch").read_bytes()
    # The public formal code may already contain this promoted control. Exercise
    # those exact current bytes; reversing its archive can erase later controls.
    if b"RETIREMENT_RETAINED_OBSERVER_START =" not in original["remote_release.py"]:
        applied = subprocess.run(
            ["/usr/bin/patch", "--batch", "--forward", "--fuzz=0", "-s", "-p1"],
            input=patch,
            cwd=destination,
            capture_output=True,
            timeout=5,
        )
        assert applied.returncode == 0
    module = types.ModuleType("_stop004_candidate")
    module.__file__ = str(destination / "scripts/remote_release.py")
    exec(compile(Path(module.__file__).read_bytes(), module.__file__, "exec"), module.__dict__)
    monkeypatch.setattr(prior, "remote", module)
    yield module
    for name, raw in original.items():
        assert (ROOT / "scripts" / name).read_bytes() == raw


def test_promoted_fixture_uses_exact_current_source_without_replaying_archive(tmp_path, monkeypatch):
    current = (ROOT / "scripts/remote_release.py").read_bytes()
    if b"RETIREMENT_RETAINED_OBSERVER_START =" not in current:
        pytest.skip("legacy unpromoted fixture intentionally needs the forward archive")
    original_run = subprocess.run

    def no_replay(argv, *args, **kwargs):
        if argv[:1] == ["/usr/bin/patch"]:
            pytest.fail("promoted formal source must not replay its archival patch")
        return original_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", no_replay)
    fixture = promoted.__wrapped__(tmp_path, monkeypatch)
    module = next(fixture)
    assert Path(module.__file__).read_bytes() == current
    with pytest.raises(StopIteration):
        next(fixture)


def seed_fourth(prepared, monkeypatch, promoted):
    prepared.engine.__class__ = promoted.RemoteRelease
    data, baseline, _, _ = prior.wire(prepared, monkeypatch)
    third, _, _ = prior.seed_retained_start(prepared, data, baseline, monkeypatch)
    engine = prepared.engine
    engine.retirement_reconcile_prestart(
        baseline,
        prior.bindings(),
        probe,
        validation,
        stop,
        engine.retirement_signal_proof(baseline, prior.bindings(), probe, validation),
    )
    retained = engine._retirement_retained_start_reconciliation
    reviewed = copy.deepcopy(promoted.RETIREMENT_RETAINED_OBSERVER_START)
    directory = prior.seed_reviewed_prestart(
        prepared, data, baseline, attempt=reviewed["attempt"], helper_sha=reviewed["helper_sha256"]
    )
    third_identity = json.loads((third / "identity.json").read_bytes())
    prestart = third_identity["prestart_reconciliations"]
    identity = json.loads((directory / "identity.json").read_bytes())
    identity.update(prestart_reconciliations=prestart, retained_start_reconciliation=retained)
    (directory / "identity.json").write_bytes(promoted.encoded(identity))
    receipt = json.loads((directory / "receipt.json").read_bytes())
    receipt["summary"].update(
        error_code="retirement_stop_observer_reference_rejected", synthetic_cleanup_verified=False
    )
    first = json.loads((directory / "events.jsonl").read_bytes().splitlines()[0])
    details = [
        ("public_bundles_verified", first["details"]),
        ("synthetic_start_intent", {"case_id": reviewed["case_id"], "nonce": reviewed["nonce"]}),
        (
            "synthetic_stop_actual_isolation",
            {
                "case_id": reviewed["case_id"],
                "invocation": reviewed["invocation_id"],
                "pid": reviewed["exec_main_pid"],
                "start_ticks": reviewed["start_ticks"],
                "uid": reviewed["uid"],
                "actual_kernel_verified": True,
            },
        ),
        (
            "synthetic_stop_exec_arrays_observed",
            {
                "case_id": reviewed["case_id"],
                "properties": {key: stop.EMPTY_EXEC_ARRAY for key in ("ExecStop", "ExecStopPost")},
            },
        ),
        ("synthetic_observer_start_intent", {"case_id": reviewed["case_id"]}),
        ("retirement_stop_validation_result", receipt["summary"]),
    ]
    lines, tail = [], "0" * 64
    for sequence, (event, value) in enumerate(details, 1):
        line = (
            promoted.encoded(
                {
                    "sequence": sequence,
                    "at": prior.existing.NOW,
                    "previous_sha256": tail,
                    "event": event,
                    "details": value,
                }
            )
            + b"\n"
        )
        lines.append(line)
        tail = promoted.digest(line)
    (directory / "events.jsonl").write_bytes(b"".join(lines))
    receipt.update(events=6, last_event_sha256=tail)
    (directory / "receipt.json").write_bytes(promoted.encoded(receipt))
    for name in ("identity", "receipt", "events"):
        reviewed[name + "_sha256"] = promoted.digest(
            (directory / (name + (".jsonl" if name == "events" else ".json"))).read_bytes()
        )
    plan = probe.descriptor(prior.bindings(), nonce=reviewed["nonce"])
    main = probe._case(plan, "int-executor")["unit"]
    observer = stop.observer_unit(plan, "int-executor")
    states, starts = {}, {}
    for unit, is_observer in ((main, False), (observer, True)):
        launch = (
            stop.observer_command(plan, "int-executor")
            if is_observer
            else validation.launch_command(plan, "int-executor")
        )
        argv = launch[launch.index("--") + 1 :]
        reviewed[("observer" if is_observer else "main") + "_argv_sha256"] = promoted.digest(
            promoted.encoded(argv)
        )
        pid = stop.RETAINED_OBSERVER["exec_main_pid"] if is_observer else reviewed["exec_main_pid"]
        invocation = stop.RETAINED_OBSERVER["invocation_id"] if is_observer else reviewed["invocation_id"]
        value = dict.fromkeys(stop.OBSERVER_PROPERTIES.split(","), "")
        value.update(
            LoadState="loaded",
            ActiveState="active",
            SubState="exited",
            MainPID="0",
            ExecMainPID=str(pid),
            ExecMainCode="1",
            ExecMainStatus="0",
            Result="success",
            InvocationID=invocation,
            ControlGroup="",
            Transient="yes",
            DynamicUser="yes",
            RemainAfterExit="yes",
            PrivateTmp="yes",
            Wants=(main + " " if is_observer else "") + "tmp.mount",
            ExecMainExitTimestampMonotonic="4",
        )
        states[unit] = value
        starts[unit] = {"type": "a(sasbttttuii)", "data": [[argv[0], argv, False, 1, 2, 3, 4, pid, 1, 0]]}
    monkeypatch.setattr(promoted, "RETIREMENT_RETAINED_OBSERVER_START", reviewed)
    monkeypatch.setattr(probe, "CGROUP_ROOT", engine.root / "sys/fs/cgroup")
    original_read = probe._read_regular
    monkeypatch.setattr(
        probe,
        "_read_regular",
        lambda path, limit: (
            (b"36 25 8:1 / / rw - ext4 /dev/public rw\n", None)
            if str(path).endswith("/mountinfo")
            else original_read(path, limit)
        ),
    )
    facts = {"states": states, "starts": starts, "job": {"type": "(uo)", "data": [0, "/"]}}
    original = engine.run

    def run(argv, *, timeout):
        if argv == stop.TMP_MOUNT_COMMAND:
            return "".join(k + "=" + v + "\n" for k, v in stop.ABSENT_TMP_MOUNT.items()).encode()
        for unit in (main, observer):
            if argv == [
                "/usr/bin/systemctl",
                "show",
                "--all",
                "--property=" + stop.OBSERVER_PROPERTIES,
                unit,
            ]:
                return "".join(k + "=" + v + "\n" for k, v in states[unit].items()).encode()
            if argv == stop.job_command(unit):
                return promoted.encoded(facts["job"])
            for key in ("ExecStart", "ExecStop", "ExecStopPost"):
                cmd = stop.job_command(unit)
                cmd[-2:] = ["org.freedesktop.systemd1.Service", key]
                if argv == cmd:
                    return promoted.encoded(starts[unit] if key == "ExecStart" else stop.EMPTY_EXEC_ARRAY)
        return original(argv, timeout=timeout)

    monkeypatch.setattr(engine, "run", run)
    modules = engine.retirement_modules

    @contextmanager
    def capable_modules(*args, **kwargs):
        with modules(*args, **kwargs) as (p, v, facade):
            execute = facade.run_stop_validation

            def admitted(request, *, retained_observer=None, **call):
                # Exercise the actual closed Root runner for fixed old reads,
                # before it binds the different nonce of the new experiment.
                assert retained_observer == stop.retained_observer_state(call["run"], request)
                return execute(request, **call)

            facade.run_stop_validation = admitted
            yield p, v, facade

    monkeypatch.setattr(engine, "retirement_modules", capable_modules)
    return data, directory, facts


def test_exact_six_event_started_observer_failure_is_only_readonly_reconciled(
    prepared, monkeypatch, promoted
):
    data, directory, facts = seed_fourth(prepared, monkeypatch, promoted)
    before = {str(p): p.read_bytes() for p in directory.parent.rglob("*") if p.is_file()}
    assert prepared.engine.execute(data)["legacy_stop"]["passed"] is True
    assert all(Path(name).read_bytes() == raw for name, raw in before.items())
    record = json.loads(
        (directory.parent / prior.ATTEMPT / "retained-observer-reconciliation.json").read_bytes()
    )
    assert record["previous_passed"] is record["previous_cleanup_verified"] is False
    assert record["failed_record_retained"] is record["no_stop_intent_in_complete_chain"] is True
    assert record["observation"]["observer"]["private_tmp_dependency"]["mount_unit"] == stop.ABSENT_TMP_MOUNT
    old_units = set(facts["states"])
    assert not any(
        argv[:2]
        in (
            ["/usr/bin/systemctl", "stop"],
            ["/usr/bin/systemctl", "kill"],
            ["/usr/bin/systemctl", "reset-failed"],
        )
        and any(unit in argv for unit in old_units)
        for argv, _ in prepared.commands
    )


@pytest.mark.parametrize(
    "change",
    [
        "main-job",
        "main-invocation",
        "main-pid",
        "main-wants",
        "observer-wants",
        "propagation",
        "private-tmp",
        "observer-pid",
        "argv",
        "identity-reference",
        "stop-intent",
        "chain",
        "passed",
        "bytes",
    ],
)
def test_unknown_004_state_or_changed_private_history_blocks_new_attempt(
    prepared, monkeypatch, promoted, change
):
    data, directory, facts = seed_fourth(prepared, monkeypatch, promoted)
    main, observer = list(facts["states"])
    if change == "main-job":
        facts["job"] = {"type": "(uo)", "data": [7, "/org/freedesktop/systemd1/job/7"]}
    elif change == "main-invocation":
        facts["states"][main]["InvocationID"] = "f" * 32
    elif change == "main-pid":
        facts["states"][main]["MainPID"] = "123"
    elif change == "main-wants":
        facts["states"][main]["Wants"] += " other.mount"
    elif change == "observer-wants":
        facts["states"][observer]["Wants"] += " other.service"
    elif change == "propagation":
        facts["states"][observer]["PropagatesStopTo"] = "stock-app.service"
    elif change == "private-tmp":
        facts["states"][observer]["PrivateTmp"] = "no"
    elif change == "observer-pid":
        facts["states"][observer]["ExecMainPID"] = "123"
    elif change == "argv":
        facts["starts"][observer]["data"][0][1][-1] = "/usr/bin/false"
    else:
        if change == "identity-reference":
            path = directory / "identity.json"
            value = json.loads(path.read_bytes())
            value["retained_start_reconciliation"]["receipt_sha256"] = "f" * 64
            path.write_bytes(promoted.encoded(value))
        elif change in {"stop-intent", "chain"}:
            path = directory / "events.jsonl"
            rows = path.read_bytes().splitlines(keepends=True)
            value = json.loads(rows[4])
            value["event" if change == "stop-intent" else "previous_sha256"] = (
                "synthetic_stop_intent" if change == "stop-intent" else "f" * 64
            )
            rows[4] = promoted.encoded(value) + b"\n"
            path.write_bytes(b"".join(rows))
        elif change == "passed":
            path = directory / "receipt.json"
            value = json.loads(path.read_bytes())
            value["passed"] = True
            value["summary"]["synthetic_cleanup_verified"] = True
            path.write_bytes(promoted.encoded(value))
        else:
            with (directory / "events.jsonl").open("ab") as stream:
                stream.write(b"\n")
        if change != "bytes":
            for name in ("identity", "events", "receipt"):
                monkeypatch.setitem(
                    promoted.RETIREMENT_RETAINED_OBSERVER_START,
                    name + "_sha256",
                    promoted.digest(
                        (directory / (name + (".jsonl" if name == "events" else ".json"))).read_bytes()
                    ),
                )
    before = {file.name: file.read_bytes() for file in directory.iterdir()}
    with pytest.raises(promoted.ReleaseError, match="retirement_previous_stop_unreconciled"):
        prepared.engine.execute(data)
    assert {file.name: file.read_bytes() for file in directory.iterdir()} == before
    assert not (directory.parent / prior.ATTEMPT).exists()
    assert not any(argv[:2] == ["/usr/bin/systemctl", "stop"] for argv, _ in prepared.commands)


@pytest.mark.parametrize("unit", ["tmp.mount", "other.mount", "stock-app.service"])
def test_absent_mount_stdout_and_nonzero_exception_only_apply_to_fixed_safe_query(
    promoted, monkeypatch, unit
):
    argv = [*stop.TMP_MOUNT_COMMAND[:-1], unit]
    raw = "".join(k + "=" + v + "\n" for k, v in stop.ABSENT_TMP_MOUNT.items()).encode()
    seen = []

    def run(command, **kwargs):
        seen.append(kwargs)
        return types.SimpleNamespace(returncode=4, stdout=raw)

    monkeypatch.setattr(promoted.subprocess, "run", run)
    if unit == "tmp.mount":
        assert promoted.run_command(argv) == raw
    else:
        with pytest.raises(promoted.ReleaseError, match="command_failed"):
            promoted.run_command(argv)
    assert seen[0]["stdout"] == subprocess.PIPE


def test_known_release_cannot_lose_or_skip_fourth_started_failure(prepared, monkeypatch, promoted):
    data, directory, _ = seed_fourth(prepared, monkeypatch, promoted)
    reviewed = dict(promoted.RETIREMENT_RETAINED_OBSERVER_START)
    reviewed.update(commit=prior.existing.COMMIT, release_id=prior.existing.RELEASE)
    monkeypatch.setattr(promoted, "RETIREMENT_RETAINED_OBSERVER_START", reviewed)
    for path in directory.iterdir():
        path.unlink()
    directory.rmdir()
    with pytest.raises(promoted.ReleaseError, match="retirement_previous_stop_unreconciled"):
        prepared.engine.execute(data)
    assert not (directory.parent / prior.ATTEMPT).exists()
    assert not any(argv[:2] == ["/usr/bin/systemctl", "stop"] for argv, _ in prepared.commands)
