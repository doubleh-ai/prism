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


def blank_column(row, column):
    for key in ("PARTICIPANTID", "PARTICIPANTABBR", "CJ"):
        row[f"{key}{column}"] = ""
    row[f"CJ{column}_CHG"] = ""


def test_independent_short_columns_preserve_other_disclosures_and_unknown_empty_totals():
    value = rankings()
    for row in value["o_cursor"][:2]:
        blank_column(row, 1)
    blank_column(value["o_cursor"][1], 3)
    result = ex.parse_rankings(encode(value), "RB", DAY, "rb2701")
    assert result["status"] == "collected"
    assert result["sides"]["volume"] == {
        "status": "not_reported",
        "ranked_members": 0,
        "quantity_sum": None,
        "change_sum": None,
        "members": [],
    }
    assert result["sides"]["long"]["quantity_sum"] == 403
    assert result["sides"]["short"]["ranked_members"] == 1
    assert result["sides"]["short"]["quantity_sum"] == 301


@pytest.mark.parametrize("fault", ["missing_name", "zero_quantity", "missing_change", "rank_hole"])
def test_incomplete_or_nonterminal_blanks_do_not_hide_corrupt_disclosures(fault):
    value = rankings()
    row = value["o_cursor"][0 if fault == "rank_hole" else 1]
    blank_column(row, 2)
    if fault == "missing_name":
        row["PARTICIPANTID2"] = "2-2"
    elif fault == "zero_quantity":
        row["CJ2"] = 0
    elif fault == "missing_change":
        del row["CJ2_CHG"]
    with pytest.raises(ValueError):
        ex.parse_rankings(encode(value), "RB", DAY, "rb2701")


def test_all_columns_unreported_never_produce_zero_position_totals():
    value = rankings()
    for row in value["o_cursor"][:2]:
        for column in (1, 2, 3):
            blank_column(row, column)
    result = ex.parse_rankings(encode(value), "RB", DAY, "rb2701")
    assert result["status"] == "not_reported"
    assert all(side["quantity_sum"] is None for side in result["sides"].values())


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


async def test_unpublished_night_session_rankings_keep_their_actual_previous_report_date():
    calls = []

    def serve(request):
        calls.append(request.url.path)
        if "/kx" in request.url.path:
            return httpx.Response(200, json=quotes())
        if request.url.path.endswith("pm20261102.dat"):
            return httpx.Response(404)
        assert request.url.path.endswith("pm20261101.dat")
        value = rankings()
        value["report_date"] = "20261101"
        return httpx.Response(200, json=value)

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        result = await ex.read_daily(client, "RB", DAY, asyncio.get_running_loop().time() + 2)
    assert len(calls) == 3
    assert result["data_as_of"] == "2026-11-02"
    member = result["member_positioning"]
    assert member["contract"] == result["active_contract"] == "rb2701"
    assert member["status"] == "collected" and member["data_as_of"] == "2026-11-01"
    assert member["source_url"].endswith("pm20261101.dat")
    assert member["source_attempts"] == [
        {
            "requested_date": "2026-11-02",
            "status": "unavailable",
            "error_code": "report_not_found",
            "http_status": 404,
        },
        {"requested_date": "2026-11-01", "status": "collected"},
    ]


@pytest.mark.parametrize("failure", ["404", "403", "429", "redirect", "schema", "network"])
async def test_rankings_discovery_only_moves_to_distinct_dates_after_404(failure):
    calls = []

    def serve(request):
        calls.append(request.url.path)
        if "/kx" in request.url.path:
            return httpx.Response(200, json=quotes())
        if failure == "network":
            raise httpx.ConnectError("synthetic private diagnostic", request=request)
        if failure == "schema":
            return httpx.Response(200, json={})
        if failure == "redirect":
            return httpx.Response(302, headers={"location": "https://untrusted.invalid/"})
        return httpx.Response(int(failure))

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve), follow_redirects=False) as client:
        result = await ex.read_daily(client, "RB", DAY, asyncio.get_running_loop().time() + 2)
    assert len(calls) == (4 if failure == "404" else 2)
    assert len(calls) == len(set(calls))
    assert result["member_positioning"]["status"] == "unavailable"
    assert len(result["member_positioning"]["source_attempts"]) == len(calls) - 1
    assert "synthetic private diagnostic" not in str(result)


async def test_undisclosed_contract_does_not_borrow_another_contract_or_an_older_ranking():
    calls = []

    def serve(request):
        calls.append(request.url.path)
        if "/kx" in request.url.path:
            return httpx.Response(200, json=quotes())
        value = rankings()
        for row in value["o_cursor"]:
            row["INSTRUMENTID"] = "rb2611"
        return httpx.Response(200, json=value)

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        result = await ex.read_daily(client, "RB", DAY, asyncio.get_running_loop().time() + 2)
    assert len(calls) == 2
    assert result["member_positioning"]["status"] == "not_reported"
    assert result["member_positioning"]["contract"] == "rb2701"


async def test_expired_deadline_and_partial_or_oversize_json_fail_closed():
    def serve(request):
        return httpx.Response(200, content=b"[" * (ex.LIMIT + 1))

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        value = await ex.read_daily(client, "RB", DAY, asyncio.get_running_loop().time() + 1)
        assert value["status"] == "unavailable"
        value = await ex.read_daily(client, "RB", DAY, asyncio.get_running_loop().time() - 1)
        assert value["status"] == "unavailable"
