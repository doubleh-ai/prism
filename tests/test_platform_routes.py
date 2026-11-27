"""Shared identity, direct Prism V3 entry, and safe legacy bookmark redirects."""

from urllib.parse import parse_qs, urlsplit


async def test_page_version_and_latest_published_release(client, auth_cookies, monkeypatch, tmp_path):
    from server import release

    notes = tmp_path / "CHANGELOG.md"
    notes.write_text(
        "## [Unreleased]\n### Added\n- Pending candidate\n\n## [v0.23.0]\n### Fixed\n- Published change\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(release, "CHANGELOG_PATH", notes)
    login = await client.get("/login")
    assert "<title>DoubleH · 登录</title>" in login.text
    prism = await client.get("/prism", cookies=auth_cookies)
    assert release.APP_VERSION + "</span>" in prism.text
    observed = await client.get("/api/releases", cookies=auth_cookies)
    assert observed.json()["version"] == release.APP_VERSION
    assert observed.json()["latest"]["version"] == "v0.23.0"


async def test_root_redirects_authenticated_users_straight_to_prism(client, auth_cookies):
    for path in ("/", "/prism", "/knowledge", "/futures", "/preferences"):
        response = await client.get(path)
        assert response.status_code == 307 and response.headers["location"] == "/login"
    root = await client.get("/", cookies=auth_cookies)
    assert root.status_code == 307
    assert root.headers["location"] == "/prism"
    assert root.headers["cache-control"] == "no-store"
    prism = await client.get("/prism", cookies=auth_cookies)
    assert prism.status_code == 200
    assert prism.headers["cache-control"] == "no-store"
    assert 'id="appShell"' in prism.text
    assert 'id="copilotShell"' in prism.text
    assert 'id="input"' in prism.text


async def test_retired_portal_api_and_legacy_query_forwarding(client, auth_cookies):
    assert (await client.get("/api/platform")).status_code == 404
    assert (await client.get("/api/platform", cookies=auth_cookies)).status_code == 404
    old = await client.get("/?futures=RB&next=https://invalid.example", cookies=auth_cookies)
    assert old.status_code == 307 and old.headers["location"] == "/futures?symbol=RB"
    kept = await client.get("/?mode=reports&next=https://invalid.example", cookies=auth_cookies)
    assert kept.status_code == 307 and kept.headers["location"] == "/prism?mode=reports"


async def test_removed_work_endpoints_are_unavailable(client, auth_cookies):
    for path in ("/work", "/api/work/overview"):
        assert (await client.get(path, cookies=auth_cookies)).status_code == 404


async def test_knowledge_bookmark_redirects_to_decision_journal_without_echoing_untrusted_query(
    client, auth_cookies
):
    claim = '"><img src=x onerror=alert(1)>&/其他'
    response = await client.get(
        "/knowledge", params={"id": claim, "next": "https://invalid.example"}, cookies=auth_cookies
    )
    assert response.status_code == 307
    location = urlsplit(response.headers["location"])
    assert location.path == "/prism" and not location.netloc
    assert parse_qs(location.query) == {"mode": ["reports"], "tab": ["journal"]}
    prism = await client.get(response.headers["location"], cookies=auth_cookies)
    assert prism.status_code == 200 and 'id="appShell"' in prism.text
    assert prism.text.count('id="journalVaultPanel"') == 1
    assert 'id="researchKnowledge"' not in prism.text
    assert 'href="/knowledge"' not in prism.text and "__RESEARCH_KNOWLEDGE__" not in prism.text
    assert claim not in prism.text
    assert 'id="feedbackDialog"' not in prism.text
    assert "__FEEDBACK_DIALOG__" not in prism.text
    assert response.headers["cache-control"] == "no-store"


async def test_preferences_bookmark_opens_persona_dialog_inside_prism(client, auth_cookies):
    response = await client.get("/preferences?next=https://invalid.example", cookies=auth_cookies)
    assert response.status_code == 307
    assert response.headers["location"] == "/prism?copilot=persona"
    assert response.headers["cache-control"] == "no-store"
    page = await client.get(response.headers["location"], cookies=auth_cookies)
    assert page.status_code == 200 and 'id="appShell"' in page.text
    assert page.text.count('id="personaDialog"') == 1
    assert page.text.count('id="preferencesWorkspace"') == 1
    assert page.text.count('id="profileForm"') == page.text.count('id="portfolioForm"') == 1
    assert 'href="/preferences"' not in page.text
    assert "__RESEARCH_PREFERENCES__" not in page.text


async def test_futures_bookmark_opens_standalone_page_without_starting_research(
    client, auth_cookies, test_user
):
    from server.agent.runtime import init_runtime_db
    from server.database import get_db

    init_runtime_db()
    for path in ("/prism?mode=futures&symbol=cu", "/?futures=cu"):
        response = await client.get(path + "&next=https://invalid.example", cookies=auth_cookies)
        assert response.status_code == 307
        assert response.headers["location"] == "/futures?symbol=CU"
    page = await client.get("/futures?symbol=CU", cookies=auth_cookies)
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert 'id="futures-content"' in page.text
    assert f'data-account="{test_user["username"]}"' in page.text
    assert "/static/js/futures.js" in page.text and "/static/js/app.core.js" not in page.text
    assert "/static/js/futures-copilot.js" in page.text
    assert "futuresStartResearch" not in page.text
    prism = await client.get("/prism", cookies=auth_cookies)
    assert 'href="/futures"' in prism.text and "futuresWorkspace" not in prism.text

    with get_db() as db:
        assert db.execute("SELECT count(*) FROM native_agent_runs").fetchone()[0] == 0
