"""Synthetic recovery journeys. No real mail, credentials or production accounts."""

import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from server import password_recovery as recovery
from server.auth import authenticated_session, create_session, hash_password, verify_password
from server.database import get_db, get_session
from server.recovery_mail import DeliveryUnavailable

PASSWORD = "new-long-test-password"


@pytest.fixture
def configured(monkeypatch):
    values = {
        "ENABLED": "1",
        "USER": "admin",
        "EMAIL": "owner@example.com",
        "SENDER": "noreply@example.org",
        "TEMPLATE_ID": "123",
        "REGION": "ap-hongkong",
        "SECRET_ID": "synthetic-id",
        "SECRET_KEY": "synthetic-key",
    }
    for key, value in values.items():
        monkeypatch.setenv("PRISM_ADMIN_RECOVERY_" + key, value)
    monkeypatch.setattr("server.auth.ADMIN_USERS", {"admin"})
    with get_db() as db:
        db.execute(
            "INSERT INTO users(username,password_hash) VALUES (?,?)", ["admin", hash_password("old-password")]
        )
    sent = []
    monkeypatch.setattr(recovery, "send_recovery_code", lambda cfg, code: sent.append((cfg, code)))
    return sent


def payload(challenge, code, password=PASSWORD):
    return {"challenge_id": challenge, "code": code, "password": password, "confirm_password": password}


def issued(configured):
    result = recovery.request_code()
    return payload(result["challenge_id"], configured[-1][1])


async def test_complete_recovery_revokes_sessions_then_normal_login(client, configured):
    old_token = create_session("admin")
    response = await client.post("/api/auth/recovery/request", json={})
    assert response.status_code == 200
    assert "owner@example.com" not in response.text
    body = payload(response.json()["challenge_id"], configured[-1][1])
    with get_db() as db:
        record = dict(db.execute("SELECT * FROM admin_recovery_challenges").fetchone())
    assert body["code"] not in record.values()
    response = await client.post("/api/auth/recovery/reset", json=body)
    assert response.status_code == 200
    assert not response.cookies
    assert get_session(old_token) is None
    assert authenticated_session("admin", "old-password") is None
    assert authenticated_session("admin", PASSWORD)
    assert (await client.post("/api/auth/recovery/reset", json=body)).status_code == 400


@pytest.mark.parametrize(
    "setting,value",
    [
        ("ENABLED", "0"),
        ("EMAIL", ""),
        ("EMAIL", "x@example.com\r\nBcc:x@example.org"),
        ("USER", "ordinary"),
        ("REGION", "arbitrary-host"),
        ("TEMPLATE_ID", "0"),
        ("TEMPLATE_ID", "bad"),
        ("SECRET_KEY", ""),
    ],
)
async def test_fail_closed_configuration(client, configured, monkeypatch, setting, value):
    monkeypatch.setenv("PRISM_ADMIN_RECOVERY_" + setting, value)
    assert (await client.get("/api/auth/recovery/status")).json() == {"configured": False}
    assert (await client.post("/api/auth/recovery/request", json={})).status_code == 503
    assert not configured


async def test_no_recipient_selection_and_admin_only_status(client, configured, test_user):
    response = await client.post("/api/auth/recovery/request", json={"email": "attacker@example.net"})
    assert response.status_code == 422 and not configured
    assert (await client.get("/api/admin/recovery")).status_code == 401
    client.cookies.set("token", create_session(test_user["username"]))
    assert (await client.get("/api/admin/recovery")).status_code == 403
    client.cookies.set("token", create_session("admin"))
    result = (await client.get("/api/admin/recovery")).json()
    assert result == {"configured": True, "passwords_readable": False, "recipient": "o***@example.com"}


@pytest.mark.parametrize(
    "change", ["expired", "disabled", "deleted", "password_changed", "email_changed", "pending"]
)
def test_invalidated_challenges_never_change_account(configured, monkeypatch, change):
    body = issued(configured)
    with get_db() as db:
        if change == "expired":
            db.execute("UPDATE admin_recovery_challenges SET expires_at=0")
        elif change == "disabled":
            db.execute("UPDATE users SET disabled_at=1 WHERE username='admin'")
        elif change == "deleted":
            db.execute("DELETE FROM users WHERE username='admin'")
        elif change == "password_changed":
            db.execute("UPDATE users SET password_hash=? WHERE username='admin'", [hash_password("another")])
        elif change == "pending":
            db.execute("UPDATE admin_recovery_challenges SET state='pending'")
        else:
            monkeypatch.setenv("PRISM_ADMIN_RECOVERY_EMAIL", "different@example.com")
    with pytest.raises(recovery.HTTPException) as error:
        recovery.reset_password(body)
    assert error.value.status_code == 400
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE username='admin'").fetchone()
    if change == "disabled":
        assert user["disabled_at"] == 1
    if user:
        assert not verify_password(PASSWORD, user["password_hash"])


def test_wrong_attempts_are_committed_and_lock_only_challenge(configured):
    body = issued(configured)
    wrong = "00000000" if body["code"] != "00000000" else "11111111"
    for _ in range(5):
        with pytest.raises(recovery.HTTPException):
            recovery.reset_password({**body, "code": wrong})
    with pytest.raises(recovery.HTTPException):
        recovery.reset_password(body)
    assert authenticated_session("admin", "old-password")
    with get_db() as db:
        assert db.execute("SELECT attempts FROM admin_recovery_challenges").fetchone()[0] == 5


def test_no_send_retry_and_failed_delivery_not_usable(configured, monkeypatch):
    def unavailable(cfg, code):
        configured.append((cfg, code))
        raise DeliveryUnavailable()

    monkeypatch.setattr(recovery, "send_recovery_code", unavailable)
    with pytest.raises(recovery.HTTPException) as error:
        recovery.request_code()
    assert error.value.status_code == 502
    assert len(configured) == 1
    with get_db() as db:
        row = db.execute("SELECT id,state FROM admin_recovery_challenges").fetchone()
        assert row["state"] == "delivery_failed"
    with pytest.raises(recovery.HTTPException):
        recovery.reset_password(payload(row["id"], configured[0][1]))
    with pytest.raises(recovery.HTTPException) as limited:
        recovery.request_code()
    assert limited.value.status_code == 429 and len(configured) == 1


def test_rate_limits_survive_resend_and_bound_daily_mail(configured, monkeypatch):
    clock = [1000000]
    monkeypatch.setattr("time.time", lambda: clock[0])
    for _ in range(6):
        recovery.request_code()
        clock[0] += 61
    with pytest.raises(recovery.HTTPException) as limited:
        recovery.request_code()
    assert limited.value.status_code == 429
    clock[0] += 3601
    for _ in range(6):
        recovery.request_code()
        clock[0] += 61
    clock[0] += 3601
    with pytest.raises(recovery.HTTPException) as limited:
        recovery.request_code()
    assert limited.value.status_code == 429 and len(configured) == 12


def test_concurrent_code_consumption_only_one_wins(configured):
    body = issued(configured)

    def reset_once():
        try:
            recovery.reset_password(body)
            return 200
        except recovery.HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(lambda _: reset_once(), range(2))) == [200, 400]


def test_reset_rolls_back_password_and_consumption_if_revocation_fails(configured):
    body = issued(configured)
    create_session("admin")
    with get_db() as db:
        db.execute(
            "CREATE TRIGGER fail_revoke BEFORE DELETE ON sessions BEGIN SELECT RAISE(ABORT, 'synthetic'); END"
        )
    with pytest.raises(Exception, match="synthetic"):
        recovery.reset_password(body)
    with get_db() as db:
        assert verify_password(
            "old-password", db.execute("SELECT password_hash FROM users WHERE username='admin'").fetchone()[0]
        )
        assert db.execute("SELECT state FROM admin_recovery_challenges").fetchone()[0] == "sent"


async def test_csrf_body_limits_and_secret_free_errors(client, configured):
    from server.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as raw:
        assert (await raw.post("/api/auth/recovery/request", json={})).status_code == 403
    secret = "sensitive-test-password"
    for body in ([secret], {"password": secret}, {"code": {"value": secret}}):
        response = await client.post("/api/auth/recovery/reset", json=body)
        assert response.status_code == 422 and secret not in response.text
    response = await client.post("/api/auth/recovery/reset", content=b"x" * 2049)
    assert response.status_code == 413
    assert (await client.post("/api/auth/recovery/reset", content=b"{")).status_code == 422


@pytest.mark.parametrize("password", ["short", "x" * 73, "中" * 25, "\ud800" * 12])
def test_password_limits(configured, password):
    body = issued(configured)
    with pytest.raises(recovery.HTTPException) as error:
        recovery.reset_password({**body, "password": password, "confirm_password": password})
    assert error.value.status_code == 422


def test_password_confirmation(configured):
    body = issued(configured)
    with pytest.raises(recovery.HTTPException) as error:
        recovery.reset_password({**body, "confirm_password": "different"})
    assert error.value.status_code == 422


async def test_public_page_readable_and_never_exposes_recipient(client, configured):
    for url in ("/recover", "/api/auth/recovery/status"):
        response = await client.get(url)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "owner@example.com" not in response.text
    assert "/recover" in (await client.get("/login")).text


def test_login_is_atomic_with_reset(configured, monkeypatch):
    import threading

    import server.auth as auth

    body = issued(configured)
    started, reset_started = threading.Event(), threading.Event()
    original = auth.create_session

    def create_inside_transaction(username):
        started.set()
        assert reset_started.wait(3)
        return original(username)

    monkeypatch.setattr(auth, "create_session", create_inside_transaction)

    def reset_after_validation():
        assert started.wait(3)
        reset_started.set()
        return recovery.reset_password(body)

    with ThreadPoolExecutor(2) as pool:
        login = pool.submit(authenticated_session, "admin", "old-password")
        reset = pool.submit(reset_after_validation)
        token = login.result(timeout=10)
        reset.result(timeout=10)
    assert token and get_session(token) is None


@pytest.mark.parametrize("outcome", ["accepted", "provider_error", "malformed", "redirect", "timeout"])
def test_fixed_mail_destination_template_signature_and_no_retry(configured, monkeypatch, outcome):
    import server.recovery_mail as mail

    calls = []

    def handle(request):
        calls.append(request)
        assert str(request.url) == "https://ses.tencentcloudapi.com/"
        data = json.loads(request.content)
        assert data["Destination"] == ["owner@example.com"]
        assert data["Template"] == {"TemplateID": 123, "TemplateData": '{"code":"12345678"}'}
        assert data["TriggerType"] == 1
        assert "Simple" not in data
        assert "Signature=" in request.headers["Authorization"]
        if outcome == "timeout":
            raise httpx.ReadTimeout("sensitive synthetic error", request=request)
        if outcome == "redirect":
            return httpx.Response(302, headers={"Location": "https://untrusted.invalid"})
        if outcome == "provider_error":
            return httpx.Response(200, json={"Response": {"Error": {"Message": "sensitive synthetic error"}}})
        if outcome == "malformed":
            return httpx.Response(200, text="sensitive synthetic error")
        return httpx.Response(200, json={"Response": {"MessageId": "test"}})

    real_client = httpx.Client

    def client(**kwargs):
        assert kwargs == {"timeout": 10, "trust_env": False, "follow_redirects": False}
        return real_client(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(mail.httpx, "Client", client)
    if outcome == "accepted":
        mail.send_recovery_code(recovery.settings(), "12345678")
    else:
        with pytest.raises(DeliveryUnavailable) as error:
            mail.send_recovery_code(recovery.settings(), "12345678")
        assert not str(error.value)
    assert len(calls) == 1


def test_reset_between_password_check_and_session_issue(configured, monkeypatch):
    import server.auth as auth

    body = issued(configured)
    original = auth.verify_password

    def reset_during_check(password, hashed, **kwargs):
        valid = original(password, hashed, **kwargs)
        recovery.reset_password(body)
        return valid

    monkeypatch.setattr(auth, "verify_password", reset_during_check)
    assert authenticated_session("admin", "old-password") is None


def test_legacy_login_still_upgrades_hash_atomically(configured):
    import hashlib

    from server.config import SECRET_KEY

    legacy = hashlib.sha256(("legacy-password" + SECRET_KEY).encode()).hexdigest()
    with get_db() as db:
        db.execute("UPDATE users SET password_hash=? WHERE username='admin'", [legacy])
    assert authenticated_session("admin", "legacy-password")
    with get_db() as db:
        assert (
            db.execute("SELECT password_hash FROM users WHERE username='admin'")
            .fetchone()[0]
            .startswith("$2")
        )


def test_recovery_frontend_runtime():
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    assert node, "Node is required for the recovery frontend regression"
    result = subprocess.run(
        [node, "tests/password_recovery_runtime.cjs"],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 password recovery frontend scenarios passed" in result.stdout
