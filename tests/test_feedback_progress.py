"""Ordinary owner visibility, narrow machine identity and durable progress receipts."""

from uuid import uuid4

import pytest

ROOT = "/api/internal/feedback-progress"
PROGRESS = "synthetic-progress-only-" + "p" * 40
MONITOR = "synthetic-monitor-only-" + "m" * 40
REVIEW = "synthetic-review-only-" + "r" * 40
WORKER = "synthetic-worker-only-" + "w" * 40
REVISION = "d" * 40


@pytest.fixture(autouse=True)
def progress_setup(temp_data_dir, monkeypatch):
    from server import feedback, feedback_progress
    from server.database import get_db

    feedback.init_feedback_db()
    feedback_progress.init_progress_db()
    for name, value in (("PROGRESS", PROGRESS), ("MONITOR", MONITOR), ("REVIEW", REVIEW), ("WORKER", WORKER)):
        monkeypatch.setenv("PRISM_FEEDBACK_" + name + "_TOKEN", value)
    monkeypatch.setattr(feedback_progress, "_read_deployed_revision", lambda: REVISION)
    monkeypatch.setattr(feedback, "_read_deployed_revision", lambda: REVISION)
    with get_db() as db:
        for name in ("synthetic-owner", "synthetic-other"):
            db.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", [name, "synthetic-only"])


def headers(**extra):
    return {"X-Prism-Progress-Token": PROGRESS, "X-Prism-Expected-Revision": REVISION, **extra}


async def submit(client, **overrides):
    from server.database import create_session

    cookie = {"token": create_session("synthetic-owner")}
    result = await client.post(
        "/api/feedback",
        cookies=cookie,
        json={
            "category": "bug",
            "feature": "chat",
            "title": "PRIVATE_TITLE",
            "description": "PRIVATE_REPORT_BODY",
            "page": "/private/user/path",
            **overrides,
        },
    )
    assert result.status_code == 200
    return result.json()["feedback"], cookie


def payload(item, **overrides):
    return {
        "request_id": str(uuid4()),
        "expected_revision": item["revision_number"],
        "reported_revision": REVISION[:12],
        "stage": "reproducing",
        **overrides,
    }


def count_updates():
    from server.database import get_db

    with get_db() as db:
        return db.execute("SELECT COUNT(*) FROM feedback_updates").fetchone()[0]


async def test_progress_is_visible_only_in_owners_existing_record(client):
    from server.database import create_session

    item, cookie = await submit(client)
    result = await client.post(f"{ROOT}/{item['id']}/update", headers=headers(), json=payload(item))
    assert result.status_code == 200, result.text
    receipt = result.json()
    assert receipt["stage"] == "reproducing" and receipt["status"] == "in_progress"
    assert receipt["fixed"] is False and receipt["deployed"] is False and receipt["changed"] is True
    assert result.headers["cache-control"] == "no-store"
    assert not any(
        secret in result.text
        for secret in ("PRIVATE_TITLE", "PRIVATE_REPORT_BODY", "synthetic-owner", "/private/user/path")
    )
    owned = await client.get(f"/api/feedback/{item['id']}", cookies=cookie)
    assert owned.status_code == 200
    detail = owned.json()["feedback"]
    assert detail["public_reply"] == receipt["public_reply"]
    assert detail["updates"][-1]["public_reply"] == receipt["public_reply"]
    assert "actor" not in detail["updates"][-1]
    other = {"token": create_session("synthetic-other")}
    assert (await client.get(f"/api/feedback/{item['id']}", cookies=other)).status_code == 404


@pytest.mark.parametrize("kind", ["none", "monitor", "review", "worker", "cookie", "duplicate"])
async def test_other_identities_and_duplicate_header_are_rejected(client, kind):
    from httpx import ASGITransport, AsyncClient

    from server.main import app

    item, cookie = await submit(client)
    supplied = []
    if kind != "none":
        value = {"monitor": MONITOR, "review": REVIEW, "worker": WORKER}.get(kind, PROGRESS)
        supplied = [("X-Prism-Progress-Token", value)]
    if kind == "duplicate":
        supplied *= 2
    supplied += [("X-Prism-Expected-Revision", REVISION), ("X-Requested-With", "XMLHttpRequest")]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as raw:
        response = await raw.post(
            f"{ROOT}/{item['id']}/update",
            headers=supplied,
            cookies=cookie if kind == "cookie" else None,
            json=payload(item),
        )
    assert response.status_code == 401
    assert count_updates() == 0


@pytest.mark.parametrize("value", ["", "short", " " * 40, "x" * 513, MONITOR, REVIEW, WORKER])
async def test_disabled_invalid_and_shared_progress_secret_fail_closed(client, monkeypatch, value):
    item, _ = await submit(client)
    monkeypatch.setenv("PRISM_FEEDBACK_PROGRESS_TOKEN", value)
    result = await client.post(f"{ROOT}/{item['id']}/update", headers=headers(), json=payload(item))
    assert result.status_code == 401 and count_updates() == 0


async def test_progress_identity_cannot_read_private_content_or_use_other_roles(client):
    item, _ = await submit(client)
    for path in (
        "/api/admin/users",
        "/api/admin/feedback",
        "/api/internal/feedback",
        f"/api/internal/feedback/{item['id']}",
        "/api/internal/feedback-monitor/queue",
        "/api/internal/feedback-review/queue?batch_date=2026-11-03",
        "/api/feedback",
    ):
        assert (await client.get(path, headers=headers())).status_code in {401, 403}
    for role, key, path in (
        ("PRISM_FEEDBACK_MONITOR_TOKEN", "X-Prism-Monitor-Token", "/api/internal/feedback-monitor/queue"),
        (
            "PRISM_FEEDBACK_REVIEW_TOKEN",
            "X-Prism-Review-Token",
            "/api/internal/feedback-review/queue?batch_date=2026-11-03",
        ),
        ("PRISM_FEEDBACK_WORKER_TOKEN", "X-Prism-Feedback-Token", "/api/internal/feedback"),
    ):
        # Equal environment slots disable both sides, not merely the new one.
        from pytest import MonkeyPatch

        with MonkeyPatch.context() as patch:
            patch.setenv(role, PROGRESS)
            assert (
                await client.get(path, headers={key: PROGRESS, "X-Prism-Expected-Revision": REVISION})
            ).status_code == 401


@pytest.mark.parametrize(
    "changes",
    [
        {"stage": "fixed"},
        {"stage": "deployed"},
        {"public_reply": "untrusted secret must not echo"},
        {"verification": "pretend complete"},
        {"expected_revision": True},
        {"expected_revision": "1"},
        {"reported_revision": "a" * 40},
        {"request_id": "x" * 36},
    ],
)
async def test_body_is_closed_and_cannot_mark_a_fix_or_supply_public_text(client, changes):
    item, _ = await submit(client)
    result = await client.post(
        f"{ROOT}/{item['id']}/update", headers=headers(), json=payload(item, **changes)
    )
    assert result.status_code == 422 and count_updates() == 0
    assert "untrusted secret" not in result.text


async def test_duplicate_json_field_and_wrong_runtime_do_not_write(client):
    item, _ = await submit(client)
    body = payload(item)
    import json

    raw = json.dumps(body)[:-1] + ',"stage":"fixed"}'
    response = await client.post(f"{ROOT}/{item['id']}/update", headers=headers(), content=raw)
    assert response.status_code == 422
    assert (
        await client.post(
            f"{ROOT}/{item['id']}/update",
            headers=headers(**{"X-Prism-Expected-Revision": "a" * 40}),
            json=body,
        )
    ).status_code == 409
    assert (
        await client.post(
            f"{ROOT}/{item['id']}/update", headers=headers(), json={**body, "reported_revision": "a" * 12}
        )
    ).status_code == 409
    assert count_updates() == 0


async def test_replay_and_receipt_lookup_are_durable_without_duplicate_history(client):
    item, _ = await submit(client)
    body = payload(item)
    first = await client.post(f"{ROOT}/{item['id']}/update", headers=headers(), json=body)
    second = await client.post(f"{ROOT}/{item['id']}/update", headers=headers(), json=body)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() and count_updates() == 1
    receipt = await client.get(ROOT + "/receipts/" + body["request_id"], headers=headers())
    assert receipt.status_code == 200 and receipt.json() == first.json()
    conflict = await client.post(
        f"{ROOT}/{item['id']}/update", headers=headers(), json={**body, "stage": "candidate_validated"}
    )
    assert conflict.status_code == 409 and count_updates() == 1


async def test_same_stage_new_request_does_not_spam_history_or_trigger_new_queue_events(client):
    item, _ = await submit(client)
    first = (await client.post(f"{ROOT}/{item['id']}/update", headers=headers(), json=payload(item))).json()
    second = await client.post(
        f"{ROOT}/{item['id']}/update",
        headers=headers(),
        json=payload(item, expected_revision=first["revision_number"]),
    )
    assert second.status_code == 200
    assert second.json()["changed"] is False and second.json()["revision_number"] == first["revision_number"]
    assert count_updates() == 1


@pytest.mark.parametrize("status", ["fixed", "deferred", "declined", "needs_info"])
async def test_human_terminal_or_paused_status_is_not_overwritten(client, status):
    from server.database import get_db

    item, _ = await submit(client)
    with get_db() as db:
        db.execute(
            "UPDATE feedback_items SET status=?,revision_number=revision_number+1 WHERE id=?",
            [status, item["id"]],
        )
    result = await client.post(
        f"{ROOT}/{item['id']}/update", headers=headers(), json=payload(item, expected_revision=2)
    )
    assert result.status_code == 409 and count_updates() == 0


async def test_progress_write_obeys_existing_maintenance_admission(client, monkeypatch):
    from server import work_adapters
    from server.work_lifecycle import KINDS, Manager

    item, _ = await submit(client)
    manager = Manager()
    for kind in KINDS:
        manager.declare(kind)
    manager.begin_drain(manager.ticket())
    monkeypatch.setattr(work_adapters, "get_manager", lambda *a, **k: manager)
    try:
        result = await client.post(f"{ROOT}/{item['id']}/update", headers=headers(), json=payload(item))
        assert result.status_code == 503 and result.json()["error"] == "maintenance_hold"
        assert count_updates() == 0
    finally:
        manager._pool.shutdown(wait=True, cancel_futures=False)


async def test_stale_cas_and_progress_regression_do_not_erase_newer_stage(client):
    item, _ = await submit(client)
    first = await client.post(
        f"{ROOT}/{item['id']}/update", headers=headers(), json=payload(item, stage="candidate_validated")
    )
    assert first.status_code == 200
    assert (
        await client.post(
            f"{ROOT}/{item['id']}/update", headers=headers(), json=payload(item, stage="awaiting_review")
        )
    ).status_code == 409
    assert (
        await client.post(
            f"{ROOT}/{item['id']}/update",
            headers=headers(),
            json=payload(item, expected_revision=2, stage="triaging"),
        )
    ).status_code == 409
    assert count_updates() == 1


async def test_suggestions_disabled_accounts_and_historical_reports_are_outside_scope(client):
    from server.database import get_db

    suggestion, _ = await submit(client, category="suggestion")
    assert (
        await client.post(f"{ROOT}/{suggestion['id']}/update", headers=headers(), json=payload(suggestion))
    ).status_code == 404
    item, _ = await submit(client)
    with get_db() as db:
        db.execute("UPDATE feedback_items SET version='v0.1.0' WHERE id=?", [item["id"]])
    assert (
        await client.post(f"{ROOT}/{item['id']}/update", headers=headers(), json=payload(item))
    ).status_code == 409
    with get_db() as db:
        db.execute("UPDATE users SET disabled_at=1 WHERE username='synthetic-owner'")
    assert (
        await client.post(f"{ROOT}/{item['id']}/update", headers=headers(), json=payload(item))
    ).status_code == 404
    assert count_updates() == 0


async def test_account_deletion_cleans_progress_and_receipts(client):
    from server.database import get_db

    item, _ = await submit(client)
    body = payload(item)
    assert (await client.post(f"{ROOT}/{item['id']}/update", headers=headers(), json=body)).status_code == 200
    with get_db() as db:
        db.execute("DELETE FROM users WHERE username='synthetic-owner'")
        assert db.execute("SELECT COUNT(*) FROM feedback_progress_state").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM feedback_progress_receipts").fetchone()[0] == 0
    assert (await client.get(ROOT + "/receipts/" + body["request_id"], headers=headers())).status_code == 404


async def test_manual_in_progress_is_not_downgraded_to_triage(client):
    from server.database import get_db

    item, _ = await submit(client)
    with get_db() as db:
        db.execute(
            "UPDATE feedback_items SET status='in_progress',revision_number=2 WHERE id=?", [item["id"]]
        )
    result = await client.post(
        f"{ROOT}/{item['id']}/update",
        headers=headers(),
        json=payload(item, stage="triaging", expected_revision=2),
    )
    assert result.status_code == 409 and count_updates() == 0
