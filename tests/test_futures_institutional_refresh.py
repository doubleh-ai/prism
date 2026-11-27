"""Source refresh, revisions and release-time visibility using isolated application SQLite."""

import asyncio
import hashlib
from datetime import datetime, timedelta

import pytest

from server import database
from server import futures_institutional_refresh as refresh
from server import futures_institutional_sources as sources
from server import futures_institutional_views as iv
from server.work_lifecycle import Manager


def stamp(day, hour=12, minute=0):
    return datetime(2026, 12, day, hour, minute, tzinfo=iv.SHANGHAI)


def report(publisher, day, text, suffix=None):
    name = refresh.PUBLISHERS[publisher]
    host = {"ruida": "www.rdqh.com", "east_asia": "www.eafutures.com"}[publisher]
    tail = suffix or (
        f"content/show/61/{200000 + day}"
        if publisher == "ruida"
        else f"storage/file/202612{day:02d}/abcdef1234567890.pdf#page=3"
    )
    return dict(
        symbol="EC",
        institution=name,
        title="近期运价资料",
        url=f"https://{host}/{tail}",
        published_on=stamp(day).date().isoformat(),
        available_at=stamp(day, 10).isoformat(),
        locator="欧线第3页",
        excerpt=text,
    )


def stored():
    with database.get_db() as db:
        return db.execute("SELECT COUNT(*) FROM futures_institutional_editions").fetchone()[0]


@pytest.fixture
def feeds(monkeypatch):
    clock = [stamp(4)]
    publishers = {
        "east_asia": [report("east_asia", 3, "预计欧洲航线运价继续上涨。")],
        "ruida": [report("ruida", 4, "预计欧线运价继续下跌。")],
    }
    calls = []

    async def collect(publisher, now):
        calls.append(publisher)
        await asyncio.sleep(0)
        value = publishers[publisher]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(sources, "collect", collect)
    monkeypatch.setattr(refresh, "_now", lambda: clock[0])
    return clock, publishers, calls


async def test_refresh_runs_once_across_workers_and_restarts_and_reads_make_no_requests(feeds):
    clock, _, calls = feeds
    await asyncio.gather(refresh.refresh_due(Manager()), refresh.refresh_due(Manager()))
    assert sorted(calls) == ["east_asia", "ruida"] and stored() == 2
    # Both reports were published before the server first actually saw them.
    assert iv.views("EC", clock[0] - timedelta(microseconds=1))["items"] == []
    before = iv.views("EC", clock[0])
    assert len(before["items"]) == 2
    assert all(x["anchor"] and x["locator"] and x["url"] for x in before["items"])
    database.reset_connection()
    clock[0] += timedelta(hours=5, minutes=59)
    await refresh.refresh_due(Manager())
    assert iv.views("EC", clock[0]) == before
    assert len(calls) == 2
    clock[0] += timedelta(minutes=1)
    await refresh.refresh_due(Manager())
    assert len(calls) == 4 and stored() == 2


async def test_partial_publisher_failure_keeps_other_update_and_retries_later(feeds):
    clock, publishers, calls = feeds
    await refresh.refresh_due(Manager())
    clock[0] += timedelta(days=1)
    publishers["east_asia"] = ValueError("synthetic publication unavailable")
    publishers["ruida"] = [report("ruida", 5, "预计欧线运价继续上涨。")]
    await refresh.refresh_due(Manager())
    rows = iv.views("EC", clock[0])["items"]
    assert {(r["institution"], r["published_on"]) for r in rows} == {
        ("上海东亚期货", "2026-12-03"),
        ("瑞达期货", "2026-12-05"),
    }
    count = len(calls)
    clock[0] += timedelta(minutes=29)
    await refresh.refresh_due(Manager())
    assert len(calls) == count
    clock[0] += timedelta(minutes=1)
    await refresh.refresh_due(Manager())
    assert calls[count:] == ["east_asia"]


async def test_same_url_revision_and_later_unclassified_supersede_only_when_observed(feeds):
    clock, publishers, _ = feeds
    t1 = clock[0]
    await refresh.refresh_due(Manager())
    clock[0] += timedelta(hours=6)
    t2 = clock[0]
    publishers["ruida"][0]["excerpt"] = "预计欧线运价继续上涨。"
    await refresh.refresh_due(Manager())
    assert stored() == 3

    def row(t):
        return next(i for i in iv.views("EC", t)["items"] if i["institution"] == "瑞达期货")

    assert row(t1)["anchor"] == "预计欧线运价继续下跌。"
    assert row(t2)["anchor"] == "预计欧线运价继续上涨。"
    clock[0] = stamp(5)
    publishers["ruida"] = [report("ruida", 5, "原文仅列市场公告。")]
    await refresh.refresh_due(Manager())
    result = iv.views("EC", clock[0])
    assert not any(i["institution"] == "瑞达期货" for i in result["items"])
    assert [r["published_on"] for r in result["unclassified_items"]] == ["2026-12-05"]
    assert row(t2)["published_on"] == "2026-12-04"
    assert not iv.views("EC", stamp(13))["items"]


async def test_closed_market_admission_stops_collector_before_network_or_sqlite_claim(feeds):
    _, _, calls = feeds
    manager = Manager()
    manager.begin_drain(manager.ticket())
    await refresh.refresh_due(manager)
    assert calls == [] and stored() == 0
    with database.get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM futures_institutional_refresh").fetchone()[0] == 0


async def test_matching_editorial_section_is_preserved_until_content_changes(monkeypatch, feeds):
    clock, publishers, _ = feeds
    clock[0] = datetime(2026, 11, 26, 11, tzinfo=iv.SHANGHAI)
    seed = iv.VIEWS["EC"][1]
    excerpt = "预计欧线运价继续下跌。"
    row = {
        **report("ruida", 1, excerpt),
        "url": seed.report.url,
        "published_on": seed.report.published_on.isoformat(),
        "available_at": seed.report.visible_at().isoformat(),
    }
    monkeypatch.setitem(iv.REVIEWED_SECTIONS, hashlib.sha256(excerpt.encode()).hexdigest(), "运价继续下跌")
    publishers.update(east_asia=[], ruida=[row])
    first = clock[0]
    await refresh.refresh_due(Manager())
    current = iv.views("EC", first)["items"][0]
    assert (current["summary"], current["horizon"], current["anchor"]) == (
        seed.summary,
        seed.horizon,
        "运价继续下跌",
    )
    clock[0] += timedelta(hours=6)
    row["excerpt"] = "预计欧线运价继续上涨。"
    await refresh.refresh_due(Manager())
    corrected = iv.views("EC", clock[0])["items"][0]
    assert corrected["stance"] == "bullish" and corrected["summary"] != seed.summary
    assert iv.views("EC", first)["items"][0] == current


async def test_publisher_correction_to_earlier_release_time_supersedes_existing_copy(feeds):
    clock, publishers, _ = feeds
    await refresh.refresh_due(Manager())
    row = publishers["ruida"][0]
    row.update(
        published_on="2026-12-03", available_at=stamp(3, 10).isoformat(), excerpt="预计欧线运价继续上涨。"
    )
    clock[0] += timedelta(hours=6)
    await refresh.refresh_due(Manager())
    corrected = next(i for i in iv.views("EC", clock[0])["items"] if i["institution"] == "瑞达期货")
    assert corrected["published_on"] == "2026-12-03" and corrected["stance"] == "bullish"


async def test_failed_pdf_download_keeps_same_issue_but_new_issue_displaces_old_quote(feeds):
    clock, publishers, _ = feeds
    await refresh.refresh_due(Manager())
    publishers["east_asia"][0].update(excerpt=None, download_unavailable=True, locator="官网周报目录")
    clock[0] += timedelta(hours=6)
    await refresh.refresh_due(Manager())
    view = iv.views("EC", clock[0])
    assert "上海东亚期货" in {i["institution"] for i in view["items"]}
    clock[0] = stamp(5)
    publishers["east_asia"] = [{**report("east_asia", 5, None), "download_unavailable": True}]
    await refresh.refresh_due(Manager())
    view = iv.views("EC", clock[0])
    assert "上海东亚期货" not in {i["institution"] for i in view["items"]}
    assert [(i["institution"], i["published_on"]) for i in view["unclassified_items"]] == [
        ("上海东亚期货", "2026-12-05")
    ]
