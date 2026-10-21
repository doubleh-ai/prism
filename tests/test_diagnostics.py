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
