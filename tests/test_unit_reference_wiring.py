"""Finite reference lifetime wiring with explicit fake bus/kernel observations."""

import hashlib
import json
import stat

import pytest
import test_live_integration_validation as live_tests
from test_market_live_validation import engine as market_engine

from scripts import live_integration_validation as live
from scripts import market_live_validation as market
from scripts import remote_release as remote

live_value = live_tests.validation


@pytest.fixture(params=("live", "market"))
def context(request, tmp_path, live_value):
    if request.param == "live":
        live_value.work.mkdir(parents=True)
        return live, live_value
    return market, market_engine(tmp_path)[0]


def test_reference_lifetime_encloses_stop_witness_and_complete_bpf_release(context):
    _, value = context
    events = value.unit_references[value.reference_roles[0]].events
    gate = value.network_gate
    prepare, cleanup = gate.prepare_stop, gate.cleanup
    gate.prepare_stop = lambda: (events.append("prepare_stop"), prepare())[1]
    gate.cleanup = lambda: (events.append("gate_objects_released"), cleanup())[1]
    value.prepare_network_stop()
    value.cleanup_network_gate()
    if value.reference_roles == ("broker", "worker"):
        assert value.report["unit_reference_released"] is False
        value.release_unit_reference("worker")
    assert events == [
        "running",
        "prepare_stop",
        "running",
        "stopped",
        "gate_objects_released",
        "stopped",
        "release",
    ]
    assert value.report["network_gate_cleaned"] is True
    assert value.report["unit_reference_released"] is True
    for kind in ("stop", "release"):
        path = value.work / ("unit-reference-" + kind + ".json")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert (
            hashlib.sha256(path.read_bytes()).hexdigest()
            == value.report["unit_reference_" + kind + "_sha256"]
        )
        data = json.loads(path.read_bytes())
        assert [row["role"] for row in data["references"]] == list(value.reference_roles)
        assert all(row["proof"]["unique_name"] == ":1.234" for row in data["references"])


def test_missing_reference_blocks_permit_stop_and_bpf_success(context):
    module, value = context
    value.unit_references[value.reference_roles[0]] = None
    for action in (value.verify_network_gate, value.prepare_network_stop, value.cleanup_network_gate):
        with pytest.raises(module.ValidationError, match="network_gate_rejected"):
            action()
    assert value.report["network_gate_cleaned"] is False
    assert value.report["unit_reference_released"] is False


@pytest.mark.parametrize("phase", ["running", "stopped", "release"])
def test_connection_or_reference_drift_never_becomes_cleanup_success(context, phase):
    module, value = context
    value.prepare_network_stop()
    old_verify = value.unit_references[value.reference_roles[0]].verify

    def verify(*, stopped=False):
        proof = old_verify(stopped=stopped)
        if ("stopped" if stopped else "running") == phase:
            proof["unique_name"] = ":1.999"
        return proof

    value.unit_references[value.reference_roles[0]].verify = verify
    if phase == "release":
        value.unit_references[value.reference_roles[0]].release = lambda: {
            **value.unit_references[value.reference_roles[0]].proof("released"),
            "unique_name": ":1.999",
        }
    if phase == "running":
        action = value.verify_network_gate
    else:
        action = value.cleanup_network_gate
    with pytest.raises(module.ValidationError, match="network_gate_rejected"):
        action()
    assert value.report["unit_reference_released"] is False
    assert value.report["network_gate_cleaned"] is False
    assert not (value.work / "unit-reference-release.json").exists()


def test_gate_failure_does_not_unref_before_object_cleanup_is_proven(context):
    module, value = context
    value.prepare_network_stop()
    value.network_gate.cleanup = lambda: (_ for _ in ()).throw(RuntimeError("private fixture"))
    with pytest.raises(module.ValidationError, match="network_gate_rejected"):
        value.cleanup_network_gate()
    assert "release" not in value.unit_references[value.reference_roles[0]].events
    value.close_unit_reference()
    assert value.unit_references[value.reference_roles[0]].events[-1] == "close"
    assert not value.report["unit_reference_released"]


def test_reference_proof_does_not_replace_existing_evidence(context):
    module, value = context
    target = value.work / "unit-reference-stop.json"
    target.write_bytes(b"previous fixed evidence")
    with pytest.raises(module.ValidationError, match="network_gate_rejected"):
        value.prepare_network_stop()
    assert target.read_bytes() == b"previous fixed evidence"
    assert value.report["unit_reference_stop_sha256"] is None


def test_safe_schema_needs_reference_release_in_addition_to_bpf_release():
    from test_network68_wiring import safe_cleanup

    value = safe_cleanup()
    assert remote.valid_network_cleanup_record(value)
    for field in (
        "unit_reference_acquire_sha256",
        "unit_reference_stop_sha256",
        "unit_reference_release_sha256",
    ):
        broken = {**value, field: None}
        assert not remote.valid_network_cleanup_record(broken)
    broken = {**value, "unit_reference_released": False}
    assert not remote.valid_network_cleanup_record(broken)
    # A failed reference release may preserve real BPF object disappearance.
    broken = {
        **value,
        "unit_reference_release_sha256": None,
        "unit_reference_released": False,
        "network_gate_cleaned": False,
    }
    assert remote.valid_network_cleanup_record(broken)


def test_live_worker_natural_exit_keeps_reference_without_faking_bpf_running(live_value):
    value = live_value
    value.work.mkdir(parents=True)
    worker = value.unit_references["worker"]
    worker.exited = True
    # Fresh broker running proof remains required for every BPF witness.
    value.prepare_network_stop()
    proof = json.loads((value.work / "unit-reference-stop.json").read_bytes())
    assert [(row["role"], row["proof"]["state"]) for row in proof["references"]] == [
        ("broker", "verified_running"),
        ("worker", "verified_exited"),
    ]
    value.cleanup_network_gate()
    assert value.report["network_gate_objects_released"]
    assert not value.report["network_gate_cleaned"]
    value.release_unit_reference("worker")
    assert value.report["network_gate_cleaned"]
    assert worker.events == ["held_exited", "held_exited", "stopped", "release"]


def test_worker_reference_loss_is_checked_before_model_gate(live_value):
    value = live_value
    worker = value.unit_references["worker"]
    worker.verify_held = lambda: (_ for _ in ()).throw(RuntimeError("public disconnected fixture"))
    with pytest.raises(live.ValidationError, match="network_gate_rejected"):
        value.verify_network_gate()
    assert value.report["unit_reference_error_class"] == "reference_state_rejected"
    assert not value.report["network_gate_cleaned"]


def test_worker_reference_release_waits_for_all_owned_descendants(live_value, monkeypatch):
    value = live_value
    base = live.CGROUP / "system.slice" / value.units["worker"]
    child = base / "nested"
    child.mkdir(parents=True)
    for path in (base, child):
        (path / "cgroup.events").write_text("populated 0\n")
        (path / "cgroup.procs").write_text("")
    assert value.owned_group_empty(value.units["worker"])
    (child / "cgroup.procs").write_text("999\n")
    assert not value.owned_group_empty(value.units["worker"])
    assert not value.report["unit_reference_released"]


def test_worker_group_census_refuses_symlinks(live_value, tmp_path):
    value = live_value
    base = live.CGROUP / "system.slice" / value.units["worker"]
    base.mkdir(parents=True)
    (base / "cgroup.events").write_text("populated 0\n")
    (base / "cgroup.procs").write_text("")
    (base / "foreign").symlink_to(tmp_path / "untrusted-target")
    with pytest.raises(live.ValidationError, match="cleanup_failed"):
        value.owned_group_empty(value.units["worker"])
    with pytest.raises(live.ValidationError, match="cleanup_failed"):
        value.owned_group_empty("stock-app.service")


def test_fixed_safe_reference_errors_never_accept_arbitrary_text():
    from test_network68_wiring import safe_cleanup

    value = safe_cleanup()
    value.update(
        network_gate_cleaned=False,
        unit_reference_released=False,
        unit_reference_release_sha256=None,
        unit_reference_error_class="reference_refs_rejected",
    )
    assert remote.valid_network_cleanup_record(value)
    for unsafe in ["raw confidential error", [], True]:
        value["unit_reference_error_class"] = unsafe
        assert not remote.valid_network_cleanup_record(value)


@pytest.mark.parametrize("module", [live, market])
def test_reference_loader_executes_only_sealed_source_and_restores_dataclass_registration(
    tmp_path, monkeypatch, module
):
    import os
    import sys
    import types
    from pathlib import Path

    from test_network_gate_wiring import Proxy, root_metadata

    source = Path(module.__file__).with_name("systemd_unit_reference.py").read_bytes()
    assert hashlib.sha256(source).hexdigest() == module.UNIT_REFERENCE_SHA256
    path = tmp_path / "sealed-reference.py"
    path.write_bytes(source)
    original_fstat, original_lstat = os.fstat, Path.lstat
    monkeypatch.setattr(module, "root_locked" if module is live else "locked", lambda value: None)
    monkeypatch.setattr(module, "os", Proxy(os, fstat=lambda fd: root_metadata(original_fstat(fd))))
    monkeypatch.setattr(Path, "lstat", lambda value: root_metadata(original_lstat(value)))
    name = "_prism_owned_unit_reference_" + module.UNIT_REFERENCE_SHA256
    sentinel = types.ModuleType(name)
    monkeypatch.setitem(sys.modules, name, sentinel)
    loaded = module.load_unit_reference(path)
    assert sys.modules[name] is sentinel
    identity = loaded.ExpectedIdentity(
        role="live-worker",
        candidate_prefix="a" * 12,
        nonce="b" * 32,
        invocation_id="c" * 32,
        pid=123,
        startticks=55,
        uid=2001,
    )
    assert identity.unit == "prism-live-worker-" + "a" * 12 + "-" + "b" * 32 + ".service"
    path.write_bytes(source + b'\nraise RuntimeError("unapproved source must not run")\n')
    with pytest.raises(module.ValidationError, match="network_gate_rejected"):
        module.load_unit_reference(path)
    assert sys.modules[name] is sentinel
