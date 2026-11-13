"""Real owner/CAS/consent contracts and observed-price calculations; no live HTTP."""

import asyncio
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from test_portfolio_import_risk import owned as owned
from test_portfolio_import_risk import snapshot

from server import capabilities as cap
from server import exchange_futures, portfolio
from server import portfolio_valuation as V
from server.agent import store as agents
from server.agent.tools import NativeTools
from server.database import get_db
from server.market.models import Quote
from server.portfolio_import import ImportPreview, parse_preview
from server.research import create_research_run
from server.research_context import EvidenceTracker, build_research_context
from server.work_lifecycle import Manager

NOW = datetime(2026, 11, 3, 8, tzinfo=UTC)
OWNER = "portfolio-alice"


@pytest.fixture
def market(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(V, "datetime", Clock)
    calls = []

    async def quote(code):
        calls.append(("quote", code))
        return Quote(
            ticker=code,
            price=12,
            source="synthetic",
            timestamp=int(NOW.timestamp()),
            data_timestamp=int(NOW.timestamp()),
            retrieved_at=int(NOW.timestamp()),
            timestamp_kind="trade",
            data_kind="quote",
        ), False

    async def settlements(symbol, deadline):
        calls.append(("settlement", symbol))
        body = json.dumps(
            {
                "o_code": "0000",
                "report_date": "20261103",
                "o_curinstrument": [
                    {
                        "PRODUCTID": "rb_f",
                        "PRODUCTNAME": "螺纹钢",
                        "DELIVERYMONTH": month,
                        "CLOSEPRICE": 3590,
                        "SETTLEMENTPRICE": 3600,
                        "VOLUME": 100,
                        "OPENINTEREST": 20,
                    }
                    for month in ("2701", "2705")
                ],
            }
        )
        return exchange_futures.parse_contracts(body.encode(), symbol, NOW.date())

    real_settlements = V._settlements
    chain = SimpleNamespace(get_quote=quote)
    monkeypatch.setattr(V, "get_chain", lambda: chain)
    monkeypatch.setattr(V, "_settlements", settlements)
    return SimpleNamespace(calls=calls, chain=chain, real_settlements=real_settlements)


async def save(owned, **changes):
    client, identities = owned
    raw = snapshot()
    raw.pop("configured")
    raw.pop("version")
    raw.update(
        expected_version=0, allow_market_pricing=True, futures_equity_basis="marked_to_reference_prices"
    )
    raw["observed_at"] = (NOW - timedelta(days=1)).isoformat()
    for p in raw["positions"]:
        p["price_as_of"] = raw["observed_at"]
        if p.get("asset_class") == "futures":
            p["terms_as_of"] = raw["observed_at"]
    raw.update(changes)
    response = await client.put("/api/preferences/portfolio", json=raw, cookies=identities[OWNER])
    assert response.status_code == 200, response.text
    return response.json()["portfolio"]


async def price(owned, version=1, **kwargs):
    client, identities = owned
    return await client.post(
        "/api/preferences/portfolio/reprice",
        json={"expected_version": version},
        cookies=identities[OWNER],
        **kwargs,
    )


async def configure_profile(owned):
    client, identities = owned
    value = dict(
        expected_version=0,
        style="balanced",
        horizon="medium",
        max_position_pct=20,
        max_drawdown_pct=10,
        allow_leverage=False,
    )
    assert (
        await client.put("/api/preferences/research", json=value, cookies=identities[OWNER])
    ).status_code == 200


async def test_reprices_real_instrument_data_without_rewriting_any_snapshot(owned, market):
    await configure_profile(owned)
    await save(owned)
    with get_db() as db:
        before = tuple(db.execute("SELECT * FROM portfolio_snapshots").fetchone())
        history = [tuple(r) for r in db.execute("SELECT * FROM portfolio_history")]
    response = await price(owned)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    value = response.json()
    risk = value["risk"]
    rules = {r["id"]: r for r in risk["rules"]}
    assert value["status"] == "collected" and value["positions_changed"] is False
    assert rules["net_equity"]["observed"] == "13600.0"  # cash1000 + shares600 + marked futures12000
    assert rules["concentration"]["status"] == "breach" and risk["account_verified"] is False
    assert "不变" in risk["assumptions"][0]
    assert value["positions"][1]["basis"] == "exchange_settlement"
    assert value["positions"][1]["precision"] == "date" and len(value["positions"][1]["body_sha256"]) == 64
    assert sorted(market.calls) == [("quote", "600000"), ("settlement", "RB")]
    with get_db() as db:
        assert tuple(db.execute("SELECT * FROM portfolio_snapshots").fetchone()) == before
        assert [tuple(r) for r in db.execute("SELECT * FROM portfolio_history")] == history


async def test_short_futures_direction_and_same_product_collection_are_not_netting(owned, market):
    await configure_profile(owned)
    raw = snapshot()["positions"]
    raw = [{**p, "price_as_of": (NOW - timedelta(days=1)).isoformat()} for p in raw]
    raw.append({**raw[1], "side": "short", "asset_id": "SHFE:RB2705"})
    await save(owned, positions=raw)
    value = (await price(owned)).json()
    rules = {r["id"]: r for r in value["risk"]["rules"]}
    assert rules["net_equity"]["observed"] == "11600.0"  # equal long gain/short loss, gross remains
    assert rules["futures_margin"]["observed"] == "17280.0"
    assert market.calls.count(("settlement", "RB")) == 1
    assert len(value["positions"]) == 3


async def test_dce_research_coverage_does_not_enable_unqualified_portfolio_prices(owned, market):
    await configure_profile(owned)
    positions = snapshot()["positions"]
    positions[1] = {**positions[1], "asset_id": "DCE:M2701", "multiplier": "10"}
    for position in positions:
        position["price_as_of"] = (NOW - timedelta(days=1)).isoformat()
    await save(owned, positions=positions)
    with get_db() as db:
        before = tuple(db.execute("SELECT * FROM portfolio_snapshots").fetchone())
    value = (await price(owned)).json()
    assert value["positions"][1]["reason"] == "instrument_not_covered"
    assert value["risk"]["overall"] == "not_evaluated"
    assert market.calls == [("quote", "600000")]
    assert not any(rule["id"] == "net_equity" for rule in value["risk"]["rules"])
    with get_db() as db:
        assert tuple(db.execute("SELECT * FROM portfolio_snapshots").fetchone()) == before


@pytest.mark.parametrize("change,status", [({"allow_market_pricing": False}, 403), ({}, 409)])
async def test_no_external_read_without_current_consent_and_matching_version(owned, market, change, status):
    await save(owned, **change)
    response = await price(owned, version=1 if change else 99)
    assert response.status_code == status and market.calls == []


async def test_route_owner_csrf_and_disabled_identity_precede_quotes(owned, market):
    await save(owned)
    client, identities = owned
    endpoint = "/api/preferences/portfolio/reprice"
    assert (await client.post(endpoint, json={"expected_version": 1})).status_code == 401
    assert (
        await client.post(endpoint, json={"expected_version": 1}, cookies=identities["portfolio-bob"])
    ).status_code == 409
    assert (await price(owned, headers={"X-Prism-Account": "portfolio-bob"})).status_code == 409
    assert (await price(owned, headers={"X-Requested-With": "not-xhr"})).status_code == 403
    with get_db() as db:
        db.execute("UPDATE users SET disabled_at=1 WHERE username=?", [OWNER])
    assert (await price(owned)).status_code == 401 and not market.calls


@pytest.mark.parametrize("change", [{"futures_equity": None}, {"observed_at": NOW.isoformat()}])
async def test_marked_equity_requires_an_explicit_synchronized_basis(owned, market, change):
    raw = snapshot()
    raw.pop("configured")
    raw.pop("version")
    raw.update(expected_version=0, futures_equity_basis="marked_to_reference_prices")
    raw.update(change)
    with pytest.raises(ValidationError):
        portfolio.PortfolioUpdate.model_validate(raw)
    assert market.calls == []


async def test_legacy_equity_basis_never_invents_a_new_account_balance(owned, market):
    await configure_profile(owned)
    await save(owned, futures_equity_basis="unverified")
    value = (await price(owned)).json()
    rules = value["risk"]["rules"]
    assert value["status"] == "collected" and value["risk"]["overall"] == "not_evaluated"
    assert any(r["id"] == "futures_equity_basis" and r["status"] == "not_evaluated" for r in rules)
    assert not any(r["id"] == "net_equity" for r in rules)


@pytest.mark.parametrize("mode", ["stale", "retrieved", "wrong_id", "zero", "future", "old"])
async def test_unusable_quote_never_blends_with_old_price_or_hides_other_source(owned, market, mode):
    await save(owned)

    async def invalid(code):
        values = dict(
            ticker=code,
            price=12,
            source="synthetic",
            timestamp=int(NOW.timestamp()),
            data_timestamp=int(NOW.timestamp()),
            timestamp_kind="trade",
            data_kind="quote",
        )
        if mode == "stale":
            return Quote(**values), True
        if mode == "retrieved":
            values.update(data_timestamp=None, timestamp_kind="retrieved")
        if mode == "wrong_id":
            values["ticker"] = "600001"
        if mode == "zero":
            values["price"] = 0
        if mode == "future":
            values["data_timestamp"] = int((NOW + timedelta(days=1)).timestamp())
        if mode == "old":
            values["data_timestamp"] = int((NOW - timedelta(days=9)).timestamp())
        return Quote(**values), False

    market.chain.get_quote = invalid
    value = (await price(owned)).json()
    assert value["status"] == "partial" and value["positions"][0]["status"] == "unavailable"
    assert "price" not in value["positions"][0]
    assert value["positions"][1]["status"] == "collected" and value["market_basis"]["missing_count"] == 1
    assert value["risk"]["overall"] == "not_evaluated" and not any(
        r["id"] == "net_equity" for r in value["risk"]["rules"]
    )


async def test_duplicate_ticker_uses_one_read_and_explicit_foreign_or_wrong_market_is_uncovered(
    owned, market
):
    raw = snapshot()["positions"][0]
    raw["price_as_of"] = (NOW - timedelta(days=1)).isoformat()
    saved = await save(owned, positions=[raw])
    # Simulate a previously accepted snapshot; new writes reject alias duplicates.
    saved["positions"] = [
        raw,
        {**raw, "asset_id": "600000"},
        {**raw, "asset_id": "SZ:600000"},
        {**raw, "asset_id": "US:AAPL", "currency": "USD"},
    ]
    with get_db() as db:
        db.execute("UPDATE portfolio_snapshots SET payload=? WHERE owner=?", [json.dumps(saved), OWNER])
    value = (await price(owned)).json()
    assert len(market.calls) == 1 and market.calls[0] == ("quote", "600000")
    assert value["market_basis"]["collected_count"] == 2 and value["market_basis"]["missing_count"] == 2


@pytest.mark.parametrize("revoke", ["version", "permission", "account"])
async def test_rechecks_identity_and_snapshot_after_real_await(owned, market, revoke):
    await save(owned)
    original = market.chain.get_quote

    async def changed(code):
        value = await original(code)
        with get_db() as db:
            if revoke == "account":
                db.execute("UPDATE users SET disabled_at=1 WHERE username=?", [OWNER])
            else:
                row = json.loads(
                    db.execute("SELECT payload FROM portfolio_snapshots WHERE owner=?", [OWNER]).fetchone()[0]
                )
                row["version"] += 1
                if revoke == "permission":
                    row["allow_market_pricing"] = False
                db.execute(
                    "UPDATE portfolio_snapshots SET version=?,payload=? WHERE owner=?",
                    [row["version"], json.dumps(row), OWNER],
                )
        return value

    market.chain.get_quote = changed
    response = await price(owned)
    assert response.status_code == (401 if revoke == "account" else 409)
    assert "positions" not in response.json()


async def test_dated_settlement_cannot_backdate_an_intraday_reference(owned, market):
    await save(
        owned,
        observed_at=NOW.isoformat(),
        positions=[{**p, "price_as_of": NOW.isoformat()} for p in snapshot()["positions"]],
    )
    value = (await price(owned)).json()
    assert value["positions"][1]["reason"] == "observation_not_newer_than_reference"
    assert value["risk"]["overall"] == "not_evaluated"


async def test_daily_close_date_uses_shanghai_day_instead_of_utc_day(owned, market):
    await configure_profile(owned)
    await save(owned)
    original = market.chain.get_quote

    async def daily(code):
        quote, stale = await original(code)
        # ChinaNov3 midnight is UTCNov2, but is newer than ChinaNov2's snapshot.
        stamp = int(datetime(2026, 11, 3, tzinfo=V.SHANGHAI).timestamp())
        return quote.model_copy(update={"data_kind": "daily_close", "data_timestamp": stamp}), stale

    market.chain.get_quote = daily
    value = (await price(owned)).json()
    assert value["positions"][0]["status"] == "collected"
    assert value["positions"][0]["precision"] == "date"
    assert value["positions"][0]["data_as_of"] == "2026-11-03"


async def test_timeout_is_bounded_concurrent_and_cancellation_does_not_restart(owned, market, monkeypatch):
    await save(
        owned,
        positions=[
            {
                **snapshot()["positions"][0],
                "asset_id": f"6000{i:02d}",
                "price_as_of": (NOW - timedelta(days=1)).isoformat(),
            }
            for i in range(12)
        ],
    )
    active = 0
    peak = 0
    calls = 0

    async def waiting(code):
        nonlocal active, peak, calls
        active += 1
        calls += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(10)
        finally:
            active -= 1

    market.chain.get_quote = waiting
    monkeypatch.setattr(V, "SECONDS", 0.01)
    response = await price(owned)
    assert response.status_code == 200
    assert peak <= V.CONCURRENCY and calls == V.CONCURRENCY and active == 0
    value = response.json()
    assert value["market_basis"]["missing_count"] == 12
    assert {p["reason"] for p in value["positions"]} <= {"source_timeout", "request_budget"}


async def test_server_does_not_disclose_upstream_exception_text(owned, market):
    await save(owned)

    async def failure(code):
        raise RuntimeError("SYNTHETIC_PRIVATE_HEADER never retain")

    market.chain.get_quote = failure
    response = await price(owned)
    assert response.status_code == 200 and "SYNTHETIC_PRIVATE_HEADER" not in response.text
    assert response.json()["positions"][0]["reason"] == "source_unavailable"


@contextmanager
def native_tools(run):
    create_research_run(OWNER, OWNER, "合成风险问题", build_research_context(run_id=run["run_id"]))
    manager = Manager(max_workers=2)
    parent = manager.admit("agent", persist_required=False)
    try:
        with cap.native_run_context(OWNER, OWNER, run["run_id"], parent_lease=parent) as context:
            yield NativeTools(context, parent, EvidenceTracker(run["run_id"]))
    finally:
        parent.release()
        manager._pool.shutdown(wait=True, cancel_futures=False)


def claimed_run():
    run = agents.create_run(OWNER, OWNER, "合成风险问题", "synthetic", "research")
    assert agents.claim_run(OWNER, run["run_id"], "synthetic-executor")
    return run


async def test_native_tool_reuses_once_sends_only_summary_and_keeps_frozen_preferences(owned, market):
    await configure_profile(owned)
    await save(owned, share_with_agent=True)
    run = claimed_run()
    # A newer preference does not rewrite this run's version1 policy.
    client, identities = owned
    newer = dict(
        expected_version=1,
        style="aggressive",
        horizon="long",
        max_position_pct=99,
        max_drawdown_pct=40,
        allow_leverage=True,
    )
    assert (
        await client.put("/api/preferences/research", json=newer, cookies=identities[OWNER])
    ).status_code == 200
    with native_tools(run) as tools:
        first = await tools.execute("portfolio_risk", {}, "risk-one")
        assert first["ok"] and first["data"]["risk"]["profile_version"] == 1
        text = json.dumps(first["data"])
        assert all(
            private not in text
            for private in ("600000", "RB2701", '"positions"', '"quantity"', '"source_url"')
        )
        assert first["data"]["risk"]["account_verified"] is False
        assert len(market.calls) == 2
        second = await tools.execute("portfolio_risk", {}, "risk-two")
        assert second["ok"] and second["data"]["cached_for_this_run"] is True and len(market.calls) == 2
        invalid = await tools.execute("portfolio_risk", {"owner": "portfolio-bob"}, "risk-forged")
        assert invalid["error"]["code"] == "invalid_arguments" and len(market.calls) == 2


@pytest.mark.parametrize("initial_share", [False, True])
async def test_agent_needs_both_original_sharing_and_unchanged_current_consent(owned, market, initial_share):
    await configure_profile(owned)
    saved = await save(owned, share_with_agent=initial_share)
    run = claimed_run()
    client, identities = owned
    body = {k: v for k, v in saved.items() if k not in {"version", "configured", "updated_at", "input_basis"}}
    body.update(expected_version=1, share_with_agent=not initial_share)
    assert (
        await client.put("/api/preferences/portfolio", json=body, cookies=identities[OWNER])
    ).status_code == 200
    with native_tools(run) as tools:
        result = await tools.execute("portfolio_risk", {}, "risk-denied")
        assert not result["ok"] and result["error"]["code"] in {
            "risk_permission_required",
            "risk_snapshot_changed",
        }
        assert market.calls == []


async def test_failed_native_read_is_not_retried_under_a_new_tool_id(owned, market, monkeypatch):
    await configure_profile(owned)
    await save(owned, share_with_agent=True)
    run = claimed_run()
    attempts = []

    async def failed(*args, **kwargs):
        attempts.append(True)
        raise RuntimeError("unknown completion")

    monkeypatch.setattr(V, "reprice", failed)
    with native_tools(run) as tools:
        first = await tools.execute("portfolio_risk", {}, "risk-failed-one")
        second = await tools.execute("portfolio_risk", {}, "risk-failed-two")
    assert not first["ok"] and not second["ok"] and len(attempts) == 1 and not market.calls


async def test_native_cached_summary_is_not_returned_after_book_changes(owned, market):
    await configure_profile(owned)
    saved = await save(owned, share_with_agent=True)
    run = claimed_run()
    with native_tools(run) as tools:
        assert (await tools.execute("portfolio_risk", {}, "risk-original"))["ok"]
        client, identities = owned
        body = {
            k: v for k, v in saved.items() if k not in {"version", "configured", "updated_at", "input_basis"}
        }
        body.update(expected_version=1, cash="2000")
        assert (
            await client.put("/api/preferences/portfolio", json=body, cookies=identities[OWNER])
        ).status_code == 200
        result = await tools.execute("portfolio_risk", {}, "risk-after-change")
        assert not result["ok"] and result["error"]["code"] == "risk_snapshot_changed"
        assert len(market.calls) == 2


async def test_authorization_is_checked_before_each_new_source_and_before_return(owned, market, monkeypatch):
    await save(owned)
    monkeypatch.setattr(V, "CONCURRENCY", 1)
    original = market.chain.get_quote
    stopped = False

    async def after_first(code):
        nonlocal stopped
        value = await original(code)
        stopped = True
        return value

    market.chain.get_quote = after_first

    def guard():
        if stopped:
            raise HTTPException(409, "synthetic stop")

    with pytest.raises(HTTPException):
        await V.reprice(OWNER, 1, authorize=guard)
    assert market.calls == [("quote", "600000")]


async def test_cancellation_closes_outstanding_reads_and_preserves_snapshot(owned, market):
    await save(owned)
    entered = asyncio.Event()
    closed = asyncio.Event()

    async def waiting(code):
        entered.set()
        try:
            await asyncio.sleep(10)
        finally:
            closed.set()

    market.chain.get_quote = waiting
    task = asyncio.create_task(V.reprice(OWNER, 1))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM portfolio_history").fetchone()[0] == 1


async def test_api_rate_limit_cannot_start_seventh_batch(owned, market):
    await save(owned)
    for _ in range(6):
        assert (await price(owned)).status_code == 200
    assert (await price(owned)).status_code == 429 and len(market.calls) == 12


@pytest.mark.parametrize("alias", ["600000", "SH600000", "600000.SH", "sh:600000"])
def test_alias_duplicates_cannot_split_new_holdings_or_understate_legacy_concentration(alias):
    raw = snapshot()
    raw["futures_equity"] = None
    raw["positions"] = [raw["positions"][0], {**raw["positions"][0], "asset_id": alias}]
    report = portfolio.evaluate(
        dict(configured=True, version=1, max_position_pct=40, max_drawdown_pct=10, allow_leverage=False),
        raw,
        now=NOW.timestamp(),
    )
    rule = next(r for r in report["rules"] if r["id"] == "concentration")
    # Cash1000 + securities1000: one issuer at50%, not two at25%.
    assert rule["observed"] == "50.0000" and rule["status"] == "breach"
    data = {k: v for k, v in raw.items() if k not in {"configured", "version"}}
    data["expected_version"] = 0
    with pytest.raises(ValidationError):
        portfolio.PortfolioUpdate.model_validate(data)
    with pytest.raises(HTTPException) as duplicate:
        parse_preview(
            ImportPreview(
                csv_text=f"代码,数量,价格\nSH:600000,1,10\n{alias},1,10",
                currency="CNY",
                price_as_of=NOW.isoformat(),
            )
        )
    assert duplicate.value.status_code == 422 and "重复" in duplicate.value.detail


async def test_older_quote_and_mixed_dates_remain_visible_as_unverified(owned, market, monkeypatch):
    await configure_profile(owned)
    positions = snapshot()["positions"]
    reference = (NOW - timedelta(days=2)).isoformat()
    for p in positions:
        p["price_as_of"] = reference
    await save(owned, positions=positions, observed_at=reference)
    original = market.chain.get_quote

    async def old_quote(code):
        value, stale = await original(code)
        return value.model_copy(
            update={"data_timestamp": int((NOW - timedelta(minutes=30)).timestamp())}
        ), stale

    market.chain.get_quote = old_quote

    async def prior_settlement(symbol, deadline):
        return {
            "status": "collected",
            "data_as_of": "2026-11-02",
            "contracts": [{"contract": "rb2701", "settlement": 3600}],
            "body_sha256": "a" * 64,
            "source_url": "https://www.shfe.com.cn/synthetic",
        }

    monkeypatch.setattr(V, "_settlements", prior_settlement)
    value = (await price(owned)).json()
    assert value["status"] == "collected"
    rules = {r["id"]: r for r in value["risk"]["rules"]}
    assert rules["market_recency"]["status"] == "not_evaluated"
    assert rules["market_dates"]["status"] == "not_evaluated"
    assert rules["broker_terms_verification"]["status"] == "not_evaluated"
    assert value["market_basis"]["data_dates"] == ["2026-11-02", "2026-11-03"]


@pytest.mark.parametrize("failure", ["dated404", "denied", "network", "malformed"])
async def test_settlement_transport_is_fixed_anonymous_and_only_dated404_looks_back(
    market, monkeypatch, failure
):
    calls = []

    async def handler(request):
        calls.append(request)
        assert request.url.scheme == "https" and request.url.host == "www.shfe.com.cn"
        assert (
            request.method == "GET"
            and "cookie" not in request.headers
            and "authorization" not in request.headers
        )
        if len(calls) == 1:
            if failure == "network":
                raise httpx.ConnectError("synthetic TLS refusal", request=request)
            if failure == "denied":
                return httpx.Response(403)
            if failure == "malformed":
                return httpx.Response(200, content=b"not-json")
            return httpx.Response(404, headers=[("Set-Cookie", "synthetic=do-not-replay")])
        assert failure == "dated404" and request.url.path.endswith("kx20261102.dat")
        return httpx.Response(
            200,
            json={
                "o_code": "0000",
                "report_date": "20261102",
                "o_curinstrument": [
                    {
                        "PRODUCTID": "rb_f",
                        "PRODUCTNAME": "螺纹钢",
                        "DELIVERYMONTH": "2701",
                        "CLOSEPRICE": 3590,
                        "SETTLEMENTPRICE": 3600,
                        "VOLUME": 100,
                        "OPENINTEREST": 20,
                    }
                ],
            },
        )

    client = httpx.AsyncClient

    def factory(**kwargs):
        assert (
            kwargs["follow_redirects"] is False
            and kwargs["trust_env"] is False
            and kwargs.get("verify", True) is not False
        )
        return client(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(V.public_network.httpx, "AsyncClient", factory)
    value = await market.real_settlements("RB", asyncio.get_running_loop().time() + 1)
    assert len(calls) == (2 if failure == "dated404" else 1)
    assert value["status"] == ("collected" if failure == "dated404" else "unavailable")
