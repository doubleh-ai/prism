"""Official exchange warrant reports for the covered futures products.

Warrant quantities come from exchange-published reports, never from a platform
estimate. These tests pin the two real formats (SHFE HTML and legacy JSON, CZCE
xlsx and legacy GB18030 text), the explicit unavailability of uncovered products,
and the rule that two data dates never share one observation or document.
"""

import asyncio
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from server import futures_inventory as inventory
from server import public_network
from server.database import get_db
from server.futures import PRODUCTS
from server.futures_inventory import parse_czce_text, parse_czce_xlsx, parse_shfe, parse_shfe_json
from server.research import create_research_run, ingest_market_snapshot
from server.research_context import build_research_context

FIXTURES = Path(__file__).parent / "fixtures"
SHFE_DAY = date(2026, 11, 20)
SHFE_LEGACY_DAY = date(2025, 11, 17)
CZCE_DAY = date(2026, 11, 20)
CZCE_LEGACY_DAY = date(2017, 1, 3)


@pytest.fixture(autouse=True)
def _empty_cache():
    inventory._CACHE.clear()
    yield
    inventory._CACHE.clear()


def _fixture(name):
    return (FIXTURES / name).read_bytes()


def _deadline():
    return asyncio.get_running_loop().time() + 5


class _IdleClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _collected(symbol, day, quantity, change):
    name, exchange, _, _ = PRODUCTS[symbol]
    if exchange == "CZCE":
        source = "郑州商品交易所"
        url = f"https://www.czce.com.cn/cn/DFSStaticFiles/Future/{day[:4]}/{day.replace('-', '')}/FutureDataWhsheet.xlsx"
        unit, basis, segment = "张", "总计", name
    else:
        source = "上海期货交易所"
        url = f"https://www.shfe.com.cn/data/tradedata/future/stockdata/dailystock_{day.replace('-', '')}/ZH/all.html"
        unit, basis, segment = "吨", "总计", name
    return {
        "status": "collected",
        "data_as_of": day,
        "source_name": source,
        "source_url": url,
        "segment": segment,
        "basis": basis,
        "unit": unit,
        "quantity": quantity,
        "change": change,
    }


def _observation(symbol, day, quantity, change):
    read = _collected(symbol, day, quantity, change)
    name, exchange = PRODUCTS[symbol][0], PRODUCTS[symbol][1]
    return {
        "symbol": symbol,
        "name": name,
        "exchange": exchange,
        "status": "collected",
        "requested_date": day,
        "date_basis": "requested",
        "data_as_of": day,
        "unit": read["unit"],
        "quantity": quantity,
        "change": change,
        "basis": read["basis"],
        "segment": read["segment"],
        "source": {
            "name": read["source_name"],
            "url": read["source_url"],
            "kind": "exchange_warehouse_report",
        },
        "retrieved_at": "2026-11-20T09:00:00+00:00",
        "source_attempts": [],
        "gaps": [],
        "cache_ttl_seconds": 300,
        "cached": False,
    }


def _mock_client(monkeypatch):
    monkeypatch.setattr(public_network, "async_client", lambda **kwargs: _IdleClient())


def test_current_shfe_report_parses_exact_segments():
    parsed = parse_shfe(_fixture("shfe_dailystock_20261120.html"), SHFE_DAY)
    assert parsed["data_as_of"] == "2026-11-20"
    segments = parsed["segments"]
    assert segments["铜"]["totals"]["总计"] == (6053, -448)
    assert segments["铜"]["totals"]["完税商品总计"] == (6053, -448)
    assert segments["铜"]["totals"]["保税商品总计"] == (0, 0)
    assert segments["螺纹钢(仓库)"]["totals"]["总计"] == (10058, 0)
    assert segments["黄金"]["unit"] == "千克"
    assert segments["黄金"]["totals"] == {}
    assert segments["黄金"]["rows"]["上期所指定交割金库"] == (119895, -3)
    assert segments["白银"]["totals"]["总计"] == (1262431, -15095)
    assert segments["中质含硫原油"]["unit"] == "桶"
    assert segments["中质含硫原油"]["totals"]["总计"] == (3666000, 0)
    assert "铜(BC)" in segments
    assert segments["铝"]["totals"]["总计"] == (32470, -3294)
    assert segments["锌"]["totals"]["总计"] == (52060, -1948)
    assert segments["镍"]["totals"]["总计"] == (87493, -505)
    assert segments["天然橡胶"]["totals"]["总计"] == (26460, 0)
    # A disclosed zero is a collected quantity, never a missing value.
    assert segments["石油沥青(仓库)"]["totals"]["总计"] == (0, 0)
    assert segments["20号胶(仓库)"]["totals"]["总计"] == (3527, 0)
    assert segments["低硫燃料油(仓库)"]["totals"]["总计"] == (0, 0)
    assert segments["白银"]["unit"] == "千克"


@pytest.mark.parametrize(
    "before,after",
    [
        ("2026-11-20", "2026-11-19"),
        ("<td>6053</td>", "<td>6 053</td>"),
        ("增减", "变化"),
    ],
)
def test_current_shfe_report_rejects_mismatched_markup(before, after):
    raw = _fixture("shfe_dailystock_20261120.html")
    assert before.encode() in raw
    with pytest.raises(ValueError):
        parse_shfe(raw.replace(before.encode(), after.encode()), SHFE_DAY)


def test_legacy_shfe_json_parses_exact_segments():
    parsed = parse_shfe_json(_fixture("shfe_dailystock_20251117.dat"), SHFE_LEGACY_DAY)
    assert parsed["data_as_of"] == "2025-11-17"
    segments = parsed["segments"]
    assert segments["铜"]["totals"]["总计"] == (56965, 7135)
    assert segments["螺纹钢仓库"]["totals"]["总计"] == (86672, -3655)
    # The factory-delivery segment publishes only warehouse subtotals.
    assert segments["螺纹钢厂库"]["totals"] == {}
    assert segments["螺纹钢厂库"]["rows"]["鞍钢股份"] == (0, 0)
    assert segments["黄金"]["rows"] == {"上期所交割库": (90426, 0)}
    assert segments["黄金"]["totals"] == {}
    assert segments["中质含硫原油"]["totals"]["总计"] == (3464000, 0)
    assert "铜(BC)" in segments
    assert segments["铝"]["totals"]["总计"] == (69484, 4742)
    assert segments["铝"]["totals"]["完税商品总计"] == (69484, 4742)
    assert segments["锌"]["totals"]["总计"] == (76861, 5108)
    assert segments["镍"]["totals"]["总计"] == (35826, 799)
    assert segments["白银"]["unit"] == "千克"
    assert segments["白银"]["totals"]["总计"] == (569355, -7539)
    assert segments["天然橡胶"]["totals"]["总计"] == (108130, -340)
    assert segments["石油沥青仓库"]["totals"]["总计"] == (4690, 0)
    assert segments["20号胶"]["totals"]["总计"] == (49695, 0)
    assert segments["低硫燃料油仓库"]["totals"]["总计"] == (26880, -3850)


@pytest.mark.parametrize(
    "before,after",
    [
        ("20251117", "20251118"),
        ('"WRTWGHTS": 56965', '"WRTWGHTS": "56965x"'),
        ('"WGHTUNIT": "2"', '"WGHTUNIT": "9"'),
    ],
)
def test_legacy_shfe_json_rejects_mismatched_rows(before, after):
    raw = _fixture("shfe_dailystock_20251117.dat").decode("utf-8")
    assert before in raw
    with pytest.raises(ValueError):
        parse_shfe_json(raw.replace(before, after).encode(), SHFE_LEGACY_DAY)


async def test_shfe_reads_route_by_publication_era_and_keep_exact_segments():
    def serve(request):
        url = str(request.url)
        if "dailydata" in url:
            return httpx.Response(200, content=_fixture("shfe_dailystock_20251117.dat"))
        if "dailystock_20261120" in url:
            return httpx.Response(200, content=_fixture("shfe_dailystock_20261120.html"))
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        current = {
            symbol: await inventory.read_shfe(client, symbol, SHFE_DAY, _deadline())
            for symbol in ("CU", "AU", "RB", "SC")
        }
        legacy = {
            symbol: await inventory.read_shfe(client, symbol, SHFE_LEGACY_DAY, _deadline())
            for symbol in ("CU", "AU", "RB", "SC")
        }
    assert (current["CU"]["quantity"], current["CU"]["change"]) == (6053, -448)
    assert current["CU"]["segment"] == "铜"
    assert current["CU"]["source_url"].endswith("20261120/ZH/all.html")
    assert (current["AU"]["quantity"], current["AU"]["change"], current["AU"]["unit"]) == (
        119895,
        -3,
        "千克",
    )
    assert (current["RB"]["quantity"], current["RB"]["segment"]) == (10058, "螺纹钢(仓库)")
    assert current["SC"]["unit"] == "桶"
    assert all(item["status"] == "collected" for item in current.values())
    assert (legacy["CU"]["quantity"], legacy["CU"]["change"]) == (56965, 7135)
    assert legacy["CU"]["source_url"].endswith("20251117dailystock.dat")
    assert (legacy["AU"]["quantity"], legacy["AU"]["basis"]) == (90426, "上期所交割库")
    assert (legacy["RB"]["quantity"], legacy["RB"]["segment"]) == (86672, "螺纹钢仓库")
    assert all(item["status"] == "collected" for item in legacy.values())


async def test_shfe_unit_mismatch_is_unavailable_without_a_quantity():
    def serve(request):
        raw = _fixture("shfe_dailystock_20261120.html")
        return httpx.Response(200, content=raw.replace("单位：千克".encode(), "单位：吨".encode()))

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        result = await inventory.read_shfe(client, "AU", SHFE_DAY, _deadline())
    assert result["status"] == "inventory_unavailable"
    assert result["error_code"] == "invalid_source_data"
    assert "quantity" not in result and "change" not in result


def test_current_czce_xlsx_parses_warehouse_sections_only():
    parsed = parse_czce_xlsx(_fixture("czce_whsheet_20261120.xlsx"), CZCE_DAY)
    assert parsed["data_as_of"] == "2026-11-20"
    segments = parsed["segments"]
    assert len(segments) == 26
    assert (segments["SR"]["quantity"], segments["SR"]["change"], segments["SR"]["unit"]) == (
        17577,
        -325,
        "张",
    )
    assert (segments["CF"]["quantity"], segments["CF"]["change"]) == (38, -6122)
    assert (segments["MA"]["quantity"], segments["MA"]["change"]) == (0, -2698)
    assert (segments["FG"]["quantity"], segments["FG"]["change"]) == (0, -142)
    assert (segments["SF"]["quantity"], segments["SF"]["change"]) == (5568, -21)
    assert (segments["PK"]["quantity"], segments["PK"]["change"]) == (900, 0)
    assert segments["CJ"]["quantity"] == 0
    # The forecast-only AP section must never be mistaken for a warehouse section.
    assert "AP" not in segments
    assert segments["SR"]["basis"] == "总计"


def test_legacy_czce_gb18030_text_parses_sugar_section():
    raw = _fixture("czce_whsheet_20170103.txt")
    assert not raw.startswith(b"\xef\xbb\xbf")
    parsed = parse_czce_text(raw, CZCE_LEGACY_DAY)
    assert list(parsed["segments"]) == ["SR", "TA"]
    sugar = parsed["segments"]["SR"]
    assert (sugar["quantity"], sugar["change"], sugar["unit"]) == (32626, 6288, "张")
    with pytest.raises(ValueError):
        parse_czce_text(raw, date(2017, 1, 2))


async def test_czce_reads_use_the_published_era_and_report_absence():
    def serve(request):
        url = str(request.url)
        if url.endswith("FutureDataWhsheet.xlsx"):
            return httpx.Response(200, content=_fixture("czce_whsheet_20261120.xlsx"))
        if url.endswith("FutureDataWhsheet.txt"):
            return httpx.Response(200, content=_fixture("czce_whsheet_20170103.txt"))
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        current = await inventory.read_czce(client, "SR", CZCE_DAY, _deadline())
        legacy = await inventory.read_czce(client, "SR", CZCE_LEGACY_DAY, _deadline())
    assert (current["quantity"], current["change"]) == (17577, -325)
    assert current["source_url"].endswith("FutureDataWhsheet.xlsx")
    assert (legacy["quantity"], legacy["change"]) == (32626, 6288)
    assert legacy["source_url"].endswith("FutureDataWhsheet.txt")


async def test_czce_non_trading_day_page_is_report_not_found():
    def serve(request):
        return httpx.Response(200, text="<html><body>当日无数据</body></html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        result = await inventory.read_czce(client, "SR", CZCE_DAY, _deadline())
    assert result["status"] == "inventory_unavailable"
    assert result["error_code"] == "report_not_found"


async def test_uncovered_products_never_fetch_and_never_estimate(monkeypatch):
    monkeypatch.setattr(
        public_network, "async_client", lambda **kwargs: pytest.fail("no network for uncovered products")
    )
    for symbol in ("IF", "IC", "T", "TF", "TS", "AP", "SI", "LC", "PS"):
        value = await inventory.snapshot(symbol)
        assert value["status"] == "inventory_unavailable"
        assert value["symbol"] == symbol
        assert "quantity" not in value and "change" not in value
        assert value["gaps"]
    assert "中国金融期货交易所" in (await inventory.snapshot("IF"))["reason"]
    assert "广州期货交易所" in (await inventory.snapshot("SI", "2026-11-20"))["reason"]
    assert "苹果预报仓单" in (await inventory.snapshot("AP", "2026-11-20"))["reason"]
    assert "国债期货不设交易所仓单库存" in (await inventory.snapshot("TS"))["reason"]


@pytest.mark.parametrize("observed", ["2026/11/20", "20261120", "2026-13-01"])
async def test_invalid_requested_date_is_rejected(observed):
    with pytest.raises(ValueError):
        await inventory.snapshot("RB", observed)


async def test_snapshot_keeps_dates_isolated_and_caches_each_request(monkeypatch):
    _mock_client(monkeypatch)
    calls = []

    async def read(client, symbol, day, deadline):
        calls.append((symbol, day.isoformat()))
        return _collected(symbol, day.isoformat(), 100 if day.day % 2 else 200, -1)

    monkeypatch.setattr(inventory, "read_inventory", read)
    first = await inventory.snapshot("RB", "2026-11-19")
    second = await inventory.snapshot("RB", "2026-11-20")
    assert (first["quantity"], second["quantity"]) == (100, 200)
    assert (first["data_as_of"], second["data_as_of"]) == ("2026-11-19", "2026-11-20")
    assert (first["requested_date"], second["requested_date"]) == ("2026-11-19", "2026-11-20")
    again = await inventory.snapshot("RB", "2026-11-19")
    assert again["cached"] is True
    assert again["quantity"] == 100 and again["data_as_of"] == "2026-11-19"
    assert calls == [("RB", "2026-11-19"), ("RB", "2026-11-20")]


async def test_snapshot_without_date_uses_a_bounded_calendar_window(monkeypatch):
    _mock_client(monkeypatch)
    seen = []
    monkeypatch.setattr(inventory, "_today", lambda: date(2026, 11, 22))

    async def read(client, symbol, day, deadline):
        seen.append(day.isoformat())
        if day != date(2026, 11, 20):
            return {
                "status": "inventory_unavailable",
                "requested_date": day.isoformat(),
                "source_url": "https://www.czce.com.cn/cn/DFSStaticFiles/Future/2026/",
                "error_code": "report_not_found",
            }
        return _collected("SR", day.isoformat(), 17577, -325)

    monkeypatch.setattr(inventory, "read_inventory", read)
    value = await inventory.snapshot("SR")
    assert seen == ["2026-11-22", "2026-11-21", "2026-11-20"]
    assert value["status"] == "collected"
    assert value["data_as_of"] == "2026-11-20"
    assert value["requested_date"] is None and value["date_basis"] == "bounded_calendar_window"
    assert [attempt["status"] for attempt in value["source_attempts"]] == [
        "inventory_unavailable",
        "inventory_unavailable",
        "collected",
    ]


async def test_requested_date_never_falls_back(monkeypatch):
    _mock_client(monkeypatch)
    seen = []

    async def read(client, symbol, day, deadline):
        seen.append(day.isoformat())
        return {
            "status": "inventory_unavailable",
            "requested_date": day.isoformat(),
            "source_url": "https://www.czce.com.cn/cn/DFSStaticFiles/Future/2026/",
            "error_code": "report_not_found",
        }

    monkeypatch.setattr(inventory, "read_inventory", read)
    value = await inventory.snapshot("FG", "2026-11-21")
    assert seen == ["2026-11-21"]
    assert value["status"] == "inventory_unavailable"
    assert value["error_code"] == "report_not_found"
    assert value["date_basis"] == "requested"
    assert "quantity" not in value and "change" not in value


async def test_non_missing_failure_stops_the_bounded_window(monkeypatch):
    _mock_client(monkeypatch)
    seen = []
    monkeypatch.setattr(inventory, "_today", lambda: date(2026, 11, 22))

    async def read(client, symbol, day, deadline):
        seen.append(day.isoformat())
        return {
            "status": "inventory_unavailable",
            "requested_date": day.isoformat(),
            "source_url": "https://www.shfe.com.cn/data/",
            "error_code": "access_denied",
        }

    monkeypatch.setattr(inventory, "read_inventory", read)
    value = await inventory.snapshot("CU")
    assert seen == ["2026-11-22"]
    assert value["status"] == "inventory_unavailable"
    assert value["error_code"] == "access_denied"
    assert len(value["source_attempts"]) == 1


def test_document_requires_a_collected_observation_and_renders_official_numbers():
    document = inventory.snapshot_document(_observation("RB", "2026-11-20", 6053, -448))
    assert document is not None
    assert document["symbol"] == "RB" and document["exchange"] == "SHFE"
    assert document["data_period"] == "2026-11-20"
    assert document["title"] == "螺纹钢 RB 仓单日报 2026-11-20"
    assert document["source"]["kind"] == "exchange_warehouse_report"
    assert document["sections"][0]["section"] == "warehouse_receipt"
    assert "总计" in document["sections"][0]["label"]
    text = document["pages"][0]["text"]
    assert "仓单数量 6053 吨" in text
    assert "仓单增减 -448 吨" in text
    assert "数据日期：2026-11-20" in text
    assert inventory.snapshot_document({"status": "inventory_unavailable"}) is None
    assert inventory.snapshot_document({"status": "collected"}) is None
    assert (
        inventory.snapshot_document(_observation("RB", "2026-11-20", 6053, -448) | {"data_as_of": "bad"})
        is None
    )


def _evidence_run(run_id="inventory-run"):
    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO users(username,password_hash) VALUES(?,?)",
            ["alice", "synthetic-q3-user"],
        )
    context = build_research_context(run_id=run_id, now=datetime(2026, 11, 21, 18, tzinfo=UTC))
    create_research_run("alice", "", "研究合成仓单与库存数据", context)
    return run_id


def test_inventory_document_is_original_evidence_and_dates_stay_separate():
    run_id = _evidence_run()
    call = {"type": "tool_use", "id": "call-current", "name": "futures_inventory", "input": {"symbol": "SR"}}
    stored = ingest_market_snapshot(
        "alice",
        run_id,
        call,
        inventory.snapshot_document(_observation("SR", "2026-11-20", 17577, -325)),
        access_method="futures_inventory",
    )
    document = stored["document"]
    assert document["source_kind"] == "market_data_snapshot"
    assert document["original_obtained"] is True
    assert document["access_method"] == "futures_inventory"
    assert document["document_number"] == "SR-2026-11-20"
    assert document["data_period"] == "2026-11-20"
    assert len(stored["passages"]) == 1
    replay = ingest_market_snapshot(
        "alice",
        run_id,
        call,
        inventory.snapshot_document(_observation("SR", "2026-11-20", 17577, -325)),
        access_method="futures_inventory",
    )
    assert replay["document"]["document_id"] == document["document_id"]
    later_call = {
        "type": "tool_use",
        "id": "call-later",
        "name": "futures_inventory",
        "input": {"symbol": "SR", "date": "2026-11-19"},
    }
    later = ingest_market_snapshot(
        "alice",
        run_id,
        later_call,
        inventory.snapshot_document(_observation("SR", "2026-11-19", 17390, 120)),
        access_method="futures_inventory",
    )["document"]
    assert later["document_id"] != document["document_id"]
    assert later["document_number"] == "SR-2026-11-19"
    assert later["data_period"] == "2026-11-19"


def test_inventory_capability_is_a_read_only_futures_tool():
    from server.agent.tools import PLATFORM_TOOLS, native_catalog
    from server.capabilities import capability_catalog

    rows = [row for row in capability_catalog() if row["id"] == "futures_inventory"]
    assert len(rows) == 1
    assert rows[0]["read_only"] is True
    assert rows[0]["view"] == "futures" and rows[0]["target_view"] == "futures"
    assert "futures_inventory" in PLATFORM_TOOLS
    assert any(row["id"] == "futures_inventory" for row in native_catalog())


async def test_dce_products_read_public_series_with_lot_unit(monkeypatch):
    from server import dce_alt

    dce_alt._TOKEN.update(value="", expires=0.0)
    seen = []

    def serve(request):
        seen.append(request)
        if request.url.path == "/app/common/v.js":
            return httpx.Response(
                200, headers={"_pcc": "_pcc_" + "a" * 170, "_pcc_expire": "1797953247303"}, content=b""
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "成功",
                "data": {"total": 2, "list": [["2026-11-20", "3071", 45559], ["2026-11-19", "3119", 46193]]},
            },
        )

    monkeypatch.setattr(
        public_network,
        "async_client",
        lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(serve)),
    )
    value = await inventory.snapshot("M", "2026-11-20")
    assert value["status"] == "collected" and value["unit"] == "手" and value["quantity"] == 45559
    assert value["change"] == -634 and value["change_basis"] == "delta_vs_previous_disclosed_row"
    assert value["source"]["name"] == "99期货"
    assert value["source"]["kind"] == "secondary_warehouse_series"
    assert value["source"]["url"].startswith(dce_alt.SERIES_URL)
    document = inventory.snapshot_document(value)
    assert document is not None
    assert document["sections"][0]["label"] == "仓单库存（99期货公开序列）"
    text = document["pages"][0]["text"]
    assert "仓单增减（较上一披露日） -634 手" in text
    assert "未换算吨数" in text
    assert sum(request.url.path == "/app/common/v.js" for request in seen) == 1


def test_current_czce_xlsx_covers_added_oilseed_and_petrochemical_sections():
    parsed = parse_czce_xlsx(_fixture("czce_whsheet_20261120.xlsx"), CZCE_DAY)
    segments = parsed["segments"]
    assert (segments["PTA"]["quantity"], segments["PTA"]["change"], segments["PTA"]["unit"]) == (
        3005,
        0,
        "张",
    )
    assert segments["PTA"]["name"] == "PTA"
    assert (segments["RM"]["quantity"], segments["RM"]["change"]) == (0, -1742)
    assert (segments["OI"]["quantity"], segments["OI"]["change"]) == (0, -236)
    assert (segments["PF"]["quantity"], segments["PF"]["change"]) == (1520, 0)


def test_legacy_czce_text_keys_pta_by_trading_code():
    parsed = parse_czce_text(_fixture("czce_whsheet_20170103.txt"), CZCE_LEGACY_DAY)
    pta = parsed["segments"]["TA"]
    assert (pta["quantity"], pta["change"], pta["unit"]) == (204178, 8344, "张")
    assert pta["basis"] == "总计"
    assert "PTA" not in parsed["segments"]


async def test_czce_reads_resolve_legacy_pta_code_but_current_printed_name():
    def serve(request):
        url = str(request.url)
        if url.endswith("FutureDataWhsheet.xlsx"):
            return httpx.Response(200, content=_fixture("czce_whsheet_20261120.xlsx"))
        if url.endswith("FutureDataWhsheet.txt"):
            return httpx.Response(200, content=_fixture("czce_whsheet_20170103.txt"))
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        current = await inventory.read_czce(client, "TA", CZCE_DAY, _deadline())
        legacy = await inventory.read_czce(client, "TA", CZCE_LEGACY_DAY, _deadline())
    assert (current["status"], current["quantity"], current["segment"]) == ("collected", 3005, "PTA")
    assert (legacy["status"], legacy["quantity"], legacy["segment"]) == ("collected", 204178, "TA")


async def test_shfe_reads_cover_added_metal_and_rubber_segments():
    def serve(request):
        assert "dailystock_20261120" in str(request.url)
        return httpx.Response(200, content=_fixture("shfe_dailystock_20261120.html"))

    expected = {
        "AL": ("吨", 32470, -3294),
        "ZN": ("吨", 52060, -1948),
        "NI": ("吨", 87493, -505),
        "AG": ("千克", 1262431, -15095),
        "RU": ("吨", 26460, 0),
        "BU": ("吨", 0, 0),
        "NR": ("吨", 3527, 0),
        "LU": ("吨", 0, 0),
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        for symbol, (unit, quantity, change) in expected.items():
            value = await inventory.read_shfe(client, symbol, SHFE_DAY, _deadline())
            assert value["status"] == "collected", symbol
            assert (value["unit"], value["quantity"], value["change"]) == (unit, quantity, change)


async def test_legacy_shfe_json_resolves_the_documented_segment_aliases():
    def serve(request):
        assert "dailydata" in str(request.url)
        return httpx.Response(200, content=_fixture("shfe_dailystock_20251117.dat"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        values = {
            symbol: await inventory.read_shfe(client, symbol, SHFE_LEGACY_DAY, _deadline())
            for symbol in ("AL", "ZN", "NI", "AG", "RU", "BU", "NR", "LU")
        }
    assert [value["status"] for value in values.values()] == ["collected"] * 8
    assert values["AG"]["unit"] == "千克"
    # The legacy sheet names the asphalt and low-sulphur sections with a 仓库 suffix.
    assert values["BU"]["segment"] == "石油沥青仓库"
    assert values["LU"]["segment"] == "低硫燃料油仓库"
    assert values["NR"]["segment"] == "20号胶"


def test_warrant_registries_cover_exactly_the_public_catalog():
    assert set(inventory.SHFE_SEGMENTS) == set(inventory.SHFE_UNITS) == set(inventory.SHFE_JSON_SEGMENTS)
    assert set(inventory.CZCE_SEGMENTS) <= set(PRODUCTS)
    assert set(inventory.COVERED).isdisjoint(inventory.UNCOVERED_REASONS)
    assert set(inventory.COVERED) | set(inventory.UNCOVERED_REASONS) == set(PRODUCTS)
