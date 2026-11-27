from datetime import UTC, datetime

import pytest

from server import futures_meal_research as meal
from server import futures_weekly as weekly
from server.futures_weekly_releases import RELEASES, Release


def at(stamp):
    return datetime.fromisoformat(stamp + "+08:00")


@pytest.mark.asyncio
async def test_question_scope_and_two_different_publication_clocks():
    start = at("2026-11-16T18:00")
    card = await meal.analyze("豆粕去库是需求拉动还是压榨变化？", start)
    text = card["explanation"]
    assert all(value in text for value in ("125.23", "795.39", "180.09", "205.53", "缺少前一周", "尚待披露"))
    assert not any(value in text for value in ("117.20", "799.63", "180.26"))
    assert {c["id"] for c in card["choices"]} == set(meal.RULES)
    assert all(datetime.fromisoformat(e["as_of"]) <= start for e in card["evidence"])
    stock = await meal.analyze("油厂豆粕库存这周怎么变了？", start)
    assert [c["id"] for c in stock["choices"]] == ["meal_stock_down", "beans_stock_up"]
    assert all("库存" in e["label"] for e in stock["evidence"])
    crush = await meal.analyze("实际压榨和预估相差多少？", start)
    assert [c["id"] for c in crush["choices"]] == ["crush_below_plan", "crush_up"]
    choice = next(c for c in card["choices"] if c["id"] == "meal_stock_down")
    assert choice["baseline"] == dict(
        key="meal_stock",
        period="2026-11-13",
        known="2026-11-16T16:27",
        scope="Mysteel全国主要油厂动态样本",
        unit="万吨",
        value=125.23,
        version="m-weekly-v1",
        target="2026-11-20",
        source_url="https://ncp.mysteel.com/a/26111616/202FA10468E86DF6.html",
    )

    choices = {c["id"]: dict(c, tracking_started_at=start.isoformat()) for c in card["choices"]}
    for cid, just_before, first_publication, difference in (
        ("meal_stock_down", "2026-11-23T16:16", "2026-11-23T16:17", "-8.03"),
        ("beans_stock_up", "2026-11-23T16:16", "2026-11-23T16:17", "+4.24"),
        ("crush_below_plan", "2026-11-20T19:10", "2026-11-20T19:11", "-25.27"),
        ("crush_up", "2026-11-20T19:10", "2026-11-20T19:11", "+0.17"),
    ):
        pending = await meal.review(choices[cid], at(just_before))
        assert pending["status"] == "pending" and not pending["evidence"]
        result = await meal.review(choices[cid], at(first_publication).astimezone(UTC))
        assert result["status"] == "supported" and difference in result["explanation"]
        assert result["evidence"][0]["as_of"] == at(first_publication).isoformat()

    end = await meal.analyze("去库能说明需求增强吗？", at("2026-11-23T18:00"))
    assert all(
        x in end["explanation"]
        for x in ("-8.03", "+4.24", "-25.27", "+0.17", "单靠减产难以解释去库", "旧合同", "新增饲料采购")
    )
    assert not any(c["id"] == "crush_below_plan" for c in end["choices"])


@pytest.mark.asyncio
async def test_no_retrospective_forecast_or_cross_sample_regrading(monkeypatch):
    start = at("2026-11-16T18:00")
    card = await meal.analyze("压榨预估是多少？", start)
    choice = next(c for c in card["choices"] if c["id"] == "crush_below_plan")
    new_forecasts = (
        Release("2026-11-20", "2026-11-20T19:12", "https://example.org/late-plan", {"crush_plan": 180.26}),
        Release("2026-11-27", "2026-11-20T19:12", "https://example.org/new-plan", {"crush_plan": 213.15}),
    )
    monkeypatch.setitem(RELEASES, "M", (*RELEASES["M"], *new_forecasts))
    current = await meal.analyze("实际压榨达到计划了吗？", at("2026-11-23T18:00"))
    assert "-25.27" in current["explanation"] and "213.15" in current["explanation"]
    assert any("205.53" in e["value"] for e in current["evidence"])
    assert all("late-plan" not in e["source_url"] for e in current["evidence"])

    # The published actual may eventually be revised; a sealed rule uses its first release.
    correction = Release("2026-11-20", "2026-11-24T18:00", "https://example.org/revised", {"crush": 210.0})
    monkeypatch.setitem(RELEASES, "M", (*RELEASES["M"], correction))
    assert (await meal.review(choice, at("2026-11-25T18:00")))["status"] == "supported"
    for field, val in (("key", "arrivals"), ("scope", "饲料厂"), ("unit", "天"), ("target", "2026-11-27")):
        changed = dict(choice, baseline={**choice["baseline"], field: val})
        assert (await meal.review(changed, at("2026-11-23T18:00")))["status"] == "unverifiable"
    late = dict(choice, tracking_started_at=at("2026-11-20T00:00").isoformat())
    assert (await meal.review(late, at("2026-11-23T18:00")))["status"] == "unverifiable"


@pytest.mark.asyncio
async def test_current_tracks_only_the_next_period_and_requires_its_exact_observation(monkeypatch):
    card = await meal.analyze("原料豆和豆粕库存变化一致吗？", at("2026-11-26T18:00"))
    stock = card["choices"][0]
    assert stock["baseline"]["value"] == 117.20 and stock["baseline"]["target"] == "2026-11-27"
    wrong_period = Release(
        "2026-12-04", "2026-12-07T15:00", "https://example.org/skipped", {"meal_stock": 90}
    )
    monkeypatch.setitem(RELEASES, "M", (*RELEASES["M"], wrong_period))
    assert (await meal.review(stock, at("2026-12-08T10:00")))["status"] == "pending"
    unchanged = Release(
        "2026-11-27", "2026-11-30T16:00", "https://example.org/on-week", {"meal_stock": 117.20}
    )
    monkeypatch.setitem(RELEASES, "M", (*RELEASES["M"], unchanged))
    assert (await meal.review(stock, at("2026-11-30T15:59")))["status"] == "pending"
    result = await meal.review(stock, at("2026-11-30T16:00"))
    assert result["status"] == "contradicted" and "+0.00" in result["explanation"]
    stale = await meal.analyze("油厂库存？", at("2026-11-27T00:00"))
    assert not stale["choices"] and "2026-11-20" in stale["explanation"]


@pytest.mark.asyncio
async def test_missing_topic_and_historical_data_are_explicit():
    early = await meal.analyze("库存？", at("2026-11-16T16:26"))
    assert early["status"] == "unavailable" and not early["evidence"] and not early["choices"]
    for question in ("现在可以做多豆粕吗？", "螺纹钢库存", "资金流", "饲料厂库存天数？"):
        assert (await meal.analyze(question, at("2026-11-26T18:00")))["status"] == "unsupported"
    with pytest.raises(ValueError):
        await meal.analyze("豆粕库存", datetime(2026, 11, 20))


@pytest.mark.asyncio
async def test_plan_only_never_claims_actual_and_later_plan_is_not_a_forecast(monkeypatch):
    target = "2026-11-20"
    plan = Release(target, "2026-11-13T15:00", "https://example.org/plan", {"crush_plan": 200})
    actual = Release(target, "2026-11-20T12:00", "https://example.org/actual", {"crush": 180})
    late = Release(target, "2026-11-20T12:01", "https://example.org/late", {"crush_plan": 178})
    monkeypatch.setitem(RELEASES, "M", (plan, actual, late))
    only = await meal.analyze("实际压榨和预估相差多少？", at("2026-11-16T18:00"))
    assert "尚待披露" in only["explanation"] and "实际较预估" not in only["explanation"]
    assert [c["id"] for c in only["choices"]] == ["crush_below_plan"]
    report = weekly.research("M", at("2026-11-16T18:00"))
    assert report["last_observed"] is None and report["historical"] is False
    assert "只有预估" in "".join(report["reading"])
    published = weekly.research("M", at("2026-11-20T12:00"))
    assert "-20.00" in "".join(published["reading"]) and "只有预估" not in "".join(published["reading"])
    assert (await meal.review(only["choices"][0], at("2026-11-20T11:59")))["status"] == "pending"

    # Simultaneous release and a forecast after the first actual both fail the time boundary,
    # including when a later actual revision would otherwise make them look early.
    actual = actual._replace(known="2026-11-18T15:00")
    simultaneous = plan._replace(known=actual.known)
    late = late._replace(known="2026-11-19T15:00")
    correction = actual._replace(known="2026-11-20T15:00")
    monkeypatch.setitem(RELEASES, "M", (simultaneous, correction, late, actual))
    result = await meal.analyze("实际压榨和预估相差多少？", at("2026-11-20T18:00"))
    assert "未收录该周事前压榨预估" in result["explanation"] and "实际较预估" not in result["explanation"]
    assert all("预估" not in e["label"] for e in result["evidence"])
    report = weekly.research("M", at("2026-11-20T18:00"))
    assert all(r["key"] != "crush_plan" for r in report["rows"])
    assert "预估" not in "".join(report["reading"])
