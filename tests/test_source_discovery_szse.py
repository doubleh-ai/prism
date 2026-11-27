"""Synthetic Shenzhen disclosure queries; dates and securities stay attributable."""

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


def args(**changes):
    return D.validate({"stock_code": "000001", **changes}, now=NOW)


def row(**changes):
    return {
        "secCode": ["000001"],
        "secName": ["合成公司"],
        "title": "合成产能披露",
        "publishTime": "2026-11-03 07:45:55",
        "attachPath": "/disc/fixture.pdf",
        **changes,
    }


def index(rows=None, total=1):
    return {"announceCount": total, "data": [row()] if rows is None else rows}


def test_code_selects_the_disclosure_source_without_user_configuration():
    assert args()["providers"] == ["szse"]
    assert args(query="合成")["providers"] == ["szse", "sina"]
    assert args(stock_code="600001")["providers"] == ["sse"]
    with pytest.raises(ValueError, match="no default disclosure"):
        args(stock_code="800001")


def test_official_attachment_keeps_second_precision_and_all_related_securities():
    items, coverage = D.parse_index(
        index([row(secCode=["000001", "200001"], secName=["合成公司", "合成公司B"])]),
        "szse",
        args(stock_code="200001"),
        now=NOW,
    )
    item = items[0]
    assert item["url"] == "https://disc.static.szse.cn/disc/fixture.pdf"
    assert item["stock_code"] == "200001" and item["stock_codes"] == ["000001", "200001"]
    assert item["published_at"] == "2026-11-03T07:45:55+08:00"
    assert item["publication_precision"] == "second"
    assert item["source_class"] == "official_disclosure" and item["original_verified"] is False
    assert coverage["complete_search"] is False


@pytest.mark.parametrize(
    "changes",
    [
        {"secCode": "000001"},
        {"secCode": [True]},
        {"secCode": ["000001", "000001"], "secName": ["合成公司", "重复"]},
        {"secName": []},
        {"publishTime": "2026-11-03"},
        {"publishTime": "2026-11-03 12:00:01"},
        {"publishTime": "2026-11-03 07:45:55+08:00"},
        {"attachPath": "https://unapproved.example/report.pdf"},
        {"attachPath": "https://127.0.0.1/report.pdf"},
        {"attachPath": "https://disc.static.szse.cn/report.pdf?token=synthetic"},
    ],
)
def test_wrong_security_shape_future_times_and_unapproved_links_are_not_evidence(changes):
    items, coverage = D.parse_index(index([row(**changes)]), "szse", args(), now=NOW)
    assert items == [] and coverage["rejected_entries"] == 1


def test_ignored_remote_filters_do_not_admit_wrong_security_date_or_keyword():
    payload = index(
        [
            row(secCode=["000002"]),
            row(publishTime="2026-10-01 09:00:00"),
            row(title="其他事项"),
        ],
        total=3,
    )
    items, coverage = D.parse_index(payload, "szse", args(query="产能"), now=NOW)
    assert items == [] and coverage["rows_received"] == 3


@pytest.mark.parametrize("total,rows", [(True, []), (-1, []), (1, [row(), row()]), (100, [{}] * 26)])
def test_schema_changes_are_not_a_successful_empty_query(total, rows):
    with pytest.raises(ValueError):
        D.parse_index(index(rows, total), "szse", args(), now=NOW)


def test_fixed_query_contains_only_public_filters_and_retains_actual_index_receipt(monkeypatch):
    calls = []

    def fetch(url, **options):
        calls.append((url, options))
        return {
            "status": "ok",
            "url": url,
            "data": index(total=60),
            "body_sha256": "a" * 64,
            "retrieved_at": NOW.isoformat(),
        }

    monkeypatch.setattr(D, "fetch_public_index", fetch)
    result = D.find({"stock_code": "000001", "providers": ["szse"], "query": "合成", "page": 2})
    assert result["status"] == "ok" and len(calls) == 1
    url, options = calls[0]
    assert url == "https://www.szse.cn/api/disc/announcement/annList"
    assert options["disclosure_query"] == {
        "seDate": ["2026-10-04", "2026-11-03"],
        "channelCode": ["listedNotice_disc"],
        "pageSize": 25,
        "pageNum": 2,
        "stock": ["000001"],
        "searchKey": ["合成"],
    }
    assert result["items"][0]["index_body_sha256"] == "a" * 64
    assert result["coverage"][0]["more_pages_available"] is True
    assert result["items"][0]["original_obtained"] is False
