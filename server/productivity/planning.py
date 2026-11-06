"""Bounded finish-to-start scheduling in a declared working calendar.

This computes a remaining-work scenario, not observed progress or a promise.
Unassigned work has no resource limit; missing estimates are explicitly marked.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from heapq import heappop, heappush
from math import ceil
from zoneinfo import ZoneInfo


class PlanError(ValueError):
    pass


def stamp(seconds):
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def seconds(value):
    return datetime.fromisoformat(value).timestamp()


class WorkingCalendar:
    def __init__(self, config):
        start = ceil(seconds(config["start_at"]) / 60) * 60
        z = ZoneInfo(config["timezone"])
        first = datetime.fromtimestamp(start, z).date()
        holidays = set(config["holidays"])
        self.segments = []
        self.ends = []
        self.totals = []
        total = 0
        self.skipped_boundaries = 0
        for offset in range(3661):
            try:
                day = first + timedelta(days=offset)
            except OverflowError:
                break
            if day.weekday() not in config["weekdays"] or day.isoformat() in holidays:
                continue
            midnight = datetime.combine(day, datetime.min.time())
            a = midnight + timedelta(minutes=config["work_start"])
            b = midnight + timedelta(minutes=config["work_end"])
            left = a.replace(tzinfo=z, fold=0).astimezone(UTC)
            right = b.replace(tzinfo=z, fold=1).astimezone(UTC)
            if left.astimezone(z).replace(tzinfo=None) != a or right.astimezone(z).replace(tzinfo=None) != b:
                self.skipped_boundaries += 1
                continue
            lo = max(start, ceil(left.timestamp() / 60) * 60)
            hi = int(right.timestamp() // 60) * 60
            if hi <= lo:
                continue
            length = (hi - lo) // 60
            self.segments.append((lo, hi, total))
            total += length
            self.ends.append(hi)
            self.totals.append(total)
        self.capacity = total
        if not self.segments:
            raise PlanError("working_calendar_has_no_capacity")

    def offset(self, instant):
        value = seconds(instant)
        i = bisect_right(self.ends, value)
        if i == len(self.ends):
            if value == self.ends[-1]:
                return self.capacity
            raise PlanError("outside_planning_horizon")
        lo, _, prior = self.segments[i]
        return prior + max(0, ceil((value - lo) / 60))

    def at(self, minute, *, finish=False):
        if not 0 <= minute <= self.capacity:
            raise PlanError("outside_planning_horizon")
        i = bisect_left(self.totals, minute) if finish and minute else bisect_right(self.totals, minute)
        if i == len(self.segments):
            return self.ends[-1]
        lo, _, prior = self.segments[i]
        return lo + (minute - prior) * 60


def graph(ids, links):
    successors = {key: [] for key in ids}
    predecessors = {key: [] for key in ids}
    for edge in links:
        a, b, lag = edge["predecessor_id"], edge["successor_id"], edge.get("lag_minutes", 0)
        if a not in successors or b not in successors or a == b:
            raise PlanError("dependency_target_invalid")
        successors[a].append((b, lag))
        predecessors[b].append((a, lag))
    pending = {key: len(predecessors[key]) for key in ids}
    ready = []
    order = []
    for key, count in pending.items():
        if not count:
            heappush(ready, key)
    while ready:
        key = heappop(ready)
        order.append(key)
        for child, _ in successors[key]:
            pending[child] -= 1
            if not pending[child]:
                heappush(ready, child)
    if len(order) != len(ids):
        raise PlanError("dependency_cycle")
    return order, successors, predecessors


def calculate(config, tasks, links):
    if len(tasks) > 1000 or len(links) > 4000:
        raise PlanError("planning_size_limit")
    calendar = WorkingCalendar(config)
    by_id = {t["id"]: t for t in tasks}
    if len(by_id) != len(tasks):
        raise PlanError("duplicate_task")
    order, successors, pred = graph(by_id, links)
    duration = {
        key: 0 if t["status"] in {"done", "cancelled"} or t["milestone"] else t["duration_minutes"]
        for key, t in by_id.items()
    }
    remaining = {}
    for key in reversed(order):
        remaining[key] = duration[key] + max((lag + remaining[c] for c, lag in successors[key]), default=0)
    priority = {"high": 0, "normal": 1, "low": 2}
    position = {t["id"]: i for i, t in enumerate(tasks)}
    pending = {key: len(pred[key]) for key in by_id}
    ready = []

    def push(key):
        heappush(ready, (priority.get(by_id[key]["priority"], 1), -remaining[key], position[key], key))

    for key, count in pending.items():
        if not count:
            push(key)
    starts = {}
    finishes = {}
    actual_start = {}
    actual_finish = {}
    blocked = {}
    reasons = {}
    resources = {}
    while ready:
        *_, key = heappop(ready)
        task = by_id[key]
        blocked_by = [parent for parent, _ in pred[key] if parent in blocked]
        reason = "dependency_blocked" if blocked_by else None
        if task["status"] == "cancelled":
            blocked_by = [key]
            reason = "task_cancelled"
        if task.get("assignee_unavailable") and task["status"] != "done":
            blocked_by = [key]
            reason = "assignee_unavailable"
        if blocked_by:
            blocked[key] = blocked_by
            reasons[key] = reason
        else:
            try:
                start = max((finishes[p] + lag for p, lag in pred[key]), default=0)
                if task["not_before"]:
                    start = max(start, calendar.offset(task["not_before"]))
                person = task["assignee"]
                if duration[key] and person:
                    reserved = resources.setdefault(person, [])
                    for left, right, _ in sorted(reserved):
                        if start + duration[key] <= left:
                            break
                        if start < right:
                            start = right
                end = start + duration[key]
                if end > calendar.capacity:
                    raise PlanError("outside_planning_horizon")
                starts[key], finishes[key] = start, end
                if not duration[key]:
                    instant = max(
                        [
                            seconds(config["start_at"]),
                            *(actual_finish[p] for p, _ in pred[key]),
                            *([seconds(task["not_before"])] if task["not_before"] else []),
                        ]
                    )
                    instant = max(instant, calendar.at(start, finish=True))
                    actual_start[key] = actual_finish[key] = instant
                else:
                    actual_start[key] = calendar.at(start)
                    actual_finish[key] = calendar.at(end, finish=True)
                    if person:
                        resources[person].append((start, end, key))
            except PlanError:
                blocked[key] = [key]
                reasons[key] = "outside_planning_horizon"
        for child, _ in successors[key]:
            pending[child] -= 1
            if not pending[child]:
                push(child)
    # Resource ordering is part of this computed scenario, not a user dependency.
    augmented = {
        (e["predecessor_id"], e["successor_id"]): e.get("lag_minutes", 0)
        for e in links
        if e["predecessor_id"] not in blocked and e["successor_id"] not in blocked
    }
    for intervals in resources.values():
        ordered = sorted(intervals)
        for a, b in zip(ordered, ordered[1:], strict=False):
            augmented[(a[2], b[2])] = max(0, augmented.get((a[2], b[2]), 0))
    active = {key for key in by_id if key not in blocked}
    latest = {}
    slack = {}
    sorted_active, succ, _ = graph(
        active,
        [{"predecessor_id": a, "successor_id": b, "lag_minutes": lag} for (a, b), lag in augmented.items()],
    )
    finish = max(finishes.values(), default=0)
    for key in reversed(sorted_active):
        last = min((latest[c] - lag for c, lag in succ[key]), default=finish)
        if by_id[key]["due_at"] and by_id[key]["status"] != "done":
            with suppress(PlanError):
                last = min(last, calendar.offset(by_id[key]["due_at"]))
        latest[key] = last - duration[key]
        slack[key] = latest[key] - starts[key]
    rows = []
    for task in tasks:
        key = task["id"]
        lo = actual_start.get(key)
        hi = actual_finish.get(key)
        due = seconds(task["due_at"]) if task["due_at"] else None
        late = bool(hi is not None and due is not None and hi > due and task["status"] != "done")
        rows.append(
            {
                **task,
                "start_at": stamp(lo) if lo is not None else None,
                "finish_at": stamp(hi) if hi is not None else None,
                "remaining_minutes": duration[key],
                "slack_minutes": slack.get(key),
                "critical": key in slack and slack[key] <= 0 and task["status"] != "done",
                "blocked_by": blocked.get(key, []),
                "blocked_reason": reasons.get(key),
                "late": late,
            }
        )
    defaults = sum(
        t["duration_source"] == "default" and t["status"] not in {"done", "cancelled"} for t in tasks
    )
    unassigned = sum(t["assignee"] is None and t["status"] not in {"done", "cancelled"} for t in tasks)
    blocking = [key for key in blocked if by_id[key]["status"] != "cancelled"]
    return {
        "status": "blocked" if blocking else "needs_estimates" if defaults or unassigned else "calculated",
        "tasks": rows,
        "critical_task_ids": [r["id"] for r in rows if r["critical"]],
        "project_finish_at": None
        if blocking
        else stamp(max(actual_finish.values(), default=seconds(config["start_at"]))),
        "scheduled_finish_at": stamp(max(actual_finish.values())) if actual_finish else None,
        "counts": {
            "blocked": len(blocking),
            "cancelled": sum(t["status"] == "cancelled" for t in tasks),
            "late": sum(r["late"] for r in rows),
            "default_estimate": defaults,
            "unassigned": unassigned,
        },
        "assumptions": {
            "remaining_work_only": True,
            "same_person_sequential": True,
            "unassigned_parallel": True,
            "default_minutes": 60,
            "working_calendar_user_supplied": True,
            "nonexistent_boundaries_skipped": calendar.skipped_boundaries,
            "horizon_days": 3660,
            "observed_actual_dates": False,
        },
    }
