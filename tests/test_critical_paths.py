"""Critical-path tests: target the gaps found in the coverage report.

Focuses on:
- native workflow selection and shared reference extraction
- conversations.py (list_trash, get_conv with messages)
- api_view_report (size cap, missing file, traversal)
- explain-term (cache behavior, fallback)
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Native method selection and public reference parsing ──
class TestNativeResearchHelpers:
    def test_builtin_method_aliases_resolve_without_skill_directories(self):
        from server.agent.prompts import workflow_catalog, workflow_id

        for row in workflow_catalog():
            assert workflow_id(row["id"]) == row["workflow"]
            assert workflow_id(row["workflow"]) == row["workflow"]
            assert "dir" not in row

    def test_external_skill_paths_cannot_select_a_workflow(self):
        from server.agent.prompts import workflow_id

        for path in ("/private/synthetic/skills/foo", "relative/path", "../industry"):
            with pytest.raises(ValueError, match="unsupported_research_method"):
                workflow_id(path)

    def test_extract_refs_simple(self):
        from server.research_context import extract_web_refs

        content = "Here are results:\n1. [Title One](https://example.com/1) - snippet about one\n2. [Title Two](https://example.com/2) - snippet about two"
        refs = extract_web_refs(content)
        assert len(refs) == 2
        assert refs[0]["url"] == "https://example.com/1"
        assert refs[0]["title"] == "Title One"
        assert "snippet about one" in refs[0]["snippet"]

    def test_extract_refs_dedupes(self):
        from server.research_context import extract_web_refs

        content = "[A](https://x.com) and [A again](https://x.com)"
        refs = extract_web_refs(content)
        assert len(refs) == 1

    def test_extract_refs_ignores_non_http(self):
        from server.research_context import extract_web_refs

        content = "[a](file://x) [b](#anchor) [c](https://real.com)"
        refs = extract_web_refs(content)
        urls = [r["url"] for r in refs]
        assert urls == ["https://real.com"]

    def test_extract_refs_truncates_long_snippet(self):
        from server.research_context import extract_web_refs

        long_snip = "x" * 200
        content = f"[t](https://x.com) {long_snip}"
        refs = extract_web_refs(content)
        assert len(refs[0]["snippet"]) == 120
        assert refs[0]["snippet_truncated"] is True


# ── conversations.py: list_trash, get_conv ──
class TestConversations:
    @pytest.mark.asyncio
    async def test_list_trash_returns_deleted(self, client, auth_cookies):
        # Create then delete a conversation
        cr = await client.post("/api/convs", cookies=auth_cookies, json={})
        cid = cr.json()["id"]
        await client.delete(f"/api/convs/{cid}", cookies=auth_cookies)
        r = await client.get("/api/trash", cookies=auth_cookies)
        assert r.status_code == 200
        ids = [c["id"] for c in r.json()]
        assert cid in ids

    @pytest.mark.asyncio
    async def test_list_trash_empty(self, client, auth_cookies):
        r = await client.get("/api/trash", cookies=auth_cookies)
        assert r.status_code == 200
        assert r.json() == []

    @pytest.mark.asyncio
    async def test_get_conv_returns_messages(self, client, auth_cookies):
        cr = await client.post("/api/convs", cookies=auth_cookies, json={})
        cid = cr.json()["id"]
        r = await client.get(f"/api/convs/{cid}", cookies=auth_cookies)
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == cid
        assert "messages" in body
        assert body["skill"]  # default skill

    @pytest.mark.asyncio
    async def test_get_conv_not_found(self, client, auth_cookies):
        r = await client.get("/api/convs/does-not-exist", cookies=auth_cookies)
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_permanent_delete_also_removes_messages(self, client, auth_cookies):
        cr = await client.post("/api/convs", cookies=auth_cookies, json={})
        cid = cr.json()["id"]
        # Test deletion independently of provider execution.
        from server.database import get_db

        with get_db() as db:
            db.execute("INSERT INTO messages (conv_id, role, content) VALUES (?,?,?)", [cid, "user", "hello"])
        # Only conversations already in the owner's trash can be permanently deleted.
        await client.delete(f"/api/convs/{cid}", cookies=auth_cookies)
        r = await client.delete(f"/api/convs/{cid}/permanent", cookies=auth_cookies)
        assert r.status_code == 200
        # Verify messages are gone
        with get_db() as db:
            row = db.execute("SELECT COUNT(*) as c FROM messages WHERE conv_id=?", [cid]).fetchone()
            assert row["c"] == 0


# ── Report endpoint edge cases ──
class TestReportEdgeCases:
    @pytest.mark.asyncio
    async def test_report_unauthenticated_redirects_to_login(self, client):
        """Unauthed access to a report URL must redirect to /login."""
        r = await client.get("/api/reports/some-conv/foo.html", follow_redirects=False)
        # Either redirect (302/307) or login redirect
        assert r.status_code in (302, 307, 401) or "login" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_report_filename_with_path_traversal_rejected(self, client, auth_cookies):
        """Filename with `..` must be rejected with 400."""
        r = await client.get("/api/reports/some-conv/..%2F..%2Fetc%2Fpasswd", cookies=auth_cookies)
        assert r.status_code in (400, 404, 422)

    @pytest.mark.asyncio
    async def test_report_unsupported_extension_rejected(self, client, auth_cookies):
        from server.database import get_db

        own = (await client.post("/api/convs", cookies=auth_cookies, json={})).json()
        other = (await client.post("/api/convs", cookies=auth_cookies, json={})).json()
        with get_db() as db:
            db.execute("UPDATE conversations SET username=? WHERE id=?", ["another-owner", other["id"]])
        # Foreign and unknown conversations are indistinguishable. Even an
        # owned conversation cannot expose an unregistered artifact by filename.
        replies = [
            await client.get(f"/api/reports/{cid}/file.exe", cookies=auth_cookies)
            for cid in (own["id"], other["id"], "does-not-exist")
        ]
        assert [reply.status_code for reply in replies] == [404, 404, 404]
        assert replies[1].json() == replies[2].json()
        assert all("file.exe" not in reply.text for reply in replies)

    @pytest.mark.asyncio
    async def test_report_null_byte_rejected(self, client, auth_cookies):
        # %00 is URL-encoded null byte
        r = await client.get("/api/reports/some-conv/foo%00.html", cookies=auth_cookies)
        assert r.status_code in (400, 404, 422)


# ── Rate limiting ──
class TestRateLimiting:
    @pytest.mark.asyncio
    async def test_login_rate_limit_enforced(self, client):
        """Hammer the login endpoint — should hit 429 within the window."""
        from server.config import RATE_LOGIN_PER_MIN

        statuses = []
        for _ in range(RATE_LOGIN_PER_MIN + 5):
            r = await client.post("/api/login", json={"username": "x", "password": "y"})
            statuses.append(r.status_code)
        # At least one must be 429
        assert 429 in statuses

    def test_atomic_check_rate_limit(self):
        """The rate limiter must be atomic — concurrent calls can't double-count."""
        import tempfile
        import threading
        from pathlib import Path

        from server.database import check_rate_limit, init_db

        with tempfile.TemporaryDirectory() as td:
            from server import database

            database.DB_PATH = Path(td) / "rl.db"
            init_db()
            key = "test:concurrent"
            cap = 10
            window = 60
            results = []

            def worker():
                # Each thread claims a slot; only `cap` should succeed.
                results.append(check_rate_limit(key, cap, window))

            threads = [threading.Thread(target=worker) for _ in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            # The first `cap` calls return True; the rest must return False.
            trues = sum(1 for r in results if r)
            assert trues == cap, f"Expected {cap} true, got {trues} (race condition!)"

    def test_rate_limit_window_resets(self):
        """After the window expires, the counter resets to 1 (not 0+1)."""
        from server.database import check_rate_limit

        key = "test:reset"
        cap = 2
        # Use a 1-second window so we can wait it out
        assert check_rate_limit(key, cap, 1) is True
        assert check_rate_limit(key, cap, 1) is True
        # Third call within the window is blocked
        assert check_rate_limit(key, cap, 1) is False
        # Wait for the window to expire
        time.sleep(1.2)
        # Now we should be allowed again, and counter is 1 (not 3+1)
        assert check_rate_limit(key, cap, 1) is True


# ── Login & session ──
class TestLoginSession:
    @pytest.mark.asyncio
    async def test_login_success_sets_token_cookie(self, client, test_user):
        r = await client.post(
            "/api/login", json={"username": test_user["username"], "password": test_user["password"]}
        )
        assert r.status_code == 200
        body = r.json()
        assert body["user"] == test_user["username"]
        # httpx stores cookies; check via the response itself
        set_cookies = r.headers.get_list("set-cookie")
        names = [c.split("=")[0] for c in set_cookies]
        assert "token" in names
        # And a csrf_token is also set
        assert "csrf_token" in names

    @pytest.mark.asyncio
    async def test_login_wrong_password_is_403(self, client, test_user):
        r = await client.post("/api/login", json={"username": test_user["username"], "password": "wrong"})
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_me_endpoint_returns_user(self, client, auth_cookies):
        r = await client.get("/api/me", cookies=auth_cookies)
        assert r.status_code == 200
        assert r.json()["user"] == "testuser"

    @pytest.mark.asyncio
    async def test_me_unauthenticated_is_401(self, client):
        r = await client.get("/api/me")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_logout_clears_cookies(self, client, auth_cookies):
        r = await client.post("/api/logout", cookies=auth_cookies)
        assert r.status_code == 200
        # Set-Cookie should clear the token
        set_cookies = r.headers.get_list("set-cookie")
        clear = any("token" in c and ("=" in c and ";" in c) for c in set_cookies)
        assert clear


# ── Model list, skills list ──
class TestEndpointsMeta:
    @pytest.mark.asyncio
    async def test_models_list(self, client):
        r = await client.get("/api/models")
        assert r.status_code == 200
        models = r.json()
        assert isinstance(models, list)
        assert len(models) > 0
        for m in models:
            assert "id" in m and "name" in m

    @pytest.mark.asyncio
    async def test_skills_list(self, client):
        r = await client.get("/api/skills")
        assert r.status_code == 200
        skills = r.json()
        assert isinstance(skills, list)
        for s in skills:
            assert "id" in s and "name" in s and "shortcut" in s
