"""Read-only operational facts, cache freshness and owner visibility boundaries."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from server import auth
from server import operations as ops


@pytest.fixture(autouse=True)
def isolated_observer(monkeypatch):
    monkeypatch.setattr(ops, "_observation", ops.Observation())
    monkeypatch.setattr(ops, "release_payload", lambda: {"version": "v0.19.7", "deployed_revision": ""})


async def test_observer_is_admin_only_before_network(client, auth_cookies, monkeypatch):
    reader = AsyncMock(return_value=([], []))
    monkeypatch.setattr(ops, "_read_github", reader)
    assert (await client.get("/api/admin/operations")).status_code == 401
    assert (await client.get("/api/admin/operations", cookies=auth_cookies)).status_code == 403
    reader.assert_not_awaited()


async def test_success_is_github_observation_not_production_evidence(client, auth_cookies, monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_USERS", {"testuser"})
    monkeypatch.setattr(
        ops,
        "_read_github",
        AsyncMock(return_value=([{"kind": "ci", "status": "completed", "conclusion": "success"}], [])),
    )
    response = await client.get("/api/admin/operations", cookies=auth_cookies)
    assert response.status_code == 200
    value = response.json()
    assert value["github"]["status"] == "ok"
    assert value["github"]["checked_at"] > 0
    assert value["runtime"]["deployed_revision"] == "unknown"
    assert value["runtime"]["production_verified"] is False
    assert not any(value["delivery"][key] for key in ["production_connected", "can_deploy", "can_rollback"])
    assert value["delivery"]["mode"] == "candidate_only"
    assert (await client.post("/api/admin/operations/deploy", cookies=auth_cookies)).status_code == 404
    assert (await client.post("/api/admin/operations/cancel", cookies=auth_cookies)).status_code == 404


async def test_concurrent_refresh_shares_cache_and_does_not_retimestamp(monkeypatch):
    calls = 0

    async def read():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return [], []

    monkeypatch.setattr(ops, "_read_github", read)
    results = await asyncio.gather(*(ops.operations_payload() for _ in range(12)))
    assert calls == 1
    checked = results[0]["github"]["checked_at"]
    assert all(value["github"]["checked_at"] == checked for value in results)
    await ops.operations_payload()
    assert calls == 1


async def test_failed_refresh_preserves_old_snapshot_but_marks_stale(monkeypatch):
    marker = {"id": 123, "kind": "ci", "status": "completed", "conclusion": "success"}
    monkeypatch.setattr(ops, "_read_github", AsyncMock(return_value=([marker], [])))
    first = await ops.operations_payload()
    ops._observation.next_poll = 0
    monkeypatch.setattr(ops, "_read_github", AsyncMock(side_effect=RuntimeError("secret-test-value")))
    failed = await ops.operations_payload()
    assert failed["github"]["status"] == "unavailable"
    assert failed["github"]["stale"] is True
    assert failed["github"]["checked_at"] == first["github"]["checked_at"]
    assert failed["github"]["runs"] == [marker]
    assert "secret-test-value" not in str(failed)


async def test_first_failure_is_unknown_not_empty_success(monkeypatch):
    monkeypatch.setattr(ops, "_read_github", AsyncMock(side_effect=TimeoutError()))
    value = await ops.operations_payload()
    assert value["github"]["status"] == "unavailable"
    assert value["github"]["checked_at"] is None
    assert value["github"]["stale"] is False
    assert value["github"]["error"]
    assert value["delivery"]["can_deploy"] is False


def test_upstream_links_and_names_cannot_impersonate_deployment():
    payload = {
        "workflow_runs": [
            {
                "id": 1,
                "name": "Deployed successfully",
                "path": ".github/workflows/ci.yml",
                "status": "completed",
                "conclusion": "success",
                "head_sha": "a" * 40,
                "html_url": "https://evil.invalid/",
            },
            {
                "id": 2,
                "name": "Release candidate",
                "path": ".github/workflows/release-candidate.yml",
                "status": "in_progress",
                "head_sha": "b" * 40,
            },
            {
                "id": 3,
                "name": "Production deployment",
                "path": ".github/workflows/unknown.yml",
                "status": "completed",
                "conclusion": "success",
            },
            {"id": True, "path": ".github/workflows/ci.yml"},
        ]
    }
    result = ops._normalise_runs(payload)
    assert [r["kind"] for r in result] == ["ci", "candidate"]
    assert result[0]["url"] == "https://github.com/doubleh-ai/prism/actions/runs/1"
    pulls = ops._normalise_pulls(
        [
            {
                "number": 7,
                "title": "PR",
                "html_url": "javascript:void(0)",
                "head": {"sha": "invalid"},
                "state": "open",
            }
        ]
    )
    assert pulls[0]["url"] == "https://github.com/doubleh-ai/prism/pull/7"
    assert pulls[0]["head_sha"] == "unknown"
    with pytest.raises(ValueError):
        ops._normalise_runs({"message": "rate limit"})
    with pytest.raises(ValueError):
        ops._normalise_pulls({"message": "rate limit"})


async def test_upstream_requests_are_fixed_gets_without_proxy_or_redirect(monkeypatch):
    captured = []

    async def respond(request):
        captured.append(request)
        return httpx.Response(200, json={"workflow_runs": []} if request.url.path.endswith("/runs") else [])

    real_client = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        assert "Authorization" not in kwargs["headers"]
        return real_client(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(ops.httpx, "AsyncClient", client)
    assert await ops._read_github() == ([], [])
    assert len(captured) == 2
    assert all(r.method == "GET" and r.url.host == "api.github.com" for r in captured)
    assert all(r.url.path.startswith("/repos/doubleh-ai/prism/") for r in captured)


async def test_partial_or_redirected_response_never_becomes_success(monkeypatch):
    real_client = httpx.AsyncClient

    async def respond(request):
        if request.url.path.endswith("/pulls"):
            return httpx.Response(302, headers={"Location": "https://evil.invalid/"})
        return httpx.Response(200, json={"workflow_runs": []})

    monkeypatch.setattr(
        ops.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(respond)),
    )
    value = await ops.operations_payload()
    assert value["github"]["status"] == "unavailable"
    assert value["github"]["checked_at"] is None


@pytest.mark.parametrize("revision", ["short", "../secret", "a" * 39, "b" * 41, "token=synthetic"])
async def test_invalid_runtime_revision_not_exposed(revision, monkeypatch):
    monkeypatch.setattr(ops, "release_payload", lambda: {"version": "v0.19.7", "deployed_revision": revision})
    monkeypatch.setattr(ops, "_read_github", AsyncMock(return_value=([], [])))
    value = await ops.operations_payload()
    assert value["runtime"]["deployed_revision"] == "unknown"
