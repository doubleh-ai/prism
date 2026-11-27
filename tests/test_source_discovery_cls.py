"""Anonymous latest-page discovery: synthetic news is a lead, not verified fact."""

from datetime import datetime
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


def arguments(**changes):
    return D.validate({"query": "合成", "providers": ["cls"], **changes}, now=NOW)


def row(**changes):
    return {
        "id": 12345,
        "title": "",
        "brief": "",
        "content": "<b>合成公司</b>披露新产线。",
        "ctime": int(NOW.timestamp()) - 60,
        "assocArticleUrl": "https://untrusted.example/ignored",
        **changes,
    }


def index(rows=None, **changes):
    return {"errno": 0, "data": {"roll_data": [row()] if rows is None else rows}, **changes}


def test_latest_entry_preserves_actual_index_time_and_canonical_article_route():
    items, coverage = D.parse_index(index(), "cls", arguments(), now=NOW)
    assert len(items) == 1
    item = items[0]
    assert item["url"] == "https://www.cls.cn/detail/12345"
    assert item["title"] == "合成公司 披露新产线。"
    assert item["published_at"] == "2026-11-03T11:59:00+08:00"
    assert item["published_at_basis"] == "source_index_metadata"
    assert item["source_class"] == "secondary_news"
    assert item["original_obtained"] is False and item["original_verified"] is False
    assert coverage["more_pages_available"] is None and coverage["complete_search"] is False
    assert "untrusted.example" not in str(item)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": True},
        {"id": "12345"},
        {"id": -1},
        {"id": 2**53},
        {"ctime": True},
        {"ctime": int(NOW.timestamp()) + 1},
        {"ctime": "invalid"},
        {"content": "", "brief": "", "title": ""},
    ],
)
def test_unusable_rows_do_not_become_dated_evidence(changes):
    items, coverage = D.parse_index(index([row(**changes)]), "cls", arguments(), now=NOW)
    assert items == [] and coverage["rejected_entries"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"errno": False, "data": {"roll_data": []}},
        {"errno": 401, "data": {"roll_data": []}},
        {"errno": 0, "data": []},
        {"errno": 0, "data": {"roll_data": ["not a record"]}},
        {"errno": 0, "data": {"roll_data": [{}] * 1001}},
    ],
)
def test_refusal_and_changed_schema_are_not_a_successful_empty_search(payload):
    with pytest.raises(ValueError):
        D.parse_index(payload, "cls", arguments(), now=NOW)


def test_keyword_window_duplicate_rows_and_shanghai_date_are_checked_locally():
    previous_day = int(datetime(2026, 11, 2, 23, 59, tzinfo=NOW.tzinfo).timestamp())
    payload = index([row(), row(), row(id=2, content="其他主题"), row(id=3, ctime=previous_day)])
    items, coverage = D.parse_index(payload, "cls", arguments(start_date="2026-11-03"), now=NOW)
    assert len(items) == 1 and coverage["rows_received"] == 4
    assert coverage["available_date_start"] == "2026-11-02"
    assert coverage["complete_search"] is False


def test_fixed_latest_feed_get_has_no_keyword_or_credentials_and_retains_its_receipt(monkeypatch):
    calls = []

    def fetched(url, **options):
        calls.append((url, options))
        return {
            "status": "ok",
            "url": url,
            "data": index(),
            "retrieved_at": NOW.isoformat(),
            "body_sha256": "a" * 64,
        }

    monkeypatch.setattr(D, "fetch_public_index", fetched)
    result = D.find({"query": "合成", "providers": ["cls"]})
    assert result["status"] == "ok" and len(calls) == 1
    assert calls[0][0] == "https://www.cls.cn/api/cache?name=telegraph"
    assert 0 < calls[0][1]["timeout_seconds"] <= 4 and calls[0][1]["referer"] is None
    assert result["coverage"][0]["query_mode"] == "recent_index_local_filter"
    assert result["items"][0]["index_body_sha256"] == "a" * 64
    assert "body_sha256" not in result["items"][0]
    assert result["complete_search"] is False and result["automatic_retry"] is False


def test_unavailable_history_page_is_visible_without_refetching_latest_page(monkeypatch):
    monkeypatch.setattr(D, "fetch_public_index", lambda *a, **k: pytest.fail("must not request"))
    result = D.find({"query": "合成", "providers": ["cls"], "page": 2})
    assert result["status"] == "unavailable" and result["items"] == []
    assert result["coverage"][0]["status"] == "unsupported_page"


def test_adding_an_explicit_source_does_not_expand_default_requests():
    assert D.validate({"query": "合成"}, now=NOW)["providers"] == ["sse", "sina"]
    assert D.validate({"stock_code": "600001"}, now=NOW)["providers"] == ["sse"]
    with pytest.raises(ValueError):
        arguments(providers=["sse", "sina", "cls"])
