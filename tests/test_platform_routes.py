"""Shared identity and explicit product entry points, without cross-product data."""

import pytest


async def test_portal_and_prism_are_distinct_authenticated_pages(client, auth_cookies):
    for path in ("/", "/prism", "/projects", "/notifications", "/knowledge", "/futures", "/preferences"):
        response = await client.get(path)
        assert response.status_code == 307 and response.headers["location"] == "/login"
    portal = await client.get("/", cookies=auth_cookies)
    prism = await client.get("/prism", cookies=auth_cookies)
    assert portal.status_code == prism.status_code == 200
    assert "<title>DoubleH</title>" in portal.text
    assert 'data-product="prism"' in portal.text and 'data-product="work"' in portal.text
    assert 'id="input"' in prism.text and 'id="input"' not in portal.text
    assert portal.headers["cache-control"] == prism.headers["cache-control"] == "no-store"


async def test_platform_availability_and_legacy_links(client, auth_cookies, monkeypatch):
    assert (await client.get("/api/platform")).status_code == 401
    monkeypatch.setenv("PRISM_WORK_ENABLED", "0")
    response = await client.get("/api/platform", cookies=auth_cookies)
    assert response.json()["products"] == [
        {"id": "prism", "name": "Prism", "href": "/prism", "available": True},
        {"id": "work", "name": "Work", "href": "/work", "available": False},
    ]
    for path in ("/projects", "/notifications"):
        assert (await client.get(path, cookies=auth_cookies)).status_code == 404
    old = await client.get("/?futures=RB&next=https://invalid.example", cookies=auth_cookies)
    assert old.status_code == 307 and old.headers["location"] == "/prism?futures=RB"
    assert (await client.get("/?next=https://invalid.example", cookies=auth_cookies)).status_code == 200


@pytest.mark.parametrize("path,title", [("/work", "任务"), ("/projects", "项目"), ("/calendar", "日历")])
async def test_work_pages_share_account_without_research_payload(client, auth_cookies, path, title):
    response = await client.get(path, cookies=auth_cookies)
    assert response.status_code == 200 and title in response.text
    assert 'id="msgList"' not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.text.count('id="workNotifications"') == 1
    assert response.text.count("data-work-notifications") == 1
    assert 'href="/notifications"' not in response.text
    assert "__WORK_NOTIFICATIONS__" not in response.text


async def test_capability_bookmarks_return_to_the_owning_workspace(client, auth_cookies):
    from urllib.parse import parse_qs, urlsplit

    claim = '"><img src=x onerror=alert(1)>&/其他'
    response = await client.get(
        "/knowledge", params={"id": claim, "next": "https://invalid.example"}, cookies=auth_cookies
    )
    assert response.status_code == 307
    location = urlsplit(response.headers["location"])
    assert location.path == "/prism" and not location.netloc
    assert parse_qs(location.query) == {"mode": ["reports"], "library": ["knowledge"], "claim": [claim]}
    prism = await client.get(response.headers["location"], cookies=auth_cookies)
    assert prism.status_code == 200 and 'id="appShell"' in prism.text
    assert prism.text.count('id="researchKnowledge"') == 1
    assert 'href="/knowledge"' not in prism.text and "__RESEARCH_KNOWLEDGE__" not in prism.text
    assert claim not in prism.text
    reminders = await client.get("/notifications?next=https://invalid.example", cookies=auth_cookies)
    assert reminders.status_code == 307
    assert reminders.headers["location"] == "/work?panel=notifications"
    assert response.headers["cache-control"] == reminders.headers["cache-control"] == "no-store"


async def test_preferences_bookmark_keeps_the_prism_shell(client, auth_cookies):
    response = await client.get("/preferences?next=https://invalid.example", cookies=auth_cookies)
    assert response.status_code == 307
    assert response.headers["location"] == "/prism?mode=preferences"
    assert response.headers["cache-control"] == "no-store"
    page = await client.get(response.headers["location"], cookies=auth_cookies)
    assert page.status_code == 200 and 'id="appShell"' in page.text
    assert page.text.count('id="preferencesWorkspace"') == 1
    assert page.text.count('id="profileForm"') == page.text.count('id="portfolioForm"') == 1
    assert 'data-mode="preferences"' in page.text
    assert 'href="/preferences"' not in page.text
    assert "__RESEARCH_PREFERENCES__" not in page.text


async def test_futures_bookmark_selects_the_same_prism_workspace_without_starting_research(
    client, auth_cookies
):
    from server.agent.runtime import init_runtime_db

    init_runtime_db()
    response = await client.get("/futures?symbol=CU&next=https://invalid.example", cookies=auth_cookies)
    assert response.status_code == 307
    assert response.headers["location"] == "/prism?mode=futures&symbol=CU"
    page = await client.get(response.headers["location"], cookies=auth_cookies)
    assert page.status_code == 200
    assert 'id="appShell"' in page.text and page.text.count('id="futuresWorkspace"') == 1
    assert 'id="futuresStartResearch"' in page.text and "__FUTURES_WORKSPACE__" not in page.text
    assert 'href="/futures"' not in page.text
    from server.database import get_db

    with get_db() as db:
        assert db.execute("SELECT count(*) FROM native_agent_runs").fetchone()[0] == 0
