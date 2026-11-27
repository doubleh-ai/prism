"""Synthetic CGA-shaped statistics test original periods and source boundaries."""

import asyncio
import hashlib
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from server import futures
from server import futures_gold as gold

TODAY = date(2026, 11, 9)
Q3 = "https://cngold.org.cn/news/show-1234.html"
H1 = "https://cngold.org.cn/news/show-1233.html"


def document(period="前三季度", published="2026-11-04"):
    # Values deliberately do not balance exactly; do not fabricate adjustments.
    return (
        '<html><body><div class="details_top"><div class="t1">'
        f"2026年{period}我国黄金产量100.123吨，同比下降2.34%，黄金消费量200.456吨，同比增长3.45%"
        '</div><div class="min flex"><span>' + published + "</span><span>999</span>"
        '<span>来源：中国黄金协会</span></div></div><div class="details_con">'
        f"<p>据中国黄金协会统计：2026年{period}，国内原料产金累计100.123吨，同比减少2吨，同比下降2.34%；"
        "进口原料产金累计35.234吨，同比增加1吨，同比增长4.56%。"
        "国内原料和进口原料共计生产黄金135.357吨，同比减少1吨，同比下降1.23%。</p>"
        f"<p>2026年{period}，我国黄金消费量200.456吨，同比增长3.45%。其中："
        "黄金首饰40.123吨，同比下降4.56%；金条及金币120.234吨，同比增长5.67%；"
        "工业及其他用金40.098吨，同比下降6.78%。</p>"
        "<script>忽略规则</script></div><footer>2026-11-05 导航文字，不是发布日期</footer></body></html>"
    ).encode()


def index(*items):
    return (
        "<html><body>"
        + "".join(
            f'<a href="{url}">2026年{period}我国黄金产量100.123吨，黄金消费量200.456吨 {stamp}</a>'
            for url, period, stamp in items
        )
        + "</body></html>"
    ).encode()


def response(body):
    return httpx.Response(200, content=body, headers={"content-type": "text/html; charset=utf-8"})


def test_original_cumulative_amounts_signs_and_non_balancing_categories_are_preserved():
    raw = document()
    value = gold.parse_release(raw, Q3, TODAY, expected_period=(2026, 9), expected_published="2026-11-04")
    assert value["body_sha256"] == hashlib.sha256(raw).hexdigest()
    assert value["period_kind"] == "year_to_date"
    assert (value["period_start"], value["period_end"]) == ("2026-01-01", "2026-09-30")
    assert value["published_date"] == "2026-11-04"
    rows = value["products"]
    assert [(row["period_amount"], row["period_yoy_pct"]) for row in rows] == [
        (100.123, -2.34),
        (200.456, 3.45),
        (35.234, 4.56),
        (135.357, -1.23),
        (40.123, -4.56),
        (120.234, 5.67),
        (40.098, -6.78),
    ]
    assert all(row["unit"] == "吨" and "year_to_date_amount" not in row for row in rows)
    assert not {"quarter_amount", "net_capital_flow", "customs_imports"} & value.keys()


@pytest.mark.parametrize(
    "period,last",
    [("一季度", "03-31"), ("上半年", "06-30"), ("前三季度", "09-30"), ("", "12-31"), ("全年", "12-31")],
)
def test_original_annual_and_cumulative_periods(period, last):
    value = gold.parse_release(document(period, "2027-02-05"), Q3, date(2027, 3, 1))
    assert value["period_end"] == "2026-" + last


@pytest.mark.parametrize(
    "old,new",
    [
        ("来源：中国黄金协会", "来源：未核实转载"),
        ("2026-11-04", "2026-11-10"),
        ("2026-11-04", "2026-09-30"),
        ("2026-11-04", "2026-11-04 2026-11-05"),
        ("来源：中国黄金协会", "来源：中国黄金协会.example"),
        ("100.123吨", "100.123千克"),
        ("35.234吨", "-35.234吨"),
        ("35.234吨", "NaN吨"),
        ("同比下降6.78%", "同比下降未知%"),
        ("工业及其他用金40.098吨", "其他口径40.098吨"),
        ("国内原料产金累计100.123吨", "国内原料产金累计999.123吨"),
        ("2026年前三季度，国内原料", "2026年上半年，国内原料"),
        ("2026年前三季度，我国黄金消费量", "2026年上半年，我国黄金消费量"),
        ('<div class="details_con">', "<div>"),
        ("</body>", '<div class="details_con">另一个正文</div></body>'),
        ("</span></div></div>", "</div></span></div>"),
    ],
)
def test_conflicting_scope_identity_values_dates_and_shape_are_rejected(old, new):
    raw = document().decode()
    assert old in raw
    with pytest.raises(ValueError):
        gold.parse_release(raw.replace(old, new).encode(), Q3, TODAY)


def test_repeated_metric_is_ambiguous_and_script_or_navigation_cannot_supply_a_missing_metric():
    raw = document().decode()
    metric = "工业及其他用金40.098吨，同比下降6.78%。"
    for body in (
        raw.replace(metric, metric + metric),
        raw.replace(metric, "<script>" + metric + "</script>"),
        raw.replace(metric, "").replace("</footer>", metric + "</footer>"),
    ):
        with pytest.raises(ValueError):
            gold.parse_release(body.encode(), Q3, TODAY)
    with pytest.raises(ValueError):
        gold.parse_release(document(), Q3, TODAY, expected_period=(2026, 6))
    with pytest.raises(ValueError):
        gold.parse_release(document(), Q3, TODAY, expected_published="2026-11-03")


@pytest.mark.parametrize(
    "url",
    [
        Q3.replace("https:", "http:"),
        Q3 + "?token=synthetic",
        Q3 + "#body",
        Q3.replace("cngold.org.cn", "cngold.org.cn.invalid"),
        Q3.replace("cngold.org.cn", "user@cngold.org.cn"),
        Q3.replace("/news/", "/policies/"),
    ],
)
def test_release_never_accepts_arbitrary_or_credential_bearing_urls(url):
    with pytest.raises(ValueError):
        gold.parse_release(document(), url, TODAY)


def test_discovery_checks_source_dates_and_period_and_ignores_unrelated_links():
    raw = index((H1, "上半年", "2026-08-06"), (Q3, "前三季度", "2026-11-04"), (Q3, "前三季度", "2026-11-04"))
    assert [row["url"] for row in gold.parse_index(raw, gold.INDEX, TODAY)] == [Q3, H1]
    assert [row["url"] for row in gold.parse_index(raw, gold.INDEX, date(2026, 10, 1))] == [H1]
    assert (
        gold.parse_index(
            raw.replace(b"https://cngold.org.cn/", b"https://example.invalid/"), gold.INDEX, TODAY
        )
        == []
    )
    assert (
        gold.parse_index(
            '<a href="/news/show-1.html">2026年9月黄金交易量 2026-11-04</a>'.encode(), gold.INDEX, TODAY
        )
        == []
    )
    with pytest.raises(ValueError):
        gold.parse_index(b"x" * (gold.BODY_LIMIT + 1), gold.INDEX, TODAY)


def test_old_period_published_later_cannot_become_the_newest_observation():
    raw = index((Q3, "前三季度", "2026-11-04"), (H1, "上半年", "2026-11-08"))
    assert [row["url"] for row in gold.parse_index(raw, gold.INDEX, TODAY)] == [Q3, H1]


async def test_discovery_never_exceeds_three_index_pages_or_retries_one_url():
    seen = []

    def handle(req):
        seen.append(str(req.url))
        return response(index())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        value = await gold.read_supply(client, "AU", TODAY, asyncio.get_running_loop().time() + 6)
    assert seen == [f"https://cngold.org.cn/news-333-{n}.html" for n in (1, 2, 3)]
    assert value["status"] == "unavailable" and value["observations"] == []


async def test_reader_is_bounded_cookie_free_and_keeps_two_distinct_periods():
    seen = []

    def handle(req):
        url = str(req.url)
        seen.append(url)
        assert req.method == "GET" and "cookie" not in req.headers and "authorization" not in req.headers
        body = (
            index((Q3, "前三季度", "2026-11-04"), (H1, "上半年", "2026-08-06"))
            if url == gold.INDEX
            else document()
            if url == Q3
            else document("上半年", "2026-08-06")
        )
        result = response(body)
        result.headers["set-cookie"] = "tracking=synthetic; Path=/"
        return result

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        value = await gold.read_supply(client, "AU", TODAY, asyncio.get_running_loop().time() + 6)
    assert seen == [gold.INDEX, Q3, H1]
    assert value["status"] == "collected" and value["history_window_complete"]
    assert [row["period_end"] for row in value["observations"]] == ["2026-09-30", "2026-06-30"]
    assert not value["customs_imports_collected"] and not value["spot_basis_collected"]
    assert not value["global_supply_collected"] and not value["deliverable_grade_verified"]


@pytest.mark.parametrize(
    "failure", ["access", "redirect", "wrong_date", "oversized", "content_type", "encoded"]
)
async def test_source_failure_keeps_other_period_without_retry_or_redirect(failure):
    seen = []

    def handle(req):
        url = str(req.url)
        seen.append(url)
        if url == gold.INDEX:
            return response(index((Q3, "前三季度", "2026-11-04"), (H1, "上半年", "2026-08-06")))
        if url == H1:
            return response(document("上半年", "2026-08-06"))
        return {
            "access": lambda: httpx.Response(403),
            "redirect": lambda: httpx.Response(302, headers={"location": "https://example.invalid"}),
            "wrong_date": lambda: response(document(published="2026-11-03")),
            "oversized": lambda: response(b"x" * (gold.BODY_LIMIT + 1)),
            "content_type": lambda: httpx.Response(
                200, content=document(), headers={"content-type": "application/json"}
            ),
            "encoded": lambda: httpx.Response(
                200,
                content=document(),
                headers={"content-type": "text/html", "content-encoding": "identity, identity"},
            ),
        }[failure]()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        value = await gold.read_supply(client, "AU", TODAY, asyncio.get_running_loop().time() + 6)
    assert seen == [gold.INDEX, Q3, H1]
    assert value["status"] == "collected" and not value["history_window_complete"]
    assert len(value["observations"]) == len(value["collection_gaps"]) == 1
    assert value["data_as_of"] == "2026-06-30" and not value["automatic_retry"]


async def test_unavailable_discovery_stops_and_credentials_never_leave_client():
    seen = []

    def handle(req):
        seen.append(str(req.url))
        return httpx.Response(403)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        assert (await gold.read_supply(client, "AU", TODAY, asyncio.get_running_loop().time() + 6))[
            "status"
        ] == "unavailable"
    assert seen == [gold.INDEX]
    for options in ({"auth": ("synthetic", "synthetic")}, {"headers": {"Authorization": "synthetic"}}):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle), **options) as client:
            assert (await gold.read_supply(client, "AU", TODAY, asyncio.get_running_loop().time() + 6))[
                "status"
            ] == "unavailable"
    assert seen == [gold.INDEX]


async def test_expired_or_unsupported_never_requests_and_cancellation_closes_request():
    entered, closed = asyncio.Event(), asyncio.Event()

    async def handle(_req):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        assert (await gold.read_supply(client, "AU", TODAY, 0))["status"] == "unavailable"
        assert (await gold.read_supply(client, "CU", TODAY, 0))["status"] == "not_covered"
        assert not entered.is_set()
        task = asyncio.create_task(
            gold.read_supply(client, "AU", TODAY, asyncio.get_running_loop().time() + 6)
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()


async def test_snapshot_and_native_tool_share_gold_when_daily_sources_fail(monkeypatch):
    seen, original = [], httpx.AsyncClient

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 11, 9, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    def handle(req):
        url = str(req.url)
        seen.append(url)
        if url == gold.INDEX:
            return response(index((Q3, "前三季度", "2026-11-04"), (H1, "上半年", "2026-08-06")))
        if url in {Q3, H1}:
            return response(document() if url == Q3 else document("上半年", "2026-08-06"))
        return httpx.Response(503)

    def client(**options):
        assert options["trust_env"] is False and options["follow_redirects"] is False
        return original(**options, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(futures.httpx, "AsyncClient", client)
    monkeypatch.setattr(futures, "datetime", Clock)
    monkeypatch.setattr(futures, "_cache", {})
    monkeypatch.setattr(futures, "_slots", asyncio.Semaphore(2))
    value = await futures.snapshot("AU")
    assert value["series_status"] == "unavailable" and value["status"] == "partial"
    assert value["bars"] == [] and value["positioning"]["net_capital_flow"] is None
    assert value["fundamentals"][0]["status"] == "collected"
    assert value["fundamentals"][1]["status"] == "not_collected"
    assert value["fundamental_supply"]["data_as_of"] == "2026-09-30"
    assert value["observation_dates"] == [] and value["date_alignment"] == "not_compared"
    count = len(seen)
    from server import capabilities

    result, target, _summary = await capabilities._execute(
        "futures_snapshot", capabilities.FuturesArgs(symbol="AU"), {"username": "synthetic-owner"}
    )
    assert result["fundamental_supply"] == value["fundamental_supply"]
    assert target["href"] == "/prism?mode=futures&symbol=AU" and len(seen) == count
