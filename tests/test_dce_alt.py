"""Synthetic 99期货 field shapes; no live request or upstream access claim."""

import asyncio
from datetime import date

import httpx
import pytest

from server import dce_alt

DAY = date(2026, 11, 20)
TOKEN = "_pcc_" + "a" * 170


@pytest.fixture(autouse=True)
def clear_token():
    dce_alt._TOKEN.update(value="", expires=0.0)


def _series(rows):
    return {"code": 0, "message": "成功", "data": {"total": len(rows), "list": rows}}


def _transport(handler):
    return httpx.MockTransport(handler)


def _client(handler):
    return httpx.AsyncClient(transport=_transport(handler))


def _serve(rows, seen, *, status=200, token_status=200):
    def handler(request):
        seen.append(request)
        if request.url.path == "/app/common/v.js":
            if token_status != 200:
                return httpx.Response(token_status, content=b"blocked")
            return httpx.Response(
                200,
                headers={"_pcc": TOKEN, "_pcc_expire": "1797953247303"},
                content=b"",
            )
        return httpx.Response(status, json=rows(), headers={"content-encoding": "identity"})

    return handler


async def test_reads_dated_series_with_token_and_keeps_lot_unit():
    seen = []
    rows = [
        ["2026-11-20", "3071", 45559],
        ["2026-11-19", "3119", 46193],
        ["2026-11-18", None, 48596],
    ]
    async with _client(_serve(lambda: _series(rows), seen)) as client:
        value = await dce_alt.read_inventory(client, "M", DAY, asyncio.get_running_loop().time() + 2)
    assert value["status"] == "collected"
    assert value["quantity"] == 45559 and value["unit"] == "手"
    assert value["change"] == -634 and value["change_basis"] == "delta_vs_previous_disclosed_row"
    assert value["data_as_of"] == "2026-11-20" and value["date_provenance"] == "reported_date"
    assert value["source_name"] == "99期货" and value["source_kind"] == "secondary_warehouse_series"
    assert value["table"] == "豆粕" and value["row"] == "仓单数量"
    assert value["source_url"].startswith(dce_alt.SERIES_URL)
    token_request, series_request = seen
    assert token_request.url.path == "/app/common/v.js"
    assert token_request.headers["referer"] == "https://www.99qh.com/"
    assert "_pcc" not in token_request.headers
    assert series_request.headers["_pcc"] == TOKEN
    assert series_request.headers["site-channel"] == "003"
    assert dict(series_request.url.params)["productId"] == "63"
    assert dict(series_request.url.params)["endDate"] == "2026-11-20"
    assert dict(series_request.url.params)["startDate"] == "2026-09-21"


async def test_token_is_reused_until_its_expiry():
    seen = []
    rows = [["2026-11-20", "3071", 10]]
    async with _client(_serve(lambda: _series(rows), seen)) as client:
        for _ in range(2):
            await dce_alt.read_inventory(client, "C", DAY, asyncio.get_running_loop().time() + 2)
    assert sum(request.url.path == "/app/common/v.js" for request in seen) == 1


async def test_missing_requested_day_is_reported_and_never_filled():
    seen = []
    rows = [["2026-11-19", "3119", 46193]]
    async with _client(_serve(lambda: _series(rows), seen)) as client:
        value = await dce_alt.read_inventory(client, "M", DAY, asyncio.get_running_loop().time() + 2)
    assert value["status"] == "unavailable" and value["error_code"] == "report_not_found"
    assert "quantity" not in value and value["requested_date"] == DAY.isoformat()


async def test_empty_series_is_a_missing_report():
    seen = []
    async with _client(_serve(lambda: _series([]), seen)) as client:
        value = await dce_alt.read_inventory(client, "I", DAY, asyncio.get_running_loop().time() + 2)
    assert value["status"] == "unavailable" and value["error_code"] == "report_not_found"


async def test_three_year_bounds_fall_back_to_the_full_bounded_series():
    seen = []
    rows = [
        ["2026-11-20", "8190", 2223],
        ["2026-11-19", "8184", 2310],
    ]

    def handler(request):
        seen.append(request)
        if request.url.path == "/app/common/v.js":
            return httpx.Response(200, headers={"_pcc": TOKEN}, content=b"")
        if request.url.params.get("endDate"):
            return httpx.Response(200, json={"code": 100010, "message": "非会员用户只可以查看3年内的数据"})
        assert request.url.params["pageSize"] == "5000"
        assert request.url.params["startDate"] == "" and request.url.params["endDate"] == ""
        return httpx.Response(200, json=_series(rows))

    async with _client(handler) as client:
        value = await dce_alt.read_inventory(client, "P", DAY, asyncio.get_running_loop().time() + 2)
    assert value["status"] == "collected" and value["change"] == -87
    assert "pageSize=5000" in value["source_url"]


async def test_challenge_is_retried_once_then_reported_without_echoing_content():
    seen = []
    secret = "upstream-challenge-must-not-be-returned"

    def handler(request):
        seen.append(request)
        if request.url.path == "/app/common/v.js":
            return httpx.Response(200, headers={"_pcc": TOKEN}, content=b"")
        return httpx.Response(412, content=secret.encode(), headers={"server": "******"})

    async with _client(handler) as client:
        value = await dce_alt.read_inventory(client, "M", DAY, asyncio.get_running_loop().time() + 2)
    assert value["status"] == "unavailable"
    assert value["error_code"] == "source_access_denied" and value["http_status"] == 412
    assert value["source_url"].startswith(dce_alt.SERIES_URL)
    assert secret not in str(value)
    assert sum(request.url.path != "/app/common/v.js" for request in seen) == 2


async def test_first_disclosed_row_has_no_change():
    seen = []
    rows = [["2026-11-20", "3071", 45559]]
    async with _client(_serve(lambda: _series(rows), seen)) as client:
        value = await dce_alt.read_inventory(client, "M", DAY, asyncio.get_running_loop().time() + 2)
    assert value["status"] == "collected"
    assert value["quantity"] == 45559 and value["change"] is None and value["change_basis"] is None


@pytest.mark.parametrize(
    "body",
    [
        b'{"code":1,"data":null}',
        b'{"code":0,"data":{"total":1,"list":[["2026-11-20",null,true]]}}',
        b'{"code":0,"data":{"total":1,"list":[["2026-11-20",null,-1]]}}',
        b'{"code":0,"data":{"total":1,"list":[["2026/11/20",null,1]]}}',
        b"not json",
    ],
)
async def test_malformed_payload_is_a_structured_failure(body):
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/app/common/v.js":
            return httpx.Response(200, headers={"_pcc": TOKEN}, content=b"")
        return httpx.Response(200, content=body, headers={"content-encoding": "identity"})

    async with _client(handler) as client:
        value = await dce_alt.read_inventory(client, "C", DAY, asyncio.get_running_loop().time() + 2)
    assert value["status"] == "unavailable" and value["error_code"] == "invalid_source_data"


async def test_oversized_or_encoded_payload_is_refused():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/app/common/v.js":
            return httpx.Response(200, headers={"_pcc": TOKEN}, content=b"")
        return httpx.Response(
            200, content=b"x" * (dce_alt.STREAM_LIMIT + 1), headers={"content-encoding": "identity"}
        )

    async with _client(handler) as client:
        value = await dce_alt.read_inventory(client, "I", DAY, asyncio.get_running_loop().time() + 2)
    assert value["status"] == "unavailable" and value["error_code"] == "invalid_source_data"


async def test_unknown_symbol_is_not_covered_without_requests():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(500)

    async with _client(handler) as client:
        value = await dce_alt.read_inventory(client, "RB", DAY, asyncio.get_running_loop().time() + 2)
    assert value == {"status": "not_covered"} and not seen
