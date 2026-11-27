"""Minimum-data monitoring against synthetic accounts and an isolated database."""

import json

import pytest

MONITOR = "synthetic-monitor-only-token-00000000000001"
WORKER = "synthetic-distinct-worker-token-000000000001"
ROOT = "/api/internal/feedback-monitor"


@pytest.fixture(autouse=True)
def monitor_db(temp_data_dir, monkeypatch):
    from server import auth, feedback
    from server.database import create_session, get_db

    monkeypatch.setenv("PRISM_FEEDBACK_MONITOR_TOKEN", MONITOR)
    monkeypatch.setenv("PRISM_FEEDBACK_WORKER_TOKEN", WORKER)
    monkeypatch.setattr(auth, "ADMIN_USERS", {"synthetic-admin"})
    feedback.init_feedback_db()
    with get_db() as db:
        db.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", ["synthetic-admin", "synthetic"])
    return {"token": create_session("synthetic-admin")}


@pytest.fixture
def headers():
    return {"X-Prism-Monitor-Token": MONITOR}


async def submit(client, monitor_db, **overrides):
    response = await client.post(
        "/api/feedback",
        cookies=monitor_db,
        json={
            "category": "bug",
            "title": "PRIVATE_TITLE",
            "description": "PRIVATE_DESCRIPTION",
            "page": "/private/user/path",
            "feature": "chat",
            **overrides,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["feedback"]


@pytest.mark.parametrize("path", ["/queue", "/diagnostics"])
@pytest.mark.parametrize("credential", ["none", "browser", "worker", "wrong", "duplicate"])
async def test_only_distinct_reader_identity_accepted(client, monitor_db, path, credential):
    kwargs = {}
    if credential == "browser":
        kwargs["cookies"] = monitor_db
    elif credential == "worker":
        kwargs["headers"] = {"X-Prism-Feedback-Token": WORKER}
    elif credential == "wrong":
        kwargs["headers"] = {"X-Prism-Monitor-Token": WORKER}
    elif credential == "duplicate":
        from httpx import ASGITransport, AsyncClient

        from server.main import app

        # The shared client fixture converts headers to a dict. Exercise actual
        # duplicate ASGI headers without that convenience wrapper.
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as raw:
            r = await raw.get(ROOT + path, headers=[("X-Prism-Monitor-Token", MONITOR)] * 2)
        assert r.status_code == 401
        assert r.headers["cache-control"] == "no-store"
        return
    r = await client.get(ROOT + path, **kwargs)
    assert r.status_code == 401
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("value", ["", "short", " " * 40, "x" * 513, WORKER])
async def test_disabled_invalid_or_shared_secret_fails_closed(client, monkeypatch, headers, value):
    monkeypatch.setenv("PRISM_FEEDBACK_MONITOR_TOKEN", value)
    assert (await client.get(ROOT + "/queue", headers=headers)).status_code == 401
    if value == WORKER:
        for path in ("/api/internal/feedback", "/api/internal/feedback/diagnostics"):
            assert (await client.get(path, headers={"X-Prism-Feedback-Token": WORKER})).status_code == 401


async def test_reader_cannot_read_text_or_write_with_either_header(client, monitor_db, headers):
    from server.database import get_db

    item = await submit(client, monitor_db)
    for identity in (headers, {"X-Prism-Feedback-Token": MONITOR}):
        for path in (
            "/api/admin/users",
            "/api/admin/feedback",
            "/api/internal/feedback",
            f"/api/internal/feedback/{item['id']}",
        ):
            assert (await client.get(path, headers=identity)).status_code in (401, 403)
        assert (
            await client.post(
                f"/api/internal/feedback/{item['id']}/update", headers=identity, json={"status": "fixed"}
            )
        ).status_code == 401
    for method in ("post", "put", "patch", "delete"):
        assert (await getattr(client, method)(ROOT + "/queue", headers=headers)).status_code == 405
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM feedback_updates").fetchone()[0] == 0
        assert db.execute("SELECT status FROM feedback_items").fetchone()[0] == "triaging"


async def test_queue_selects_only_metadata_and_bounds_vocabulary(client, monitor_db, headers, monkeypatch):
    from server import feedback_monitor
    from server.database import get_db

    item = await submit(client, monitor_db)
    monkeypatch.setattr(feedback_monitor, "_read_deployed_revision", lambda: "c" * 40)
    with get_db() as db:
        db.execute(
            "UPDATE feedback_items SET feature=?, status=?, version=?, revision=? WHERE id=?",
            ["PRIVATE_FEATURE", "PRIVATE_STATUS", "PRIVATE_VERSION", "PRIVATE_REVISION", item["id"]],
        )
        before = tuple(db.execute("SELECT * FROM feedback_items").fetchone())
        queries = []
        db.set_trace_callback(queries.append)
    try:
        r = await client.get(ROOT + "/queue", headers=headers)
    finally:
        with get_db() as db:
            db.set_trace_callback(None)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    v = r.json()
    assert v["read_only"] and not v["signals_verified"] and not v["user_text_included"]
    assert v["release"]["revision"] == "c" * 12
    assert all(s not in r.text for s in ("PRIVATE", "synthetic-admin", "/private", MONITOR, WORKER))
    row = v["items"][0]
    assert row["feature"] == row["status"] == "unknown"
    assert row["version"] == row["revision"] == ""
    assert set(row) == {
        "id",
        "category",
        "feature",
        "status",
        "version",
        "revision",
        "revision_number",
        "created_at",
        "updated_at",
    }
    selects = [q.lower().split(" from ")[0] for q in queries if "from feedback_items" in q.lower()]
    assert selects and all(
        not any(s in q for s in ("*", "title", "description", "username", "triage", "public_reply"))
        for q in selects
    )
    with get_db() as db:
        assert tuple(db.execute("SELECT * FROM feedback_items").fetchone()) == before


async def test_inclusive_polling_finds_same_second_update_and_paginates(
    client, monitor_db, headers, monkeypatch
):
    from server import feedback_monitor
    from server.database import get_db

    a = await submit(client, monitor_db)
    b = await submit(client, monitor_db, title="second")
    c = await submit(client, monitor_db, category="suggestion")
    with get_db() as db:
        db.execute("UPDATE feedback_items SET created_at=100,updated_at=100")
    monkeypatch.setattr(feedback_monitor.time, "time", lambda: 100)
    first = (await client.get(ROOT + "/queue?since=100&limit=1", headers=headers)).json()
    assert [r["id"] for r in first["items"]] == [a["id"]]
    assert first["next_after_id"] == a["id"] and first["poll_started_at"] == 100
    second = (await client.get(ROOT + f"/queue?since=100&limit=1&after_id={a['id']}", headers=headers)).json()
    assert [r["id"] for r in second["items"]] == [b["id"]] and second["next_after_id"] is None
    with get_db() as db:
        db.execute("UPDATE feedback_items SET revision_number=2 WHERE id=?", [a["id"]])
    again = (await client.get(ROOT + "/queue?since=100", headers=headers)).json()
    assert again["items"][0]["revision_number"] == 2
    suggestion = (await client.get(ROOT + "/queue?category=suggestion", headers=headers)).json()
    assert [r["id"] for r in suggestion["items"]] == [c["id"]]


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=101",
        "limit=-1",
        "since=9999999999999999",
        "after_id=abc",
        "category=secret",
        "token=PRIVATE",
        "limit=1&limit=2",
        "after_id=9007199254740992",
    ],
)
async def test_invalid_queries_do_not_echo_values(client, headers, query):
    r = await client.get(ROOT + "/queue?" + query, headers=headers)
    assert r.status_code == 422 and "PRIVATE" not in r.text and "secret" not in r.text


async def test_diagnostics_omit_arbitrary_methods_history_text_and_non_faults(client, headers):
    from server.database import get_db
    from server.diagnostics import record_diagnostic
    from server.release import APP_VERSION

    for kind, message, version in (
        ("js_error", "TypeError: this.PRIVATE_IDENTIFIER is not a function", "v0.1.0"),
        ("server_exception", "ValueError: PRIVATE_MESSAGE", APP_VERSION),
        ("server_http_5xx", "HTTP 503 from /private/history", APP_VERSION),
        ("anonymous_access_denied", "HTTP 401", APP_VERSION),
        ("anonymous_not_found", "HTTP 404", APP_VERSION),
        ("server_native_run_cancelled", "user cancelled", APP_VERSION),
    ):
        record_diagnostic(
            username="synthetic-admin",
            kind=kind,
            message=message,
            version=version,
            stack="PRIVATE_STACK",
            context={"request_id": "PRIVATE_CONTEXT"},
        )
    r = await client.get(ROOT + "/diagnostics", headers=headers)
    assert r.status_code == 200
    assert all(s not in r.text for s in ("PRIVATE", "synthetic-admin", "/private", "anonymous_", "cancelled"))
    items = r.json()["items"]
    assert len(items) == 3
    assert any(
        v["error_code"] == "method_not_callable" and v["version_scope"] == "historical_version" for v in items
    )
    assert any(v.get("http_status") == 503 for v in items)
    assert any(v.get("exception_class") == "ValueError" for v in items)
    with get_db() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM diagnostic_events WHERE resolved_at IS NULL").fetchone()[0] == 6
        )
    page = (await client.get(ROOT + "/diagnostics?limit=1", headers=headers)).json()
    assert len(page["items"]) == 1 and page["next_after_id"] == page["items"][0]["id"]
    assert "context" not in json.dumps(items)


async def test_rate_limit_and_rotation_fail_closed(client, headers, monkeypatch):
    from server import feedback_monitor

    monkeypatch.setenv("PRISM_FEEDBACK_MONITOR_TOKEN", "synthetic-new-reader-token-after-rotation-0001")
    assert (await client.get(ROOT + "/queue", headers=headers)).status_code == 401
    monkeypatch.setenv("PRISM_FEEDBACK_MONITOR_TOKEN", MONITOR)
    monkeypatch.setattr(feedback_monitor, "check_rate_limit", lambda *args: False)
    assert (await client.get(ROOT + "/queue", headers=headers)).status_code == 401
