"""Synthetic documented DCE fields; no live report or upstream access claim."""

import asyncio
import copy
import json
from datetime import date

import httpx
import pytest

from server import dce_futures as dce
from server import exchange_futures, futures, warehouse_receipts

DAY = date(2026, 11, 6)


def daily():
    return {
        "code": 200,
        "data": [
            {
                "variety": "豆粕",
                "contractId": "m2611",
                "close": "3,101",
                "clearPrice": "3100",
                "volumn": "100",
                "openInterest": "200",
            },
            {
                "variety": "豆粕",
                "contractId": "m2701",
                "close": "3210",
                "clearPrice": "3,200",
                "volumn": "1,000",
                "openInterest": "900",
            },
            {"variety": "豆粕小计", "contractId": "m小计", "volumn": 9999999},
            {"variety": "玉米", "contractId": "c2701", "volumn": 9999999},
        ],
    }


def warehouse():
    return {
        "code": 200,
        "data": {
            "entityList": [
                {"variety": "豆粕", "whAbbr": "合成仓库", "wbillQty": 12, "lastWbillQty": 10, "diff": 2},
                {"variety": "豆粕小计", "whAbbr": "", "wbillQty": 30, "lastWbillQty": 28, "diff": 2},
                {"variety": "玉米小计", "whAbbr": "", "wbillQty": 999, "lastWbillQty": 0, "diff": 999},
            ]
        },
    }


def raw(value):
    return json.dumps(value, ensure_ascii=False).encode()


def test_daily_filters_real_contracts_and_keeps_date_and_transport_provenance():
    value = dce.parse_daily(raw(daily()), DAY)
    assert value["active_contract"] == "m2701"
    assert [v["contract"] for v in value["contracts"]] == ["m2611", "m2701"]
    assert value["curve"]["far_minus_near"] == 100
    assert value["date_provenance"] == "request_parameter"
    assert not value["transport_authenticated"] and not value["original_verified"]
    assert value["member_positioning"]["status"] == "not_covered"
    assert value["basis"]["status"] == "not_collected"
    assert "net_capital_flow" not in value and "响应未单列日期" in value["notice"]
    dated = daily()
    dated["tradeDate"] = "20261106"
    assert dce.parse_daily(raw(dated), DAY)["date_provenance"] == "reported_date"


@pytest.mark.parametrize(
    "fault",
    [
        "date",
        "duplicate",
        "month",
        "missing_count",
        "negative",
        "boolean",
        "nan",
        "grouping",
        "wrong_product",
        "refused",
        "invalid_success",
        "empty",
    ],
)
def test_invalid_daily_values_never_become_zero_or_an_active_contract(fault):
    value = daily()
    row = value["data"][0]
    if fault == "date":
        value["tradeDate"] = "20261105"
    elif fault == "duplicate":
        value["data"].append(copy.deepcopy(row))
    elif fault == "month":
        row["contractId"] = "m2613"
    elif fault == "missing_count":
        row.pop("openInterest")
    elif fault == "negative":
        row["volumn"] = -1
    elif fault == "boolean":
        row["volumn"] = True
    elif fault == "nan":
        row["clearPrice"] = float("nan")
    elif fault == "grouping":
        row["close"] = "31,00"
    elif fault == "wrong_product":
        value["data"] = value["data"][2:]
    elif fault == "refused":
        value["code"] = 500
    elif fault == "invalid_success":
        value["success"] = "false"
    else:
        value["data"] = []
    with pytest.raises(ValueError):
        dce.parse_daily(raw(value), DAY)


def test_warehouse_requires_one_labelled_total_and_preserves_reported_lot_unit():
    value = dce.parse_warehouse(raw(warehouse()), DAY)
    assert value["quantity"] == 30 and value["change"] == 2 and value["unit"] == "手"
    assert value["row"] == "豆粕小计" and value["date_provenance"] == "request_parameter"
    assert "未换算吨数" in value["notice"]


@pytest.mark.parametrize(
    "fault", ["duplicate", "no_total", "negative", "change", "date", "boolean", "envelope"]
)
def test_warehouse_never_adds_summary_and_warehouse_rows_together(fault):
    value = warehouse()
    rows = value["data"]["entityList"]
    row = rows[1]
    if fault == "duplicate":
        rows.append(copy.deepcopy(row))
    elif fault == "no_total":
        rows.pop(1)
    elif fault == "negative":
        row["wbillQty"] = -1
    elif fault == "change":
        row["diff"] = 3
    elif fault == "date":
        value["data"]["tradeDate"] = "20261105"
    elif fault == "envelope":
        value["data"] = rows
    else:
        row["wbillQty"] = True
    with pytest.raises(ValueError):
        dce.parse_warehouse(raw(value), DAY)


def test_duplicate_json_keys_are_not_silently_overwritten():
    with pytest.raises(ValueError):
        dce.parse_daily(b'{"data":[],"data":[]}', DAY)


async def test_actual_readers_use_only_fixed_public_posts_without_credentials_or_redirects():
    seen = []

    def serve(request):
        seen.append(request)
        assert request.method == "POST" and request.url.host == "www.dce.com.cn"
        assert request.url.scheme == "http" and not request.url.query
        assert "cookie" not in request.headers and "authorization" not in request.headers
        body = json.loads(request.content)
        assert body["tradeDate"] == "20261106" and body["varietyId"] == "all"
        return httpx.Response(200, json=warehouse() if "wbill" in request.url.path else daily())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(serve), cookies={"synthetic": "not-forwarded"}
    ) as client:
        limit = asyncio.get_running_loop().time() + 2
        value = await exchange_futures.read_daily(client, "M", DAY, limit)
        receipt = await warehouse_receipts.read_report(client, "M", DAY, limit)
    assert value["status"] == receipt["status"] == "collected"
    assert len(seen) == 2 and value["member_positioning"]["status"] == "not_covered"


async def test_access_restriction_is_visible_and_does_not_query_earlier_dates():
    seen = []

    def serve(request):
        seen.append(request)
        return httpx.Response(412, content=b"upstream challenge content must not be returned")

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        reports, attempts = await futures._reports(client, "M", DAY, asyncio.get_running_loop().time() + 2)
    for key in ("exchange_daily", "warehouse_receipt"):
        assert reports[key]["status"] == "unavailable"
        assert (
            reports[key]["error_code"] == "automated_access_restricted" and reports[key]["http_status"] == 412
        )
    assert len(seen) == 2 and all(v["requested_date"] == DAY.isoformat() for v in attempts)
    assert "challenge content" not in json.dumps(reports)


@pytest.mark.parametrize("mode", ["basic", "authorization", "proxy-authorization", "cookie"])
async def test_client_authentication_is_rejected_before_any_public_http_request(mode):
    seen = []

    def serve(request):
        seen.append(request)
        return httpx.Response(200, json=daily())

    options = (
        {"auth": ("synthetic", "must-not-send")}
        if mode == "basic"
        else {"headers": {mode: "synthetic-must-not-send"}}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve), **options) as client:
        value = await dce.read_daily(client, DAY, asyncio.get_running_loop().time() + 2)
    assert value["status"] == "unavailable" and value["error_code"] == "invalid_source_data"
    assert not seen and "synthetic-must-not-send" not in json.dumps(value)


async def test_expired_budget_and_redirect_never_make_another_source_request():
    seen = []

    def serve(request):
        seen.append(request)
        return httpx.Response(302, headers={"location": "https://untrusted.invalid/collect"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve), follow_redirects=True) as client:
        expired = await dce.read_daily(client, DAY, 0)
        assert expired["error_code"] == "source_timeout" and not seen
        expired_warehouse = await warehouse_receipts.read_report(client, "M", DAY, 0)
        assert expired_warehouse["error_code"] == "source_timeout" and not seen
        value = await dce.read_daily(client, DAY, asyncio.get_running_loop().time() + 2)
    assert value["status"] == "unavailable" and len(seen) == 1
