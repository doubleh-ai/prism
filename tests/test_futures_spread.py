"""Same-day near/far settlement spreads as checkable research sources.

The spread tool reads two official reports: the complete listed-contract calendar
and the same-day settlement bulletin. These tests pin the three properties the
research flow depends on: a covered product yields the dated far-minus-near
difference with the right direction and a registered original page, a product or
date without a complete calendar returns an explicit ``spread_unavailable``
reason instead of a number, and two trading days never cross-bind.
"""

from datetime import UTC, datetime

import pytest

from server import capabilities, exchange_futures, futures_parameters
from server.agent.tools import PLATFORM_TOOLS, native_catalog
from server.database import get_db
from server.futures_spread import spread, spread_document
from server.research import (
    create_research_run,
    finalize_research_run,
    ingest_market_snapshot,
    read_research_run,
)
from server.research_context import build_research_context

DAY = "2026-11-20"
MONTHS = (
    ("rb2601", "2026-01-16", "2026-01-22"),
    ("rb2603", "2026-03-16", "2026-03-22"),
    ("rb2605", "2026-05-15", "2026-05-21"),
    ("rb2607", "2026-07-16", "2026-07-22"),
    ("rb2701", "2027-01-15", "2027-01-21"),
)


def _calendar(day=DAY):
    stamp = day.replace("-", "")
    return {
        "status": "collected",
        "symbol": "RB",
        "data_as_of": day,
        "source_updated_at": day + " 15:10:00",
        "source_url": f"https://www.shfe.com.cn/data/busiparamdata/future/ContractBaseInfo{stamp}.dat",
        "reference_url": "https://www.shfe.com.cn/reports/businessdata/prmsummary/",
        "body_sha256": "a" * 64,
        "contracts": [
            {
                "contract": contract,
                "listed_on": "2025-01-16",
                "listed_expiry_date": "2026-12-15",
                "delivery_start": start,
                "delivery_end": end,
            }
            for contract, start, end in MONTHS
        ],
        "calendar_adjustments_status": "not_evaluated",
        "notice": "synthetic calendar",
    }


def _settlements(day=DAY, near=3100.0, far=3220.0):
    prices = (near, near + 10.0, near + 40.0, near + 80.0, far)
    stamp = day.replace("-", "")
    return {
        "status": "collected",
        "data_as_of": day,
        "requested_date": day,
        "source_url": f"https://www.shfe.com.cn/data/tradedata/future/dailydata/kx{stamp}.dat",
        "source_method": "official_daily_report",
        "date_provenance": "reported_date",
        "body_sha256": "b" * 64,
        "contracts": [
            {
                "contract": contract,
                "delivery_month": start[:7],
                "close": price,
                "settlement": price,
                "volume": 1000.0,
                "open_interest": 5000.0,
            }
            for (contract, start, _end), price in zip(MONTHS, prices, strict=True)
        ],
        "notice": "synthetic settlements",
    }


def _patch(monkeypatch, calendar_factory, settlement_factory):
    async def read_contract_dates(client, symbol, day, deadline):
        return calendar_factory(day.isoformat())

    async def read_settlements(client, symbol, day, deadline):
        return settlement_factory(day.isoformat())

    monkeypatch.setattr(futures_parameters, "read_contract_dates", read_contract_dates)
    monkeypatch.setattr(exchange_futures, "read_settlements", read_settlements)


def _user(username="alice"):
    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO users(username,password_hash) VALUES(?,?)",
            [username, "synthetic-q3-user"],
        )


def _run(run_id):
    _user()
    context = build_research_context(run_id=run_id, now=datetime(2026, 11, 21, 18, tzinfo=UTC))
    create_research_run("alice", "", "研究合成期货品种的同日跨期价差", context)


def _record(run_id, observation, call_id=None):
    call = {
        "type": "tool_use",
        "id": call_id or f"call-{run_id}-{observation['symbol']}-{observation['data_as_of']}",
        "name": "futures_spread",
        "input": {"symbol": observation["symbol"], "trading_day": observation["data_as_of"]},
    }
    document = spread_document(observation)
    assert document is not None
    return ingest_market_snapshot(
        "alice",
        run_id,
        call,
        document,
        access_method="futures_spread",
        request=call["input"],
    )


def _claims_by_statement(stored):
    return {claim["statement"]: claim for claim in stored["claims"]}


# ---- test 1: a covered product yields a dated, correctly directed spread -------------


async def test_covered_product_returns_dated_spread_with_direction(monkeypatch):
    _patch(monkeypatch, _calendar, _settlements)
    observation = await spread("RB", "2026-11-20")
    assert observation["status"] == "collected"
    assert observation["data_as_of"] == "2026-11-20"
    assert observation["pair_selection"] == "nearest_and_farthest_priced_listed_delivery_months"
    assert observation["near_contract"] == "rb2601"
    assert observation["far_contract"] == "rb2701"
    assert observation["near_settlement"] == pytest.approx(3100.0)
    assert observation["far_settlement"] == pytest.approx(3220.0)
    assert observation["far_minus_near"] == pytest.approx(120.0)
    assert observation["far_minus_near"] > 0
    assert observation["shape"] == "contango"
    assert observation["tradable_contract"] is False
    assert observation["contract_months"]["priced_contracts"] == 5
    assert observation["contract_months"]["count"] == 5

    # Direction is signed by the two settlement prices, not by contract order only.
    _patch(monkeypatch, _calendar, lambda day: _settlements(day, near=3200.0, far=3100.0))
    backwardation = await spread("RB", "2026-11-20")
    assert backwardation["far_minus_near"] == pytest.approx(-100.0)
    assert backwardation["shape"] == "backwardation"

    # The collected observation becomes an obtained original page, so the report's
    # own spread figure goes through the existing claim verification path.
    document = spread_document(observation)
    assert document is not None
    assert len(document["pages"]) == 2
    assert "远月减近月价差 120 元/吨" in document["pages"][1]["text"]
    _run("run-spread-price")
    stored_document = _record("run-spread-price", observation)
    assert stored_document["document"]["original_obtained"] is True
    assert stored_document["document"]["source_kind"] == "market_data_snapshot"
    assert stored_document["document"]["access_method"] == "futures_spread"
    assert stored_document["document"]["data_period"] == "2026-11-20"
    document_id = stored_document["document"]["document_id"]

    report = "螺纹钢 RB 近月结算价 3100 元/吨，远月结算价 3220 元/吨，远月减近月价差 120 元/吨。"
    revision = finalize_research_run("alice", "run-spread-price", report)
    assert revision["acceptance_status"] != "blocked"
    claims = _claims_by_statement(read_research_run("alice", "run-spread-price"))
    spread_claim = claims["报告正文数字：120 元/吨"]
    assert spread_claim["verification"] == "verified_source"
    assert spread_claim["origin"] == "server_locator"
    assert spread_claim["source_locator"] == {"document_id": document_id, "page": 2}
    assert "2026-11-20" in spread_claim["source"]
    assert "上海期货交易所" in spread_claim["source"]
    for display in ("3100 元/吨", "3220 元/吨"):
        assert claims[f"报告正文数字：{display}"]["verification"] == "verified_source"


# ---- test 2: missing contract months never become a number ---------------------------


async def test_missing_contract_months_returns_spread_unavailable(monkeypatch):
    # A product outside the published calendar scope is refused before any read.
    async def unexpected(*args, **kwargs):
        raise AssertionError("a not-covered product must not read exchange reports")

    monkeypatch.setattr(futures_parameters, "read_contract_dates", unexpected)
    monkeypatch.setattr(exchange_futures, "read_settlements", unexpected)
    not_covered = await spread("MA", "2026-11-20")
    assert not_covered["status"] == "spread_unavailable"
    assert not_covered["reason_code"] == "contract_months_not_covered"
    assert not_covered["covered_symbols"] == sorted(futures_parameters.PRODUCTS)
    assert not_covered["far_minus_near"] is None
    assert spread_document(not_covered) is None

    # DCE products name the official-site challenge and the missing calendar.
    for symbol in ("M", "C", "I", "P"):
        dce = await spread(symbol, "2026-11-20")
        assert dce["status"] == "spread_unavailable"
        assert dce["reason_code"] == "contract_months_not_covered"
        assert "412" in dce["reason"] and "合约月份" in dce["reason"]
        assert dce["near"] is None and dce["far"] is None
        assert dce["near_settlement"] is None and dce["far_minus_near"] is None
        assert dce["source"] is None

    # A covered product whose calendar report is missing stops before settlement.
    calls = []

    async def read_contract_dates(client, symbol, day, deadline):
        return {
            "status": "unavailable",
            "requested_date": day.isoformat(),
            "source_url": "https://www.shfe.com.cn/data/busiparamdata/future/ContractBaseInfo20261120.dat",
            "error_code": "report_not_found",
            "http_status": 404,
        }

    async def read_settlements(client, symbol, day, deadline):
        calls.append(day.isoformat())
        return _settlements(day.isoformat())

    monkeypatch.setattr(futures_parameters, "read_contract_dates", read_contract_dates)
    monkeypatch.setattr(exchange_futures, "read_settlements", read_settlements)
    no_calendar = await spread("RB", "2026-11-20")
    assert no_calendar["status"] == "spread_unavailable"
    assert no_calendar["reason_code"] == "contract_months_unavailable"
    assert no_calendar["contract_months"]["status"] == "unavailable"
    assert no_calendar["far_minus_near"] is None
    assert calls == []
    assert spread_document(no_calendar) is None

    # A calendar with fewer than two priced months is also unavailable, not zero.
    single = _settlements()
    single["contracts"] = single["contracts"][:1]
    _patch(monkeypatch, _calendar, lambda day: single)
    too_few = await spread("RB", "2026-11-20")
    assert too_few["status"] == "spread_unavailable"
    assert too_few["reason_code"] == "insufficient_settlement_contracts"
    assert too_few["settlement_report"]["priced_contracts"] == 1
    assert too_few["far_minus_near"] is None
    assert spread_document(too_few) is None


# ---- test 3: two trading days never cross-bind --------------------------------------


async def test_two_trading_days_do_not_cross_bind(monkeypatch):
    settlements = {
        "2026-11-19": _settlements("2026-11-19", near=3000.0, far=3100.0),
        "2026-11-20": _settlements("2026-11-20", near=3100.0, far=3220.0),
    }
    _patch(monkeypatch, _calendar, lambda day: settlements[day])

    earlier = await spread("RB", "2026-11-19")
    later = await spread("RB", "2026-11-20")
    assert earlier["data_as_of"] == "2026-11-19"
    assert later["data_as_of"] == "2026-11-20"
    assert earlier["far_minus_near"] == pytest.approx(100.0)
    assert later["far_minus_near"] == pytest.approx(120.0)

    _run("run-spread-dates")
    earlier_document = _record("run-spread-dates", earlier, "call-spread-earlier")
    later_document = _record("run-spread-dates", later, "call-spread-later")
    earlier_id = earlier_document["document"]["document_id"]
    later_id = later_document["document"]["document_id"]
    assert earlier_id != later_id
    earlier_text = "\n".join(page["text"] for page in spread_document(earlier)["pages"])
    later_text = "\n".join(page["text"] for page in spread_document(later)["pages"])
    assert "2026年11月19日" in earlier_text
    assert "2026年11月20日" not in earlier_text
    assert "2026年11月20日" in later_text
    assert "2026年11月19日" not in later_text

    report = "前一交易日远月减近月价差 100 元/吨；最新交易日远月减近月价差 120 元/吨。"
    finalize_research_run("alice", "run-spread-dates", report)
    claims = _claims_by_statement(read_research_run("alice", "run-spread-dates"))
    previous = claims["报告正文数字：100 元/吨"]
    latest = claims["报告正文数字：120 元/吨"]
    assert previous["source_locator"] == {"document_id": earlier_id, "page": 2}
    assert latest["source_locator"] == {"document_id": later_id, "page": 2}
    assert "2026-11-19" in previous["source"]
    assert "2026-11-20" in latest["source"]
    assert "2026-11-20" not in previous["source"]
    assert "2026-11-19" not in latest["source"]


# ---- registration -------------------------------------------------------------------


def test_futures_spread_is_a_read_only_registered_tool():
    assert "futures_spread" in PLATFORM_TOOLS
    capability = capabilities.REGISTRY["futures_spread"]
    assert capability.schema is capabilities.FuturesSpreadArgs
    assert capability.read_only is True
    assert capability.view == "futures"
    rows = {row["id"]: row for row in native_catalog()}
    assert "futures_spread" in rows
    schema = rows["futures_spread"]["input_schema"]
    assert set(schema["required"]) == {"symbol", "trading_day"}
    assert schema["properties"]["trading_day"]["pattern"] == r"^\d{4}-\d{2}-\d{2}$"
