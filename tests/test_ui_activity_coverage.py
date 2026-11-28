"""Ordinary page opens reach admin usage totals without storing page contents."""

import json

import pytest


@pytest.mark.parametrize("feature", ["futures", "preferences"])
async def test_page_open_is_recorded_and_visible_in_admin_overview(
    client, auth_cookies, ops_headers, monkeypatch, feature
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

    assert (
        await client.get("/api/admin/analytics/overview?range=7d", cookies=auth_cookies)
    ).status_code == 403
    overview = await client.get("/api/admin/analytics/overview?range=7d", headers=ops_headers)
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


async def test_modern_route_classification_and_anonymized_ops_aggregates(client, auth_cookies, ops_headers):
    from starlette.requests import Request

    from server.activity import classify_activity

    def _req(method: str, path: str, query: str = "") -> Request:
        return Request(
            {
                "type": "http",
                "method": method,
                "path": path,
                "query_string": query.encode("utf-8"),
                "headers": [],
            }
        )

    assert classify_activity(_req("POST", "/api/agent/chat/stream")).feature == "copilot"
    assert classify_activity(_req("GET", "/api/radar/feed")).feature == "radar"
    assert classify_activity(_req("POST", "/api/intel")).feature == "intel"
    assert classify_activity(_req("PUT", "/api/private-agent/state")).feature == "private_agent"
    assert classify_activity(_req("GET", "/api/futures/overview", "symbol=RB")).feature == "futures"
    assert classify_activity(_req("PUT", "/api/user/model-preference")).feature == "model"

    csrf = auth_cookies.get("csrf_token", "")
    save_pos = await client.put(
        "/api/private-agent/state",
        cookies=auth_cookies,
        headers={"X-CSRF-Token": csrf},
        json={
            "position_pct": 65,
            "max_single_loss_pct": 6.0,
            "cash_note": "PRIVATE_CASH_SECRET_NOTE",
            "holdings": [
                {"code": "600519", "name": "贵州茅台", "cost_price": 1500, "weight_note": "SECRET_HEAVY"}
            ],
        },
    )
    assert save_pos.status_code == 200

    users_resp = await client.get("/api/admin/users", headers=ops_headers)
    assert users_resp.status_code == 200
    stats_resp = await client.get("/api/admin/stats", headers=ops_headers)
    assert stats_resp.status_code == 200
    payload = stats_resp.json()
    raw_dump = json.dumps({"users": users_resp.json(), "stats": payload}, ensure_ascii=False)
    assert "PRIVATE_CASH_SECRET_NOTE" not in raw_dump
    assert "SECRET_HEAVY" not in raw_dump
    assert "600519" not in raw_dump
    pa_ops = payload["private_agent_ops"]
    assert pa_ops["position_configured_users"] >= 1
    assert set(pa_ops.keys()) == {
        "circle_intel_active",
        "circle_intel_total",
        "circle_intel_reconciled",
        "watchman_active_rules",
        "watchman_triggered_total",
        "persona_configured_users",
        "persona_traits_total",
        "position_configured_users",
    }
