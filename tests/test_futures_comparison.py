from datetime import date
from pathlib import Path

import pytest

from server import futures_basis, futures_comparison, futures_inventory, futures_spread

START, END = date(2026, 11, 19), date(2026, 11, 20)
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sources(monkeypatch):
    async def table(_client, url, _deadline):
        stamp = Path(url).stem.removeprefix("day-").replace("-", "")
        return (FIXTURES / f"ppi_sf_{stamp}.html").read_bytes()

    monkeypatch.setattr(futures_basis, "_CACHE", {})
    monkeypatch.setattr(futures_basis, "_table_page", table)

    def curve(day):
        prices = (
            {"2612": 3010, "2701": 3000, "2705": 3050}
            if day == START.isoformat()
            else {"2701": 3020, "2705": 3045, "2710": 3100}
        )
        return {
            "status": "collected",
            "symbol": "RB",
            "data_as_of": day,
            "quote_unit": "元/吨",
            "source": {"name": "合成结算样本", "url": "https://example.com/settlements"},
            "settlement_report": {"data_as_of": day},
            "contract_months": {
                "data_as_of": day,
                "months": [
                    {
                        "contract": "rb" + code,
                        "delivery_month": f"20{code[:2]}-{code[2:]}",
                        "settlement": value,
                    }
                    for code, value in prices.items()
                ],
            },
        }

    data = {d.isoformat(): curve(d.isoformat()) for d in (START, END)}

    async def spread(_symbol, day):
        return data[day]

    async def inventory(symbol, day):
        return {
            "status": "collected",
            "symbol": symbol,
            "data_as_of": day,
            "unit": "吨",
            "quantity": 810 if day == START.isoformat() else 800,
            "segment": "螺纹钢(仓库)",
            "basis": "总计",
            "source": {"name": "合成仓单样本", "url": "https://example.com/inventory"},
        }

    monkeypatch.setattr(futures_inventory, "snapshot", inventory)
    monkeypatch.setattr(futures_spread, "spread", spread)
    return data


async def test_saved_spot_rows_and_identical_settlement_pair(client, auth_cookies, sources):
    url = f"/api/futures/compare/RB?start={START}&end={END}"
    assert (await client.get(url)).status_code == 401
    response = await client.get(url, cookies=auth_cookies)
    assert response.status_code == 200
    value = response.json()
    assert [r["spot"] for r in value["basis"]] == [3075, 3108.5]
    assert [r["main"] for r in value["basis"]] == [2983, 2999]
    assert [r["basis"] for r in value["basis"]] == [92, 109.5]
    assert [r["contract"] for r in value["basis"]] == ["RB2701", "RB2701"]
    assert value["changes"] == {"spot": 33.5, "main": 16, "basis": 17.5, "rolled": False}
    assert value["pair"] == ["RB2701", "RB2705"]
    assert [r["spread"] for r in value["curves"]] == [50, 25]
    assert value["spread_change"] == -25


async def test_rollover_and_insufficient_common_contracts(monkeypatch, sources):
    original = futures_basis.snapshot

    async def rolled(symbol, day):
        row = await original(symbol, day)
        return {**row, "futures_contract": "rb2705"} if day == END.isoformat() else row

    monkeypatch.setattr(futures_basis, "snapshot", rolled)
    sources[END.isoformat()]["contract_months"]["months"][0]["settlement"] = None
    value = await futures_comparison.compare("RB", START, END)
    assert value["changes"]["rolled"] is True
    assert [row["contract"] for row in value["basis"]] == ["RB2701", "RB2705"]
    assert value["pair"] is value["spread_change"] is None
    assert "不足两个" in value["spread_gap"]


@pytest.mark.parametrize("bad", ["unit", "date", "timeout", "missing_source"])
async def test_source_gaps_do_not_hide_other_observations(monkeypatch, sources, bad):
    original = futures_basis.snapshot

    async def partial(symbol, day):
        if day == END.isoformat():
            if bad == "timeout":
                raise TimeoutError()
            row = await original(symbol, day)
            if bad == "missing_source":
                return {**row, "source": None}
            if bad == "unit":
                return {**row, "quote_unit": "元/千克"}
            return {**row, "data_as_of": START.isoformat()}
        return await original(symbol, day)

    monkeypatch.setattr(futures_basis, "snapshot", partial)
    value = await futures_comparison.compare("RB", START, END)
    assert value["basis"][1]["reason"] and "basis" not in value["basis"][1]
    assert value["basis"][0]["basis"] == 92 and value["changes"] is None
    assert value["spread_change"] == -25


async def test_mismatched_settlement_date(sources):
    sources[END.isoformat()]["settlement_report"]["data_as_of"] = START.isoformat()
    value = await futures_comparison.compare("RB", START, END)
    assert value["curves"][1]["reason"] == "挂牌与结算资料日期不一致"
    assert value["spread_change"] is None and value["changes"]["basis"] == 17.5


@pytest.mark.parametrize(
    ("symbol", "start", "end"),
    [("CU", START, END), ("RB", END, START), ("RB", START, START), ("RB", START, "2100-01-01")],
)
async def test_invalid_comparisons(client, auth_cookies, symbol, start, end):
    result = await client.get(f"/api/futures/compare/{symbol}?start={start}&end={end}", cookies=auth_cookies)
    assert result.status_code == 422


async def test_physical_hypothesis_requires_independent_inventory_evidence(sources, monkeypatch):
    value = await futures_comparison.compare("RB", START, END)
    assert value["physical_assessment"]["state"] == "tightening_consistent"
    assert value["physical_assessment"]["warrant_change_tonnes"] == -10
    original = futures_inventory.snapshot

    async def changed_scope(symbol, day):
        row = await original(symbol, day)
        return {**row, "segment": "厂库"} if day == END.isoformat() else row

    monkeypatch.setattr(futures_inventory, "snapshot", changed_scope)
    result = await futures_comparison.compare("RB", START, END)
    assert result["physical_assessment"]["state"] == "insufficient"
    assert result["physical_assessment"]["warrant_change_tonnes"] is None
    assert result["changes"]["basis"] == 17.5


def test_falling_futures_can_widen_basis_without_stronger_spot():
    warrants = [{"quantity": n, "segment": "螺纹钢(仓库)", "basis": "总计"} for n in (5030, 5000)]
    changes = {"rolled": False, "basis": 15, "spot": -5, "main": -20}
    view = futures_comparison._physical_assessment(changes, -12, warrants)
    assert view["state"] == "mixed" and "期价跌得更多" in view["assessment"]
    assert (
        futures_comparison._physical_assessment({**changes, "rolled": True}, -12, warrants)["state"]
        == "insufficient"
    )
