"""Ordinary research questions use the information actually published at their cutoff."""

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from bs4 import BeautifulSoup

from server import futures_jm_research as research
from server import futures_jm_sources as sources
from server.futures_weekly_releases import Release


def at(daytime):
    return datetime.fromisoformat(daytime).replace(tzinfo=sources.SHANGHAI)


@pytest.mark.parametrize(
    ("question", "wanted"),
    [
        ("近月相对走强，两份合约都涨了吗？", "收盘变化 -63.5"),
        ("持仓变化能说明资金在做多吗？", "-21,058"),
        ("焦企在补库吗？", "752.66"),
        ("进口收紧了吗？", "761.77"),
        ("是进口收紧还是焦企补库？", "还需"),
    ],
)
async def test_dated_question_is_grounded_without_leaking_next_report(question, wanted):
    cutoff = at("2026-11-19T16:30")
    data = await research.analyze(question, cutoff.astimezone(UTC))
    assert data["status"] == "available"
    assert wanted in data["explanation"]
    assert data["choices"] and data["evidence"]
    for e in data["evidence"]:
        assert datetime.fromisoformat(e["as_of"]) <= cutoff
        assert e["source_url"].startswith("https://")
        assert "748.89" not in e["value"] and "783.08" not in e["value"]


async def test_supported_topics_match_native_presets_and_unknown_does_not_invent_data():
    for q in (
        "螺纹的持仓变化",
        "JM2801 的收盘价差",
        "苹果期货近月走强吗",
        "怎么做蛋糕？",
        "请算黄金明天的目标价",
        "",
    ):
        value = await research.analyze(q, at("2026-11-19T16:30"))
        assert value["status"] == "unsupported" and not value["choices"] and not value["evidence"]
    assert not (await research.analyze("焦煤持仓情况？", at("2026-11-10T16:30")))["choices"]


@pytest.mark.parametrize(
    ("cid", "status", "expected", "published"),
    [
        ("restock", "contradicted", "3.77", "08:33:00"),
        ("imports", "contradicted", "21.31", "09:00:00"),
        ("curve", "supported", "11.5", "16:14:05"),
        ("wait", "observed", "不计方向", "08:33:00"),
    ],
)
async def test_first_comparable_issue_only_after_original_publication(cid, status, expected, published):
    cutoff = at("2026-11-19T16:30")
    choice = next(c for c in (await research.analyze("焦煤供需", cutoff))["choices"] if c["id"] == cid)
    choice["tracking_started_at"] = cutoff.isoformat()
    disclosure = at(f"2026-11-20T{published}")
    pending = await research.review(choice, disclosure - timedelta(seconds=1))
    assert (pending["status"], pending["evidence"]) == ("pending", [])
    done = await research.review(choice, disclosure.astimezone(UTC))
    assert done["status"] == status and expected in done["explanation"]
    assert len(done["evidence"]) == 1
    choice["tracking_started_at"] = disclosure.isoformat()
    assert (await research.review(choice, disclosure))["status"] == "unverifiable"


async def test_issue_gap_equal_values_and_contract_substitution(monkeypatch):
    day = at("2026-11-19T16:30")
    data = await research.analyze("焦煤月差", day)
    choice = next(c for c in data["choices"] if c["id"] == "curve")
    assert choice["baseline"]["legs"] == ["JM2701", "JM2705"]
    wrong = Release("2026-11-20", "2026-11-20T16:30", "u", {"JM2701": 100, "JM2709": 15})
    later = Release("2026-11-23", "2026-11-23T16:30", "u2", {"JM2701": 300, "JM2705": 25})

    async def fake(_, **kwargs):
        return [wrong, later]

    monkeypatch.setattr(sources, "editions", fake)
    assert (await research.review(choice, at("2026-11-23T17:00")))["status"] == "pending"

    async def equal(_, **kwargs):
        return [Release("2026-11-20", "2026-11-20T16:30", "u", {"JM2701": 1318.5, "JM2705": 1200})]

    monkeypatch.setattr(sources, "editions", equal)
    equal_out = await research.review(choice, at("2026-11-23T17:00"))
    assert equal_out["status"] == "contradicted" and "持平" in equal_out["explanation"]


async def test_stale_data_cannot_seed_backdated_predictions_or_expired_legs(monkeypatch):
    async def stale(_):
        return list(sources.ARCHIVE)

    monkeypatch.setattr(sources, "editions", stale)
    d = await research.analyze("焦煤的供需呢", at("2027-06-01T16:30"))
    assert d["choices"] == []
    assert "已收录的历史数据" in d["explanation"]
    assert all("JM2701" not in row["value"] for row in d["evidence"])


def test_link_discovery_ignores_untrusted_links_and_other_sources():
    doc = BeautifulSoup(
        """
    <a href="/24798074">11月20日大商所焦煤期货行情</a>
    <a href='https://bad.invalid/24798074'>11月19日大商所焦煤期货行情</a>
    <a href='/24798070'>11月20日大商所焦炭期货行情</a>
    <a href='https://coal.mysteel.com.evil.invalid/a/26112008/279D99C8A60D88DB.html'>Mysteel数据：焦煤、焦炭港口库存调查统计（20261120）</a>
    <a href='//coal.mysteel.com/a/26112008/279D99C8A60D88DB.html'>Mysteel数据：焦煤、焦炭港口库存调查统计（20261120）</a>
    """,
        "html.parser",
    )
    day = date(2026, 11, 20)
    assert sources._links(doc, sources.DAILY, day) == {("market", day): sources._report_url(4798074)}
    assert sources._links(doc, sources.COAL, day) == {
        ("port", day): "https://coal.mysteel.com/a/26112008/279D99C8A60D88DB.html"
    }


def test_mysteel_requires_named_sample_and_report_identity():
    html = """<h1>Mysteel数据：独立焦企生产情况调查统计（20261120）</h1>
    <p>2026-11-20 08:33 来源：我的钢铁网(Mysteel)</p>
    <p>本周Mysteel统计独立焦企全样本：产能利用率为71.09%，炼焦煤总库存898.61万吨，焦煤可用天数11.1天。</p>
    <p>本周Mysteel统计全国230家独立焦企样本：产能利用率为70.34%，炼焦煤总库存748.89万吨，焦煤可用天数11.6天。</p>"""

    def parse(text):
        return sources._survey(
            BeautifulSoup(text, "html.parser"), "coke", date(2026, 11, 20), "https://jiaotan.mysteel.com/test"
        )

    issue = parse(html)
    assert issue.values == {"coke": 748.89, "use": 70.34, "days": 11.6}
    assert issue.known == "2026-11-20T08:33:00"
    for replacement in (
        html.replace("230家", "18家"),
        html.replace("调查统计（20261120）", "调查统计（20261119）"),
        html.replace("08:33", "收盘后"),
    ):
        with pytest.raises(ValueError):
            parse(replacement)


async def test_public_daily_fetch_discovers_new_real_months_from_dated_tables(monkeypatch):
    day = date(2027, 2, 2)
    index = "<a href='/25000003'>2月2日大商所焦煤期货行情</a>"
    report = """
    <div class='show_title'><h3>2月2日大商所焦煤期货行情</h3></div>
    <div class='show_title'><span>2027-02-02 16:14:03</span></div>
    <table><tr><td>交割月份</td><td>收盘价</td><td>成交手</td><td>变化</td></tr>
    <tr><td>jm2705</td><td>1400</td><td>10000</td><td>-30</td></tr>
    <tr><td>jm2709</td><td>1300</td><td>5000</td><td>40</td></tr></table>"""
    seen = []

    def handler(r):
        seen.append(r)
        return httpx.Response(200, text=index if r.url.path != "/detailspage.php" else report)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        issues = await sources._recent_for_index(client, sources.DAILY, day)
    assert len(seen) == 2 and all(r.url.host == "www.steelhome.com" for r in seen)

    async def live():
        sources._store_editions(issues)

    monkeypatch.setattr(sources, "_live", live)
    q = "焦煤的两份合约怎么走？"
    for t, visible in (("16:14:02", False), ("16:14:03", True)):
        d = await research.analyze(q, at("2027-02-02T" + t))
        curve = [c for c in d["choices"] if c["id"] == "curve"]
        assert bool(curve) == visible
        if visible:
            assert (
                curve[0]["baseline"]["legs"] == ["JM2705", "JM2709"] and curve[0]["baseline"]["value"] == 100
            )


async def test_frozen_public_editions_survive_a_restart_and_silent_revision(monkeypatch):
    first = Release(
        "2026-12-18",
        "2026-12-18T08:33:00",
        "https://coal.mysteel.com/original",
        {"coke": 700, "use": 70, "days": 10},
    )
    revised = Release(first.period, first.known, first.url, {"coke": 710, "use": 71, "days": 11})
    adjacent = Release(
        "2026-12-25",
        "2026-12-25T09:13:00",
        "https://coal.mysteel.com/later",
        {"coke": 701, "use": 70, "days": 12},
    )
    sources._store_editions([first, revised, adjacent])

    async def should_use_archive():
        raise AssertionError("target edition already saved")

    monkeypatch.setattr(sources, "_live", should_use_archive)
    target = await sources.editions(at("2027-02-08T17:00"), target=first.period, series="coke")
    assert target == [first]


async def test_missing_survey_fetches_even_if_other_dataset_already_archived(monkeypatch):
    day = "2026-12-18"
    coke = Release(
        day, day + "T08:33:00", "https://coal.mysteel.com/coke", {"coke": 700, "use": 70, "days": 10}
    )
    port = Release(day, day + "T09:00:00", "https://coal.mysteel.com/port", {"port": 790})
    sources._store_editions([coke])

    async def publish_port():
        sources._store_editions([port])

    monkeypatch.setattr(sources, "_live", publish_port)
    result = await sources.editions(at("2026-12-18T10:00"), target=day, series="port")
    assert result == [port]


async def test_only_recent_outstanding_current_record_triggers_scheduled_fetch(monkeypatch, temp_data_dir):
    from server.database import get_db

    calls = []

    async def stub():
        calls.append(1)

    monkeypatch.setattr(sources, "_live", stub)
    await sources.capture_for_saved_judgments()
    assert not calls
    with get_db() as db:
        db.execute(
            """INSERT INTO jm_research_journal(id,username,session,as_of,snapshot_json,saved_at)
            VALUES('example','u','replay','','{}',?)""",
            (datetime.now(sources.SHANGHAI).isoformat(),),
        )
    await sources.capture_for_saved_judgments()
    assert not calls
    with get_db() as db:
        db.execute("UPDATE jm_research_journal SET session='current' WHERE id='example'")
    await sources.capture_for_saved_judgments()
    assert calls == [1]
    with get_db() as db:
        db.execute("UPDATE jm_research_journal SET review_json='{}' WHERE id='example'")
    await sources.capture_for_saved_judgments()
    assert calls == [1]
