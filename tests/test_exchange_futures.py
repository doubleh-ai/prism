"""Exchange adapters preserve actual dates, contract identity and ranking columns."""

import asyncio
import json
from datetime import date

import httpx
import pytest

from server import exchange_futures as ex

DAY = date(2026, 11, 2)


def quotes():
    return {
        "o_code": "0000",
        "report_date": "20261102",
        "o_curinstrument": [
            {
                "PRODUCTID": "rb_f",
                "PRODUCTNAME": "螺纹钢",
                "DELIVERYMONTH": "2611",
                "CLOSEPRICE": 2995,
                "SETTLEMENTPRICE": 2999,
                "VOLUME": 370,
                "OPENINTEREST": 2940,
            },
            {
                "PRODUCTID": "rb_f",
                "PRODUCTNAME": "螺纹钢",
                "DELIVERYMONTH": "2701",
                "CLOSEPRICE": 3021,
                "SETTLEMENTPRICE": 3035,
                "VOLUME": 1596,
                "OPENINTEREST": 8395,
            },
            {"PRODUCTID": "rb_f", "DELIVERYMONTH": "小计"},
            {"PRODUCTID": "rb_tas", "DELIVERYMONTH": "2701"},
        ],
    }


def rankings():
    rows = []
    for rank in (1, 2):
        row = {"INSTRUMENTID": "rb2701", "RANK": rank}
        for col in (1, 2, 3):
            row.update(
                {
                    f"PARTICIPANTID{col}": f"{col}-{rank}",
                    f"PARTICIPANTABBR{col}": f"合成会员{col}-{rank}",
                    f"CJ{col}": col * 100 + rank,
                    f"CJ{col}_CHG": -col * rank,
                }
            )
        rows.append(row)
    rows.append({"INSTRUMENTID": "rb2701", "RANK": 999, "CJ2": 999999999})
    rows.append({"INSTRUMENTID": "rball", "RANK": -1, "CJ2": 999999999})
    return {"o_code": "0000", "report_date": "20261102", "o_cursor": rows}


def encode(value):
    return json.dumps(value).encode()


def test_curve_is_dated_contract_settlement_not_spot_basis():
    result = ex.parse_contracts(encode(quotes()), "RB", DAY)
    assert result["active_contract"] == "rb2701" and len(result["contracts"]) == 2
    assert result["curve"]["far_minus_near"] == 36
    assert result["curve"]["shape"] == "contango"
    assert result["basis"]["status"] == "not_collected"
    assert result["contract_risk"]["status"] == "not_evaluated"
    assert result["data_as_of"] == DAY.isoformat()


@pytest.mark.parametrize(
    "change", ["date", "product", "duplicate", "month", "nan", "negative", "bool", "empty"]
)
def test_contract_mismatch_is_not_silently_calculated(change):
    value = quotes()
    if change == "date":
        value["report_date"] = "20261101"
    if change == "product":
        value["o_curinstrument"][0]["PRODUCTNAME"] = "铜"
    if change == "duplicate":
        value["o_curinstrument"].append(value["o_curinstrument"][0])
    if change == "month":
        value["o_curinstrument"][0]["DELIVERYMONTH"] = "2613"
    if change == "nan":
        value["o_curinstrument"][0]["SETTLEMENTPRICE"] = float("nan")
    if change == "negative":
        value["o_curinstrument"][0]["VOLUME"] = -1
    if change == "bool":
        value["o_curinstrument"][0]["OPENINTEREST"] = True
    if change == "empty":
        value["o_curinstrument"] = []
    with pytest.raises(ValueError):
        ex.parse_contracts(encode(value), "RB", DAY)


def test_members_remain_in_separate_lists_and_summary_is_never_double_counted():
    result = ex.parse_rankings(encode(rankings()), "RB", DAY, "rb2701")
    assert result["status"] == "collected"
    assert result["sides"]["long"]["quantity_sum"] == 403
    assert result["sides"]["short"]["quantity_sum"] == 603
    assert result["sides"]["long"]["change_sum"] == -6
    assert result["sides"]["long"]["members"][0]["member"] != result["sides"]["short"]["members"][0]["member"]
    assert "net_capital_flow" not in result
    assert ex.parse_rankings(encode(rankings()), "RB", DAY, "rb2612")["status"] == "not_reported"


@pytest.mark.parametrize("change", ["date", "duplicate", "rank_gap", "member_duplicate", "negative", "bool"])
def test_bad_rankings_are_unavailable_not_zero(change):
    value = rankings()
    if change == "date":
        value["report_date"] = "20261101"
    if change == "duplicate":
        value["o_cursor"].append(value["o_cursor"][0])
    if change == "rank_gap":
        value["o_cursor"][1]["RANK"] = 3
    if change == "member_duplicate":
        value["o_cursor"][1]["PARTICIPANTID2"] = "2-1"
    if change == "negative":
        value["o_cursor"][0]["CJ2"] = -1
    if change == "bool":
        value["o_cursor"][0]["CJ2"] = True
    with pytest.raises(ValueError):
        ex.parse_rankings(encode(value), "RB", DAY, "rb2701")


async def test_unavailable_rankings_keep_curve_and_no_redirect_or_arbitrary_host():
    seen = []

    def serve(request):
        seen.append(str(request.url))
        assert request.url.host == "www.shfe.com.cn"
        return (
            httpx.Response(200, json=quotes())
            if "/kx" in request.url.path
            else httpx.Response(302, headers={"location": "https://untrusted.invalid/"})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve), follow_redirects=False) as client:
        result = await ex.read_daily(client, "RB", DAY, asyncio.get_running_loop().time() + 2)
        assert result["status"] == "collected" and result["member_positioning"]["status"] == "unavailable"
        expired = await ex.read_daily(client, "M", DAY, 0)
        assert expired["status"] == "unavailable" and expired["error_code"] == "source_timeout"
        assert expired["member_positioning"]["status"] == "not_covered"
    assert len(seen) == 2


async def test_expired_deadline_and_partial_or_oversize_json_fail_closed():
    def serve(request):
        return httpx.Response(200, content=b"[" * (ex.LIMIT + 1))

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        value = await ex.read_daily(client, "RB", DAY, asyncio.get_running_loop().time() + 1)
        assert value["status"] == "unavailable"
        value = await ex.read_daily(client, "RB", DAY, asyncio.get_running_loop().time() - 1)
        assert value["status"] == "unavailable"
