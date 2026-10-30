"""Cross-account, temporal and real database journeys for the new product paths."""

import json
from datetime import date, timedelta
from uuid import uuid4

import httpx
import pytest

from server import futures
from server.agent import store as agent_store
from server.database import get_db
from server.preferences import init_preferences_db, profile_prompt
from server.productivity.calendar import init_calendar_db
from server.productivity.store import init_productivity_db


@pytest.fixture
def feature_db(auth_cookies):
    init_preferences_db()
    init_productivity_db()
    init_calendar_db()
    with get_db() as db:
        db.execute("INSERT INTO users(username,password_hash) VALUES('second-user','synthetic-only')")
    from server.auth import create_session

    return auth_cookies, {"token": create_session("second-user")}


def profile(**updates):
    return {
        "expected_version": 0,
        "style": "conservative",
        "horizon": "long",
        "max_position_pct": 15,
        "max_drawdown_pct": 8,
        "allow_leverage": False,
        **updates,
    }


def event(**updates):
    return {
        "request_id": str(uuid4()),
        "title": "项目评审",
        "description": "核对里程碑",
        "start_at": "2026-11-02T09:00:00+08:00",
        "end_at": "2026-11-02T10:00:00+08:00",
        "timezone": "Asia/Shanghai",
        **updates,
    }


RANGE = "?start=2026-11-02T00:00:00Z&end=2026-11-09T00:00:00Z"


async def test_profile_is_owned_versioned_and_bound_to_run(client, feature_db):
    first, second = feature_db
    assert (await client.get("/api/preferences/research", cookies=first)).json()["profile"] == {
        "configured": False,
        "version": 0,
    }
    saved = await client.put("/api/preferences/research", cookies=first, json=profile())
    assert saved.status_code == 200
    assert saved.json()["portfolio_risk_status"] == "not_evaluated"
    with get_db() as db:
        db.execute("INSERT INTO conversations(id,username) VALUES('profile-conv','testuser')")
    run = agent_store.create_run("testuser", "profile-conv", "合成研究", "test-model", "research", "once")
    updated = await client.put(
        "/api/preferences/research", cookies=first, json=profile(expected_version=1, style="aggressive")
    )
    assert updated.status_code == 200
    assert agent_store.get_run("testuser", run["id"])["profile"]["style"] == "conservative"
    again = agent_store.create_run("testuser", "profile-conv", "合成研究", "test-model", "research", "once")
    assert again["id"] == run["id"] and again["profile"]["version"] == 1
    assert "未评估" in profile_prompt(again["profile"])
    assert (await client.put("/api/preferences/research", cookies=first, json=profile())).status_code == 409
    assert (await client.get("/api/preferences/research", cookies=second)).json()["profile"][
        "configured"
    ] is False
    with get_db() as db:
        db.execute("DELETE FROM conversations WHERE id='profile-conv'")
        assert not db.execute("SELECT * FROM native_run_profiles").fetchall()


@pytest.mark.parametrize(
    "change",
    [
        {"max_drawdown_pct": 101},
        {"style": "invented"},
        {"allow_leverage": "yes"},
        {"owner": "second-user"},
        {"max_position_pct": True},
    ],
)
async def test_invalid_profile_does_not_write(client, feature_db, change):
    first, _ = feature_db
    response = await client.put("/api/preferences/research", cookies=first, json=profile(**change))
    assert response.status_code == 422
    assert (await client.get("/api/preferences/research", cookies=first)).json()["profile"]["version"] == 0


async def test_account_binding_and_auth(client, feature_db):
    first, _ = feature_db
    for path in ["/api/preferences/research", "/api/work/calendar" + RANGE]:
        assert (
            await client.get(path, cookies=first, headers={"X-Prism-Account": "second-user"})
        ).status_code == 409
        assert (await client.get(path)).status_code == 401
    response = await client.put(
        "/api/preferences/research",
        cookies=first,
        content=json.dumps(profile()),
        headers={"X-Requested-With": "invalid"},
    )
    assert response.status_code == 403


async def test_calendar_create_conflict_replay_edit_and_export(client, feature_db):
    first, second = feature_db
    payload = event()
    response = await client.post("/api/work/calendar", cookies=first, json=payload)
    assert response.status_code == 200, response.text
    row = response.json()["event"]
    assert row["start_at"] == "2026-11-02T01:00:00.000000Z"
    replay = await client.post("/api/work/calendar", cookies=first, json=payload)
    assert replay.json()["replayed"] and replay.json()["event"]["id"] == row["id"]
    assert (
        await client.post("/api/work/calendar", cookies=first, json={**payload, "title": "不同日程"})
    ).status_code == 409
    overlap = await client.post(
        "/api/work/calendar", cookies=first, json=event(start_at="2026-11-02T09:30:00+08:00")
    )
    assert overlap.json()["conflicts"][0]["id"] == row["id"]
    assert (await client.get("/api/work/calendar" + RANGE, cookies=second)).json()["events"] == []
    assert (await client.get("/api/work/calendar/" + row["id"] + ".ics", cookies=second)).status_code == 404
    edit = event(expected_version=1, title="已调整", cancelled=False)
    result = await client.put("/api/work/calendar/" + row["id"], cookies=first, json=edit)
    assert result.status_code == 200 and result.json()["event"]["version"] == 2
    assert (await client.put("/api/work/calendar/" + row["id"], cookies=first, json=edit)).json()["replayed"]
    assert (
        await client.put("/api/work/calendar/" + row["id"], cookies=first, json=event(expected_version=1))
    ).status_code == 409
    exported = await client.get("/api/work/calendar/" + row["id"] + ".ics", cookies=first)
    assert exported.headers["cache-control"] == "no-store"
    assert "DTSTART:20261102T010000Z" in exported.text and "SUMMARY:已调整" in exported.text
    cancelled = await client.put(
        "/api/work/calendar/" + row["id"], cookies=first, json=event(expected_version=2, cancelled=True)
    )
    assert cancelled.json()["event"]["cancelled"]
    assert row["id"] not in [
        r["id"] for r in (await client.get("/api/work/calendar" + RANGE, cookies=first)).json()["events"]
    ]


@pytest.mark.parametrize(
    "updates",
    [
        {"end_at": "2026-11-02T08:00:00+08:00"},
        {
            "start_at": "2026-03-08T02:30:00-05:00",
            "end_at": "2026-03-08T04:00:00-04:00",
            "timezone": "America/New_York",
        },
        {"start_at": "2026-11-02T09:00:00"},
        {"timezone": "../bad"},
        {"owner": "second-user"},
    ],
)
async def test_calendar_rejects_invalid_time_or_owner(client, feature_db, updates):
    response = await client.post("/api/work/calendar", cookies=feature_db[0], json=event(**updates))
    assert response.status_code == 422


async def test_calendar_includes_due_tasks_and_deletion_cleans_private_data(client, feature_db):
    first, _ = feature_db
    space = (
        await client.post(
            "/api/work/spaces", cookies=first, json={"request_id": str(uuid4()), "name": "我的工作"}
        )
    ).json()["workspace"]
    await client.post(
        "/api/work/spaces/" + space["id"] + "/tasks",
        cookies=first,
        json={"request_id": str(uuid4()), "title": "本周交付", "due_at": "2026-11-03T09:00:00+08:00"},
    )
    await client.post("/api/work/calendar", cookies=first, json=event())
    await client.put("/api/preferences/research", cookies=first, json=profile())
    agenda = (await client.get("/api/work/calendar" + RANGE, cookies=first)).json()
    assert len(agenda["tasks"]) == len(agenda["events"]) == 1
    assert agenda["reminders"] == "not_configured"
    with get_db() as db:
        db.execute("DELETE FROM users WHERE username='testuser'")
        for table in ["work_calendar", "work_tasks", "work_mutations", "research_profiles"]:
            assert not db.execute("SELECT * FROM " + table).fetchall()


def bars(count=70):
    start = date(2026, 7, 1)
    return [
        [(start + timedelta(days=i)).isoformat(), 100 + i, 102 + i, 99 + i, 101 + i, 1000 + i, 2000 + i, 0]
        for i in range(count)
    ]


def test_futures_calculations_keep_missing_data_and_series_boundaries():
    result = futures.analyze(
        "RB", futures.parse_bars(json.dumps(bars()).encode(), date(2026, 10, 30)), "2026-10-30T12:00:00+08:00"
    )
    assert result["technical"]["sma20"] == 160.5
    assert result["positioning"]["open_interest_change"] == 1
    assert result["positioning"]["net_capital_flow"] is None
    assert result["latest"]["settlement"] is None
    assert not result["tradable_contract"]
    assert result["freshness"] == "older_observation"
    assert all(item["status"] == "not_collected" for item in result["fundamentals"])


@pytest.mark.parametrize("change", ["duplicate", "future", "nan", "bad_ohlc"])
def test_futures_rejects_malformed_observations(change):
    rows = bars(2)
    if change == "duplicate":
        rows[1][0] = rows[0][0]
    if change == "future":
        rows[1][0] = "2030-01-01"
    if change == "nan":
        rows[1][4] = float("nan")
    if change == "bad_ohlc":
        rows[1][3] = 10000
    with pytest.raises(ValueError):
        futures.parse_bars(json.dumps(rows).encode(), date(2026, 10, 30))


async def test_real_futures_http_parser_and_cache_without_external_calls(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "stock2.finance.sina.com.cn"
        assert request.url.params["symbol"] == "RB0"
        return httpx.Response(
            200, text="/*<script>location.href='//sina.com';</script>*/\n=(" + json.dumps(bars()) + ");"
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        futures.httpx, "AsyncClient", lambda **kw: original(**kw, transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(futures, "_cache", {})
    first = await futures.snapshot("RB")
    first["latest"]["close"] = 0
    again = await futures.snapshot("RB")
    assert len(requests) == 1 and again["cached"] and again["latest"]["close"] == 170


def test_native_catalog_and_futures_workflow_share_real_tool():
    from server.agent.prompts import system_prompt, workflow_id
    from server.agent.tools import native_catalog

    assert "futures_snapshot" in {row["id"] for row in native_catalog()}
    assert workflow_id("futures-research") == "futures"
    from server.research_context import build_research_context

    text = system_prompt(build_research_context(), "futures", search_available=False)
    assert "futures_snapshot" in text and "多空方向" in text


async def test_new_pages_require_auth_and_are_not_cacheable(client, feature_db):
    for path in ["/work", "/calendar", "/preferences", "/futures"]:
        assert (await client.get(path)).status_code == 307
        response = await client.get(path, cookies=feature_db[0])
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("feature", ["work", "calendar", "futures", "preferences"])
async def test_new_pages_keep_feedback_feature_and_owned_progress(client, feature_db, feature):
    from server.feedback import init_feedback_db

    init_feedback_db()
    first, second = feature_db
    result = await client.post(
        "/api/feedback",
        cookies=first,
        json={
            "request_id": str(uuid4()),
            "category": "bug",
            "feature": feature,
            "page": "/" + feature,
            "title": "合成页面检查",
            "description": "仅使用合成描述验证功能归类和本人进度。",
        },
    )
    assert result.status_code == 200
    item = result.json()["feedback"]
    assert item["feature"] == feature
    response = await client.get("/api/feedback?feature=" + feature, cookies=first)
    assert response.status_code == 200 and response.json()["items"][0]["id"] == item["id"]
    assert (await client.get("/api/feedback/" + str(item["id"]), cookies=second)).status_code == 404


def test_futures_descending_series_keeps_latest_observations():
    rows = bars(210)
    forward = futures.parse_bars(json.dumps(rows).encode(), date(2027, 12, 31))
    reverse = futures.parse_bars(json.dumps(list(reversed(rows))).encode(), date(2027, 12, 31))
    assert len(reverse) == 180 and reverse == forward


def test_only_selected_futures_window_is_certified():
    rows = bars(210)
    rows[0][1] = 0
    assert len(futures.parse_bars(json.dumps(rows).encode(), date(2027, 12, 31))) == 180
    rows[-1][1] = 0
    with pytest.raises(ValueError):
        futures.parse_bars(json.dumps(rows).encode(), date(2027, 12, 31))
