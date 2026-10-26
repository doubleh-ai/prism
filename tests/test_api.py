"""Test API endpoints"""

import json

import pytest

from server.release import APP_VERSION


class TestPublicEndpoints:
    """Endpoints that don't require auth"""

    async def test_skills_endpoint(self, client):
        r = await client.get("/api/skills")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        skill = data[0]
        assert "id" in skill
        assert "name" in skill
        assert "shortcut" in skill

    async def test_models_endpoint(self, client):
        r = await client.get("/api/models")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        for m in data:
            assert "id" in m
            assert "name" in m

    async def test_login_page(self, client):
        r = await client.get("/login")
        assert r.status_code == 200
        assert "login" in r.text.lower() or "登录" in r.text

    async def test_root_redirects_to_login(self, client):
        r = await client.get("/", follow_redirects=False)
        assert r.status_code in (307, 302)


class TestAuthEndpoints:
    """Endpoints that require authentication"""

    async def test_login_success(self, client, test_user):
        r = await client.post(
            "/api/login", json={"username": test_user["username"], "password": test_user["password"]}
        )
        assert r.status_code == 200
        data = r.json()
        assert data.get("user") == test_user["username"]

    async def test_login_failure(self, client):
        r = await client.post("/api/login", json={"username": "nonexistent", "password": "wrong"})
        assert r.status_code in (401, 403)  # rate limiter may return 403

    async def test_me_authenticated(self, client, auth_cookies):
        r = await client.get("/api/me", cookies=auth_cookies)
        assert r.status_code == 200
        assert r.json()["user"] == "testuser"

    async def test_me_unauthenticated(self, client):
        r = await client.get("/api/me")
        assert r.status_code == 401

    async def test_logout(self, client, auth_cookies):
        r = await client.post("/api/logout", cookies=auth_cookies)
        assert r.status_code == 200
        # After logout, me should fail
        r2 = await client.get("/api/me", cookies=auth_cookies)
        assert r2.status_code == 401


class TestConversationCRUD:
    """Conversation create/read/delete/list"""

    async def test_create_conv(self, client, auth_cookies):
        r = await client.post("/api/convs", cookies=auth_cookies, json={"skill": "bottleneck-hunter"})
        assert r.status_code == 200
        data = r.json()
        assert "id" in data
        assert len(data["id"]) == 24  # hex(12)

    async def test_list_convs_empty(self, client, auth_cookies):
        r = await client.get("/api/convs", cookies=auth_cookies)
        assert r.status_code == 200
        assert r.json() == []

    async def test_list_convs_with_data(self, client, auth_cookies):
        # Create a conversation
        c1 = await client.post("/api/convs", cookies=auth_cookies, json={"skill": "bottleneck-hunter"})
        cid = c1.json()["id"]

        r = await client.get("/api/convs", cookies=auth_cookies)
        assert r.status_code == 200
        convs = r.json()
        assert len(convs) == 1
        assert convs[0]["id"] == cid
        assert convs[0]["title"] == "新对话"

    async def test_get_conv_not_found(self, client, auth_cookies):
        r = await client.get("/api/convs/nonexistent", cookies=auth_cookies)
        assert r.status_code == 404

    async def test_get_conv(self, client, auth_cookies):
        c1 = await client.post("/api/convs", cookies=auth_cookies, json={"skill": "bottleneck-hunter"})
        cid = c1.json()["id"]

        r = await client.get(f"/api/convs/{cid}", cookies=auth_cookies)
        assert r.status_code == 200
        conv = r.json()
        assert conv["id"] == cid
        assert conv["title"] == "新对话"
        assert conv["messages"] == []

    async def test_get_conv_sanitizes_legacy_db_messages(self, client, auth_cookies):
        """Stored DB messages from older builds should be cleaned on read."""
        c1 = await client.post("/api/convs", cookies=auth_cookies, json={"skill": "bottleneck-hunter"})
        cid = c1.json()["id"]
        dirty = json.dumps(
            {
                "text": "answer [object Object] done",
                "chunks": [
                    {"t": "think", "c": "[object Object]\n\n[object Object]"},
                    {"t": "think", "c": "[object Object]\nvisible thought"},
                    {"t": "text", "c": "answer [object Object] done"},
                ],
            },
            ensure_ascii=False,
        )
        from server.database import get_db

        with get_db() as db:
            db.execute(
                "INSERT INTO messages (conv_id, role, content) VALUES (?,?,?)", [cid, "assistant", dirty]
            )

        r = await client.get(f"/api/convs/{cid}", cookies=auth_cookies)
        assert r.status_code == 200
        raw = json.dumps(r.json(), ensure_ascii=False)
        assert "[object Object]" not in raw
        payload = json.loads(r.json()["messages"][0]["content"])
        assert payload["text"] == "answer  done"
        assert all(c.get("c") for c in payload["chunks"])
        assert "visible thought" not in raw
        assert all(c["t"] == "text" for c in payload["chunks"])
        with get_db() as db:
            assert db.execute("SELECT content FROM messages WHERE conv_id=?", [cid]).fetchone()[0] == dirty

    async def test_delete_conv_soft(self, client, auth_cookies):
        """DELETE now soft-deletes (moves to trash). GET returns 404 for trashed items."""
        c1 = await client.post("/api/convs", cookies=auth_cookies, json={"skill": "bottleneck-hunter"})
        cid = c1.json()["id"]

        r = await client.delete(f"/api/convs/{cid}", cookies=auth_cookies)
        assert r.status_code == 200
        assert r.json() == {"ok": True}

        # Should be gone from active list
        r2 = await client.get(f"/api/convs/{cid}", cookies=auth_cookies)
        assert r2.status_code == 404

        # Should appear in trash
        r3 = await client.get("/api/trash", cookies=auth_cookies)
        assert r3.status_code == 200
        trash = r3.json()
        assert len(trash) == 1
        assert trash[0]["id"] == cid

    async def test_update_skill(self, client, auth_cookies):
        c1 = await client.post("/api/convs", cookies=auth_cookies, json={"skill": "bottleneck-hunter"})
        cid = c1.json()["id"]

        r = await client.put(f"/api/convs/{cid}/skill", cookies=auth_cookies, json={"skill": "serenity"})
        assert r.status_code == 200

        # Verify
        r2 = await client.get(f"/api/convs/{cid}", cookies=auth_cookies)
        assert r2.json()["skill"] == "serenity"


class TestRecycleBin:
    """Soft delete / restore / permanent delete / trash list"""

    async def test_trash_empty(self, client, auth_cookies):
        r = await client.get("/api/trash", cookies=auth_cookies)
        assert r.status_code == 200
        assert r.json() == []

    async def test_restore_conv(self, client, auth_cookies):
        c1 = await client.post("/api/convs", cookies=auth_cookies, json={"skill": "bottleneck-hunter"})
        cid = c1.json()["id"]

        # Soft delete
        await client.delete(f"/api/convs/{cid}", cookies=auth_cookies)

        # Restore
        r = await client.post(f"/api/convs/{cid}/restore", cookies=auth_cookies)
        assert r.status_code == 200

        # Should be back in active list
        r2 = await client.get(f"/api/convs/{cid}", cookies=auth_cookies)
        assert r2.status_code == 200

        # Trash should be empty
        r3 = await client.get("/api/trash", cookies=auth_cookies)
        assert r3.json() == []

    async def test_permanent_delete(self, client, auth_cookies):
        c1 = await client.post("/api/convs", cookies=auth_cookies, json={"skill": "bottleneck-hunter"})
        cid = c1.json()["id"]

        # Permanent deletion is only available for owned conversations in the trash.
        await client.delete(f"/api/convs/{cid}", cookies=auth_cookies)
        r = await client.delete(f"/api/convs/{cid}/permanent", cookies=auth_cookies)
        assert r.status_code == 200

        # Should be gone from active list
        r2 = await client.get(f"/api/convs/{cid}", cookies=auth_cookies)
        assert r2.status_code == 404

        # Should NOT appear in trash
        r3 = await client.get("/api/trash", cookies=auth_cookies)
        assert r3.json() == []

    async def test_trash_has_deleted_at(self, client, auth_cookies):
        c1 = await client.post("/api/convs", cookies=auth_cookies, json={"skill": "bottleneck-hunter"})
        cid = c1.json()["id"]

        await client.delete(f"/api/convs/{cid}", cookies=auth_cookies)

        r = await client.get("/api/trash", cookies=auth_cookies)
        trash = r.json()
        assert "deleted_at" in trash[0]
        assert isinstance(trash[0]["deleted_at"], int)


class TestChatControl:
    """Chat respond and cancel endpoints"""

    async def test_chat_respond_requires_native_followup(self, client, auth_cookies):
        r = await client.post("/api/chat/respond", cookies=auth_cookies, json={})
        assert r.status_code == 409
        assert "当前对话" in r.json()["error"]

    async def test_chat_cancel_missing_fields(self, client, auth_cookies):
        r = await client.post("/api/chat/cancel", cookies=auth_cookies, json={})
        assert r.status_code == 422

    async def test_chat_cancel_without_run(self, client, auth_cookies):
        """A conversation without a native run has no cancellable task."""
        created = await client.post("/api/convs", cookies=auth_cookies, json={})
        r = await client.post(
            "/api/chat/cancel", cookies=auth_cookies, json={"conv_id": created.json()["id"]}
        )
        assert r.status_code == 404

    async def test_chat_cancel_queued_native_run(self, client, auth_cookies):
        from server.agent import store

        created = await client.post("/api/convs", cookies=auth_cookies, json={})
        cid = created.json()["id"]
        run = store.create_run("testuser", cid, "待执行问题", "synthetic", "research")
        r = await client.post(
            "/api/chat/cancel", cookies=auth_cookies, json={"conv_id": cid, "run_id": run["run_id"]}
        )
        assert r.status_code == 200
        assert r.json() == {
            "ok": True,
            "run_id": run["run_id"],
            "status": "queued",
            "cancel_requested": True,
            "stopped": False,
        }
        assert store.get_run("testuser", run["run_id"])["cancel_requested"] is True
        event = store.list_events("testuser", run["run_id"])[-1]
        assert event["type"] == "cancel_requested"
        assert event["external_stop_confirmed"] is False

    async def test_conv_isolation(self, client, auth_cookies, test_user):
        """Different users can't see each other's conversations."""
        c1 = await client.post("/api/convs", cookies=auth_cookies, json={"skill": "bottleneck-hunter"})
        cid = c1.json()["id"]

        # Login as different user
        from server.auth import hash_password
        from server.database import get_db

        with get_db() as db:
            db.execute(
                "INSERT OR REPLACE INTO users (username, password_hash) VALUES (?,?)",
                ["other", hash_password("pass")],
            )
        from server.auth import create_session

        token2 = create_session("other")

        r = await client.get(f"/api/convs/{cid}", cookies={"token": token2})
        assert r.status_code == 404


class TestReleaseMetadata:
    """Release metadata endpoint."""

    async def test_releases_requires_auth(self, client):
        r = await client.get("/api/releases")
        assert r.status_code == 401

    async def test_releases_returns_current_version(self, client, auth_cookies):
        r = await client.get("/api/releases", cookies=auth_cookies)
        assert r.status_code == 200
        data = r.json()
        assert data["version"] == APP_VERSION
        assert data["latest"]["version"] == APP_VERSION
        assert data["releases"]
        assert data["releases"][0]["sections"]

    async def test_app_shell_is_not_cached(self, client, auth_cookies):
        r = await client.get("/", cookies=auth_cookies)
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-store"

    async def test_login_page_is_not_cached(self, client):
        r = await client.get("/login")
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-store"


class TestAnalysisApi:
    """Astock API options should be wired through to the engine."""

    async def test_analysis_start_accepts_mode_force_refresh_and_reports_cache(
        self, client, auth_cookies, monkeypatch
    ):
        from server import main

        captured = {}

        async def fake_start_analysis(
            ticker, name="", date="", username="", mode="deep", force_refresh=False
        ):
            captured.update(
                {
                    "ticker": ticker,
                    "name": name,
                    "username": username,
                    "mode": mode,
                    "force_refresh": force_refresh,
                }
            )
            main._store["fakeaid"] = {
                "id": "fakeaid",
                "mode": "fast",
                "cached": True,
                "selected_analysts": ["market", "fundamentals"],
            }
            return "fakeaid"

        main._store.pop("fakeaid", None)
        monkeypatch.setattr(main, "start_analysis", fake_start_analysis)
        r = await client.post(
            "/api/analysis/start",
            cookies=auth_cookies,
            json={"ticker": "003000", "name": "劲仔食品", "mode": "fast", "force_refresh": True},
        )

        assert r.status_code == 200
        data = r.json()
        assert data["analysis_id"] == "fakeaid"
        assert data["status"] == "cached"
        assert data["mode"] == "fast"
        assert data["cached"] is True
        assert data["selected_analysts"] == ["market", "fundamentals"]
        assert captured["ticker"] == "003000"
        assert captured["mode"] == "fast"
        assert captured["force_refresh"] is True
        main._store.pop("fakeaid", None)


class TestSavedReports:
    """User-scoped saved report library."""

    async def test_saved_report_crud_and_isolation(self, client, auth_cookies):
        payload = {
            "source": "manual",
            "source_id": "note-1",
            "ticker": "600519",
            "name": "Kweichow Moutai",
            "title": "600519 research note",
            "signal": "HOLD",
            "summary": "A compact thesis",
            "report": {"final_decision": "Hold with valuation discipline."},
        }
        created = await client.post("/api/reports/library", cookies=auth_cookies, json=payload)
        assert created.status_code == 200
        saved = created.json()["report"]
        assert saved["ticker"] == "600519"
        assert saved["summary"] == "A compact thesis"
        assert saved["report"]["final_decision"].startswith("Hold")

        listed = await client.get("/api/reports/library", cookies=auth_cookies)
        assert listed.status_code == 200
        assert [r["id"] for r in listed.json()["reports"]] == [saved["id"]]

        detail = await client.get(f"/api/reports/library/{saved['id']}", cookies=auth_cookies)
        assert detail.status_code == 200
        assert detail.json()["report"]["final_decision"].startswith("Hold")

        from server.auth import create_session, hash_password
        from server.database import get_db

        with get_db() as db:
            db.execute(
                "INSERT OR REPLACE INTO users (username, password_hash) VALUES (?,?)",
                ["other", hash_password("pass12345")],
            )
        other_token = create_session("other")
        other_detail = await client.get(f"/api/reports/library/{saved['id']}", cookies={"token": other_token})
        assert other_detail.status_code == 404

        deleted = await client.delete(f"/api/reports/library/{saved['id']}", cookies=auth_cookies)
        assert deleted.status_code == 200
        empty = await client.get("/api/reports/library", cookies=auth_cookies)
        assert empty.json()["reports"] == []

    async def test_save_completed_analysis_report(self, client, auth_cookies):
        from server.analysis_engine import _store

        aid = "unit-analysis-1"
        _store[aid] = {
            "id": aid,
            "ticker": "300750",
            "name": "CATL",
            "username": "testuser",
            "status": "done",
            "report": {
                "signal": "BUY",
                "final_decision": "Battery leader with supply-chain leverage.",
                "structured": {"signal": "BUY", "thesis": "Power battery bottleneck thesis."},
            },
            "signal": "BUY",
            "error": None,
            "started_at": 1,
        }
        try:
            r = await client.post(
                f"/api/analysis/{aid}/save-report",
                cookies=auth_cookies,
                json={"title": "CATL snapshot"},
            )
            assert r.status_code == 200
            saved = r.json()["report"]
            assert saved["source"] == "analysis"
            assert saved["source_id"] == aid
            assert saved["title"] == "CATL snapshot"
            assert saved["signal"] == "BUY"

            r2 = await client.post(
                f"/api/analysis/{aid}/save-report",
                cookies=auth_cookies,
                json={"title": "CATL refreshed"},
            )
            assert r2.status_code == 200
            assert r2.json()["report"]["id"] == saved["id"]
            # Re-saving a completed research revision is idempotent. Follow-ups
            # create new runs instead of overwriting the previous report.
            assert r2.json()["report"]["title"] == "CATL snapshot"
            assert r2.json()["report"]["report"] == saved["report"]
            assert r2.json()["report"]["updated_at"] == saved["updated_at"]
        finally:
            _store.pop(aid, None)

    async def test_conversation_reports_include_saved_snapshots(self, client, auth_cookies):
        created_conv = await client.post(
            "/api/convs",
            cookies=auth_cookies,
            json={"skill": "bottleneck-hunter"},
        )
        assert created_conv.status_code == 200
        conv_id = created_conv.json()["id"]

        from server.reports import save_report_snapshot

        # Server-generated chat provenance is created by the trusted producer,
        # never accepted from a browser's arbitrary JSON body.
        report_id = save_report_snapshot(
            "testuser",
            {
                "source": "chat",
                "source_id": "fixture-run",
                "conv_id": conv_id,
                "title": "Chat thesis snapshot",
                "summary": "Final answer was saved automatically.",
                "report": {"final_decision": "A complete chat answer."},
            },
        )["id"]

        listed = await client.get(f"/api/convs/{conv_id}/reports", cookies=auth_cookies)
        assert listed.status_code == 200
        reports = listed.json()["reports"]
        assert any(r.get("kind") == "saved" and r.get("report_id") == report_id for r in reports)


class TestAdminDefaults:
    """Default admin policy and session behavior."""

    async def test_regular_user_is_not_admin_by_default(self, client, auth_cookies):
        r = await client.get("/api/admin/users", cookies=auth_cookies)
        assert r.status_code == 403
        r2 = await client.get("/api/admin/stats", cookies=auth_cookies)
        assert r2.status_code == 403
        r3 = await client.get("/api/admin/health", cookies=auth_cookies)
        assert r3.status_code == 403
        r4 = await client.get("/api/admin/analytics/overview", cookies=auth_cookies)
        assert r4.status_code == 403
        r5 = await client.get("/api/admin/analytics/events", cookies=auth_cookies)
        assert r5.status_code == 403
        r6 = await client.get("/api/admin/analytics/users", cookies=auth_cookies)
        assert r6.status_code == 403

    async def test_same_user_can_have_multiple_sessions(self, client, test_user):
        login1 = await client.post("/api/login", json=test_user)
        login2 = await client.post("/api/login", json=test_user)
        assert login1.status_code == 200
        assert login2.status_code == 200
        from server.database import get_db

        with get_db() as db:
            row = db.execute(
                "SELECT COUNT(*) AS c FROM sessions WHERE username=? AND expires > strftime('%s','now')",
                [test_user["username"]],
            ).fetchone()
        assert row["c"] >= 2

    async def test_bootstrap_creates_configured_admin_user(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_USER", "bootstrap_admin")
        monkeypatch.setenv("ADMIN_PASS", "bootstrappass123")
        from server.database import get_db
        from server.main import ensure_configured_admin_user

        ensure_configured_admin_user()

        with get_db() as db:
            row = db.execute(
                "SELECT username, disabled_at FROM users WHERE username=?", ["bootstrap_admin"]
            ).fetchone()
        assert row is not None
        assert row["disabled_at"] is None

        login = await client.post(
            "/api/login", json={"username": "bootstrap_admin", "password": "bootstrappass123"}
        )
        assert login.status_code == 200


class TestAdminAccountManagement:
    """Admin account management endpoints."""

    @pytest.fixture(autouse=True)
    def _allow_testuser_admin(self):
        from server import auth

        old = set(auth.ADMIN_USERS)
        auth.ADMIN_USERS.add("testuser")
        try:
            yield
        finally:
            auth.ADMIN_USERS.clear()
            auth.ADMIN_USERS.update(old)

    async def test_admin_list_users(self, client, auth_cookies):
        r = await client.get("/api/admin/users", cookies=auth_cookies)
        assert r.status_code == 200
        data = r.json()
        assert data["current_user"] == "testuser"
        assert len(data["users"]) == 1
        assert data["users"][0]["username"] == "testuser"
        assert data["users"][0]["is_admin"] is True

    async def test_admin_create_disable_enable_reset_and_delete_user(self, client, auth_cookies):
        r = await client.post(
            "/api/admin/users", cookies=auth_cookies, json={"username": "managed", "password": "firstpass123"}
        )
        assert r.status_code == 200

        login_ok = await client.post("/api/login", json={"username": "managed", "password": "firstpass123"})
        assert login_ok.status_code == 200

        r = await client.put("/api/admin/users/managed/status", cookies=auth_cookies, json={"disabled": True})
        assert r.status_code == 200
        login_disabled = await client.post(
            "/api/login", json={"username": "managed", "password": "firstpass123"}
        )
        assert login_disabled.status_code == 403

        r = await client.put(
            "/api/admin/users/managed/status", cookies=auth_cookies, json={"disabled": False}
        )
        assert r.status_code == 200

        r = await client.put(
            "/api/admin/users/managed/password", cookies=auth_cookies, json={"password": "secondpass123"}
        )
        assert r.status_code == 200
        login_old = await client.post("/api/login", json={"username": "managed", "password": "firstpass123"})
        assert login_old.status_code == 403
        login_new = await client.post("/api/login", json={"username": "managed", "password": "secondpass123"})
        assert login_new.status_code == 200

        r = await client.delete("/api/admin/users/managed", cookies=auth_cookies)
        assert r.status_code == 200
        login_deleted = await client.post(
            "/api/login", json={"username": "managed", "password": "secondpass123"}
        )
        assert login_deleted.status_code == 403

    async def test_admin_cannot_disable_or_delete_self(self, client, auth_cookies):
        r = await client.put(
            "/api/admin/users/testuser/status", cookies=auth_cookies, json={"disabled": True}
        )
        assert r.status_code == 400
        r = await client.delete("/api/admin/users/testuser", cookies=auth_cookies)
        assert r.status_code == 400

    async def test_admin_stats_endpoint(self, client, auth_cookies):
        r = await client.get("/api/admin/stats", cookies=auth_cookies)
        assert r.status_code == 200
        data = r.json()
        assert "totals" in data
        assert data["totals"]["users"] >= 1
        assert "users" in data

    async def test_admin_health_endpoint(self, client, auth_cookies):
        r = await client.get("/api/admin/health", cookies=auth_cookies)
        assert r.status_code == 200
        data = r.json()
        assert data["version"] == APP_VERSION
        assert "checks" in data
        assert "database" in data["checks"]
        assert data["checks"]["native_core"] == {"status": "ok", "engine": "prism-native"}
        assert data["checks"]["tool_catalog"]["count"] > 0
        assert data["checks"]["provider_configuration"]["live_checked"] is False
        assert "claude_cli" not in data["checks"]
        assert data["scope"] == "local_configuration"
        assert "models" in data

    async def test_admin_activity_analytics_endpoints(self, client, auth_cookies):
        from server.database import record_activity

        record_activity(
            username="testuser",
            feature="market",
            action="search",
            target_type="query",
            target_id="300607",
            target_label="300607",
            duration_ms=42,
            meta={"status_code": 200},
        )
        record_activity(
            username="testuser",
            feature="analysis",
            action="start",
            target_type="ticker",
            target_id="300607",
            target_label="拓斯达",
            status="failed",
            duration_ms=88,
            meta={"status_code": 500},
        )

        overview = await client.get("/api/admin/analytics/overview?range=7d", cookies=auth_cookies)
        assert overview.status_code == 200
        overview_data = overview.json()
        assert overview_data["totals"]["events"] >= 2
        assert any(f["feature"] == "market" for f in overview_data["features"])
        assert all("avg_dau" in f and "peak_dau" in f for f in overview_data["features"])
        assert any(a["action"] == "search" for a in overview_data["actions"])

        events = await client.get("/api/admin/analytics/events?range=7d&feature=market", cookies=auth_cookies)
        assert events.status_code == 200
        event_rows = events.json()["events"]
        assert any(e["feature"] == "market" and e["target_label"] == "300607" for e in event_rows)

        users = await client.get("/api/admin/analytics/users?range=7d", cookies=auth_cookies)
        assert users.status_code == 200
        user_row = next(u for u in users.json()["users"] if u["username"] == "testuser")
        assert user_row["events"] >= 2
        assert user_row["feature_count"] >= 2

    async def test_admin_activity_insights_and_event_search(self, client, auth_cookies):
        import time

        from server.database import record_activity

        now = int(time.time())
        record_activity(
            username="testuser",
            feature="market",
            action="search",
            target_type="query",
            target_id="600519",
            target_label="600519",
            duration_ms=35,
            created_at=now - 2 * 86400,
        )
        record_activity(
            username="testuser",
            feature="market",
            action="quote",
            target_type="ticker",
            target_id="600519",
            target_label="贵州茅台",
            duration_ms=120,
            created_at=now - 86400,
        )
        record_activity(
            username="testuser",
            feature="analysis",
            action="start",
            target_type="ticker",
            target_id="600519",
            target_label="贵州茅台",
            duration_ms=5100,
            created_at=now - 3600,
        )
        record_activity(
            username="testuser",
            feature="analysis",
            action="open_report",
            status="failed",
            duration_ms=6200,
            meta={"status_code": 504},
            created_at=now - 1800,
        )

        response = await client.get("/api/admin/analytics/insights?range=7d", cookies=auth_cookies)
        assert response.status_code == 200
        data = response.json()
        assert data["timezone"] == "Asia/Shanghai"
        assert data["engagement"]["returning_users"] >= 1
        assert data["engagement"]["sessions"] >= 1
        assert data["engagement"]["p95_duration_ms"] >= 5100
        assert len(data["hourly"]) == 24
        assert any(item["target_id"] == "600519" for item in data["top_targets"])
        assert any(item["status_code"] == "504" for item in data["errors"])
        market_funnel = next(item for item in data["funnels"] if item["id"] == "market_research")
        assert market_funnel["steps"][2]["users"] == 1

        events = await client.get(
            "/api/admin/analytics/events?range=7d&action=search&query=600519&limit=1",
            cookies=auth_cookies,
        )
        assert events.status_code == 200
        event_data = events.json()
        assert event_data["total"] >= 1
        assert event_data["events"][0]["action"] == "search"
        assert "has_more" in event_data

    async def test_ui_activity_tracking_is_allowlisted(self, client, auth_cookies):
        from server.database import get_db

        tracked = await client.post(
            "/api/activity",
            cookies=auth_cookies,
            json={
                "feature": "market",
                "action": "watchlist_add",
                "target_type": "ticker",
                "target_id": "300750",
                "target_label": "宁德时代",
            },
        )
        assert tracked.status_code == 200
        with get_db() as db:
            row = db.execute(
                "SELECT feature, action, target_id, meta_json FROM activity_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert row["feature"] == "market"
        assert row["action"] == "watchlist_add"
        assert row["target_id"] == "300750"
        assert json.loads(row["meta_json"])["source"] == "ui"

        rejected = await client.post(
            "/api/activity",
            cookies=auth_cookies,
            json={"feature": "market", "action": "arbitrary_event"},
        )
        assert rejected.status_code == 422

    async def test_activity_middleware_records_release_open(self, client, auth_cookies):
        from server.database import get_db

        r = await client.get("/api/releases", cookies=auth_cookies)
        assert r.status_code == 200
        with get_db() as db:
            row = db.execute(
                """
                SELECT feature, action, username, status
                FROM activity_events
                WHERE feature='release' AND action='open_changelog'
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
        assert row is not None
        assert row["username"] == "testuser"
        assert row["status"] == "success"

    async def test_admin_endpoint_respects_admin_users_allowlist(self, client, auth_cookies):
        from server import auth

        old = set(auth.ADMIN_USERS)
        auth.ADMIN_USERS.clear()
        auth.ADMIN_USERS.add("admin")
        try:
            r = await client.get("/api/admin/users", cookies=auth_cookies)
            assert r.status_code == 403
        finally:
            auth.ADMIN_USERS.clear()
            auth.ADMIN_USERS.update(old)
