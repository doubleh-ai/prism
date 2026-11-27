"""Scheduled collection and point-in-time storage of public institutional reports."""

import asyncio
import hashlib
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import urldefrag

from .database import get_db
from .work_lifecycle import LifecycleError

PUBLISHERS = {"ruida": "瑞达期货", "east_asia": "上海东亚期货"}
INTERVAL = timedelta(hours=6)
RETRY = timedelta(minutes=30)
_log = logging.getLogger(__name__)


def _now():
    return datetime.now(UTC)


def _stamp(value):
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _claim(publisher, now):
    cutoff, following = _stamp(now), _stamp(now + RETRY)
    with get_db() as db:
        claimed = db.execute(
            """INSERT INTO futures_institutional_refresh(publisher,not_before) VALUES(?,?)
               ON CONFLICT(publisher) DO UPDATE SET not_before=excluded.not_before
               WHERE futures_institutional_refresh.not_before<=?""",
            (publisher, following, cutoff),
        )
    return following if claimed.rowcount == 1 else None


def _row(publisher, source, captured):
    from .futures_institutional_views import COVERED_SYMBOLS, REVIEWED_SECTIONS, VIEWS
    from .futures_opinion_extraction import extract, report_from_source

    report = report_from_source(source)
    if (
        report is None
        or report.institution != PUBLISHERS[publisher]
        or source["symbol"] not in COVERED_SYMBOLS
        or (
            source["excerpt"] is not None
            and not (isinstance(source["excerpt"], str) and 0 < len(source["excerpt"].strip()) <= 4000)
        )
        or not source["locator"]
    ):
        raise ValueError("invalid public report identity")
    available = report.visible_at()
    if available > captured:
        return None

    source_url = urldefrag(report.url).url
    digest = hashlib.sha256(source["excerpt"].encode()).hexdigest() if source["excerpt"] is not None else ""
    anchor = REVIEWED_SECTIONS.get(digest)
    seed = next(
        (
            v
            for v in VIEWS.get(source["symbol"], ())
            if urldefrag(v.report.url).url == source_url
            and v.report.institution == report.institution
            and v.report.visible_at() == available
        ),
        None,
    )
    if seed and source.get("download_unavailable"):
        return None  # A transport failure is not evidence of a revision of this already reviewed PDF.
    if seed and anchor and anchor in source["excerpt"]:
        view = replace(seed, anchor=anchor, locator=source["locator"])
    else:
        view = extract(source)
    item = {k: source[k] for k in ("institution", "title", "url", "published_on", "locator")}
    if view is not None:
        item.update(
            stance=view.stance,
            horizon=view.horizon,
            summary=view.summary,
            condition=view.condition,
            anchor=view.anchor,
            locator=view.locator,
        )
    return (
        publisher,
        source["symbol"],
        source_url,
        _stamp(available),
        _stamp(captured),
        digest,
        json.dumps(item, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
    )


def _store(publisher, claim, captured, rows):
    # Publish a whole fetched edition atomically, including any explicit loss of a directional view.
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if db.execute(
                "SELECT 1 FROM futures_institutional_refresh WHERE publisher=? AND not_before=?",
                (publisher, claim),
            ).fetchone():
                for row, download_unavailable in rows:
                    previous = db.execute(
                        """SELECT available_at,excerpt_sha256,edition_json FROM futures_institutional_editions
                           WHERE publisher=? AND symbol=? AND source_url=? ORDER BY captured_at DESC,id DESC LIMIT 1""",
                        row[:3],
                    ).fetchone()
                    if download_unavailable and previous is not None and previous[0] == row[3]:
                        continue
                    if previous is None or tuple(previous) != (row[3], row[5], row[6]):
                        db.execute(
                            """INSERT INTO futures_institutional_editions
                               (publisher,symbol,source_url,available_at,captured_at,excerpt_sha256,edition_json)
                               VALUES(?,?,?,?,?,?,?)""",
                            row,
                        )
                db.execute(
                    "UPDATE futures_institutional_refresh SET not_before=? WHERE publisher=?",
                    (_stamp(captured + INTERVAL), publisher),
                )
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise


async def _refresh_one(publisher):
    from .futures_institutional_sources import collect

    now = _now()
    claim = _claim(publisher, now)
    if claim is None:
        return
    try:
        async with asyncio.timeout(120):
            batch = await collect(publisher, now)
        captured = _now()
        rows = []
        for source in batch:
            row = _row(publisher, source, captured)
            if row is not None:
                rows.append((row, source.get("download_unavailable", False)))
        _store(publisher, claim, captured, rows)
    except Exception as exc:
        # A failed or interrupted collection keeps its short claim and all earlier evidence.
        _log.warning("Futures institutional collection failed for %s (%s)", publisher, type(exc).__name__)


async def refresh_due(manager):
    # Draining this manager must fence both outbound collection and the SQLite commit.
    try:
        lease = manager.admit("market", persist_required=False)
    except LifecycleError:
        return
    with lease:
        async with asyncio.TaskGroup() as group:
            for publisher in PUBLISHERS:
                group.create_task(_refresh_one(publisher))


def latest(symbol, cutoff):
    """Select both the public edition and the revision actually captured by the decision time."""
    stamp = _stamp(cutoff)
    result = []
    with get_db() as db:
        for publisher in PUBLISHERS:
            row = db.execute(
                """SELECT available_at,captured_at,edition_json FROM futures_institutional_editions
                   WHERE symbol=? AND publisher=? AND available_at<=? AND captured_at<=?
                   AND NOT EXISTS (
                       SELECT 1 FROM futures_institutional_editions AS revision
                       WHERE revision.symbol=futures_institutional_editions.symbol
                       AND revision.publisher=futures_institutional_editions.publisher
                       AND revision.source_url=futures_institutional_editions.source_url
                       AND revision.captured_at<=? AND (revision.captured_at,revision.id)>
                           (futures_institutional_editions.captured_at,futures_institutional_editions.id)
                   )
                   ORDER BY available_at DESC,captured_at DESC,id DESC LIMIT 1""",
                (symbol, publisher, stamp, stamp, stamp),
            ).fetchone()
            if row is not None:
                result.append(
                    (datetime.fromisoformat(row[0]), datetime.fromisoformat(row[1]), json.loads(row[2]))
                )
    return result
