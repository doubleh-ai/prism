"""Shared identity and explicit product entry points, without cross-product data."""

import pytest


async def test_portal_and_prism_are_distinct_authenticated_pages(client, auth_cookies):
    for path in ("/", "/prism", "/projects", "/notifications"):
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


@pytest.mark.parametrize("path,title", [("/projects", "项目"), ("/notifications", "提醒")])
async def test_work_pages_share_account_without_research_payload(client, auth_cookies, path, title):
    response = await client.get(path, cookies=auth_cookies)
    assert response.status_code == 200 and title in response.text
    assert 'id="msgList"' not in response.text
    assert response.headers["cache-control"] == "no-store"
