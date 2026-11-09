"""Ordinary page opens reach admin usage totals without storing page contents."""

import json

import pytest


@pytest.mark.parametrize("feature", ["futures", "preferences"])
async def test_page_open_is_recorded_and_visible_in_admin_overview(
    client, auth_cookies, monkeypatch, feature
):
    from server import auth
    from server.database import get_db

    assert "testuser" not in auth.ADMIN_USERS
    response = await client.post(
        "/api/activity",
        cookies=auth_cookies,
        json={
            "feature": feature,
            "action": "open",
            "target_type": "draft",
            "target_id": "SYNTHETIC_PRIVATE_DRAFT",
            "target_label": "SYNTHETIC_PRIVATE_CONTENT",
        },
    )
    assert response.status_code == 200
    with get_db() as db:
        row = db.execute(
            "SELECT feature,action,target_type,target_id,target_label,meta_json FROM activity_events "
            "WHERE feature=? AND action='open' ORDER BY id DESC LIMIT 1",
            [feature],
        ).fetchone()
    assert row is not None and row["feature"] == feature
    assert row["target_type"] == row["target_id"] == row["target_label"] == ""
    assert json.loads(row["meta_json"]) == {"source": "ui"}

    monkeypatch.setattr(auth, "ADMIN_USERS", auth.ADMIN_USERS | {"testuser"})
    overview = await client.get("/api/admin/analytics/overview?range=7d", cookies=auth_cookies)
    assert overview.status_code == 200
    assert any(item["feature"] == feature and item["events"] >= 1 for item in overview.json()["features"])


@pytest.mark.parametrize("feature", ["futures", "preferences"])
@pytest.mark.parametrize("action", ["risk_passed", "research_complete"])
async def test_page_tracking_does_not_accept_fabricated_business_success(
    client, auth_cookies, feature, action
):
    response = await client.post(
        "/api/activity", cookies=auth_cookies, json={"feature": feature, "action": action}
    )
    assert response.status_code == 422
