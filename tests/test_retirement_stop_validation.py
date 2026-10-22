"""Mac/offline contracts only. These mocks never constitute Linux stop evidence."""

import ast
import json
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
    obj.exec_arrays[case_id] = {key: stop.EMPTY_EXEC_ARRAY for key in ("ExecStop", "ExecStopPost")}
    monkeypatch.setattr(obj, "unit_missing", lambda _: True)
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
    monkeypatch.setattr(obj, "unit_missing", lambda _: False)
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
    with pytest.raises(base.RetirementValidationError, match="observer_(reference|dependency)_rejected"):
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


@pytest.mark.parametrize("observer", [False, True])
def test_show_requires_actual_typed_arrays_instead_of_omitted_systemctl_structs(observer):
    value = controller()
    case = probe._case(value.plan, "int-executor")
    properties = stop.OBSERVER_PROPERTIES if observer else stop.PROPERTIES
    rows = dict.fromkeys(properties.split(","), "")
    rows["LoadState"] = "loaded"
    calls = []

    def runner(argv, *, timeout):
        calls.append(argv)
        if argv[0] == "/usr/bin/busctl":
            return json.dumps(
                {"type": "(uo)", "data": [0, "/"]} if argv[-1] == "Job" else stop.EMPTY_EXEC_ARRAY
            ).encode()
        assert argv[:3] == ["/usr/bin/systemctl", "show", "--all"]
        assert argv[3] == "--property=" + properties and timeout == 10
        return "".join(key + "=" + item + "\n" for key, item in rows.items()).encode()

    value.run = runner
    actual = value.observer_show(case) if observer else value.show(case["unit"])
    assert set(actual) == set(properties.split(",")) | (set() if observer else {"ExecStop", "ExecStopPost"})
    assert actual["Job"] == "0"
    if not observer:
        assert actual["ExecStop"] == actual["ExecStopPost"] == ""
        assert len(calls) == 4 and calls[1:] == stop.exec_array_commands(case["unit"]) + [
            stop.job_command(case["unit"])
        ]
        rows.pop("KillSignal")
        with pytest.raises(base.RetirementValidationError, match="systemd_properties_rejected"):
            value.show(case["unit"])


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b'{"type":"a(sasbttttuii)","data":[[]]}',
        b'{"type":"as","data":[]}',
        b'{"type":"a(sasbttttuii)","data":[["public-command"]]}',
        b'{"type":"a(sasbttttuii)","data":[],"extra":true}',
        b'{"type":"a(sasbttttuii)","data":[1],"data":[]}',
        b"not json",
    ],
)
def test_typed_exec_arrays_require_actual_exact_empty_data(raw):
    obj = controller()
    case = probe._case(obj.plan, "int-executor")
    with pytest.raises(base.RetirementValidationError, match="exec_array_rejected"):
        stop.empty_exec_arrays(lambda *args, **kwargs: raw, case["unit"])


def test_prestart_notfound_uses_only_loadstate_without_asking_inexistent_bus_object():
    obj = controller()
    case = probe._case(obj.plan, "int-executor")
    calls = []
    rows = {"LoadState": "not-found"}

    def run(argv, *, timeout):
        calls.append(argv)
        assert argv == ["/usr/bin/systemctl", "show", "--property=LoadState", case["unit"]]
        return "".join(k + "=" + v + "\n" for k, v in rows.items()).encode()

    obj.run = run
    assert obj.unit_missing(case["unit"]) is True and len(calls) == 1
    rows.pop("LoadState")
    with pytest.raises(base.RetirementValidationError, match="systemd_properties_rejected"):
        obj.unit_missing(case["unit"])


def test_bus_property_query_cannot_target_production_or_another_case():
    for unit in (
        "stock-app.service",
        "vibe-trading.service",
        "prism-retirement-probe-" + NONCE + "-int-child.service",
    ):
        with pytest.raises(base.RetirementValidationError):
            stop.exec_array_commands(unit)


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"type": "uo", "data": [0, "/"]},
        {"type": "(uo)", "data": []},
        {"type": "(uo)", "data": [False, "/"]},
        {"type": "(uo)", "data": [-1, "/"]},
        {"type": "(uo)", "data": [2**32, "/"]},
        {"type": "(uo)", "data": ["0", "/"]},
        {"type": "(uo)", "data": [0, "/org/freedesktop/systemd1/job/0"]},
        {"type": "(uo)", "data": [7, "/org/freedesktop/systemd1/job/8"]},
        {"type": "(uo)", "data": [7, "/"]},
        {"type": "(uo)", "data": [0, "/"], "extra": True},
    ],
)
def test_typed_job_rejects_bad_uint32_paths_and_shapes(value):
    with pytest.raises(base.RetirementValidationError, match="job_property_rejected"):
        stop.validate_job(value)


@pytest.mark.parametrize("printed", ["", "0"])
def test_empty_printed_job_is_zero_only_after_actual_typed_read(printed):
    calls = []
    unit = "prism-retirement-probe-" + NONCE + "-int-executor.service"

    def run(argv, *, timeout):
        calls.append(argv)
        assert argv == stop.job_command(unit) and timeout == 10
        return b'{"type":"(uo)","data":[0,"/"]}'

    assert stop.typed_job(run, unit, printed) == {"type": "(uo)", "data": [0, "/"]}
    assert len(calls) == 1


@pytest.mark.parametrize(
    "raw", [b"", b"not json", b'{"type":"(uo)","data":[7,"/org/freedesktop/systemd1/job/7"],"data":[0,"/"]}']
)
def test_missing_or_duplicate_typed_job_is_not_empty_job(raw):
    with pytest.raises(base.RetirementValidationError, match="job_property_rejected"):
        stop.typed_job(
            lambda *args, **kwargs: raw, "prism-retirement-probe-" + NONCE + "-int-executor.service", ""
        )


def test_nonempty_show_job_must_match_actual_typed_value():
    with pytest.raises(base.RetirementValidationError, match="job_property_rejected"):
        stop.typed_job(
            lambda *args, **kwargs: b'{"type":"(uo)","data":[0,"/"]}',
            "prism-retirement-probe-" + NONCE + "-int-executor.service",
            "7",
        )
    actual = {"type": "(uo)", "data": [7, "/org/freedesktop/systemd1/job/7"]}
    assert stop.validate_job(actual, "7") == actual
    assert stop.validate_job(actual, "") == actual


@pytest.mark.parametrize(
    "wants", ["MAIN tmp.mount extra.service", "MAIN tmp.mount tmp.mount", "tmp.mount", "MAIN other.mount"]
)
def test_observer_extra_dependencies_are_finite_not_generic_mount_allowlist(monkeypatch, wants):
    main = "prism-retirement-probe-" + NONCE + "-int-executor.service"
    state = {"Wants": wants.replace("MAIN", main), "PrivateTmp": "yes"}
    called = []
    with pytest.raises(base.RetirementValidationError, match="observer_dependency_rejected"):
        stop.observer_dependency(lambda *a, **k: called.append(a), state, main)
    assert not called


@pytest.mark.parametrize(
    "change", [None, "private-tmp", "mounted", "missing-root", "tmp-mounted", "duplicate-property"]
)
def test_only_actual_private_tmp_weak_want_and_absent_host_mount_are_accepted(monkeypatch, change):
    main = "prism-retirement-probe-" + NONCE + "-int-executor.service"
    state = {"Wants": main + " tmp.mount", "PrivateTmp": "no" if change == "private-tmp" else "yes"}
    mount = dict(stop.ABSENT_TMP_MOUNT)
    if change == "mounted":
        mount.update(LoadState="loaded", ActiveState="active", SubState="mounted", Where="/tmp")
    raw = "".join(k + "=" + v + "\n" for k, v in mount.items()).encode()
    if change == "duplicate-property":
        raw += b"Where=\n"
    kernel = b"36 25 8:1 / / rw - ext4 /dev/public rw\n"
    if change == "missing-root":
        kernel = b""
    if change == "tmp-mounted":
        kernel += b"37 36 0:2 / /tmp rw - tmpfs tmpfs rw\n"
    monkeypatch.setattr(probe, "_read_regular", lambda *args: (kernel, None))
    calls = []

    def run(argv, *, timeout):
        calls.append(argv)
        assert argv == stop.TMP_MOUNT_COMMAND and timeout == 10
        return raw

    if change is None:
        proof = stop.observer_dependency(run, state, main)
        assert stop.validate_observer_dependency(state, main, proof) == proof
        assert proof["mount_unit"] == stop.ABSENT_TMP_MOUNT and proof["kernel_tmp_mount_count"] == 0
    else:
        with pytest.raises(base.RetirementValidationError, match="observer_dependency_rejected"):
            stop.observer_dependency(run, state, main)


def test_single_expected_want_needs_no_mount_exception_or_host_read(monkeypatch):
    main = "prism-retirement-probe-" + NONCE + "-int-executor.service"
    assert (
        stop.observer_dependency(
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()), {"Wants": main}, main
        )
        is None
    )


@pytest.mark.parametrize(
    "change", [None, "no-root-witness", "witness-drift", "extra-observer", "active-job", "argv", "cgroup"]
)
def test_only_independently_rechecked_004_empty_observer_can_remain_in_inventory(
    tmp_path, monkeypatch, change
):
    plan = probe.descriptor(REQUEST, nonce=stop.RETAINED_OBSERVER["nonce"])
    unit = stop.observer_unit(plan, "int-executor")
    state = dict.fromkeys(stop.OBSERVER_PROPERTIES.split(","), "")
    state.update(
        LoadState="loaded",
        ActiveState="active",
        SubState="exited",
        MainPID="0",
        ExecMainPID=str(stop.RETAINED_OBSERVER["exec_main_pid"]),
        ExecMainCode="1",
        ExecMainStatus="0",
        Result="success",
        InvocationID=stop.RETAINED_OBSERVER["invocation_id"],
        Transient="yes",
        DynamicUser="yes",
        RemainAfterExit="yes",
        PrivateTmp="yes",
        Wants=probe._case(plan, "int-executor")["unit"] + " tmp.mount",
        ExecMainExitTimestampMonotonic="4",
    )
    commands = stop.retained_observer_read_commands(REQUEST)
    launch = stop.observer_command(plan, "int-executor")
    argv = launch[launch.index("--") + 1 :]
    start = {
        "type": "a(sasbttttuii)",
        "data": [[argv[0], argv, False, 1, 2, 3, 4, stop.RETAINED_OBSERVER["exec_main_pid"], 1, 0]],
    }
    facts = {"job": {"type": "(uo)", "data": [0, "/"]}}
    monkeypatch.setattr(probe, "CGROUP_ROOT", tmp_path / "cgroup")
    monkeypatch.setattr(
        probe, "_read_regular", lambda *args: (b"36 25 8:1 / / rw - ext4 /dev/public rw\n", None)
    )

    def run(command, *, timeout):
        if command == stop.OBSERVER_INVENTORY:
            return (
                unit
                + " loaded active exited public\n"
                + (
                    "prism-retirement-stop-ref-" + NONCE + "-int-post.service loaded active exited public\n"
                    if change == "extra-observer"
                    else ""
                )
            ).encode()
        if command == commands[1]:
            return "".join(k + "=" + v + "\n" for k, v in state.items()).encode()
        if command == commands[2]:
            return probe.encoded(facts["job"])
        if command == commands[3]:
            return probe.encoded(start)
        if command == stop.TMP_MOUNT_COMMAND:
            return "".join(k + "=" + v + "\n" for k, v in stop.ABSENT_TMP_MOUNT.items()).encode()
        raise AssertionError("no_other_command_allowed")

    witness = stop.retained_observer_state(run, REQUEST)
    if change == "witness-drift":
        witness["state_sha256"] = "f" * 64
    if change == "active-job":
        facts["job"] = {"type": "(uo)", "data": [7, "/org/freedesktop/systemd1/job/7"]}
    if change == "argv":
        start["data"][0][1][-1] = "/usr/bin/false"
    if change == "cgroup":
        (probe.CGROUP_ROOT / "system.slice" / unit).mkdir(parents=True)
    value = controller(run=run, retained_observer=None if change == "no-root-witness" else witness)
    if change is None:
        assert value.no_existing_observers() is True
    else:
        with pytest.raises(base.RetirementValidationError):
            value.no_existing_observers()
