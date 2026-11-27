"""Owner-scoped deterministic portfolio rules."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server import portfolio, preferences
from server.database import create_session, get_db


@pytest.fixture
async def owned_client():
    portfolio.init_portfolio_db()
    cookies = {}
    for owner in ("risk-alice", "risk-bob"):
        with get_db() as db:
            db.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", [owner, "synthetic-only"])
        cookies[owner] = {"token": create_session(owner)}
    app = FastAPI()
    app.include_router(preferences.router)
    app.include_router(portfolio.router)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Requested-With": "XMLHttpRequest"},
    ) as client:
        yield client, cookies


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
