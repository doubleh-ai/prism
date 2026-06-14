"""Test API endpoints"""
import json
import pytest


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
        r = await client.post("/api/login", json={
            "username": test_user["username"],
            "password": test_user["password"]
        })
        assert r.status_code == 200
        data = r.json()
        assert data.get("user") == test_user["username"]

    async def test_login_failure(self, client):
        r = await client.post("/api/login", json={
            "username": "nonexistent", "password": "wrong"
        })
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
        r = await client.post("/api/convs", cookies=auth_cookies,
                              json={"skill": "bottleneck-hunter"})
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
        c1 = await client.post("/api/convs", cookies=auth_cookies,
                               json={"skill": "bottleneck-hunter"})
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
        c1 = await client.post("/api/convs", cookies=auth_cookies,
                               json={"skill": "bottleneck-hunter"})
        cid = c1.json()["id"]

        r = await client.get(f"/api/convs/{cid}", cookies=auth_cookies)
        assert r.status_code == 200
        conv = r.json()
        assert conv["id"] == cid
        assert conv["title"] == "新对话"
        assert conv["messages"] == []

    async def test_delete_conv_soft(self, client, auth_cookies):
        """DELETE now soft-deletes (moves to trash). GET returns 404 for trashed items."""
        c1 = await client.post("/api/convs", cookies=auth_cookies,
                               json={"skill": "bottleneck-hunter"})
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
        c1 = await client.post("/api/convs", cookies=auth_cookies,
                               json={"skill": "bottleneck-hunter"})
        cid = c1.json()["id"]

        r = await client.put(f"/api/convs/{cid}/skill", cookies=auth_cookies,
                             json={"skill": "serenity"})
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
        c1 = await client.post("/api/convs", cookies=auth_cookies,
                               json={"skill": "bottleneck-hunter"})
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
        c1 = await client.post("/api/convs", cookies=auth_cookies,
                               json={"skill": "bottleneck-hunter"})
        cid = c1.json()["id"]

        # Permanent delete
        r = await client.delete(f"/api/convs/{cid}/permanent", cookies=auth_cookies)
        assert r.status_code == 200

        # Should be gone from active list
        r2 = await client.get(f"/api/convs/{cid}", cookies=auth_cookies)
        assert r2.status_code == 404

        # Should NOT appear in trash
        r3 = await client.get("/api/trash", cookies=auth_cookies)
        assert r3.json() == []

    async def test_trash_has_deleted_at(self, client, auth_cookies):
        c1 = await client.post("/api/convs", cookies=auth_cookies,
                               json={"skill": "bottleneck-hunter"})
        cid = c1.json()["id"]

        await client.delete(f"/api/convs/{cid}", cookies=auth_cookies)

        r = await client.get("/api/trash", cookies=auth_cookies)
        trash = r.json()
        assert "deleted_at" in trash[0]
        assert isinstance(trash[0]["deleted_at"], int)


class TestChatControl:
    """Chat respond and cancel endpoints"""

    async def test_chat_respond_missing_fields(self, client, auth_cookies):
        r = await client.post("/api/chat/respond", cookies=auth_cookies, json={})
        assert r.status_code == 400

    async def test_chat_cancel_missing_fields(self, client, auth_cookies):
        r = await client.post("/api/chat/cancel", cookies=auth_cookies, json={})
        assert r.status_code == 400

    async def test_chat_cancel_no_process(self, client, auth_cookies):
        """Cancel returns ok even when no process running."""
        r = await client.post("/api/chat/cancel", cookies=auth_cookies,
                              json={"conv_id": "nonexistent"})
        assert r.status_code == 200
        assert r.json() == {"ok": False}


    async def test_conv_isolation(self, client, auth_cookies, test_user):
        """Different users can't see each other's conversations."""
        c1 = await client.post("/api/convs", cookies=auth_cookies,
                               json={"skill": "bottleneck-hunter"})
        cid = c1.json()["id"]

        # Login as different user
        from server.database import get_db
        from server.auth import hash_password
        with get_db() as db:
            db.execute("INSERT OR REPLACE INTO users (username, password_hash) VALUES (?,?)",
                       ["other", hash_password("pass")])
        from server.auth import create_session
        token2 = create_session("other")

        r = await client.get(f"/api/convs/{cid}", cookies={"token": token2})
        assert r.status_code == 404
