"""Retired execution is absent; owned historical records remain readable."""

import httpx
import pytest
from vibe_history import historical_plan, insert_historical_plan

from server import vibe, vibe_engine


def test_execution_routes_and_engine_client_are_removed():
    paths = {route.path for route in vibe.router.routes}
    assert "/api/vibe/plan" not in paths
    assert "/api/vibe/plans/{plan_id}/follow-up" not in paths
    for name in (
        "_TASKS",
        "_CANCEL_INFLIGHT",
        "build_vibe_plan",
        "_insert_vibe_plan",
        "_start_task",
        "_ensure_task",
        "_monitor_attempt",
        "_answer_for_attempt",
        "_run_official_research",
        "start_owned_followup",
    ):
        assert not hasattr(vibe, name)
    assert not any(name.startswith("engine_") for name in vars(vibe_engine))


@pytest.mark.parametrize("path", ["/api/vibe/plans", "/api/vibe/plans/saved"])
async def test_history_requires_auth(client, path):
    assert (await client.get(path)).status_code == 401


async def test_history_reads_are_owner_scoped_without_network(client, auth_cookies, test_user, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("Historical Vibe reads must not create an HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", no_network)
    monkeypatch.setattr(httpx, "Client", no_network)
    own = historical_plan("saved research")
    own.update(
        id="saved", report_markdown="# Saved report", followups=[{"answer_markdown": "saved followup"}]
    )
    insert_historical_plan(test_user["username"], own)
    foreign = historical_plan("foreign research")
    foreign.update(id="foreign")
    insert_historical_plan("other", foreign)
    listing = await client.get("/api/vibe/plans", cookies=auth_cookies)
    assert listing.status_code == 200
    assert [plan["id"] for plan in listing.json()["plans"]] == ["saved"]
    detail = await client.get("/api/vibe/plans/saved", cookies=auth_cookies)
    assert detail.status_code == 200
    assert detail.json()["plan"]["report_markdown"] == "# Saved report"
    assert detail.json()["plan"]["followups"] == own["followups"]
    assert (await client.get("/api/vibe/plans/foreign", cookies=auth_cookies)).status_code == 404
