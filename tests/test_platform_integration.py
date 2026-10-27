"""Work main-app routing, privacy headers and shipped update behavior."""

import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient


async def test_work_page_flag_auth_and_private_responses(client, auth_cookies, monkeypatch):
    from server.main import app
    from server.productivity import init_productivity_db

    monkeypatch.delenv("PRISM_WORK_ENABLED", raising=False)
    disabled = await client.get("/work", cookies=auth_cookies)
    assert disabled.status_code == 404
    assert disabled.headers["cache-control"] == "no-store"
    monkeypatch.setenv("PRISM_WORK_ENABLED", "1")
    init_productivity_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as raw:
        redirect = await raw.get("/work")
        assert redirect.status_code == 307
        assert redirect.headers["location"] == "/login?next=/work"
        assert redirect.headers["cache-control"] == "no-store"
        rejected = await raw.post("/api/work/spaces", json={}, cookies=auth_cookies)
        assert rejected.status_code == 403
        assert rejected.headers["cache-control"] == "no-store"
        unauthorized = await raw.get("/api/work/overview")
        assert unauthorized.status_code == 401
        assert unauthorized.headers["cache-control"] == "no-store"
    page = await client.get("/work", cookies=auth_cookies)
    assert page.status_code == 200
    assert "Work" in page.text and "/static/js/app.work.js?v=work-2" in page.text
    assert page.headers["cache-control"] == "no-store"
    space = await client.post(
        "/api/work/spaces",
        json={"request_id": str(uuid4()), "name": "隔离测试", "timezone": "Asia/Shanghai"},
        cookies=auth_cookies,
    )
    assert space.status_code == 200
    sid = space.json()["workspace"]["id"]
    task = await client.post(
        f"/api/work/spaces/{sid}/tasks",
        json={"request_id": str(uuid4()), "title": "合成任务"},
        cookies=auth_cookies,
    )
    assert task.status_code == 200
    tid = task.json()["task"]["id"]
    for path in (
        "/api/work/overview",
        f"/api/work/spaces/{sid}/tasks",
        f"/api/work/spaces/{sid}/tasks/{tid}",
        f"/api/work/spaces/{sid}/tasks/{uuid4()}",
    ):
        result = await client.get(path, cookies=auth_cookies)
        assert result.status_code in (200, 404)
        assert result.headers["cache-control"] == "no-store"
    invalid = await client.patch(
        f"/api/work/spaces/{sid}/tasks/{tid}",
        json={"expected_version": 1, "title": "", "owner": "private-input-never-echo"},
        cookies=auth_cookies,
    )
    assert invalid.status_code == 422
    assert invalid.headers["cache-control"] == "no-store"
    assert "private-input-never-echo" not in invalid.text


def test_shipped_service_worker_and_client_keep_edits():
    node = shutil.which("node")
    if node is None:
        pytest.fail("Node is required to verify shipped update behavior")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, str(root / "tests/service_worker_runtime.cjs")],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "updates preserve drafts and private responses" in result.stdout
