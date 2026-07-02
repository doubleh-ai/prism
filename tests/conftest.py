"""Test fixtures — isolated temp DB, test client, test user"""
import os
import sys
import tempfile
import json
from pathlib import Path
import pytest

# Ensure server package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def temp_data_dir(monkeypatch):
    """Use a temp directory for DATA_DIR so tests don't touch production DB."""
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("STOCK_DATA_DIR", td)
        # Re-import config to pick up new DATA_DIR
        import server.config
        server.config.DATA_DIR = Path(td)
        server.config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        # Reset DB_PATH
        import server.database
        server.database.DB_PATH = Path(td) / "chat.db"
        # Reset thread-local connection (Task 4) — without this, the
        # previous test's connection (pointing to a deleted tempdir)
        # would be reused.
        server.database.reset_connection()
        server.database.init_db()
        yield td
        # Teardown: close the connection to the temp DB
        server.database.reset_connection()


@pytest.fixture
def test_user():
    """Create a test user and return credentials."""
    from server.database import get_db
    from server.auth import hash_password
    username = "testuser"
    password = "testpass123"
    pw_hash = hash_password(password)
    with get_db() as db:
        db.execute("INSERT OR REPLACE INTO users (username, password_hash) VALUES (?,?)",
                   [username, pw_hash])
    return {"username": username, "password": password}


@pytest.fixture
def auth_cookies(test_user):
    """Login and return auth cookies dict."""
    from server.auth import create_session
    token = create_session(test_user["username"])
    return {"token": token}


# CSRF header — every state-changing test request must include this.
# Mirrors what the real frontend does.
CSRF_HEADERS = {"X-Requested-With": "XMLHttpRequest"}


@pytest.fixture
async def client():
    """Async HTTP test client for FastAPI with CSRF headers pre-injected."""
    from httpx import ASGITransport, AsyncClient
    from server.main import app
    transport = ASGITransport(app=app)

    async def _send(method, url, **kw):
        # Auto-merge CSRF header onto all mutating requests
        headers = dict(kw.pop("headers", {}) or {})
        if method.upper() in ("POST", "PUT", "DELETE", "PATCH"):
            for k, v in CSRF_HEADERS.items():
                headers.setdefault(k, v)
        return await _client.request(method, url, headers=headers, **kw)

    async with AsyncClient(transport=transport, base_url="http://test") as c:
        _client = c
        # Wrap with a helper that auto-injects CSRF headers
        class CSRFClient:
            def __getattr__(self, name):
                return getattr(c, name)
        # Simpler: monkey-patch the request method
        orig_request = c.request

        async def patched_request(method, url, **kw):
            headers = dict(kw.pop("headers", {}) or {})
            if method.upper() in ("POST", "PUT", "DELETE", "PATCH"):
                for k, v in CSRF_HEADERS.items():
                    headers.setdefault(k, v)
            return await orig_request(method, url, headers=headers, **kw)

        c.request = patched_request
        yield c


@pytest.fixture
def sample_jsonl():
    """Create a temporary JSONL file matching Claude Code format."""
    import uuid
    import shutil
    sid = str(uuid.uuid4())
    d = tempfile.mkdtemp()
    path = Path(d) / f"{sid}.jsonl"
    lines = [
        json.dumps({"type": "user", "message": {"role": "user", "content": "测试问题？"}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "用户问了一个测试问题，需要简单回答。"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "echo test"}},
            {"type": "text", "text": "这是一个测试回答。"}
        ]}}),
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    yield sid, path
    shutil.rmtree(d, ignore_errors=True)
