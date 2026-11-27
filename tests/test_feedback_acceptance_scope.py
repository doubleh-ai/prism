"""Real app routes select only a named synthetic record; no provider is used."""

from datetime import datetime

import pytest

MONITOR = "synthetic-acceptance-monitor-" + "m" * 40
REVIEW = "synthetic-acceptance-review-" + "r" * 40
REVISION = "a" * 40
DAY = "2026-11-08"


@pytest.fixture(autouse=True)
def scope_setup(temp_data_dir, monkeypatch):
    from server import feedback, feedback_review

    feedback.init_feedback_db()
    monkeypatch.setenv("PRISM_FEEDBACK_MONITOR_TOKEN", MONITOR)
    monkeypatch.setenv("PRISM_FEEDBACK_REVIEW_TOKEN", REVIEW)
    monkeypatch.setattr(feedback_review, "_read_deployed_revision", lambda: REVISION)
    monkeypatch.setattr(
        feedback_review,
        "_day",
        lambda: datetime(2026, 11, 8, 10, 0, tzinfo=feedback._SHANGHAI),
    )


def headers(role):
    if role == "monitor":
        return {"X-Prism-Monitor-Token": MONITOR}
    return {"X-Prism-Review-Token": REVIEW, "X-Prism-Expected-Revision": REVISION}


def route(role):
    return f"/api/internal/feedback-{role}/queue"


def query(role, value):
    return [("acceptance_id", value)] + ([("batch_date", DAY)] if role == "review" else [])


async def submit(client, owner, category="suggestion", title="目标合成反馈"):
    from server.database import create_session, get_db

    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO users(username,password_hash) VALUES(?,?)",
            [owner, "synthetic-only-not-a-login"],
        )
    result = await client.post(
        "/api/feedback",
        cookies={"token": create_session(owner)},
        json={
            "category": category,
            "feature": "chat",
            "title": title,
            "description": title + "，仅用于隔离验收。",
            "page": "/prism",
        },
    )
    assert result.status_code == 200, result.text
    item = result.json()["feedback"]
    with get_db() as db:
        db.execute("UPDATE feedback_items SET review_batch_date=? WHERE id=?", [DAY, item["id"]])
    return item["id"]


@pytest.mark.parametrize("role", ["monitor", "review"])
async def test_selected_record_excludes_other_users_and_prior_test_records(client, role):
    foreign = await submit(client, "synthetic-other", title="其他用户资料")
    prior = await submit(client, "prism-acceptance", title="旧批次合成资料")
    selected = await submit(client, "prism-acceptance")
    params = query(role, str(selected))
    if role == "monitor":
        params.append(("category", "suggestion"))
    result = await client.get(route(role), params=params, headers=headers(role))
    assert result.status_code == 200
    assert [item["id"] for item in result.json()["items"]] == [selected]
    assert result.json()["next_after_id"] is None
    assert "其他用户资料" not in result.text and "旧批次合成资料" not in result.text
    if role == "monitor":
        assert "目标合成反馈" not in result.text and result.json()["user_text_included"] is False

    # Knowing another owner's ID does not broaden the dedicated-account scope.
    params = query(role, str(foreign))
    if role == "monitor":
        params.append(("category", "suggestion"))
    empty = await client.get(route(role), params=params, headers=headers(role))
    assert empty.status_code == 200 and empty.json()["items"] == []

    normal = [("batch_date", DAY)] if role == "review" else [("category", "suggestion")]
    unfiltered = await client.get(route(role), params=normal, headers=headers(role))
    assert {item["id"] for item in unfiltered.json()["items"]} == {foreign, prior, selected}


@pytest.mark.parametrize("role", ["monitor", "review"])
@pytest.mark.parametrize("change", ["disabled", "deleted"])
async def test_missing_selected_record_never_falls_through_to_another_user(client, role, change):
    from server.database import get_db

    selected = await submit(client, "prism-acceptance")
    await submit(client, "synthetic-next", title="后续用户资料")
    with get_db() as db:
        if change == "disabled":
            db.execute("UPDATE users SET disabled_at=1 WHERE username='prism-acceptance'")
        else:
            db.execute("DELETE FROM feedback_items WHERE id=?", [selected])
    params = query(role, str(selected))
    if role == "monitor":
        params.append(("category", "suggestion"))
    result = await client.get(route(role), params=params, headers=headers(role))
    assert result.status_code == 200 and result.json()["items"] == []
    assert "后续用户资料" not in result.text


@pytest.mark.parametrize("role", ["monitor", "review"])
@pytest.mark.parametrize("value", ["", "0", "-1", "+1", "01", "1.0", "true", "9007199254740992"])
async def test_noncanonical_or_unbounded_selection_is_rejected(client, role, value):
    result = await client.get(route(role), params=query(role, value), headers=headers(role))
    assert result.status_code == 422 and result.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("role", ["monitor", "review"])
async def test_selection_never_grants_identity_or_an_arbitrary_account_filter(client, role):
    params = query(role, "1")
    assert (await client.get(route(role), params=params)).status_code == 401
    wrong = headers("review" if role == "monitor" else "monitor")
    assert (await client.get(route(role), params=params, headers=wrong)).status_code == 401
    for extra in (("acceptance_id", "2"), ("acceptance_account", "synthetic-other")):
        result = await client.get(route(role), params=params + [extra], headers=headers(role))
        assert result.status_code == 422


async def test_review_selection_preserves_real_schedule_and_runtime_binding(client, monkeypatch):
    from server import feedback, feedback_review

    selected = await submit(client, "prism-acceptance")
    params = query("review", str(selected))
    stale = {**headers("review"), "X-Prism-Expected-Revision": "b" * 40}
    assert (await client.get(route("review"), params=params, headers=stale)).status_code == 409
    monkeypatch.setattr(
        feedback_review,
        "_day",
        lambda: datetime(2026, 11, 8, 9, 29, tzinfo=feedback._SHANGHAI),
    )
    result = await client.get(route("review"), params=params, headers=headers("review"))
    assert result.status_code == 200
    assert result.json()["due"] is False and result.json()["items"] == []
