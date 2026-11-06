"""Actual HTTP parsing with synthetic sources; independent failures retain data."""

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import HTTPException

from server import futures
from server.data_availability import source_failure

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture(autouse=True)
def isolated_market(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(futures, "datetime", Clock)
    monkeypatch.setattr(futures, "_cache", {})
    monkeypatch.setattr(futures, "_slots", asyncio.Semaphore(2))


def install(monkeypatch, handler):
    original = httpx.AsyncClient

    def client(**options):
        assert options["trust_env"] is False and options["follow_redirects"] is False
        return original(**options, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(futures.httpx, "AsyncClient", client)


def quote(day):
    return {
        "o_code": "0000",
        "report_date": day.strftime("%Y%m%d"),
        "o_curinstrument": [
            {
                "PRODUCTID": "rb_f",
                "PRODUCTNAME": "螺纹钢",
                "DELIVERYMONTH": "2701",
                "CLOSEPRICE": 3500,
                "SETTLEMENTPRICE": 3490,
                "VOLUME": 50,
                "OPENINTEREST": 80,
            }
        ],
    }


def bars():
    return [
        {"d": day, "o": "3500", "h": "3520", "l": "3490", "c": close, "v": "100", "p": "80", "s": "3500"}
        for day, close in [("2026-10-30", "3500"), ("2026-11-02", "3510")]
    ]


@pytest.mark.parametrize("series_mode", ["http_failure", "invalid_series", "timeout"])
async def test_series_failure_still_reads_and_returns_actual_exchange_contracts(monkeypatch, series_mode):
    calls = []

    def handler(request):
        calls.append(request)
        assert "cookie" not in request.headers and "authorization" not in request.headers
        if request.url.host == "stock2.finance.sina.com.cn":
            if series_mode == "timeout":
                raise httpx.ReadTimeout("synthetic upstream diagnostic must stay private", request=request)
            return httpx.Response(
                500 if series_mode == "http_failure" else 200,
                json={"bad": "schema"},
                headers=[("set-cookie", "public_tracking=must-not-forward; Domain=.com.cn; Path=/")],
            )
        assert request.url.host == "www.shfe.com.cn"
        if "stockdata" in request.url.path:
            return httpx.Response(403)
        if "kx" in request.url.path:
            return httpx.Response(200, json=quote(NOW.date()))
        return httpx.Response(200, json={"o_code": "0000", "report_date": "20261103", "o_cursor": []})

    install(monkeypatch, handler)
    value = await futures.snapshot("RB")
    assert value["status"] == "partial" and value["series_status"] == "unavailable"
    assert value["data_as_of"] is None and value["latest"]["close"] is None
    assert value["technical"]["sma20"] is None and value["positioning"]["open_interest_change"] is None
    assert value["bars"] == [] and value["positioning"]["net_capital_flow"] is None
    assert value["exchange_daily"]["contracts"][0]["settlement"] == 3490
    assert value["exchange_daily"]["data_as_of"] == "2026-11-03"
    assert value["warehouse_receipt"]["error_code"] == "access_denied"
    assert len(calls) == 6 and value["cache_ttl_seconds"] == 30
    assert value["contract_dates"]["status"] == value["exchange_parameters"]["status"] == "unavailable"
    assert value["report_date_basis"] == "bounded_calendar_window"
    from server import capabilities

    result, target, summary = await capabilities._execute(
        "futures_snapshot", capabilities.FuturesArgs(symbol="RB"), {"username": "synthetic-owner"}
    )
    assert result["latest"]["close"] is None and result["status"] == "partial"
    assert result["exchange_daily"]["contracts"][0]["settlement"] == 3490
    assert target["href"] == "/futures?symbol=RB"
    assert "部分" in summary and "连续日线" not in summary
    assert len(calls) == 6


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (404, "report_not_found"),
        (401, "access_denied"),
        (403, "access_denied"),
        (429, "http_error"),
        (500, "http_error"),
        (302, "http_error"),
    ],
)
def test_status_metadata_excludes_error_text_headers_and_response_body(status, code):
    marker = "synthetic-sensitive-diagnostic"
    response = httpx.Response(
        status,
        text=marker,
        headers={"set-cookie": marker},
        request=httpx.Request("GET", "https://example.test/"),
    )
    error = httpx.HTTPStatusError(marker, request=response.request, response=response)
    assert source_failure(error) == {"error_code": code, "http_status": status}


def test_local_timeout_and_invalid_source_metadata_are_finite():
    marker = "synthetic-sensitive-diagnostic"
    assert source_failure(TimeoutError(marker)) == {"error_code": "source_timeout"}
    assert source_failure(httpx.ConnectError(marker)) == {"error_code": "network_error"}
    assert source_failure(ValueError(marker)) == {"error_code": "invalid_source_data"}


async def test_dated_404_queries_an_earlier_report_without_overwriting_a_collected_source(monkeypatch):
    install(monkeypatch, lambda request: httpx.Response(500))
    calls = []

    async def warehouse(client, symbol, day, deadline):
        calls.append(("warehouse", day))
        return {"status": "collected", "data_as_of": day.isoformat(), "quantity": 7, "unit": "吨"}

    async def daily(client, symbol, day, deadline):
        calls.append(("daily", day))
        if day == NOW.date():
            return {"status": "unavailable", "error_code": "report_not_found"}
        return {
            "status": "collected",
            "data_as_of": day.isoformat(),
            "member_positioning": {"status": "not_reported"},
        }

    monkeypatch.setattr(futures.warehouse_receipts, "read_report", warehouse)
    monkeypatch.setattr(futures.exchange_futures, "read_daily", daily)
    value = await futures.snapshot("RB")
    assert calls == [
        ("warehouse", NOW.date()),
        ("daily", NOW.date()),
        ("daily", NOW.date() - timedelta(days=1)),
    ]
    assert value["warehouse_receipt"]["data_as_of"] == "2026-11-03"
    assert value["exchange_daily"]["data_as_of"] == "2026-11-02"
    assert value["date_alignment"] == "mixed" and value["observation_dates"] == ["2026-11-02", "2026-11-03"]
    assert value["latest"]["close"] is None
    assert [x["requested_date"] for x in value["source_attempts"]] == [
        "2026-11-03",
        "2026-11-03",
        "2026-11-03",
        "2026-11-03",
        "2026-11-02",
    ]


@pytest.mark.parametrize(
    "failure", ["network_error", "access_denied", "invalid_source_data", "source_timeout"]
)
async def test_network_access_or_schema_failures_do_not_trigger_date_fallback(monkeypatch, failure):
    install(monkeypatch, lambda request: httpx.Response(500))
    calls = []

    async def unavailable(client, symbol, day, deadline):
        calls.append(day)
        return {"status": "unavailable", "error_code": failure}

    monkeypatch.setattr(futures.warehouse_receipts, "read_report", unavailable)
    monkeypatch.setattr(futures.exchange_futures, "read_daily", unavailable)
    with pytest.raises(HTTPException) as result:
        await futures.snapshot("RB")
    assert result.value.status_code == 503 and result.value.headers["X-Prism-Data-Status"] == "unavailable"
    assert calls == [NOW.date(), NOW.date()] and futures._cache == {}


async def test_missing_reports_have_a_bounded_three_date_window_and_never_invent_a_trading_date(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(500 if request.url.host == "stock2.finance.sina.com.cn" else 404)

    install(monkeypatch, handler)
    with pytest.raises(HTTPException) as result:
        await futures.snapshot("RB")
    assert result.value.status_code == 503 and len(requests) == 13
    urls = [str(r.url) for r in requests]
    assert len(urls) == len(set(urls)) and futures._cache == {}
    assert all(any(f"2026110{day}" in str(r.url) for day in (1, 2, 3)) for r in requests[1:])


async def test_stale_continuous_date_does_not_prevent_reading_newer_official_reports(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == "stock2.finance.sina.com.cn":
            return httpx.Response(200, json=bars())
        if "kx" in request.url.path:
            return httpx.Response(200, json=quote(NOW.date()))
        return httpx.Response(403)

    install(monkeypatch, handler)
    value = await futures.snapshot("RB")
    assert value["series_status"] == "collected" and value["data_as_of"] == "2026-11-02"
    assert value["latest"]["close"] == 3510 and len(requests) == 6
    assert value["exchange_daily"]["data_as_of"] == "2026-11-03"
    assert value["report_date_basis"] == "bounded_calendar_window"
    assert all(item["requested_date"] == "2026-11-03" for item in value["source_attempts"])
    assert value["date_alignment"] == "mixed"


async def test_partial_cache_is_short_lived_and_cannot_be_changed_by_a_caller(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return (
            httpx.Response(200, json=bars())
            if request.url.host == "stock2.finance.sina.com.cn"
            else httpx.Response(403)
        )

    install(monkeypatch, handler)
    first = await futures.snapshot("RB")
    first["latest"]["close"] = 999999
    second = await futures.snapshot("RB")
    assert second["cached"] is True and second["latest"]["close"] == 3510
    assert len(calls) == 5
    _, value = futures._cache["RB"]
    futures._cache["RB"] = (futures.time.monotonic() - 31, value)
    third = await futures.snapshot("RB")
    assert third["cached"] is False and len(calls) == 10


async def test_cancelled_series_does_not_start_official_requests_or_populate_cache(monkeypatch):
    entered = asyncio.Event()
    requests = []

    async def handler(request):
        requests.append(request)
        entered.set()
        await asyncio.Event().wait()

    install(monkeypatch, handler)
    task = asyncio.create_task(futures.snapshot("RB"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(requests) == 1 and futures._cache == {}


async def test_only_calendar_source_is_retained_and_shared_with_native_tool(monkeypatch):
    from server import capabilities
    from tests.test_futures_parameters import document

    calls = []

    def handler(request):
        calls.append(request)
        if "ContractBaseInfo20261103" in request.url.path:
            return httpx.Response(404)
        if "ContractBaseInfo20261102" in request.url.path:
            return httpx.Response(200, json=document("contract_dates"))
        return httpx.Response(403)

    install(monkeypatch, handler)
    value = await futures.snapshot("RB")
    assert value["status"] == "partial" and value["latest"]["close"] is None
    assert value["contract_dates"]["contracts"][0]["listed_expiry_date"] == "2027-01-15"
    assert value["observation_dates"] == ["2026-11-02"] and value["date_alignment"] == "not_compared"
    assert value["exchange_parameters"]["error_code"] == "access_denied"
    assert len(calls) == 6 and len({str(r.url) for r in calls}) == 6
    actual, _, summary = await capabilities._execute(
        "futures_snapshot", capabilities.FuturesArgs(symbol="RB"), {"username": "synthetic-owner"}
    )
    assert actual["contract_dates"] == value["contract_dates"] and len(calls) == 6
    assert "部分" in summary and actual["portfolio_risk_status"] == "not_evaluated"


async def test_parameter_files_keep_separate_observation_dates_and_404_scope(monkeypatch):
    from tests.test_futures_parameters import document

    def handler(request):
        if "ContractBaseInfo20261103" in request.url.path:
            return httpx.Response(404)
        if "ContractBaseInfo20261102" in request.url.path:
            return httpx.Response(200, json=document("contract_dates"))
        if "ContractDailyTradeArgument20261103" in request.url.path:
            doc = document("exchange_parameters")
            doc["report_date"], doc["update_date"] = "20261103", "20261103 16:20:06"
            doc["ContractDailyTradeArgument"][0]["TRADINGDAY"] = "20261103"
            return httpx.Response(200, json=doc)
        return httpx.Response(403)

    install(monkeypatch, handler)
    value = await futures.snapshot("RB")
    assert value["contract_dates"]["data_as_of"] == "2026-11-02"
    assert value["exchange_parameters"]["data_as_of"] == "2026-11-03"
    assert value["date_alignment"] == "mixed" and value["observation_dates"] == ["2026-11-02", "2026-11-03"]
    requests = value["source_attempts"]
    assert [r["source"] for r in requests if r["requested_date"] == "2026-11-02"] == ["contract_dates"]
