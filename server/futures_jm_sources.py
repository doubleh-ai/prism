"""Dated JM publications. Survey editions and contract closes keep their own publication time."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from datetime import date, datetime, timedelta
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

from . import public_network
from .database import get_db
from .futures_weekly_releases import Release

SHANGHAI = ZoneInfo("Asia/Shanghai")
STEEL = "https://www.steelhome.com"
DAILY = STEEL + "/2=11=002,185%7C001,185===/1.html"
COAL = "https://coal.mysteel.com/article/pa5157a050202aaaaa1.html"
COKE = "https://jiaotan.mysteel.com/article/pa5417aaaaaa1.html"
SURVEYS = {
    "mine": "全国523家炼焦煤矿山生产情况调查统计",
    "port": "焦煤、焦炭港口库存调查统计",
    "coke": "独立焦企生产情况调查统计",
}


def _report_url(nid):
    return f"{STEEL}/detailspage.php?nid={nid}&ntype=n"


def _market_edition(day, stamp, nid, near, far, near_change, far_change):
    return Release(
        day,
        f"{day}T{stamp}",
        _report_url(nid),
        {
            "JM2701": near,
            "JM2705": far,
            "JM2701_change": near_change,
            "JM2705_change": far_change,
        },
    )


# Verified editions allow a reproducible historical replay independent of an upstream outage.
ARCHIVE = (
    _market_edition("2026-11-13", "16:14:06", 4793598, 1439, 1326.5, 20513, -810),
    _market_edition("2026-11-19", "16:14:08", 4796924, 1375.5, 1257, -21058, 5829),
    _market_edition("2026-11-20", "16:14:05", 4798074, 1397, 1267, -16672, 1369),
    Release(
        "2026-11-12",
        "2026-11-12T09:17",
        "https://coal.mysteel.com/a/26111209/2B4D1E3A46E5C0E2.html",
        {"output": 66.1, "mine": 249.5},
    ),
    Release(
        "2026-11-19",
        "2026-11-19T09:15",
        "https://coal.mysteel.com/a/26111909/FFB31951A5A21269.html",
        {"output": 66.2, "mine": 252.9},
    ),
    Release(
        "2026-11-13",
        "2026-11-13T08:05",
        "https://coal.mysteel.com/a/26111308/55CD0C8DC79F04CF.html",
        {"coke": 752.66, "days": 11.8, "use": 69.48},
    ),
    Release(
        "2026-11-20",
        "2026-11-20T08:33",
        "https://jiaotan.mysteel.com/a/26112008/F2767B1E8D2E5FE3.html",
        {"coke": 748.89, "days": 11.6, "use": 70.34},
    ),
    Release(
        "2026-11-13",
        "2026-11-13T09:50",
        "https://coal.mysteel.com/a/26111309/C89A9E00ABCD0253.html",
        {"port": 761.77},
    ),
    Release(
        "2026-11-20",
        "2026-11-20T09:00",
        "https://coal.mysteel.com/a/26112008/279D99C8A60D88DB.html",
        {"port": 783.08},
    ),
)


def _links(page, index, today):
    result = {}
    for a in page.select("a[href]"):
        title = "".join(a.stripped_strings)
        if index == DAILY:
            label = re.fullmatch(r"(\d{1,2})月(\d{1,2})日大商所焦煤期货行情", title)
            nid = re.fullmatch(r"/2(\d{6,10})", a["href"])
            if label and nid:
                month, d = map(int, label.groups())
                year = today.year - ((month, d) > (today.month, today.day))
                period = date(year, month, d)
                result["market", period] = _report_url(nid[1])
            continue
        url = urljoin(index, a["href"])
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.netloc not in ("coal.mysteel.com", "jiaotan.mysteel.com")
            or parts.query
            or parts.fragment
            or not re.fullmatch(r"/a/\d{8}/[a-fA-F\d]{16}\.html", parts.path)
        ):
            continue
        for kind, name in SURVEYS.items():
            m = re.fullmatch(r"(?:Mysteel数据[:：])?\s*" + name + r"[（(](\d{8})[）)]", title)
            if m:
                period = date.fromisoformat(m[1])
                result[kind, period] = url
    return result


def _market(page, day, url):
    title = page.select_one(".show_title h3")
    if not title or title.get_text(strip=True) != f"{day.month}月{day.day}日大商所焦煤期货行情":
        raise ValueError("different market edition")
    stamps = [e.get_text(strip=True) for e in page.select(".show_title span")]
    stamp = next(s for s in stamps if re.fullmatch(day.isoformat() + r" 1[5-9]:\d{2}:\d{2}", s))
    result = {}
    for table in page.select("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"], recursive=False)]
        expected = ["交割月份", "收盘价", "成交手", "变化"]
        if not all(header.count(k) == 1 for k in expected):
            continue
        for tr in rows[1:]:
            cells = [c.get_text(strip=True) for c in tr.find_all(["th", "td"], recursive=False)]
            if len(cells) != len(header):
                continue
            code, close, vol, change = [cells[header.index(k)] for k in expected]
            if not re.fullmatch(r"jm\d{2}(?:01|05|09)", code) or not vol.isdigit() or int(vol) <= 0:
                continue
            code = code.upper()
            price = float(close)
            if (
                code in result
                or not math.isfinite(price)
                or price <= 0
                or not re.fullmatch(r"[+-]?\d+", change)
            ):
                raise ValueError("invalid contract")
            result[code] = price
            result[code + "_change"] = int(change)
    if not result:
        raise ValueError("no contract closes")
    return Release(day.isoformat(), stamp.replace(" ", "T"), url, result)


def _survey(page, kind, day, url):
    title = page.find("h1")
    if (
        title is None
        or SURVEYS[kind] not in title.get_text(strip=True)
        or day.strftime("%Y%m%d") not in title.get_text(strip=True)
    ):
        raise ValueError("different survey edition")
    text = "".join(page.stripped_strings)
    published = re.search(r"(20\d\d-\d\d-\d\d)\s*(\d\d:\d\d)(?::(\d\d))?\s*来源\s*[:：]\s*我的钢铁网", text)
    if not published or published[1] < day.isoformat():
        raise ValueError("no publication timestamp")
    # Isolate the named survey and its first paragraph. Other samples on the same
    # page have identically named metrics with different values.
    specs = {
        "coke": (
            "230家独立焦企样本",
            (("coke", "炼焦煤总库存", "万吨"), ("days", "焦煤可用天数", "天"), ("use", "产能利用率", "%")),
        ),
        "port": ("16个港口进口焦煤库存", (("port", "库存", "万吨"),)),
        "mine": ("523家炼焦煤矿山样本", (("output", "精煤日均产量", "万吨"), ("mine", "精煤库存", "万吨"))),
    }
    marker, fields = specs[kind]
    body = text.partition(marker)[2]
    if not body:
        raise ValueError("no matching sample")
    body = marker + re.split(r"表一|调研指标|表1|更多资讯|本周Mysteel统计", body, maxsplit=1)[0][:350]
    values = {}
    for key, label, unit in fields:
        m = re.search(label + r"(?:为)?(\d{1,7}(?:\.\d{1,4})?)" + re.escape(unit), body)
        if not m:
            raise ValueError("missing field or unit")
        values[key] = float(m[1])
    stamp = f"{published[1]}T{published[2]}:{published[3] or '00'}"
    return Release(day.isoformat(), stamp, url, values)


async def _get(client, url):
    # No redirects, browser identities or retries. A refused source stays unavailable.
    async with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
        response.raise_for_status()
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            if len(raw) + len(chunk) > 1024 * 1024:
                raise ValueError("oversized report")
            raw.extend(chunk)
    return BeautifulSoup(raw.decode("utf-8"), "html.parser")


ERRORS = (httpx.HTTPError, ValueError, TimeoutError, StopIteration, OverflowError)
_refresh_after = 0.0
_lock = asyncio.Lock()


async def _recent_for_index(client, index, today):
    try:
        links = _links(await _get(client, index), index, today)
    except ERRORS:
        return []

    async def get_one(kind, day, url):
        try:
            page = await _get(client, url)
            return _market(page, day, url) if kind == "market" else _survey(page, kind, day, url)
        except ERRORS:
            return None

    # Index pages may contain navigation to years of archive. Bound fan-out by
    # dataset; parsing and provenance checks decide whether each issue is usable.
    wanted = []
    for kind in ("market",) if index == DAILY else ("coke",) if index == COKE else ("port", "mine"):
        items = sorted(
            ((day, u) for (k, day), u in links.items() if k == kind and 0 <= (today - day).days <= 45),
            reverse=True,
        )
        wanted += [get_one(kind, day, url) for day, url in items[: (4 if kind == "market" else 2)]]
    results = await asyncio.gather(*wanted)
    return [r for r in results if r is not None]


def _store_editions(rows):
    # Only four public series and one immutable original edition per reporting
    # period. Frozen user judgments are kept separately from shareable public data.
    archive = []
    for row in rows:
        series = next((k for k in SURVEYS if k in row.values), "market")
        archive.append((series, row.period, row.known, row.url, json.dumps(row.values, allow_nan=False)))
    if archive:
        with get_db() as db:
            db.executemany(
                """INSERT INTO jm_research_editions(series,period,known_at,source_url,values_json)
                VALUES (?,?,?,?,?) ON CONFLICT(series,period) DO NOTHING""",
                archive,
            )


def _stored(local, target, series=None):
    # Indexed lookups also work after process restarts and when the target has
    # long since disappeared from the publishers' first directory page.
    out = []
    with get_db() as db:
        for kind in (series,) if series else ("market", *SURVEYS):
            period_test = "period=?" if target else "period<=?"
            found = db.execute(
                "SELECT period,known_at,source_url,values_json FROM jm_research_editions "
                f"WHERE series=? AND {period_test} AND known_at<=? ORDER BY period DESC LIMIT 2",
                (kind, target or local.date().isoformat(), local.isoformat(timespec="seconds")),
            )
            out.extend(Release(r[0], r[1], r[2], json.loads(r[3])) for r in found)
    return out


async def _live():
    global _refresh_after
    async with _lock:
        if time.monotonic() < _refresh_after:
            return

        async def read(index):
            try:
                async with asyncio.timeout(7), public_network.async_client(timeout=4) as client:
                    return await _recent_for_index(client, index, datetime.now(SHANGHAI).date())
            except ERRORS:
                return []

        pages = await asyncio.gather(*(read(idx) for idx in (DAILY, COAL, COKE)))
        _store_editions(row for group in pages for row in group)
        _refresh_after = time.monotonic() + 300


async def capture_for_saved_judgments():
    # Every offered target is due within seven days. Allow three further weeks
    # for delayed publications / a restarted server; no scheduled model calls.
    now = datetime.now(SHANGHAI)
    with get_db() as db:
        active = db.execute(
            """SELECT 1 FROM jm_research_journal WHERE session='current'
            AND saved_at>=? AND review_json IS NULL LIMIT 1""",
            ((now - timedelta(days=28)).isoformat(),),
        ).fetchone()
    if active:
        await _live()


async def editions(cutoff, *, target=None, series=None):
    local = cutoff.astimezone(SHANGHAI).replace(tzinfo=None)
    live = []
    if local.date() > date(2026, 11, 20):
        if target:
            live = _stored(local, target, series)
        if not target or not live:
            await _live()
            live = _stored(local, target, series)
    # A reviewed vintage wins if a subsequently collected copy of that edition
    # has different content or timestamp.
    by_url = {r.url: r for r in [*live, *ARCHIVE]}
    return sorted(
        (
            r
            for r in by_url.values()
            if datetime.fromisoformat(r.known) <= local
            and r.period <= local.date().isoformat()
            and (not target or r.period == target)
        ),
        key=lambda r: (r.period, r.known),
    )
