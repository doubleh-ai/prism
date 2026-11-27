"""Tests for the Authelia-backed loopback operations gateway and external admin entry."""

import os
from pathlib import Path

import httpx

from server.auth import hash_password
from server.database import create_user
from server.main import app


def _write_private_token(tmp_path: Path, value: str, mode: int = 0o600) -> Path:
    token_file = tmp_path / "ops_gateway_token"
    token_file.write_text(value + "\n", encoding="utf-8")
    os.chmod(token_file, mode)
    return token_file


async def test_ops_gateway_authenticates_loopback_with_private_token_and_rejects_misuse(
    client, monkeypatch, tmp_path
):
    monkeypatch.setattr("server.auth.ADMIN_USERS", {"admin"})
    create_user("admin", hash_password("adminpass12345"))
    secret = "ops_gateway_test_secret_0123456789abcdef"
    token_file = _write_private_token(tmp_path, secret, 0o600)
    monkeypatch.setenv("PRISM_OPS_GATEWAY_TOKEN_FILE", str(token_file))

    # 1. Missing headers -> 401
    r0 = await client.get("/api/ops/me")
    assert r0.status_code == 401

    # 2. Valid loopback + private token + SSO principal -> resolves active admin account
    headers = {
        "X-Prism-Ops-Token": secret,
        "X-Prism-Ops-Principal": "ops-operator",
        "X-Requested-With": "XMLHttpRequest",
    }
    me = await client.get("/api/ops/me", headers=headers)
    assert me.status_code == 200
    body = me.json()
    assert body["is_admin"] is True
    assert body["principal"] == "ops-operator"
    assert body["auth_mode"] == "ops_gateway"
    assert body["user"] == "admin"

    # 3. Admin read & Futures V2 ops endpoints work seamlessly over gateway
    stats = await client.get("/api/admin/stats", headers=headers)
    assert stats.status_code == 200 and "totals" in stats.json()
    fops = await client.get("/api/admin/futures-ops", headers=headers)
    assert fops.status_code == 200
    assert fops.json()["universe_size"] == 46
    assert "auto_interpretations" in fops.json()

    # 4. Non-loopback client address is strictly rejected even with valid token
    transport = httpx.ASGITransport(app=app, client=("203.0.113.25", 44321))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as remote_client:
        remote_res = await remote_client.get("/api/ops/me", headers=headers)
        assert remote_res.status_code == 401

    # 5. World-readable token file fails closed -> 401
    os.chmod(token_file, 0o644)
    insecure = await client.get("/api/ops/me", headers=headers)
    assert insecure.status_code == 401


async def test_prism_sidebar_removes_admin_entry_and_deprivileges_admin_account(client, monkeypatch):
    from server.auth import create_session

    monkeypatch.setattr("server.auth.ADMIN_USERS", {"admin"})
    create_user("admin", hash_password("adminpass12345"))
    admin_cookies = {"token": create_session("admin")}

    html = Path("templates/app.html").read_text(encoding="utf-8")
    assert 'id="adminEntry"' not in html
    assert "/ops/" not in html
    assert "onclick=\"App.switchMode('admin')\"" not in html

    me = await client.get("/api/me", cookies=admin_cookies)
    assert me.status_code == 200
    assert me.json() == {"user": "admin", "is_admin": False}

    for path in ("/api/ops/me", "/api/admin/stats", "/api/admin/users"):
        denied = await client.get(path, cookies=admin_cookies)
        assert denied.status_code == 403
