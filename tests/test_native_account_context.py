"""A shared-browser account switch cannot admit research under the other user."""

from server.auth import create_session
from server.database import get_db


async def test_account_switch_rejects_conversation_before_any_write(client, auth_cookies):
    with get_db() as db:
        db.execute("INSERT INTO users(username,password_hash) VALUES('second-owner','synthetic')")
    other = {"token": create_session("second-owner")}
    headers = {"X-Prism-Account": "testuser"}
    response = await client.post(
        "/api/convs", cookies=other, headers=headers, json={"skill": "futures-research"}
    )
    assert response.status_code == 409
    with get_db() as db:
        assert (
            db.execute("SELECT count(*) FROM conversations WHERE username='second-owner'").fetchone()[0] == 0
        )
    owned = await client.post(
        "/api/convs", cookies=auth_cookies, headers=headers, json={"skill": "futures-research"}
    )
    assert owned.status_code == 200


async def test_native_account_context_is_checked_before_model_or_admission(client, auth_cookies, monkeypatch):
    from server.agent import api

    def must_not_admit(*args, **kwargs):
        raise AssertionError("mismatched account must never reach model admission")

    monkeypatch.setattr(api, "get_service", must_not_admit)
    # Even an invalid body must be fenced by account context first.
    response = await client.post(
        "/api/agent/chat/stream", cookies=auth_cookies, headers={"X-Prism-Account": "another"}, json={}
    )
    assert response.status_code == 409
    anonymous = await client.post("/api/agent/chat/stream", headers={"X-Prism-Account": "testuser"}, json={})
    assert anonymous.status_code == 401
