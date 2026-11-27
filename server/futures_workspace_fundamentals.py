"""Additional product-specific readings for the futures workspace."""

import asyncio
import re
from datetime import date
from urllib.parse import urljoin, urlsplit

import httpx

from . import futures_soybean, public_network
from .futures_fundamentals import _HTML, _text

LIVESTOCK = "https://xmsyj.moa.gov.cn/jcyj/"
LABELS = {"LH": "全国生猪平均价格", "JD": "全国鸡蛋平均价格"}
ERRORS = (httpx.HTTPError, ValueError, TypeError, TimeoutError, RecursionError)


async def _page(client, url):
    async with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > 1024 * 1024:
                raise ValueError("oversized source document")
            body.extend(chunk)
    page = _HTML()
    page.feed(body.decode(response.encoding or "utf-8"))
    page.close()
    return page


def _reading(day, publisher, url, label, rows, scope):
    return {
        "data_as_of": day,
        "source": {"name": publisher, "url": url},
        "values": {"label": label, "rows": rows, "scope": scope},
    }


def _livestock_urls(links, today):
    candidates = {}
    for href, label in links:
        if "畜产品和饲料集贸市场价格情况" not in _text(label):
            continue
        url = urljoin(LIVESTOCK, href)
        parts = urlsplit(url)
        match = re.fullmatch(r"/jcyj/(20\d{4})/t(20\d{6})_\d+\.htm", parts.path)
        if (
            parts.scheme != "https"
            or parts.netloc != "xmsyj.moa.gov.cn"
            or parts.query
            or parts.fragment
            or not match
        ):
            continue
        folder, stamp = match.groups()
        published = date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:]))
        if folder == stamp[:6] and published <= today:
            candidates[url] = published
    return sorted(candidates.items(), key=lambda item: item[1], reverse=True)


def _livestock_readings(page, url, published):
    text = _text("".join(page.text))
    sample = re.search(r"采集日为\s*(\d{1,2})月\s*(\d{1,2})日", text)
    result = {}
    if not sample:
        return result
    month, day = map(int, sample.groups())
    sampled = date(published.year - (month > published.month), month, day)
    if not 0 <= (published - sampled).days <= 31:
        return result
    for symbol, label in LABELS.items():
        prices = re.findall(label + r"\s*(\d+(?:\.\d+)?)\s*元/公斤", text)
        if len(prices) == 1 and float(prices[0]) > 0:
            result[symbol] = _reading(
                sampled.isoformat(),
                "农业农村部畜牧兽医局",
                url,
                "集贸市场监测",
                [{"label": label, "value": float(prices[0]), "unit": "元/公斤"}],
                f"全国集贸市场监测均价。采集日见上方，发布日期为 {published.isoformat()}。",
            )
    return result


async def _livestock(client, today):
    index = await _page(client, LIVESTOCK)
    for url, published in _livestock_urls(index.links, today)[:2]:
        try:
            readings = _livestock_readings(await _page(client, url), url, published)
        except ERRORS:
            continue
        if readings:
            return readings
    return {}


async def _soybean(client, today):
    # CASDE's published balance is for all soybeans, also used by the meal reader.
    result = await futures_soybean.read_supply(client, "M", today, asyncio.get_running_loop().time() + 6)
    if result["status"] != "collected":
        return {}
    published = result["observations"][0]
    forecast = published["estimates"][-1]
    rows = [{"label": "中国大豆进口预测", "value": forecast["values"]["imports"], "unit": published["unit"]}]
    return {
        "B": _reading(
            published["published_date"],
            "农业农村部市场预警专家委员会",
            published["source_url"],
            "中国大豆供需月报",
            rows,
            f"{forecast['label']}。覆盖中国大豆进口，采用 CASDE 年度平衡表口径。",
        )
    }


def _starch_reference(today):
    # The dated release can be read as a recent reference while the automatic
    # price reader has yet to be verified. Expire it at the weekly boundary.
    published = date(2026, 11, 24)
    if not 1 <= (today - published).days <= 7:
        return {}
    return {
        "CS": _reading(
            published.isoformat(),
            "生意社",
            "https://www.100ppi.com/news/detail-20261124-6533687.html",
            "玉米淀粉基准价",
            [{"label": "生意社基准价", "value": 2778.0, "unit": "元/吨"}],
            "最近人工核验的生意社基准价，按原发布日期展示。",
        )
    }


async def read(today):
    async def bounded(reader):
        try:
            async with asyncio.timeout(10), public_network.async_client(timeout=6) as client:
                return await reader(client, today)
        except ERRORS:
            return {}

    async with asyncio.TaskGroup() as reads:
        livestock = reads.create_task(bounded(_livestock))
        soybean = reads.create_task(bounded(_soybean))
    return {**livestock.result(), **soybean.result(), **_starch_reference(today)}
