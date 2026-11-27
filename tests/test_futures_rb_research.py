from datetime import datetime

import pytest

from server import futures_rb_research as rb
from server.futures_weekly_releases import RELEASES, Release


def at(time):
    return datetime.fromisoformat(time + "+08:00")


@pytest.mark.asyncio
async def test_research_and_replay_follow_what_had_been_published():
    before = at("2026-11-16T16:30")
    card = await rb.analyze("是需求回暖，还是钢厂减产？", before)
    assert card["status"] == "available"
    assert all(datetime.fromisoformat(e["as_of"]) <= before for e in card["evidence"])
    assert "188.31" not in card["explanation"]
    choices = {c["id"]: {**c, "tracking_started_at": before.isoformat()} for c in card["choices"]}
    for cid, status, change in (
        ("society_down", "supported", "-10.13"),
        ("mill_down", "contradicted", "+4.46"),
        ("output_down", "supported", "-0.18"),
    ):
        pending = await rb.review(choices[cid], at("2026-11-19T14:15"))
        assert pending["status"] == "pending" and not pending["evidence"]
        decision = await rb.review(choices[cid], at("2026-11-19T14:16"))
        assert decision["status"] == status and change in decision["explanation"]
        assert decision["evidence"][0]["data_date"] == "2026-11-19"

    friday = await rb.analyze("去库就能证明需求回暖吗？", at("2026-11-20T16:30"))
    assert "188.31" in friday["explanation"] and "不足以判定需求回暖" in friday["explanation"]
    assert "-0.08" not in friday["explanation"]
    assert (await rb.review(choices["purchases_up"], at("2026-11-21T07:26")))["status"] == "pending"
    saturday = await rb.review(choices["purchases_up"], at("2026-11-21T07:27"))
    assert saturday["status"] == "contradicted" and "-0.08" in saturday["explanation"]


@pytest.mark.asyncio
async def test_question_controls_evidence_and_pending_tells_user_which_week():
    cutoff = at("2026-11-26T10:00")
    card = await rb.analyze("终端采购能印证需求吗？", cutoff)
    assert [c["id"] for c in card["choices"]] == ["purchases_up"]
    assert all("西本" in e["label"] for e in card["evidence"])
    assert "2026-11-20" in card["explanation"] and "-0.08" in card["explanation"]
    choice = card["choices"][0]
    assert choice["baseline"]["target"] == "2026-11-27"
    pending = await rb.review(choice, at("2026-12-05T10:00"))
    assert pending["status"] == "pending" and "2026-11-27" in pending["explanation"]
    stale = await rb.analyze("螺纹厂库下降了吗？", at("2026-12-05T10:00"))
    assert not stale["choices"] and "2026-11-19" in stale["explanation"]
    early = await rb.analyze("厂库下降了吗？", at("2026-11-10T16:30"))
    assert early["status"] == "unavailable"
    assert (await rb.analyze("豆粕库存是多少？", cutoff))["status"] == "unsupported"


@pytest.mark.asyncio
async def test_later_revisions_never_regrade_old_prediction_or_validate_post_release_seal(monkeypatch):
    card = await rb.analyze("社库和厂库的变化一致吗？", at("2026-11-16T16:30"))
    choice = next(c for c in card["choices"] if c["id"] == "society_down")
    revision = Release("2026-11-19", "2026-11-20T10:00", "https://example.com/revision", {"society": 330.0})
    monkeypatch.setitem(RELEASES, "RB", (*RELEASES["RB"], revision))
    assert (await rb.review(choice, at("2026-11-21T16:30")))["status"] == "supported"
    late = {**choice, "tracking_started_at": at("2026-11-19T14:16").isoformat()}
    assert (await rb.review(late, at("2026-11-21T16:30")))["status"] == "unverifiable"
    changed = {**choice, "baseline": {**choice["baseline"], "scope": "单一城市"}}
    assert (await rb.review(changed, at("2026-11-21T16:30")))["status"] == "unverifiable"


@pytest.mark.asyncio
async def test_current_choices_require_a_new_future_period_even_when_target_report_is_late():
    today = await rb.analyze("去库能印证需求吗？", at("2026-11-26T10:28"))
    assert [choice["id"] for choice in today["choices"]] == ["purchases_up"]
    assert today["choices"][0]["baseline"]["target"] == "2026-11-27"
    assert "2026-11-19" in today["explanation"] and "2026-11-20" in today["explanation"]
    yesterday = await rb.analyze("社会库存下降了吗？", at("2026-11-25T23:50"))
    choice = next(c for c in yesterday["choices"] if c["id"] == "society_down")
    sealed = {**choice, "tracking_started_at": at("2026-11-26T00:01").isoformat()}
    assert (await rb.review(sealed, at("2026-11-26T10:28")))["status"] == "unverifiable"
    assert (await rb.review(choice, at("2026-11-26T10:28")))["status"] == "pending"
