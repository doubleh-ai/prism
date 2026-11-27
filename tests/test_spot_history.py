"""Daily spot-price history from the dated 100ppi spot-versus-futures table.

The series is the public table's own spot price on each date; these tests pin
that missing dates are omitted rather than filled, that the one documented unit
conversion (glass 元/平方米 -> 元/吨) is explicit and checked, that every
unavailable path returns a stable reason code without a number, and that a
collected series is registered as the run's original document.
"""

import asyncio
import re
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from server import futures_basis as basis
from server import futures_spot_history as spot
from server import public_network
from server.database import get_db
from server.research import create_research_run
from server.research_context import build_research_context

FIXTURES = Path(__file__).parent / "fixtures"
DAY = date(2026, 11, 20)


@pytest.fixture(autouse=True)
def _empty_cache():
    spot._CACHE.clear()
    yield
    spot._CACHE.clear()


def _fixture(name):
    return (FIXTURES / name).read_bytes()


def _page(day):
    """One real table page whose title date is rewritten to ``day``."""
    text = _fixture("ppi_sf_20261120.html").decode("utf-8")
    text = text.replace("2026年11月20日", f"{day.year}年{day.month}月{day.day}日")
    text = text.replace("2026-11-20", day.isoformat())
    return text.encode("utf-8")


def _empty_page(day):
    text = _fixture("ppi_sf_20261121_empty.html").decode("utf-8")
    text = text.replace("2026年11月21日", f"{day.year}年{day.month}月{day.day}日")
    return text.encode("utf-8")


def _client_factory(handler):
    def factory(**kwargs):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            trust_env=False,
        )

    return factory


def _serve(behaviour=None):
    """Serve per-date page kinds; ``behaviour`` maps ISO date -> kind."""
    behaviour = behaviour or {}

    def serve(request):
        matched = re.search(r"day-(\d{4})-(\d{2})-(\d{2})", str(request.url))
        assert matched is not None
        day = date(*map(int, matched.groups()))
        kind = behaviour.get(day.isoformat(), behaviour.get("*", "page"))
        if kind == "page":
            return httpx.Response(200, content=_page(day))
        if kind == "empty":
            return httpx.Response(200, content=_empty_page(day))
        if kind == "challenge":
            return httpx.Response(200, content=_fixture("ppi_sf_challenge.html"))
        if kind == "invalid":
            return httpx.Response(200, content=b"<html><body>not a table</body></html>")
        if kind == "error":
            return httpx.Response(500, content=b"upstream error")
        if kind == "timeout":
            raise httpx.ReadTimeout("synthetic timeout")
        raise AssertionError(kind)

    return serve


def _use(monkeypatch, behaviour=None):
    monkeypatch.setattr(public_network, "async_client", _client_factory(_serve(behaviour)))


async def _history(symbol, start, end):
    return await spot.history(symbol, start.isoformat(), end.isoformat())


async def test_daily_series_reports_observed_dates_only(monkeypatch):
    # 2026-11-02 Mon .. 2026-11-06 Fri: one empty publication day, one
    # unrecognisable page, three usable rows.
    _use(
        monkeypatch,
        {
            "2026-11-03": "empty",
            "2026-11-04": "invalid",
        },
    )
    value = await spot.history("CU", "2026-11-02", "2026-11-06")
    assert value["status"] == "collected"
    assert [row["date"] for row in value["observations"]] == ["2026-11-02", "2026-11-05", "2026-11-06"]
    assert value["sample_count"] == 3
    for row in value["observations"]:
        assert set(row) == {"date", "price", "unit", "source", "access_method"}
        assert row["price"] == 101616.67
        assert (row["unit"], row["source"], row["access_method"]) == (
            "元/吨",
            basis.SOURCE_NAME,
            spot.ACCESS_METHOD,
        )
    coverage = value["coverage"]
    assert coverage["weekdays_requested"] == 5
    assert coverage["no_publication_days"] == 1
    assert coverage["invalid_source_days"] == 1
    assert value["first_date"] == "2026-11-02" and value["last_date"] == "2026-11-06"
    assert any("不插值" in note for note in value["gaps"])


async def test_weekend_days_are_not_requested(monkeypatch):
    _use(monkeypatch)
    value = await spot.history("CU", "2026-10-31", "2026-11-02")
    assert [row["date"] for row in value["observations"]] == ["2026-11-02"]
    assert value["coverage"]["weekdays_requested"] == 1
    assert value["coverage"]["calendar_days"] == 3


async def test_glass_converts_square_metre_prices_to_quote_unit(monkeypatch):
    _use(monkeypatch)
    value = await spot.history("FG0", "2026-11-20", "2026-11-20")
    assert value["status"] == "collected"
    assert (value["unit"], value["source_unit"]) == ("元/吨", "元/平方米")
    assert value["unit_conversion"] == {
        "factor": 80.0,
        "formula": "元/平方米*80=元/吨",
        "evidence_url": "https://www.100ppi.com/sf/959.html",
    }
    assert value["observations"][0]["price"] == pytest.approx(12.25 * 80)
    assert value["unit_checks"]["spot_futures_ratio_min"] == pytest.approx(12.25 * 80 / 885.0)
    assert value["unit_checks"]["tolerance"] == [0.5, 2.0]


async def test_precious_metals_keep_their_own_source_unit(monkeypatch):
    _use(monkeypatch)
    gold = await spot.history("AU", "2026-11-20", "2026-11-20")
    silver = await spot.history("AG", "2026-11-20", "2026-11-20")
    assert (gold["unit"], gold["source_unit"]) == ("元/克", "元/克")
    assert gold["observations"][0]["price"] == 775.11
    assert (silver["unit"], silver["source_unit"]) == ("元/千克", "元/千克")
    assert silver["observations"][0]["price"] == 9978.67


@pytest.mark.parametrize(
    ("value", "source_unit", "target_unit", "expected"),
    [
        (12.25, "元/平方米", "元/吨", 980.0),
        (2200.0, "元/吨", "元/公斤", 2.2),
        (8.5, "元/公斤", "元/吨", 8500.0),
        (101616.67, "元/吨", "元/吨", 101616.67),
    ],
)
def test_convert_price_uses_only_documented_rules(value, source_unit, target_unit, expected):
    assert spot.convert_price(value, source_unit, target_unit) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("value", "source_unit", "target_unit"),
    [
        (0, "元/吨", "元/公斤"),
        (-1, "元/吨", "元/公斤"),
        (True, "元/吨", "元/吨"),
        (10.0, "元/吨", "美元/吨"),
    ],
)
def test_convert_price_rejects_unknown_or_invalid_input(value, source_unit, target_unit):
    with pytest.raises(ValueError):
        spot.convert_price(value, source_unit, target_unit)


def test_unit_plan_covers_every_mapped_product():
    assert spot.covered_symbols() == sorted(basis.BASIS_PRODUCTS)
    assert spot.unit_plan("FG")["factor"] == 80.0
    assert spot.unit_plan("CU") == {
        "source_unit": "元/吨",
        "unit": "元/吨",
        "factor": 1.0,
        "formula": None,
        "evidence_url": None,
    }
    assert spot.unit_plan("SC") is None


async def test_unit_incompatible_product_is_not_converted(monkeypatch):
    _use(monkeypatch)
    monkeypatch.delitem(spot._UNIT_CONVERSIONS, ("元/平方米", "元/吨"))
    value = await spot.history("FG", "2026-11-20", "2026-11-20")
    assert value["status"] == "spot_history_unavailable"
    assert value["error_code"] == "unit_incompatible"
    assert value["observations"] == [] and value["sample_count"] == 0
    assert "FG" not in spot.covered_symbols()


async def test_unreliable_spot_unit_fails_as_unit_unverified(monkeypatch):
    real = basis.parse_basis_page

    def inflated(raw, day):
        parsed = real(raw, day)
        row = parsed["products"]["铜"]
        row["spot_price"] = row["spot_price"] * 100
        return parsed

    _use(monkeypatch)
    monkeypatch.setattr(basis, "parse_basis_page", inflated)
    value = await spot.history("CU", "2026-11-20", "2026-11-20")
    assert value["status"] == "spot_history_unavailable"
    assert value["error_code"] == "unit_unverified"
    assert value["observations"] == []
    assert value["unverified_dates"] == ["2026-11-20"]


async def test_product_missing_from_published_table_is_not_estimated(monkeypatch):
    real = basis.parse_basis_page

    def without_copper(raw, day):
        parsed = real(raw, day)
        parsed["products"].pop("铜")
        return parsed

    _use(monkeypatch)
    monkeypatch.setattr(basis, "parse_basis_page", without_copper)
    value = await spot.history("CU", "2026-11-19", "2026-11-20")
    assert value["status"] == "spot_history_unavailable"
    assert value["error_code"] == "product_not_listed"
    assert value["coverage"]["published_days"] == 2
    assert value["coverage"]["listed_days"] == 0


async def test_empty_pages_report_no_publication(monkeypatch):
    _use(monkeypatch, {"*": "empty"})
    value = await spot.history("CU", "2026-11-19", "2026-11-20")
    assert value["status"] == "spot_history_unavailable"
    assert value["error_code"] == "report_not_found"


async def test_unrecognisable_pages_report_invalid_source_data(monkeypatch):
    _use(monkeypatch, {"*": "invalid"})
    value = await spot.history("CU", "2026-11-19", "2026-11-20")
    assert value["status"] == "spot_history_unavailable"
    assert value["error_code"] == "invalid_source_data"


async def test_unsolved_check_page_reports_challenge(monkeypatch):
    _use(monkeypatch, {"*": "challenge"})
    value = await spot.history("CU", "2026-11-19", "2026-11-20")
    assert value["status"] == "spot_history_unavailable"
    assert value["error_code"] == "challenge_unsolved"
    assert value["observations"] == []


async def test_source_failure_aborts_without_partial_series(monkeypatch):
    _use(monkeypatch, {"2026-11-20": "error"})
    value = await spot.history("CU", "2026-11-19", "2026-11-20")
    assert value["status"] == "spot_history_unavailable"
    assert value["error_code"] == "http_error"
    assert value["observations"] == []
    assert value["source_attempts"]


async def test_source_timeout_aborts_without_partial_series(monkeypatch):
    _use(monkeypatch, {"*": "timeout"})
    value = await spot.history("CU", "2026-11-19", "2026-11-20")
    assert value["status"] == "spot_history_unavailable"
    assert value["error_code"] == "source_timeout"


def test_symbol_not_covered_does_not_touch_the_network(monkeypatch):
    monkeypatch.setattr(
        public_network,
        "async_client",
        lambda **kwargs: pytest.fail("network must not be used for an uncovered symbol"),
    )
    value = asyncio.run(spot.history("SC", "2026-11-19", "2026-11-20"))
    assert value["status"] == "spot_history_unavailable"
    assert value["error_code"] == "symbol_not_covered"
    assert value["reason"] == basis.UNCOVERED_REASONS["SC"]
    assert value["backtest_ready"] is False
    assert value["backtest_ready_reasons"] == ["spot_history_unavailable"]


@pytest.mark.parametrize(
    ("symbol", "error_code"),
    [
        ("CU2701", "contract_not_supported"),
        ("ZC", None),
    ],
)
def test_contract_and_symbol_validation(symbol, error_code):
    if error_code is None:
        with pytest.raises(ValueError):
            asyncio.run(spot.history(symbol, "2026-11-19", "2026-11-20"))
        return
    value = asyncio.run(spot.history(symbol, "2026-11-19", "2026-11-20"))
    assert value["error_code"] == error_code
    assert value["requested_contract"] == symbol


@pytest.mark.parametrize(
    ("start", "end", "error_code"),
    [
        ("2012-12-31", "2013-01-04", "range_before_source_history"),
        ("2020-01-01", "2023-12-31", "range_too_large"),
        ("2026-11-19", "2027-01-01", "invalid_range"),
    ],
)
def test_range_guards_return_static_reason_codes(start, end, error_code):
    value = asyncio.run(spot.history("CU", start, end))
    assert value["status"] == "spot_history_unavailable"
    assert value["error_code"] == error_code
    assert value["observations"] == []


def test_start_after_end_is_invalid_arguments():
    with pytest.raises(ValueError):
        asyncio.run(spot.history("CU", "2026-11-20", "2026-11-19"))


async def test_short_series_is_not_backtest_ready(monkeypatch):
    _use(monkeypatch)
    value = await spot.history("CU", "2026-11-02", "2026-11-06")
    assert value["backtest_ready"] is False
    assert value["backtest_ready_reasons"] == ["insufficient_observations", "span_too_short"]
    assert value["backtest_requirements"] == {"min_samples": 250, "min_span_days": 365}


async def test_full_year_series_meets_backtest_requirements(monkeypatch):
    _use(monkeypatch)
    monkeypatch.setattr(spot, "_today", lambda: date(2026, 11, 20))
    value = await spot.history("CU")
    assert value["status"] == "collected"
    assert value["requested_end"] == "2026-11-19"
    assert value["sample_count"] >= 250
    assert value["span_days"] >= 365
    assert value["backtest_ready"] is True
    assert value["backtest_ready_reasons"] == []


async def test_series_document_registers_as_original_evidence(monkeypatch):
    from server.agent import tools as agent_tools

    _use(monkeypatch)
    run_id = _evidence_run()
    call = {
        "type": "tool_use",
        "id": "call-spot",
        "name": "futures_spot_history",
        "input": {"symbol": "CU", "start_date": "2026-11-19", "end_date": "2026-11-20"},
    }
    series = await spot.history("CU", "2026-11-19", "2026-11-20")
    document = spot.history_document(series)
    assert document is not None
    assert document["data_period"] == "2026-11-20"
    assert document["title"] == "铜 CU 现货历史价格 2026-11-19 至 2026-11-20"
    assert len(document["pages"]) == 1 and len(document["sections"]) == 1
    assert "2026-11-19 现货价格 101616.67 元/吨" in document["pages"][0]["text"]
    assert "2026-11-20 现货价格 101616.67 元/吨" in document["pages"][0]["text"]
    stored = agent_tools._MARKET_EVIDENCE["futures_spot_history"]("alice", run_id, call, series)
    record = stored["document"]
    assert record["source_kind"] == "market_data_snapshot"
    assert record["original_obtained"] is True
    assert record["access_method"] == "futures_spot_history"
    assert record["document_number"] == "CU-2026-11-20"
    assert record["data_period"] == "2026-11-20"
    assert record["request"] == {
        "symbol": "CU",
        "start_date": "2026-11-19",
        "end_date": "2026-11-20",
    }
    assert len(stored["passages"]) == 1
    page_meta = stored["passages"][0]
    assert page_meta["page"] == 1 and page_meta["chars"] > 0
    assert len(page_meta["sha256"]) == 64
    replay = agent_tools._MARKET_EVIDENCE["futures_spot_history"]("alice", run_id, call, series)
    assert replay["document"]["document_id"] == record["document_id"]


def test_unavailable_series_is_not_registered(monkeypatch):
    document = spot.history_document(
        {
            "status": "spot_history_unavailable",
            "symbol": "SC",
            "observations": [],
        }
    )
    assert document is None
    assert spot.history_document({"status": "collected", "symbol": "CU", "observations": []}) is None


async def test_document_discloses_glass_conversion(monkeypatch):
    _use(monkeypatch)
    series = await spot.history("FG", "2026-11-20", "2026-11-20")
    document = spot.history_document(series)
    assert document is not None
    text = document["pages"][0]["text"]
    assert "元/平方米*80=元/吨" in text
    assert "https://www.100ppi.com/sf/959.html" in text
    assert "980 元/吨" in text


def test_capability_is_a_read_only_futures_tool():
    from server.agent.tools import PLATFORM_TOOLS, native_catalog
    from server.capabilities import SpotHistoryArgs, capability_catalog

    rows = [row for row in capability_catalog() if row["id"] == "futures_spot_history"]
    assert len(rows) == 1
    assert rows[0]["read_only"] is True
    assert rows[0]["view"] == "futures" and rows[0]["target_view"] == "futures"
    assert "futures_spot_history" in PLATFORM_TOOLS
    assert any(row["id"] == "futures_spot_history" for row in native_catalog())
    args = SpotHistoryArgs(symbol="cu0", start_date="2026-11-19", end_date="2026-11-20")
    assert args.model_dump()["symbol"] == "cu0"
    with pytest.raises(Exception):
        SpotHistoryArgs(symbol="CU", start_date="2026/11/19", end_date="2026-11-20")


def _evidence_run(run_id="spot-history-run"):
    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO users(username,password_hash) VALUES(?,?)",
            ["alice", "synthetic-q3-user"],
        )
    context = build_research_context(run_id=run_id, now=datetime(2026, 11, 21, 18, tzinfo=UTC))
    create_research_run("alice", "", "研究合成现货历史价格序列", context)
    return run_id
