"""The UI only trusts a protected executor projection for the exact served commit."""

import json
import os
import time
from unittest.mock import AsyncMock

import pytest

from server import operations as ops

NON_PRODUCTION_STAGES = (
    "finish_stage",
    "diagnose_build",
    "quarantine_failed_prepare",
    "integration_astock",
    "integration_cli",
    "integration_live",
    "integration_market",
    "validate_legacy_retirement",
    "validate_legacy_stop",
    "legacy_drain",
    "switch_stopped",
)


@pytest.fixture
def record_path(tmp_path, monkeypatch):
    path = tmp_path / "status.json"
    monkeypatch.setattr(ops, "RELEASE_STATUS", path)
    monkeypatch.setattr(ops, "RELEASE_STATUS_OWNER", os.getuid())
    return path


def record(path, **values):
    payload = {
        "release_id": "prism-20261021-test",
        "commit": "a" * 40,
        "archive_sha256": "b" * 64,
        "stage": "resume",
        "status": "completed",
        "outcome": "completed",
        "updated_at": int(time.time()),
        "cutover_occurred": True,
        "production_verified": True,
        "steps": [{"stage": "switch", "status": "started", "at": int(time.time())}],
        **values,
    }
    path.write_text(json.dumps(payload))
    path.chmod(0o600)


def test_only_bounded_safe_projection_is_returned(record_path):
    record(record_path, private_secret="never-display", receipts={"private_data": "never-display"})
    result = ops.release_observation()
    assert result["status"] == "ok"
    assert result["record"]["production_verified"] is True
    assert "never-display" not in json.dumps(result)
    assert result["url"] == "https://github.com/doubleh-ai/prism/issues/9"


@pytest.mark.parametrize("mode", ["symlink", "writable", "wrong_owner", "invalid_identity", "oversize"])
def test_untrusted_or_unsafe_record_does_not_certify_release(record_path, monkeypatch, mode):
    record(record_path)
    if mode == "symlink":
        target = record_path.with_name("target.json")
        record_path.rename(target)
        record_path.symlink_to(target)
    if mode == "writable":
        record_path.chmod(0o666)
    if mode == "wrong_owner":
        monkeypatch.setattr(ops, "RELEASE_STATUS_OWNER", os.getuid() + 1)
    if mode == "invalid_identity":
        record(record_path, commit="bad")
    if mode == "oversize":
        record_path.write_text(" " * 128001)
    result = ops.release_observation()
    assert result["status"] == "unavailable"
    assert result["record"] is None


async def test_success_record_requires_same_served_revision(record_path, monkeypatch):
    record(record_path)
    monkeypatch.setattr(ops, "_read_github", AsyncMock(return_value=([], [])))
    monkeypatch.setattr(ops, "_observation", ops.Observation())
    monkeypatch.setattr(ops, "OPERATIONS_ENVIRONMENT", "production")
    monkeypatch.setattr(ops, "release_payload", lambda: {"version": "v0.20.0", "deployed_revision": "c" * 40})
    assert not (await ops.operations_payload())["runtime"]["production_verified"]
    monkeypatch.setattr(ops, "release_payload", lambda: {"version": "v0.20.0", "deployed_revision": "a" * 40})
    value = await ops.operations_payload()
    assert value["runtime"]["production_verified"]
    assert not value["delivery"]["can_deploy"]
    assert not value["delivery"]["can_rollback"]


def test_stalled_running_record_is_stale(record_path):
    record(record_path, stage="prepare", status="running", updated_at=int(time.time()) - 700)
    assert ops.release_observation()["status"] == "stale"
    assert not ops.release_observation()["record"]["production_verified"]


def test_internal_verification_does_not_certify_closed_public_entry(record_path):
    record(record_path, stage="verified")
    result = ops.release_observation()
    assert result["status"] == "ok"
    assert not result["record"]["production_verified"]


@pytest.mark.parametrize("stage", NON_PRODUCTION_STAGES)
@pytest.mark.parametrize("status", ["completed", "success"])
async def test_validation_stages_and_green_ci_never_certify_production(
    record_path, monkeypatch, stage, status
):
    checked_at = int(time.time())
    record(
        record_path,
        stage=stage,
        status=status,
        cutover_occurred=False,
        # Even an inconsistent upstream flag cannot promote a validation phase.
        production_verified=True,
        last_completed_stage=stage,
        updated_at=checked_at,
        steps=[{"stage": stage, "status": status, "at": checked_at}],
    )
    monkeypatch.setattr(ops, "_observation", ops.Observation())
    monkeypatch.setattr(ops, "OPERATIONS_ENVIRONMENT", "production")
    monkeypatch.setattr(
        ops, "release_payload", lambda: {"version": "synthetic-local-build", "deployed_revision": "a" * 40}
    )
    monkeypatch.setattr(
        ops,
        "_read_github",
        AsyncMock(
            return_value=(
                [
                    {"kind": "ci", "status": "completed", "conclusion": "success"},
                    {"kind": "candidate", "status": "completed", "conclusion": "success"},
                ],
                [],
            )
        ),
    )
    value = await ops.operations_payload()
    projected = value["production"]["record"]
    assert value["production"]["status"] == "ok"
    assert projected["stage"] == projected["last_completed_stage"] == stage
    assert projected["steps"] == [{"stage": stage, "status": status, "at": projected["updated_at"]}]
    assert projected["production_verified"] is False
    assert value["runtime"]["production_verified"] is False
    assert value["github"]["status"] == "ok"
    assert all(run["conclusion"] == "success" for run in value["github"]["runs"])
    assert value["delivery"]["can_deploy"] is False and value["delivery"]["can_rollback"] is False
