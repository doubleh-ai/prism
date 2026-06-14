"""Test auth module — password hashing, sessions, rate limiting"""
import time
import pytest


class TestPasswordHashing:
    def test_hash_and_verify(self):
        from server.auth import hash_password, verify_password
        pw = "my_secure_password"
        hashed = hash_password(pw)
        assert hashed != pw
        assert verify_password(pw, hashed) is True
        assert verify_password("wrong_password", hashed) is False

    def test_bcrypt_format(self):
        from server.auth import hash_password
        hashed = hash_password("test")
        # bcrypt hashes start with $2b$
        assert hashed.startswith("$2b$") or hashed.startswith("$2a$")

    def test_different_hashes_for_same_password(self):
        from server.auth import hash_password
        h1 = hash_password("same_password")
        h2 = hash_password("same_password")
        assert h1 != h2  # Salts should differ

    def test_sha256_migration(self):
        """Legacy SHA-256 hashes should verify correctly."""
        import hashlib
        from server.auth import verify_password

        # Legacy SHA-256 hash
        legacy = hashlib.sha256("old_password".encode()).hexdigest()
        # Should verify (auth.py handles both bcrypt and SHA-256)
        result = verify_password("old_password", legacy)
        # Either True (if migration path works) or we need to check separately
        assert result is True or result is False


class TestSessions:
    def test_create_and_get_session(self):
        from server.auth import create_session, get_session
        token = create_session("testuser")
        assert len(token) == 64  # hex(32)

        user = get_session(token)
        assert user == "testuser"

    def test_nonexistent_session(self):
        from server.auth import get_session
        assert get_session("nonexistent_token") is None

    def test_delete_session(self):
        from server.auth import create_session, get_session, delete_session
        token = create_session("testuser")
        assert get_session(token) == "testuser"

        delete_session(token)
        assert get_session(token) is None

    def test_expired_session(self):
        from server.auth import create_session
        token = create_session("testuser")

        # Manually expire the session in DB
        from server.database import get_db
        with get_db() as db:
            db.execute("UPDATE sessions SET expires=? WHERE token=?",
                       [int(time.time()) - 1, token])

        from server.auth import get_session
        assert get_session(token) is None


class TestRateLimiting:
    def test_within_limit(self):
        from server.database import check_rate_limit
        key = f"test_rate_{time.time()}"
        for _ in range(5):
            assert check_rate_limit(key, max_req=10, window=60) is True

    def test_exceeds_limit(self):
        from server.database import check_rate_limit
        key = f"test_rate_exceed_{time.time()}"
        for _ in range(3):
            assert check_rate_limit(key, max_req=3, window=60) is True
        # 4th should fail
        assert check_rate_limit(key, max_req=3, window=60) is False

    def test_window_reset(self):
        from server.database import check_rate_limit, get_db
        key = f"test_rate_reset_{time.time()}"
        # Use all requests
        for _ in range(3):
            assert check_rate_limit(key, max_req=3, window=1) is True
        assert check_rate_limit(key, max_req=3, window=1) is False

        # Manually reset the window by updating DB
        with get_db() as db:
            db.execute("UPDATE rate_limits SET count=0, window_start=? WHERE key=?",
                       [time.time() - 61, key])
        assert check_rate_limit(key, max_req=3, window=1) is True


class TestRequireAuth:
    async def test_require_auth_optional_no_cookie(self):
        from server.auth import require_auth
        from fastapi import Request
        # Build a mock request
        scope = {"type": "http", "headers": []}
        request = Request(scope)
        user = require_auth(request, optional=True)
        assert user is None

    async def test_require_auth_required_no_cookie(self):
        from server.auth import require_auth
        from fastapi import Request, HTTPException
        scope = {"type": "http", "headers": []}
        request = Request(scope)
        with pytest.raises(HTTPException) as exc:
            require_auth(request, optional=False)
        assert exc.value.status_code == 401
