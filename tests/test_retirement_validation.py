"""Mac/offline contracts only: mock observations never certify Linux isolation."""

import ast
import json
import os
import stat
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from scripts import retirement_probe as probe
from scripts import retirement_validation as validation

REQUEST = {
    "commit": "a" * 40,
    "release_id": "retirement-test-002",
    "base_environment_sha256": probe.OLD_ENV_SHA256,
}
NONCE = "c" * 32


def plan():
    return probe.descriptor(REQUEST, nonce=NONCE)


def test_entry_requires_actual_linux_root_before_any_dispatched_operation(monkeypatch):
    monkeypatch.setattr(probe.sys, "platform", "darwin")
    operation = mock.Mock(side_effect=AssertionError("no operation authorized"))
    with pytest.raises(probe.RetirementProbeError, match="linux_required"):
        validation.run_validation(REQUEST, boundary=operation, run=operation, record=operation)
    operation.assert_not_called()


def test_frozen_probe_and_wrapper_are_bound_without_old_startup_hooks():
    assert probe.sha256(Path(probe.__file__).read_bytes()) == validation.PROBE_SOURCE_SHA256
    source = validation.wrapper_source()
    ast.parse(source)
    assert probe.bundle_identity().encode() in source
    assert probe.sha256(probe.FIXTURE_SOURCE.encode()).encode() in source
    assert b"call_later(45,natural_deadline)" in source
    assert b"server.should_exit=True" in source
    assert b"uvicorn.Server.serve=bounded_serve" in source
    tree = ast.parse(source)
    names = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not names & {"kill", "terminate", "system", "popen", "send_signal"}
    assert all(
        isinstance(node.func.value, ast.Name) and node.func.value.id == "timer"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "cancel"
    )
    # Cancelling only the event-loop timer handle is allowed, never work.
    assert source.count(b"timer.cancel()") == 1


@pytest.mark.parametrize("case_id", [case[0] for case in probe.CASES])
def test_launch_only_targets_one_fixed_synthetic_unit_with_a_private_run(case_id):
    value = plan()
    case = probe._case(value, case_id)
    command = validation.launch_command(value, case_id)
    assert command[:2] == ["/usr/bin/systemd-run", "--quiet"]
    assert "--unit=" + case["unit"] in command
    assert "stock-app.service" not in " ".join(command)
    properties = {
        item[len("--property=") :].split("=", 1)[0]: item.split("=", 2)[2]
        for item in command
        if item.startswith("--property=")
    }
    assert properties["DynamicUser"] == "yes"
    assert properties["RemainAfterExit"] == "yes"
    assert properties["TemporaryFileSystem"] == "/run:ro /run/systemd:ro,mode=000 /run/dbus:ro,mode=000"
    assert properties["BindPaths"] == case["host_runtime_directory"] + ":" + case["runtime_directory"]
    assert properties["ReadWritePaths"] == case["runtime_directory"]
    assert properties["SendSIGKILL"] == "no"
    assert properties["KillMode"] == "process"
    assert properties["TimeoutStopSec"] == "infinity"
    assert properties["PrivateNetwork"] == properties["NoNewPrivileges"] == "yes"
    assert properties["ProtectProc"] == "invisible"
    assert properties["RestrictAddressFamilies"] == "AF_UNIX"
    assert properties["InaccessiblePaths"].split() == [
        "/home",
        "/root",
        "/var/lib",
        "/etc",
        "/opt/prism/releases",
    ]
    # No mandatory child path can fail merely because production has not
    # created it yet; the existing ancestors also conceal future children.
    assert "/etc/prism" not in properties["InaccessiblePaths"].split()
    assert str(validation.wrapper_directory() / "wrapper.py") in command


def test_descriptor_mutation_cannot_select_another_service_or_path():
    value = plan()
    value["cases"][0]["unit"] = "stock-app.service"
    with pytest.raises(probe.RetirementProbeError, match="descriptor_changed"):
        validation.launch_command(value, "int-executor")


def test_only_the_fixed_interpreter_wrapper_and_case_arguments_can_match_a_live_process():
    value = plan()
    raw = validation._expected_argv(value, "int-post")
    assert raw.startswith(b"/usr/bin/python3\0-I\0-S\0-B\0")
    assert raw.endswith(
        b"\0serve\0post_complete\0"
        + NONCE.encode()
        + b"\0"
        + value["binding_sha256"].encode()
        + b"\0"
        + value["bundle_sha256"].encode()
        + b"\0"
    )
    assert b"/home/ubuntu/stock-venv/bin" not in raw


def test_prior_synthetic_descendants_block_a_new_run_without_sending_signals(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    monkeypatch.setattr(probe, "_require_linux", lambda: None)
    monkeypatch.setattr(probe, "CGROUP_ROOT", root)
    (root / "cgroup.controllers").write_text("")
    group = root / "system.slice" / ("prism-retirement-probe-" + "d" * 32 + "-int-child.service")
    group.mkdir(parents=True)
    (group / "cgroup.procs").write_text("")
    child = group / "nested"
    child.mkdir()
    (child / "cgroup.procs").write_text("123\n")
    with pytest.raises(validation.RetirementValidationError, match="previous_workload_active"):
        validation.no_existing_synthetic_workloads(plan())
    (child / "cgroup.procs").write_text("")
    assert validation.no_existing_synthetic_workloads(plan()) is True


def test_data_copy_reader_rejects_links_in_every_ancestor_and_hardlinks(tmp_path):
    root = tmp_path.resolve()
    folder = root / "actual"
    folder.mkdir()
    source = folder / "source.py"
    source.write_bytes(b"public bytes")
    data, _ = validation.read_data(source)
    assert data == b"public bytes"
    (root / "alias").symlink_to(folder, target_is_directory=True)
    with pytest.raises(OSError):
        validation.read_data(root / "alias/source.py")
    (folder / "linked.py").symlink_to(source)
    with pytest.raises(OSError):
        validation.read_data(folder / "linked.py")
    os.link(source, folder / "hardlinked.py")
    with pytest.raises(validation.RetirementValidationError, match="data_file_rejected"):
        validation.read_data(source)


def test_data_copy_reader_binds_content_to_stable_stat_and_size(tmp_path, monkeypatch):
    source = tmp_path.resolve() / "source.py"
    source.write_bytes(b"public")
    with pytest.raises(validation.RetirementValidationError, match="data_file_rejected"):
        validation.read_data(source, maximum=2)
    original = validation._stable
    calls = 0

    def changed(info):
        nonlocal calls
        calls += 1
        return (*original(info), calls)

    monkeypatch.setattr(validation, "_stable", changed)
    with pytest.raises(validation.RetirementValidationError, match="data_changed"):
        validation.read_data(source)


def isolation_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(
        validation, "_host_runtime", lambda plan, cid: Path(probe._case(plan, cid)["host_runtime_directory"])
    )
    monkeypatch.setattr(probe, "RUNTIME_ROOT", tmp_path.resolve())
    monkeypatch.setattr(validation.pwd, "getpwnam", mock.Mock(side_effect=KeyError))
    value = plan()
    case = value["cases"][0]
    root = Path(case["host_runtime_directory"])
    root.mkdir(parents=True, mode=0o700)
    data = {
        "nonce": NONCE,
        "binding_sha256": value["binding_sha256"],
        "case_profile": case["profile"],
        "at_ns": 1,
        "uid_nonroot": True,
        "production_paths_hidden": True,
        "inet_sockets_denied": True,
        "run_readonly": True,
        "runtime_endpoints_hidden": True,
    }
    marker = root / "validation_isolation.json"
    marker.write_text(json.dumps(data))
    marker.chmod(0o600)
    return value, case, marker, data


@pytest.mark.parametrize(
    "field",
    [
        "production_paths_hidden",
        "inet_sockets_denied",
        "run_readonly",
        "runtime_endpoints_hidden",
        "uid_nonroot",
    ],
)
def test_flags_never_replace_actual_nonroot_isolation_witness(tmp_path, monkeypatch, field):
    value, case, marker, data = isolation_fixture(tmp_path, monkeypatch)
    assert validation._isolation(value, case["case_id"]) == os.geteuid()
    data[field] = False
    marker.write_text(json.dumps(data))
    with pytest.raises(validation.RetirementValidationError, match="actual_isolation_rejected"):
        validation._isolation(value, case["case_id"])


def test_isolation_witness_binds_nonce_and_mode_and_gate_never_follows_links(tmp_path, monkeypatch):
    value, case, marker, data = isolation_fixture(tmp_path, monkeypatch)
    data["nonce"] = "d" * 32
    marker.write_text(json.dumps(data))
    with pytest.raises(validation.RetirementValidationError):
        validation._isolation(value, case["case_id"])
    data["nonce"] = NONCE
    marker.write_text(json.dumps(data))
    marker.chmod(0o644)
    with pytest.raises(validation.RetirementValidationError):
        validation._isolation(value, case["case_id"])
    marker.chmod(0o600)
    gate = marker.parent / "release-work"
    gate.symlink_to(marker.parent / "outside")
    with pytest.raises(validation.RetirementValidationError, match="gate_rejected"):
        validation._open_gate(value, case["case_id"])
    assert not (marker.parent / "outside").exists()


def test_host_unit_rejection_happens_before_runner_dispatch():
    runner = mock.Mock()
    controller = validation.Validation(REQUEST, boundary=mock.Mock(), run=runner, record=mock.Mock())
    with pytest.raises(validation.RetirementValidationError, match="host_unit_rejected"):
        controller.show("vibe-trading.service")
    runner.assert_not_called()


def test_mutated_case_cannot_signal_or_stop_production_even_through_internal_methods():
    runner = mock.Mock()
    controller = validation.Validation(REQUEST, boundary=mock.Mock(), run=runner, record=mock.Mock())
    case = dict(controller.plan["cases"][0], unit="stock-app.service")
    with pytest.raises(validation.RetirementValidationError, match="descriptor_changed"):
        controller.case(case)
    with pytest.raises(validation.RetirementValidationError, match="descriptor_changed"):
        controller._retire_empty_unit(case, "a" * 32)
    runner.assert_not_called()


def case_harness(tmp_path, monkeypatch, case_id, *, dispatch_error=False, deny_signal=False):
    monkeypatch.setattr(
        validation, "_host_runtime", lambda plan, cid: Path(probe._case(plan, cid)["host_runtime_directory"])
    )
    monkeypatch.setattr(probe, "RUNTIME_ROOT", tmp_path.resolve())
    monkeypatch.setattr(probe.secrets, "token_hex", lambda _: NONCE)
    actions = []
    phase = {"exited": False}

    def runner(command, **_):
        actions.append(("command", command))
        if command[1:2] == ["kill"] and dispatch_error:
            raise RuntimeError("sensitive command detail must not escape")
        return b""

    def boundary(label):
        actions.append(("boundary", label))
        if label.startswith("retirement_signal_") and deny_signal:
            raise RuntimeError("operator stop")

    controller = validation.Validation(
        REQUEST, boundary=boundary, run=runner, record=lambda *v: actions.append(("record", v))
    )
    case = probe._case(controller.plan, case_id)
    live = {"pid": 123, "uid": 4321, "start_ticks": 567, "invocation": "e" * 32}
    monkeypatch.setattr(controller, "check_production", lambda: None)
    monkeypatch.setattr(controller, "live", lambda _: live)
    monkeypatch.setattr(controller, "_ready", lambda _: live)
    monkeypatch.setattr(controller, "_empty", lambda _: {"empty": True})
    monkeypatch.setattr(controller, "_retire_empty_unit", lambda *_: True)
    monkeypatch.setattr(validation, "_request_fixture", lambda *_: actions.append(("request", case_id)))
    monkeypatch.setattr(validation, "_deadline", lambda *_: False)
    monkeypatch.setattr(
        validation, "server_finished", lambda *_: actions.append(("unwound", case_id)) or True
    )
    monkeypatch.setattr(validation, "_open_gate", lambda *_: actions.append(("gate", case_id)))
    monkeypatch.setattr(controller, "sleep", lambda _: None)

    def markers(*_):
        result = {"fixture_ready": True, "stream_finished": True, case["start_phase_marker"]: True}
        if phase["exited"]:
            if case["expectation"] == "executor_preserved":
                result.update(
                    analysis_saved=True,
                    post_saved=True,
                    executor_finished=True,
                    nested_saved=True,
                    nested_joined=True,
                )
            elif case_id == "int-async":
                result["async_cancelled"] = True
            elif case_id == "int-daemon":
                result["daemon_started"] = True
            elif case_id == "int-child":
                result.update(child_started=True, child_finished=True)
        return result

    monkeypatch.setattr(probe, "read_markers", markers)

    def exited(*_):
        phase["exited"] = True
        actions.append(("exit", case_id))
        term = case_id == "term-control"
        return (
            {
                "ExecMainCode": "2" if term else "1",
                "ExecMainStatus": "15" if term else "0",
                "Result": "signal" if term else "success",
            },
            {
                "observed": True,
                "empty": case_id != "int-child",
                "member_count": 1 if case_id == "int-child" else 0,
            },
        )

    monkeypatch.setattr(controller, "exit_state", exited)
    return controller, case, actions


@pytest.mark.parametrize("case_id", [case[0] for case in probe.CASES])
def test_synthetic_matrix_dispatches_one_bound_main_signal_and_never_proves_production(
    tmp_path, monkeypatch, case_id
):
    controller, case, actions = case_harness(tmp_path, monkeypatch, case_id)
    result = controller.case(case)
    assert result["passed"] is True
    assert result["production_quiescence_proven"] is False
    commands = [value for kind, value in actions if kind == "command"]
    signals = [value for value in commands if value[1:2] == ["kill"]]
    assert signals == [
        ["/usr/bin/systemctl", "kill", "--kill-whom=main", "--signal=" + case["signal"], case["unit"]]
    ]
    assert all(command[-1] != "stock-app.service" for command in commands)
    if case_id in {"int-child", "int-async", "int-daemon", "term-control"}:
        assert actions.index(("exit", case_id)) < actions.index(("gate", case_id))
    else:
        assert (
            actions.index(("unwound", case_id))
            < actions.index(("gate", case_id))
            < actions.index(("exit", case_id))
        )
    with pytest.raises(validation.RetirementValidationError, match="already_sent"):
        controller.case(case)
    assert len([1 for kind, value in actions if kind == "command" and value[1:2] == ["kill"]]) == 1


@pytest.mark.parametrize("deny_signal", [False, True])
def test_uncertain_dispatch_or_operator_stop_only_opens_synthetic_gate_on_cleanup(
    tmp_path, monkeypatch, deny_signal
):
    controller, case, actions = case_harness(
        tmp_path, monkeypatch, "int-executor", dispatch_error=not deny_signal, deny_signal=deny_signal
    )
    with pytest.raises(RuntimeError):
        controller.case(case)
    monkeypatch.setattr(controller, "show", lambda *_: {"MainPID": "0", "InvocationID": "e" * 32})
    assert controller.cleanup() is True
    assert ("gate", "int-executor") in actions
    signals = [v for k, v in actions if k == "command" and v[1:2] == ["kill"]]
    assert len(signals) == (0 if deny_signal else 1)


def test_cleanup_never_stops_or_signals_a_live_or_unknown_unit(tmp_path, monkeypatch):
    controller, case, actions = case_harness(tmp_path, monkeypatch, "int-executor")
    controller.active_case = case
    monkeypatch.setattr(controller, "show", lambda *_: {"MainPID": "123"})
    ticks = iter([0, 0, validation.CLEANUP_SECONDS + 1])
    monkeypatch.setattr(controller, "monotonic", lambda: next(ticks))
    assert controller.cleanup() is False
    assert not [v for k, v in actions if k == "command"]


def test_zero_pid_during_start_is_not_an_empty_stable_unit_for_cleanup(monkeypatch):
    runner = mock.Mock()
    controller = validation.Validation(REQUEST, boundary=mock.Mock(), run=runner, record=mock.Mock())
    case = controller.plan["cases"][0]
    monkeypatch.setattr(
        controller,
        "show",
        lambda *_: {
            "MainPID": "0",
            "InvocationID": "a" * 32,
            "ActiveState": "activating",
            "SubState": "start",
        },
    )
    monkeypatch.setattr(controller, "_empty", lambda *_: True)
    with pytest.raises(validation.RetirementValidationError, match="empty_unit_required"):
        controller._retire_empty_unit(case, "a" * 32)
    runner.assert_not_called()


def namespace_failure_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "RUNTIME_ROOT", tmp_path.resolve() / "runtime")
    monkeypatch.setattr(probe, "CGROUP_ROOT", tmp_path.resolve() / "cgroup")
    monkeypatch.setattr(validation, "PROC_ROOT", tmp_path.resolve() / "proc")
    probe.CGROUP_ROOT.mkdir()
    (probe.CGROUP_ROOT / "cgroup.controllers").write_text("cpu memory")
    controller = validation.Validation(REQUEST, boundary=mock.Mock(), run=mock.Mock(), record=mock.Mock())
    case = controller.plan["cases"][0]
    Path(case["host_runtime_directory"]).mkdir(parents=True, mode=0o700)
    controller.active_case = case
    controller.start_intents[case["case_id"]] = 100
    state = {
        "LoadState": "loaded",
        "Transient": "yes",
        "ActiveState": "failed",
        "SubState": "failed",
        "MainPID": "0",
        "ExecMainPID": "123",
        "ExecMainCode": "1",
        "ExecMainStatus": "226",
        "Result": "exit-code",
        "InvocationID": "a" * 32,
        "ControlGroup": "",
        "ExecMainExitTimestampMonotonic": "101",
    }
    monkeypatch.setattr(controller, "show", lambda *_: dict(state))
    return controller, case, state


def test_namespace_setup_failure_can_prove_no_resident_work_without_clearing_failed_unit(
    tmp_path, monkeypatch
):
    controller, case, _ = namespace_failure_fixture(tmp_path, monkeypatch)
    assert controller.cleanup() is True
    assert controller.namespace_setup_failed is True
    controller.run.assert_not_called()
    controller.record.assert_called_once_with(
        "synthetic_exit_observed",
        {"case_id": case["case_id"], "recursive_member_count": 0, "exit_code": 1, "exit_status": 226},
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("LoadState", "not-found"),
        ("Transient", "no"),
        ("ActiveState", "activating"),
        ("MainPID", "123"),
        ("ExecMainPID", "0"),
        ("ExecMainCode", "2"),
        ("ExecMainStatus", "0"),
        ("Result", "success"),
        ("InvocationID", ""),
        ("ControlGroup", "/system.slice/stock-app.service"),
        ("ExecMainExitTimestampMonotonic", "99"),
    ],
)
def test_namespace_failure_witness_rejects_unknown_or_mismatched_manager_state(
    tmp_path, monkeypatch, field, value
):
    controller, case, state = namespace_failure_fixture(tmp_path, monkeypatch)
    state[field] = value
    assert controller._namespace_failed_no_work(case) is False
    controller.run.assert_not_called()
    controller.record.assert_not_called()


@pytest.mark.parametrize(
    "obstacle", ["pid", "marker", "group_link", "no_intent", "signal", "changed_invocation"]
)
def test_no_work_witness_rejects_pid_reuse_markers_links_and_unbound_attempts(
    tmp_path, monkeypatch, obstacle
):
    controller, case, state = namespace_failure_fixture(tmp_path, monkeypatch)
    if obstacle == "pid":
        (validation.PROC_ROOT / "123").mkdir(parents=True)
    elif obstacle == "marker":
        (Path(case["host_runtime_directory"]) / "fixture_ready.json").write_text("{}")
    elif obstacle == "group_link":
        group = probe.CGROUP_ROOT / case["control_group"].lstrip("/")
        group.parent.mkdir(parents=True)
        group.symlink_to(tmp_path.resolve())
    elif obstacle == "no_intent":
        controller.start_intents.clear()
    elif obstacle == "signal":
        controller.signals.add(case["case_id"])
    else:
        count = 0

        def changed(_):
            nonlocal count
            count += 1
            return dict(state, InvocationID=("a" if count == 1 else "b") * 32)

        monkeypatch.setattr(controller, "show", changed)
    assert controller._namespace_failed_no_work(case) is False
    controller.run.assert_not_called()
    controller.record.assert_not_called()


def test_namespace_failure_requires_recursive_cgroup_empty_if_group_still_exists(tmp_path, monkeypatch):
    controller, case, _ = namespace_failure_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(probe, "_require_linux", lambda: None)
    group = probe.CGROUP_ROOT / case["control_group"].lstrip("/")
    child = group / "nested"
    child.mkdir(parents=True)
    (group / "cgroup.procs").write_text("")
    (child / "cgroup.procs").write_text("456\n")
    assert controller._namespace_failed_no_work(case) is False
    (child / "cgroup.procs").write_text("")
    assert controller._namespace_failed_no_work(case) is True
    controller.run.assert_not_called()


def test_failed_namespace_remains_a_failed_validation_even_when_no_work_is_verified(monkeypatch):
    monkeypatch.setattr(probe, "_require_linux", lambda: None)
    monkeypatch.setattr(validation.Validation, "production_identity", lambda *_: {})
    monkeypatch.setattr(validation, "no_existing_synthetic_workloads", lambda *_: None)
    monkeypatch.setattr(validation, "install_bundles", lambda *_: None)
    monkeypatch.setattr(validation.Validation, "case", mock.Mock(side_effect=RuntimeError("not emitted")))

    def failed_cleanup(self):
        self.namespace_setup_failed = True
        return True

    monkeypatch.setattr(validation.Validation, "cleanup", failed_cleanup)
    summary = validation.run_validation(REQUEST, boundary=mock.Mock(), run=mock.Mock(), record=mock.Mock())
    assert summary["error_code"] == "retirement_namespace_setup_failed"
    assert summary["synthetic_cleanup_verified"] is True
    assert summary["retirement_validation_passed"] is False
    assert summary["production_quiescence_proven"] is False
    assert summary["cases"] == []


def test_unknown_exception_text_is_never_in_safe_failure_result(monkeypatch):
    # Unit-only injected guard; this result must never be persisted as a Linux receipt.
    monkeypatch.setattr(probe, "_require_linux", lambda: None)
    monkeypatch.setattr(
        validation.Validation,
        "production_identity",
        mock.Mock(side_effect=RuntimeError("private raw output")),
    )
    events = []
    summary = validation.run_validation(
        REQUEST, boundary=mock.Mock(), run=mock.Mock(), record=lambda *v: events.append(v)
    )
    assert summary["retirement_validation_passed"] is False
    assert summary["production_quiescence_proven"] is False
    assert summary["systemctl_stop_end_to_end_proven"] is False
    assert summary["error_code"] == "retirement_boundary_or_command_failed"
    assert "private raw output" not in json.dumps(events)


def test_actual_mount_witness_requires_ro_tmpfs_and_the_same_runtime_inode(monkeypatch):
    runtime = "/run/prism-retirement-probe-test"
    mountinfo = f"1 0 0:1 / /run ro - tmpfs tmpfs ro\n2 1 0:2 / {runtime} rw - tmpfs tmpfs rw\n3 1 0:3 / /run/systemd ro - tmpfs tmpfs ro\n4 1 0:3 / /run/dbus ro - tmpfs tmpfs ro\n".encode()
    monkeypatch.setattr(probe, "_read_regular", lambda *_: (mountinfo, None))
    original = validation.os.stat

    def fake_stat(path, **kwargs):
        value = str(path)
        if value == "/proc/123/ns/mnt":
            return SimpleNamespace(st_ino=3)
        if value == "/proc/self/ns/mnt":
            return SimpleNamespace(st_ino=4)
        if value in {"/run/systemd", "/run/dbus"}:
            return SimpleNamespace(st_dev=1, st_ino=20, st_uid=0, st_gid=0, st_mode=stat.S_IFDIR | 0o755)
        if value in {"/proc/123/root/run/systemd", "/proc/123/root/run/dbus"}:
            return SimpleNamespace(st_dev=1, st_ino=21, st_uid=0, st_gid=0, st_mode=stat.S_IFDIR)
        if value in {"/run/private/host-storage", "/proc/123/root" + runtime}:
            return SimpleNamespace(st_dev=1, st_ino=2, st_uid=4321, st_mode=stat.S_IFDIR | 0o700)
        return original(path, **kwargs)

    with monkeypatch.context() as scope:
        scope.setattr(validation.os, "stat", fake_stat)
        scope.setattr(validation.Path, "lstat", lambda path: fake_stat(path))
        validation._mounts(123, runtime, "/run/private/host-storage")
        for mutated in (
            mountinfo.replace(b"/run/systemd ro - tmpfs", b"/run/systemd rw - tmpfs"),
            mountinfo.replace(b"/run/dbus ro - tmpfs", b"/run/dbus ro - ext4"),
        ):
            scope.setattr(probe, "_read_regular", lambda *_, raw=mutated: (raw, None))
            with pytest.raises(validation.RetirementValidationError, match="runtime_ipc_mount_rejected"):
                validation._mounts(123, runtime, "/run/private/host-storage")
        monkeypatch.setattr(
            probe, "_read_regular", lambda *_: (mountinfo.replace(b"/run ro", b"/run rw"), None)
        )
        with pytest.raises(validation.RetirementValidationError, match="actual_mounts_rejected"):
            validation._mounts(123, runtime, "/run/private/host-storage")


def test_serve_unwind_witness_must_follow_signal_and_match_actual_runtime_owner(tmp_path, monkeypatch):
    value, case, isolation, _ = isolation_fixture(tmp_path, monkeypatch)
    target = isolation.parent / "validation_server_finished.json"
    assert validation.server_finished(value, case["case_id"], 1) is False
    payload = {
        "nonce": NONCE,
        "binding_sha256": value["binding_sha256"],
        "case_profile": case["profile"],
        "at_ns": 1000,
        "finished": True,
    }
    target.write_text(json.dumps(payload))
    target.chmod(0o600)
    assert validation.server_finished(value, case["case_id"], 1) is True
    with pytest.raises(validation.RetirementValidationError, match="unwind_rejected"):
        validation.server_finished(value, case["case_id"], 2)
    payload["nonce"] = "f" * 32
    target.write_text(json.dumps(payload))
    with pytest.raises(validation.RetirementValidationError, match="unwind_rejected"):
        validation.server_finished(value, case["case_id"], 1)


@pytest.mark.parametrize("peer", [(123, 4321), (999, 4321), (123, 999)])
def test_uds_peer_must_be_the_proven_fixture_main_pid_and_uid(monkeypatch, peer):
    case = plan()["cases"][0]
    connection = mock.MagicMock()
    connection.__enter__.return_value = connection
    connection.getsockopt.return_value = struct.pack("3i", peer[0], peer[1], peer[1])
    connection.recv.return_value = b"HTTP/1.1 200 OK\r\n\r\ndata: started\n\n"
    with monkeypatch.context() as scope:
        scope.setattr(validation.socket, "SO_PEERCRED", 17, raising=False)
        scope.setattr(validation.socket, "socket", mock.Mock(return_value=connection))
        scope.setattr(
            validation.Path, "lstat", lambda *_: SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=4321)
        )
        if peer == (123, 4321):
            validation._request_fixture(case, 4321, 123)
            connection.sendall.assert_called_once()
        else:
            with pytest.raises(validation.RetirementValidationError, match="peer_rejected"):
                validation._request_fixture(case, 4321, 123)
            connection.sendall.assert_not_called()


def managed_layout_fixture(tmp_path, monkeypatch):
    # These owner substitutions are unit-test inputs, not Linux evidence.
    root = tmp_path.resolve() / "run"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    (root / "private").mkdir(mode=0o700)
    monkeypatch.setattr(probe, "RUNTIME_ROOT", root)
    monkeypatch.setattr(probe, "_require_linux", lambda: None)
    value = plan()
    case = value["cases"][0]
    physical = Path(case["host_runtime_directory"])
    physical.mkdir(mode=0o700)
    alias = Path(case["managed_runtime_alias"])
    alias.symlink_to("private/" + physical.name)
    old_stat, old_fstat = validation.os.stat, validation.os.fstat
    root_ids = {(p.stat().st_dev, p.stat().st_ino) for p in (root, root / "private")}
    target_id = (physical.stat().st_dev, physical.stat().st_ino)

    def normalized(info):
        data = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
        if (info.st_dev, info.st_ino) in root_ids or stat.S_ISLNK(info.st_mode):
            data.update(st_uid=0, st_gid=0)
        if stat.S_ISLNK(info.st_mode):
            data.update(st_mode=stat.S_IFLNK | 0o777)
        elif (info.st_dev, info.st_ino) == target_id:
            data.update(st_gid=info.st_uid)
        return SimpleNamespace(**data)

    monkeypatch.setattr(validation.os, "stat", lambda *a, **k: normalized(old_stat(*a, **k)))
    monkeypatch.setattr(validation.os, "fstat", lambda fd: normalized(old_fstat(fd)))
    return value, case, root, physical, alias


def test_host_retained_runtime_is_exact_private_storage_and_child_mount_is_a_distinct_name(
    tmp_path, monkeypatch
):
    value, case, _, physical, _ = managed_layout_fixture(tmp_path, monkeypatch)
    assert validation._host_runtime(value, case["case_id"]) == physical
    assert case["properties"]["RuntimeDirectory"].endswith("-storage")
    assert Path(case["runtime_directory"]).name != Path(case["managed_runtime_alias"]).name
    assert case["runtime_directory"] not in case["host_runtime_directory"]


@pytest.mark.parametrize(
    "target", ["../private/escape", "/run/private/other", "private/other", "private/../private/other"]
)
def test_managed_alias_never_accepts_other_relative_absolute_or_normalized_targets(
    tmp_path, monkeypatch, target
):
    value, case, _, _, alias = managed_layout_fixture(tmp_path, monkeypatch)
    alias.unlink()
    alias.symlink_to(target)
    with pytest.raises(validation.RetirementValidationError, match="managed_runtime_alias_rejected"):
        validation._host_runtime(value, case["case_id"])


@pytest.mark.parametrize("which", ["run_mode", "private_mode", "target_mode", "target_symlink"])
def test_host_runtime_requires_root_only_private_ancestor_and_no_target_symlink(tmp_path, monkeypatch, which):
    value, case, root, physical, _ = managed_layout_fixture(tmp_path, monkeypatch)
    if which == "run_mode":
        root.chmod(0o777)
    if which == "private_mode":
        (root / "private").chmod(0o755)
    if which == "target_mode":
        physical.chmod(0o755)
    if which == "target_symlink":
        physical.rmdir()
        physical.symlink_to(root, target_is_directory=True)
    with pytest.raises((OSError, validation.RetirementValidationError)):
        validation._host_runtime(value, case["case_id"])


def test_managed_runtime_private_ancestor_cannot_itself_be_a_link(tmp_path, monkeypatch):
    value, case, root, physical, alias = managed_layout_fixture(tmp_path, monkeypatch)
    alias.unlink()
    physical.rmdir()
    (root / "private").rmdir()
    alternate = root / "alternate"
    alternate.mkdir()
    (root / "private").symlink_to(alternate, target_is_directory=True)
    with pytest.raises(OSError):
        validation._host_runtime(value, case["case_id"])


def test_wrapper_failure_diagnostics_are_fixed_phase_type_with_no_exception_text(tmp_path, monkeypatch):
    value, case, isolation, _ = isolation_fixture(tmp_path, monkeypatch)
    payload = {
        "nonce": NONCE,
        "binding_sha256": value["binding_sha256"],
        "case_profile": case["profile"],
        "at_ns": 1,
        "phase": "runtime_layout",
        "error_type": "system_exit",
    }
    marker = isolation.parent / "validation_failure.json"
    marker.write_text(json.dumps(payload))
    marker.chmod(0o600)
    assert (
        validation._wrapper_failure(value, case["case_id"]) == "retirement_wrapper_runtime_layout_system_exit"
    )
    payload["exception_text"] = "private must not be accepted"
    marker.write_text(json.dumps(payload))
    with pytest.raises(validation.RetirementValidationError, match="wrapper_diagnostic_rejected"):
        validation._wrapper_failure(value, case["case_id"])
    source = validation.wrapper_source()
    ast.parse(source)
    assert b"dir_fd=fd" in source and b"str(exc)" not in source


@pytest.mark.parametrize(
    "error,expected",
    [
        (PermissionError(13, "not emitted"), True),
        (FileNotFoundError(2, "not emitted"), True),
        (ConnectionRefusedError(61, "not emitted"), False),
        (TimeoutError(60, "not emitted"), False),
        (None, False),
    ],
)
def test_runtime_ipc_canary_requires_denial_not_path_absence_or_refused_listener(error, expected):
    import errno

    tree = ast.parse(validation.wrapper_source())
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    loop = next(
        n
        for n in main.body
        if isinstance(n, ast.For)
        and isinstance(n.iter, ast.Tuple)
        and any(isinstance(v, ast.Constant) and v.value == "/run/systemd/private" for v in n.iter.elts)
    )
    assignment = ast.Assign(
        targets=[ast.Name(id="endpoints_hidden", ctx=ast.Store())], value=ast.Constant(True)
    )
    function = ast.FunctionDef(
        name="canary",
        args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=[assignment, loop, ast.Return(value=ast.Name(id="endpoints_hidden", ctx=ast.Load()))],
        decorator_list=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    connection = mock.MagicMock()
    connection.__enter__.return_value = connection
    connection.connect.side_effect = error
    fake_socket = SimpleNamespace(socket=mock.Mock(return_value=connection), AF_UNIX=1, SOCK_STREAM=1)
    namespace = {"socket": fake_socket, "errno": errno}
    exec(compile(module, "<fixed-ipc-canary-unit-only>", "exec"), namespace)
    assert namespace["canary"]() is expected
    assert connection.connect.call_args_list == [
        mock.call("/run/systemd/private"),
        mock.call("/run/dbus/system_bus_socket"),
    ]
    connection.sendall.assert_not_called()
    connection.send.assert_not_called()
