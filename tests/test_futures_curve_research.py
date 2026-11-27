from datetime import date, timedelta

from scripts.evaluate_futures_curve import replay
from server.futures_curve_research import active_pair

DAY = date(2026, 1, 2)
WEEK = timedelta(days=7)


def row(month, price, volume):
    return {
        "contract": f"rb26{month:02}",
        "delivery_month": f"2026-{month:02}",
        "volume": volume,
        "open_interest": 500,
        "settlement": price,
    }


def test_replay_lags_entry_and_preserves_both_legs_after_volume_roll():
    observations = {
        DAY: [row(2, 130, 2000), row(3, 112, 400), row(5, 100, 300), row(9, 90, 10)],
        DAY + WEEK: [row(3, 132, 70), row(5, 100, 42), row(9, 110, 900)],
        DAY + 2 * WEEK: [row(3, 129, 40), row(5, 102, 50), row(9, 170, 1000)],
    }
    pair = active_pair(observations[DAY], DAY)
    assert (pair["near_contract"], pair["far_contract"]) == ("rb2603", "rb2605")
    trades, gaps = replay(observations, DAY, DAY + 2 * WEEK)
    assert not gaps and trades == [(DAY, -5, -5, 1)]
    observations[DAY + 2 * WEEK].pop(0)
    trades, gaps = replay(observations, DAY, DAY + 2 * WEEK)
    assert not trades and gaps == {"missing_fixed_leg": 1}
