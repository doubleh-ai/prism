"""Calendar exceptions/DST and owner-scoped deterministic portfolio rules."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from server import portfolio, preferences
from server.database import create_session, get_db
from server.productivity import calendar, notifications, store
from server.productivity.recurrence import Recurrence, expand


def test_monthly_recurrence_skips_invalid_dates_and_dst_gaps_do_not_consume_count():
    rows = expand(
        "series",
        "2026-01-31T14:00:00Z",
        "2026-01-31T15:00:00Z",
        "UTC",
        Recurrence(frequency="monthly", count=3),
    )
    assert [row["start_at"][:10] for row in rows] == ["2026-01-31", "2026-03-31", "2026-05-31"]
    rows = expand(
        "spring",
        "2026-03-07T07:30:00Z",
        "2026-03-07T08:30:00Z",
        "America/New_York",
        Recurrence(frequency="daily", count=3),
    )
    assert [row["start_at"][:16] for row in rows] == [
        "2026-03-07T07:30",
        "2026-03-09T06:30",
        "2026-03-10T06:30",
    ]
    with pytest.raises(ValueError):
        expand(
            "large",
            "2026-01-01T10:00:00Z",
            "2026-01-01T11:00:00Z",
            "UTC",
            Recurrence(frequency="daily", until="2028-01-01"),
        )
    with pytest.raises(ValidationError):
        Recurrence(frequency="weekly", count=2, weekdays=[True])


@pytest.fixture
async def owned_client():
    store.init_productivity_db()
    calendar.init_calendar_db()
    notifications.init_notifications_db()
    portfolio.init_portfolio_db()
    cookies = {}
    for owner in ("risk-alice", "risk-bob"):
        with get_db() as db:
            db.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", [owner, "synthetic-only"])
        cookies[owner] = {"token": create_session(owner)}
    app = FastAPI()
    app.include_router(calendar.router)
    app.include_router(notifications.router)
    app.include_router(preferences.router)
    app.include_router(portfolio.router)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Requested-With": "XMLHttpRequest", "X-CSRF-Token": "synthetic-test-csrf"},
        cookies={"csrf_token": "synthetic-test-csrf"},
    ) as client:
        yield client, cookies


async def test_recurring_instances_cancel_idempotency_and_real_ics(owned_client):
    client, cookies = owned_client
    alice, bob = cookies.values()
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=10)
    body = dict(
        request_id=str(uuid4()),
        title="合成周会",
        start_at=start.isoformat(),
        end_at=(start + timedelta(hours=1)).isoformat(),
        timezone="UTC",
        recurrence=dict(frequency="daily", count=3),
    )
    response = await client.post("/api/work/calendar", json=body, cookies=alice)
    assert response.status_code == 200, response.text
    event = response.json()["event"]
    assert event["recurrence"]["count"] == 3
    assert (await client.post("/api/work/calendar", json=body, cookies=alice)).json()["replayed"]
    params = dict(start=(start - timedelta(hours=1)).isoformat(), end=(start + timedelta(days=4)).isoformat())
    agenda = (await client.get("/api/work/calendar", params=params, cookies=alice)).json()
    assert len(agenda["events"]) == 1 and len(agenda["occurrences"]) == 3
    occurrence = agenda["occurrences"][0]
    path = f"/api/work/calendar/{event['id']}/occurrences/{occurrence['occurrence_id']}"
    cancel = dict(request_id=str(uuid4()), expected_version=occurrence["version"], cancelled=True)
    assert (await client.put(path, json=cancel, cookies=bob)).status_code == 404
    assert (await client.put(path, json=cancel, cookies=alice)).status_code == 200
    assert (await client.put(path, json=cancel, cookies=alice)).json()["replayed"]
    assert (
        await client.put(path, json={**cancel, "request_id": str(uuid4())}, cookies=alice)
    ).status_code == 409
    assert (
        len((await client.get("/api/work/calendar", params=params, cookies=alice)).json()["occurrences"]) == 2
    )
    assert notifications.collect_due(now=start.timestamp() - 60) == 0
    export = await client.get(f"/api/work/calendar/{event['id']}.ics", cookies=alice)
    assert export.status_code == 200 and "EXDATE:" in export.text and "RDATE:" in export.text
    assert (await client.get(f"/api/work/calendar/{event['id']}", cookies=alice)).json()["event"][
        "version"
    ] == 1
    assert (await client.get(f"/api/work/calendar/{event['id']}", cookies=bob)).status_code == 404


def _snapshot(**updates):
    now = datetime.now(UTC).isoformat()
    return dict(
        configured=True,
        version=1,
        currency="CNY",
        observed_at=now,
        cash="500",
        debt="0",
        positions=[dict(asset_id="SH:600000", quantity="50", price="10", currency="CNY", price_as_of=now)],
        **updates,
    )


def _profile():
    return dict(configured=True, version=4, max_position_pct=20, max_drawdown_pct=10, allow_leverage=False)


def test_risk_missing_stale_mixed_currency_and_precise_concentration():
    report = portfolio.evaluate(_profile(), _snapshot())
    assert report["overall"] == "breach"
    concentration = next(row for row in report["rules"] if row["id"] == "concentration")
    assert concentration["observed"] == "50.0000" and report["profile_version"] == 4
    stale = _snapshot()
    stale["positions"][0]["price_as_of"] = (datetime.now(UTC) - timedelta(days=9)).isoformat()
    assert portfolio.evaluate(_profile(), stale)["overall"] == "not_evaluated"
    stale["positions"][0]["price_as_of"] = datetime.now(UTC).isoformat()
    stale["positions"][0]["currency"] = "USD"
    assert portfolio.evaluate(_profile(), stale)["overall"] == "not_evaluated"
    assert portfolio.evaluate({"configured": False}, _snapshot())["overall"] == "not_evaluated"
    peak = _snapshot(equity_peak="2000", peak_as_of=datetime.now(UTC).isoformat(), net_flows_since_peak="0")
    assert (
        next(r for r in portfolio.evaluate(_profile(), peak)["rules"] if r["id"] == "drawdown")["observed"]
        == "50.0000"
    )


async def test_portfolio_ownership_versions_precision_and_account_deletion(owned_client):
    client, cookies = owned_client
    alice, bob = cookies.values()
    body = {k: v for k, v in _snapshot().items() if k not in {"configured", "version"}}
    body["expected_version"] = 0
    saved = await client.put("/api/preferences/portfolio", json=body, cookies=alice)
    assert saved.status_code == 200, saved.text
    assert saved.json()["portfolio"]["version"] == 1
    assert (await client.get("/api/preferences/portfolio", cookies=bob)).json()["portfolio"][
        "configured"
    ] is False
    assert (await client.put("/api/preferences/portfolio", json=body, cookies=alice)).status_code == 409
    assert (
        await client.put("/api/preferences/portfolio", json={**body, "cash": "NaN"}, cookies=alice)
    ).status_code == 422
    assert (
        await client.get("/api/preferences/portfolio", cookies=alice, headers={"X-Prism-Account": "risk-bob"})
    ).status_code == 409
    with get_db() as db:
        db.execute("DELETE FROM users WHERE username='risk-alice'")
        assert (
            db.execute("SELECT COUNT(*) FROM portfolio_history WHERE owner='risk-alice'").fetchone()[0] == 0
        )
        assert (
            db.execute("SELECT COUNT(*) FROM portfolio_snapshots WHERE owner='risk-alice'").fetchone()[0] == 0
        )
