"""Backup/restore receipt observation is read-only, bounded and never raises."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from server import operations as ops


@pytest.fixture(autouse=True)
def isolated_observer(monkeypatch):
    monkeypatch.setattr(ops, "_observation", ops.Observation())
    monkeypatch.setattr(ops, "release_payload", lambda: {"version": "v0.25.0", "deployed_revision": ""})


@pytest.fixture
def state(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr(ops, "BACKUP_STATE", root)
    return root


def receipt(**overrides):
    value = {
        "schema": "prism.backup.receipt.v1",
        "file": "prism-20261120T120000Z.tar.gz",
        "sha256": "a" * 64,
        "bytes": 1234,
        "created_at": 1763600000,
    }
    value.update(overrides)
    return value


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def test_valid_receipts_are_observed_with_key_fields(state):
    write(state / "last-backup.json", receipt(cos={"uploaded": True}))
    write(
        state / "last-restore.json",
        receipt(file="prism-20261119T120000Z.tar.gz", created_at=1763500000, finished_at=1763500010),
    )

    observation = ops.backup_observation()

    assert observation["status"] == "ok"
    assert observation["last_backup"]["file"] == "prism-20261120T120000Z.tar.gz"
    assert observation["last_backup"]["sha256"] == "a" * 64
    assert observation["last_backup"]["bytes"] == 1234
    assert observation["last_backup"]["created_at"] == 1763600000
    assert observation["last_backup"]["cos"] == {"uploaded": True}
    assert observation["last_restore"]["file"] == "prism-20261119T120000Z.tar.gz"
    assert observation["last_restore"]["finished_at"] == 1763500010


def test_missing_receipts_are_reported_not_raised(state):
    observation = ops.backup_observation()

    assert observation["status"] == "missing"
    assert observation["last_backup"] == {
        "status": "missing",
        "path": str(state / "last-backup.json"),
    }
    assert observation["last_restore"] == {
        "status": "missing",
        "path": str(state / "last-restore.json"),
    }


def test_corrupt_or_foreign_content_is_not_ok_and_never_raises(state):
    (state / "last-backup.json").write_text("{not json", encoding="utf-8")
    write(state / "last-restore.json", {"schema": "some.other.schema", "file": "x"})

    observation = ops.backup_observation()

    assert observation["status"] == "missing"
    assert observation["last_backup"]["status"] == "missing"
    assert observation["last_restore"]["status"] == "missing"


def test_deeply_nested_content_is_not_ok_and_never_raises(state):
    (state / "last-backup.json").write_text("[" * 20_000 + "]" * 20_000, encoding="utf-8")
    write(state / "last-restore.json", receipt())

    observation = ops.backup_observation()

    assert observation["status"] == "missing"
    assert observation["last_backup"]["status"] == "missing"
    assert observation["last_restore"]["status"] == "ok"


def test_wrong_field_types_are_rejected(state):
    write(state / "last-backup.json", receipt(bytes="1234"))
    write(state / "last-restore.json", receipt(created_at="yesterday"))

    observation = ops.backup_observation()

    assert observation["status"] == "missing"
    assert observation["last_backup"]["status"] == "missing"
    assert observation["last_restore"]["status"] == "missing"


def test_unreadable_receipt_is_reported_not_raised(state, monkeypatch):
    write(state / "last-backup.json", receipt())
    write(state / "last-restore.json", receipt())

    def deny(*args, **kwargs):
        raise PermissionError(13, "synthetic permission denied")

    monkeypatch.setattr(Path, "open", deny)
    observation = ops.backup_observation()

    assert observation["status"] == "unreadable"
    assert observation["last_backup"]["status"] == "unreadable"
    assert observation["last_restore"]["status"] == "unreadable"


async def test_operations_payload_exposes_backup_observation(state, monkeypatch):
    monkeypatch.setattr(ops, "_read_github", AsyncMock(return_value=([], [])))

    payload = await ops.operations_payload()

    assert payload["backup"]["status"] == "missing"
    assert payload["backup"]["last_backup"]["path"] == str(state / "last-backup.json")
