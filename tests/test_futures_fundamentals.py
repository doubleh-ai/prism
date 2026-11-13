"""Synthetic HTTP/HTML exercises the real bounded NBS reader and shared snapshot.

Numbers/column layout mirror two public NBS releases; fixture markup is
synthetic. No model, live network, production account or deployment is used.
"""

import asyncio
import copy
import hashlib
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from server import futures
from server import futures_fundamentals as nbs

TODAY = date(2026, 11, 8)
SEPTEMBER = "https://www.stats.gov.cn/sj/zxfb/202610/t20261019_1965617.html"
AUGUST = "https://www.stats.gov.cn/sj/zxfb/202609/t20260915_1965308.html"
ROWS = [
    ["生铁（万吨）", "6534", "-1.4", "62844", "-3.0"],
    ["粗钢（万吨）", "7359", "0.1", "72477", "-2.9"],
    ["钢材（万吨）", "11817", "-3.8", "106710", "-2.0"],
]
OIL_ROWS = [
    ["原油（万吨）", "1821", "2.2", "16460", "1.0"],
    ["原油加工量（万吨）", "5748", "-8.4", "51360", "-6.8"],
]


def document(*, month=9, rows=None, duplicate=True, changed_duplicate=False, combined=False):
    period = "1—2" if combined else str(month)
    day = "2026/03/16" if combined else "2026/10/19" if month == 9 else "2026/09/15"
    selected = copy.deepcopy(ROWS if rows is None else rows)
    if combined:
        selected = [row[:3] for row in selected]
        headers = [["指标", "1—2月"], ["绝对量", "同比增长 （%）"]]
    else:
        headers = [
            ["指标", period + "月", "1—" + period + "月"],
            ["绝对量", "同比增长 （%）", "绝对量", "同比增长 （%）"],
        ]

    def table(values):
        return (
            "<table>"
            + "".join(
                "<tr>" + "".join("<td><span>" + cell + "</span></td>" for cell in row) + "</tr>"
                for row in headers + values
            )
            + "</table>"
        )

    body = table(selected)
    if duplicate:
        if changed_duplicate:
            selected[1][1] = "9999"
        body += table(selected)
    return (
        "<html><head><title>2026年" + period + "月份规模以上工业增加值增长5.5% - 国家统计局"
        "</title></head><body><p>" + day + " 10:00</p>" + body + "</body></html>"
    ).encode()


def index(*urls):
    titles = {
        SEPTEMBER: "2026年9月份规模以上工业增加值增长5.5%",
        AUGUST: "2026年8月份规模以上工业增加值增长5.2%",
    }
    return (
        "<html><body>"
        + "".join('<a href="' + url + '">' + titles[url] + "</a>" for url in urls)
        + "</body></html>"
    ).encode()


def response(raw):
    return httpx.Response(200, content=raw, headers={"content-type": "text/html; charset=utf-8"})


def test_report_periods_official_yoy_and_duplicate_tables_are_not_summed():
    raw = document()
    value = nbs.parse_release(raw, SEPTEMBER, TODAY)
    assert value["period_start"] == "2026-09-01" and value["period_end"] == "2026-09-30"
    assert value["published_date"] == "2026-10-19" and value["published_date_precision"] == "date"
    assert value["body_sha256"] == hashlib.sha256(raw).hexdigest()
    assert value["duplicate_tables"] == 2 and len(value["products"]) == 3
    steel = value["products"][1]
    assert steel == {
        "product": "crude_steel",
        "label": "粗钢",
        "unit": "万吨",
        "period_amount": 7359,
        "period_yoy_pct": 0.1,
        "year_to_date_amount": 72477,
        "year_to_date_yoy_pct": -2.9,
    }
    assert "rebar" not in steel and "inventory" not in value
    # The previous release's cumulative total differs from a reconstruction of
    # the next published monthly figure. Never recalculate the original series.
    assert steel["period_amount"] != steel["year_to_date_amount"] - 65185


@pytest.mark.parametrize("symbol,rows", [("RB", ROWS), ("SC", OIL_ROWS)])
def test_combined_january_february_stays_combined_not_two_invented_months(symbol, rows):
    url = "https://www.stats.gov.cn/sj/zxfb/202603/t20260316_1960000.html"
    value = nbs.parse_release(document(combined=True, rows=rows), url, TODAY, symbol=symbol)
    assert value["period_start"] == "2026-01-01" and value["period_end"] == "2026-02-28"
    assert value["period_kind"] == "combined_january_february"
    assert value["period_label"] == "2026年1—2月"
    assert "year_to_date_amount" not in value["products"][0]


def test_oil_production_and_refinery_input_retain_their_own_metrics_and_scope():
    raw = document(rows=ROWS + OIL_ROWS + [["十种有色金属（万吨）", "693", "0.1", "6235", "2.5"]])
    value = nbs.parse_release(raw, SEPTEMBER, TODAY, symbol="SC")
    assert value["scope"] == "national_industrial_enterprises_above_designated_size"
    assert value["body_sha256"] == hashlib.sha256(raw).hexdigest()
    assert value["duplicate_tables"] == 2
    assert value["products"] == [
        {
            "product": "crude_oil_production",
            "label": "原油",
            "unit": "万吨",
            "period_amount": 1821,
            "period_yoy_pct": 2.2,
            "year_to_date_amount": 16460,
            "year_to_date_yoy_pct": 1,
        },
        {
            "product": "refinery_throughput",
            "label": "原油加工量",
            "unit": "万吨",
            "period_amount": 5748,
            "period_yoy_pct": -8.4,
            "year_to_date_amount": 51360,
            "year_to_date_yoy_pct": -6.8,
        },
    ]
    assert [row["product"] for row in nbs.parse_release(raw, SEPTEMBER, TODAY)["products"]] == [
        "pig_iron",
        "crude_steel",
        "steel_products",
    ]
    for symbol in ("CU", "AU", "M"):
        with pytest.raises(ValueError):
            nbs.parse_release(raw, SEPTEMBER, TODAY, symbol=symbol)


@pytest.mark.parametrize("symbol,rows", [("RB", ROWS), ("SC", OIL_ROWS)])
@pytest.mark.parametrize(
    "fault",
    [
        "different_duplicate",
        "missing_row",
        "duplicate_row",
        "wrong_unit",
        "negative_amount",
        "nan_yoy",
        "exponent",
        "future_publication",
        "period_mismatch",
        "publication_missing",
        "incomplete_month",
    ],
)
def test_changed_or_ambiguous_source_is_rejected(fault, symbol, rows):
    rows = copy.deepcopy(rows)
    url = SEPTEMBER
    raw = None
    expected = None
    if fault == "different_duplicate":
        raw = document(rows=rows, changed_duplicate=True)
    elif fault == "missing_row":
        rows.pop()
    elif fault == "duplicate_row":
        rows.append(rows[1])
    elif fault == "wrong_unit":
        rows[0][0] = rows[0][0].replace("万吨", "吨")
    elif fault == "negative_amount":
        rows[0][1] = "-1"
    elif fault == "nan_yoy":
        rows[0][2] = "NaN"
    elif fault == "exponent":
        rows[0][1] = "1e4"
    elif fault == "future_publication":
        url = "https://www.stats.gov.cn/sj/zxfb/202612/t20261219_1965617.html"
    elif fault == "period_mismatch":
        expected = (2026, 8, 8)
    elif fault == "publication_missing":
        raw = document(rows=rows).replace(b"2026/10/19 10:00", b"unknown")
    elif fault == "incomplete_month":
        url = "https://www.stats.gov.cn/sj/zxfb/202609/t20260919_1965617.html"
        raw = document(rows=rows).replace(b"2026/10/19 10:00", b"2026/09/19 10:00")
    with pytest.raises((ValueError, OverflowError)):
        nbs.parse_release(raw or document(rows=rows), url, TODAY, expected_period=expected, symbol=symbol)


@pytest.mark.parametrize(
    "url",
    [
        SEPTEMBER.replace("https:", "http:"),
        SEPTEMBER + "?token=no",
        SEPTEMBER + "#other",
        SEPTEMBER.replace("www.stats.gov.cn", "attacker.example"),
        SEPTEMBER.replace("www.stats.gov.cn", "www.stats.gov.cn@attacker.example"),
        SEPTEMBER.replace("/202610/", "/202609/"),
    ],
)
def test_only_exact_canonical_official_release_paths_are_accepted(url):
    with pytest.raises(ValueError):
        nbs.parse_release(document(), url, TODAY)


@pytest.mark.parametrize("symbol,rows", [("RB", ROWS), ("SC", OIL_ROWS)])
async def test_bounded_real_http_parser_selects_two_periods_from_official_links_once(symbol, rows):
    seen = []

    def handle(req):
        seen.append(req)
        assert req.method == "GET" and req.url.host == "www.stats.gov.cn" and not req.url.query
        assert not any(key in req.headers for key in ("cookie", "authorization", "proxy-authorization"))
        if str(req.url) == nbs.INDEX:
            return response(index(SEPTEMBER, SEPTEMBER))
        if str(req.url) == nbs.INDEX + "index_1.html":
            return response(index(AUGUST))
        assert str(req.url) in {SEPTEMBER, AUGUST}
        return response(document(month=9 if str(req.url) == SEPTEMBER else 8, rows=rows))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False) as client:
        value = await nbs.read_supply(client, symbol, TODAY, asyncio.get_running_loop().time() + 6)
    assert value["status"] == "collected" and len(value["observations"]) == 2
    assert value["history_window_complete"] is True
    assert len(seen) == len({str(req.url) for req in seen}) == 4
    assert [row["period_end"] for row in value["observations"]] == ["2026-09-30", "2026-08-31"]
    assert value["social_inventory_collected"] is False
    assert value["spot_basis_collected"] is value["operating_rate_collected"] is False
    if symbol == "RB":
        assert value["rebar_specific_output"] is False
    else:
        assert value["deliverable_grade_verified"] is value["imports_collected"] is False
        assert value["global_supply_collected"] is False
        assert "全球供需" in value["notice"] and "不是" in value["notice"]
        assert "炼厂开工率" in value["notice"]


async def test_one_source_failure_does_not_discard_a_valid_period_or_retry_it():
    seen = []

    def handle(req):
        seen.append(str(req.url))
        if str(req.url) == nbs.INDEX:
            return response(index(SEPTEMBER, AUGUST))
        if str(req.url) == AUGUST:
            return httpx.Response(404)
        return response(document())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False) as client:
        value = await nbs.read_supply(client, "RB", TODAY, asyncio.get_running_loop().time() + 6)
    assert value["status"] == "collected" and value["history_window_complete"] is False
    assert len(value["observations"]) == 1
    assert value["collection_gaps"] == [
        {"source_url": AUGUST, "error_code": "report_not_found", "http_status": 404}
    ]
    assert len(seen) == len(set(seen)) == 3


@pytest.mark.parametrize("fault", ["redirect", "challenge", "oversize", "encoding", "json", "timeout"])
async def test_access_and_format_failures_are_bounded_unavailable_not_bypassed(fault):
    seen = []

    def handle(req):
        seen.append(req)
        if fault == "timeout":
            raise httpx.ReadTimeout("private diagnostic text must not escape")
        if fault == "redirect":
            return httpx.Response(302, headers={"location": SEPTEMBER})
        if fault == "challenge":
            return httpx.Response(412, text="challenge must not be solved")
        if fault == "oversize":
            return response(b"x" * (nbs.BODY_LIMIT + 1))
        if fault == "encoding":
            return httpx.Response(
                200, headers={"content-encoding": "gzip", "content-type": "text/html"}, content=b"bad"
            )
        return httpx.Response(200, json={"data": []})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), trust_env=False, follow_redirects=True
    ) as client:
        value = await nbs.read_supply(client, "RB", TODAY, asyncio.get_running_loop().time() + 6)
    assert value["status"] == "unavailable" and value["observations"] == []
    assert len(seen) == 1
    assert "private diagnostic" not in str(value) and "challenge must" not in str(value)


@pytest.mark.parametrize(
    "options",
    [
        {"auth": ("not-real", "not-real")},
        {"headers": {"Cookie": "synthetic=private"}},
        {"headers": {"X-Api-Key": "synthetic-private"}},
    ],
)
async def test_credential_bearing_clients_are_refused_before_any_network(options):
    seen = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: seen.append(req)), trust_env=False, **options
    ) as client:
        value = await nbs.read_supply(client, "RB", TODAY, asyncio.get_running_loop().time() + 6)
    assert value["status"] == "unavailable" and not seen
    assert "synthetic-private" not in str(value)


@pytest.mark.parametrize("symbol", ["CU", "AU", "M"])
async def test_other_products_are_not_relabelled_steel_or_oil_fundamentals(symbol):
    seen = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: seen.append(req))) as client:
        value = await nbs.read_supply(client, symbol, TODAY, asyncio.get_running_loop().time() + 6)
    assert value["status"] == "not_covered" and not seen and not value["observations"]


async def test_expired_deadline_and_actual_cancellation_do_not_spawn_later_fetches():
    seen = []
    entered = asyncio.Event()

    async def handle(req):
        seen.append(req)
        entered.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False) as client:
        expired = await nbs.read_supply(client, "RB", TODAY, 0)
        assert expired["status"] == "unavailable" and not seen
        task = asyncio.create_task(
            nbs.read_supply(client, "RB", TODAY, asyncio.get_running_loop().time() + 6)
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(seen) == 1


@pytest.mark.parametrize("symbol,rows", [("RB", ROWS), ("SC", OIL_ROWS)])
async def test_the_shared_snapshot_and_native_capability_keep_supply_when_other_sources_fail(
    monkeypatch, symbol, rows
):
    seen = []
    original = httpx.AsyncClient

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 11, 8, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    def handle(req):
        seen.append(str(req.url))
        if str(req.url) == nbs.INDEX:
            return response(index(SEPTEMBER, AUGUST))
        if str(req.url) in {SEPTEMBER, AUGUST}:
            return response(document(month=9 if str(req.url) == SEPTEMBER else 8, rows=rows))
        return httpx.Response(503)

    def client(**options):
        assert options["trust_env"] is False and options["follow_redirects"] is False
        return original(**options, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(futures.httpx, "AsyncClient", client)
    monkeypatch.setattr(futures, "datetime", Clock)
    monkeypatch.setattr(futures, "_cache", {})
    monkeypatch.setattr(futures, "_slots", asyncio.Semaphore(2))
    value = await futures.snapshot(symbol)
    assert value["status"] == "partial" and value["series_status"] == "unavailable"
    assert value["bars"] == [] and value["latest"]["close"] is None
    assert value["fundamentals"][0]["status"] == "collected"
    assert value["fundamentals"][1]["status"] == "not_collected"
    assert value["fundamental_supply"]["observations"][0]["products"][1]["period_amount"] == int(rows[1][1])
    assert value["warehouse_receipt"]["status"] == value["exchange_daily"]["status"] == "unavailable"
    assert value["date_alignment_scope"].startswith("daily_observations_only")
    count = len(seen)
    from server import capabilities

    result, target, _summary = await capabilities._execute(
        "futures_snapshot", capabilities.FuturesArgs(symbol=symbol), {"username": "synthetic-owner"}
    )
    assert result["fundamental_supply"] == value["fundamental_supply"]
    assert target["href"] == "/prism?mode=futures&symbol=" + symbol and len(seen) == count


async def test_unexpected_sibling_failure_waits_for_monthly_reader_to_close(monkeypatch):
    entered = asyncio.Event()
    closed = asyncio.Event()

    async def continuous(*_args):
        return None, {"error_code": "source_timeout"}

    async def monthly(*_args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            closed.set()

    async def dated(*_args):
        await entered.wait()
        raise RuntimeError("synthetic unexpected reader failure")

    monkeypatch.setattr(futures, "_continuous", continuous)
    monkeypatch.setattr(futures, "_reports", dated)
    monkeypatch.setattr(futures.futures_fundamentals, "read_supply", monthly)
    monkeypatch.setattr(futures, "_cache", {})
    monkeypatch.setattr(futures, "_slots", asyncio.Semaphore(2))
    with pytest.raises(ExceptionGroup, match="TaskGroup"):
        await futures.snapshot("RB")
    assert closed.is_set(), "a snapshot cannot return while its source child still owns work"
