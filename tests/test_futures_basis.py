"""Daily spot-versus-futures basis from the 100ppi public table.

Spot and main-contract futures prices come from the source table, never from a
platform estimate. These tests pin the real table format, the ``HW_CHECK``
check-page flow and its explicit failure, the rule that two data dates never
share one observation or document, and the unavailable paths for products the
table does not list.
"""

import asyncio
import re
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from server import futures_basis as basis
from server import public_network
from server.database import get_db
from server.futures import PRODUCTS
from server.futures_basis import page_url, parse_basis_page
from server.research import create_research_run, ingest_market_snapshot
from server.research_context import build_research_context

FIXTURES = Path(__file__).parent / "fixtures"
DAY = date(2026, 11, 20)
EARLIER_DAY = date(2026, 11, 19)
EMPTY_DAY = date(2026, 11, 21)


@pytest.fixture(autouse=True)
def _empty_cache():
    basis._CACHE.clear()
    yield
    basis._CACHE.clear()


def _fixture(name):
    return (FIXTURES / name).read_bytes()


def _deadline():
    return asyncio.get_running_loop().time() + 5


def _token():
    matched = re.search(rb'var\s+_0x[0-9a-zA-Z]+\s*=\s*"([0-9a-fA-F]+)"', _fixture("ppi_sf_challenge.html"))
    assert matched is not None
    return matched.group(1).decode()


def _empty_page(day):
    text = _fixture("ppi_sf_20261121_empty.html").decode("utf-8")
    return text.replace("2026年11月21日", f"{day.year}年{day.month}月{day.day}日").encode("utf-8")


def _client_factory(handler):
    def factory(**kwargs):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            trust_env=False,
        )

    return factory


def _serve(page_20, page_19):
    def serve(request):
        url = str(request.url)
        if "day-2026-11-20" in url:
            if "HW_CHECK=" in (request.headers.get("cookie") or ""):
                return httpx.Response(200, content=page_20)
            return httpx.Response(200, content=_fixture("ppi_sf_challenge.html"))
        if "day-2026-11-19" in url:
            return httpx.Response(200, content=page_19)
        matched = re.search(r"day-(\d{4})-(\d{2})-(\d{2})", url)
        return httpx.Response(200, content=_empty_page(date(*map(int, matched.groups()))))

    return serve


def _collected(symbol="CU", day="2026-11-20", **overrides):
    spot, contract, price, published = {
        "CU": (101616.67, "2701", 92400.0, 9216.0),
        "AU": (801.77, "2702", 804.32, -2.55),
        "MA": (3695.0, "2701", 3073.0, 622.0),
    }[symbol]
    direction = "spot_premium" if spot - price > 0 else "futures_premium"
    value = {
        "symbol": symbol,
        "name": PRODUCTS[symbol][0],
        "exchange": PRODUCTS[symbol][1],
        "status": "collected",
        "requested_date": day,
        "date_basis": "requested",
        "data_as_of": day,
        "spot_price": spot,
        "futures_contract": contract,
        "futures_price": price,
        "basis": spot - price,
        "basis_direction": direction,
        "source_published_basis": published,
        "quote_unit": basis.futures._QUOTE_UNITS.get(symbol, "元/吨"),
        "source": {
            "name": basis.SOURCE_NAME,
            "url": page_url(date.fromisoformat(day)),
            "kind": basis.SOURCE_KIND,
        },
        "collected_at": "2026-11-20T10:00:00+00:00",
        "source_attempts": [],
        "gaps": [],
        "cache_ttl_seconds": 300,
        "cached": False,
    }
    value.update(overrides)
    return value


def test_spot_futures_table_parses_quotes_and_basis():
    parsed = parse_basis_page(_fixture("ppi_sf_20261120.html"), DAY)
    assert parsed["data_as_of"] == "2026-11-20"
    assert parsed["source_url"] == page_url(DAY)
    products = parsed["products"]
    assert len(products) == 54
    copper = products["铜"]
    assert copper["group"] == "上海期货交易所"
    assert (copper["spot_price"], copper["main_contract"], copper["main_price"]) == (
        101616.67,
        "2701",
        92400.0,
    )
    assert copper["basis"] == 9216.67
    assert copper["published_basis"] == 9216.0
    assert copper["basis_direction"] == "spot_premium"
    assert (products["螺纹钢"]["spot_price"], products["螺纹钢"]["main_price"]) == (3108.5, 2999.0)
    assert products["螺纹钢"]["basis"] == 109.5
    assert products["甲醇"]["main_price"] == 3073.0
    assert products["甲醇"]["basis"] == 622.0
    assert products["工业硅"]["basis"] == 1040.0
    assert products["碳酸锂"]["basis"] == 1460.0
    assert products["多晶硅"]["main_price"] == 31890.0
    assert products["豆粕"]["basis"] == 81.0
    assert products["棉花"]["main_contract"] == "2701"
    assert products["白糖"]["basis"] == 110.67
    assert (products["铝"]["group"], products["铝"]["basis"]) == ("上海期货交易所", 821.67)
    assert (products["锌"]["spot_price"], products["锌"]["main_price"]) == (24059.0, 23930.0)
    assert products["镍"]["basis"] == 950.0
    assert (products["白银"]["main_contract"], products["白银"]["basis"]) == ("2702", -59.33)
    assert products["白银"]["basis_direction"] == "futures_premium"
    assert products["天然橡胶"]["basis"] == -191.67
    assert products["石油沥青"]["main_price"] == 4300.0
    assert (products["PTA"]["group"], products["PTA"]["basis"]) == ("郑州商品交易所", 808.5)
    assert products["菜籽粕"]["basis"] == 159.0
    assert products["菜籽油"]["basis"] == 602.0
    assert products["涤纶短纤"]["basis"] == 1298.08


def test_spot_futures_table_marks_both_basis_directions_and_history():
    earlier = parse_basis_page(_fixture("ppi_sf_20261119.html"), EARLIER_DAY)["products"]
    assert earlier["黄金"]["spot_price"] == 801.77
    assert earlier["黄金"]["basis"] == -2.55
    assert earlier["黄金"]["basis_direction"] == "futures_premium"
    assert earlier["铜"]["spot_price"] == 105355.0
    assert earlier["铜"]["basis"] == 10635.0
    flat = earlier["碳酸锂"]
    assert flat["basis"] == 0.0 and flat["basis_direction"] == "flat"
    legacy = parse_basis_page(_fixture("ppi_sf_20150105.html"), date(2015, 1, 5))["products"]
    assert legacy["黄金"]["main_contract"] == "1506"
    assert legacy["黄金"]["basis"] == -2.35
    assert legacy["甲醇"]["main_contract"] == "MA506"
    assert legacy["玻璃"]["spot_price"] == 14.62


def test_empty_table_has_no_rows_and_stale_dates_are_rejected():
    empty = parse_basis_page(_fixture("ppi_sf_20261121_empty.html"), EMPTY_DAY)
    assert empty["products"] == {}
    raw = _fixture("ppi_sf_20261120.html")
    with pytest.raises(ValueError):
        parse_basis_page(raw, EARLIER_DAY)
    with pytest.raises(ValueError):
        parse_basis_page(raw.replace(b'id="fdata"', b'id="xdata"'), DAY)
    with pytest.raises(ValueError):
        parse_basis_page(raw.replace("现期差2".encode(), "现期差X".encode()), DAY)
    with pytest.raises(ValueError):
        parse_basis_page(raw.replace(b"<td>101616.67&nbsp;</td>", b"<td>101 616.67&nbsp;</td>"), DAY)


async def test_read_basis_solves_the_check_page_before_reading_the_table():
    seen = []

    def serve(request):
        seen.append(request.headers.get("cookie") or "")
        return _serve(_fixture("ppi_sf_20261120.html"), _fixture("ppi_sf_20261119.html"))(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        result = await basis.read_basis(client, "CU", DAY, _deadline())
    assert result["status"] == "collected"
    assert len(seen) == 2
    assert "HW_CHECK=" not in seen[0]
    assert f"HW_CHECK={_token()}" in seen[1]
    assert result["source_url"] == page_url(DAY)
    assert result["data_as_of"] == "2026-11-20"
    assert (result["spot_price"], result["futures_contract"], result["futures_price"]) == (
        101616.67,
        "2701",
        92400.0,
    )
    assert result["basis"] == 9216.67
    assert result["basis_direction"] == "spot_premium"
    assert result["source_published_basis"] == 9216.0


async def test_read_basis_accepts_a_literal_cookie_check_page():
    challenge = _fixture("ppi_sf_challenge.html").decode("utf-8")
    token = _token()
    inline = challenge.replace(f'var _0x2 = "{token}";', "")
    inline = inline.replace('"HW_CHECK" + "=" + _0x2', f'"HW_CHECK={token}"')
    assert _token_literal(token) in inline

    def serve(request):
        if "HW_CHECK=" in (request.headers.get("cookie") or ""):
            return httpx.Response(200, content=_fixture("ppi_sf_20261120.html"))
        return httpx.Response(200, content=inline.encode("utf-8"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        result = await basis.read_basis(client, "CU", DAY, _deadline())
    assert result["status"] == "collected" and result["basis"] == 9216.67


def _token_literal(token):
    return f'document.cookie = "HW_CHECK={token}'


async def test_unresolved_check_page_is_unavailable_without_prices():
    def serve(request):
        return httpx.Response(200, content=_fixture("ppi_sf_challenge.html"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        result = await basis.read_basis(client, "CU", DAY, _deadline())
    assert result["status"] == "basis_unavailable"
    assert result["error_code"] == "challenge_unsolved"
    assert "basis" not in result and "spot_price" not in result and "futures_price" not in result

    stripped = re.sub(rb'var\s+_0x[0-9a-zA-Z]+\s*=\s*"[0-9a-fA-F]+"', b"", _fixture("ppi_sf_challenge.html"))

    def serve_without_token(request):
        return httpx.Response(200, content=stripped)

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve_without_token)) as client:
        missing = await basis.read_basis(client, "CU", DAY, _deadline())
    assert missing["status"] == "basis_unavailable"
    assert missing["error_code"] == "challenge_unsolved"


async def test_read_basis_reports_missing_rows_and_inconsistent_units():
    def serve(request):
        url = str(request.url)
        if "day-2026-11-20" in url:
            if "HW_CHECK=" in (request.headers.get("cookie") or ""):
                return httpx.Response(200, content=_fixture("ppi_sf_20261120.html"))
            return httpx.Response(200, content=_fixture("ppi_sf_challenge.html"))
        return httpx.Response(200, content=_empty_page(EMPTY_DAY))

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        glass = await basis.read_basis(client, "FG", DAY, _deadline())
        oil = await basis.read_basis(client, "SC", DAY, _deadline())
        absent = await basis.read_basis(client, "CU", EMPTY_DAY, _deadline())
    assert glass["status"] == "basis_unavailable"
    assert glass["error_code"] == "source_price_mismatch"
    assert "basis" not in glass
    assert oil["status"] == "basis_unavailable" and oil["error_code"] == "symbol_not_covered"
    assert absent["status"] == "basis_unavailable" and absent["error_code"] == "report_not_found"
    assert "basis" not in absent


async def test_snapshot_window_walks_back_and_keeps_dates_and_cache_separate(monkeypatch):
    # 钉住"今天"，否则跨零点运行时窗口会多出一天（曾在本仓库 CI 上真实出现）。
    monkeypatch.setattr(basis, "_today", lambda: date(2026, 11, 22))
    page_20 = _fixture("ppi_sf_20261120.html")
    page_19 = _fixture("ppi_sf_20261119.html")
    seen = []

    def serve(request):
        seen.append(str(request.url))
        return _serve(page_20, page_19)(request)

    monkeypatch.setattr(public_network, "async_client", _client_factory(serve))
    newest = await basis.snapshot("CU")
    assert newest["status"] == "collected"
    assert newest["date_basis"] == "bounded_calendar_window"
    assert newest["data_as_of"] == "2026-11-20"
    assert newest["basis"] == 9216.67
    assert [attempt["requested_date"] for attempt in newest["source_attempts"]] == [
        "2026-11-22",
        "2026-11-21",
        "2026-11-20",
    ]
    assert [attempt["error_code"] for attempt in newest["source_attempts"]] == [
        "report_not_found",
        "report_not_found",
        None,
    ]
    assert newest["gaps"] == []
    calls = len(seen)
    cached = await basis.snapshot("CU")
    assert cached["cached"] is True and len(seen) == calls
    earlier = await basis.snapshot("AU", "2026-11-19")
    assert earlier["status"] == "collected"
    assert earlier["date_basis"] == "requested"
    assert earlier["data_as_of"] == "2026-11-19"
    assert earlier["basis"] == -2.55
    assert earlier["basis_direction"] == "futures_premium"
    assert [attempt["requested_date"] for attempt in earlier["source_attempts"]] == ["2026-11-19"]
    assert earlier["data_as_of"] != newest["data_as_of"]
    holiday = await basis.snapshot("CU", "2026-11-21")
    assert holiday["status"] == "basis_unavailable"
    assert holiday["error_code"] == "report_not_found"
    assert [attempt["requested_date"] for attempt in holiday["source_attempts"]] == ["2026-11-21"]


async def test_snapshot_never_estimates_when_the_check_page_never_solves(monkeypatch):
    def serve(request):
        return httpx.Response(200, content=_fixture("ppi_sf_challenge.html"))

    monkeypatch.setattr(public_network, "async_client", _client_factory(serve))
    result = await basis.snapshot("CU")
    assert result["status"] == "basis_unavailable"
    assert result["error_code"] == "challenge_unsolved"
    assert result["reason"]
    assert result["gaps"] != []
    assert "basis" not in result and "spot_price" not in result
    assert len(result["source_attempts"]) == 1


async def test_unmapped_products_are_unavailable_without_network(monkeypatch):
    def fail(**_kwargs):
        pytest.fail("unmapped product must not touch the network")

    monkeypatch.setattr(public_network, "async_client", fail)
    for symbol in ("SC", "IF", "IC", "T", "TF", "TS", "AP", "NR", "LU"):
        result = await basis.snapshot(symbol)
        assert result["status"] == "basis_unavailable"
        assert result["error_code"] == "symbol_not_covered"
        assert result["reason"]
        assert result["covered_symbols"] == sorted(basis.BASIS_PRODUCTS)
        assert "basis" not in result and "spot_price" not in result
    assert "国债" in (await basis.snapshot("TS"))["reason"]
    assert "上海国际能源交易中心" in (await basis.snapshot("NR"))["reason"]
    assert "苹果" in (await basis.snapshot("AP"))["reason"]
    with pytest.raises(ValueError):
        await basis.snapshot("CU", "2026/11/20")


@pytest.mark.parametrize("observed", ["2026/11/20", "20261120", "2026-13-01"])
async def test_invalid_dates_and_symbols_raise(observed):
    with pytest.raises(ValueError):
        await basis.snapshot("CU", observed)
    with pytest.raises(ValueError):
        await basis.snapshot("XX")


def test_basis_document_renders_basis_with_quote_units():
    document = basis.snapshot_document(_collected("CU"))
    assert document["title"] == "铜 CU 现货基差快照 2026-11-20"
    assert document["data_period"] == "2026-11-20"
    assert document["status"] == "collected"
    assert document["tradable_contract"] is False
    assert document["source"]["kind"] == basis.SOURCE_KIND
    assert document["url"] == page_url(DAY)
    section = document["sections"][0]
    assert section["section"] == "spot_futures_basis"
    assert section["label"] == "现货价、主力期货价与基差"
    assert section["data_period"] == "2026-11-20"
    text = document["pages"][0]["text"]
    assert "数据日期：2026年11月20日" in text
    assert "现货价格 101616.67 元/吨" in text
    assert "主力期货合约 2701 价格 92400 元/吨" in text
    assert "基差（现货减主力期货） 9216.67 元/吨" in text
    assert "来源披露的主力合约现期差 9216 元/吨" in text
    assert "现货升水（基差为正）" in text
    gold = basis.snapshot_document(_collected("AU", "2026-11-19"))
    assert "现货价格 801.77 元/克" in gold["pages"][0]["text"]
    assert "期货升水（基差为负）" in gold["pages"][0]["text"]
    assert basis.snapshot_document({"status": "basis_unavailable"}) is None
    assert basis.snapshot_document({"status": "collected"}) is None
    assert basis.snapshot_document(_collected() | {"data_as_of": "bad"}) is None
    assert basis.snapshot_document(_collected() | {"spot_price": None}) is None
    assert basis.snapshot_document(_collected() | {"basis_direction": "unknown"}) is None


async def test_basis_document_is_original_evidence_and_dates_stay_separate(monkeypatch):
    page_20 = _fixture("ppi_sf_20261120.html")
    page_19 = _fixture("ppi_sf_20261119.html")
    monkeypatch.setattr(
        public_network,
        "async_client",
        _client_factory(_serve(page_20, page_19)),
    )
    run_id = _evidence_run()
    call = {"type": "tool_use", "id": "call-current", "name": "futures_basis", "input": {"symbol": "CU"}}
    observation = await basis.snapshot("CU", "2026-11-20")
    document = basis.snapshot_document(observation)
    assert document is not None
    stored = ingest_market_snapshot(
        "alice",
        run_id,
        call,
        document,
        access_method="futures_basis",
        request={"symbol": "CU", "date": "2026-11-20"},
    )
    record = stored["document"]
    assert record["source_kind"] == "market_data_snapshot"
    assert record["original_obtained"] is True
    assert record["access_method"] == "futures_basis"
    assert record["document_number"] == "CU-2026-11-20"
    assert record["data_period"] == "2026-11-20"
    assert record["request"] == {"symbol": "CU", "date": "2026-11-20"}
    assert len(stored["passages"]) == 1
    replay = ingest_market_snapshot(
        "alice",
        run_id,
        call,
        document,
        access_method="futures_basis",
        request={"symbol": "CU", "date": "2026-11-20"},
    )
    assert replay["document"]["document_id"] == record["document_id"]
    later = await basis.snapshot("CU", "2026-11-19")
    later_call = {"type": "tool_use", "id": "call-later", "name": "futures_basis", "input": {}}
    later_document = ingest_market_snapshot(
        "alice",
        run_id,
        later_call,
        basis.snapshot_document(later),
        access_method="futures_basis",
        request={"symbol": "CU", "date": "2026-11-19"},
    )["document"]
    assert later_document["document_id"] != record["document_id"]
    assert later_document["document_number"] == "CU-2026-11-19"


def _evidence_run(run_id="basis-run"):
    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO users(username,password_hash) VALUES(?,?)",
            ["alice", "synthetic-q3-user"],
        )
    context = build_research_context(run_id=run_id, now=datetime(2026, 11, 21, 18, tzinfo=UTC))
    create_research_run("alice", "", "研究合成现货与期货基差数据", context)
    return run_id


def test_basis_capability_is_a_read_only_futures_tool():
    from server.agent.tools import PLATFORM_TOOLS, native_catalog
    from server.capabilities import capability_catalog

    rows = [row for row in capability_catalog() if row["id"] == "futures_basis"]
    assert len(rows) == 1
    assert rows[0]["read_only"] is True
    assert rows[0]["view"] == "futures" and rows[0]["target_view"] == "futures"
    assert "futures_basis" in PLATFORM_TOOLS
    assert any(row["id"] == "futures_basis" for row in native_catalog())


def test_every_catalog_product_has_exactly_one_basis_outcome():
    covered = set(basis.BASIS_PRODUCTS)
    uncovered = set(basis.UNCOVERED_REASONS)
    assert covered.isdisjoint(uncovered)
    assert covered | uncovered == set(PRODUCTS)
    assert basis.BASIS_PRODUCTS["AL"] == ("上海期货交易所", "铝")
    assert basis.BASIS_PRODUCTS["TA"] == ("郑州商品交易所", "PTA")
    assert basis.BASIS_PRODUCTS["PF"] == ("郑州商品交易所", "涤纶短纤")


async def test_added_products_read_the_published_spot_futures_rows(monkeypatch):
    monkeypatch.setattr(
        public_network,
        "async_client",
        _client_factory(_serve(_fixture("ppi_sf_20261120.html"), _fixture("ppi_sf_20261119.html"))),
    )
    expected = {
        "AL": (22356.67, "2701", 21535.0, 821.67),
        "ZN": (24059.0, "2701", 23930.0, 129.0),
        "NI": (118250.0, "2701", 117300.0, 950.0),
        "AG": (9978.67, "2702", 10038.0, -59.33),
        "RU": (18433.33, "2701", 18625.0, -191.67),
        "BU": (4780.0, "2701", 4300.0, 480.0),
        "TA": (6758.5, "2701", 5950.0, 808.5),
        "RM": (2362.0, "2701", 2203.0, 159.0),
        "OI": (10474.0, "2701", 9872.0, 602.0),
        "PF": (8878.08, "2701", 7580.0, 1298.08),
    }
    for symbol, (spot, contract, price, computed) in expected.items():
        value = await basis.snapshot(symbol, "2026-11-20")
        assert value["status"] == "collected", symbol
        assert (value["spot_price"], value["futures_contract"], value["futures_price"]) == (
            spot,
            contract,
            price,
        )
        assert value["basis"] == pytest.approx(computed, abs=0.01)
        assert value["quote_unit"] == basis.futures._QUOTE_UNITS[symbol]
    silver = await basis.snapshot("AG", "2026-11-20")
    assert silver["quote_unit"] == "元/千克"
    document = basis.snapshot_document(silver)
    assert "元/千克" in document["pages"][0]["text"]
