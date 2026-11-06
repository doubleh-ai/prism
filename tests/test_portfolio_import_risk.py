"""Actual import, owner/CAS, linear futures calculations and consent snapshots."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from server import portfolio, preferences
from server.agent import store as agents
from server.database import create_session, get_db
from server.portfolio_import import ImportPreview, parse_preview

NOW = datetime(2026, 11, 3, 4, tzinfo=UTC)
STAMP = NOW.isoformat()


def profile():
    return dict(configured=True, version=3, max_position_pct=20, max_drawdown_pct=10, allow_leverage=False)


def future(**changes):
    return dict(
        asset_class="futures",
        asset_id="SHFE:RB2701",
        quantity="2",
        price="3500",
        currency="CNY",
        price_as_of=STAMP,
        side="long",
        multiplier="10",
        broker_margin_rate_pct="12",
        terms_as_of=STAMP,
        last_trading_date="2027-01-15",
        source_url="https://www.shfe.com.cn/",
        **changes,
    )


def snapshot():
    return dict(
        configured=True,
        version=5,
        currency="CNY",
        observed_at=STAMP,
        cash="1000",
        debt="0",
        futures_equity="10000",
        positions=[
            dict(
                asset_class="security",
                asset_id="SH:600000",
                quantity="50",
                price="10",
                currency="CNY",
                price_as_of=STAMP,
            ),
            future(),
        ],
    )


def check(value=None, *, now=NOW, preference=None):
    report = portfolio.evaluate(preference or profile(), value or snapshot(), now=now.timestamp())
    return report, {r["id"]: r for r in report["rules"]}


def test_futures_notional_never_becomes_equity_or_cash_and_stress_is_exact():
    report, rules = check()
    assert rules["net_equity"]["observed"] == "11500"
    assert rules["concentration"]["observed"] == "608.6957"
    assert rules["leverage"]["observed"] == "6.1304" and rules["leverage"]["status"] == "breach"
    assert rules["futures_margin"]["observed"] == "8400" and rules["futures_margin"]["status"] == "pass"
    assert report["scenarios"][0]["futures_loss"] == "3500"
    assert report["scenarios"][0]["remaining_futures_equity"] == "6500"
    assert report["scenarios"][0]["margin_shortfall"] == "1480.00"
    assert report["scenarios"][1]["margin_shortfall"] == "4560.0"
    assert report["input_basis"] == "user_supplied_not_independently_verified"


def test_same_contract_opposite_sides_do_not_hide_gross_exposure_or_margin():
    value = snapshot()
    value["positions"] = [future(), {**future(), "side": "short"}]
    value["cash"] = "0"
    report, rules = check(value)
    assert rules["concentration"]["observed"] == "1400.0000"
    assert rules["futures_margin"]["observed"] == "16800" and rules["futures_margin"]["status"] == "breach"
    assert report["scenarios"][0]["futures_loss"] == "7000"
    assert report["scenarios"][0]["estimated_margin"] == "16800.00"


def test_same_futures_product_across_months_is_one_concentration_scope():
    value = snapshot()
    value["positions"] = [future(), {**future(), "asset_id": "SHFE:RB2705"}]
    _, rules = check(value)
    assert rules["concentration"]["observed"] == "1272.7273"


@pytest.mark.parametrize("missing", ["source_url", "terms_as_of", "broker_margin_rate_pct"])
def test_missing_contract_terms_cannot_pass_margin_check(missing):
    value = snapshot()
    value["positions"][1][missing] = None
    report, rules = check(value)
    assert rules["futures_margin"]["status"] == "not_evaluated"
    assert report["scenarios"][0]["margin_shortfall"] is None


def test_missing_negative_and_stale_equity_or_terms_are_not_false_passes():
    value = snapshot()
    value["futures_equity"] = None
    report, rules = check(value)
    assert report["overall"] == "not_evaluated" and "net_equity" not in rules
    assert report["scenarios"][0]["remaining_futures_equity"] is None
    value["futures_equity"] = "-50"
    report, rules = check(value)
    assert rules["net_equity"]["observed"] == "1450"
    assert rules["futures_equity"]["status"] == "breach" and rules["futures_margin"]["status"] == "breach"
    value["positions"][1]["terms_as_of"] = (NOW - timedelta(days=8)).isoformat()
    _, rules = check(value)
    assert rules["futures_terms"]["status"] == "not_evaluated"


def test_expiry_uses_shanghai_date_and_near_delivery_remains_unassessed():
    value = snapshot()
    value["positions"][1]["last_trading_date"] = "2026-11-02"
    value["observed_at"] = "2026-11-02T19:00:00+00:00"
    for position in value["positions"]:
        position["price_as_of"] = "2026-11-02T19:00:00+00:00"
    value["positions"][1]["terms_as_of"] = "2026-11-02T19:00:00+00:00"
    _, rules = check(value, now=datetime(2026, 11, 2, 20, tzinfo=UTC))
    assert rules["futures_delivery"]["status"] == "breach"
    value["positions"][1]["last_trading_date"] = "2026-11-06"
    _, rules = check(value)
    assert rules["futures_delivery"]["status"] == "not_evaluated"
    value["positions"][1]["last_trading_date"] = None
    _, rules = check(value)
    assert rules["futures_delivery"]["status"] == "not_evaluated"


@pytest.mark.parametrize(
    "changes",
    [
        {"asset_id": "SHFE:RB0"},
        {"asset_id": "SHFE:RB2713"},
        {"quantity": "1.5"},
        {"quantity": True},
        {"multiplier": "NaN"},
        {"price": "0"},
        {"currency": "USD"},
        {"broker_margin_rate_pct": "101"},
        {"broker_margin_rate_pct": "0"},
        {"terms_as_of": "2026-11-03T12:00"},
        {"last_trading_date": "2026-02-30"},
        {"source_url": "https://private:secret@www.shfe.com.cn/"},
    ],
)
def test_future_contract_schema_does_not_guess_critical_inputs(changes):
    with pytest.raises(ValidationError):
        portfolio.FuturesPosition.model_validate({**future(), **changes})


def test_security_cannot_mislabel_a_domestic_futures_code_and_duplicate_side_rejected():
    with pytest.raises(ValidationError):
        portfolio.Position.model_validate(
            {
                k: v
                for k, v in future().items()
                if k in {"asset_id", "quantity", "price", "currency", "price_as_of"}
            }
        )
    value = {k: v for k, v in snapshot().items() if k not in {"configured", "version"}}
    value["expected_version"] = 0
    value["positions"] = [future(), future()]
    with pytest.raises(ValidationError):
        portfolio.PortfolioUpdate.model_validate(value)
    value["positions"][1] = {**future(), "side": "short"}
    assert len(portfolio.PortfolioUpdate.model_validate(value).positions) == 2


def preview(text, **changes):
    return parse_preview(ImportPreview(csv_text=text, currency="CNY", price_as_of=STAMP, **changes))


def test_csv_chinese_tab_bom_and_ignored_columns_are_bounded_and_only_preview():
    value = preview("\ufeff证券代码\t持仓数量\t最新价\t私人备注\nSH:600000\t50\t10\t不应回传的私人内容\n")
    assert value["counts"] == {"security": 1, "futures": 0, "total": 1}
    assert value["ignored_columns"] == 1 and value["input_basis"] == "user_supplied_preview"
    assert datetime.fromisoformat(value["positions"][0]["price_as_of"]) == NOW
    assert "私人" not in json.dumps(value, ensure_ascii=False)
    csv = (
        "代码,数量,价格,方向,合约乘数,保证金比例,规则核对时间,最后交易日\nSHFE:rb2701,2,3500,空,10,12,"
        + STAMP
        + ",2027-01-15\n"
    )
    result = preview(csv)
    assert result["counts"] == {"security": 0, "futures": 1, "total": 1}
    assert result["positions"][0]["asset_id"] == "SHFE:RB2701" and result["positions"][0]["side"] == "short"


@pytest.mark.parametrize(
    "text",
    [
        "\n",
        "代码,数量,成本价\n600000,1,10",
        "代码,asset_id,数量,价格\nA,A,1,10",
        "代码,数量,价格\nA,1,10,extra",
        '代码,数量,价格\n"A,1,10',
        "代码,数量,价格\nA,1,=RUN()",
        "代码,数量,价格\nA,1,NaN",
        "代码,数量,价格\nA,-1,10",
        "代码,数量,价格\nA,1,10\na,1,20",
        "代码,数量,价格\nSHFE:RB0,2,3500",
        "代码,数量,价格\nA,1,10\x00",
    ],
)
def test_csv_unsafe_ambiguous_or_duplicate_data_cannot_partially_import(text):
    with pytest.raises(HTTPException) as error:
        preview(text)
    assert error.value.status_code == 422
    assert "RUN()" not in error.value.detail


def test_csv_row_and_byte_bounds_and_explicit_time_are_enforced():
    with pytest.raises(HTTPException):
        preview("代码,数量,价格\n" + "\n".join(f"A{i},1,10" for i in range(101)))
    with pytest.raises(HTTPException) as error:
        preview("代码,数量,价格,备注\nA,1,10," + "甲" * 23000)
    assert error.value.status_code == 413
    with pytest.raises(HTTPException):
        parse_preview(ImportPreview(csv_text="代码,数量,价格\nA,1,10", currency="CNY"))
    with pytest.raises(HTTPException):
        parse_preview(
            ImportPreview(csv_text="代码,数量,价格,价格时间\nA,1,10,2026-11-03T12:00", currency="CNY")
        )


@pytest.fixture
async def owned():
    portfolio.init_portfolio_db()
    agents.init_agent_db()
    identities = {}
    for owner in ("portfolio-alice", "portfolio-bob"):
        with get_db() as db:
            db.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", [owner, "synthetic-only"])
            db.execute("INSERT INTO conversations(id,username) VALUES(?,?)", [owner, owner])
        identities[owner] = {"token": create_session(owner)}
    app = FastAPI()
    app.include_router(portfolio.router)
    app.include_router(preferences.router)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Requested-With": "XMLHttpRequest"},
    ) as client:
        yield client, identities


async def test_preview_never_writes_then_single_save_cas_and_owner_permissions(owned):
    client, identities = owned
    alice = identities["portfolio-alice"]
    bob = identities["portfolio-bob"]
    csv = "代码,数量,价格,方向,合约乘数\nSHFE:RB2701,2,3500,多,10"
    request = {"csv_text": csv, "currency": "CNY", "price_as_of": STAMP}
    r = await client.post("/api/preferences/portfolio/import-preview", json=request, cookies=alice)
    assert r.status_code == 200, r.text
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM portfolio_snapshots").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM portfolio_history").fetchone()[0] == 0
    assert (await client.post("/api/preferences/portfolio/import-preview", json=request)).status_code == 401
    assert (
        await client.post(
            "/api/preferences/portfolio/import-preview",
            json=request,
            cookies=alice,
            headers={"X-Prism-Account": "portfolio-bob"},
        )
    ).status_code == 409
    assert (
        await client.post(
            "/api/preferences/portfolio/import-preview",
            json=request,
            cookies=alice,
            headers={"X-Requested-With": "wrong"},
        )
    ).status_code == 403
    value = {k: v for k, v in snapshot().items() if k not in {"configured", "version"}}
    value.update(expected_version=0, positions=r.json()["positions"])
    saved = await client.put("/api/preferences/portfolio", json=value, cookies=alice)
    assert saved.status_code == 200, saved.text
    assert saved.json()["portfolio"]["positions"][0]["multiplier"] == "10"
    assert (await client.put("/api/preferences/portfolio", json=value, cookies=alice)).status_code == 409
    assert not (await client.get("/api/preferences/portfolio", cookies=bob)).json()["portfolio"]["configured"]
    with get_db() as db:
        db.execute("UPDATE users SET disabled_at=1 WHERE username='portfolio-alice'")
    assert (
        await client.post("/api/preferences/portfolio/import-preview", json=request, cookies=alice)
    ).status_code == 401


async def test_explicit_consent_shares_only_immutable_risk_summary_with_owned_new_runs(owned):
    client, identities = owned
    owner = "portfolio-alice"
    alice = identities[owner]
    p = profile()
    body = {k: v for k, v in p.items() if k not in {"configured", "version"}}
    body.update(expected_version=0, style="balanced", horizon="medium")
    assert (await client.put("/api/preferences/research", json=body, cookies=alice)).status_code == 200
    value = {k: v for k, v in snapshot().items() if k not in {"configured", "version"}}
    value["expected_version"] = 0
    assert (await client.put("/api/preferences/portfolio", json=value, cookies=alice)).status_code == 200
    first = agents.create_run(owner, owner, "合成风险问题", "synthetic", "research", str(uuid4()))
    assert "portfolio_risk" not in first["profile"]
    value.update(expected_version=1, share_with_agent=True)
    assert (await client.put("/api/preferences/portfolio", json=value, cookies=alice)).status_code == 200
    second = agents.create_run(owner, owner, "合成风险问题", "synthetic", "research", str(uuid4()))
    frozen = deepcopy(second["profile"])
    assert frozen["portfolio_risk"]["portfolio_version"] == 2
    assert "SHFE:RB2701" not in json.dumps(frozen) and "positions" not in frozen
    assert "portfolio_risk" in preferences.profile_prompt(frozen)
    other = agents.create_run(
        "portfolio-bob", "portfolio-bob", "合成问题", "synthetic", "research", str(uuid4())
    )
    assert "portfolio_risk" not in other["profile"]
    value.update(expected_version=2, share_with_agent=False, cash="2000")
    assert (await client.put("/api/preferences/portfolio", json=value, cookies=alice)).status_code == 200
    third = agents.create_run(owner, owner, "合成风险问题", "synthetic", "research", str(uuid4()))
    assert "portfolio_risk" not in third["profile"]
    assert agents.get_run(owner, second["run_id"])["profile"] == frozen
    assert "portfolio_risk" not in agents.get_run(owner, first["run_id"])["profile"]
