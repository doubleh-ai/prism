from datetime import UTC, date, datetime, time
from urllib.parse import urlsplit

import pytest

from server import futures_institutional_views as iv


def at(day, hour=12, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=iv.SHANGHAI)


def test_date_only_uses_publication_day_and_no_earlier_report_cover_date():
    # The Nov 20 freight report was posted on Nov 23; exact time of day is unknown.
    assert iv.views("EC", at(20))["status"] == "unavailable"
    assert iv.views("EC", at(23, 23, 59))["latest_published_on"] is None
    card = iv.views(" ec ", datetime(2026, 11, 23, 16, tzinfo=UTC))
    assert card["latest_published_on"] == "2026-11-23"
    assert [row["stance"] for row in card["items"]] == ["bullish"]
    assert card["missing_sides"] == ["bearish"]


def test_known_publication_minute_is_inclusive_and_in_shanghai():
    assert len(iv.views("EC", at(26, 10, 14))["items"]) == 1
    card = iv.views("EC", datetime(2026, 11, 26, 2, 15, tzinfo=UTC))
    assert [(r["institution"], r["stance"]) for r in card["items"]] == [
        ("瑞达期货", "bearish"),
        ("上海东亚期货", "bullish"),
    ]
    assert card["missing_sides"] == []
    assert {r["horizon"] for r in card["items"]} == {"近期，欧线现货运价", "十二月上半月，欧线现货运价"}


@pytest.mark.parametrize("symbol", ["JM", "RB", "M", "EC"])
def test_reports_link_back_to_publishers_and_keep_horizon_and_conditions(symbol):
    card = iv.views(symbol, at(26))
    assert card["status"] == "available"
    assert card["window_days"] == 7
    assert all(
        urlsplit(r["url"]).scheme == "https"
        and urlsplit(r["url"]).hostname in {"www.eafutures.com", "www.rdqh.com"}
        and r["summary"]
        and r["condition"]
        and r["horizon"]
        and date.fromisoformat(r["published_on"]) <= at(26).date()
        for r in card["items"]
    )


def test_expired_opinion_does_not_populate_a_side():
    card = iv.views("EC", at(30, 0))
    assert len(card["items"]) == 1
    assert card["items"][0]["institution"] == "瑞达期货"
    assert card["missing_sides"] == ["bullish"]
    card = iv.views("EC", datetime(2026, 12, 3, tzinfo=iv.SHANGHAI))
    assert (card["status"], card["items"], card["latest_published_on"]) == ("stale", [], "2026-11-26")
    assert card["missing_sides"] == ["bullish", "bearish"]


def test_missing_and_neutral_sources_do_not_manufacture_disagreement():
    assert iv.views("AU", at(26))["status"] == "unavailable"
    card = iv.views("RB", at(26))
    assert {row["institution"]: row["stance"] for row in card["items"]} == {
        "瑞达期货": "unclassified",
        "上海东亚期货": "neutral",
    }
    assert card["missing_sides"] == ["bullish", "bearish"]


def test_meal_supply_and_past_weakness_are_not_attributed_price_forecasts():
    card = iv.views("M", at(26))
    assert len(card["items"]) == 2
    assert all(row["stance"] == "unclassified" for row in card["items"])
    assert card["missing_sides"] == ["bullish", "bearish"]


def test_later_report_replaces_prior_stance_only_after_publication(monkeypatch):
    older = iv.VIEWS["EC"][0]
    published = iv.Report(
        older.report.institution, "后续报告", older.report.url, date(2026, 11, 26), time(13, 30)
    )
    newer = iv.View(published, "neutral", "近月", "记录新的研究结论。", "观察下次公布。")
    monkeypatch.setitem(iv.VIEWS, "EC", (older, newer))
    assert [r["stance"] for r in iv.views("EC", at(26, 13, 29))["items"]] == ["bullish"]
    assert [r["stance"] for r in iv.views("EC", at(26, 13, 30))["items"]] == ["neutral"]


def test_naive_time_cannot_select_a_live_view():
    with pytest.raises(ValueError, match="timezone"):
        iv.views("EC", datetime(2026, 11, 26))
