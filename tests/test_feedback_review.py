"""Suggestion review scope, owner visibility and lost-response reconciliation."""

from datetime import datetime
from uuid import uuid4

import pytest

ROOT = "/api/internal/feedback-review"
REVIEW = "synthetic-review-only-" + "r" * 40
MONITOR = "synthetic-monitor-only-" + "m" * 40
WORKER = "synthetic-worker-only-" + "w" * 40
REVISION = "d" * 40


@pytest.fixture(autouse=True)
def review_setup(temp_data_dir, monkeypatch):
    from server import feedback, feedback_review
    from server.database import get_db

    feedback.init_feedback_db()
    feedback_review.init_review_db()
    monkeypatch.setenv("PRISM_FEEDBACK_REVIEW_TOKEN", REVIEW)
    monkeypatch.setenv("PRISM_FEEDBACK_MONITOR_TOKEN", MONITOR)
    monkeypatch.setenv("PRISM_FEEDBACK_WORKER_TOKEN", WORKER)
    monkeypatch.setattr(feedback_review, "_read_deployed_revision", lambda: REVISION)
    monkeypatch.setattr(
        feedback_review, "_day", lambda: datetime(2026, 11, 3, 9, 30, tzinfo=feedback._SHANGHAI)
    )
    with get_db() as db:
        for name in ("synthetic-owner", "synthetic-other"):
            db.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", [name, "synthetic-only"])


def headers(**extra):
    return {"X-Prism-Review-Token": REVIEW, "X-Prism-Expected-Revision": REVISION, **extra}


async def submit(client, category="suggestion", owner="synthetic-owner"):
    from server.database import create_session, get_db

    cookie = {"token": create_session(owner)}
    r = await client.post(
        "/api/feedback",
        cookies=cookie,
        json={
            "category": category,
            "feature": "chat",
            "title": "改进研究体验",
            "description": "请在结果中更清楚地显示来源。联系 test@example.test 或 13812345678",
            "page": "/prism",
        },
    )
    assert r.status_code == 200
    item = r.json()["feedback"]
    with get_db() as db:
        db.execute("UPDATE feedback_items SET review_batch_date='2026-11-03' WHERE id=?", [item["id"]])
    return item, cookie


def decision(**extra):
    return {
        "request_id": str(uuid4()),
        "expected_revision": 1,
        "batch_date": "2026-11-03",
        "status": "accepted",
        "reason": "符合研究目标，先补来源展示。",
        "priority": "normal",
        "dependencies": ["完成来源回归"],
        "risk": "ordinary",
        "topic": "experience",
        **extra,
    }


@pytest.mark.parametrize("credential", ["none", "wrong", "monitor", "worker", "shared", "cookie", "revision"])
async def test_distinct_version_bound_identity(client, monkeypatch, credential):
    h = headers()
    if credential == "none":
        h.pop("X-Prism-Review-Token")
    if credential in {"wrong", "monitor", "worker"}:
        h["X-Prism-Review-Token"] = {"wrong": "z" * 40, "monitor": MONITOR, "worker": WORKER}[credential]
    if credential == "shared":
        monkeypatch.setenv("PRISM_FEEDBACK_REVIEW_TOKEN", MONITOR)
    if credential == "cookie":
        h["Cookie"] = "synthetic=not-a-session"
    if credential == "revision":
        h["X-Prism-Expected-Revision"] = "b" * 40
    r = await client.get(ROOT + "/queue?batch_date=2026-11-03", headers=h)
    assert r.status_code == (409 if credential == "revision" else 401)
    assert r.headers["cache-control"] == "no-store"


async def test_queue_due_scope_and_redaction(client, monkeypatch):
    from server import feedback, feedback_review
    from server.database import get_db

    item, _ = await submit(client)
    await submit(client, "bug")
    other, _ = await submit(client, owner="synthetic-other")
    with get_db() as db:
        db.execute("UPDATE users SET disabled_at=1 WHERE username='synthetic-other'")
    r = await client.get(ROOT + "/queue?batch_date=2026-11-03", headers=headers())
    assert r.status_code == 200
    assert [v["id"] for v in r.json()["items"]] == [item["id"]]
    assert str(other["id"]) != str(item["id"])
    assert all(s not in r.text for s in ("synthetic-owner", "13812345678", "test@example.test", "/prism"))
    monkeypatch.setattr(
        feedback_review, "_day", lambda: datetime(2026, 11, 3, 9, 29, tzinfo=feedback._SHANGHAI)
    )
    r = await client.get(ROOT + "/queue?batch_date=2026-11-03", headers=headers())
    assert not r.json()["due"] and r.json()["items"] == []
    for query in (
        "batch_date=2026-11-02",
        "batch_date=2026-11-03&limit=11",
        "batch_date=2026-11-03&after_id=0&after_id=1",
        "batch_date=2026-11-03&owner=x",
    ):
        assert (await client.get(ROOT + "/queue?" + query, headers=headers())).status_code in {409, 422}


async def test_decision_exactly_once_visible_to_owner_only(client):
    from server.database import create_session, get_db

    item, cookie = await submit(client)
    body = decision()
    path = f"{ROOT}/{item['id']}/decision"
    first = await client.post(path, headers=headers(), json=body)
    assert first.status_code == 200, first.text
    again = await client.post(path, headers=headers(), json=body)
    assert again.json() == first.json()
    receipt = await client.get(ROOT + "/receipts/" + body["request_id"], headers=headers())
    assert receipt.json() == first.json()
    assert receipt.json()["deployed"] is False
    assert "尚未上线" in receipt.json()["public_reply"]
    owner = await client.get(f"/api/feedback/{item['id']}", cookies=cookie)
    assert owner.status_code == 200 and body["reason"] in owner.text
    other = await client.get(
        f"/api/feedback/{item['id']}", cookies={"token": create_session("synthetic-other")}
    )
    assert other.status_code == 404
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM feedback_updates").fetchone()[0] == 1
    assert (
        await client.post(path, headers=headers(), json={**body, "reason": "更换理由应当冲突"})
    ).status_code == 409
    assert (await client.post(path, headers=headers(), json=decision())).status_code == 409


@pytest.mark.parametrize(
    "override",
    [
        {"status": "fixed"},
        {"status": "in_progress"},
        {"status": "accepted", "risk": "owner_decision"},
        {"topic": "email_sms"},
        {"topic": "cross_product_content"},
        {"expected_revision": True},
        {"expected_revision": 2},
        {"dependencies": ["x" * 161]},
        {"request_id": "bad"},
        {"commands": ["ignored"]},
    ],
)
async def test_no_authority_escalation_or_stale_update(client, override):
    item, _ = await submit(client)
    r = await client.post(f"{ROOT}/{item['id']}/decision", headers=headers(), json=decision(**override))
    assert r.status_code in {409, 422}


async def test_role_cannot_read_accounts_history_bugs_or_change_them(client):
    item, _ = await submit(client, "bug")
    for path in (
        "/api/admin/users",
        "/api/admin/feedback",
        "/api/internal/feedback",
        "/api/internal/feedback-monitor/queue",
        "/api/feedback",
    ):
        assert (await client.get(path, headers=headers())).status_code in {401, 403}
    assert (
        await client.post(f"{ROOT}/{item['id']}/decision", headers=headers(), json=decision())
    ).status_code == 404


async def test_owner_deletion_purges_review_receipt(client):
    from server.database import get_db

    item, _ = await submit(client)
    body = decision(status="deferred")
    assert (
        await client.post(f"{ROOT}/{item['id']}/decision", headers=headers(), json=body)
    ).status_code == 200
    with get_db() as db:
        db.execute("DELETE FROM users WHERE username='synthetic-owner'")
        assert db.execute("SELECT COUNT(*) FROM feedback_review_receipts").fetchone()[0] == 0
    assert (await client.get(ROOT + "/receipts/" + body["request_id"], headers=headers())).status_code == 404
