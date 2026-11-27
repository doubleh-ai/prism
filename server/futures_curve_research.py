"""Fixed-contract term structure for physical commodities. Sign is far minus near."""

from datetime import date
from decimal import Decimal


def active_pair(contracts: list[dict], observed: date) -> dict | None:
    """Choose by volume known on the observation date, then order by maturity.

    Exclude delivery this month and next so both legs still exist across the
    pilot's two-week observation/entry/exit window. Never substitute a new leg.
    """
    eligible = []
    for row in contracts:
        year, month = map(int, row["delivery_month"].split("-"))
        months_away = (year - observed.year) * 12 + month - observed.month
        if months_away >= 2 and all((row.get(k) or 0) > 0 for k in ("settlement", "volume", "open_interest")):
            eligible.append(row)
    if len(eligible) < 2:
        return None
    eligible.sort(key=lambda r: (-r["volume"], -r["open_interest"], r["contract"]))
    near, far = sorted(eligible[:2], key=lambda r: r["delivery_month"])
    return {
        "near_contract": near["contract"],
        "far_contract": far["contract"],
        "near_settlement": near["settlement"],
        "far_settlement": far["settlement"],
        "far_minus_near": float(Decimal(str(far["settlement"])) - Decimal(str(near["settlement"]))),
    }
