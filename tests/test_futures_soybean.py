"""Synthetic official-shaped releases exercise period, transport and sharing boundaries."""

import asyncio
import hashlib
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from server import futures
from server import futures_soybean as soy

TODAY = date(2026, 11, 9)
OCTOBER = "https://scs.moa.gov.cn/jcyj/202610/t20261009_1234567.htm"
SEPTEMBER = "https://scs.moa.gov.cn/jcyj/202609/t20260910_1234566.htm"


def document(month=10, *, changes=None, duplicate=False):
    title = f"2026年{month}月中国农产品供需形势分析（CASDE-No.{month + 100}）"
    rows = [
        ["", "2024/25", f"2025/26（{month}月估计）", "2026/27（9月预测）", f"2026/27（{month}月预测）"],
        ["万吨（10000 tons）"],
    ]
    # Deliberately synthetic values, including a negative balance change.
    for n, label in enumerate(soy.METRICS):
        rows.append([label, "10", "20", "30", str(-4 if label == "结余变化" else 40 + n)])
    # September has only one September forecast, following an August forecast.
    if month == 9:
        rows[0][3] = "2026/27（8月预测）"
    table = (
        "<table>"
        + "".join("<tr>" + "".join("<td>" + cell + "</td>" for cell in row) + "</tr>" for row in rows)
        + "</table>"
    )
    day = "09" if month == 10 else "10"
    raw = (
        f'<html><head><title>{title}</title><meta name="PubDate" '
        f'content="2026-{month:02d}-{day} 10:00:00"><meta name="ArticleTitle" '
        f'content="{title}"></head><body><p>大豆市场年度为当年10月至下年9月</p>'
        "<h2>中国大豆供需平衡表</h2>"
        + table
        + ("<h2>中国大豆供需平衡表</h2>" + table if duplicate else "")
        + "</body></html>"
    )
    for old, new in (changes or {}).items():
        assert old in raw
        raw = raw.replace(old, new)
    return raw.encode()


def index(*urls):
    return (
        "<html><body>"
        + "".join(
            f'<a href="{url}">2026年{10 if url == OCTOBER else 9}月中国农产品供需形势分析'
            f"（CASDE-No.{110 if url == OCTOBER else 109}）</a>"
            for url in urls
        )
        + "</body></html>"
    ).encode()


def response(raw):
    return httpx.Response(200, content=raw, headers={"content-type": "text/html; charset=utf-8"})


def test_original_forecast_versions_and_marketing_year_are_preserved():
    raw = document(duplicate=True)
    value = soy.parse_release(raw, OCTOBER, TODAY)
    assert value["body_sha256"] == hashlib.sha256(raw).hexdigest()
    assert value["published_date"] == "2026-10-09" and value["duplicate_tables"] == 2
    history, estimate, previous, current = value["estimates"]
    assert history["kind"] == "reported_history" and estimate["kind"] == "estimate"
    assert previous["market_year"] == current["market_year"] == "2026/27"
    assert previous["issue_month"] == 9 and current["issue_month"] == 10
    assert current["kind"] == "forecast"
    assert (current["period_start"], current["period_end"]) == ("2026-10-01", "2027-09-30")
    assert current["values"]["balance_change"] == -4
    assert current["values"]["crush_consumption"] == 43
    assert not {"meal_output", "ending_inventory", "spot_basis"} & current["values"].keys()
    assert len(value["metrics"]) == 9


@pytest.mark.parametrize(
    "old,new",
    [
        ("万吨（10000 tons）", "吨（tons）"),
        ("中国大豆供需平衡表", "中国玉米供需平衡表"),
        ("大豆市场年度为当年10月至下年9月", "其他年度"),
        ("2026-10-09 10:00:00", "2026-10-10 10:00:00"),
        ("2026-10-09 10:00:00", "2026-10-09"),
        ("2026/27（10月预测）", "2026/27（9月预测）"),
        ("2026/27（10月预测）", "2026/27（10月估计）"),
        ("2026/27（10月预测）", "2027/28（10月预测）"),
        ("2026/27（10月预测）", "2026/28（10月预测）"),
        ("<td>40</td>", "<td>-40</td>"),
        ("<td>40</td>", "<td>NaN</td>"),
        ("<td>40</td>", "<td></td>"),
        ("<td>压榨消费</td>", "<td>食用消费</td>"),
        ("<td>出口</td>", "<td>其他项目</td>"),
    ],
)
def test_mismatched_identity_period_unit_or_missing_values_are_rejected(old, new):
    with pytest.raises(ValueError):
        soy.parse_release(document(changes={old: new}), OCTOBER, TODAY)


@pytest.mark.parametrize(
    "url",
    [
        OCTOBER.replace("https:", "http:"),
        OCTOBER + "?token=synthetic",
        OCTOBER + "#table",
        OCTOBER.replace("scs.moa.gov.cn", "scs.moa.gov.cn.invalid"),
        OCTOBER.replace("scs.moa.gov.cn", "user@scs.moa.gov.cn"),
        OCTOBER.replace("/202610/", "/202609/"),
    ],
)
def test_release_is_only_the_dated_official_https_path(url):
    with pytest.raises(ValueError):
        soy.parse_release(document(), url, TODAY)


def test_future_publication_and_index_release_identity_cannot_be_substituted():
    with pytest.raises(ValueError):
        soy.parse_release(document(), OCTOBER, date(2026, 10, 8))
    with pytest.raises(ValueError):
        soy.parse_release(document(), OCTOBER, TODAY, expected_report=(2026, 10, 999))
    raw = document(duplicate=True).replace(b"<td>40</td>", b"<td>999</td>", 1)
    with pytest.raises(ValueError):
        soy.parse_release(raw, OCTOBER, TODAY)


async def test_bounded_discovery_clears_source_cookies_and_returns_two_distinct_reports():
    seen = []

    def handle(req):
        url = str(req.url)
        seen.append(url)
        assert req.method == "GET" and "authorization" not in req.headers and "cookie" not in req.headers
        pages = {
            soy.INDEX: index(),
            soy.INDEX + "index_1.htm": index(OCTOBER, OCTOBER),
            soy.INDEX + "index_2.htm": index(SEPTEMBER),
        }
        result = response(pages.get(url, document(10 if url == OCTOBER else 9)))
        result.headers["set-cookie"] = "tracking=synthetic; Path=/"
        return result

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False) as client:
        value = await soy.read_supply(client, "M", TODAY, asyncio.get_running_loop().time() + 6)
    assert value["status"] == "collected" and value["history_window_complete"]
    assert value["data_as_of"] == "2026-10-09"  # publication, not future crop end
    assert [row["report_month"] for row in value["observations"]] == ["2026-10", "2026-09"]
    assert len(seen) == len(set(seen)) == 5 and value["automatic_retry"] is False
    assert value["soybean_meal_output_collected"] is False


@pytest.mark.parametrize("failure", ["redirect", "access_restriction", "too_large", "wrong_type"])
async def test_latest_source_failure_preserves_older_data_without_redirect_or_retry(failure):
    seen = []

    def handle(req):
        url = str(req.url)
        seen.append(url)
        if url == soy.INDEX:
            return response(index(OCTOBER, SEPTEMBER))
        if url == SEPTEMBER:
            return response(document(9))
        assert url == OCTOBER
        if failure == "redirect":
            return httpx.Response(302, headers={"location": "https://invalid.test/"})
        if failure == "access_restriction":
            return httpx.Response(412)
        if failure == "too_large":
            return response(b"x" * (soy.BODY_LIMIT + 1))
        return httpx.Response(200, content=document(), headers={"content-type": "application/json"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False) as client:
        value = await soy.read_supply(client, "M", TODAY, asyncio.get_running_loop().time() + 6)
    assert value["status"] == "collected" and not value["history_window_complete"]
    assert value["data_as_of"] == "2026-09-10" and len(value["observations"]) == 1
    assert value["collection_gaps"][0]["source_url"] == OCTOBER
    assert seen == [soy.INDEX, OCTOBER, SEPTEMBER]


@pytest.mark.parametrize(
    "options", [{"auth": ("synthetic", "test")}, {"headers": {"X-API-Key": "synthetic"}}]
)
async def test_credential_bearing_clients_are_rejected_before_network(options):
    def handle(_req):
        pytest.fail("must not send authenticated requests to this public source")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), **options) as client:
        value = await soy.read_supply(client, "M", TODAY, asyncio.get_running_loop().time() + 6)
    assert value["status"] == "unavailable"


async def test_cancellation_closes_reader_and_expired_deadline_never_requests():
    entered, closed = asyncio.Event(), asyncio.Event()

    async def handle(_req):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        assert (await soy.read_supply(client, "M", TODAY, 0))["status"] == "unavailable"
        assert not entered.is_set()
        assert (await soy.read_supply(client, "CU", TODAY, 0))["status"] == "not_covered"
        task = asyncio.create_task(soy.read_supply(client, "M", TODAY, asyncio.get_running_loop().time() + 6))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()


async def test_snapshot_and_native_tool_share_soybean_context_when_prices_and_exchange_fail(monkeypatch):
    seen, original = [], httpx.AsyncClient

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 11, 9, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    def handle(req):
        url = str(req.url)
        seen.append(url)
        if url == soy.INDEX:
            return response(index(OCTOBER, SEPTEMBER))
        if url in {OCTOBER, SEPTEMBER}:
            return response(document(10 if url == OCTOBER else 9))
        return httpx.Response(503)

    def client(**options):
        assert options["trust_env"] is False and options["follow_redirects"] is False
        return original(**options, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(futures.httpx, "AsyncClient", client)
    monkeypatch.setattr(futures, "datetime", Clock)
    monkeypatch.setattr(futures, "_cache", {})
    monkeypatch.setattr(futures, "_slots", asyncio.Semaphore(2))
    value = await futures.snapshot("M")
    assert value["status"] == "partial" and value["series_status"] == "unavailable"
    assert value["bars"] == [] and value["latest"]["close"] is None
    assert value["fundamentals"][0]["status"] == "collected"
    assert value["fundamentals"][1]["status"] == "not_collected"
    assert value["fundamental_supply"]["observations"][0]["estimates"][-1]["values"]["imports"] == 41
    count = len(seen)
    from server import capabilities

    result, target, _summary = await capabilities._execute(
        "futures_snapshot", capabilities.FuturesArgs(symbol="M"), {"username": "synthetic-owner"}
    )
    assert result["fundamental_supply"] == value["fundamental_supply"]
    assert target["href"] == "/prism?mode=futures&symbol=M" and len(seen) == count
