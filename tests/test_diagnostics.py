from server.auth import hash_password
from server.database import create_session, get_db


def _admin_cookies():
    with get_db() as db:
        db.execute(
            "INSERT OR REPLACE INTO users(username, password_hash) VALUES (?,?)",
            ["admin", hash_password("admin-test-pass")],
        )
    return {"token": create_session("admin")}


async def test_client_diagnostic_is_redacted_and_admin_can_resolve(client, auth_cookies):
    captured = await client.post(
        "/api/diagnostics/client",
        cookies=auth_cookies,
        json={
            "kind": "js_error",
            "severity": "error",
            "component": "marketKlineChart",
            "page": "/",
            "message": "request failed api_key=sk-super-secret-token",
            "stack": "at render (https://doubleh.site/app.js?token=secret)",
            "version": "v-test",
            "context": {"route": "/api/quote/603019", "method": "GET", "ignored_prompt": "private"},
        },
    )
    assert captured.status_code == 200

    denied = await client.get("/api/admin/diagnostics", cookies=auth_cookies)
    assert denied.status_code == 403

    admin = _admin_cookies()
    listed = await client.get("/api/admin/diagnostics", cookies=admin)
    assert listed.status_code == 200
    event = listed.json()["events"][0]
    assert event["component"] == "marketKlineChart"
    assert "super-secret" not in event["message"]
    assert "ignored_prompt" not in event["context"]

    bundle = await client.get("/api/admin/diagnostics/bundle", cookies=admin)
    assert bundle.status_code == 200
    assert bundle.json()["schema"] == "prism-codex-diagnostics-v1"

    resolved = await client.post(f"/api/admin/diagnostics/{event['id']}/resolve", cookies=admin)
    assert resolved.status_code == 200
    after = await client.get("/api/admin/diagnostics", cookies=admin)
    assert after.json()["events"] == []


async def test_bundle_separates_history_and_access_denials_without_claiming_fixed(client):
    from server.database import record_activity
    from server.diagnostics import record_diagnostic
    from server.release import APP_VERSION

    for version in (APP_VERSION, "v0.19.7", ""):
        record_diagnostic(kind="js_error", message="synthetic anomaly", version=version)
    for status, username in ((401, ""), (403, ""), (404, ""), (503, ""), (404, "admin")):
        record_activity(
            username=username,
            feature="api",
            action="get",
            status="failed",
            meta={"status_code": status},
        )
    response = await client.get("/api/admin/diagnostics/bundle", cookies=_admin_cookies())
    assert response.status_code == 200
    data = response.json()
    assert set(data["release"]) == {"version", "deployed_revision"}
    assert data["triage"]["anomaly_version_counts"] == {
        "current_version": 1,
        "historical_version": 1,
        "unattributed": 1,
    }
    assert data["triage"]["recent_request_counts"] == {
        "anonymous_access_denied": 2,
        "anonymous_not_found": 1,
        "server_failure": 1,
        "needs_review": 1,
    }
    assert all(row["resolved_at"] is None for row in data["open_anomalies"])


async def test_bad_login_json_is_input_error_not_server_anomaly(client):
    for body in (b"", b"not-json", b"[]", b"null"):
        response = await client.post("/api/login", content=body, headers={"Content-Type": "application/json"})
        assert response.status_code == 422
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM diagnostic_events").fetchone()[0] == 0


def test_new_server_diagnostic_records_its_version():
    from server.diagnostics import record_diagnostic
    from server.release import APP_VERSION

    event_id = record_diagnostic(kind="server_exception", message="synthetic")
    with get_db() as db:
        row = db.execute("SELECT version FROM diagnostic_events WHERE id=?", [event_id]).fetchone()
    assert row["version"] == APP_VERSION
