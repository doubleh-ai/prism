"""Admin health surfaces the work-manager snapshot over the ops gateway and never fails on a bad probe."""

import pytest

from server import main
from server.work_lifecycle import Manager


@pytest.fixture
def manager():
    value = Manager()
    yield value
    value._pool.shutdown(wait=True, cancel_futures=False)


async def test_health_reports_work_state_and_drain_counts(
    client, auth_cookies, ops_headers, manager, monkeypatch
):
    monkeypatch.setattr(main.app.state, "work_manager", manager, raising=False)
    manager.begin_drain(manager.ticket())

    assert (await client.get("/api/admin/health", cookies=auth_cookies)).status_code == 403
    response = await client.get("/api/admin/health", headers=ops_headers)
    assert response.status_code == 200
    work = response.json()["work"]
    assert work["schema"] == 1
    assert work["state"] == "draining"
    assert work["drain_counts"] == {"begin": 1, "seal": 0, "resume": 0}


async def test_health_omits_work_when_no_manager_is_bound(client, ops_headers, monkeypatch):
    monkeypatch.delattr(main.app.state, "work_manager", raising=False)

    response = await client.get("/api/admin/health", headers=ops_headers)
    assert response.status_code == 200
    assert "work" not in response.json()


async def test_health_stays_available_when_work_observation_raises(client, ops_headers, manager, monkeypatch):
    def broken_observe():
        raise RuntimeError("synthetic work observation failure")

    monkeypatch.setattr(main.app.state, "work_manager", manager, raising=False)
    monkeypatch.setattr(manager, "observe", broken_observe)

    response = await client.get("/api/admin/health", headers=ops_headers)
    assert response.status_code == 200
    assert response.json()["work"] == {"status": "unavailable"}
