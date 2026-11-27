from datetime import UTC, datetime

import pytest

from server import futures_freight_research as ec
from server.futures_weekly_releases import CALENDAR, NE3_NOTICE, RELEASES, SCFIS, SETTLEMENT_RULE, Release


def at(stamp):
    return datetime.fromisoformat(stamp + "+08:00")


@pytest.mark.asyncio
async def test_index_directions_replay_the_first_visible_official_publication():
    start = at("2026-11-17T16:30")
    answer = await ec.analyze("欧线结算运价指数最近一期怎么变了？", start.astimezone(UTC))
    assert answer["status"] == "available"
    assert "2289.56" in answer["explanation"] and "+90.90" in answer["explanation"]
    assert "2201.88" not in str(answer)
    assert answer["evidence"][-1] == {
        "label": "2026-11-16 SCFIS欧洲航线",
        "data_date": "2026-11-16",
        "as_of": "2026-11-17T00:00:00+08:00",
        "source_name": "上海航运交易所",
        "source_url": SCFIS,
        "value": "2289.56 点",
    }
    choices = {c["id"]: dict(c, tracking_started_at=start.isoformat()) for c in answer["choices"]}
    assert set(choices) == {"index_down", "index_up"}
    for cid, status in (("index_down", "supported"), ("index_up", "contradicted")):
        base = choices[cid]["baseline"]
        assert (
            base["key"],
            base["period"],
            base["target"],
            base["value"],
            base["unit"],
            base["source_url"],
        ) == (
            "index",
            "2026-11-16",
            "2026-11-23",
            2289.56,
            "点",
            SCFIS,
        )
        pending = await ec.review(choices[cid], at("2026-11-23T23:59"))
        assert pending["status"] == "pending" and not pending["evidence"]
        for cutoff in (at("2026-11-24T00:00"), at("2026-11-24T16:30")):
            result = await ec.review(choices[cid], cutoff)
            assert result["status"] == status
            assert "-87.68" in result["explanation"]
            assert result["evidence"][0]["data_date"] == "2026-11-23"
            assert result["evidence"][0]["value"] == "2201.88 点"


@pytest.mark.asyncio
async def test_review_ignores_first_unrelated_source_later_revision_and_wrong_period(monkeypatch):
    card = await ec.analyze("欧线运价", at("2026-11-17T16:30"))
    choice = card["choices"][0]
    samples = (
        *RELEASES["EC"],
        Release("2026-11-23", "2026-11-23T17:00", "https://example.com/spot", {"index": 3000}),
        Release("2026-11-23", "2026-11-25T08:00", SCFIS, {"index": 2900}),
    )
    monkeypatch.setitem(RELEASES, "EC", samples)
    assert (await ec.review(choice, at("2026-11-23T22:00")))["status"] == "pending"
    assert (await ec.review(choice, at("2026-12-01T16:30")))["status"] == "supported"
    for key, change in (
        ("scope", "全球"),
        ("target", "2026-11-30"),
        ("value", 1),
        ("version", "v2"),
        ("source_url", "other"),
    ):
        changed = dict(choice, baseline={**choice["baseline"], key: change})
        assert (await ec.review(changed, at("2026-11-24T16:30")))["status"] == "unverifiable"
    for stamp in ("2026-11-16T23:59", "2026-11-23T00:00"):
        unsealed = dict(choice, tracking_started_at=at(stamp).isoformat())
        assert (await ec.review(unsealed, at("2026-11-24T16:30")))["status"] == "unverifiable"

    flat = Release("2026-11-23", "2026-11-24T00:00", SCFIS, {"index": 2289.56})
    unchanged = [r for r in RELEASES["EC"] if r.period != flat.period]
    monkeypatch.setitem(RELEASES, "EC", (*unchanged, flat))
    for choice in card["choices"]:
        assert (await ec.review(choice, at("2026-11-24T16:30")))["status"] == "contradicted"


@pytest.mark.asyncio
async def test_index_tracking_is_limited_to_a_future_verified_publication_date():
    assert (await ec.analyze("欧线运价", at("2026-11-02T16:30")))["status"] == "unavailable"
    assert (await ec.analyze("焦煤指数怎么走？", at("2026-11-17T16:30")))["status"] == "unsupported"
    assert not (await ec.analyze("欧线指数", at("2026-11-23T00:00")))["choices"]
    card = await ec.analyze("SCFIS 怎么走？", at("2026-11-26T16:30"))
    assert card["choices"][0]["baseline"]["target"] == "2026-11-30"
    assert (await ec.review(card["choices"][0], at("2026-12-05T16:30")))["status"] == "pending"
    assert not (await ec.analyze("欧线指数", at("2026-12-05T16:30")))["choices"]
    with pytest.raises(ValueError):
        await ec.analyze("欧线指数", datetime(2026, 11, 26, 16, 30))


@pytest.mark.asyncio
async def test_daily_settlement_and_two_fixings_only_define_a_conditional_threshold(monkeypatch):
    question = "这个合约的交割结算价怎样计算？"
    a = await ec.analyze(question, at("2026-11-17T16:30"))
    assert "1/3" in a["explanation"] and "2201.88" not in str(a)
    b = await ec.analyze(question, at("2026-11-24T16:30"))
    assert "2/3" in b["explanation"] and "尚缺 2026-11-30" in b["explanation"] and "2120.56" not in str(b)
    c = await ec.analyze(question, at("2026-11-25T00:00"))
    assert all(term in c["explanation"] for term in ("2/3", "2204.00", "2120.56", "条件值", "待发布"))
    assert c["evidence"][-1]["source_url"].endswith("kx20261124.dat")
    assert SETTLEMENT_RULE in {r["source_url"] for r in c["evidence"]}
    assert not c["choices"]
    due = await ec.analyze(question, at("2026-12-01T16:30"))
    assert "尚缺 2026-11-30" in due["explanation"] and "2120.56" not in due["explanation"]
    monkeypatch.setitem(
        RELEASES, "EC", (*RELEASES["EC"], Release("2026-11-30", "2026-12-01T00:00", SCFIS, {"index": 2100}))
    )
    final = await ec.analyze(question, at("2026-12-01T16:30"))
    assert "3/3" in final["explanation"] and "2197.15" in final["explanation"]
    assert "2204.00" not in final["explanation"] and "2120.56" not in final["explanation"]


@pytest.mark.asyncio
async def test_capacity_distinguishes_announced_service_from_net_departing_capacity():
    prompt = "停航公告能说明运力减少吗？"
    before = await ec.analyze(prompt, at("2026-11-25T08:19"))
    assert before["status"] == "unavailable" and not before["choices"] and not before["evidence"]
    after = await ec.analyze(prompt, at("2026-11-25T08:20"))
    assert all(s in after["explanation"] for s in ("NE3 651W", "NE1 650W", "到期后", "实际可订舱位"))
    assert after["evidence"][0]["source_url"] == NE3_NOTICE and not after["choices"]
    expired = await ec.analyze(prompt, at("2026-12-16T00:00"))
    assert expired["status"] == "unavailable"


@pytest.mark.asyncio
async def test_index_release_calendar_is_independent_of_the_delivery_contract(monkeypatch):
    card = await ec.analyze("欧线运价", at("2026-11-10T09:00"))
    choice = next(c for c in card["choices"] if c["id"] == "index_up")
    assert (choice["baseline"]["period"], choice["baseline"]["target"]) == ("2026-11-09", "2026-11-16")
    assert choice["baseline"]["calendar_url"] == CALENDAR
    assert card["evidence"][0]["source_url"] == CALENDAR and "2026-11-16" in card["evidence"][0]["value"]
    assert (await ec.review(choice, at("2026-11-17T00:00")))["status"] == "supported"

    # Isolated observations for later weeks; the reviewed calendar is real.
    additions = (
        Release("2026-11-30", "2026-12-01T00:00", SCFIS, {"index": 2180.0}),
        Release("2026-12-07", "2026-12-08T00:00", SCFIS, {"index": 2250.0}),
        Release("2026-12-28", "2026-12-29T00:00", SCFIS, {"index": 2000.0}),
    )
    monkeypatch.setitem(RELEASES, "EC", (*RELEASES["EC"], *additions))
    dec = await ec.analyze("欧线运价", at("2026-12-01T16:30"))
    choice = next(c for c in dec["choices"] if c["id"] == "index_down")
    assert (choice["baseline"]["period"], choice["baseline"]["target"]) == ("2026-11-30", "2026-12-07")
    assert (await ec.review(choice, at("2026-12-07T23:59")))["status"] == "pending"
    assert (await ec.review(choice, at("2026-12-08T00:00")))["status"] == "contradicted"
    end = await ec.analyze("欧线运价", at("2026-12-29T09:00"))
    assert end["status"] == "available" and not end["choices"]
    assert "2026-12-28" in end["explanation"] and "待核对" in end["explanation"]


@pytest.mark.asyncio
async def test_other_contract_and_different_freight_index_do_not_silently_answer_as_ec2611():
    for question in ("EC2612的交割结算价？", "SCFI欧线运价多少？"):
        answer = await ec.analyze(question, at("2026-11-25T08:30"))
        assert answer["status"] == "unsupported" and not answer["choices"]
