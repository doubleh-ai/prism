"""Offline failed-build recovery: no SSH, installs, real units or credentials."""

import errno
import json
import os
import sys
import types
from pathlib import Path
from unittest import mock

import pytest
import test_release_controller as existing

remote = existing.remote


def test_both_fixed_recovery_actions_are_exposed_without_integration_wip():
    assert {"diagnose_build", "quarantine_failed_prepare"}.issubset(remote.ACTIONS)
    assert {"diagnose_build", "quarantine_failed_prepare"}.issubset(existing.controller.ACTIONS)
    assert "integration_astock" not in remote.ACTIONS


@pytest.fixture
def failed():
    fixture = existing.Fixture()
    fixture.setUp()
    original = fixture.engine.run

    def runner(args, **kwargs):
        if args[0] == "/usr/bin/systemd-run" and "install" in args:
            raise remote.ReleaseError("command_failed")
        return original(args, **kwargs)

    fixture.engine.run = runner
    with pytest.raises(remote.ReleaseError, match="command_failed"):
        fixture.engine.execute(fixture.data)

    def controls(args, **kwargs):
        if args[:2] == ["/usr/bin/systemctl", "show"] and args[-1].startswith("prism-diagnose-"):
            return b"LoadState=not-found\nActiveState=inactive\n"
        return original(args, **kwargs)

    fixture.engine.run = controls
    fixture.engine.runtime = lambda: {"working_directory": str(fixture.engine.production)}
    fixture.engine.no_stage_processes = lambda: None
    system = fixture.root / "usr/bin/python3.12"
    system.parent.mkdir(parents=True)
    system.write_bytes((fixture.engine.release / ".venv/bin/python").read_bytes())
    fixture.diagnostics = []

    def diagnose(args, **kwargs):
        fixture.diagnostics.append(args)
        output = (
            b"" if args[0] == "/usr/bin/journalctl" else b"example 1 requires pip, which is not installed.\n"
        )
        return {
            "return_code": 0 if "/usr/bin/true" in args else 1,
            "output": output,
            "truncated": False,
            "timed_out": False,
        }

    fixture.engine.diagnostic_runner = diagnose
    try:
        yield fixture
    finally:
        fixture.tearDown()


def test_diagnosis_returns_nonzero_facts_without_marking_prepared_or_rewriting_failure(failed):
    before = (failed.engine.record / "journal.jsonl").read_bytes()
    snapshot = remote.tree_digest(failed.engine.release)
    result = failed.engine.execute(dict(failed.data, action="diagnose_build"))
    report = result["diagnostics"]
    assert report["prepare_verified"] is False
    assert report["commands"]["check"]["return_code"] == 1
    assert report["commands"]["dryrun"]["return_code"] == 1
    assert report["commands"]["check"]["categories"] == ["missing_dependency"]
    assert not (failed.engine.record / "prepared.json").exists()
    assert (failed.engine.record / "journal.jsonl").read_bytes().startswith(before)
    assert remote.tree_digest(failed.engine.release) == snapshot
    assert len(list(failed.engine.record.glob("build-diagnostic-*.json"))) == 1
    commands = [args for args in failed.diagnostics if args[0] == "/usr/bin/systemd-run"]
    assert len(commands) == 3
    for command in commands:
        for flag in (
            "--uid=prism-stage",
            "--property=PrivateNetwork=yes",
            "--property=ProtectHome=yes",
            "--property=ProtectProc=invisible",
            "--property=ProtectSystem=strict",
        ):
            assert flag in command
        assert "--property=InaccessiblePaths=-" + str(failed.engine.operations / "staging") in command
        assert "--property=TemporaryFileSystem=/run:ro" in command
    assert any(arg.startswith("--property=ReadWritePaths=") for arg in commands[0])
    assert all(
        not any(arg.startswith("--property=ReadWritePaths=") for arg in command) for command in commands[1:]
    )
    assert "--dry-run" in commands[2] and "--no-index" in commands[2]


def test_namespace_failure_is_observed_and_skips_pip_without_weakening_isolation(failed):
    def diagnose(args, **kwargs):
        failed.diagnostics.append(args)
        output = b"Main processes terminated with: code=exited/status=226/NAMESPACE\n"
        if args[0] == "/usr/bin/journalctl":
            output = (
                b"Failed to set up mount namespacing: /run/systemd/unit-root/run: No such file or directory\n"
            )
        return {"return_code": 226, "output": output, "truncated": False, "timed_out": False}

    failed.engine.diagnostic_runner = diagnose
    report = failed.engine.execute(dict(failed.data, action="diagnose_build"))["diagnostics"]
    assert report["pip_checks_skipped"] == "namespace_preflight_failed"
    assert set(report["commands"]) == {"namespace"}
    namespace = report["commands"]["namespace"]
    assert "namespace_setup_failed" in namespace["categories"]
    assert "missing_path" in namespace["unit_journal"]["categories"]
    assert namespace["unit_journal"]["sandbox_system_paths"] == ["/run/systemd/unit-root/run"]
    assert set(report["namespace_variants"]) == {
        "without_optional_staging",
        "private_run_tmpfs",
        "masked_runtime_endpoints",
        "basic_namespace",
    }
    assert all("pip" not in command for command in failed.diagnostics)


def test_variant_success_never_allows_pip_and_all_variants_are_fixed_true_only(failed):
    def diagnose(args, **kwargs):
        failed.diagnostics.append(args)
        original = any(arg.startswith("--unit=prism-diagnose-") and "-namespace-" in arg for arg in args)
        return {"return_code": 226 if original else 0, "output": b"", "truncated": False, "timed_out": False}

    failed.engine.diagnostic_runner = diagnose
    data = dict(failed.data, action="diagnose_build", namespace_variants=["--property=PrivateNetwork=no"])
    report = failed.engine.execute(data)["diagnostics"]
    assert report["original_namespace_succeeded"] is False
    assert report["prepare_verified"] is False
    assert report["pip_checks_skipped"] == "namespace_preflight_failed"
    commands = [args for args in failed.diagnostics if args[0] == "/usr/bin/systemd-run"]
    assert len(commands) == 5
    assert all(command[-1] == "/usr/bin/true" and "pip" not in command for command in commands)
    assert len({next(arg for arg in command if arg.startswith("--unit=")) for command in commands}) == 5
    for command in commands:
        for flag in (
            "--uid=prism-stage",
            "--property=PrivateNetwork=yes",
            "--property=ProtectHome=yes",
            "--property=ProtectProc=invisible",
            "--property=NoNewPrivileges=yes",
            "--property=PrivateTmp=yes",
            "--property=ProtectSystem=strict",
            "--property=RuntimeMaxSec=60",
            "--property=ReadWritePaths=" + str(failed.engine.release / ".venv"),
        ):
            assert flag in command
        assert "--property=PrivateNetwork=no" not in command
    assert "--property=InaccessiblePaths=/run" in commands[1]
    assert "--property=TemporaryFileSystem=/run:ro" in commands[2]
    assert "--property=InaccessiblePaths=-" + str(failed.engine.operations / "staging") in commands[2]
    assert "--property=InaccessiblePaths=-/run/dbus -/run/systemd" in commands[3]
    assert not any(arg.startswith("--property=InaccessiblePaths=") for arg in commands[4])
    for name, result in report["namespace_variants"].items():
        assert result["return_code"] == 0
        assert result["diagnostic_only"] is True
        assert result["isolation_verified"] is False
        assert result["prepare_verified"] is False
        assert "diagnose_build_" + name in failed.boundaries


def test_matrix_interruption_preserves_completed_diagnostic_results(failed):
    def diagnose(args, **kwargs):
        failed.diagnostics.append(args)
        return {"return_code": 226, "output": b"", "truncated": False, "timed_out": False}

    failed.engine.diagnostic_runner = diagnose
    failed.engine.permit = lambda nonce, label: None if label == "diagnose_build_private_run_tmpfs" else nonce
    with pytest.raises(remote.ReleaseError, match="permit"):
        failed.engine.execute(dict(failed.data, action="diagnose_build"))
    paths = list(failed.engine.record.glob("build-diagnostic-*.json"))
    assert len(paths) == 1
    partial = json.loads(paths[0].read_bytes())
    assert partial["commands"]["namespace"]["return_code"] == 226
    assert set(partial["namespace_variants"]) == {"without_optional_staging"}
    assert not (failed.engine.record / "prepared.json").exists()
    assert len([args for args in failed.diagnostics if args[0] == "/usr/bin/systemd-run"]) == 2


@pytest.mark.parametrize("phase", ["runonly", "privaterun", "runtimemasks", "basicnamespace"])
def test_only_fixed_matrix_units_accept_a_collected_notfound_state(phase):
    result = types.SimpleNamespace(returncode=1, stdout=b"LoadState=not-found\nActiveState=inactive\n")
    with mock.patch.object(remote.subprocess, "run", return_value=result):
        remote.run_command(
            [
                "/usr/bin/systemctl",
                "show",
                "--property=LoadState,ActiveState",
                "prism-diagnose-" + "a" * 12 + "-" + phase + "-" + "1" * 8 + ".service",
            ]
        )
        with pytest.raises(remote.ReleaseError, match="command_failed"):
            remote.run_command(
                [
                    "/usr/bin/systemctl",
                    "show",
                    "--property=LoadState,ActiveState",
                    "prism-diagnose-" + "a" * 12 + "-arbitrary-" + "1" * 8 + ".service",
                ]
            )


@pytest.mark.parametrize(
    "marker",
    ["prepared.json", "staged.json", "previous.json", "cutover.json", "rollback.json", "admission.json"],
)
def test_completed_or_cutover_candidate_cannot_be_diagnosed_or_quarantined(failed, marker):
    (failed.engine.record / marker).write_text("{}")
    for action in ("diagnose_build", "quarantine_failed_prepare"):
        with pytest.raises(remote.ReleaseError, match="failed_prepare_only"):
            failed.engine.execute(dict(failed.data, action=action))
    assert not failed.diagnostics


def test_guard_requires_original_runtime_source_identity_and_no_active_uid(failed):
    failed.engine.runtime = lambda: {"working_directory": str(failed.engine.release)}
    with pytest.raises(remote.ReleaseError, match="original_runtime"):
        failed.engine.execute(dict(failed.data, action="diagnose_build"))
    failed.engine.runtime = lambda: {"working_directory": str(failed.engine.production)}
    (failed.engine.release / "server/main.py").write_text("changed")
    with pytest.raises(remote.ReleaseError, match="source_drift"):
        failed.engine.execute(dict(failed.data, action="diagnose_build"))
    proc = failed.root / "proc/99"
    proc.mkdir(parents=True)
    (proc / "status").write_text("Uid:\t" + "\t".join([str(os.getuid())] * 4) + "\n")
    with mock.patch.object(remote.pwd, "getpwnam", return_value=types.SimpleNamespace(pw_uid=os.getuid())):
        with pytest.raises(remote.ReleaseError, match="stage_process_active"):
            remote.RemoteRelease.no_stage_processes(failed.engine)
    assert not failed.diagnostics


@pytest.mark.parametrize("position", range(4))
def test_nondumpable_root_owned_proc_is_rejected_when_any_kernel_uid_is_stage(failed, position):
    proc = failed.root / "proc/99"
    proc.mkdir(parents=True)
    values = [0, 0, 0, 0]
    values[position] = 424242
    (proc / "status").write_text("Name:\tsynthetic\nUid:\t" + "\t".join(map(str, values)) + "\n")
    with (
        mock.patch.object(remote.pwd, "getpwnam", return_value=types.SimpleNamespace(pw_uid=424242)),
        mock.patch.object(Path, "stat", return_value=types.SimpleNamespace(st_uid=0)),
    ):
        with pytest.raises(remote.ReleaseError, match="stage_process_active"):
            remote.RemoteRelease.no_stage_processes(failed.engine)


@pytest.mark.parametrize(
    "status",
    [
        b"Name:\tsynthetic\n",
        b"Uid:\t0\t1\t2\n",
        b"Uid:\t0\t1\t2\t3\t4\n",
        b"Uid:\t0\t1\t2\t-1\n",
        b"Uid:\t0\t1\t2\t4294967296\n",
    ],
)
def test_malformed_process_uid_status_fails_closed(failed, status):
    proc = failed.root / "proc/99"
    proc.mkdir(parents=True)
    (proc / "status").write_bytes(status)
    with mock.patch.object(remote.pwd, "getpwnam", return_value=types.SimpleNamespace(pw_uid=424242)):
        with pytest.raises(remote.ReleaseError, match="process_status_rejected"):
            remote.RemoteRelease.no_stage_processes(failed.engine)


def test_unreadable_uid_status_fails_closed_and_actual_process_disappearance_is_allowed(failed):
    proc = failed.root / "proc/99"
    proc.mkdir(parents=True)
    original = Path.open

    def forbidden(path, *args, **kwargs):
        if path == proc / "status":
            raise PermissionError
        return original(path, *args, **kwargs)

    with (
        mock.patch.object(remote.pwd, "getpwnam", return_value=types.SimpleNamespace(pw_uid=424242)),
        mock.patch.object(Path, "open", forbidden),
    ):
        with pytest.raises(remote.ReleaseError, match="process_inspection_failed"):
            remote.RemoteRelease.no_stage_processes(failed.engine)

    def disappeared(path, *args, **kwargs):
        if path == proc / "status":
            proc.rmdir()
            raise FileNotFoundError
        return original(path, *args, **kwargs)

    with (
        mock.patch.object(remote.pwd, "getpwnam", return_value=types.SimpleNamespace(pw_uid=424242)),
        mock.patch.object(Path, "open", disappeared),
    ):
        remote.RemoteRelease.no_stage_processes(failed.engine)


def test_diagnostic_stream_is_capped_while_reading_not_after_communicate():
    result = remote.run_diagnostic(
        [sys.executable, "-I", "-B", "-c", "import os; os.write(1, b'x' * 400000)"]
    )
    assert result["truncated"] is True
    assert len(result["output"]) == 256 * 1024


def test_diagnostic_extraction_never_echoes_raw_lines_unknown_packages_or_private_paths():
    release = Path("/opt/prism/releases/" + "a" * 40)
    raw = (
        b"PRIVATE_UPSTREAM_FIXTURE /home/ubuntu/.env\n"
        b"private_fixture 1 has requirement secret_fixture==2, but you have pip 1.\n"
        b"ERROR: Permission denied: '"
        + str(release).encode()
        + b"/.venv/lib/python3.12/site-packages/pip/file.py'\n"
        b"ERROR: Permission denied: '/home/ubuntu/.ssh/private_fixture'\n"
        b"Failed mount /run/systemd/private_fixture: permission denied\n"
    )
    result = remote.safe_build_diagnostics(raw, release=release, packages={"pip": "1"})
    serialized = json.dumps(result)
    assert "PRIVATE" not in serialized and "secret_fixture" not in serialized
    assert "/home" not in serialized
    assert "private_fixture" not in serialized
    assert result["candidate_relative_paths"] == [".venv/lib/python3.12/site-packages/pip/file.py"]


def test_quarantine_renames_only_bound_directories_and_preserves_journal_and_shared_inputs(failed):
    before = (failed.engine.record / "journal.jsonl").read_bytes()
    source_inode = failed.engine.release.stat().st_ino
    shared = remote.tree_digest(failed.root / "opt/prism/engines")
    result = failed.engine.execute(dict(failed.data, action="quarantine_failed_prepare"))
    assert result["production_verified"] is False
    assert not failed.engine.release.exists()
    intent = json.loads((failed.engine.record / "quarantine-intent.json").read_bytes())
    assert intent["completed"] is True
    candidate = Path(intent["items"][0]["destination"])
    assert candidate.stat().st_ino == source_inode
    assert all(
        Path(item["destination"]).is_dir() and not Path(item["source"]).exists() for item in intent["items"]
    )
    assert (failed.engine.record / "journal.jsonl").read_bytes().startswith(before)
    assert remote.tree_digest(failed.root / "opt/prism/engines") == shared
    failed.engine.execute(failed.data)
    assert (failed.engine.record / "prepared.json").exists()
    assert candidate.stat().st_ino == source_inode


def test_quarantine_resume_binds_inodes_and_does_not_delete_or_copy_on_exdev(failed):
    rename = Path.rename
    count = 0

    def interrupted(path, destination):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError(errno.EXDEV, "synthetic cross-device failure")
        return rename(path, destination)

    with mock.patch.object(Path, "rename", interrupted):
        with pytest.raises(OSError):
            failed.engine.execute(dict(failed.data, action="quarantine_failed_prepare"))
    intent = json.loads((failed.engine.record / "quarantine-intent.json").read_bytes())
    assert not failed.engine.release.exists()
    assert Path(intent["items"][0]["destination"]).exists()
    assert Path(intent["items"][1]["source"]).exists()
    failed.engine.execute(dict(failed.data, action="quarantine_failed_prepare"))
    assert json.loads((failed.engine.record / "quarantine-intent.json").read_bytes())["completed"] is True


def test_quarantine_rejects_swapped_inodes_and_symlink_targets(failed):
    failed.engine.permit = lambda nonce, label: None if label == "quarantine_failed_prepare" else nonce
    with pytest.raises(remote.ReleaseError, match="permit"):
        failed.engine.execute(dict(failed.data, action="quarantine_failed_prepare"))
    intent = json.loads((failed.engine.record / "quarantine-intent.json").read_bytes())
    destination = Path(intent["items"][0]["destination"])
    destination.symlink_to(failed.engine.production)
    failed.engine.permit = failed.permit
    with pytest.raises(remote.ReleaseError, match="symlink_path_rejected"):
        failed.engine.execute(dict(failed.data, action="quarantine_failed_prepare"))
    destination.unlink()
    source = failed.engine.release
    renamed = source.with_name("different-directory")
    source.rename(renamed)
    source.mkdir()
    with pytest.raises(remote.ReleaseError, match="quarantine_identity_changed"):
        failed.engine.execute(dict(failed.data, action="quarantine_failed_prepare"))


def test_each_quarantine_rename_requires_a_fresh_permit_and_preserves_partial_attempt(failed):
    labels = []

    def permit(nonce, label):
        labels.append(label)
        return None if label == "quarantine_dependencies" else nonce

    failed.engine.permit = permit
    with pytest.raises(remote.ReleaseError, match="permit"):
        failed.engine.execute(dict(failed.data, action="quarantine_failed_prepare"))
    intent = json.loads((failed.engine.record / "quarantine-intent.json").read_bytes())
    assert Path(intent["items"][0]["destination"]).exists()
    assert Path(intent["items"][1]["source"]).exists()
    assert Path(intent["items"][2]["source"]).exists()
    assert "quarantine_candidate" in labels
    assert "quarantine_dependencies" in labels
    assert "quarantine_build_inputs" not in labels
    failed.engine.permit = failed.permit
    failed.engine.execute(dict(failed.data, action="quarantine_failed_prepare"))
    assert "quarantine_dependencies" in failed.boundaries
    assert "quarantine_build_inputs" in failed.boundaries


def test_quarantine_inode_is_rechecked_after_the_fresh_permit(failed):
    def permit(nonce, label):
        if label == "quarantine_candidate":
            failed.engine.release.rename(failed.engine.release.with_name("source-before-permit"))
            failed.engine.release.mkdir()
        return nonce

    failed.engine.permit = permit
    with pytest.raises(remote.ReleaseError, match="quarantine_identity_changed"):
        failed.engine.execute(dict(failed.data, action="quarantine_failed_prepare"))
    intent = json.loads((failed.engine.record / "quarantine-intent.json").read_bytes())
    assert not Path(intent["items"][0]["destination"]).exists()
