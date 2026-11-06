"""Calendar exports advance for each real change without advancing on replays."""

from uuid import uuid4

import pytest

from server.database import get_db
from server.productivity import calendar, store


@pytest.fixture
async def series(client, auth_cookies, monkeypatch):
    monkeypatch.setenv("PRISM_WORK_ENABLED", "1")
    store.init_productivity_db()
    calendar.init_calendar_db()
    body = {
        "request_id": str(uuid4()),
        "title": "合成重复评审",
        "start_at": "2026-11-02T09:00:00+08:00",
        "end_at": "2026-11-02T10:00:00+08:00",
        "timezone": "Asia/Shanghai",
        "recurrence": {"frequency": "daily", "interval": 1, "count": 3},
    }
    created = await client.post("/api/work/calendar", cookies=auth_cookies, json=body)
    assert created.status_code == 200
    event = created.json()["event"]
    response = await client.get(
        "/api/work/calendar?start=2026-11-02T00:00:00Z&end=2026-11-09T00:00:00Z", cookies=auth_cookies
    )
    rows = [r for r in response.json()["occurrences"] if r["series_id"] == event["id"]]
    assert len(rows) == 3
    return client, auth_cookies, body, event, rows


async def exported_sequence(data):
    client, cookies, _, event, _ = data
    response = await client.get(f"/api/work/calendar/{event['id']}.ics", cookies=cookies)
    assert response.status_code == 200
    return int(
        next(line.split(":")[1] for line in response.text.splitlines() if line.startswith("SEQUENCE:"))
    )


async def edit_occurrence(data, row, **fields):
    client, cookies, _, event, _ = data
    return await client.put(
        f"/api/work/calendar/{event['id']}/occurrences/{row['occurrence_id']}",
        cookies=cookies,
        json={"request_id": str(uuid4()), "expected_version": row["version"], **fields},
    )


async def test_different_occurrence_edits_advance_sequence_and_replay_does_not(series):
    client, cookies, _, event, rows = series
    revisions = [await exported_sequence(series)]
    for row in rows[:2]:
        path = f"/api/work/calendar/{event['id']}/occurrences/{row['occurrence_id']}"
        body = {"request_id": str(uuid4()), "expected_version": row["version"], "cancelled": True}
        assert (await client.put(path, cookies=cookies, json=body)).status_code == 200
        revisions.append(await exported_sequence(series))
        replay = await client.put(path, cookies=cookies, json=body)
        assert replay.status_code == 200 and replay.json()["replayed"]
        assert await exported_sequence(series) == revisions[-1]
        stale = await client.put(path, cookies=cookies, json={**body, "request_id": str(uuid4())})
        assert stale.status_code == 409 and await exported_sequence(series) == revisions[-1]
    assert revisions == [1, 2, 3]


async def test_series_rebuild_and_exception_reset_never_lower_sequence(series):
    client, cookies, body, event, rows = series
    row = rows[0]
    for version in range(1, 5):
        response = await edit_occurrence(series, {**row, "version": version}, cancelled=bool(version % 2))
        assert response.status_code == 200
    before = await exported_sequence(series)
    changed = {
        **body,
        "request_id": str(uuid4()),
        "expected_version": event["version"],
        "recurrence": {"frequency": "daily", "interval": 1, "count": 1},
        "reset_exceptions": True,
    }
    response = await client.put(f"/api/work/calendar/{event['id']}", cookies=cookies, json=changed)
    assert response.status_code == 200
    assert await exported_sequence(series) == before + 1


async def test_legacy_revision_is_seeded_once_and_compatible_sql_writers_advance_it(series):
    _, _, _, event, rows = series
    with get_db() as db:
        for name in (
            "calendar_export_created",
            "calendar_export_updated",
            "calendar_export_occurrence_updated",
            "calendar_export_deleted",
        ):
            db.execute("DROP TRIGGER " + name)
        db.execute("DROP TABLE work_calendar_export_versions")
        db.execute("UPDATE work_calendar SET version=3 WHERE id=?", [event["id"]])
        db.execute("UPDATE work_calendar_instances SET version=5 WHERE id=?", [rows[0]["occurrence_id"]])
    calendar.init_calendar_db()
    assert await exported_sequence(series) == 7
    calendar.init_calendar_db()
    assert await exported_sequence(series) == 7
    # An older compatible writer has no knowledge of the new table. Triggers
    # still update the export revision in that writer's transaction.
    with get_db() as db:
        db.execute(
            "UPDATE work_calendar_instances SET version=version+1,cancelled=1 WHERE id=?",
            [rows[1]["occurrence_id"]],
        )
    assert await exported_sequence(series) == 8
    with get_db() as db:
        db.execute("DELETE FROM users WHERE username='testuser'")
        assert db.execute("SELECT COUNT(*) FROM work_calendar_export_versions").fetchone()[0] == 0
