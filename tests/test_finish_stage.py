"""Stop only one accepted synthetic unit/cgroup; no real service operations."""

import json
import types
from unittest import mock

import pytest
import test_release_controller as existing

remote = existing.remote


@pytest.fixture
def accepted():
    fixture = existing.Fixture()
    fixture.setUp()
    fixture.prepared()
    record = fixture.engine.record
    proof = {"commit": existing.COMMIT, "stage_pid": 222}
    identity = {"stage_pid": 222, "read_only_private_runtime_mount": True}
    journeys = {"commit": existing.COMMIT, "served_commit": existing.COMMIT, "passed": True}
    staged = {
        "read_only_private_runtime_mount": True,
        "isolation_proof_sha256": remote.digest(remote.encoded(proof)),
        "journeys_sha256": remote.digest(remote.encoded(journeys)),
    }
    for name, value in (
        ("staged.json", staged),
        ("stage-isolation.json", {"proof": proof, "identity": identity}),
        ("stage-journeys.json", journeys),
    ):
        (record / name).write_bytes(remote.encoded(value))
    fixture.unit = "prism-stage-" + existing.COMMIT[:12] + ".service"
    fixture.group = "/system.slice/" + fixture.unit
    cgroups = fixture.root / "sys/fs/cgroup"
    cgroups.mkdir(parents=True)
    (cgroups / "cgroup.controllers").write_text("memory pids cpu")
    fixture.members = cgroups / fixture.group.lstrip("/") / "cgroup.procs"
    fixture.members.parent.mkdir(parents=True)
    fixture.members.write_text("222\n")
    other = cgroups / "system.slice/prism-stage-other.service/cgroup.procs"
    other.parent.mkdir(parents=True)
    other.write_text("333\n")
    fixture.other = other
    process = fixture.root / "proc/222"
    process.mkdir(parents=True)
    (process / "status").write_text("Uid:\t4242\t4242\t4242\t4242\n")
    (process / "cwd").symlink_to(fixture.engine.release)
    (process / "cgroup").write_text("0::" + fixture.group + "\n")
    fixture.stopped = []
    original = fixture.engine.run

    def runner(args, **kwargs):
        if args[:2] == ["/usr/bin/systemctl", "show"] and args[-1] == fixture.unit:
            if "--property=LoadState,ActiveState" in args:
                return b"LoadState=loaded\nActiveState=active\n"
            if "--property=ControlGroup" in args:
                return fixture.group.encode()
            if "--property=MainPID" in args:
                return b"222"
        if args[:2] == ["/usr/bin/systemctl", "stop"]:
            fixture.stopped.append(args[-1])
            fixture.members.write_text("")
            return b""
        return original(args, **kwargs)

    fixture.engine.run = runner
    try:
        with mock.patch.object(remote.pwd, "getpwnam", return_value=types.SimpleNamespace(pw_uid=4242)):
            yield fixture
    finally:
        fixture.tearDown()


def test_finish_stops_only_accepted_unit_and_preserves_data_receipts_and_other_stage(accepted):
    before = remote.tree_digest(accepted.engine.release)
    result = accepted.engine.execute(dict(accepted.data, action="finish_stage"))
    assert accepted.stopped == [accepted.unit]
    assert result["stage_cleanup"]["cgroup_empty"] is True
    assert accepted.other.read_text() == "333\n"
    assert remote.tree_digest(accepted.engine.release) == before
    assert (accepted.engine.record / "staged.json").exists()
    assert (accepted.engine.record / "finished-stage.json").exists()
    assert "finish_accepted_stage_unit" in accepted.boundaries


def test_finish_rejects_unaccepted_or_wrong_group_without_stopping_anything(accepted):
    staged = json.loads((accepted.engine.record / "staged.json").read_bytes())
    staged["journeys_sha256"] = "0" * 64
    (accepted.engine.record / "staged.json").write_bytes(remote.encoded(staged))
    with pytest.raises(remote.ReleaseError, match="accepted_stage_cleanup_required"):
        accepted.engine.execute(dict(accepted.data, action="finish_stage"))
    assert not accepted.stopped


def test_finish_never_stops_a_process_that_is_in_the_wrong_kernel_cgroup(accepted):
    (accepted.root / "proc/222/cgroup").write_text("0::/system.slice/stock-app.service\n")
    with pytest.raises(remote.ReleaseError, match="stage_cgroup_identity_mismatch"):
        accepted.engine.execute(dict(accepted.data, action="finish_stage"))
    assert not accepted.stopped


def test_finish_reconciles_again_after_permission_before_stop(accepted):
    def permit(nonce, label):
        if label == "finish_accepted_stage_unit":
            (accepted.root / "proc/222/status").write_text("Uid:\t0\t0\t0\t0\n")
        return nonce

    accepted.engine.permit = permit
    with pytest.raises(remote.ReleaseError, match="stage_cgroup_identity_mismatch"):
        accepted.engine.execute(dict(accepted.data, action="finish_stage"))
    assert not accepted.stopped


def test_finish_records_an_already_collected_empty_unit_without_touching_other_units(accepted):
    accepted.members.write_text("")
    runner = accepted.engine.run

    def missing(args, **kwargs):
        if args[:2] == ["/usr/bin/systemctl", "show"] and args[-1] == accepted.unit:
            return b"LoadState=not-found\nActiveState=inactive\n"
        return runner(args, **kwargs)

    accepted.engine.run = missing
    accepted.engine.execute(dict(accepted.data, action="finish_stage"))
    assert not accepted.stopped
    assert accepted.other.read_text() == "333\n"
