"""Offline policy/wiring evidence only; no Linux, BPF or provider success claim."""

import hashlib
import json
import stat
import types

import pytest
import test_live_integration_validation as live_tests
from test_market_live_validation import engine as market_engine
from test_network_gate_wiring import ProofGate

from scripts import live_integration_validation as live
from scripts import market_live_validation as market
from scripts import remote_release as remote

live_value = live_tests.validation


@pytest.fixture(params=("live", "market"))
def context(request, tmp_path, live_value):
    if request.param == "live":
        value = live_value
        value.work.mkdir(parents=True)
        return live, value
    return market, market_engine(tmp_path)[0]


def test_stop_and_cleanup_evidence_is_durable_private_and_digest_bound(context):
    module, value = context
    value.prepare_network_stop()
    path = value.work / "network-gate-stop.json"
    before = path.read_bytes()
    assert hashlib.sha256(before).hexdigest() == value.report["network_gate_stop_proof_sha256"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(before)["profile"] == "linux-6.8"
    # A second call cannot manufacture a new active witness after natural exit.
    value.network_gate.prepare_stop = lambda: pytest.fail("do not re-probe after ack")
    value.prepare_network_stop()
    assert path.read_bytes() == before
    value.cleanup_network_gate()
    if value.reference_roles == ("broker", "worker"):
        assert value.report["network_gate_cleaned"] is False
        value.release_unit_reference("worker")
    cleanup = value.work / "network-gate-cleanup.json"
    assert (
        hashlib.sha256(cleanup.read_bytes()).hexdigest() == value.report["network_gate_cleanup_proof_sha256"]
    )
    assert stat.S_IMODE(cleanup.stat().st_mode) == 0o600
    assert value.report["network_gate_cleaned"] is True
    assert value.report["network_gate_objects_released"] is True
    assert value.report["network_gate_cleanup_mode"] == "retained_empty"
    assert value.report["network_gate_diagnostic"] == {"stage": "complete", "command": None, "errno": None}


def test_existing_proof_is_never_replaced_or_treated_as_verified(context):
    module, value = context
    path = value.work / "network-gate-stop.json"
    path.write_bytes(b"previous evidence must remain")
    with pytest.raises(module.ValidationError, match="network_gate_rejected"):
        value.prepare_network_stop()
    assert path.read_bytes() == b"previous evidence must remain"
    assert value.report["network_gate_stop_proof_sha256"] is None
    assert not value.report["network_gate_cleaned"]


@pytest.mark.parametrize(
    "bad", ["none", "extra", "partial_released", "incomplete_four", "different_stop", "short"]
)
def test_cleanup_requires_typed_actual_gate_proof_not_mock_success(context, bad):
    module, value = context
    value.prepare_network_stop()
    proof = ProofGate().cleanup()
    if bad == "none":
        proof = None
    elif bad == "extra":
        proof["raw_error"] = "public unsafe fixture"
    elif bad == "partial_released":
        proof["complete"] = False
    elif bad == "incomplete_four":
        proof.update(complete=False, objects_released=False, mode="partial_closed")
    elif bad == "different_stop":
        proof["stop_verified_at_ns"] += 1
    else:
        proof["program_count"] = 3
    value.network_gate.cleanup = lambda: proof
    with pytest.raises(module.ValidationError, match="network_gate_rejected"):
        value.cleanup_network_gate()
    assert not value.report["network_gate_cleaned"]
    assert value.report["network_gate_objects_released"] is False
    if bad == "incomplete_four":
        # A partial install may own four FDs. Preserve that evidence, not success.
        assert value.report["network_gate_cleanup_mode"] == "partial_closed"
        assert (value.work / "network-gate-cleanup.json").is_file()
    else:
        assert not (value.work / "network-gate-cleanup.json").exists()


def test_failed_phase_without_errno_is_not_erased_by_successful_cleanup(context):
    _, value = context
    value.network_gate.diagnostic = lambda: {"stage": "verify", "command": None, "errno": None}
    value.network_diagnostic(RuntimeError("public synthetic failure"))
    value.network_gate = ProofGate()
    value.network_diagnostic()
    assert value.report["network_gate_diagnostic"] == {"stage": "verify", "command": None, "errno": None}


def test_constructor_diagnostic_is_finite_and_retains_actual_syscall_errno(context):
    module, value = context
    value.network_gate = None
    value.network_module = types.SimpleNamespace(
        safe_diagnostic=lambda error: {"stage": "construct", "command": 16, "errno": 22}
    )
    value.network_diagnostic(RuntimeError("must not appear in safe output"))
    value.network_gate = ProofGate()
    value.network_diagnostic()
    assert value.report["network_gate_diagnostic"] == {"stage": "construct", "command": 16, "errno": 22}
    assert "must not appear" not in json.dumps(value.report)


@pytest.mark.parametrize(
    "diagnostic",
    [
        {"stage": "arbitrary secret", "command": None, "errno": None},
        {"stage": "attach", "command": True, "errno": 1},
        {"stage": "attach", "command": 4, "errno": 1},
        {"stage": "attach", "command": 8, "errno": True},
        {"stage": "attach", "command": 8, "errno": 4096},
        {"stage": "attach", "command": 8, "errno": 1, "raw": "unsafe"},
    ],
)
def test_untrusted_diagnostic_cannot_escape_fixed_safe_shape(context, diagnostic):
    _, value = context
    value.network_gate.diagnostic = lambda: diagnostic
    value.network_diagnostic()
    assert value.report["network_gate_diagnostic"] == {"stage": "unknown", "command": None, "errno": None}


@pytest.mark.parametrize("prepare_fails", [False, True])
def test_live_cleanup_stops_broker_then_releases_gate_before_worker(live_value, monkeypatch, prepare_fails):
    value = live_value
    value.work.mkdir(parents=True)
    events = []
    value.launched = {"broker", "worker"}
    value.invocations = {role: ("c" * 32, pid, 500) for role, pid in value.pids.items()}
    monkeypatch.setattr(live, "startticks", lambda path: 500)
    monkeypatch.setattr(
        value,
        "show",
        lambda role, **kw: {
            "InvocationID": "c" * 32,
            "ExecMainPID": str(value.pids[role]),
            "MainPID": str(value.pids[role]),
        },
    )
    original_prepare = value.network_gate.prepare_stop
    original_cleanup = value.network_gate.cleanup

    def prepare():
        events.append("active_exact_four_hooks")
        if prepare_fails:
            raise RuntimeError("public fixture read-only gate failure")
        return original_prepare()

    def cleanup():
        events.append("gate_cleanup")
        return original_cleanup()

    def run(argv, **kwargs):
        if argv[1] == "stop":
            events.append("stop_broker" if argv[-1] == value.units["broker"] else "stop_worker")
        return b"MainPID=0\n"

    value.network_gate.prepare_stop, value.network_gate.cleanup = prepare, cleanup
    value._run = run
    value.cleanup()
    assert events == ["active_exact_four_hooks", "stop_broker", "gate_cleanup", "stop_worker"]
    assert value.report["units_stopped"] is True
    assert value.report["network_gate_cleaned"] is (not prepare_fails)
    assert value.report["cleanup_error_class"] == ("cleanup_failed" if prepare_fails else None)
    assert value.report["network_gate_objects_released"] is (not prepare_fails)


def safe_cleanup():
    return {
        "network_gate_diagnostic": {"stage": "complete", "command": None, "errno": None},
        "network_gate_stop_proof_sha256": "a" * 64,
        "network_gate_cleanup_proof_sha256": "b" * 64,
        "network_gate_cleanup_mode": "collected_offline",
        "network_gate_objects_released": True,
        "unit_reference_acquire_sha256": "c" * 64,
        "unit_reference_stop_sha256": "d" * 64,
        "unit_reference_release_sha256": "e" * 64,
        "unit_reference_released": True,
        "unit_reference_error_class": None,
        "network_gate_cleaned": True,
    }


@pytest.mark.parametrize(
    "change",
    [
        "missing_stop",
        "missing_cleanup",
        "partial",
        "released_false",
        "cleaned_false",
        "sha_bool",
        "mode_list",
        "unsafe_diagnostic",
    ],
)
def test_root_safe_validator_rejects_cleanup_claim_without_all_bound_evidence(change):
    value = safe_cleanup()
    assert remote.valid_network_cleanup_record(value)
    if change == "missing_stop":
        value["network_gate_stop_proof_sha256"] = None
    elif change == "missing_cleanup":
        value["network_gate_cleanup_proof_sha256"] = None
    elif change == "partial":
        value["network_gate_cleanup_mode"] = "partial_closed"
    elif change == "released_false":
        value["network_gate_objects_released"] = False
    elif change == "cleaned_false":
        value["network_gate_cleaned"] = False
    elif change == "sha_bool":
        value["network_gate_stop_proof_sha256"] = True
    elif change == "mode_list":
        value["network_gate_cleanup_mode"] = []
    else:
        value["network_gate_diagnostic"]["raw"] = "must never persist"
    assert not remote.valid_network_cleanup_record(value)


def test_failed_attempt_can_retain_bounded_errno_and_partial_closed_proof():
    value = safe_cleanup()
    value.update(
        network_gate_diagnostic={"stage": "query_direct", "command": 16, "errno": 2},
        network_gate_stop_proof_sha256=None,
        network_gate_cleanup_mode="partial_closed",
        network_gate_objects_released=False,
        network_gate_cleaned=False,
        unit_reference_stop_sha256=None,
        unit_reference_release_sha256=None,
        unit_reference_released=False,
    )
    assert remote.valid_network_cleanup_record(value)
