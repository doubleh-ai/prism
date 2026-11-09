"""Actual private-reader API with synthetic records; no raw context disclosure."""

import json
import uuid
from typing import get_args

import pytest

from server import diagnostics, feedback, repair_scopes
from server.database import get_db

REVISION = "f" * 40
MONITOR = "synthetic-attribution-monitor-000000000001"
WORKER = "synthetic-attribution-worker-0000000000001"
ROOT = "/api/internal/feedback-monitor"


@pytest.fixture(autouse=True)
def configure(temp_data_dir, monkeypatch):
    monkeypatch.setenv("PRISM_FEEDBACK_MONITOR_TOKEN", MONITOR)
    monkeypatch.setenv("PRISM_FEEDBACK_WORKER_TOKEN", WORKER)
    monkeypatch.setattr(feedback, "_read_deployed_revision", lambda: REVISION)
    monkeypatch.setattr(diagnostics, "_read_deployed_revision", lambda: REVISION)
    feedback.init_feedback_db()


async def submit(client, auth_cookies, **fields):
    return await client.post(
        "/api/feedback",
        cookies=auth_cookies,
        json={
            "category": "bug",
            "title": "private synthetic title",
            "description": "private synthetic description",
            "page": "/private-owner-record?secret=discarded",
            "feature": "futures",
            **fields,
        },
    )


def test_scope_wire_contract_is_closed_and_does_not_return_arbitrary_routes():
    assert set(get_args(repair_scopes.RepairScope)) == set(repair_scopes.SCOPE_FEATURES) | {"unknown"}
    for value in (None, {}, [], 1, "futures-gold extra", "private-user-identifier", "__proto__"):
        assert repair_scopes.normalize_scope(value) == "unknown"
    assert repair_scopes.scope_from_api_route("/api/futures/AU/snapshot") == "futures-interface"
    assert repair_scopes.scope_from_api_route("/api/chat/owned-private-id") == "research-runtime"
    assert repair_scopes.scope_from_api_route("https://example.invalid/api/chat") == "unknown"
    assert repair_scopes.scope_from_api_route("/api/chat?token=synthetic") == "unknown"


async def test_scope_survives_submission_and_reader_emits_only_the_closed_hint(client, auth_cookies):
    response = await submit(client, auth_cookies, repair_scope="futures-gold")
    assert response.status_code == 200
    item = response.json()["feedback"]
    assert item["repair_scope"] == "futures-gold"
    result = await client.get(ROOT + "/queue", headers={"X-Prism-Monitor-Token": MONITOR})
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    signal = result.json()["items"][0]
    assert signal["repair_scope"] == "futures-gold" and signal["feature"] == "futures"
    assert signal["revision"] == REVISION[:12]
    assert not {"title", "description", "page", "triage", "username", "context", "stack"} & signal.keys()
    assert "private synthetic" not in result.text and "private-owner" not in result.text


@pytest.mark.parametrize(
    "fields",
    [
        {"repair_scope": "run-this-command"},
        {"repair_scope": {"id": "futures-gold"}},
        {"repair_scope": "profile-risk"},
        {"repair_scope": "unknown", "arbitrary_scope": "futures-gold"},
    ],
)
async def test_arbitrary_or_cross_feature_scope_does_not_create_a_record(client, auth_cookies, fields):
    r = await submit(client, auth_cookies, **fields)
    assert r.status_code == 422
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM feedback_items").fetchone()[0] == 0


async def test_scope_is_part_of_idempotency_but_legacy_unknown_keeps_original_fingerprint(
    client, auth_cookies
):
    request_id = str(uuid.uuid4())
    first = await submit(client, auth_cookies, request_id=request_id, repair_scope="futures-gold")
    again = await submit(client, auth_cookies, request_id=request_id, repair_scope="futures-gold")
    changed = await submit(client, auth_cookies, request_id=request_id, repair_scope="futures-soybean")
    assert first.status_code == again.status_code == 200 and changed.status_code == 409
    assert first.json()["feedback"]["id"] == again.json()["feedback"]["id"]
    legacy_id = str(uuid.uuid4())
    legacy = await submit(client, auth_cookies, request_id=legacy_id)
    retry = await submit(client, auth_cookies, request_id=legacy_id, repair_scope="unknown")
    assert legacy.status_code == retry.status_code == 200
    assert legacy.json()["feedback"]["id"] == retry.json()["feedback"]["id"]


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        '{"repair_scope":["futures-gold"]}',
        '{"repair_scope":"private-user-id"}',
        '{"repair_scope":"futures-gold","padding":"' + "x" * 8192 + '"}',
    ],
)
async def test_reader_drops_malformed_or_oversized_metadata_without_reading_private_fields(
    client, auth_cookies, raw
):
    r = await submit(client, auth_cookies)
    assert r.status_code == 200
    with get_db() as db:
        db.execute("UPDATE feedback_items SET triage_json=?", [raw])
    response = await client.get(ROOT + "/queue", headers={"X-Prism-Monitor-Token": MONITOR})
    assert response.status_code == 200
    assert "repair_scope" not in response.json()["items"][0]
    assert "private-user" not in response.text


async def test_diagnostic_server_stamps_revision_and_does_not_export_paths_context_or_stack(client):
    diagnostics.record_diagnostic(
        kind="server_exception",
        component="api",
        page="/private/owned-id",
        message="TypeError: private-message",
        stack="private-stack",
        context={
            "repair_scope": "futures-gold",
            "server_revision": "e" * 40,
            "recorded_revision": "d" * 40,
            "resource": "private-resource",
            "mode": "futures",
        },
    )
    r = await client.get(ROOT + "/diagnostics", headers={"X-Prism-Monitor-Token": MONITOR})
    assert r.status_code == 200
    item = r.json()["items"][0]
    assert item["repair_scope"] == "futures-gold" and item["revision"] == REVISION[:12]
    assert not {"page", "message", "stack", "context", "resource", "server_revision"} & item.keys()
    assert "private" not in r.text
    assert "d" * 40 not in r.text and "e" * 40 not in r.text


async def test_api_route_scope_overrides_client_hint_and_scopes_do_not_dedupe_together(client):
    a = diagnostics.record_diagnostic(
        kind="server_exception",
        message="TypeError: synthetic",
        context={"route": "/api/futures/AU/snapshot", "repair_scope": "profile-risk"},
    )
    b = diagnostics.record_diagnostic(
        kind="server_exception", message="TypeError: synthetic", context={"repair_scope": "futures-gold"}
    )
    c = diagnostics.record_diagnostic(
        kind="server_exception", message="TypeError: synthetic", context={"repair_scope": "futures-gold"}
    )
    assert a != b == c
    r = await client.get(ROOT + "/diagnostics", headers={"X-Prism-Monitor-Token": MONITOR})
    assert {row["repair_scope"] for row in r.json()["items"]} == {"futures-interface", "futures-gold"}


async def test_context_only_carries_known_scope_and_client_cannot_stamp_recorded_revision(
    client, auth_cookies
):
    response = await client.post(
        "/api/diagnostics/client",
        cookies=auth_cookies,
        json={
            "kind": "js_error",
            "message": "TypeError: synthetic",
            "version": feedback.APP_VERSION,
            "context": {"repair_scope": "private-route-id", "recorded_revision": "d" * 40},
        },
    )
    assert response.status_code == 200
    with get_db() as db:
        raw = json.loads(db.execute("SELECT context_json FROM diagnostic_events").fetchone()[0])
    assert raw["recorded_revision"] == REVISION and "repair_scope" not in raw
    r = await client.get(ROOT + "/diagnostics", headers={"X-Prism-Monitor-Token": MONITOR})
    assert "revision" not in r.json()["items"][0]
    assert "repair_scope" not in r.json()["items"][0]


async def test_triage_text_cannot_override_the_separate_scope_column(client, auth_cookies):
    r = await submit(client, auth_cookies, repair_scope="futures-gold")
    assert r.status_code == 200
    with get_db() as db:
        db.execute("UPDATE feedback_items SET triage_json=?", ['{"repair_scope":"profile-risk"}'])
    result = await client.get(ROOT + "/queue", headers={"X-Prism-Monitor-Token": MONITOR})
    assert result.json()["items"][0]["repair_scope"] == "futures-gold"


@pytest.mark.parametrize("claimed", ["", "e" * 40, "e" * 12, None, "private-user-id"])
async def test_stale_or_missing_client_origin_is_not_restamped_as_current(client, auth_cookies, claimed):
    result = await client.post(
        "/api/diagnostics/client",
        cookies=auth_cookies,
        json={
            "kind": "js_error",
            "version": feedback.APP_VERSION,
            "message": "TypeError: synthetic old browser",
            "context": {"server_revision": claimed, "repair_scope": "futures-gold"},
        },
    )
    assert result.status_code == 200
    signals = await client.get(ROOT + "/diagnostics", headers={"X-Prism-Monitor-Token": MONITOR})
    assert signals.status_code == 200
    assert "revision" not in signals.json()["items"][0]
    with get_db() as db:
        row = db.execute("SELECT recorded_revision,origin_revision FROM diagnostic_events").fetchone()
    assert tuple(row) == (REVISION, "")


@pytest.mark.parametrize("claimed", [REVISION, REVISION[:12]])
async def test_matching_client_hint_remains_distinct_from_unattributed_origin(client, auth_cookies, claimed):
    body = {
        "kind": "js_error",
        "version": feedback.APP_VERSION,
        "message": "TypeError: synthetic browser",
        "context": {"repair_scope": "futures-gold"},
    }
    unknown = await client.post("/api/diagnostics/client", cookies=auth_cookies, json=body)
    body["context"]["server_revision"] = claimed
    current = await client.post("/api/diagnostics/client", cookies=auth_cookies, json=body)
    assert unknown.status_code == current.status_code == 200
    assert unknown.json()["id"] != current.json()["id"]
    result = await client.get(ROOT + "/diagnostics", headers={"X-Prism-Monitor-Token": MONITOR})
    items = result.json()["items"]
    assert [item.get("revision") for item in items] == [None, REVISION[:12]]
    assert all(item["occurrences"] == 1 for item in items)


@pytest.mark.parametrize("kind", ["server_exception", "server_native_run_failed", "unknown_event"])
async def test_client_cannot_claim_a_server_diagnostic_kind(client, auth_cookies, kind):
    result = await client.post(
        "/api/diagnostics/client",
        cookies=auth_cookies,
        json={"kind": kind, "message": "synthetic", "context": {"server_revision": REVISION}},
    )
    assert result.status_code == 422
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM diagnostic_events").fetchone()[0] == 0


@pytest.mark.parametrize("fields", [{"version": "v0.0.1"}, {"_occurred_at": 123.0}])
async def test_historical_or_deferred_server_outcome_does_not_gain_current_origin(client, fields):
    diagnostics.record_diagnostic(
        kind="server_native_run_failed", message="Native outcome: provider_timeout", **fields
    )
    result = await client.get(ROOT + "/diagnostics", headers={"X-Prism-Monitor-Token": MONITOR})
    assert result.status_code == 200 and len(result.json()["items"]) == 1
    assert "revision" not in result.json()["items"][0]


def test_additive_diagnostic_migration_preserves_legacy_rows_and_is_repeatable(tmp_path, monkeypatch):
    import sqlite3

    from server import database

    path = tmp_path / "legacy-diagnostics.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE diagnostic_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT DEFAULT '', kind TEXT NOT NULL,
          severity TEXT NOT NULL DEFAULT 'error', component TEXT DEFAULT '', page TEXT DEFAULT '',
          message TEXT NOT NULL, stack TEXT DEFAULT '', fingerprint TEXT NOT NULL, version TEXT DEFAULT '',
          context_json TEXT DEFAULT '{}', occurrences INTEGER NOT NULL DEFAULT 1, first_seen INTEGER NOT NULL,
          last_seen INTEGER NOT NULL, resolved_at INTEGER)""")
        db.execute(
            "INSERT INTO diagnostic_events(kind,message,fingerprint,first_seen,last_seen) VALUES(?,?,?,?,?)",
            ["js_error", "synthetic legacy row", "legacy-fingerprint", 1, 2],
        )
    database.reset_connection()
    monkeypatch.setattr(database, "DB_PATH", path)
    database.init_db()
    database.init_db()
    with database.get_db() as db:
        row = db.execute(
            "SELECT message,repair_scope,recorded_revision,origin_revision,occurrences FROM diagnostic_events"
        ).fetchone()
        assert tuple(row) == ("synthetic legacy row", "unknown", "", "", 1)
        db.execute(
            "INSERT INTO diagnostic_events(kind,message,fingerprint,first_seen,last_seen) VALUES(?,?,?,?,?)",
            ["js_error", "synthetic recovery insert", "recovery-fingerprint", 3, 4],
        )
        assert db.execute("SELECT COUNT(*) FROM diagnostic_events").fetchone()[0] == 2
    database.reset_connection()
