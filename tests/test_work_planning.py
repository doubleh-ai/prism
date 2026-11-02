"""Known scheduling scenarios: calendars, resources, float and truthful gaps."""

import pytest

from server.productivity.planning import PlanError, calculate

CONFIG = {
    "start_at": "2026-11-02T01:00:00Z",
    "timezone": "Asia/Shanghai",
    "work_start": 540,
    "work_end": 1080,
    "weekdays": [0, 1, 2, 3, 4],
    "holidays": [],
}


def task(key, duration=60, **changes):
    return {
        "id": key,
        "title": key,
        "status": "todo",
        "priority": "normal",
        "due_at": None,
        "duration_minutes": duration,
        "duration_source": "user",
        "assignee": "person",
        "not_before": None,
        "milestone": False,
        "assignee_unavailable": False,
        **changes,
    }


def link(a, b, lag=0):
    return {"predecessor_id": a, "successor_id": b, "lag_minutes": lag}


def rows(result):
    return {r["id"]: r for r in result["tasks"]}


def test_diamond_longest_path_resource_separation_and_float():
    result = calculate(
        CONFIG,
        [
            task("a", 60, assignee="a"),
            task("b", 120, assignee="b"),
            task("c", 60, assignee="c"),
            task("d", 30, assignee="d"),
        ],
        [link("a", "b"), link("a", "c"), link("b", "d"), link("c", "d")],
    )
    data = rows(result)
    assert result["project_finish_at"] == "2026-11-02T04:30:00.000000Z"
    assert data["c"]["slack_minutes"] == 60 and not data["c"]["critical"]
    assert set(result["critical_task_ids"]) == {"a", "b", "d"}
    assert data["d"]["start_at"] == "2026-11-02T04:00:00.000000Z"


def test_same_assignee_never_overlaps_and_resource_edges_affect_float():
    result = calculate(CONFIG, [task("first", 60), task("second", 60)], [])
    data = rows(result)
    assert data["first"]["finish_at"] <= data["second"]["start_at"]
    assert result["project_finish_at"] == "2026-11-02T03:00:00.000000Z"
    assert all(t["critical"] for t in data.values())


def test_weekend_holiday_and_boundary_start_finish_are_distinct():
    config = {**CONFIG, "start_at": "2026-11-06T09:00:00Z", "holidays": ["2026-11-09"]}
    result = calculate(config, [task("a", 120), task("b", 60)], [link("a", "b")])
    data = rows(result)
    assert data["a"]["start_at"] == "2026-11-06T09:00:00.000000Z"
    assert data["a"]["finish_at"] == "2026-11-10T02:00:00.000000Z"
    assert data["b"]["finish_at"] == "2026-11-10T03:00:00.000000Z"
    full = calculate(CONFIG, [task("a", 540), task("b", 60)], [link("a", "b")])
    data = rows(full)
    assert data["a"]["finish_at"] == "2026-11-02T10:00:00.000000Z"
    assert data["b"]["start_at"] == "2026-11-03T01:00:00.000000Z"


def test_calendar_tracks_dst_offset_without_using_server_timezone():
    config = {**CONFIG, "timezone": "America/New_York", "start_at": "2026-10-30T21:00:00Z"}
    result = calculate(config, [task("a", 120)], [])
    assert rows(result)["a"]["finish_at"] == "2026-11-02T15:00:00.000000Z"


def test_lag_and_not_before_advance_in_working_minutes():
    result = calculate(
        CONFIG, [task("a", 60), task("b", 60, not_before="2026-11-03T02:00:00Z")], [link("a", "b", 60)]
    )
    assert rows(result)["b"]["start_at"] == "2026-11-03T02:00:00.000000Z"
    assert rows(result)["b"]["finish_at"] == "2026-11-03T03:00:00.000000Z"


def test_cancelled_predecessor_blocks_descendants_but_not_unrelated_work():
    result = calculate(
        CONFIG, [task("a", status="cancelled"), task("b"), task("c", assignee="other")], [link("a", "b")]
    )
    assert result["status"] == "blocked" and result["project_finish_at"] is None
    assert rows(result)["b"]["blocked_by"] == ["a"] and rows(result)["b"]["start_at"] is None
    assert rows(result)["c"]["start_at"] is not None
    only = calculate(CONFIG, [task("cancelled", status="cancelled")], [])
    assert only["counts"]["blocked"] == 0 and only["counts"]["cancelled"] == 1


def test_unavailable_assignee_is_not_silently_treated_as_unlimited_capacity():
    result = calculate(CONFIG, [task("a", assignee_unavailable=True)], [])
    assert rows(result)["a"]["blocked_reason"] == "assignee_unavailable"
    assert result["project_finish_at"] is None


def test_default_estimates_unassigned_work_and_done_dates_are_not_claimed_actual():
    result = calculate(
        CONFIG, [task("a", duration_source="default", assignee=None), task("done", status="done")], []
    )
    assert result["status"] == "needs_estimates" and result["counts"]["default_estimate"] == 1
    assert result["counts"]["unassigned"] == 1 and result["assumptions"]["observed_actual_dates"] is False
    assert rows(result)["done"]["remaining_minutes"] == 0


def test_milestone_finishes_at_predecessor_boundary_and_honors_weekend_not_before():
    result = calculate(CONFIG, [task("a", 540), task("release", 0, milestone=True)], [link("a", "release")])
    assert rows(result)["release"]["finish_at"] == rows(result)["a"]["finish_at"]
    config = {**CONFIG, "start_at": "2026-11-06T01:00:00Z"}
    result = calculate(
        config,
        [task("a", 540), task("release", 0, milestone=True, not_before="2026-11-08T01:00:00Z")],
        [link("a", "release")],
    )
    assert rows(result)["release"]["finish_at"] == "2026-11-08T01:00:00.000000Z"


def test_deadline_breach_reports_negative_float_without_changing_deadline():
    due = "2026-11-02T01:30:00Z"
    result = calculate(CONFIG, [task("a", 60, due_at=due)], [])
    assert rows(result)["a"]["late"] and rows(result)["a"]["slack_minutes"] == -30
    assert rows(result)["a"]["due_at"] == due and result["counts"]["late"] == 1


@pytest.mark.parametrize(
    "edges", [[link("a", "b"), link("b", "a")], [link("a", "a")], [link("a", "missing")]]
)
def test_invalid_graph_refuses_instead_of_truncating_or_dropping_edges(edges):
    with pytest.raises(PlanError):
        calculate(CONFIG, [task("a"), task("b")], edges)
