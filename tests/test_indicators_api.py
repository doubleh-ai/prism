"""Indicator library CRUD + translate endpoint."""
import pytest
from unittest.mock import patch


@pytest.mark.asyncio
async def test_library_seeds_builtins(client, auth_cookies):
    r = await client.get("/api/indicators", cookies=auth_cookies)
    assert r.status_code == 200
    inds = r.json()["indicators"]
    assert len(inds) >= 6  # built-in starter indicators
    assert any(i["kind"] == "signal" for i in inds)


@pytest.mark.asyncio
async def test_create_and_delete(client, auth_cookies):
    r = await client.post("/api/indicators", cookies=auth_cookies, json={
        "name": "myMA", "kind": "plot", "dsl": {"ma": 10}, "description": "x"})
    assert r.status_code == 200
    iid = r.json()["id"]
    r = await client.get("/api/indicators", cookies=auth_cookies)
    assert "myMA" in [i["name"] for i in r.json()["indicators"]]
    r = await client.delete("/api/indicators/" + iid, cookies=auth_cookies)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_create_rejects_non_whitelist_dsl(client, auth_cookies):
    r = await client.post("/api/indicators", cookies=auth_cookies, json={
        "name": "bad", "kind": "plot", "dsl": {"exec": ["malicious"]}})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_translate_passthrough(client, auth_cookies):
    with patch("server.indicators.router.translate") as tr:
        tr.return_value = {"name": "金叉", "kind": "signal", "description": "d",
                           "dsl": {"cross_up": [{"ma": 5}, {"ma": 20}]}}
        r = await client.post("/api/indicators/translate", cookies=auth_cookies,
                              json={"input": "均线金叉"})
    assert r.status_code == 200
    assert r.json()["dsl"]["cross_up"] is not None


@pytest.mark.asyncio
async def test_translate_requires_auth(client):
    r = await client.post("/api/indicators/translate", json={"input": "x"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_isolation_between_users(client, auth_cookies):
    # user A creates an indicator
    await client.post("/api/indicators", cookies=auth_cookies, json={
        "name": "privateA", "kind": "plot", "dsl": {"ma": 5}})
    # user B (different session) should NOT see it
    from server.auth import create_session, hash_password
    from server.database import get_db
    with get_db() as db:
        db.execute("INSERT OR REPLACE INTO users(username,password_hash) VALUES(?,?)",
                   ["userB", hash_password("p")])
    b_cookies = {"token": create_session("userB")}
    r = await client.get("/api/indicators", cookies=b_cookies)
    assert "privateA" not in [i["name"] for i in r.json()["indicators"]]
