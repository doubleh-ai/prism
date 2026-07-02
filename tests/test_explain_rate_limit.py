"""/api/explain-term rate limit — 10 requests per user per minute."""
import pytest


@pytest.mark.asyncio
async def test_explain_term_429_after_10(client, test_user):
    """11th call within 60s for the same user must return 429."""
    # Authenticate
    login = await client.post(
        "/api/login",
        json={"username": test_user["username"], "password": test_user["password"]},
    )
    assert login.status_code == 200

    # First 10 calls should NOT be rate-limited (we don't care about their
    # content — the subprocess will fail in test env, but rate-limit check
    # happens BEFORE the subprocess).
    for i in range(10):
        r = await client.post("/api/explain-term", json={"term": f"term{i}"})
        assert r.status_code != 429, f"Call {i+1} unexpectedly rate-limited"

    # 11th call must be rate-limited
    r = await client.post("/api/explain-term", json={"term": "term11"})
    assert r.status_code == 429
    assert r.json()["error"] == "请求太频繁，请稍后再试"


@pytest.mark.asyncio
async def test_explain_term_rate_limit_per_user(client):
    """Different users have independent rate-limit buckets."""
    from server.database import create_user, get_db
    from server.auth import hash_password

    # Create two users
    for u in ["alice", "bob"]:
        create_user(u, hash_password("pw1234"))

    try:
        # Log in as alice and exhaust her quota
        await client.post("/api/login", json={"username": "alice", "password": "pw1234"})
        for i in range(10):
            await client.post("/api/explain-term", json={"term": f"a{i}"})
        r_alice = await client.post("/api/explain-term", json={"term": "a11"})
        assert r_alice.status_code == 429

        # Bob's bucket is independent — should not be limited
        await client.post("/api/logout")
        await client.post("/api/login", json={"username": "bob", "password": "pw1234"})
        r_bob = await client.post("/api/explain-term", json={"term": "b1"})
        assert r_bob.status_code != 429
    finally:
        with get_db() as db:
            db.execute("DELETE FROM users WHERE username IN (?,?)", ["alice", "bob"])
