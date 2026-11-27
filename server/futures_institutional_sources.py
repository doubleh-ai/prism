"""Bounded, credential-free reads of named sections in two publishers' futures reports."""

import asyncio
import logging
import re
from datetime import date, datetime, time, timedelta
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from . import public_network
from .agent.pdf_text import MAX_PDF_BYTES, extract_pdf
from .futures_institutional_views import EAST, SHANGHAI, WINDOW_DAYS
from .work_lifecycle import current_lease, get_manager

RUIDA = "https://www.rdqh.com"
EAST_ORIGIN = "https://www.eafutures.com"
RD_INDEX = RUIDA + "/content/index/61"
EA_INDEX = EAST_ORIGIN + "/yjzx.html"
EA_SECTORS = {"70": ("RB", "JM"), "71": ("M",), "133": ("EC",)}
EA_TITLES = {
    "M": r"豆类周报[-—－]豆粕[、，]菜粕",
    "EC": r"(?:上海东亚期货)?运价指数周报[-—－]+集运[（(]欧线[）)]",
    "RB": r"钢材(?:市场周度报告|产业周报)",
    "JM": r"双焦市场周报",
}
HEADINGS = {
    "豆粕": "M",
    "集运指数(欧线)": "EC",
    "集运指数（欧线）": "EC",
    "螺纹": "RB",
    "螺纹钢": "RB",
    "焦煤": "JM",
}
EXTENDED_HEADINGS = {
    **HEADINGS,
    "沪金": "AU",
    "贵金属": "AU",
    "沪银": "AG",
    "沪铜": "CU",
    "沪铝": "AL",
    "氧化铝": "AO",
    "沪镍": "NI",
    "沪锡": "SN",
    "不锈钢": "SS",
    "碳酸锂": "LC",
    "工业硅": "SI",
    "多晶硅": "PS",
    "铁矿": "I",
    "铁矿石": "I",
    "热卷": "HC",
    "热轧卷板": "HC",
    "原油": "SC",
    "液化石油气": "PG",
    "LPG": "PG",
    "沥青": "BU",
    "甲醇": "MA",
    "PTA": "TA",
    "聚丙烯": "PP",
    "PVC": "V",
    "尿素": "UR",
    "纯碱": "SA",
    "玻璃": "FG",
    "烧碱": "SH",
    "沪胶": "RU",
    "天然橡胶": "RU",
    "20号胶": "NR",
    "纸浆": "SP",
    "豆一": "A",
    "豆二": "B",
    "菜粕": "RM",
    "菜籽粕": "RM",
    "棕榈油": "P",
    "菜油": "OI",
    "菜籽油": "OI",
    "花生": "PK",
    "玉米": "C",
    "玉米淀粉": "CS",
    "郑棉": "CF",
    "棉花": "CF",
    "郑糖": "SR",
    "白糖": "SR",
    "生猪": "LH",
    "苹果": "AP",
    "红枣": "CJ",
    "鸡蛋": "JD",
}
HTML_LIMIT = 1024 * 1024
EXCERPT_LIMIT = 280
_LOG = logging.getLogger(__name__)


def _fresh(day, now):
    return 0 <= (now.date() - day).days < WINDOW_DAYS


async def _get(client, url, *, pdf=False):
    limit = MAX_PDF_BYTES if pdf else HTML_LIMIT
    content = bytearray()
    async with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
        response.raise_for_status()
        media = response.headers.get("content-type", "").split(";", 1)[0].lower().strip()
        allowed = {"application/pdf", "application/octet-stream"} if pdf else {"text/html"}
        if (
            response.status_code != 200
            or response.headers.get("content-encoding", "identity") != "identity"
            or media not in allowed
        ):
            raise ValueError("unexpected report response")
        declared = response.headers.get("content-length")
        if declared is not None and (not declared.isdecimal() or int(declared) > limit):
            raise ValueError("report size")
        async for piece in response.aiter_bytes(65536):
            if len(content) + len(piece) > limit:
                raise ValueError("report size")
            content.extend(piece)
    if not content or (pdf and not content.startswith(b"%PDF-")):
        raise ValueError("report body")
    return bytes(content)


def _excerpt(text):
    # Retain complete contiguous closing sentences, including qualifications.
    text = " ".join(text.split()).strip()
    if not 12 <= len(text) <= 4000:
        return None
    if len(text) <= EXCERPT_LIMIT:
        return text
    for end in re.finditer("[。！？]", text):
        tail = text[end.end() :].strip()
        if 12 <= len(tail) <= EXCERPT_LIMIT:
            return tail
    return None


def _row(symbol, institution, title, url, day, available, locator, text):
    return dict(
        symbol=symbol,
        institution=institution,
        title=title,
        url=url,
        published_on=day.isoformat(),
        available_at=available.isoformat(timespec="seconds"),
        locator=locator,
        excerpt=text,
    )


def _rd_editions(body, now):
    page = BeautifulSoup(body, "html.parser")
    rows = page.select("ul.contact_list > li")
    if not rows:
        raise ValueError("unrecognized morning index")
    editions = {}
    recognized = False
    for row in rows:
        link, stamp = row.select_one("p > a[href]"), row.find("em")
        if not link or not stamp or not re.fullmatch(r"/content/show/61/\d{1,10}", link["href"]):
            continue
        title = link.get_text(strip=True)
        m = re.fullmatch(r"瑞达期货晨会纪要观点(\d{8})", title)
        if not m:
            continue
        day = date.fromisoformat(m[1])
        if stamp.get_text(strip=True) != day.isoformat():
            continue
        recognized = True
        if _fresh(day, now):
            edition = (day, title, RUIDA + link["href"])
            if day in editions and edition != editions[day]:
                raise ValueError("ambiguous morning edition")
            editions[day] = edition
    if not recognized:
        raise ValueError("unrecognized morning editions")
    return sorted(editions.values(), reverse=True)[:2]


def _rd_sections(body, day, title, url, now, *, headings=HEADINGS):
    page = BeautifulSoup(body, "html.parser")
    h = page.select_one(".work_article > h1")
    stamp = page.select_one(".work_article > p > span")
    main = page.select_one(".work_article + .work_article1")
    if not h or h.get_text(strip=True) != title or not main or not stamp:
        raise ValueError("wrong morning edition")
    published = re.fullmatch(r"时间[：:]\s*(\d{4}-\d\d-\d\d \d\d:\d\d)", stamp.get_text(strip=True))
    if not published:
        raise ValueError("missing publication time")
    known = datetime.fromisoformat(published[1]).replace(tzinfo=SHANGHAI)
    if known.date() != day:
        raise ValueError("conflicting publication date")
    if known > now:
        return []
    found = {}
    for heading in main.find_all("strong"):
        label = heading.get_text(strip=True)
        symbol = headings.get(label)
        p = heading.find_parent("p")
        if not symbol or not p or p.get_text(strip=True) != label:
            continue
        section = p.find_next_sibling()
        # A verified product heading still identifies today's edition when its body is
        # unreadable or ambiguous. It must supersede yesterday's attributed opinion.
        text = None
        if (
            symbol not in found
            and section
            and section.name == "p"
            and not section.find(["p", "section", "strong"])
        ):
            text = _excerpt(section.get_text(strip=True))
        found[symbol] = _row(symbol, "瑞达期货", title, url, day, known, f"晨会纪要 · {label}", text)
    if not found:
        raise ValueError("no identifiable morning sections")
    return list(found.values())


def _ea_entries(body, symbols, now):
    page = BeautifulSoup(body, "html.parser")
    entries = page.select("a.list[href]")
    if not entries:
        raise ValueError("unrecognized weekly index")
    candidates = {}
    recognized = False
    for a in entries:
        label, stamp = a.select_one(".left"), a.select_one(".time")
        if not label or not stamp or not re.fullmatch(r"/preview\?id=\d{1,10}&cid=41", a["href"]):
            continue
        title = label.get_text(strip=True)
        symbol = next((s for s in symbols if re.fullmatch(EA_TITLES[s] + r"[ _-]?\d{8}", title)), None)
        if not symbol:
            continue
        day = date.fromisoformat(stamp.get_text(strip=True).replace(".", "-"))
        if date.fromisoformat(title[-8:]) > day:
            raise ValueError("weekly report date after publication")
        recognized = True
        available = datetime.combine(day + timedelta(days=1), time.min, SHANGHAI)
        if not _fresh(day, now) or now < available:
            continue
        item = (day, available, title, EAST_ORIGIN + a["href"])
        if symbol not in candidates or day > candidates[symbol][0]:
            candidates[symbol] = item
        elif day == candidates[symbol][0] and item != candidates[symbol]:
            raise ValueError("ambiguous weekly edition")
    if not recognized:
        raise ValueError("unrecognized weekly editions")
    return candidates


async def _ea_pdf_url(client, url, day):
    # The site's preview route returns exactly one same-origin PDF redirect.
    async with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as preview:
        loc = urljoin(url, preview.headers.get("location", ""))
        if preview.status_code not in (301, 302, 303, 307, 308) or not re.fullmatch(
            re.escape(EAST_ORIGIN) + r"/storage/file/" + day.strftime("%Y%m%d") + r"/[0-9a-f]{32}\.pdf", loc
        ):
            raise ValueError("unexpected weekly preview")
    return loc


def _ea_section(symbol, pages):
    sections = []
    for page in pages:
        text = page["text"]
        if symbol == "M":
            match = re.search(
                r"(?:^|\n)M\s*豆粕\n[^\n]*\n[^\n]*\n本\s*周\s*逻\s*辑\s*\n(.+?)\n基\s*本\s*面\s*信\s*息",
                text,
                re.S,
            )
            label = "豆粕 · 本周逻辑"
        elif symbol == "EC":
            match = re.search(
                r"集运[（(]欧线[）)]\s*\n[•\s]*【观点】\s*\n(.+?)\n[•\s]*【基本面信息】", text, re.S
            )
            label = "集运（欧线）· 观点"
        else:
            product = {"RB": "螺纹钢", "JM": "焦煤"}[symbol]
            match = re.search(
                r"(?:^|\n)[一二三四五六七八九十]+、后市展望[（(]"
                + product
                + r"[）)]\s*\n(综合评估[：:].+?)(?=\n[ A-Z]*P\s*A\s*R\s*T\b|\n第[一二三四五六七八九十]+部分|\Z)",
                text,
                re.S,
            )
            label = f"{product} · 后市展望"
        if match:
            raw = match[1]
            # An unexpected heading ends attribution; never carry another product into an excerpt.
            if re.search(
                r"\n(?:RM\s*菜粕|第[一二三四五六七八九十]+部分|[一二三四五六七八九十]+、|[•\s]*【)", raw
            ):
                continue
            passage = _excerpt(raw.replace("\n", ""))
            if passage:
                sections.append((page["page"], label, passage))
    if len(sections) != 1:
        raise ValueError("missing or ambiguous weekly section")
    return sections[0]


async def _read_east(client, now):
    result, errors = [], 0
    for sector, symbols in EA_SECTORS.items():
        try:
            entries = _ea_entries(await _get(client, EA_INDEX + f"?breed={sector}&type=77"), symbols, now)
        except (httpx.HTTPError, ValueError):
            errors += 1
            continue
        for symbol, (day, available, title, url) in entries.items():
            try:
                link = await _ea_pdf_url(client, url, day)
            except (httpx.HTTPError, ValueError):
                errors += 1
                continue
            try:
                raw = await _get(client, link, pdf=True)
                parsed = await get_manager().run_blocking(
                    "auxiliary", extract_pdf, raw, first_page=1, page_count=9, parent=current_lease()
                )
                if parsed.get("status") != "ok":
                    raise ValueError("weekly PDF unreadable")
                number, label, excerpt = _ea_section(symbol, parsed["pages"])
                result.append(
                    _row(
                        symbol,
                        EAST,
                        title,
                        f"{link}#page={number}",
                        day,
                        available,
                        f"第{number}页 · {label}",
                        excerpt,
                    )
                )
            except (httpx.HTTPError, ValueError) as exc:
                errors += 1
                # The publisher's dated product title and exact original PDF were confirmed.
                # The unverified content yields a reference to that issue, with no quote or page.
                reference = _row(symbol, EAST, title, link, day, available, "官网周报目录", None)
                if isinstance(exc, httpx.HTTPError):
                    reference["download_unavailable"] = True
                result.append(reference)
    if errors:
        if not result:
            raise ValueError("weekly collection failed")
        _LOG.warning("Futures weekly report collection incomplete (%d sources)", errors)
    return result


async def collect(publisher: str, as_of: datetime) -> list[dict]:
    if as_of.utcoffset() is None or publisher not in {"ruida", "east_asia"}:
        raise ValueError("invalid publication context")
    now = as_of.astimezone(SHANGHAI)
    async with (
        asyncio.timeout(70),
        public_network.async_client(
            timeout=8, limits=httpx.Limits(max_connections=1, max_keepalive_connections=0)
        ) as client,
    ):
        if publisher == "east_asia":
            return await _read_east(client, now)
        editions = _rd_editions(await _get(client, RD_INDEX), now)
        for day, title, url in editions:
            rows = _rd_sections(await _get(client, url), day, title, url, now, headings=EXTENDED_HEADINGS)
            if rows:
                return rows
        return []
