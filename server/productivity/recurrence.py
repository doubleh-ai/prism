"""Finite recurrence in the event's IANA zone, not the server's time zone.

Daily/weekly/monthly is the supported RFC 5545 subset. Invalid month dates and
DST gaps are skipped and do not consume COUNT. A repeated local time uses the
explicit fold selected by DTSTART. No unbounded rule or implicit UTC wall time.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Literal
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from .models import ClosedModel

MAX_OCCURRENCES = 366
MAX_DAYS = 3660


class Recurrence(ClosedModel):
    frequency: Literal["daily", "weekly", "monthly"]
    interval: int = Field(default=1, ge=1, le=12)
    weekdays: list[int] = Field(default_factory=list, max_length=7)
    until: str | None = Field(default=None, max_length=10)
    count: int | None = Field(default=None, ge=1, le=MAX_OCCURRENCES)

    @model_validator(mode="after")
    def finite_rule(self):
        if (self.count is None) == (self.until is None):
            raise ValueError("choose exactly one recurrence end")
        if self.until is not None and date.fromisoformat(self.until).isoformat() != self.until:
            raise ValueError("canonical recurrence end date required")
        if any(type(day) is not int or not 0 <= day <= 6 for day in self.weekdays):
            raise ValueError("invalid weekday")
        if len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError("duplicate weekday")
        if self.frequency != "weekly" and self.weekdays:
            raise ValueError("weekdays only apply to weekly rules")
        self.weekdays.sort()
        return self


def _stamp(value):
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def expand(series_id, start_at, end_at, timezone, rule: Recurrence | None):
    """Materialize at most 366 stable occurrences; fail instead of truncating."""
    start, end = datetime.fromisoformat(start_at), datetime.fromisoformat(end_at)
    zone = ZoneInfo(timezone)
    local = start.astimezone(zone)
    duration = end - start
    if rule is None:
        return [{"id": series_id, "start_at": _stamp(start), "end_at": _stamp(end)}]
    last = date.fromisoformat(rule.until) if rule.until else None
    if last is not None and not 0 <= (last - local.date()).days <= MAX_DAYS:
        raise ValueError("recurrence end must be within ten years")
    weekdays = rule.weekdays or [local.weekday()]
    if rule.frequency == "weekly" and local.weekday() not in weekdays:
        raise ValueError("DTSTART must match the selected weekdays")
    first_week = local.date() - timedelta(days=local.weekday())
    result = []
    for offset in range(MAX_DAYS + 1):
        day = local.date() + timedelta(days=offset)
        if last is not None and day > last:
            break
        months = (day.year - local.year) * 12 + day.month - local.month
        selected = (
            (rule.frequency == "daily" and offset % rule.interval == 0)
            or (
                rule.frequency == "weekly"
                and (day - first_week).days // 7 % rule.interval == 0
                and day.weekday() in weekdays
            )
            or (rule.frequency == "monthly" and months % rule.interval == 0 and day.day == local.day)
        )
        if not selected:
            continue
        wall = datetime.combine(day, local.timetz().replace(tzinfo=None))
        candidate = wall.replace(tzinfo=zone, fold=local.fold)
        instant = candidate.astimezone(UTC)
        # ZoneInfo permits constructing imaginary times. Prove round-trip.
        if instant.astimezone(zone).replace(tzinfo=None) != wall:
            continue
        result.append(
            {
                "id": str(uuid5(NAMESPACE_URL, f"prism-calendar:{series_id}:{day.isoformat()}")),
                "start_at": _stamp(instant),
                "end_at": _stamp(instant + duration),
            }
        )
        if rule.count is not None and len(result) == rule.count:
            return result
        if len(result) > MAX_OCCURRENCES:
            raise ValueError("recurrence exceeds 366 occurrences")
    if not result or (rule.count is not None and len(result) != rule.count):
        raise ValueError("recurrence cannot be fully expanded within ten years")
    return result
