"""Research workspace and Native tools agree on products, dates, units and gaps."""

import json
from datetime import date
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from server import (
    exchange_futures,
    futures,
    futures_basis,
    futures_inventory,
    futures_panel,
    futures_parameters,
)
from server.agent.tools import native_catalog
from server.futures_spread import spread

FIXTURES = Path(__file__).parent / "fixtures"
REQUESTED = set(
    "AU AG CU AL NI SN AO LC SI PS JM I RB HC SS C CS CF SR SP LH AP CJ JD SA FG SH MA "
    "TA RU NR PP V UR SC PG BU A B M RM P Y OI PK EC".split()
)
DAY = date(2026, 11, 20)


def test_workspace_and_native_tools_cover_same_products():
    rows = BeautifulSoup(futures_panel.catalog_markup(), "html.parser")
    selects = BeautifulSoup(futures_panel.options_markup(), "html.parser")
    expected = REQUESTED | set("ZN LU PF IF IC T TF TS".split())
    assert set(futures.PRODUCTS) == {option["value"] for option in selects.find_all("option")} == expected
    assert {row["data-symbol"] for row in rows.select("tr[data-symbol]")} == expected
    for symbol, term in {"AU": "沪金", "TA": "PTA", "V": "PVC", "EC": "集运欧线"}.items():
        assert term in rows.select_one(f'tr[data-symbol="{symbol}"] th').get_text()
    tools = {tool["id"]: tool["input_schema"] for tool in native_catalog()}
    for tool in ("futures_snapshot", "futures_inventory", "futures_basis", "futures_spread"):
        assert set(tools[tool]["properties"]["symbol"]["enum"]) == expected
    assert all(futures._QUOTE_UNITS[symbol] for symbol in expected)


def test_continuous_series_for_every_requested_product():
    samples = json.loads((FIXTURES / "futures_daily_20261124.json").read_bytes())
    assert set(samples) == REQUESTED
    for symbol, pair in samples.items():
        bars = futures.parse_bars(json.dumps(pair).encode(), date(2026, 11, 24))
        snapshot = futures.analyze(symbol, bars, "2026-11-24T17:00:00+08:00")
        assert snapshot["series_symbol"] == symbol + "0"
        assert snapshot["latest"]["date"] == "2026-11-24"
        assert snapshot["latest"]["close"] == float(pair[-1]["c"])
    assert futures._QUOTE_UNITS["JD"] == "元/500千克"
    assert futures._QUOTE_UNITS["SH"] == "元/吨（折百）"
    assert futures._QUOTE_UNITS["EC"] == "指数点"


async def test_ec_settlement_spread_uses_index_points_and_no_physical_inventory(monkeypatch):
    reports = json.loads((FIXTURES / "ine_ec_20261120.json").read_bytes())
    bodies = {
        futures_parameters.report_url("contract_dates", DAY): reports["dates"],
        exchange_futures.report_url("EC", DAY): reports["ine"],
    }

    async def recorded(client, url, deadline):
        return json.dumps(bodies[url]).encode()

    monkeypatch.setattr(exchange_futures, "_fetch", recorded)
    monkeypatch.setattr(futures_parameters, "_fetch", recorded)
    result = await spread("EC", DAY.isoformat())
    assert result["status"] == "collected"
    assert (result["near_contract"], result["far_contract"]) == ("ec2611", "ec2709")
    assert (result["near_settlement"], result["far_settlement"]) == (2191, 1699.5)
    assert (result["far_minus_near"], result["quote_unit"]) == (-491.5, "指数点")
    assert "现金交割" in (await futures_inventory.snapshot("EC"))["reason"]


@pytest.mark.parametrize(
    "symbol,quantity", [("SN", 1412), ("AO", 192589), ("HC", 196676), ("SS", 53675), ("SP", 399114)]
)
def test_shfe_new_warrants_keep_report_segment(symbol, quantity):
    report = futures_inventory.parse_shfe(
        (FIXTURES / "shfe_warrant_segments_20261120.html").read_bytes(), DAY
    )
    segment, _, (total, _) = futures_inventory._shfe_segment(report, symbol)
    assert segment["unit"] == "吨" and total == quantity
    if symbol != "SN":
        assert segment["title"].endswith("(仓库)")


def test_czce_warrants_keep_published_zero_and_lot_units():
    report = futures_inventory.parse_czce_xlsx((FIXTURES / "czce_whsheet_20261120.xlsx").read_bytes(), DAY)
    for symbol, quantity in {"CJ": 0, "SA": 0, "SH": 0, "PK": 900, "UR": 3048}.items():
        section = report["segments"][futures_inventory.CZCE_SEGMENTS[symbol]]
        assert (section["quantity"], section["unit"]) == (quantity, "张")


async def test_new_basis_rows_agree_with_source_without_mixing_products(monkeypatch):
    body = (FIXTURES / "ppi_sf_20261120.html").read_bytes()

    async def recorded(client, url, deadline):
        return body

    monkeypatch.setattr(futures_basis, "_table_page", recorded)
    expected = {
        "SN": 13275,
        "HC": 16,
        "SS": 690,
        "SP": 232.67,
        "UR": 31.5,
        "SA": 96.33,
        "JM": 694.25,
        "PP": 1313.33,
        "V": 16,
        "A": 108,
        "Y": 114,
        "PG": 890,
    }
    for symbol, number in expected.items():
        row = await futures_basis.read_basis(None, symbol, DAY, 0)
        assert (row["status"], row["basis"]) == ("collected", number)
    for symbol in ("B", "LH", "SH", "JD", "EC"):
        result = await futures_basis.read_basis(None, symbol, DAY, 0)
        assert result["error_code"] == "symbol_not_covered" and "basis" not in result
