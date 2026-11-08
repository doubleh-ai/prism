"""Dated NBS steel production: national supply context, never rebar output or inventory.

Only fixed public HTTPS index/release paths are read. Each URL is requested at
most once, with a shared deadline and finite discovery/body limits. Published
monthly and cumulative figures retain their original periods and official
year-on-year values; changing enterprise coverage prevents reconstructing them
by subtracting releases.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import unicodedata
from datetime import date
from decimal import Decimal
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from .data_availability import source_failure

INDEX = "https://www.stats.gov.cn/sj/zxfb/"
BODY_LIMIT = 1024 * 1024
INDEX_PAGES = 3
RELEASES = 2
SECONDS = 6
LABELS = {"生铁(万吨)": "pig_iron", "粗钢(万吨)": "crude_steel", "钢材(万吨)": "steel_products"}
RELEASE_PATH = re.compile(r"/sj/zxfb/(\d{6})/t(\d{8})_(\d{5,12})\.html\Z")
TITLE = re.compile(r"(20\d{2})年(?:(1)[—－–-](2)|(\d{1,2}))月份?规模以上工业增加值")
NOTICE = (
    "全国规模以上工业企业的生铁、粗钢及钢材产量，仅作螺纹钢供给背景；不是螺纹钢专属产量、"
    "钢厂开工率、社会库存或现货基差。单位为万吨，同比采用原文。企业统计范围及历史数可能修订，"
    "不以累计差分替代当月产量或重算官方同比。"
)


def _need(value):
    if not value:
        raise ValueError("invalid NBS steel release")


def _text(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


class _HTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.links, self.title, self.text = [], [], [], []
        self.table = self.row = self.cell = self.anchor = None
        self.in_title = False
        self.hidden = 0
        self.cells = 0

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag == "title":
            self.in_title = True
        if tag == "a":
            self.anchor = [values.get("href", ""), [], values.get("title", "")]
        if tag == "table":
            _need(self.table is None and len(self.tables) < 12)
            self.table = []
        elif tag == "tr" and self.table is not None:
            _need(self.row is None and len(self.table) < 600)
            self.row = []
        elif tag in {"th", "td"} and self.row is not None:
            _need(self.cell is None and len(self.row) < 20)
            self.cells += 1
            _need(self.cells <= 6000)
            self.cell = []

    def handle_data(self, value):
        if not self.hidden:
            self.text.append(value)
        if self.in_title:
            self.title.append(value)
        if self.anchor is not None:
            self.anchor[1].append(value)
        if self.cell is not None:
            self.cell.append(value)

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag == "title":
            self.in_title = False
        if tag == "a" and self.anchor is not None:
            href, words, title = self.anchor
            _need(len(self.links) < 4096)
            self.links.append((href, _text(title or "".join(words))))
            self.anchor = None
        if tag in {"td", "th"} and self.cell is not None:
            self.row.append(_text("".join(self.cell)))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            _need(self.row is None and self.cell is None)
            self.tables.append(self.table)
            self.table = None


def _html(raw):
    _need(type(raw) is bytes and 0 < len(raw) <= BODY_LIMIT)
    parser = _HTML()
    parser.feed(raw.decode("utf-8"))
    parser.close()
    _need(parser.table is parser.row is parser.cell is None)
    return parser


def _release(url):
    parts = urlsplit(url)
    match = RELEASE_PATH.fullmatch(parts.path)
    _need(
        parts.scheme == "https"
        and parts.netloc == "www.stats.gov.cn"
        and not parts.query
        and not parts.fragment
        and match is not None
    )
    folder, stamp, _ = match.groups()
    published = date.fromisoformat(stamp[:4] + "-" + stamp[4:6] + "-" + stamp[6:])
    _need(folder == stamp[:6])
    return published


def _period(title):
    match = TITLE.search(title)
    _need(match is not None)
    year, combined_start, combined_end, month = match.groups()
    year, end = int(year), int(combined_end or month)
    _need(2010 <= year <= 2100 and (combined_start is not None or 3 <= end <= 12))
    start = 1 if combined_start else end
    return year, start, end


def parse_index(raw, index_url, today):
    parser = _html(raw)
    matches = {}
    for href, title in parser.links:
        if TITLE.search(title) is None:
            continue
        url = urljoin(index_url, href)
        try:
            published = _release(url)
            period = _period(title)
        except (ValueError, TypeError):
            continue
        _need(published <= today and date(period[0], period[2], 1) <= published)
        item = {"url": url, "published_date": published.isoformat(), "period": period}
        _need(url not in matches or matches[url] == item)
        matches[url] = item
    return sorted(matches.values(), key=lambda row: (row["published_date"], row["url"]), reverse=True)


def _number(value, *, nonnegative=False):
    _need(type(value) is str and re.fullmatch(r"-?\d{1,9}(?:\.\d{1,4})?", value) is not None)
    result = Decimal(value)
    _need(result.is_finite() and abs(result) <= 10**9 and (not nonnegative or result >= 0))
    return int(result) if result == int(result) else float(result)


def parse_release(raw, url, today, *, expected_period=None):
    published = _release(url)
    _need(published <= today)
    parser = _html(raw)
    year, first, last = _period(_text("".join(parser.title)))
    _need(expected_period is None or (year, first, last) == tuple(expected_period))
    from calendar import monthrange

    period_end = date(year, last, monthrange(year, last)[1])
    _need(period_end < published)
    body = " ".join(parser.text)
    displayed = re.findall(r"(?<!\d)(20\d{2})[/-](\d{2})[/-](\d{2})\s+\d{2}:\d{2}", body)
    _need(displayed and {date(int(y), int(m), int(d)) for y, m, d in displayed} == {published})
    combined = first != last
    period_header = "1—2月" if combined else f"{last}月"
    cumulative_header = f"1—{last}月"
    matching = []
    for table in parser.tables:
        if any(row and any(label in row[0] for label in ("生铁", "粗钢", "钢材")) for row in table):
            _need(
                table[:2]
                == (
                    [["指标", period_header], ["绝对量", "同比增长 (%)"]]
                    if combined
                    else [
                        ["指标", period_header, cumulative_header],
                        ["绝对量", "同比增长 (%)", "绝对量", "同比增长 (%)"],
                    ]
                )
            )
            matching.append(table)
    _need(1 <= len(matching) <= 2 and all(table == matching[0] for table in matching))
    products = {}
    for row in matching[0]:
        if row and any(label in row[0] for label in ("生铁", "粗钢", "钢材")):
            _need(row[0] in LABELS and len(row) == (3 if combined else 5) and row[0] not in products)
            products[row[0]] = {
                "product": LABELS[row[0]],
                "label": row[0][:-4],
                "unit": "万吨",
                "period_amount": _number(row[1], nonnegative=True),
                "period_yoy_pct": _number(row[2]),
                **(
                    {}
                    if combined
                    else {
                        "year_to_date_amount": _number(row[3], nonnegative=True),
                        "year_to_date_yoy_pct": _number(row[4]),
                    }
                ),
            }
    _need(set(products) == set(LABELS))
    return {
        "source_url": url,
        "body_sha256": hashlib.sha256(raw).hexdigest(),
        "published_date": published.isoformat(),
        "published_date_precision": "date",
        "period_start": date(year, first, 1).isoformat(),
        "period_end": period_end.isoformat(),
        "period_label": f"{year}年{first}—{last}月" if combined else f"{year}年{last}月",
        "period_kind": "combined_january_february" if combined else "calendar_month",
        "year_to_date_start": date(year, 1, 1).isoformat(),
        "scope": "national_industrial_enterprises_above_designated_size",
        "products": [products[label] for label in LABELS],
        "duplicate_tables": len(matching),
    }


async def _fetch(client, url, deadline, seen):
    _need(url not in seen and len(seen) < INDEX_PAGES + RELEASES)
    if url != INDEX and url not in {INDEX + f"index_{number}.html" for number in range(1, INDEX_PAGES)}:
        _release(url)
    _need(
        client.auth is None
        and set(client.headers) <= {"accept", "accept-encoding", "connection", "user-agent"}
    )
    if asyncio.get_running_loop().time() >= deadline:
        raise TimeoutError()
    seen.add(url)
    async with asyncio.timeout_at(deadline):
        client.cookies.clear()
        async with client.stream(
            "GET",
            url,
            headers={"Accept": "text/html", "Accept-Encoding": "identity"},
            auth=None,
            follow_redirects=False,
        ) as response:
            response.raise_for_status()
            _need(
                response.status_code == 200
                and response.headers.get("content-encoding", "identity").lower() == "identity"
                and response.headers.get("content-type", "").split(";")[0].strip().lower() == "text/html"
            )
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                _need(len(raw) + len(chunk) <= BODY_LIMIT)
                raw.extend(chunk)
            return bytes(raw)


async def read_supply(client, symbol, today, deadline):
    base = {
        "topic": "全国钢铁生产供给背景",
        "source_kind": "official_public_release",
        "index_url": INDEX,
        "observations": [],
        "notice": NOTICE,
        "rebar_specific_output": False,
        "social_inventory_collected": False,
        "spot_basis_collected": False,
        "operating_rate_collected": False,
    }
    if symbol != "RB":
        return {**base, "status": "not_covered", "reason": "national_steel_series_is_rb_context_only"}
    end = min(deadline, asyncio.get_running_loop().time() + SECONDS)
    candidates, gaps, seen = {}, [], set()
    errors = (httpx.HTTPError, ValueError, TypeError, OverflowError, TimeoutError, RecursionError)
    for number in range(INDEX_PAGES):
        url = INDEX if number == 0 else INDEX + f"index_{number}.html"
        try:
            raw = await _fetch(client, url, end, seen)
            for item in parse_index(raw, url, today):
                candidates.setdefault(item["url"], item)
            if len({tuple(item["period"]) for item in candidates.values()}) >= RELEASES:
                break
        except errors as error:
            gaps.append({"source_url": url, **source_failure(error)})
            break  # Preserve an earlier index; do not retry or solve access challenges.
    observations, periods = [], set()
    for item in sorted(
        candidates.values(), key=lambda row: (row["published_date"], row["url"]), reverse=True
    ):
        period = tuple(item["period"])
        if period in periods:
            continue
        periods.add(period)
        if len(periods) > RELEASES:
            break
        try:
            raw = await _fetch(client, item["url"], end, seen)
            observations.append(parse_release(raw, item["url"], today, expected_period=period))
        except errors as error:
            gaps.append({"source_url": item["url"], **source_failure(error)})
    return {
        **base,
        "status": "collected" if observations else "unavailable",
        "source_url": observations[0]["source_url"] if observations else INDEX,
        "data_as_of": observations[0]["period_end"] if observations else None,
        "history_window_complete": len(observations) == RELEASES and not gaps,
        "collection_gaps": gaps,
        "observations": observations,
        "max_index_pages": INDEX_PAGES,
        "max_releases": RELEASES,
        "automatic_retry": False,
    }
