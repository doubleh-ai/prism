"""Research model routing, provider budgets and compatibility with retired settings."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from server import model_routes
from server.database import get_db


@pytest.fixture(autouse=True)
def reset_deployment_routes(monkeypatch):
    monkeypatch.setattr(model_routes, "_deployment_routes", None)


def deployment_setting(**changes):
    return {
        "model_id": model_routes.config.DEFAULT_MODEL,
        "enabled": True,
        "output_tokens_max": 1024,
        "model_calls_max": 1,
        "wall_seconds_max": 60,
        "daily_calls_max": 2,
        **changes,
    }


def test_deployment_routes_do_not_write_admin_settings_or_erase_unknown_usage(monkeypatch):
    policy = {"research": deployment_setting(daily_calls_max=1)}
    with monkeypatch.context() as isolated:
        isolated.setattr(model_routes, "get_db", lambda: pytest.fail("startup must not open a database"))
        model_routes.install_defaults(policy)
    policy["research"]["daily_calls_max"] = 1000
    model_id, _, route = model_routes.resolve("research")
    assert route["version"] == 0 and route["daily_calls_max"] == 1
    model_routes.reserve("research", "synthetic", "unobserved", model_id, expected_route_version=0)
    with pytest.raises(HTTPException) as denied:
        model_routes.reserve("research", "synthetic", "next", model_id, expected_route_version=0)
    assert denied.value.status_code == 429
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM model_routes").fetchone()[0] == 0
        assert tuple(
            db.execute("SELECT status,input_tokens,output_tokens FROM model_request_reservations").fetchone()
        ) == ("unknown", None, None)
    with pytest.raises(ValueError, match="already_installed"):
        model_routes.install_defaults({})


def test_pre_removal_policy_still_loads_only_research():
    model_routes.install_defaults(
        {
            "research": deployment_setting(allow_private_work=False),
            "work_planning": deployment_setting(allow_private_work=True),
        }
    )
    model_id, _, route = model_routes.resolve("research")
    assert model_id == model_routes.config.DEFAULT_MODEL
    assert "allow_private_work" not in route
    assert [route["purpose"] for route in model_routes.snapshot()["routes"]] == ["research"]


def test_explicit_empty_deployment_policy_closes_research():
    purpose = "research"
    model_routes.install_defaults({})
    with pytest.raises(HTTPException) as denied:
        model_routes.resolve(purpose, explicit_model=model_routes.config.DEFAULT_MODEL)
    assert denied.value.status_code == 503


def test_existing_admin_setting_overrides_deployment_default():
    model_routes.init_model_routes_db()
    stored = {
        **deployment_setting(enabled=False),
        "allow_private_work": False,
        "purpose": "research",
        "configured": True,
        "version": 7,
    }
    with get_db() as db:
        db.execute("INSERT INTO model_routes VALUES(?,?,?)", ["research", 7, json.dumps(stored)])
    model_routes.install_defaults({"research": deployment_setting()})
    with pytest.raises(HTTPException) as denied:
        model_routes.resolve("research")
    assert denied.value.status_code == 503
    with get_db() as db:
        assert json.loads(db.execute("SELECT payload FROM model_routes").fetchone()[0]) == stored
    assert "allow_private_work" not in model_routes.snapshot()["routes"][0]


@pytest.mark.parametrize(
    "change",
    [
        {"model_id": "not-registered"},
        {"enabled": 1},
        {"daily_calls_max": True},
        {"model_calls_max": 0},
        {"wall_seconds_max": 181},
        {"output_tokens_max": 2049},
        {"allow_private_work": True},
        {"api_key": "synthetic-forbidden"},
    ],
)
def test_invalid_deployment_policy_does_not_attach_partial_settings(change):
    with pytest.raises(ValueError, match="defaults_invalid"):
        model_routes.install_defaults({"research": deployment_setting(**change)})
    assert not model_routes.defaults_installed()


def test_purpose_quota_unknown_usage_not_reset_by_configuration():
    purpose = "research"
    model_routes.init_model_routes_db()
    route = {
        "purpose": purpose,
        "version": 1,
        "configured": True,
        "enabled": True,
        "daily_calls_max": 1,
        "model_id": "synthetic-model",
    }
    with get_db() as db:
        db.execute("INSERT INTO model_routes VALUES(?,?,?)", [purpose, 1, json.dumps(route)])
    model_routes.reserve(
        purpose, "synthetic-owner", "request-one", "synthetic-model", expected_route_version=1
    )
    with pytest.raises(HTTPException) as repeated:
        model_routes.reserve(
            purpose, "synthetic-owner", "request-one", "synthetic-model", expected_route_version=1
        )
    assert repeated.value.status_code == 409
    route["version"] = 2
    with get_db() as db:
        db.execute("UPDATE model_routes SET version=2,payload=?", [json.dumps(route)])
    with pytest.raises(HTTPException) as exhausted:
        model_routes.reserve(
            purpose, "synthetic-owner", "request-two", "synthetic-model", expected_route_version=2
        )
    assert exhausted.value.status_code == 429
    with get_db() as db:
        row = db.execute(
            "SELECT status,input_tokens,output_tokens FROM model_request_reservations"
        ).fetchone()
        assert tuple(row) == ("unknown", None, None)
    assert "api_key" not in json.dumps(model_routes.snapshot())


@pytest.fixture
def historical_unused_model_settings():
    """Old enabled settings and both known/unknown usage remain private history."""
    model_routes.init_model_routes_db()
    day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    with get_db() as db:
        for purpose in ("feedback_triage", "suggestion_review", "validation", "work_planning"):
            route = {
                "purpose": purpose,
                "configured": True,
                "version": 7,
                "enabled": True,
                "model_id": model_routes.config.DEFAULT_MODEL,
                "daily_calls_max": 50,
            }
            db.execute("INSERT INTO model_routes VALUES(?,?,?)", [purpose, 7, json.dumps(route)])
            for status in ("unknown", "complete"):
                db.execute(
                    "INSERT INTO model_request_reservations VALUES(?,?,?,?,?,?,?,?,?,?)",
                    [
                        f"historical-{purpose}-{status}",
                        purpose,
                        "synthetic-owner",
                        7,
                        route["model_id"],
                        day,
                        status,
                        12 if status == "complete" else None,
                        8 if status == "complete" else None,
                        100,
                    ],
                )

    def snapshot():
        with get_db() as db:
            return (
                [tuple(row) for row in db.execute("SELECT * FROM model_routes ORDER BY purpose")],
                [
                    tuple(row)
                    for row in db.execute("SELECT * FROM model_request_reservations ORDER BY request_id")
                ],
            )

    return snapshot


async def test_admin_lists_only_live_application_purposes_without_erasing_history(
    client, auth_cookies, ops_headers, historical_unused_model_settings
):
    before = historical_unused_model_settings()
    assert (await client.get("/api/admin/model-routes", cookies=auth_cookies)).status_code == 403
    response = await client.get("/api/admin/model-routes", headers=ops_headers)
    assert response.status_code == 200, response.text
    value = response.json()
    assert [route["purpose"] for route in value["routes"]] == ["research"]
    assert all(route["configured"] is False for route in value["routes"])
    assert "api_key" not in json.dumps(value)
    assert historical_unused_model_settings() == before


@pytest.mark.parametrize("purpose", ["feedback_triage", "suggestion_review", "validation", "work_planning"])
async def test_nonconsumer_purposes_cannot_be_saved_resolved_or_reserved(
    purpose, client, ops_headers, historical_unused_model_settings
):
    from types import SimpleNamespace

    before = historical_unused_model_settings()
    response = await client.put(
        "/api/admin/model-routes/" + purpose,
        headers=ops_headers,
        json={"expected_version": 7, "model_id": model_routes.config.DEFAULT_MODEL},
    )
    assert response.status_code == 422, response.text
    with pytest.raises(HTTPException) as resolved:
        model_routes.resolve(purpose)
    assert resolved.value.status_code == 422
    with pytest.raises(HTTPException) as reserved:
        model_routes.reserve(
            purpose,
            "synthetic-owner",
            "new-unsupported-request",
            model_routes.config.DEFAULT_MODEL,
            expected_route_version=7,
        )
    assert reserved.value.status_code == 422
    # The direct handler also refuses before parsing a body or touching history.
    with pytest.raises(HTTPException) as direct:
        await model_routes.save_route(
            purpose,
            type(
                "SyntheticRequest",
                (),
                {
                    "method": "PUT",
                    "client": SimpleNamespace(host="127.0.0.1"),
                    "cookies": {},
                    "headers": ops_headers,
                },
            )(),
        )
    assert direct.value.status_code == 422 and direct.value.detail == "模型用途无效"
    assert historical_unused_model_settings() == before


async def test_live_application_routes_still_save_and_drive_resolution(client, ops_headers):
    purpose = "research"
    response = await client.put(
        "/api/admin/model-routes/" + purpose,
        headers=ops_headers,
        json={
            "expected_version": 0,
            "model_id": model_routes.config.DEFAULT_MODEL,
            "daily_calls_max": 1,
        },
    )
    assert response.status_code == 200, response.text
    model_id, _model, route = model_routes.resolve(purpose)
    assert model_id == response.json()["route"]["model_id"] and route["version"] == 1
    model_routes.reserve(purpose, "synthetic-owner", "one-live-request", model_id, expected_route_version=1)
    with pytest.raises(HTTPException) as exhausted:
        model_routes.reserve(
            purpose, "synthetic-owner", "two-live-request", model_id, expected_route_version=1
        )
    assert exhausted.value.status_code == 429
