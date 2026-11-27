"""Real SQLite durability and drain tests; synthetic users, no models/network."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from server.agent import diagnostic_delivery as delivery
from server.agent import store
from server.database import get_db, reset_connection
from server.work_lifecycle import Manager


@pytest.fixture
def owner():
    with get_db() as db:
        db.execute("INSERT INTO users(username,password_hash) VALUES('diagnostic-owner','synthetic-only')")
        db.execute(
            "INSERT INTO conversations(id,username) VALUES('diagnostic-conversation','diagnostic-owner')"
        )
    store.init_agent_db()
    return "diagnostic-owner"


def admitted(owner, *, status="failed", code="provider_timeout"):
    run = store.create_run(owner, "diagnostic-conversation", "PRIVATE_QUESTION", "synthetic", "research")
    if status:
        store.finish_run(owner, run["id"], status, "PRIVATE_ANSWER", code)
    return run["id"]


def rows():
    with get_db() as db:
        return [dict(row) for row in db.execute("SELECT * FROM diagnostic_events ORDER BY id")]


async def test_terminal_survives_projection_failure_and_restart_without_duplicate(owner, monkeypatch):
    run_id = admitted(owner)
    original = delivery.record_diagnostic

    def fail(**kwargs):
        raise RuntimeError("PRIVATE_SINK_FAILURE")

    monkeypatch.setattr(delivery, "record_diagnostic", fail)
    with pytest.raises(RuntimeError):
        delivery.deliver_pending()
    assert store.get_run(owner, run_id)["status"] == "failed"
    assert delivery.pending_count() == 1 and not rows()
    # Restart the connection and construct a fresh worker from persisted data.
    reset_connection()
    monkeypatch.setattr(delivery, "record_diagnostic", original)
    manager = Manager()
    worker = delivery.DiagnosticDelivery(manager)
    assert worker.tick() == 1 and worker.tick() == 0
    assert worker.health()["pending"] == 0
    assert len(rows()) == 1 and rows()[0]["occurrences"] == 1
    assert "PRIVATE_" not in json.dumps(rows())
    manager.begin_drain(manager.ticket())
    await manager.wait_drained(timeout=1)
    manager.close_development()


def test_counter_and_receipt_rollback_together_if_delivery_ack_fails(owner):
    admitted(owner)
    with get_db() as db:
        db.execute("""CREATE TRIGGER fail_diagnostic_ack BEFORE UPDATE ON native_agent_diagnostic_delivery
                      BEGIN SELECT RAISE(ABORT,'synthetic receipt failure'); END""")
    with pytest.raises(Exception, match="synthetic receipt failure"):
        delivery.deliver_pending()
    assert not rows() and delivery.pending_count() == 1
    with get_db() as db:
        db.execute("DROP TRIGGER fail_diagnostic_ack")
    assert delivery.deliver_pending() == 1
    assert delivery.deliver_pending() == 0 and rows()[0]["occurrences"] == 1


def test_concurrent_consumers_count_each_actual_run_once(owner):
    for _ in range(4):
        admitted(owner)

    def consume():
        try:
            return delivery.deliver_pending()
        finally:
            reset_connection()

    with ThreadPoolExecutor(max_workers=2) as pool:
        counts = list(pool.map(lambda _: consume(), range(2)))
    assert sum(counts) == 4 and rows()[0]["occurrences"] == 4
    assert delivery.pending_count() == 0


@pytest.mark.parametrize(
    "status,code,kind,severity",
    [
        ("completed", None, None, None),
        ("cancelled", "cancelled_by_user", None, None),
        ("cancelled", "cancelled_before_execution", None, None),
        ("cancelled", "execution_interrupted_unknown", "server_native_run_incomplete", "warning"),
        ("partial", "output_incomplete", "server_native_run_incomplete", "warning"),
        ("failed", "budget_exceeded", "server_native_run_incomplete", "warning"),
        ("failed", "private_unknown", "server_native_run_failed", "error"),
    ],
)
def test_projection_preserves_classification_and_private_data_boundary(owner, status, code, kind, severity):
    run_id = admitted(owner, status=status, code=code)
    assert delivery.deliver_pending() == 1 and delivery.deliver_pending() == 0
    recorded = rows()
    assert len(recorded) == (0 if kind is None else 1)
    if kind is not None:
        assert recorded[0]["kind"] == kind and recorded[0]["severity"] == severity
    assert "PRIVATE_" not in json.dumps(recorded)
    assert "private_unknown" not in json.dumps(recorded)
    assert store.get_run(owner, run_id)["status"] == status


def test_release_provenance_and_event_time_survive_delayed_delivery(owner, monkeypatch):
    monkeypatch.setattr(store, "APP_VERSION", "v0.23.7")
    run_id = admitted(owner)
    with get_db() as db:
        db.execute("UPDATE native_agent_runs SET finished_at=1793700000 WHERE run_id=?", [run_id])
    monkeypatch.setattr(store, "APP_VERSION", "v0.24.0")
    assert delivery.deliver_pending() == 1
    event = rows()[0]
    assert event["version"] == "v0.23.7"
    assert event["first_seen"] == event["last_seen"] == 1793700000


def test_exclusive_startup_interruption_becomes_warning_without_restarting_model(owner):
    run_id = admitted(owner, status=None)
    store.claim_run(owner, run_id, "synthetic-executor")
    assert store.recover_interrupted_runs(exclusive_startup=True) == 1
    assert delivery.deliver_pending() == 1
    event = rows()[0]
    assert event["message"] == "Native outcome: execution_interrupted_unknown"
    assert event["kind"] == "server_native_run_incomplete" and event["severity"] == "warning"
    assert store.get_run(owner, run_id)["status"] == "partial"


def test_legacy_runs_without_release_provenance_are_not_backfilled(owner):
    run_id = admitted(owner)
    with get_db() as db:
        db.execute("DELETE FROM native_agent_diagnostic_delivery WHERE run_id=?", [run_id])
    store.init_agent_db()
    assert delivery.deliver_pending() == 0 and not rows()


def test_admission_and_release_provenance_cannot_commit_separately(owner):
    with get_db() as db:
        db.execute("""CREATE TRIGGER fail_diagnostic_birth BEFORE INSERT ON native_agent_diagnostic_delivery
                      BEGIN SELECT RAISE(ABORT,'synthetic provenance failure'); END""")
    with pytest.raises(Exception, match="synthetic provenance failure"):
        admitted(owner, status=None)
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM native_agent_runs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM native_run_profiles").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM native_agent_diagnostic_delivery").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["disabled", "soft_deleted", "deleted"])
def test_removed_or_inactive_owner_does_not_regenerate_private_diagnostics(owner, change):
    run_id = admitted(owner)
    with get_db() as db:
        if change == "disabled":
            db.execute("UPDATE users SET disabled_at=1 WHERE username=?", [owner])
        elif change == "soft_deleted":
            db.execute("UPDATE conversations SET deleted_at=1 WHERE id='diagnostic-conversation'")
        else:
            db.execute("DELETE FROM conversations WHERE id='diagnostic-conversation'")
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM native_agent_diagnostic_delivery WHERE run_id=?", [run_id]
                ).fetchone()[0]
                == 0
            )
    assert delivery.deliver_pending() == 0 and not rows()


def test_resolved_incident_is_not_reopened_by_an_already_delivered_run(owner):
    admitted(owner)
    delivery.deliver_pending()
    with get_db() as db:
        db.execute("UPDATE diagnostic_events SET resolved_at=1")
    assert delivery.deliver_pending() == 0
    assert rows()[0]["resolved_at"] == 1 and rows()[0]["occurrences"] == 1
    admitted(owner)
    delivery.deliver_pending()
    assert len(rows()) == 2 and rows()[1]["resolved_at"] is None


def test_bounded_batches_and_record_owner_cannot_be_widened(owner):
    first = admitted(owner)
    admitted(owner)
    assert delivery.deliver_pending(run_id=first, username="foreign-owner", limit=1) == 0
    assert delivery.pending_count() == 2
    assert delivery.deliver_pending(limit=1) == 1 and delivery.pending_count() == 1
    assert delivery.deliver_pending() == 1 and rows()[0]["occurrences"] == 2
    for arguments in ({"limit": True}, {"limit": 51}, {"run_id": first}, {"username": owner}):
        with pytest.raises(ValueError):
            delivery.deliver_pending(**arguments)


async def test_scheduler_does_not_project_during_maintenance_and_releases_idle_work(owner):
    admitted(owner)
    manager = Manager()
    worker = delivery.DiagnosticDelivery(manager)
    manager.begin_drain(manager.ticket())
    assert worker.tick() == 0 and worker.status == "maintenance"
    assert delivery.pending_count() == 1 and not rows()
    await manager.wait_drained(timeout=1)
    assert manager.observe()["blocked"] == 0
    manager.close_development()


async def test_scheduler_stops_and_health_failure_is_unknown(owner, monkeypatch):
    admitted(owner)
    manager = Manager()
    worker = delivery.DiagnosticDelivery(manager)
    worker.start()
    await asyncio.sleep(0)
    assert worker.health()["status"] == "ready" and len(rows()) == 1
    await worker.close()
    assert worker.task is None and worker.health()["status"] == "stopped"

    def unavailable():
        raise RuntimeError("PRIVATE_SQLITE_ERROR")

    monkeypatch.setattr(delivery, "pending_count", unavailable)
    assert worker.health()["status"] == "unavailable" and worker.health()["pending"] is None
    manager.begin_drain(manager.ticket())
    await manager.wait_drained(timeout=1)
    manager.close_development()


async def test_failed_sql_batch_releases_only_after_actual_rollback(owner):
    admitted(owner)
    manager = Manager()
    worker = delivery.DiagnosticDelivery(manager)
    with get_db() as db:
        db.execute("""CREATE TRIGGER fail_delivery_tick BEFORE INSERT ON diagnostic_events
                      BEGIN SELECT RAISE(ABORT,'synthetic projection failure'); END""")
    assert worker.tick() == 0
    assert worker.health()["status"] == "unavailable" and delivery.pending_count() == 1
    assert manager.observe()["blocked"] == 0 and not any(manager.observe()["leases"].values())
    with get_db() as db:
        assert not db.in_transaction
        db.execute("DROP TRIGGER fail_delivery_tick")
    assert worker.tick() == 1 and rows()[0]["occurrences"] == 1
    manager.begin_drain(manager.ticket())
    await manager.wait_drained(timeout=1)
    manager.close_development()


def test_unconfirmed_sql_transaction_keeps_the_drain_blocked(owner, monkeypatch):
    manager = Manager()
    worker = delivery.DiagnosticDelivery(manager)

    def uncertain():
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
        raise RuntimeError("synthetic unknown rollback")

    monkeypatch.setattr(delivery, "deliver_pending", uncertain)
    try:
        assert worker.tick() == 0 and manager.observe()["blocked"] == 1
    finally:
        # Test cleanup is not a production reconciliation or drain receipt.
        with get_db() as db:
            db.execute("ROLLBACK")
        manager._pool.shutdown(wait=True)
