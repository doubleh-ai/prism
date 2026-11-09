"""CGA's dated national gold statistics, retaining original cumulative periods.

Imported raw-material production is not customs gold imports. Reported totals
and rounded categories are retained separately, never differenced into quarters
or converted into a global balance, deliverable inventory or capital flow.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from calendar import monthrange
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from .data_availability import source_failure
from .futures_fundamentals import _HTML, _number, _text

INDEX = "https://cngold.org.cn/news-333-1.html"
BODY_LIMIT = 1024 * 1024
INDEX_PAGES = 3
RELEASES = 2
SECONDS = 6
PERIODS = {"一季度": 3, "上半年": 6, "前三季度": 9, "全年": 12, "": 12}
TITLE = re.compile(r"^(20\d{2})年(一季度|上半年|前三季度|全年)?我国黄金产量")
NUMBER = r"\d{1,9}(?:\.\d{1,4})?"
# Headline domestic production and consumption come first for the existing UI.
METRICS = (
    ("domestic_raw_material_production", "国内原料产金", r"国内原料产金(?:累计)?"),
    ("gold_consumption", "黄金消费量", r"我国黄金消费量"),
    ("imported_raw_material_production", "进口原料产金", r"(?<!和)进口原料产金(?:累计)?"),
    ("combined_raw_material_production", "国内与进口原料产金合计", r"国内原料和进口原料共计生产黄金"),
    ("jewellery_consumption", "黄金首饰消费", r"黄金首饰"),
    ("bars_and_coins_consumption", "金条及金币消费", r"金条及金币"),
    ("industrial_and_other_consumption", "工业及其他用金", r"工业及其他用金"),
)
NOTICE = (
    "中国黄金协会的国内黄金产量与消费统计，单位为吨，均保留年初至该期的累计口径和原文同比。"
    "进口原料产金不是海关黄金进口量；各项按原文精度分别保留，不强行配平、不用累计差分推算单季。"
    "这些资料不代表全球供需、交易所可交割产量、社会库存、现货基差或资金净流入。"
)


def _need(value):
    if not value:
        raise ValueError("invalid CGA gold statistics")


def _url(url, *, index=False):
    parts = urlsplit(url)
    pattern = r"/news-333-[1-3]\.html" if index else r"/news/show-\d{1,10}\.html"
    _need(
        parts.scheme == "https"
        and parts.netloc == "cngold.org.cn"
        and not parts.query
        and not parts.fragment
        and re.fullmatch(pattern, parts.path) is not None
    )


def _period(title):
    match = TITLE.match(title)
    _need(match is not None and "黄金消费量" in title)
    year, name = int(match[1]), match[2] or ""
    _need(2010 <= year <= 2100)
    last = PERIODS[name]
    return year, last, f"{year}年{name}"


def parse_index(raw, index_url, today):
    _url(index_url, index=True)
    _need(type(raw) is bytes and 0 < len(raw) <= BODY_LIMIT)
    parser = _HTML()
    parser.feed(raw.decode("utf-8-sig"))
    parser.close()
    found = {}
    for href, title in parser.links:
        if TITLE.match(title) is None:
            continue
        try:
            url = urljoin(index_url, href)
            _url(url)
            year, last, _label = _period(title)
            stamp = re.search(r" (20\d{2}-\d{2}-\d{2})\Z", title)
            _need(stamp is not None)
            published = date.fromisoformat(stamp[1])
            end = date(year, last, monthrange(year, last)[1])
            _need(end < published <= today)
        except (ValueError, TypeError):
            continue
        item = {"url": url, "published_date": published.isoformat(), "period": (year, last)}
        _need(url not in found or found[url] == item)
        found[url] = item
    return sorted(
        found.values(), key=lambda row: (row["period"], row["published_date"], row["url"]), reverse=True
    )


class _ReleaseHTML(HTMLParser):
    """Scope identity and observations to the article, excluding navigation."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.parts, self.counts = [], {key: [] for key in ("title", "meta", "body")}, {}

    def handle_starttag(self, tag, attrs):
        classes = set(dict(attrs).get("class", "").split())
        parent = self.stack[-1][1] if self.stack else None
        zone = parent
        if tag in {"script", "style", "template"} or parent == "hidden":
            zone = "hidden"
        elif tag == "div":
            if "details_top" in classes:
                zone = "top"
            elif "details_con" in classes:
                zone = "body"
            elif parent == "top" and "t1" in classes:
                zone = "title"
            elif parent == "top" and "min" in classes:
                zone = "meta"
            if zone in self.parts and zone != parent:
                self.counts[zone] = self.counts.get(zone, 0) + 1
        if (tag in {"p", "div", "br"} or (tag == "span" and zone == "meta")) and zone in self.parts:
            self.parts[zone].append(" ")
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            _need(len(self.stack) < 100)
            self.stack.append((tag, zone))

    def handle_data(self, text):
        if self.stack and self.stack[-1][1] in self.parts:
            self.parts[self.stack[-1][1]].append(text)

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1][0] == tag:
            self.stack.pop()
        elif any(item[0] == tag for item in self.stack):
            # Malformed nesting inside a statistics article is not accepted.
            _need(not any(item[1] in self.parts for item in self.stack))
            while self.stack and self.stack[-1][0] != tag:
                self.stack.pop()
            self.stack.pop()


def _metric(body, stem):
    matches = re.findall(
        stem + rf"\s*({NUMBER})吨,\s*(?:同比(?:增加|减少){NUMBER}吨,\s*)?同比(增长|上升|下降)({NUMBER})%",
        body,
    )
    _need(len(matches) == 1)
    amount, direction, change = matches[0]
    yoy = _number(change, nonnegative=True)
    return _number(amount, nonnegative=True), -yoy if direction == "下降" else yoy


def parse_release(raw, url, today, *, expected_period=None, expected_published=None):
    _url(url)
    _need(type(raw) is bytes and 0 < len(raw) <= BODY_LIMIT)
    parser = _ReleaseHTML()
    parser.feed(raw.decode("utf-8-sig"))
    parser.close()
    _need(not parser.stack and parser.counts == {"title": 1, "meta": 1, "body": 1})
    title, metadata, body = (_text("".join(parser.parts[key])) for key in ("title", "meta", "body"))
    year, last, label = _period(title)
    _need(expected_period is None or (year, last) == tuple(expected_period))
    stamps = re.findall(r"(?<!\d)20\d{2}-\d{2}-\d{2}(?!\d)", metadata)
    _need(len(stamps) == 1 and re.search(r"来源:\s*中国黄金协会(?:\s|$)", metadata) is not None)
    published = date.fromisoformat(stamps[0])
    period_end = date(year, last, monthrange(year, last)[1])
    _need(period_end < published <= today)
    _need(expected_published is None or published.isoformat() == expected_published)
    _need(re.search(re.escape(label) + r",\s*国内原料产金", body) is not None)
    _need(re.search(re.escape(label) + r",\s*我国黄金消费量", body) is not None)
    products = []
    for product, name, stem in METRICS:
        amount, yoy = _metric(body, stem)
        products.append(
            {"product": product, "label": name, "unit": "吨", "period_amount": amount, "period_yoy_pct": yoy}
        )
    # The headline and article must describe the same domestic/consumption data.
    for product, stem in zip(products[:2], (r"我国黄金产量", r"黄金消费量"), strict=True):
        _need(_metric(title, stem) == (product["period_amount"], product["period_yoy_pct"]))
    return {
        "series_kind": "gold_year_to_date_production_consumption",
        "source_url": url,
        "body_sha256": hashlib.sha256(raw).hexdigest(),
        "published_date": published.isoformat(),
        "published_date_precision": "date",
        "period_start": date(year, 1, 1).isoformat(),
        "period_end": period_end.isoformat(),
        "period_label": label,
        "period_kind": "year_to_date",
        "scope": "china_gold_association_national_statistics",
        "products": products,
    }


async def _fetch(client, url, deadline, seen):
    _url(url, index="/news-333-" in url)
    _need(url not in seen and len(seen) < INDEX_PAGES + RELEASES)
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
        "topic": "国内黄金产量与消费背景",
        "source_kind": "industry_association_statistics",
        "index_url": INDEX,
        "observations": [],
        "notice": NOTICE,
        "global_supply_collected": False,
        "customs_imports_collected": False,
        "social_inventory_collected": False,
        "spot_basis_collected": False,
        "deliverable_grade_verified": False,
    }
    if symbol != "AU":
        return {**base, "status": "not_covered", "reason": "cga_product_series_not_covered"}
    end = min(deadline, asyncio.get_running_loop().time() + SECONDS)
    candidates, gaps, seen = {}, [], set()
    errors = (httpx.HTTPError, ValueError, TypeError, OverflowError, TimeoutError, RecursionError)
    for number in range(1, INDEX_PAGES + 1):
        url = f"https://cngold.org.cn/news-333-{number}.html"
        try:
            for item in parse_index(await _fetch(client, url, end, seen), url, today):
                candidates.setdefault(item["url"], item)
            if len({item["period"] for item in candidates.values()}) >= RELEASES:
                break
        except errors as error:
            gaps.append({"source_url": url, **source_failure(error)})
            break
    observations, periods = [], set()
    for item in sorted(
        candidates.values(), key=lambda row: (row["period"], row["published_date"], row["url"]), reverse=True
    ):
        if item["period"] in periods:
            continue
        periods.add(item["period"])
        if len(periods) > RELEASES:
            break
        try:
            observations.append(
                parse_release(
                    await _fetch(client, item["url"], end, seen),
                    item["url"],
                    today,
                    expected_period=item["period"],
                    expected_published=item["published_date"],
                )
            )
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
