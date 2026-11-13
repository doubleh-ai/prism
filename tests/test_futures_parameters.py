"""Public business reports keep units, dates and customer-risk boundaries."""

import asyncio
import json
from datetime import date

import httpx
import pytest

from server import futures_parameters as p

DAY = date(2026, 11, 2)


def document(kind, symbol="RB"):
    row = {"INSTRUMENTID": symbol.lower() + "2701", "TRADINGDAY": "20261102"}
    if kind == "contract_dates":
        row.update(
            COMMODITYID=symbol.lower(),
            COMMODITYNAME=p.PRODUCTS[symbol],
            EXCHANGEID="SHFE",  # The official combined report also uses this for SC.
            OPENDATE="20260116",
            EXPIREDATE="20261231" if symbol == "SC" else "20270115",
            STARTDELIVDATE="20270118",
            ENDDELIVDATE="20270119",
        )
    else:
        row.update(
            SPEC_LONGMARGINRATIO="0.07",
            SPEC_SHORTMARGINRATIO="0.08",
            HDEGE_LONGMARGINRATIO="0.06",
            HDEGE_SHORTMARGINRATIO="0.065",
            UPPER_VALUE="0.05",
            LOWER_VALUE="0.04",
            PRICE_LIMITS="",
        )
    return {"report_date": "20261102", "update_date": "20261102 16:20:06", p.REPORTS[kind]: [row]}


def encode(doc):
    return json.dumps(doc, ensure_ascii=False).encode()


@pytest.mark.parametrize("symbol", ["RB", "CU", "AU", "SC"])
def test_concrete_calendar_preserves_actual_listed_dates_without_holiday_or_broker_certification(symbol):
    value = p.parse_contract_dates(encode(document("contract_dates", symbol)), symbol, DAY)
    row = value["contracts"][0]
    assert row["contract"] == symbol.lower() + "2701"
    assert row["listed_expiry_date"] == ("2026-12-31" if symbol == "SC" else "2027-01-15")
    assert row["delivery_start"] == "2027-01-18" and row["delivery_end"] == "2027-01-19"
    assert value["data_as_of"] == "2026-11-02"
    assert value["source_updated_at"] == "2026-11-02T16:20:06+08:00"
    assert value["calendar_adjustments_status"] == value["broker_terms_status"] == "not_evaluated"
    assert value["delivery_eligibility_status"] == "not_evaluated"
    assert not value["latest_effective_terms_verified"]
    assert "未含法定节假日调整" in value["notice"]
    assert "last_trading_date" not in row


def test_official_fraction_is_converted_to_percentage_without_long_short_or_hedge_netting():
    result = p.parse_exchange_parameters(encode(document("exchange_parameters")), "RB", DAY)
    row = result["contracts"][0]
    assert [
        row[k]
        for k in (
            "spec_long_margin_pct",
            "spec_short_margin_pct",
            "hedge_long_margin_pct",
            "hedge_short_margin_pct",
        )
    ] == [7, 8, 6, 6.5]
    assert row["upper_limit_pct"] == 5 and row["lower_limit_pct"] == 4
    assert result["ratio_unit"] == "percent"
    assert result["broker_terms_status"] == "not_evaluated" and not result["latest_effective_terms_verified"]


@pytest.mark.parametrize("kind", ["contract_dates", "exchange_parameters"])
def test_night_session_trading_day_is_separate_from_previous_evening_publication(kind):
    doc = document(kind)
    doc["update_date"] = "20261030 20:55:06"  # Friday publication for Monday's trading day.
    parser = p.parse_contract_dates if kind == "contract_dates" else p.parse_exchange_parameters
    result = parser(encode(doc), "RB", DAY)
    assert result["data_as_of"] == "2026-11-02"
    assert result["source_updated_at"] == "2026-10-30T20:55:06+08:00"
    assert not result["latest_effective_terms_verified"]


def test_missing_ratios_are_unknown_and_special_limits_require_the_notice():
    doc = document("exchange_parameters")
    doc["ContractDailyTradeArgument"][0].update(
        SPEC_LONGMARGINRATIO="--", SPEC_SHORTMARGINRATIO=None, PRICE_LIMITS="special"
    )
    row = p.parse_exchange_parameters(encode(doc), "RB", DAY)["contracts"][0]
    assert row["spec_long_margin_pct"] is row["spec_short_margin_pct"] is None
    assert row["hedge_long_margin_pct"] == 6 and row["additional_limit_notice_required"]
    assert "special" not in json.dumps(row)


@pytest.mark.parametrize("kind", ["contract_dates", "exchange_parameters"])
@pytest.mark.parametrize(
    "fault",
    [
        "wrong_day",
        "row_day",
        "duplicate",
        "continuous",
        "invalid_month",
        "wrong_update",
        "invalid_time",
        "empty",
        "huge",
        "non_object",
    ],
)
def test_wrong_identity_dates_and_schema_never_become_current_contract_rules(kind, fault):
    doc = document(kind)
    rows = doc[p.REPORTS[kind]]
    if fault == "wrong_day":
        doc["report_date"] = "20261101"
    elif fault == "row_day":
        rows[0]["TRADINGDAY"] = "20261101"
    elif fault == "duplicate":
        rows.append(rows[0].copy())
    elif fault == "continuous":
        rows[0]["INSTRUMENTID"] = "rb0"
    elif fault == "invalid_month":
        rows[0]["INSTRUMENTID"] = "rb2713"
    elif fault == "wrong_update":
        doc["update_date"] = "20261103 16:20:06"
    elif fault == "invalid_time":
        doc["update_date"] = "20261102 27:20:06"
    elif fault == "empty":
        rows.clear()
    elif fault == "huge":
        rows *= 20001
    elif fault == "non_object":
        rows.append(False)
    parser = p.parse_contract_dates if kind == "contract_dates" else p.parse_exchange_parameters
    with pytest.raises(ValueError):
        parser(encode(doc), "RB", DAY)


@pytest.mark.parametrize(
    "field,value",
    [
        ("COMMODITYID", "cu"),
        ("COMMODITYNAME", "铜"),
        ("EXPIREDATE", "20260230"),
        ("STARTDELIVDATE", "20270114"),
        ("STARTDELIVDATE", "20270218"),
        ("ENDDELIVDATE", "20270322"),
    ],
)
def test_calendar_and_product_mismatch_is_rejected(field, value):
    doc = document("contract_dates")
    doc["ContractBaseInfo"][0][field] = value
    with pytest.raises(ValueError):
        p.parse_contract_dates(encode(doc), "RB", DAY)


@pytest.mark.parametrize("ratio", [True, 0, "-0.1", "7", "NaN", "Infinity", "0.2%"])
def test_ambiguous_or_invalid_ratio_is_not_guessed(ratio):
    doc = document("exchange_parameters")
    doc["ContractDailyTradeArgument"][0]["SPEC_LONGMARGINRATIO"] = ratio
    with pytest.raises(ValueError):
        p.parse_exchange_parameters(encode(doc), "RB", DAY)


async def test_fixed_host_bounded_sources_and_tracking_cookies_are_not_reused():
    seen = []

    def handler(req):
        seen.append(req)
        assert req.url.host == "www.shfe.com.cn" and req.url.scheme == "https"
        assert not any(k in req.headers for k in ("cookie", "authorization"))
        kind = "contract_dates" if "ContractBaseInfo" in req.url.path else "exchange_parameters"
        return httpx.Response(
            200, json=document(kind), headers=[("set-cookie", "tracking=synthetic; Path=/")]
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False
    ) as client:
        for reader in (p.read_contract_dates, p.read_exchange_parameters):
            value = await reader(client, "RB", DAY, asyncio.get_running_loop().time() + 1)
            assert value["status"] == "collected" and len(value["body_sha256"]) == 64
        assert await p.read_contract_dates(client, "M", DAY, 0) == {"status": "not_covered"}
    assert len(seen) == 2


@pytest.mark.parametrize("mode", ["redirect", "auth", "oversize", "encoding", "waf", "timeout", "malformed"])
async def test_source_failures_keep_finite_diagnostics_without_retry_or_downgrade(mode):
    seen = []

    def handler(req):
        seen.append(req)
        if mode == "timeout":
            raise httpx.ReadTimeout("private diagnostic", request=req)
        if mode == "redirect":
            return httpx.Response(302, headers={"location": "http://arbitrary.invalid/"})
        if mode == "auth":
            return httpx.Response(403, text="private diagnostic")
        if mode == "oversize":
            return httpx.Response(200, content=b"x" * (p.LIMIT + 1))
        if mode == "encoding":
            return httpx.Response(200, headers={"content-encoding": "unsupported"})
        if mode == "waf":
            return httpx.Response(200, text="<html>WAF private diagnostic</html>")
        return httpx.Response(200, content=b"{broken")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        value = await p.read_exchange_parameters(client, "RB", DAY, asyncio.get_running_loop().time() + 1)
    assert len(seen) == 1 and value["status"] == "unavailable"
    assert value["error_code"] in {"http_error", "access_denied", "invalid_source_data", "source_timeout"}
    assert "private diagnostic" not in json.dumps(value) and "contracts" not in value


async def test_cancellation_propagates_without_a_fallback_request():
    entered = asyncio.Event()
    calls = []

    async def handler(req):
        calls.append(req)
        entered.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(
            p.read_contract_dates(client, "RB", DAY, asyncio.get_running_loop().time() + 2)
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(calls) == 1
