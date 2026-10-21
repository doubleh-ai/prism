"""Mac/offline contracts only. These mocks never constitute Linux stop evidence."""

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from scripts import retirement_probe as probe
from scripts import retirement_stop_validation as stop
from scripts import retirement_validation as base

REQUEST = {
    "commit": "a" * 40,
    "release_id": "retirement-stop-test",
    "base_environment_sha256": probe.OLD_ENV_SHA256,
}
PYTHON_SHA = "b" * 64
NONCE = "c" * 32


def controller(**kwargs):
    return stop.StopValidation(
        REQUEST,
        expected_python_sha256=PYTHON_SHA,
        boundary=kwargs.pop("boundary", mock.Mock()),
        run=kwargs.pop("run", mock.Mock(return_value=b"")),
        record=kwargs.pop("record", mock.Mock()),
        **kwargs,
    )


def test_linux_root_is_required_before_any_actuation(monkeypatch):
    monkeypatch.setattr(probe.sys, "platform", "darwin")
    operation = mock.Mock(side_effect=AssertionError("unexpected action"))
    with pytest.raises(probe.RetirementProbeError, match="linux_required"):
        stop.run_stop_validation(
            REQUEST, expected_python_sha256=PYTHON_SHA, boundary=operation, run=operation, record=operation
        )
    operation.assert_not_called()


def test_exact_published_helpers_and_bound_python_are_part_of_descriptor():
    assert probe.sha256(Path(probe.__file__).read_bytes()) == stop.PROBE_SOURCE_SHA256
    assert probe.sha256(Path(base.__file__).read_bytes()) == stop.VALIDATION_SOURCE_SHA256
    value = stop.descriptor(REQUEST, expected_python_sha256=PYTHON_SHA, nonce=NONCE)
    changed = stop.descriptor(REQUEST, expected_python_sha256="d" * 64, nonce=NONCE)
    assert value["binding_sha256"] != changed["binding_sha256"]
    assert value["python_version"] == "3.12.3"
    assert value["cases"] == ["int-executor", "int-post"]
    assert value["production_quiescence_proven"] is False
    assert value["actual_linux_acceptance"] is False


@pytest.mark.parametrize("value", [None, True, "a" * 40, "../other", "A" * 64])
def test_python_binary_identity_cannot_be_an_arbitrary_path_or_missing(value):
    with pytest.raises(base.RetirementValidationError, match="python_digest_rejected"):
        stop.descriptor(REQUEST, expected_python_sha256=value)


@pytest.mark.parametrize("case_id", stop.CASES)
def test_observer_only_holds_a_wants_reference_and_executes_fixed_public_true(case_id):
    value = probe.descriptor(REQUEST, nonce=NONCE)
    case = probe._case(value, case_id)
    command = stop.observer_command(value, case_id)
    props = dict(
        item.removeprefix("--property=").split("=", 1) for item in command if item.startswith("--property=")
    )
    assert props["Wants"] == case["unit"]
    assert not set(props) & {"PartOf", "BindsTo", "PropagatesStopTo"}
    assert props["DynamicUser"] == props["PrivateNetwork"] == props["NoNewPrivileges"] == "yes"
    assert props["TemporaryFileSystem"] == "/run:ro /run/systemd:ro,mode=000 /run/dbus:ro,mode=000"
    assert props["InaccessiblePaths"].split() == [
        "/home",
        "/root",
        "/var/lib",
        "/etc",
        "/opt/prism/releases",
    ]
    assert "ReadWritePaths" not in props
    assert command[command.index("--") + 1 :] == ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "/usr/bin/true"]
    assert "stock-app.service" not in command and "vibe-trading.service" not in command


def test_mutated_case_and_unrelated_units_are_rejected_before_runner():
    runner = mock.Mock()
    value = controller(run=runner)
    with pytest.raises(base.RetirementValidationError, match="host_unit_rejected"):
        value.show("vibe-trading.service")
    mutated = dict(value.plan["cases"][0], unit="stock-app.service")
    with pytest.raises(base.RetirementValidationError, match="descriptor_changed"):
        value.case(mutated)
    with pytest.raises(base.RetirementValidationError, match="descriptor_changed"):
        value.release_observer(mutated)
    runner.assert_not_called()


@pytest.mark.parametrize(
    "raw,code",
    [
        (
            b"prism-retirement-stop-ref-"
            + NONCE.encode()
            + b"-int-executor.service loaded active exited private-description\n",
            "previous_observer_unreconciled",
        ),
        (b"stock-app.service loaded active running private-description\n", "observer_inventory_rejected"),
        (b"\xff", "observer_inventory_rejected"),
    ],
)
def test_old_empty_observers_and_unrecognized_inventory_block_without_actuation(raw, code):
    runner = mock.Mock(return_value=raw)
    value = controller(run=runner)
    with pytest.raises(base.RetirementValidationError, match=code):
        value.no_existing_observers()
    runner.assert_called_once_with(stop.OBSERVER_INVENTORY, timeout=10)


@pytest.mark.parametrize("digest,version", [("d" * 64, b"3.12.3\n"), (PYTHON_SHA, b"3.12.4\n")])
def test_different_python_bytes_or_patch_version_cannot_certify_stop(monkeypatch, digest, version):
    value = controller(run=mock.Mock(return_value=version))
    monkeypatch.setattr(base, "read_data", lambda *a, **k: (b"trusted", SimpleNamespace(st_mode=0o755)))
    monkeypatch.setattr(probe, "sha256", lambda _: digest)
    with pytest.raises(base.RetirementValidationError, match="python_(binary|version)_changed"):
        value.python_identity()


def test_system_python_may_be_root_writable_but_not_group_writable(monkeypatch):
    value = controller(run=mock.Mock(return_value=b"3.12.3\n"))
    monkeypatch.setattr(base, "read_data", lambda *a, **k: (b"trusted", SimpleNamespace(st_mode=0o755)))
    monkeypatch.setattr(probe, "sha256", lambda _: PYTHON_SHA)
    assert value.python_identity() == PYTHON_SHA
    monkeypatch.setattr(base, "read_data", lambda *a, **k: (b"trusted", SimpleNamespace(st_mode=0o775)))
    with pytest.raises(base.RetirementValidationError, match="python_binary_changed"):
        value.python_identity()


@pytest.mark.parametrize(
    "field,value",
    [
        ("KillSignal", "15"),
        ("SendSIGKILL", "yes"),
        ("KillMode", "control-group"),
        ("TimeoutStopUSec", "90s"),
        ("ExecStop", "arbitrary command"),
        ("WatchdogUSec", "1min"),
    ],
)
def test_actual_systemd_policy_is_required_not_the_launch_command(monkeypatch, field, value):
    obj = controller()
    case = obj.plan["cases"][0]
    state = {**stop.POLICY, "Job": "0", field: value}
    monkeypatch.setattr(obj, "show", lambda _: state)
    with pytest.raises(base.RetirementValidationError, match="actual_policy_rejected"):
        obj.policy(case)


def test_stop_job_must_be_present_during_executor_join_and_absent_before_dispatch(monkeypatch):
    obj = controller()
    case = obj.plan["cases"][0]
    state = {**stop.POLICY, "Job": "0"}
    monkeypatch.setattr(obj, "show", lambda _: state)
    assert obj.policy(case) == state
    with pytest.raises(base.RetirementValidationError, match="job_state_rejected"):
        obj.policy(case, waiting=True)
    state["Job"] = "123"
    assert obj.policy(case, waiting=True) == state
    with pytest.raises(base.RetirementValidationError, match="job_state_rejected"):
        obj.policy(case)


def case_harness(tmp_path, monkeypatch, case_id, *, delivery_error=False, deny_dispatch=False):
    monkeypatch.setattr(probe, "RUNTIME_ROOT", tmp_path.resolve())
    monkeypatch.setattr(probe.secrets, "token_hex", lambda _: NONCE)
    actions = []
    phase = {"exit": False}

    def runner(command, **_):
        actions.append(("command", command))
        if command[1:2] == ["stop"] and delivery_error:
            raise RuntimeError("private command error")
        return b""

    def boundary(label):
        actions.append(("boundary", label))
        if label.startswith("retirement_stop_dispatch_") and deny_dispatch:
            raise RuntimeError("operator stop")

    obj = controller(boundary=boundary, run=runner, record=lambda *args: actions.append(("record", args)))
    case = probe._case(obj.plan, case_id)
    live = {"pid": 123, "uid": 4321, "start_ticks": 456, "invocation": "e" * 32}
    monkeypatch.setattr(obj, "check_production", lambda: None)
    monkeypatch.setattr(obj, "python_identity", lambda: PYTHON_SHA)
    monkeypatch.setattr(obj, "_ready", lambda _: live)
    monkeypatch.setattr(obj, "live", lambda _: live)
    monkeypatch.setattr(obj, "policy", lambda *a, **k: {})
    monkeypatch.setattr(obj, "show", lambda _: {"LoadState": "not-found"})
    monkeypatch.setattr(obj, "observer_show", lambda _: {"LoadState": "not-found"})
    monkeypatch.setattr(obj, "observer_ready", lambda _: "f" * 32)
    monkeypatch.setattr(obj, "same_process_waiting", lambda *a: actions.append(("waiting", case_id)) or True)
    monkeypatch.setattr(
        obj, "release_observer", lambda _: actions.append(("release_observer", case_id)) or True
    )
    monkeypatch.setattr(base, "_request_fixture", lambda *a: actions.append(("request", case_id)))
    monkeypatch.setattr(base, "_deadline", lambda _: False)
    monkeypatch.setattr(base, "_open_gate", lambda *a: actions.append(("gate", case_id)))

    def markers(*_):
        return {
            "fixture_ready": True,
            "stream_finished": True,
            case["start_phase_marker"]: True,
            "analysis_saved": phase["exit"] or case_id == "int-post",
            "post_saved": phase["exit"],
            "executor_finished": phase["exit"],
        }

    monkeypatch.setattr(probe, "read_markers", markers)

    def stopped(*_):
        phase["exit"] = True
        actions.append(("exit", case_id))
        return {"ExecMainExitTimestampMonotonic": "999999999999"}, {"member_count": 0}

    monkeypatch.setattr(obj, "stopped", stopped)
    return obj, case, actions


@pytest.mark.parametrize("case_id", stop.CASES)
def test_one_stop_is_durably_committed_then_waits_for_runner_join_before_opening_gate(
    tmp_path, monkeypatch, case_id
):
    obj, case, actions = case_harness(tmp_path, monkeypatch, case_id)
    result = obj.case(case)
    assert result["passed"] is True and result["production_quiescence_proven"] is False
    stop_command = ["/usr/bin/systemctl", "stop", "--no-block", case["unit"]]
    commands = [v for k, v in actions if k == "command"]
    assert [c for c in commands if c[1:2] == ["stop"]] == [stop_command]
    assert not [c for c in commands if c[1:2] in (["kill"], ["restart"], ["reset-failed"])]
    commit = next(
        i for i, (k, v) in enumerate(actions) if k == "record" and v[0] == "synthetic_stop_committed"
    )
    dispatch_boundary = actions.index(("boundary", "retirement_stop_dispatch_" + case_id))
    intent = next(i for i, (k, v) in enumerate(actions) if k == "record" and v[0] == "synthetic_stop_intent")
    assert intent < dispatch_boundary < commit < actions.index(("command", stop_command))
    assert (
        actions.index(("waiting", case_id))
        < actions.index(("gate", case_id))
        < actions.index(("exit", case_id))
        < actions.index(("release_observer", case_id))
    )
    with pytest.raises(base.RetirementValidationError, match="already_consumed"):
        obj.case(case)
    assert len([v for k, v in actions if k == "command" and v[1:2] == ["stop"]]) == 1


@pytest.mark.parametrize("denied", [False, True])
def test_uncertain_stop_or_operator_denial_never_retries_or_force_kills(tmp_path, monkeypatch, denied):
    obj, case, actions = case_harness(
        tmp_path, monkeypatch, "int-executor", delivery_error=not denied, deny_dispatch=denied
    )
    with pytest.raises(RuntimeError):
        obj.case(case)
    if not denied:
        with pytest.raises(base.RetirementValidationError, match="already_consumed"):
            obj.case(case)
    monkeypatch.setattr(obj, "_namespace_failed_no_work", lambda _: False)
    monkeypatch.setattr(obj, "show", lambda _: {"LoadState": "loaded", "MainPID": "123"})
    ticks = iter([0, 0, base.CLEANUP_SECONDS + 1])
    monkeypatch.setattr(obj, "monotonic", lambda: next(ticks))
    assert obj.cleanup() is False
    assert ("gate", "int-executor") in actions
    assert len([v for k, v in actions if k == "command" and v[1:2] == ["stop"]]) == (0 if denied else 1)


def test_empty_observer_can_only_be_released_after_fixture_has_no_processes(monkeypatch):
    obj = controller()
    case = obj.plan["cases"][0]
    obj.observers[case["case_id"]] = "f" * 32
    obj.case_identities[case["case_id"]] = {"invocation": "e" * 32, "pid": 123}
    monkeypatch.setattr(
        obj, "show", lambda _: {"MainPID": "123", "InvocationID": "e" * 32, "ExecMainPID": "123"}
    )
    assert obj.release_observer(case) is False
    obj.run.assert_not_called()
    monkeypatch.setattr(
        obj, "show", lambda _: {"MainPID": "0", "InvocationID": "e" * 32, "ExecMainPID": "123"}
    )
    monkeypatch.setattr(obj, "_empty", lambda _: None)
    assert obj.release_observer(case) is False
    obj.run.assert_not_called()


def test_no_direct_signals_or_production_stop_code_path_exists():
    tree = ast.parse(Path(stop.__file__).read_text())
    called = {
        n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert not called & {"kill", "terminate", "send_signal", "system", "popen"}
    literals = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and type(n.value) is str}
    assert "--signal=" not in literals and "restart" not in literals and "reset-failed" not in literals


def test_preexisting_synthetic_unit_is_never_treated_as_our_attempt(tmp_path, monkeypatch):
    obj, case, actions = case_harness(tmp_path, monkeypatch, "int-executor")
    monkeypatch.setattr(obj, "show", lambda _: {"LoadState": "loaded"})
    with pytest.raises(base.RetirementValidationError, match="unit_already_exists"):
        obj.case(case)
    assert obj.active_case is None
    assert not [v for k, v in actions if k == "command"]


def observer_state(obj, case):
    result = dict.fromkeys(stop.OBSERVER_PROPERTIES.split(","), "")
    result.update(
        LoadState="loaded",
        Transient="yes",
        ActiveState="active",
        SubState="exited",
        MainPID="0",
        ExecMainCode="1",
        ExecMainStatus="0",
        Result="success",
        Wants=case["unit"],
        DynamicUser="yes",
        RemainAfterExit="yes",
        InvocationID="f" * 32,
    )
    return result


@pytest.mark.parametrize(
    "field,value",
    [
        ("Wants", "stock-app.service"),
        ("PartOf", "stock-app.service"),
        ("BindsTo", "other.service"),
        ("PropagatesStopTo", "other.service"),
        ("MainPID", "123"),
        ("ExecMainStatus", "1"),
        ("DynamicUser", "no"),
        ("InvocationID", ""),
        ("Transient", "no"),
    ],
)
def test_observer_actual_reference_and_nonpropagation_are_mandatory(monkeypatch, field, value):
    obj = controller()
    case = obj.plan["cases"][0]
    state = observer_state(obj, case)
    state[field] = value
    monkeypatch.setattr(obj, "observer_show", lambda _: state)
    monkeypatch.setattr(obj, "observer_empty", lambda *_: True)
    with pytest.raises(base.RetirementValidationError, match="observer_reference_rejected"):
        obj.observer_ready(case)
    obj.run.assert_not_called()


@pytest.mark.parametrize(
    "change", ["invocation", "pid", "phase", "job", "start_ticks", "uid", "private_net", "post_saved"]
)
def test_runner_join_requires_real_stop_job_same_original_process_and_unsaved_work(monkeypatch, change):
    obj = controller()
    case = obj.plan["cases"][0]
    live = {"pid": 123, "uid": 4321, "start_ticks": 456, "invocation": "e" * 32}
    obj.observers[case["case_id"]] = "f" * 32
    state = {
        **stop.POLICY,
        "Job": "1",
        "LoadState": "loaded",
        "Transient": "yes",
        "InvocationID": live["invocation"],
        "MainPID": "123",
        "ExecMainPID": "123",
        "ControlGroup": case["control_group"],
        "ActiveState": "deactivating",
        "SubState": "stop-sigterm",
    }
    identity = {"start_ticks": 456, "uid_isolated": True, "private_network": True, "cgroup_bound": True}
    if change == "invocation":
        state["InvocationID"] = "d" * 32
    if change == "pid":
        state["MainPID"] = "999"
    if change == "phase":
        state["ActiveState"] = "active"
    if change == "job":
        state["Job"] = "0"
    if change == "start_ticks":
        identity["start_ticks"] = 999
    if change == "private_net":
        identity["private_network"] = False
    monkeypatch.setattr(obj, "show", lambda _: state)
    monkeypatch.setattr(base, "server_finished", lambda *_: True)
    monkeypatch.setattr(probe, "linux_process_identity", lambda *_: identity)
    monkeypatch.setattr(base, "_uids", lambda _: 999 if change == "uid" else 4321)
    monkeypatch.setattr(probe, "read_markers", lambda *_: {"post_saved": change == "post_saved"})
    monkeypatch.setattr(base, "_deadline", lambda _: False)
    monkeypatch.setattr(obj, "observer_ready", lambda _: "f" * 32)
    with pytest.raises(base.RetirementValidationError):
        obj.same_process_waiting(case, live, 1)
    obj.run.assert_not_called()


def test_clean_zero_without_durable_post_marker_does_not_pass(tmp_path, monkeypatch):
    obj, case, _ = case_harness(tmp_path, monkeypatch, "int-post")
    monkeypatch.setattr(
        probe,
        "read_markers",
        lambda *_: {
            "fixture_ready": True,
            "stream_finished": True,
            "post_started": True,
            "analysis_saved": True,
            "post_saved": False,
            "executor_finished": False,
        },
    )
    with pytest.raises(base.RetirementValidationError, match="durable_markers_unverified"):
        obj.case(case)
