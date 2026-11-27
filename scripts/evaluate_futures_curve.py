"""Replay the RB curve hypothesis on locally archived, publicly dated SHFE Friday bulletins.

Invoke from repository root with python -m scripts.evaluate_futures_curve REPORT_DIR.
Use ISO dates as filenames (YYYY-MM-DD.dat). No model, orders or network calls.
"""

import argparse
import json
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from statistics import mean, median

from server.exchange_futures import parse_contracts
from server.futures_curve_research import active_pair
from server.futures_fundamentals import _HTML

WEEK = timedelta(days=7)


def leg_change(rows, near, far):
    settlements = {
        r["contract"]: r["settlement"] for r in rows if (r["settlement"] or 0) > 0 and r["volume"] > 0
    }
    if near in settlements and far in settlements:
        return settlements[near] - settlements[far]
    return None


def replay(reports, start, end):
    trades, gaps = [], Counter()
    # The signal is known after Friday publication. Enter the following Friday
    # and measure the next week using the same legs throughout, including rolls.
    day = start
    while day + 2 * WEEK <= end:
        entry, exit_day = day + WEEK, day + 2 * WEEK
        if any(d not in reports for d in (day, entry, exit_day)):
            gaps["missing_week"] += 1
        elif not (pair := active_pair(reports[day], day)):
            gaps["no_pair"] += 1
        elif pair["far_minus_near"] == 0:
            gaps["flat_curve"] += 1
        else:
            args = pair["near_contract"], pair["far_contract"]
            before, after = (leg_change(reports[d], *args) for d in (entry, exit_day))
            if before is None or after is None:
                gaps["missing_fixed_leg"] += 1
            else:
                long_near = after - before
                sign = 1 if pair["far_minus_near"] < 0 else -1
                trades.append((day, sign * long_near, long_near, sign))
        day += WEEK
    return trades, gaps


def summarize(trades):
    if not trades:
        return {"n": 0}
    pnl = [r[1] for r in trades]
    return {
        "n": len(pnl),
        "gross_mean_yuan_per_tonne": round(mean(pnl), 2),
        "median": median(pnl),
        "worst_week": min(pnl),
        "positive_weeks": sum(n > 0 for n in pnl),
        "long_near_weeks": sum(t[3] > 0 for t in trades),
        "always_long_near_mean": round(mean(t[2] for t in trades), 2),
        "mean_after_2_yuan_total_cost": round(mean(pnl) - 2, 2),
    }


def procurement(raw):
    doc = _HTML()
    doc.feed(raw.decode("utf-8"))
    doc.close()
    if "沪市终端线螺每周采购量监控" not in "".join(doc.text):
        raise ValueError("unexpected physical market series")
    matches = [t for t in doc.tables if t and t[0] == ["日期", "值", "涨跌额", "涨跌幅"]]
    if len(matches) != 1:
        raise ValueError("missing weekly procurement table")
    observations = {}
    for row in matches[0][1:]:
        day = date.fromisoformat(row[0])
        value = float(row[1])
        if day in observations or not 0 <= value < float("inf"):
            raise ValueError("invalid procurement reading")
        observations[day] = value
    return observations


def replay_purchases(purchases, contracts, split):
    trades, gaps = [], Counter()
    for day, volume in sorted(purchases.items()):
        if day.weekday() != 4 or day - WEEK not in purchases:
            gaps["no_adjacent_fridays"] += 1
            continue
        if day + 2 * WEEK > max(purchases):
            gaps["beyond_study_end"] += 1
            continue
        if not all(d in contracts for d in (day, day + WEEK, day + 2 * WEEK)):
            gaps["missing_report"] += 1
            continue
        change = volume - purchases[day - WEEK]
        if change == 0:
            gaps["flat_observation"] += 1
            continue
        pair = active_pair(contracts[day], day)
        if not pair:
            gaps["no_fixed_pair"] += 1
            continue
        legs = pair["near_contract"], pair["far_contract"]
        entry, exit_value = (leg_change(contracts[d], *legs) for d in (day + WEEK, day + 2 * WEEK))
        if entry is None or exit_value is None:
            gaps["missing_fixed_leg"] += 1
            continue
        sign = 1 if change > 0 else -1
        buy_near = exit_value - entry
        trades.append((day, sign * buy_near, buy_near, sign))
    return {
        "all": summarize(trades),
        "early": summarize([r for r in trades if r[0] + 2 * WEEK < split]),
        "later": summarize([r for r in trades if r[0] >= split]),
        "gaps": dict(gaps),
        "cross_split": sum(r[0] < split <= r[0] + 2 * WEEK for r in trades),
    }


def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("directory", type=Path)
    args.add_argument("--purchases-html", type=Path, help="Xiben weekly Shanghai purchases table (tonnes)")
    args.add_argument("--start", type=date.fromisoformat, default=date(2024, 11, 22))
    args.add_argument("--split", type=date.fromisoformat, default=date(2025, 11, 21))
    args.add_argument("--end", type=date.fromisoformat, default=date(2026, 11, 20))
    opts = args.parse_args()
    if opts.start.weekday() != 4 or not opts.start < opts.split < opts.end:
        args.error("start must be Friday and start < split < end")
    reports = {}
    for p in opts.directory.glob("????-??-??.dat"):
        day = date.fromisoformat(p.stem)
        if opts.start <= day <= opts.end and day.weekday() == 4:
            reports[day] = parse_contracts(p.read_bytes(), "RB", day)["contracts"]
    if opts.purchases_html:
        data = {
            d: v
            for d, v in procurement(opts.purchases_html.read_bytes()).items()
            if opts.start <= d <= opts.end
        }
        if not data:
            args.error("no procurement observations in the selected interval")
        print(json.dumps(replay_purchases(data, reports, opts.split), indent=2))
        return
    trades, gaps = replay(reports, opts.start, opts.end)
    # Windows crossing the split belong to neither subperiod.
    print(
        json.dumps(
            {
                "reports": len(reports),
                "gaps": gaps,
                "all": summarize(trades),
                "early": summarize([r for r in trades if r[0] + 2 * WEEK < opts.split]),
                "later": summarize([r for r in trades if r[0] >= opts.split]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
