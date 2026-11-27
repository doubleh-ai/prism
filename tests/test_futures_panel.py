"""Read-only futures panel: static coverage plus four independently read observations."""

from datetime import date

import pytest
from fastapi import HTTPException

from server import (
    futures,
    futures_basis,
    futures_inventory,
    futures_panel,
    futures_parameters,
    futures_spread,
)

SINA = {"name": "新浪财经", "url": "https://finance.sina.com.cn/futures/quotes/CU0.shtml"}
SHFE_DAILY = {"name": "交易所公开日报", "url": "https://www.shfe.com.cn/data/dailydata/kx/kx20261120.dat"}
SHFE_WARRANT = {
    "name": "上海期货交易所",
    "url": "https://www.shfe.com.cn/data/dailydata/ck/20261120dailyStock.dat",
}
SCI = {"name": "生意社现期表", "url": "https://www.100ppi.com/basis/"}


def snapshot_value(change=0.53):
    return {
        "status": "collected",
        "series_status": "collected",
        "data_as_of": "2026-11-21",
        "latest": {"close": 70120},
        "bars": [{"date": "2026-11-20", "close": 70000}, {"date": "2026-11-21", "close": 70120}],
        "technical": {"change_pct": change},
        "positioning": {"volume": 120000, "open_interest": 251000},
        "source": SINA,
        "gaps": [],
        "name": "铜",
        "exchange": "SHFE",
    }


def spread_value():
    return {
        "status": "collected",
        "data_as_of": "2026-11-20",
        "near_contract": "cu2612",
        "near_delivery_month": "2026-12",
        "near_settlement": 70000,
        "far_contract": "cu2701",
        "far_delivery_month": "2027-01",
        "far_settlement": 70200,
        "far_minus_near": 200,
        "shape": "contango",
        "quote_unit": "元/吨",
        "contract_months": {
            "months": [
                {
                    "contract": "cu2612",
                    "delivery_month": "2026-12",
                    "settlement": 70000,
                    "volume": 100,
                    "open_interest": 80,
                },
                {
                    "contract": "cu2701",
                    "delivery_month": "2027-01",
                    "settlement": 70200,
                    "volume": 50,
                    "open_interest": 40,
                },
            ]
        },
        "source": SHFE_DAILY,
    }


def inventory_value(change=0):
    return {
        "status": "collected",
        "data_as_of": "2026-11-20",
        "quantity": 17000,
        "change": change,
        "unit": "吨",
        "segment": "仓库仓单",
        "source": SHFE_WARRANT,
        "gaps": [],
    }


def basis_value():
    return {
        "status": "collected",
        "data_as_of": "2026-11-19",
        "spot_price": 70500,
        "futures_contract": "cu2612",
        "futures_price": 70000,
        "basis": 500,
        "basis_direction": "spot_premium",
        "source_published_basis": 480,
        "quote_unit": "元/吨",
        "source": SCI,
        "gaps": [],
    }


def install_collected(monkeypatch, *, change=0):
    async def read_snapshot(symbol):
        return snapshot_value()

    async def read_spread(symbol, trading_day):
        assert trading_day == "2026-11-20"
        return spread_value()

    async def read_inventory(symbol, observed=None):
        return inventory_value(change)

    async def read_basis(symbol, observed=None):
        return basis_value()

    monkeypatch.setattr(futures, "snapshot", read_snapshot)
    monkeypatch.setattr(futures_spread, "spread", read_spread)
    monkeypatch.setattr(futures_inventory, "snapshot", read_inventory)
    monkeypatch.setattr(futures_basis, "snapshot", read_basis)


def test_catalog_reports_each_dimension_coverage_with_reason_codes():
    catalog = futures_panel.catalog()
    assert catalog["count"] == 54
    assert [dimension["key"] for dimension in catalog["dimensions"]] == [
        "snapshot",
        "spread",
        "inventory",
        "basis",
    ]
    by_symbol = {product["symbol"]: product for product in catalog["products"]}
    assert set(by_symbol) == set(futures.PRODUCTS)

    spread_available = {
        symbol
        for symbol, product in by_symbol.items()
        if product["dimensions"]["spread"]["status"] == "available"
    }
    inventory_available = {
        symbol
        for symbol, product in by_symbol.items()
        if product["dimensions"]["inventory"]["status"] == "available"
    }
    basis_available = {
        symbol
        for symbol, product in by_symbol.items()
        if product["dimensions"]["basis"]["status"] == "available"
    }
    assert spread_available == set(futures_parameters.PRODUCTS)
    assert inventory_available == set(futures_inventory.COVERED)
    assert basis_available == set(futures_basis.BASIS_PRODUCTS)
    assert all(product["dimensions"]["snapshot"]["status"] == "available" for product in catalog["products"])

    copper = by_symbol["CU"]["dimensions"]
    assert copper["snapshot"]["reason_code"] is None
    assert copper["spread"]["reason_code"] is None
    assert copper["inventory"]["reason_code"] is None
    assert copper["basis"]["reason_code"] is None

    index = by_symbol["IF"]["dimensions"]
    assert index["snapshot"]["status"] == "available"
    assert index["spread"]["reason_code"] == "spread_unavailable:contract_months_not_covered"
    assert index["inventory"]["reason_code"] == "inventory_unavailable"
    assert index["basis"]["reason_code"] == "basis_unavailable:symbol_not_covered"
    assert index["spread"]["reason"] and "合约" in index["spread"]["reason"]

    crude = by_symbol["SC"]["dimensions"]
    assert crude["spread"]["status"] == "available"
    assert crude["basis"]["reason_code"] == "basis_unavailable:symbol_not_covered"


def test_catalog_markup_renders_visible_availability_without_a_client_fetch():
    markup = futures_panel.catalog_markup()
    assert markup.count('<tr data-symbol="') == 54
    for symbol in ("CU", "IF", "SC"):
        assert f'data-symbol="{symbol}"' in markup
    assert "spread_unavailable:contract_months_not_covered" in markup
    assert "inventory_unavailable" in markup
    assert "basis_unavailable:symbol_not_covered" in markup
    assert "可用" in markup
    assert "__FUTURES_PANEL_CATALOG__" not in markup
    assert "<script" not in markup.lower()


async def test_panel_normalizes_all_four_observations(monkeypatch):
    install_collected(monkeypatch, change=0)
    value = await futures_panel.panel("CU", "2026-11-20")

    assert value["symbol"] == "CU"
    assert value["name"] == "铜"
    assert value["trading_day"] == "2026-11-20"
    dimensions = value["dimensions"]
    assert set(dimensions) == {"snapshot", "spread", "inventory", "basis"}
    for dimension in dimensions.values():
        assert dimension["available"] is True
        assert dimension["data_as_of"]
        assert dimension["source"]["name"]
        assert dimension["label"]

    assert dimensions["snapshot"]["values"]["latest_close"] == 70120
    assert dimensions["snapshot"]["values"]["closes"] == snapshot_value()["bars"]
    assert dimensions["snapshot"]["source"] == SINA
    assert dimensions["spread"]["values"]["contract_months"][0]["contract"] == "cu2612"
    assert dimensions["spread"]["values"]["shape_label"] == "远月升水（远月结算价高于近月）"
    assert dimensions["inventory"]["values"]["quantity"] == 17000
    # A source-reported zero daily change stays a zero; it is not a missing value.
    assert dimensions["inventory"]["values"]["change"] == 0
    assert dimensions["basis"]["values"]["basis"] == 500
    assert dimensions["basis"]["values"]["basis_label"] == "现货升水（基差为正）"


async def test_panel_unavailable_dimension_has_reason_code_and_no_values(monkeypatch):
    install_collected(monkeypatch)

    async def unavailable_spread(symbol, trading_day):
        return {
            "status": "spread_unavailable",
            "reason_code": "contract_months_unavailable",
            "reason": "合约日历暂未取得。",
        }

    async def unavailable_inventory(symbol, observed=None):
        return {"status": "inventory_unavailable", "reason": "该品种未接入交易所仓单数据源；不估算、不补零。"}

    async def unavailable_basis(symbol, observed=None):
        return {
            "status": "basis_unavailable",
            "error_code": "challenge_unsolved",
            "reason": "未能通过生意社 HW_CHECK 安全检查，现期表暂未取得。",
        }

    monkeypatch.setattr(futures_spread, "spread", unavailable_spread)
    monkeypatch.setattr(futures_inventory, "snapshot", unavailable_inventory)
    monkeypatch.setattr(futures_basis, "snapshot", unavailable_basis)

    value = await futures_panel.panel("SC", "2026-11-20")
    dimensions = value["dimensions"]
    assert dimensions["snapshot"]["available"] is True
    assert dimensions["snapshot"]["values"]["latest_close"] == 70120
    expected = {
        "spread": "spread_unavailable:contract_months_unavailable",
        "inventory": "inventory_unavailable",
        "basis": "basis_unavailable:challenge_unsolved",
    }
    for key, code in expected.items():
        dimension = dimensions[key]
        assert dimension["available"] is False
        assert dimension["status"] == "unavailable"
        assert dimension["reason_code"] == code
        assert dimension["reason"]
        assert dimension["values"] is None
        assert dimension["data_as_of"] is None
        assert dimension["source"] is None


async def test_panel_keeps_other_dimensions_when_one_source_fails(monkeypatch):
    install_collected(monkeypatch)

    async def failing_snapshot(symbol):
        raise HTTPException(503, "行情来源暂不可用")

    monkeypatch.setattr(futures, "snapshot", failing_snapshot)

    value = await futures_panel.panel("CU", "2026-11-20")
    dimensions = value["dimensions"]
    assert dimensions["snapshot"]["available"] is False
    assert dimensions["snapshot"]["reason_code"] == "snapshot_unavailable"
    assert dimensions["snapshot"]["reason"] == "行情来源暂不可用"
    assert dimensions["snapshot"]["values"] is None
    for key in ("spread", "inventory", "basis"):
        assert dimensions[key]["available"] is True
        assert dimensions[key]["values"]


async def test_panel_rejects_unknown_symbol_and_malformed_date():
    with pytest.raises(HTTPException) as unknown:
        await futures_panel.panel("XX", "2026-11-20")
    assert unknown.value.status_code == 422
    with pytest.raises(HTTPException) as malformed:
        await futures_panel.panel("CU", "2026-13-40")
    assert malformed.value.status_code == 422


async def test_panel_endpoint_reads_and_reports_each_dimension(client, auth_cookies, monkeypatch):
    install_collected(monkeypatch, change=-250)
    response = await client.get("/api/futures/panel/CU?date=2026-11-20", cookies=auth_cookies)
    assert response.status_code == 200
    value = response.json()
    assert value["symbol"] == "CU"
    assert value["dimensions"]["inventory"]["values"]["change"] == -250
    assert value["dimensions"]["basis"]["values"]["spot_price"] == 70500
    assert value["dimensions"]["spread"]["values"]["contract_months"][1]["contract"] == "cu2701"
    assert value["dimensions"]["snapshot"]["source"]["name"] == "新浪财经"
    assert value["notice"]

    unknown = await client.get("/api/futures/panel/XX", cookies=auth_cookies)
    assert unknown.status_code == 422
    malformed = await client.get("/api/futures/panel/CU?date=2026-13-40", cookies=auth_cookies)
    assert malformed.status_code == 422
    assert malformed.json()["detail"] == "日期参数应为 YYYY-MM-DD"


async def test_legacy_panel_catalog_stays_available(client, auth_cookies):
    response = await client.get("/api/futures/panel/catalog", cookies=auth_cookies)
    assert response.status_code == 200
    catalog = response.json()
    assert catalog["count"] == 54
    assert {dimension["key"] for dimension in catalog["dimensions"]} == {
        "snapshot",
        "spread",
        "inventory",
        "basis",
    }


def test_default_trading_day_stays_on_a_weekday():
    # The default spread trading day must never fall on a weekend.
    assert futures_panel._latest_weekday().weekday() < 5
    assert date.fromisoformat(futures_panel._trading_day(None)).weekday() < 5
