"""Offline unit tests only: these never certify Linux or execute a systemd unit."""

import ast
import copy
import json
import os
from pathlib import Path
from unittest import mock

import pytest

from scripts import retirement_probe as probe

REQUEST = {
    "commit": "a" * 40,
    "release_id": "retirement-test-001",
    "base_environment_sha256": probe.OLD_ENV_SHA256,
}
TOKEN = "b" * 32


def plan():
    return probe.descriptor(REQUEST, nonce=TOKEN)


def observation(case_id):
    value = {
        key: True
        for key in (
            "linux",
            "source_bound",
            "uid_isolated",
            "private_network",
            "cgroup_bound",
            "same_invocation",
            "exit_after_signal",
            "inactive",
            "result_success",
            "cgroup_observed",
            "cgroup_empty",
            "cleanup_cgroup_empty",
        )
    }
    value.update(
        exec_main_code=1, exec_main_status=0, signals_sent=1, deadline_expired=False, cgroup_member_count=0
    )
    value["gate_closed_before_signal"] = True
    value["markers_before_signal"] = {
        "fixture_ready": True,
        "stream_finished": True,
        probe._case(plan(), case_id)["start_phase_marker"]: True,
    }
    value["markers"] = {key: True for key in ("fixture_ready", "stream_finished")}
    if case_id in {"int-executor", "int-post", "int-nested"}:
        value["markers"].update(analysis_saved=True, post_saved=True, executor_finished=True)
        if case_id == "int-nested":
            value["markers"].update(nested_saved=True, nested_joined=True)
    elif case_id == "int-async":
        value["markers"]["async_cancelled"] = True
    elif case_id == "int-daemon":
        value["markers"]["daemon_started"] = True
    elif case_id == "int-child":
        value.update(cgroup_empty=False, cgroup_member_count=1)
        value["markers"].update(child_started=True, child_finished=True)
    else:
        value.update(exec_main_code=2, exec_main_status=15, result_success=False)
    return value


@pytest.mark.parametrize(
    "changed",
    [
        {"command": "anything"},
        {"unit": "stock-app.service"},
        {"commit": "A" * 40},
        {"release_id": "../../outside"},
        {"base_environment_sha256": "c" * 64},
        {"commit": 1},
        {"release_id": "\nprivate-value"},
    ],
)
def test_only_three_fixed_identity_fields_are_accepted(changed):
    request = dict(REQUEST, **changed)
    with pytest.raises(probe.RetirementProbeError):
        probe.descriptor(request, nonce=TOKEN)


def test_descriptor_cannot_choose_a_host_unit_uid_or_executable():
    value = plan()
    assert len(value["cases"]) == 7
    assert value["production_quiescence_proven"] is False
    assert value["actual_linux_acceptance"] is False
    for case in value["cases"]:
        assert case["unit"].startswith("prism-retirement-probe-" + TOKEN + "-")
        assert case["control_group"] == "/system.slice/" + case["unit"]
        assert case["command"][:2] == ["/usr/bin/env", "-i"]
        assert "/usr/bin/python3" in case["command"]
        assert all(flag in case["command"] for flag in ("-I", "-S", "-B"))
        assert case["stop_once_only"] is True
        properties = case["properties"]
        for key in (
            "DynamicUser",
            "PrivateNetwork",
            "PrivateTmp",
            "ProtectHome",
            "NoNewPrivileges",
            "ProtectControlGroups",
            "ProtectKernelTunables",
            "ProtectKernelModules",
            "PrivateDevices",
        ):
            assert properties[key] == "yes"
        assert properties["ProtectProc"] == "invisible"
        assert properties["RuntimeDirectoryMode"] == "0700"
        assert properties["RuntimeDirectoryPreserve"] == "yes"
        assert properties["ProtectSystem"] == "strict"
        assert properties["RestrictAddressFamilies"] == "AF_UNIX"
        assert properties["KillMode"] == "process"
        assert properties["SendSIGKILL"] == "no"
        assert properties["SendSIGHUP"] == "no"
        assert properties["Restart"] == "no"
        assert properties["TimeoutStopSec"] == properties["RuntimeMaxSec"] == "infinity"
        assert case["signal"] == ("SIGTERM" if case["case_id"] == "term-control" else "SIGINT")
        masks = properties["InaccessiblePaths"].split()
        assert "/etc" in masks and "/var/lib" in masks
        assert not any(path.startswith("-") for path in masks)
        for protected in ("/etc/prism", "/var/lib/prism", "/var/lib/prism-operations"):
            assert any(Path(mask) in Path(protected).parents for mask in masks)


def test_manifest_only_binds_public_runtime_bytes_without_startup_hooks():
    files = probe.package_manifest()
    assert len(files) == 72
    assert sum(row["size"] for row in files) == 16669798
    assert {row["path"].split("/")[0] for row in files} == {"uvicorn", "uvloop", "click", "h11"}
    assert all(
        Path(row["path"]).suffix in {".py", ".so"}
        and "__pycache__" not in row["path"]
        and ".." not in Path(row["path"]).parts
        for row in files
    )
    assert next(row["sha256"] for row in files if row["path"] == "uvicorn/server.py") == probe.UVICORN_SHA256
    assert next(row["sha256"] for row in files if row["path"].endswith(".so")) == probe.UVLOOP_SHA256
    source = probe.FIXTURE_SOURCE
    compile(source, "fixed-retirement-fixture", "exec")
    assert source.index("os.geteuid() in forbidden") < source.index("import uvicorn")
    assert source.index('pinned("uvloop/') < source.index("import uvloop")
    assert "site.main" not in source and "timeout_graceful_shutdown=None" in source
    ast.parse(Path(probe.__file__).read_text())


@pytest.mark.parametrize("case_id", [case[0] for case in probe.CASES])
def test_expected_positive_and_negative_observations_are_distinguished(case_id):
    result = probe.evaluate_case(plan(), case_id, observation(case_id))
    assert result["passed"] is True
    assert result["expected_counterexample"] is (case_id not in {"int-executor", "int-post", "int-nested"})
    assert result["production_quiescence_proven"] is False
    assert result["task_census_still_required"] is True


@pytest.mark.parametrize(
    "field",
    [
        "linux",
        "source_bound",
        "uid_isolated",
        "private_network",
        "cgroup_bound",
        "same_invocation",
        "exit_after_signal",
        "inactive",
        "result_success",
        "cgroup_observed",
        "cleanup_cgroup_empty",
    ],
)
def test_unknown_or_false_runtime_condition_blocks_positive_case(field):
    data = observation("int-executor")
    data[field] = False
    assert probe.evaluate_case(plan(), "int-executor", data)["passed"] is False


@pytest.mark.parametrize(
    "changed",
    [
        {"signals_sent": 2},
        {"deadline_expired": True},
        {"exec_main_code": 2, "exec_main_status": 15},
        {"cgroup_empty": False, "cgroup_member_count": 1},
    ],
)
def test_zero_or_signal_exit_descendant_or_deadline_is_not_preservation(changed):
    data = dict(observation("int-executor"), **changed)
    assert probe.evaluate_case(plan(), "int-executor", data)["passed"] is False


def test_exit_zero_without_post_complete_result_is_rejected():
    data = observation("int-post")
    data["markers"]["post_saved"] = False
    assert probe.evaluate_case(plan(), "int-post", data)["passed"] is False
    data["markers"]["post_saved"] = True
    data["markers"]["fixture_gate_expired"] = True
    assert probe.evaluate_case(plan(), "int-post", data)["passed"] is False


def test_orphan_must_be_witnessed_before_natural_cleanup():
    data = observation("int-child")
    data.update(cgroup_empty=True, cgroup_member_count=0)
    assert probe.evaluate_case(plan(), "int-child", data)["passed"] is False


def test_completed_work_before_signal_cannot_certify_executor_join():
    data = observation("int-post")
    data["markers_before_signal"]["post_saved"] = True
    assert probe.evaluate_case(plan(), "int-post", data)["passed"] is False
    data["markers_before_signal"]["post_saved"] = False
    data["gate_closed_before_signal"] = False
    assert probe.evaluate_case(plan(), "int-post", data)["passed"] is False


@pytest.mark.parametrize(
    "change",
    [{"linux": 1}, {"signals_sent": True}, {"raw_log": "never emit this"}, {"cgroup_member_count": -1}],
)
def test_observation_schema_is_strict_and_errors_are_fixed_codes(change):
    data = dict(observation("int-executor"), **change)
    with pytest.raises(probe.RetirementProbeError) as error:
        probe.evaluate_case(plan(), "int-executor", data)
    assert "never emit this" not in str(error.value)


def test_descriptor_mutation_is_rejected_before_filesystem_access():
    value = plan()
    value["cases"][0]["unit"] = "stock-app.service"
    with pytest.raises(probe.RetirementProbeError, match="descriptor_changed"):
        probe.evaluate_case(value, "int-executor", observation("int-executor"))


def test_summary_never_means_production_is_quiescent_or_tests_are_linux_proof():
    value = plan()
    results = [probe.evaluate_case(value, case[0], observation(case[0])) for case in probe.CASES]
    summary = probe.summarize(value, results)
    assert summary["retirement_validation_passed"] is True
    assert summary["production_quiescence_proven"] is False
    assert summary["actual_linux_acceptance_requires_root_collection"] is True
    assert summary["task_census_still_required"] is True
    with pytest.raises(probe.RetirementProbeError, match="matrix_incomplete"):
        probe.summarize(value, results[:-1])
    with pytest.raises(probe.RetirementProbeError, match="matrix_incomplete"):
        probe.summarize(value, [1] * len(results))
    altered = copy.deepcopy(results)
    altered[0]["private_log"] = "do not forward"
    with pytest.raises(probe.RetirementProbeError, match="result_rejected"):
        probe.summarize(value, altered)


def test_recursive_plain_filesystem_unit_test_is_not_a_linux_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "_require_linux", lambda: None)
    monkeypatch.setattr(probe, "CGROUP_ROOT", tmp_path)
    (tmp_path / "cgroup.controllers").write_text("cpu memory")
    value = plan()
    case = value["cases"][0]
    group = tmp_path / case["control_group"].lstrip("/")
    group.mkdir(parents=True)
    (group / "cgroup.procs").write_text("111\n")
    child = group / "nested"
    child.mkdir()
    (child / "cgroup.procs").write_text("222\n333\n")
    state = probe.recursive_cgroup_state(value, case["case_id"])
    assert state["members"] == [111, 222, 333] and state["empty"] is False
    (child / "cgroup.procs").unlink()
    (group / "cgroup.procs").unlink()
    child.rmdir()
    group.rmdir()
    assert probe.recursive_cgroup_state(value, case["case_id"])["observed"] is False
    assert probe.recursive_cgroup_state(value, case["case_id"], stopped=True)["empty"] is False
    assert (
        probe.recursive_cgroup_state(value, case["case_id"], stopped=True, previously_observed=True)["empty"]
        is True
    )


def test_symlink_and_invalid_pid_in_recursive_group_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "_require_linux", lambda: None)
    monkeypatch.setattr(probe, "CGROUP_ROOT", tmp_path)
    (tmp_path / "cgroup.controllers").write_text("cpu")
    value = plan()
    group = tmp_path / value["cases"][0]["control_group"].lstrip("/")
    group.mkdir(parents=True)
    (group / "cgroup.procs").write_text("not-a-pid")
    with pytest.raises(probe.RetirementProbeError, match="member_rejected"):
        probe.recursive_cgroup_state(value, "int-executor")
    (group / "cgroup.procs").write_text("")
    (group / "linked").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(probe.RetirementProbeError, match="link_rejected"):
        probe.recursive_cgroup_state(value, "int-executor")


def test_read_regular_never_follows_a_link_or_accepts_hard_links(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"synthetic")
    linked = tmp_path / "linked"
    linked.symlink_to(source)
    with pytest.raises(probe.RetirementProbeError, match="file_open_rejected"):
        probe._read_regular(linked, 100)
    linked.unlink()
    os.link(source, linked)
    with pytest.raises(probe.RetirementProbeError, match="regular_file_required"):
        probe._read_regular(source, 100)


def test_proc_stat_parser_binds_pid_and_handles_spaces_parentheses():
    fields = [b"S"] + [b"0"] * 18 + [b"456"]
    raw = b"123 (synthetic ) name) " + b" ".join(fields)
    assert probe._start_ticks(raw, 123) == 456
    for invalid in (b"", raw.replace(b"456", b"0"), raw.replace(b"456", b"-1")):
        with pytest.raises(probe.RetirementProbeError):
            probe._start_ticks(invalid, 123)
    with pytest.raises(probe.RetirementProbeError):
        probe._start_ticks(raw, 124)


def test_marker_read_emits_only_bound_boolean_states(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(probe.pwd, "getpwnam", mock.Mock(side_effect=KeyError))
    value = plan()
    case = value["cases"][0]
    root = Path(case["runtime_directory"])
    root.mkdir(mode=0o700)
    source = root / "fixture_ready.json"
    payload = {
        "nonce": TOKEN,
        "binding_sha256": value["binding_sha256"],
        "case_profile": "executor",
        "at_ns": 1,
        "uid_nonroot": True,
        "versions_bound": True,
    }
    source.write_text(json.dumps(payload))
    source.chmod(0o600)
    result = probe.read_markers(value, "int-executor")
    assert result["fixture_ready"] is True and all(type(v) is bool for v in result.values())
    payload["nonce"] = "c" * 32
    source.write_text(json.dumps(payload))
    with pytest.raises(probe.RetirementProbeError, match="identity_rejected"):
        probe.read_markers(value, "int-executor")
    source.unlink()
    source.symlink_to(root / "absent")
    with pytest.raises(probe.RetirementProbeError, match="link_rejected"):
        probe.read_markers(value, "int-executor")


def test_real_kernel_readers_reject_non_linux_without_side_effects(monkeypatch):
    monkeypatch.setattr(probe.sys, "platform", "darwin")
    with pytest.raises(probe.RetirementProbeError, match="linux_required"):
        probe.recursive_cgroup_state(plan(), "int-executor")
    with pytest.raises(probe.RetirementProbeError, match="linux_required"):
        probe.linux_process_identity(plan(), "int-executor", 123)


def test_process_outside_fixture_is_rejected_before_any_proc_read(monkeypatch):
    monkeypatch.setattr(probe, "_require_linux", lambda: None)
    monkeypatch.setattr(probe, "recursive_cgroup_state", lambda *_: {"members": []})
    reader = mock.Mock(side_effect=AssertionError("must not read another process"))
    monkeypatch.setattr(probe, "_read_regular", reader)
    with pytest.raises(probe.RetirementProbeError, match="process_outside_fixture"):
        probe.linux_process_identity(plan(), "int-executor", 123)
    reader.assert_not_called()
