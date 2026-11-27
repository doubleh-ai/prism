"""Futures snapshots as checkable sources for report figures.

A futures report quotes prices, volumes and open interest, but a platform tool
result is not a document the ledger can point at. The futures module renders one
snapshot into source- and date-labelled pages, the ledger stores that rendering as
an obtained original, and the existing page matcher then binds the report's own
numbers to those pages. These tests pin all three properties: a quoted price
verifies, a figure absent from the snapshot stays unverified without tripping the
evidence gate, and two snapshots from different days never cross-bind.
"""

from datetime import UTC, datetime

from server.database import get_db
from server.futures import snapshot_document
from server.research import (
    create_research_run,
    finalize_research_run,
    ingest_market_snapshot,
    read_research_run,
)
from server.research_context import build_research_context


def _user(username="alice"):
    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO users(username,password_hash) VALUES(?,?)",
            [username, "synthetic-q3-user"],
        )


def _run(run_id):
    _user()
    context = build_research_context(run_id=run_id, now=datetime(2026, 11, 21, 18, tzinfo=UTC))
    create_research_run("alice", "", "研究合成期货品种的行情与持仓数据", context)


def _snapshot(symbol="RB", day="2026-11-20", close=3125.0):
    name, exchange = {"RB": ("螺纹钢", "SHFE"), "CU": ("铜", "SHFE")}[symbol]
    return {
        "symbol": symbol,
        "name": name,
        "exchange": exchange,
        "status": "partial",
        "series_status": "collected",
        "tradable_contract": False,
        "data_as_of": day,
        "collected_at": day + "T15:30:00+08:00",
        "source": {
            "name": "新浪财经",
            "kind": "secondary_market_data",
            "url": f"https://finance.sina.com.cn/futures/quotes/{symbol}0.shtml",
            "status": "collected",
            "original_verified": False,
        },
        "latest": {
            "date": day,
            "open": close - 25.0,
            "high": close + 25.0,
            "low": close - 35.0,
            "close": close,
            "volume": 123456.0,
            "open_interest": 789012.0,
            "settlement": close - 5.0,
        },
        "technical": {
            "sma20": close - 20.0,
            "sma60": close - 50.0,
            "change_pct": 1.25,
            "return_5_observations_pct": 2.5,
            "range20_low": close - 100.0,
            "range20_high": close + 100.0,
        },
        "positioning": {"open_interest_change": 3456.0},
        "exchange_daily": {
            "status": "collected",
            "data_as_of": day,
            "source_url": "https://www.shfe.com.cn/data/dailydata/kx/kx20261120.dat",
            "contracts": [
                {
                    "contract": symbol.lower() + "2601",
                    "delivery_month": "2026-01",
                    "close": close,
                    "settlement": close - 5.0,
                    "volume": 100000.0,
                    "open_interest": 500000.0,
                }
            ],
            "curve": {
                "status": "collected",
                "near_contract": symbol.lower() + "2601",
                "far_contract": symbol.lower() + "2605",
                "near_settlement": close - 5.0,
                "far_settlement": close + 75.0,
                "far_minus_near": 80.0,
            },
            "member_positioning": {
                "status": "collected",
                "data_as_of": day,
                "sides": {
                    "volume": {"status": "collected", "quantity_sum": 80000.0},
                    "long": {"status": "collected", "quantity_sum": 450000.0},
                    "short": {"status": "collected", "quantity_sum": 460000.0},
                },
            },
        },
        "warehouse_receipt": {
            "status": "collected",
            "data_as_of": day,
            "source_url": "https://www.shfe.com.cn/data/tradedata/future/stockdata/dailystock_20261120/ZH/all.html",
            "table": "螺纹钢(仓库)",
            "row": "总计",
            "unit": "吨",
            "quantity": 23456.0,
            "change": -345.0,
        },
        "contract_dates": {
            "status": "collected",
            "data_as_of": day,
            "source_url": "https://www.shfe.com.cn/data/busiparamdata/future/ContractBaseInfo20261120.dat",
            "contracts": [
                {
                    "contract": symbol.lower() + "2601",
                    "listed_on": "2025-01-16",
                    "listed_expiry_date": "2026-01-15",
                    "delivery_start": "2026-01-16",
                    "delivery_end": "2026-01-22",
                }
            ],
        },
        "exchange_parameters": {
            "status": "collected",
            "data_as_of": day,
            "source_url": "https://www.shfe.com.cn/data/busiparamdata/future/ContractDailyTradeArgument20261120.dat",
            "ratio_unit": "percent",
            "contracts": [
                {
                    "contract": symbol.lower() + "2601",
                    "spec_long_margin_pct": 7.0,
                    "spec_short_margin_pct": 7.0,
                    "hedge_long_margin_pct": 7.0,
                    "hedge_short_margin_pct": 7.0,
                    "upper_limit_pct": 8.0,
                    "lower_limit_pct": 8.0,
                }
            ],
        },
        "gaps": [],
        "notice": "",
    }


def _ingest(run_id, snapshot, call_id=None):
    call = {
        "type": "tool_use",
        "id": call_id or f"call-{run_id}-{snapshot['data_as_of']}-{snapshot['symbol']}",
        "name": "futures_snapshot",
        "input": {"symbol": snapshot["symbol"]},
    }
    document = snapshot_document(snapshot)
    assert document is not None
    return ingest_market_snapshot("alice", run_id, call, document)


def _claims_by_statement(stored):
    return {claim["statement"]: claim for claim in stored["claims"]}


# ---- test 1: a quoted futures price binds to the snapshot page ----------------------------


def test_report_price_matches_snapshot_document_page():
    _run("run-futures-price")
    stored_document = _ingest("run-futures-price", _snapshot())
    assert stored_document["document"]["original_obtained"] is True
    assert stored_document["document"]["source_kind"] == "market_data_snapshot"
    assert stored_document["document"]["data_period"] == "2026-11-20"
    assert len(stored_document["passages"]) == 5

    report = "螺纹钢 RB 收盘价 3125 元/吨，较前一交易日上涨 1.25%，成交量 123456 手，持仓量 789012 手。"
    revision = finalize_research_run("alice", "run-futures-price", report)
    assert revision["acceptance_status"] != "blocked"
    stored = read_research_run("alice", "run-futures-price")
    claims = _claims_by_statement(stored)
    document_id = stored_document["document"]["document_id"]

    price = claims["报告正文数字：3125 元/吨"]
    assert price["verification"] == "verified_source"
    assert price["origin"] == "server_locator"
    assert price["source_locator"] == {"document_id": document_id, "page": 1}
    assert "新浪财经" in price["source"]
    assert "2026-11-20" in price["source"]
    assert price["published_at"] == "2026-11-20T00:00:00+08:00"
    for display in ("123456 手", "789012 手"):
        assert claims[f"报告正文数字：{display}"]["verification"] == "verified_source"
    # A percentage is admitted to the locator like any other measurement when the
    # obtained page itself states it (涨跌幅 1.25%), so the citation becomes complete
    # instead of depending on the model claim pass.
    percent = claims["报告正文数字：1.25%"]
    assert percent["verification"] == "verified_source"
    assert percent["origin"] == "server_locator"
    assert percent["source_locator"] == {"document_id": document_id, "page": 1}
    assert revision["claim_reconciliation"]["verified_claim_ratio"] == 1.0
    assert revision["claim_reconciliation"]["report_numbers_checked"] == 4


# ---- test 2: a figure absent from the snapshot stays unverified, gate stays open ---------


def test_number_absent_from_snapshot_stays_unverified_without_blocking():
    _run("run-futures-miss")
    _ingest("run-futures-miss", _snapshot(symbol="RB"))
    report = "螺纹钢 RB 收盘价 3125 元/吨；铜 CU 收盘价 71000 元/吨。"
    revision = finalize_research_run("alice", "run-futures-miss", report)
    # The snapshot is an obtained original, so the financial-number gate opens even
    # though one quoted product has no page in it.
    assert revision.get("error_code") != "original_required"
    assert revision["acceptance_status"] != "blocked"
    assert revision["narrative"]
    stored = read_research_run("alice", "run-futures-miss")
    claims = _claims_by_statement(stored)
    copper = claims["报告正文数字：71000 元/吨"]
    assert copper["verification"] == "unverified"
    assert copper["source_locator"] is None
    assert copper["locator_attempt"] == "not_found"
    assert claims["报告正文数字：3125 元/吨"]["verification"] == "verified_source"


# ---- test 3: two snapshot dates never cross-bind ----------------------------------------


def test_two_snapshot_dates_do_not_cross_bind():
    _run("run-futures-dates")
    earlier = _ingest("run-futures-dates", _snapshot(day="2026-11-19", close=2999.0))
    later = _ingest("run-futures-dates", _snapshot(day="2026-11-20", close=3125.0))
    earlier_id = earlier["document"]["document_id"]
    later_id = later["document"]["document_id"]
    assert earlier_id != later_id

    report = "前一交易日螺纹钢 RB 收盘价 2999 元/吨；最新交易日收盘价 3125 元/吨。"
    finalize_research_run("alice", "run-futures-dates", report)
    claims = _claims_by_statement(read_research_run("alice", "run-futures-dates"))
    previous = claims["报告正文数字：2999 元/吨"]
    latest = claims["报告正文数字：3125 元/吨"]
    assert previous["source_locator"] == {"document_id": earlier_id, "page": 1}
    assert latest["source_locator"] == {"document_id": later_id, "page": 1}
    assert "2026-11-19" in previous["source"]
    assert "2026-11-20" in latest["source"]
    assert "2026-11-20" not in previous["source"]
    assert "2026-11-19" not in latest["source"]
