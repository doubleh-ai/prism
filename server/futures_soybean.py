"""Public CASDE soybean balance estimates: upstream context, not meal output.

Discover a bounded set of official releases. Preserve each original marketing
year, estimate/forecast label and unit; never turn a forecast into an observed
crop, an arrival schedule, meal production, inventory or a trading signal.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import date, datetime
from urllib.parse import urljoin, urlsplit

import httpx

from .data_availability import source_failure
from .futures_fundamentals import _HTML, _number, _text

INDEX = "https://scs.moa.gov.cn/jcyj/"
INDEX_PAGES = 3
RELEASES = 2
BODY_LIMIT = 1024 * 1024
SECONDS = 6
TITLE = re.compile(r"(20\d{2})年(\d{1,2})月中国农产品供需形势分析\(CASDE-No\.(\d{1,4})\)")
PATH = re.compile(r"/jcyj/(20\d{4})/t(20\d{6})_(\d{6,10})\.htm\Z")
METRICS = {
    "产量": "production",
    "进口": "imports",
    "消费": "consumption",
    "压榨消费": "crush_consumption",
    "食用消费": "food_consumption",
    "种子用量": "seed_consumption",
    "损耗及其它": "loss_and_other",
    "出口": "exports",
    "结余变化": "balance_change",
}
NOTICE = (
    "农业农村部中国大豆年度供需估计及预测，仅作豆粕上游原料和压榨背景。大豆市场年度为当年10月至"
    "次年9月；按原表保留历史值、估计和预测，不能当作已实现产量或月度到港。压榨消费是大豆投入量，"
    "不是豆粕产量；结余变化不是期末库存。未据此估算出粕率、油厂开工、社会库存、现货基差或资金流向。"
)


def _need(ok):
    if not ok:
        raise ValueError("invalid CASDE soybean balance")


class _Document(_HTML):
    def __init__(self):
        super().__init__()
        self.table_headings, self.publication_dates, self.article_titles = [], [], []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "table":
            self.table_headings.append(_text("".join(self.text[-50:]))[-300:])
        if tag == "meta":
            if values.get("name") == "PubDate":
                self.publication_dates.append(values.get("content", ""))
            elif values.get("name") == "ArticleTitle":
                self.article_titles.append(_text(values.get("content", "")))
        super().handle_starttag(tag, attrs)


def _document(raw):
    _need(type(raw) is bytes and 0 < len(raw) <= BODY_LIMIT)
    doc = _Document()
    doc.feed(raw.decode("utf-8"))
    doc.close()
    _need(doc.table is doc.row is doc.cell is None and len(doc.tables) == len(doc.table_headings))
    return doc


def _release(url):
    parts = urlsplit(url)
    match = PATH.fullmatch(parts.path)
    _need(parts.scheme == "https" and parts.netloc == "scs.moa.gov.cn")
    _need(not parts.query and not parts.fragment and match is not None)
    folder, stamp, _ = match.groups()
    published = date.fromisoformat(stamp[:4] + "-" + stamp[4:6] + "-" + stamp[6:])
    _need(folder == stamp[:6])
    return published


def _title(text):
    match = TITLE.search(_text(text))
    _need(match is not None)
    year, month, report = map(int, match.groups())
    _need(2010 <= year <= 2100 and 1 <= month <= 12 and report > 0)
    return year, month, report


def parse_index(raw, index_url, today):
    found = {}
    for href, text in _document(raw).links:
        if TITLE.search(_text(text)) is None:
            continue
        url = urljoin(index_url, href)
        try:
            published = _release(url)
            year, month, report = _title(text)
        except (ValueError, TypeError):
            continue
        _need(published <= today and (published.year, published.month) == (year, month))
        item = {"url": url, "published_date": published.isoformat(), "report": (year, month, report)}
        _need(url not in found or found[url] == item)
        found[url] = item
    return sorted(found.values(), key=lambda row: (row["published_date"], row["url"]), reverse=True)


def _column(label, year):
    match = re.fullmatch(r"(20\d{2})/(\d{2})(?:\s*\((\d{1,2})月(估计|预测)\))?", label)
    _need(match is not None)
    start, end, month, kind = match.groups()
    start = int(start)
    _need(int(end) == (start + 1) % 100 and year - 3 <= start <= year + 1)
    _need(month is None or 1 <= int(month) <= 12)
    return {
        "label": label,
        "market_year": f"{start}/{end}",
        "period_start": f"{start}-10-01",
        "period_end": f"{start + 1}-09-30",
        "kind": {"估计": "estimate", "预测": "forecast"}.get(kind, "reported_history"),
        "issue_month": int(month) if month else None,
        "values": {},
    }


def parse_release(raw, url, today, *, expected_report=None):
    published = _release(url)
    _need(published <= today)
    doc = _document(raw)
    identity = _title("".join(doc.title))
    year, month, report = identity
    _need((year, month) == (published.year, published.month))
    _need(expected_report is None or identity == tuple(expected_report))
    _need(len(doc.publication_dates) == 1)
    _need(re.fullmatch(r"20\d{2}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", doc.publication_dates[0]))
    _need(datetime.strptime(doc.publication_dates[0], "%Y-%m-%d %H:%M:%S").date() == published)
    _need(not doc.article_titles or all(_title(title) == identity for title in doc.article_titles))
    _need("大豆市场年度为当年10月至下年9月" in "".join(doc.text))
    matches = [
        table
        for heading, table in zip(doc.table_headings, doc.tables, strict=True)
        if heading.endswith("中国大豆供需平衡表")
    ]
    _need(1 <= len(matches) <= 2 and all(table == matches[0] for table in matches))
    table = matches[0]
    _need(table and len(table[0]) == 5 and table[0][0] == "")
    columns = [_column(label, year) for label in table[0][1:]]
    _need(len({column["label"] for column in columns}) == len(columns))
    _need(columns[-1]["kind"] == "forecast" and columns[-1]["issue_month"] == month)
    # CASDE introduces the next soybean crop in May. A release month is not
    # permission to accept a forecast for an unrelated future marketing year.
    target_year = year if month >= 5 else year - 1
    _need(columns[-1]["period_start"] == f"{target_year}-10-01")
    unit, seen = None, set()
    for row in table[1:]:
        if len(row) == 1:
            unit = row[0].replace(" ", "")
        elif row and row[0] in METRICS:
            label = row[0]
            _need(unit == "万吨(10000tons)" and len(row) == 5 and label not in seen)
            seen.add(label)
            for column, value in zip(columns, row[1:], strict=True):
                column["values"][METRICS[label]] = _number(value, nonnegative=label != "结余变化")
    _need(seen == set(METRICS))
    return {
        "source_url": url,
        "body_sha256": hashlib.sha256(raw).hexdigest(),
        "published_date": published.isoformat(),
        "published_date_precision": "date",
        "report_number": report,
        "period_label": f"{year}年{month}月报告",
        "period_kind": "annual_balance_estimate_release",
        "series_kind": "soybean_annual_balance_estimates",
        "report_month": f"{year}-{month:02d}",
        "unit": "万吨",
        "metrics": [{"key": key, "label": label} for label, key in METRICS.items()],
        "estimates": columns,
        "duplicate_tables": len(matches),
    }


async def _fetch(client, url, deadline, seen):
    _need(url not in seen and len(seen) < INDEX_PAGES + RELEASES)
    if url not in {INDEX, *(INDEX + f"index_{n}.htm" for n in range(1, INDEX_PAGES))}:
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
            )
            _need(response.headers.get("content-type", "").split(";")[0].strip().lower() == "text/html")
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                _need(len(raw) + len(chunk) <= BODY_LIMIT)
                raw.extend(chunk)
            return bytes(raw)


async def read_supply(client, symbol, today, deadline):
    base = {
        "topic": "大豆年度供需与压榨背景",
        "source_kind": "official_public_release",
        "index_url": INDEX,
        "observations": [],
        "notice": NOTICE,
        "soybean_meal_output_collected": False,
        "monthly_arrivals_collected": False,
        "social_inventory_collected": False,
        "spot_basis_collected": False,
        "operating_rate_collected": False,
    }
    if symbol != "M":
        return {**base, "status": "not_covered", "reason": "not_soybean_meal_context"}
    end = min(deadline, asyncio.get_running_loop().time() + SECONDS)
    candidates, gaps, seen = {}, [], set()
    errors = (httpx.HTTPError, ValueError, TypeError, OverflowError, TimeoutError, RecursionError)
    for n in range(INDEX_PAGES):
        url = INDEX if n == 0 else INDEX + f"index_{n}.htm"
        try:
            for item in parse_index(await _fetch(client, url, end, seen), url, today):
                candidates.setdefault(item["url"], item)
            if len({item["report"][:2] for item in candidates.values()}) >= RELEASES:
                break
        except errors as error:
            gaps.append({"source_url": url, **source_failure(error)})
            break
    observations, reports = [], set()
    for item in sorted(
        candidates.values(), key=lambda row: (row["published_date"], row["url"]), reverse=True
    ):
        period = item["report"][:2]
        if period in reports:
            continue
        reports.add(period)
        if len(reports) > RELEASES:
            break
        try:
            raw = await _fetch(client, item["url"], end, seen)
            observations.append(parse_release(raw, item["url"], today, expected_report=item["report"]))
        except errors as error:
            gaps.append({"source_url": item["url"], **source_failure(error)})
    return {
        **base,
        "status": "collected" if observations else "unavailable",
        "source_url": observations[0]["source_url"] if observations else INDEX,
        "data_as_of": observations[0]["published_date"] if observations else None,
        "history_window_complete": len(observations) == RELEASES and not gaps,
        "collection_gaps": gaps,
        "observations": observations,
        "max_index_pages": INDEX_PAGES,
        "max_releases": RELEASES,
        "automatic_retry": False,
    }
