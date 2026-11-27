from datetime import date, datetime

import pytest

from scripts.evaluate_futures_curve import procurement
from server import futures, futures_weekly
from server.futures_weekly_releases import NE3_NOTICE, RELEASES, SERIES


def at(symbol, stamp):
    return futures_weekly.research(symbol, datetime.fromisoformat(stamp + "+08:00"))


def test_rebar_replay_before_and_after_independent_terminal_release():
    assert at("RB", "2026-11-12T15:00") is None
    friday = at("RB", "2026-11-20T15:00")
    assert "188.31" in "".join(friday["reading"]) and "等待西本周报" in "".join(friday["reading"])
    for stamp, visible in [("07:26", False), ("07:27", True)]:
        saturday = at("RB", "2026-11-21T" + stamp)
        assert ("-0.08" in "".join(saturday["reading"])) is visible
        assert ("649563" in " ".join(saturday["sources"])) is visible
    assert at("RB", "2026-12-01T15:00")["historical"]
    assert at("CU", "2026-11-21T15:00") is None


def test_soy_plan_is_knowable_before_realization_and_arrival_is_published_later():
    before = at("M", "2026-11-20T19:10")
    assert "只有预估" in "".join(before["reading"])
    after = at("M", "2026-11-20T19:11")
    assert "-25.27" in "".join(after["reading"]) and "+0.17" in "".join(after["reading"])
    assert not any(r["period"] == "2026-11-20" and r["key"] == "arrivals" for r in after["rows"])
    monday = at("M", "2026-11-23T16:26")
    assert all(x in "".join(monday["reading"]) for x in ("-9.10", "+4.24", "-8.03"))


def test_ec_settlement_uses_only_two_valid_fixings_and_the_named_contract():
    early = at("EC", "2026-11-23T10:00")
    assert "1/3" in "".join(early["reading"])
    assert not any(r["value"] == 2201.88 for r in early["rows"])
    later = at("EC", "2026-11-25T09:00")
    assert all(x in "".join(later["reading"]) for x in ("2/3", "4491.44", "2120.56", "NE1 650W"))
    assert NE3_NOTICE in later["sources"] and NE3_NOTICE not in early["sources"]


def test_expired_contract_waits_for_actual_final_fix_then_replays_it(monkeypatch):
    from server.futures_weekly_releases import SCFIS, Release

    expired = at("EC", "2026-12-01T09:00")
    assert "已到期" in "".join(expired["reading"]) and "需为" not in "".join(expired["reading"])
    # Synthetic last fixing. Its numerical value must be unavailable before the dated release.
    addition = Release("2026-11-30", "2026-12-01T00:00", SCFIS, {"index": 2100})
    monkeypatch.setitem(RELEASES, "EC", (*RELEASES["EC"], addition))
    assert "3/3" not in "".join(at("EC", "2026-11-30T15:00")["reading"])
    assert "2197.15" in "".join(at("EC", "2026-12-01T00:00")["reading"])


def test_series_scope_and_known_time_for_every_vintage():
    for symbol, releases in RELEASES.items():
        keys = set()
        for release in releases:
            day = date.fromisoformat(release.period)
            known = datetime.fromisoformat(release.known)
            for key, value in release.values.items():
                assert key in SERIES[symbol] and value >= 0 and release.url.startswith("https://")
                assert (day, key, known) not in keys
                keys.add((day, key, known))
                assert day <= known.date() or key == "crush_plan"


def test_registered_evidence_preserves_actual_source_period_and_forecast_label():
    weekly = at("M", "2026-11-17T09:00")
    doc = futures.snapshot_document(
        {
            "symbol": "M",
            "source": {"name": "测试", "url": "https://example.com"},
            "collected_at": weekly["as_of"],
            "weekly_research": weekly,
            "data_as_of": "2026-11-17",
        }
    )
    section = next(s for s in doc["sections"] if s["section"] == "weekly_crush_plan")
    page = doc["pages"][section["page"] - 1]["text"]
    assert section["url"].endswith("99FD9AEB5E42D319.html")
    assert section["data_period"] == "2026-11-20" and "预估" in page and "2026-11-13T17:56" in page


def test_weekly_change_requires_consecutive_fridays_and_valid_input_table():
    rows = [{"period": d, "key": "purchases", "value": v} for d, v in [("2026-11-06", 1), ("2026-11-20", 2)]]
    assert futures_weekly._weekly_change(rows, "purchases") is None
    with pytest.raises(ValueError):
        procurement(b"<table><tr><td>42</td></tr></table>")


def test_procurement_replay_uses_lagged_prices_and_fixed_contracts():
    from datetime import timedelta

    from scripts.evaluate_futures_curve import replay_purchases

    friday = date(2026, 3, 13)
    week = timedelta(days=7)
    purchases = {friday - week: 12, friday: 14, friday + week: 14, friday + 2 * week: 14}

    def contracts(a, b):
        return [
            {"contract": c, "delivery_month": m, "volume": 100, "open_interest": 100, "settlement": v}
            for c, m, v in [("rb2606", "2026-06", a), ("rb2610", "2026-10", b)]
        ]

    reports = {
        friday: contracts(3500, 3550),
        friday + week: contracts(3400, 3520),
        friday + 2 * week: contracts(3410, 3525),
    }
    results = replay_purchases(purchases, reports, friday)
    assert results["later"]["n"] == 1
    assert results["later"]["gross_mean_yuan_per_tonne"] == 5
    reports[friday + 2 * week][0]["contract"] = "rb2607"
    assert replay_purchases(purchases, reports, friday)["all"] == {"n": 0}
