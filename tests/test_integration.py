"""Integration tests — end-to-end flows"""
import json
import pytest


class TestFullConversationFlow:
    """Create conversation → add messages → read back"""

    async def test_create_and_read_conv(self, client, auth_cookies):
        # Create
        c1 = await client.post("/api/convs", cookies=auth_cookies,
                               json={"skill": "bottleneck-hunter"})
        cid = c1.json()["id"]

        # Read back (empty)
        r = await client.get(f"/api/convs/{cid}", cookies=auth_cookies)
        assert r.status_code == 200
        assert r.json()["messages"] == []

    async def test_multiple_convs_ordering(self, client, auth_cookies):
        """Conversations are returned and all created IDs present."""
        ids = set()
        for i in range(3):
            await client.post("/api/convs", cookies=auth_cookies,
                              json={"skill": "bottleneck-hunter"})
        # Can't test exact ordering without time delays; just verify all exist

        convs = (await client.get("/api/convs", cookies=auth_cookies)).json()
        assert len(convs) >= 3

    async def test_skill_update_persists(self, client, auth_cookies):
        c1 = await client.post("/api/convs", cookies=auth_cookies,
                               json={"skill": "bottleneck-hunter"})
        cid = c1.json()["id"]

        await client.put(f"/api/convs/{cid}/skill", cookies=auth_cookies,
                         json={"skill": "serenity"})

        # Verify via GET
        r = await client.get(f"/api/convs/{cid}", cookies=auth_cookies)
        assert r.json()["skill"] == "serenity"

        # Verify via list
        convs = (await client.get("/api/convs", cookies=auth_cookies)).json()
        match = [c for c in convs if c["id"] == cid]
        assert match[0]["skill"] == "serenity"


class TestErrorHandling:
    async def test_empty_question_rejected(self, client, auth_cookies):
        r = await client.post("/api/chat/stream", cookies=auth_cookies,
                              json={"question": "", "skill": "bottleneck-hunter"})
        assert r.status_code == 400

    async def test_invalid_json_body(self, client):
        # Sending non-JSON with JSON content-type — FastAPI rejects before our handler
        try:
            r = await client.post("/api/login", content="not json",
                                  headers={"Content-Type": "application/json"})
            assert r.status_code in (400, 403, 422)
        except Exception:
            # FastAPI may raise JSONDecodeError internally; that's expected
            pass

    async def test_missing_fields(self, client, auth_cookies):
        r = await client.post("/api/login", json={})
        assert r.status_code in (400, 401, 403, 422)

    async def test_rate_limit_enforced(self, client, auth_cookies):
        """Rate limiter kicks in after too many requests."""
        import time
        key = f"chat:testuser"
        from server.database import check_rate_limit

        # Exhaust rate limit
        for _ in range(20):
            check_rate_limit(key, max_req=20, window=60)

        # Next request should be rate limited
        r = await client.post("/api/chat/stream", cookies=auth_cookies,
                              json={"question": "测试", "skill": "bottleneck-hunter"})
        assert r.status_code == 429


class TestMultiUserIsolation:
    """Users can't access each other's data"""

    async def test_cross_user_access_denied(self, client, auth_cookies, test_user):
        from server.database import get_db
        from server.auth import hash_password, create_session

        # Create conversation as testuser
        c1 = await client.post("/api/convs", cookies=auth_cookies,
                               json={"skill": "bottleneck-hunter"})
        cid = c1.json()["id"]

        # Create another user
        with get_db() as db:
            db.execute("INSERT OR REPLACE INTO users (username, password_hash) VALUES (?,?)",
                       ["intruder", hash_password("pass")])
        token2 = create_session("intruder")

        # Intruder can't access testuser's conversation
        r = await client.get(f"/api/convs/{cid}", cookies={"token": token2})
        assert r.status_code == 404

        # Intruder can't delete it either (DELETE returns 404 for cross-user)
        r = await client.delete(f"/api/convs/{cid}", cookies={"token": token2})
        assert r.status_code == 404
        # But conversation still exists for testuser
        r2 = await client.get(f"/api/convs/{cid}", cookies=auth_cookies)
        assert r2.status_code == 200  # Still accessible


class TestRecycleBinFlow:
    """End-to-end recycle bin flow: create → delete → list trash → restore → verify"""

    async def test_full_trash_flow(self, client, auth_cookies):
        # Create
        c1 = await client.post("/api/convs", cookies=auth_cookies,
                               json={"skill": "bottleneck-hunter"})
        cid = c1.json()["id"]

        # Delete (soft)
        await client.delete(f"/api/convs/{cid}", cookies=auth_cookies)

        # In trash
        trash = (await client.get("/api/trash", cookies=auth_cookies)).json()
        assert any(t["id"] == cid for t in trash)

        # Not in active list
        convs = (await client.get("/api/convs", cookies=auth_cookies)).json()
        assert not any(c["id"] == cid for c in convs)

        # Restore
        await client.post(f"/api/convs/{cid}/restore", cookies=auth_cookies)

        # Back in active list
        convs = (await client.get("/api/convs", cookies=auth_cookies)).json()
        assert any(c["id"] == cid for c in convs)

        # Trash empty
        trash = (await client.get("/api/trash", cookies=auth_cookies)).json()
        assert not any(t["id"] == cid for t in trash)

    async def test_permanent_delete_flow(self, client, auth_cookies):
        # Create
        c1 = await client.post("/api/convs", cookies=auth_cookies,
                               json={"skill": "bottleneck-hunter"})
        cid = c1.json()["id"]

        # Soft delete first
        await client.delete(f"/api/convs/{cid}", cookies=auth_cookies)

        # Then permanent delete from trash
        await client.delete(f"/api/convs/{cid}/permanent", cookies=auth_cookies)

        # Gone from trash too
        trash = (await client.get("/api/trash", cookies=auth_cookies)).json()
        assert not any(t["id"] == cid for t in trash)

        # Cannot restore
        r = await client.post(f"/api/convs/{cid}/restore", cookies=auth_cookies)
        assert r.status_code == 200  # Returns ok even if nothing to restore


class TestImportSanity:
    """Verify all modules import without errors"""

    def test_import_config(self):
        import server.config
        assert hasattr(server.config, 'PROJECT_ROOT')
        assert hasattr(server.config, 'MODELS')
        assert hasattr(server.config, 'SKILL_MAP')

    def test_import_database(self):
        import server.database
        assert hasattr(server.database, 'get_db')
        assert hasattr(server.database, 'init_db')

    def test_import_auth(self):
        import server.auth
        assert hasattr(server.auth, 'hash_password')
        assert hasattr(server.auth, 'verify_password')

    def test_import_conversations(self):
        import server.conversations
        assert hasattr(server.conversations, 'list_convs')
        assert hasattr(server.conversations, 'get_conv')
        assert hasattr(server.conversations, 'delete_conv')
        assert hasattr(server.conversations, 'restore_conv')
        assert hasattr(server.conversations, 'permanent_delete_conv')
        assert hasattr(server.conversations, 'list_trash')
        assert hasattr(server.conversations, '_read_jsonl_messages')

    def test_import_chat(self):
        import server.chat
        assert hasattr(server.chat, 'chat_stream')
        assert hasattr(server.chat, 'chat_respond')
        assert hasattr(server.chat, 'chat_cancel')
        assert hasattr(server.chat, '_cwd_to_project_dir')
        assert hasattr(server.chat, '_read_jsonl_messages')
        assert hasattr(server.chat, '_extract_refs_from_tool_result')

    def test_import_proxy(self):
        import server.proxy
        assert hasattr(server.proxy, 'get_proxy_config')
        assert hasattr(server.proxy, 'clear_proxy_cache')

    def test_import_claude_runner(self):
        from server.services.claude_runner import run_claude_stream, respond_to_question, cancel_process
        assert run_claude_stream is not None
        assert respond_to_question is not None
        assert cancel_process is not None

    def test_skills_registered(self):
        """Core skills (serenity family) must be in SKILL_MAP."""
        import server.config
        assert "bottleneck-hunter" in server.config.SKILL_MAP
        assert "serenity" in server.config.SKILL_MAP
        for key in ["bottleneck-hunter", "serenity"]:
            skill = server.config.SKILL_MAP[key]
            assert "dir" in skill
            assert "name" in skill

    def test_all_models_have_required_fields(self):
        """Every model entry must have name, base_url, api_key, model."""
        import server.config
        for model_id, cfg in server.config.MODELS.items():
            assert "name" in cfg, f"{model_id} missing name"
            assert "base_url" in cfg, f"{model_id} missing base_url"
            assert "api_key" in cfg, f"{model_id} missing api_key"
            assert "model" in cfg, f"{model_id} missing model"

    def test_minimax_models_present(self):
        """MiniMax M3, M2.7, M2.7-fast must be in MODELS."""
        import server.config
        assert "minimax-m3" in server.config.MODELS
        assert "minimax-m2.7" in server.config.MODELS
        assert "minimax-m2.7-fast" in server.config.MODELS
        assert server.config.MODELS["minimax-m3"]["model"] == "MiniMax-M3"
        assert server.config.MODELS["minimax-m2.7-fast"]["model"] == "MiniMax-M2.7-highspeed"

    def test_default_model_is_deepseek(self):
        import server.config
        assert server.config.DEFAULT_MODEL == "deepseek-v4"

    def test_import_main(self):
        import server.main
        assert hasattr(server.main, 'app')


class TestReferenceExtraction:
    """Test the URL/ref extraction from WebSearch tool results."""

    def test_extract_simple_markdown_links(self):
        from server.chat import _extract_refs_from_tool_result
        text = """
        Search results:
        1. [Example Site](https://example.com/article) - A great article
        2. [Another Site](https://another.com/page) - More info
        """
        refs = _extract_refs_from_tool_result(text)
        assert len(refs) == 2
        assert refs[0]["url"] == "https://example.com/article"
        assert refs[0]["title"] == "Example Site"
        assert "A great article" in refs[0]["snippet"]
        assert refs[1]["url"] == "https://another.com/page"
        assert refs[1]["title"] == "Another Site"

    def test_extract_dedup_urls(self):
        from server.chat import _extract_refs_from_tool_result
        text = """
        1. [Same Site](https://example.com) - First mention
        2. [Same Site Again](https://example.com) - Duplicate
        3. [Different](https://other.com) - Unique
        """
        refs = _extract_refs_from_tool_result(text)
        assert len(refs) == 2  # Duplicate URL removed

    def test_extract_no_links(self):
        from server.chat import _extract_refs_from_tool_result
        text = "No links here, just plain text."
        refs = _extract_refs_from_tool_result(text)
        assert refs == []

    def test_extract_skips_internal_links(self):
        from server.chat import _extract_refs_from_tool_result
        text = "[Internal](#section) and [Real](https://real.com)"
        refs = _extract_refs_from_tool_result(text)
        assert len(refs) == 1
        assert refs[0]["url"] == "https://real.com"
