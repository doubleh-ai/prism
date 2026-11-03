"""Dated discovery contracts from synthetic public indexes; no network/models."""

from datetime import datetime
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest

from server.agent import discovery as D

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(D, "datetime", Clock)


def args(**kw):
    return D.validate({"query": "合成", "start_date": "2026-10-10", "end_date": "2026-11-03", **kw}, now=NOW)


def sse_row(**kw):
    return {
        "TITLE": "合成公司产能公告",
        "SECURITY_CODE": "600001",
        "SECURITY_NAME": "合成公司",
        "SSEDATE": "2026-11-02",
        "URL": "/disclosure/fixture.pdf",
        **kw,
    }


def sse(rows=None, **kw):
    return {
        "result": [[sse_row()]] if rows is None else rows,
        "pageHelp": {"pageNo": 1, "pageCount": 3},
        **kw,
    }


def sina_row(**kw):
    return {
        "title": "合成行业新闻",
        "summary": "合成新闻线索，仅作补充",
        "url": "https://finance.sina.com.cn/fixture.shtml",
        "ctime": str(int(NOW.timestamp()) - 60),
        **kw,
    }


def sina(rows=None, **kw):
    return {
        "result": {
            "status": {"code": 0},
            "data": [sina_row()] if rows is None else rows,
            "total": 100,
            "end": 50,
            **kw,
        }
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"query": ""},
        {"query": " " * 90 + "合成"},
        {"query": "synthetic api_key=abcd1234abcde"},
        {"url": "https://attacker.example/"},
        {"providers": ["sse", "sse"]},
        {"providers": ["unconfigured"]},
        {"providers": [["sse"]]},
        {"page": True},
        {"page": 6},
        {"limit": 0},
        {"stock_code": 600001},
        {"start_date": "2026-13-01"},
        {"start_date": "2026-11-04"},
        {"end_date": "2026-11-04"},
        {"start_date": "2020-01-01"},
        {"end_date": "2026-10-01"},
    ],
)
def test_closed_arguments_and_real_shanghai_date_boundary(changes):
    with pytest.raises(ValueError):
        args(**changes)


def test_fixed_encoded_provider_requests_do_not_accept_arbitrary_hosts_or_headers():
    inputs = args(query="合成 & announcement", stock_code="600001", start_date="2025-11-03")
    url = D.index_url("sse", inputs)
    parsed = urlsplit(url)
    fields = parse_qs(parsed.query)
    assert parsed.scheme == "https" and parsed.hostname == "query.sse.com.cn"
    assert fields["TITLE"] == ["合成 & announcement"] and fields["SECURITY_CODE"] == ["600001"]
    assert fields["pageHelp.pageNo"] == ["1"] and fields["pageHelp.pageSize"] == ["25"]
    sina_fields = parse_qs(urlsplit(D.index_url("sina", inputs)).query)
    assert "k" not in sina_fields and sina_fields["num"] == ["50"]
    assert int(sina_fields["etime"][0]) < int(sina_fields["stime"][0])


def test_stock_code_only_discovers_its_actual_disclosures_without_unrelated_news():
    inputs = D.validate({"stock_code": "600001"}, now=NOW)
    assert inputs["providers"] == ["sse"] and inputs["query"] == ""
    url = D.index_url("sse", inputs)
    fields = parse_qs(urlsplit(url).query, keep_blank_values=True)
    assert fields["TITLE"] == [""] and fields["SECURITY_CODE"] == ["600001"]
    payload = sse([[sse_row(), sse_row(SECURITY_CODE="600001-malformed", URL="/wrong.pdf")]])
    items, coverage = D.parse_index(payload, "sse", inputs, now=NOW)
    assert len(items) == 1 and items[0]["stock_code"] == "600001" and coverage["rejected_entries"] == 1
    with pytest.raises(ValueError):
        D.validate({"stock_code": "600001", "providers": ["sina"]}, now=NOW)


def test_sse_grouped_disclosures_are_real_links_day_precision_and_not_original_bodies():
    value = sse([[sse_row(), sse_row(URL="/disclosure/other.pdf", TITLE="合成补充公告")]])
    items, coverage = D.parse_index(value, "sse", args(), now=NOW)
    assert len(items) == 2 and coverage["more_pages_available"] is True
    assert items[0]["url"] == "https://www.sse.com.cn/disclosure/fixture.pdf"
    assert items[0]["publication_precision"] == "day" and items[0]["published_at"] == "2026-11-02"
    assert items[0]["published_at_basis"] == "source_index_metadata"
    assert items[0]["original_verified"] is False and items[0]["original_obtained"] is False
    assert items[0]["content_scope"] == "search_snippet" and coverage["complete_search"] is False


def test_disclosure_date_query_and_actual_stock_code_rechecked_after_provider_response():
    rows = [
        [
            sse_row(),
            sse_row(SECURITY_CODE="600002", URL="/other.pdf"),
            sse_row(SSEDATE="2026-11-04", URL="/future.pdf"),
            sse_row(SSEDATE="2020-01-01", URL="/old.pdf"),
            sse_row(TITLE="无关标题", SECURITY_NAME="无关公司", URL="/unrelated.pdf"),
        ]
    ]
    items, coverage = D.parse_index(sse(rows), "sse", args(stock_code="600001"), now=NOW)
    assert [i["url"] for i in items] == ["https://www.sse.com.cn/disclosure/fixture.pdf"]
    assert coverage["rows_received"] == 5


@pytest.mark.parametrize(
    "url",
    [
        "https://www.sse.com.cn.evil.example/file",
        "https://127.0.0.1/private",
        "http://www.sse.com.cn/file",
        "https://www.sse.com.cn/file?token=synthetic",
        "https://www.sse.com.cn:8443/file",
        "javascript:alert(1)",
    ],
)
def test_index_link_is_not_a_network_authorization(url):
    items, coverage = D.parse_index(sse([[sse_row(URL=url)]]), "sse", args(), now=NOW)
    assert items == [] and coverage["rejected_entries"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"result": []},
        {"result": "wrong", "pageHelp": {}},
        sse(pageHelp={"pageNo": 2, "pageCount": 3}),
        sse([[[]]]),
        sse([["bad-row"]]),
    ],
)
def test_invalid_or_mismatched_page_cannot_be_certified(payload):
    with pytest.raises(ValueError):
        D.parse_index(payload, "sse", args(), now=NOW)


def test_sina_only_matches_bounded_returned_rows_and_uses_original_ctime_not_update_time():
    rows = [
        sina_row(),
        sina_row(title="无关新闻", summary="没有相关词", url="https://finance.sina.com.cn/other"),
        sina_row(ctime=str(int(NOW.timestamp()) + 60), url="https://finance.sina.com.cn/future"),
        sina_row(ctime="invalid", url="https://finance.sina.com.cn/unknown"),
        sina_row(url="https://sports.sina.com.cn/wrong"),
        sina_row(),
    ]
    items, coverage = D.parse_index(sina(rows), "sina", args(), now=NOW)
    assert len(items) == 1 and items[0]["source_class"] == "secondary_news"
    assert items[0]["published_at"] == "2026-11-03T11:59:00+08:00"
    assert coverage["rejected_entries"] == 3 and coverage["rows_received"] == 6
    assert coverage["more_pages_available"] is True and coverage["complete_search"] is False


@pytest.mark.parametrize(
    "payload", [sina(status={"code": False}), sina(status={"code": 1}), sina(data="wrong"), {"result": None}]
)
def test_news_business_error_is_not_an_empty_success(payload):
    with pytest.raises(ValueError):
        D.parse_index(payload, "sina", args(), now=NOW)


def test_find_records_each_actual_index_origin_and_no_article_body_hash(monkeypatch):
    calls = []

    def fetched(url, **kwargs):
        calls.append((url, kwargs))
        provider = "sse" if "sse.com.cn" in url else "sina"
        return {
            "status": "ok",
            "url": url,
            "data": sse() if provider == "sse" else sina(),
            "retrieved_at": NOW.isoformat(),
            "body_sha256": "a" * 64,
        }

    monkeypatch.setattr(D, "fetch_public_index", fetched)
    result = D.find({"query": "合成", "start_date": "2026-11-01", "end_date": "2026-11-03"})
    assert result["status"] == "ok" and len(calls) == 2 and len(result["items"]) == 2
    assert result["items"][0]["provider"] == "sse"
    assert all("body_sha256" not in i and i["index_body_sha256"] == "a" * 64 for i in result["items"])
    assert result["coverage"][1]["query_mode"] == "recent_index_local_filter"
    assert all(c["complete_search"] is False for c in result["coverage"])
    assert result["automatic_retry"] is False and 0 < calls[0][1]["timeout_seconds"] <= 4


def test_unavailable_or_redirected_wrong_origin_stays_visible_without_provider_retry(monkeypatch):
    calls = []

    def refused(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            return {"status": "ok", "url": "https://www.cls.cn/fixture", "data": sse()}
        return {"status": "http_error"}

    monkeypatch.setattr(D, "fetch_public_index", refused)
    result = D.find({"query": "合成"})
    assert result["status"] == "unavailable" and result["items"] == [] and len(calls) == 2
    assert [c["status"] for c in result["coverage"]] == ["invalid_index", "http_error"]


def test_unmatched_index_is_a_bounded_no_match_not_a_failure_or_worldwide_absence(monkeypatch):
    def fetched(url, **kw):
        return {
            "status": "ok",
            "url": url,
            "data": sse([]),
            "retrieved_at": NOW.isoformat(),
            "body_sha256": "b" * 64,
        }

    monkeypatch.setattr(D, "fetch_public_index", fetched)
    result = D.find({"query": "其他", "providers": ["sse"]})
    assert result["status"] == "ok" and not result["items"] and result["complete_search"] is False


def test_all_invalid_entries_are_an_index_failure_not_a_certified_empty_search(monkeypatch):
    def fetched(url, **kw):
        return {"status": "ok", "url": url, "data": sse([[sse_row(SSEDATE="invalid")]])}

    monkeypatch.setattr(D, "fetch_public_index", fetched)
    result = D.find({"query": "合成", "providers": ["sse"]})
    assert result["status"] == "unavailable" and result["coverage"][0]["status"] == "invalid_index"
