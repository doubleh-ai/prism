"""Read official exchange warrant reports for covered futures products.

Inventory is published as a dated warrant report by each exchange. This module
normalizes the two covered official sources into one symbol-level observation:

* SHFE/INE daily warrant report (HTML since 2025-11-18, legacy JSON before it)
* CZCE warrant sheet (xlsx since 2025-11-03, legacy text sheet before it)
* DCE varieties through the public 99期货 warrant series, in the disclosed lot
  unit with the change labelled as a delta to the previous disclosed row

Products without an official covered source return ``inventory_unavailable``.
Quantities are never estimated, aggregated across uncovered warehouses or
filled with zero.
"""

from __future__ import annotations

import asyncio
import copy
import io
import json
import re
import time
import zipfile
from datetime import UTC, date, datetime, timedelta
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

from . import dce_alt, public_network
from .data_availability import source_failure
from .futures import PRODUCTS

SHANGHAI = ZoneInfo("Asia/Shanghai")

SHFE_HTML_SINCE = date(2025, 11, 18)
SHFE_JSON_SINCE = date(2016, 1, 1)
CZCE_TEXT_SINCE = date(2017, 1, 1)
CZCE_XLSX_SINCE = date(2025, 11, 3)

NOTICE = (
    "本页为交易所公开仓单日报快照；仅登记该品种当日披露的仓单数量与增减，"
    "不据仓单推断价格方向，也不代表全部库存。"
)

# Exact product segment titles. CU is the copper segment, never BC copper; RB is
# the separately reported warehouse segment, never the factory segment.
SHFE_SEGMENTS = {
    "CU": ("铜", "总计"),
    "AU": ("黄金", "上期所指定交割金库"),
    "RB": ("螺纹钢(仓库)", "总计"),
    "SC": ("中质含硫原油", "总计"),
    "AL": ("铝", "总计"),
    "ZN": ("锌", "总计"),
    "NI": ("镍", "总计"),
    "AG": ("白银", "总计"),
    "RU": ("天然橡胶", "总计"),
    "BU": ("石油沥青(仓库)", "总计"),
    "NR": ("20号胶(仓库)", "总计"),
    "LU": ("低硫燃料油(仓库)", "总计"),
    "SN": ("锡", "总计"),
    "AO": ("氧化铝(仓库)", "总计"),
    "HC": ("热轧卷板(仓库)", "总计"),
    "SS": ("不锈钢(仓库)", "总计"),
    "SP": ("纸浆(仓库)", "总计"),
}
SHFE_JSON_SEGMENTS = {
    "CU": ("铜", "总计"),
    "AU": ("黄金", None),
    "RB": ("螺纹钢", "总计"),
    "SC": ("中质含硫原油", "总计"),
    "AL": ("铝", "总计"),
    "ZN": ("锌", "总计"),
    "NI": ("镍", "总计"),
    "AG": ("白银", "总计"),
    "RU": ("天然橡胶", "总计"),
    "BU": ("石油沥青", "总计"),
    "NR": ("20号胶", "总计"),
    "LU": ("低硫燃料油", "总计"),
    "SN": ("锡", "总计"),
    "AO": ("氧化铝仓库", "总计"),
    "HC": ("热轧卷板仓库", "总计"),
    "SS": ("不锈钢仓库", "总计"),
    "SP": ("纸浆仓库", "总计"),
}
SHFE_UNITS = {
    "CU": "吨",
    "AU": "千克",
    "RB": "吨",
    "SC": "桶",
    "AL": "吨",
    "ZN": "吨",
    "NI": "吨",
    "AG": "千克",
    "RU": "吨",
    "BU": "吨",
    "NR": "吨",
    "LU": "吨",
    "SN": "吨",
    "AO": "吨",
    "HC": "吨",
    "SS": "吨",
    "SP": "吨",
}
SHFE_UNIT_CODES = {"1": "千克", "2": "吨", "3": "桶"}
CZCE_SEGMENTS = {
    "MA": "MA",
    "CF": "CF",
    "SR": "SR",
    "FG": "FG",
    "TA": "PTA",
    "RM": "RM",
    "OI": "OI",
    "PF": "PF",
    "CJ": "CJ",
    "PK": "PK",
    "SA": "SA",
    "SH": "SH",
    "UR": "UR",
}
# The legacy text sheet keys the PTA section by its trading code (TA); the
# current xlsx sheet keys it by the printed product name (PTA).
CZCE_LEGACY_SEGMENTS = {"TA": "TA"}
COVERED = (
    {symbol: "SHFE" for symbol in SHFE_SEGMENTS}
    | {symbol: "CZCE" for symbol in CZCE_SEGMENTS}
    | {symbol: "99QH" for symbol in dce_alt.PRODUCTS}
)

UNCOVERED_REASONS = {
    "IF": "中国金融期货交易所股指期货不设交易所仓单库存",
    "IC": "中国金融期货交易所股指期货不设交易所仓单库存",
    "T": "中国金融期货交易所国债期货不设交易所仓单库存",
    "TF": "中国金融期货交易所国债期货不设交易所仓单库存",
    "TS": "中国金融期货交易所国债期货不设交易所仓单库存",
    "SI": "广州期货交易所仓单日报尚未接入",
    "LC": "广州期货交易所仓单日报尚未接入",
    "PS": "广州期货交易所仓单日报尚未接入",
    "AP": "郑州商品交易所现行仓单日报表只披露苹果预报仓单，未披露实际仓单，无官方仓单库存数据源",
    **dict.fromkeys(
        ("JM", "CS", "LH", "JD", "PP", "V", "PG", "A", "B", "Y"), "该品种尚未接入可核验的大商所仓单数据"
    ),
    "EC": "集运指数（欧线）采用现金交割，不适用实物仓单。",
}

FAILURE_REASONS = {
    "report_not_found": "该日期交易所未发布仓单日报（非交易日或尚未发布）",
    "access_denied": "交易所拒绝自动化读取仓单日报",
    "source_timeout": "读取交易所仓单日报超时",
    "network_error": "连接交易所仓单日报失败",
    "http_error": "交易所仓单日报返回异常状态",
    "invalid_source_data": "交易所仓单日报结构无法识别",
    "source_not_available": "该日期不在已接入的仓单日报覆盖范围内",
    "source_access_denied": "公开仓单数据源拒绝了自动化读取请求",
}

_SOURCE_NAMES = {"SHFE": "上海期货交易所", "CZCE": "郑州商品交易所", "99QH": dce_alt.SOURCE_NAME}
_CACHE_TTL = 300
_CACHE_PARTIAL_TTL = 30
_STATIC_TTL = 3600
_STREAM_LIMIT = 2 * 1024 * 1024
_LOOKBACK_DAYS = 8
_SOURCE_SYMBOLS = {symbol: exchange for symbol, exchange in COVERED.items()}
_CACHE: dict[tuple[str, str], tuple[float, int, dict]] = {}
_SLOTS = asyncio.Semaphore(2)


def shfe_report_url(day: date) -> str:
    """Return the published SHFE warrant report for one calendar day."""
    if day >= SHFE_HTML_SINCE:
        return f"https://www.shfe.com.cn/data/tradedata/future/stockdata/dailystock_{day:%Y%m%d}/ZH/all.html"
    return f"https://www.shfe.com.cn/data/tradedata/future/dailydata/{day:%Y%m%d}dailystock.dat"


def czce_report_url(day: date) -> str:
    """Return the published CZCE warrant sheet for one calendar day."""
    if day >= CZCE_XLSX_SINCE:
        name = "FutureDataWhsheet.xlsx"
    else:
        name = "FutureDataWhsheet.txt"
    return f"https://www.czce.com.cn/cn/DFSStaticFiles/Future/{day:%Y}/{day:%Y%m%d}/{name}"


def _cells(row) -> list[str]:
    return [cell.get_text(strip=True) for cell in row.find_all(["td", "th"], recursive=False)]


def _pair(quantity, change, *, where: str) -> tuple[int, int]:
    if not isinstance(quantity, str) or not re.fullmatch(r"[0-9]{1,15}", quantity):
        raise ValueError(f"unrecognized warrant quantity in {where}")
    if not isinstance(change, str) or not re.fullmatch(r"[+-]?[0-9]{1,15}", change):
        raise ValueError(f"unrecognized warrant change in {where}")
    return int(quantity), int(change)


def parse_shfe(raw: bytes, day: date) -> dict:
    """Parse the current SHFE/INE HTML warrant report into dated segments."""
    page = BeautifulSoup(raw.decode("utf-8"), "html.parser")
    datelines = page.select("#stock > table.content_intro tr:first-child td")
    if len(datelines) != 1 or datelines[0].get_text(strip=True).split()[:1] != [day.isoformat()]:
        raise ValueError("report date mismatch")
    segments: dict[str, dict] = {}
    for table in page.select("#stock > table.el-table_table"):
        rows = [_cells(row) for row in table.select("tr")]
        if len(rows) < 2 or len(rows[0]) != 4 or rows[0][3] != "增减":
            raise ValueError("unrecognized report columns")
        heading, unit_cell = rows[1]
        if not unit_cell.startswith("单位："):
            raise ValueError("unrecognized report heading")
        unit = unit_cell[len("单位：") :]
        totals: dict[str, tuple[int, int]] = {}
        reported: dict[str, tuple[int, int]] = {}
        for row in rows[2:]:
            if len(row) != 3 or not row[0]:
                continue
            if row[0].endswith("总计") or row[0] == "合计":
                totals[row[0]] = _pair(row[1], row[2], where=f"SHFE {heading}")
            else:
                # Gold reports a single vault row instead of region totals.
                reported.setdefault(row[0], _pair(row[1], row[2], where=f"SHFE {heading}"))
        if heading in segments:
            raise ValueError("ambiguous report segment")
        segments[heading] = {"title": heading, "unit": unit, "totals": totals, "rows": reported}
    if not segments:
        raise ValueError("missing report tables")
    return {"data_as_of": day.isoformat(), "segments": segments}


def _json_number(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]{1,15}", value.strip()):
        return int(value.strip())
    return None


def parse_shfe_json(raw: bytes, day: date) -> dict:
    """Parse the legacy SHFE/INE JSON warrant report into dated segments."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("unrecognized report payload") from None
    if not isinstance(payload, dict):
        raise ValueError("unrecognized report payload")
    if str(payload.get("o_tradingday") or "") != f"{day:%Y%m%d}":
        raise ValueError("report date mismatch")
    rows = payload.get("o_cursor")
    if not isinstance(rows, list) or not rows:
        raise ValueError("missing report rows")
    segments: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("unrecognized report row")
        name = str(row.get("VARNAME") or "").split("$$")[0].strip()
        label = str(row.get("WHABBRNAME") or "").split("$$")[0].strip()
        region = str(row.get("REGNAME") or "").split("$$")[0].strip()
        unit = SHFE_UNIT_CODES.get(str(row.get("WGHTUNIT") or ""))
        quantity = _json_number(row.get("WRTWGHTS"))
        change = _json_number(row.get("WRTCHANGE"))
        if not name or not label or unit is None or quantity is None or change is None:
            raise ValueError("unrecognized report row")
        segment = segments.setdefault(name, {"title": name, "unit": unit, "totals": {}, "rows": {}})
        if segment["unit"] != unit:
            raise ValueError("mixed report units")
        if not region and label.endswith("总计"):
            segment["totals"].setdefault(label, (quantity, change))
        elif label != "合计":
            segment["rows"].setdefault(label, (quantity, change))
    if not segments:
        raise ValueError("missing report segments")
    return {"data_as_of": day.isoformat(), "segments": segments}


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _xml(raw: bytes) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        raise ValueError("unrecognized xlsx worksheet") from None


def _children(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [child for child in element if _local(child.tag) == name]


def _column(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference.upper())
    if letters is None:
        raise ValueError("unrecognized xlsx cell")
    value = 0
    for character in letters.group():
        value = value * 26 + ord(character) - 64
    return value - 1


def _shared_strings(archive: zipfile.ZipFile, names: set[str]) -> list[str]:
    if "xl/sharedStrings.xml" not in names:
        return []
    root = _xml(archive.read("xl/sharedStrings.xml"))
    return [
        "".join(node.text or "" for node in item.iter() if _local(node.tag) == "t")
        for item in _children(root, "si")
    ]


def _sheet_member(archive: zipfile.ZipFile, names: set[str]) -> str:
    try:
        workbook = _xml(archive.read("xl/workbook.xml"))
        relationships = _xml(archive.read("xl/_rels/workbook.xml.rels"))
    except KeyError:
        raise ValueError("missing xlsx workbook") from None
    sheets = _children(workbook, "sheets")
    sheet = _children(sheets[0], "sheet")[0] if sheets and _children(sheets[0], "sheet") else None
    target = None
    if sheet is not None:
        relationship_id = next((value for key, value in sheet.attrib.items() if _local(key) == "id"), None)
        for relationship in _children(relationships, "Relationship"):
            if relationship.get("Id") == relationship_id:
                target = relationship.get("Target")
                break
    if target:
        member = target.lstrip("/")
        member = member if member.startswith("xl/") else "xl/" + member
        if member in names:
            return member
    candidates = sorted(name for name in names if name.startswith("xl/worksheets/") and name.endswith(".xml"))
    if len(candidates) == 1:
        return candidates[0]
    raise ValueError("missing xlsx worksheet")


def _xlsx_rows(raw: bytes) -> list[list[str]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ValueError("unrecognized xlsx report") from None
    with archive:
        names = set(archive.namelist())
        shared = _shared_strings(archive, names)
        root = _xml(archive.read(_sheet_member(archive, names)))
    sheet_data = next(iter(_children(root, "sheetData")), None)
    if sheet_data is None:
        raise ValueError("missing xlsx worksheet rows")
    rows: list[list[str]] = []
    for row in _children(sheet_data, "row"):
        values: dict[int, str] = {}
        for cell in _children(row, "c"):
            kind = cell.get("t") or "n"
            if kind == "inlineStr":
                text = "".join(node.text or "" for node in cell.iter() if _local(node.tag) == "t")
            else:
                nodes = _children(cell, "v")
                text = "" if not nodes or nodes[0].text is None else nodes[0].text
                if kind == "s":
                    try:
                        text = shared[int(text)]
                    except (ValueError, IndexError):
                        raise ValueError("unrecognized xlsx shared string") from None
            values[_column(cell.get("r") or "")] = text.strip()
        if values:
            rows.append([values.get(index, "") for index in range(max(values) + 1)])
    return rows


def _text_rows(raw: bytes) -> list[list[str]]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gb18030")
        except UnicodeDecodeError:
            raise ValueError("unrecognized text encoding") from None
    rows: list[list[str]] = []
    for line in text.splitlines():
        if "|" in line:
            cells = [cell.strip() for cell in line.split("|")]
            while cells and not cells[-1]:
                cells.pop()
            if any(cells):
                rows.append(cells)
        else:
            value = line.strip().replace("\t", "").strip()
            if value:
                rows.append([value])
    return rows


def _czce_meta(line: str) -> tuple[str, str, str, str]:
    match = re.match(r"品种[:：]\s*(?P<title>\S+)\s+单位[:：]\s*(?P<unit>\S+)", line)
    if match is None:
        raise ValueError("unrecognized CZCE section header")
    title = match.group("title")
    code = re.search(r"[A-Za-z]{1,4}$", title)
    if code is None:
        raise ValueError("unrecognized CZCE product code")
    published = re.search(r"日期[:：]\s*(\d{4}-\d{2}-\d{2})", line)
    if published is None:
        raise ValueError("unrecognized CZCE section date")
    name = title[: code.start()] or code.group()
    return name, code.group().upper(), match.group("unit"), published.group(1)


def _czce_number(row: list[str], index: int) -> int:
    cell = row[index] if index < len(row) else ""
    if not re.fullmatch(r"-?[0-9]{1,15}(?:\.0+)?", cell):
        raise ValueError("unrecognized CZCE warrant quantity")
    return int(cell.split(".")[0])


def _parse_czce_rows(rows: list[list[str]], day: date) -> dict:
    if not rows:
        raise ValueError("missing report")
    title_row = next((row for row in rows if row and "仓单日报表" in row[0]), None)
    if title_row is None or day.isoformat() not in title_row[0]:
        raise ValueError("report date mismatch")
    segments: dict[str, dict] = {}
    forecasts: dict[str, dict] = {}
    index = 0
    while index < len(rows):
        row = rows[index]
        if not row or not row[0].startswith("品种："):
            index += 1
            continue
        name, code, unit, published = _czce_meta(row[0])
        if published != day.isoformat():
            raise ValueError("report date mismatch")
        header = None
        cursor = index + 1
        while cursor < len(rows) and cursor <= index + 4:
            candidate = rows[cursor]
            if candidate and candidate[0].endswith("编号"):
                header = candidate
                break
            if candidate and candidate[0].startswith("品种："):
                break
            cursor += 1
        if header is None:
            raise ValueError("missing report header")
        forecast = "每张预报" in row[0] or any(name.startswith("预报数量") for name in header)
        quantity_columns = [
            column
            for column, name in enumerate(header)
            if name.startswith("仓单数量") or name == "确认书数量"
        ]
        change_columns = [column for column, name in enumerate(header) if name == "当日增减"]
        if forecast:
            quantity_columns = [column for column, label in enumerate(header) if label == "预报数量"]
            if code != "AP" or unit != "张" or len(quantity_columns) != 1 or len(change_columns) != 1:
                index = cursor + 1
                continue
        if not quantity_columns or len(change_columns) != 1:
            raise ValueError("unrecognized report columns")
        total = None
        cursor += 1
        while cursor < len(rows):
            candidate = rows[cursor]
            if candidate and candidate[0].startswith("品种："):
                break
            if candidate and candidate[0] == "总计":
                total = candidate
                break
            cursor += 1
        if total is None and forecast:
            index = cursor
            continue
        if total is None:
            raise ValueError("missing report total")
        quantity = sum(_czce_number(total, column) for column in quantity_columns)
        observations = forecasts if forecast else segments
        observations[code] = {
            "title": row[0],
            "name": name,
            "unit": unit,
            "quantity": quantity,
            "change": _czce_number(total, change_columns[0]),
            "basis": "总计",
        }
        index = cursor + 1
    if not segments:
        raise ValueError("missing report sections")
    return {"data_as_of": day.isoformat(), "segments": segments, "forecasts": forecasts}


def parse_czce_xlsx(raw: bytes, day: date) -> dict:
    """Parse the current CZCE xlsx warrant sheet into dated product sections."""
    return _parse_czce_rows(_xlsx_rows(raw), day)


def parse_czce_text(raw: bytes, day: date) -> dict:
    """Parse the legacy CZCE text warrant sheet into dated product sections."""
    return _parse_czce_rows(_text_rows(raw), day)


async def _fetch(client, url: str, deadline: float) -> bytes:
    raw = bytearray()
    client.cookies.clear()
    async with asyncio.timeout_at(deadline):
        async with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
            response.raise_for_status()
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ValueError("unexpected encoding")
            async for chunk in response.aiter_bytes():
                if len(raw) + len(chunk) > _STREAM_LIMIT:
                    raise ValueError("report size")
                raw.extend(chunk)
    return bytes(raw)


def _no_report(day: date, url: str) -> dict:
    return {
        "status": "inventory_unavailable",
        "requested_date": day.isoformat(),
        "source_url": url,
        "error_code": "report_not_found",
    }


def _failure(day: date, url: str, error: BaseException) -> dict:
    return {
        "status": "inventory_unavailable",
        "requested_date": day.isoformat(),
        "source_url": url,
        **source_failure(error),
    }


def _shfe_segment(parsed: dict, symbol: str) -> tuple[dict, str, tuple[int, int]]:
    titles = [SHFE_SEGMENTS[symbol][0]]
    labels = [SHFE_SEGMENTS[symbol][1]]
    if symbol in SHFE_JSON_SEGMENTS:
        prefix, legacy_label = SHFE_JSON_SEGMENTS[symbol]
        titles.extend([prefix, prefix + "仓库"])
        if legacy_label:
            labels.append(legacy_label)
    segment = next((parsed["segments"][name] for name in titles if name in parsed["segments"]), None)
    if segment is None:
        raise ValueError("missing report segment")
    for label in labels:
        pair = segment["totals"].get(label) or segment["rows"].get(label)
        if pair is not None:
            return segment, label, pair
    # Gold publishes one vault row instead of a region total.
    if symbol == "AU" and len(segment["rows"]) == 1:
        label, pair = next(iter(segment["rows"].items()))
        return segment, label, pair
    raise ValueError("missing report total")


async def _shfe_report(client, day, deadline):
    raw = await _fetch(client, shfe_report_url(day), deadline)
    parser = parse_shfe if day >= SHFE_HTML_SINCE else parse_shfe_json
    return await asyncio.to_thread(parser, raw, day)


def _shfe_observation(parsed, symbol, day):
    segment, basis, (quantity, change) = _shfe_segment(parsed, symbol)
    unit = segment["unit"]
    if unit != SHFE_UNITS[symbol]:
        raise ValueError("report unit mismatch")
    return {
        "status": "collected",
        "data_as_of": parsed["data_as_of"],
        "source_name": _SOURCE_NAMES["SHFE"],
        "source_url": shfe_report_url(day),
        "segment": segment["title"],
        "basis": basis,
        "unit": unit,
        "quantity": quantity,
        "change": change,
    }


async def read_shfe(client, symbol: str, day: date, deadline: float) -> dict:
    try:
        parsed = await _shfe_report(client, day, deadline)
        return _shfe_observation(parsed, symbol, day)
    except (ValueError, httpx.HTTPError, TimeoutError, RecursionError) as error:
        return _failure(day, shfe_report_url(day), error)


async def _czce_report(client, day, deadline):
    if day < CZCE_TEXT_SINCE:
        return None
    raw = await _fetch(client, czce_report_url(day), deadline)
    if day >= CZCE_XLSX_SINCE and not raw.startswith(b"PK"):
        return None
    parser = parse_czce_xlsx if day >= CZCE_XLSX_SINCE else parse_czce_text
    return await asyncio.to_thread(parser, raw, day)


def _czce_observation(parsed, symbol, day):
    segment_key = CZCE_SEGMENTS[symbol]
    if day < CZCE_XLSX_SINCE:
        segment_key = CZCE_LEGACY_SEGMENTS.get(symbol, segment_key)
    segment = parsed["segments"].get(segment_key)
    if segment is None:
        raise ValueError("missing report section")
    return {
        "status": "collected",
        "data_as_of": parsed["data_as_of"],
        "source_name": _SOURCE_NAMES["CZCE"],
        "source_url": czce_report_url(day),
        "segment": segment["name"],
        "basis": segment["basis"],
        "unit": segment["unit"],
        "quantity": segment["quantity"],
        "change": segment["change"],
    }


def _apple_forecast(parsed, day):
    segment = parsed["forecasts"].get("AP")
    if not segment:
        raise ValueError("missing apple warrant forecast")
    return {
        "status": "collected",
        "data_as_of": parsed["data_as_of"],
        "source_name": _SOURCE_NAMES["CZCE"],
        "source_url": czce_report_url(day),
        "label": "预报仓单",
        "value": segment["quantity"],
        "unit": segment["unit"],
        "scope": "郑商所苹果预报仓单，含当日披露的预报数量。实际注册仓单为另一口径。",
    }


async def read_czce(client, symbol: str, day: date, deadline: float) -> dict:
    url = czce_report_url(day)
    try:
        parsed = await _czce_report(client, day, deadline)
        return _czce_observation(parsed, symbol, day) if parsed else _no_report(day, url)
    except (ValueError, httpx.HTTPError, TimeoutError, RecursionError) as error:
        return _failure(day, url, error)


async def latest_exchange_tables(symbols, today):
    """Read a shared report for each of the two exchanges that publish one table."""

    async def scan(covered, load, extract, report_url):
        requested = [symbol for symbol in symbols if symbol in covered]
        if not requested:
            return {}
        loop = asyncio.get_running_loop()
        try:
            async with asyncio.timeout(15), _SLOTS, public_network.async_client(timeout=8) as client:
                for offset in range(_LOOKBACK_DAYS):
                    day = today - timedelta(days=offset)
                    try:
                        parsed = await load(client, day, loop.time() + 6)
                    except (ValueError, httpx.HTTPError, TimeoutError, RecursionError) as error:
                        if _failure(day, report_url(day), error)["error_code"] != "report_not_found":
                            return {}
                        continue
                    if parsed:
                        results = {}
                        for symbol in requested:
                            try:
                                results[symbol] = extract(parsed, symbol, day)
                            except ValueError:
                                continue
                        return results
        except (TimeoutError, httpx.HTTPError):
            pass
        return {}

    async with asyncio.TaskGroup() as reports:
        shfe = reports.create_task(scan(SHFE_SEGMENTS, _shfe_report, _shfe_observation, shfe_report_url))

        def czce_observation(parsed, symbol, day):
            return _apple_forecast(parsed, day) if symbol == "AP" else _czce_observation(parsed, symbol, day)

        czce = reports.create_task(
            scan((*CZCE_SEGMENTS, "AP"), _czce_report, czce_observation, czce_report_url)
        )
    return {**shfe.result(), **czce.result()}


async def read_inventory(client, symbol: str, day: date, deadline: float) -> dict:
    """Read one dated warrant report for a covered product."""
    source = _SOURCE_SYMBOLS.get(symbol)
    if source == "SHFE":
        return await read_shfe(client, symbol, day, deadline)
    if source == "CZCE":
        return await read_czce(client, symbol, day, deadline)
    if source == "99QH":
        return await dce_alt.read_inventory(client, symbol, day, deadline)
    return _no_report(day, "")


def _identity(symbol: str) -> dict:
    name, exchange = PRODUCTS[symbol][0], PRODUCTS[symbol][1]
    return {"symbol": symbol, "name": name, "exchange": exchange}


def _requested_day(observed: str | None) -> date | None:
    if observed is None:
        return None
    if not isinstance(observed, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", observed) is None:
        raise ValueError("日期格式应为 YYYY-MM-DD")
    try:
        return date.fromisoformat(observed)
    except ValueError:
        raise ValueError("日期格式应为 YYYY-MM-DD") from None


def _today() -> date:
    return datetime.now(SHANGHAI).date()


def _remember(key: tuple[str, str], ttl: int, value: dict) -> None:
    _CACHE[key] = (time.monotonic(), ttl, copy.deepcopy(value))


async def snapshot(symbol: str, observed: str | None = None) -> dict:
    """Read the latest or requested dated warrant report for one product.

    ``observed`` is an optional ``YYYY-MM-DD`` data date. Without it the newest
    report inside a bounded calendar window is used. Uncovered products and
    missing reports return ``inventory_unavailable`` without any estimate.
    """
    if symbol not in PRODUCTS:
        raise ValueError("请选择支持的期货品种")
    day = _requested_day(observed)
    key = (symbol, observed or "")
    cached = _CACHE.get(key)
    if cached is not None:
        stamp, ttl, value = cached
        if time.monotonic() - stamp < ttl:
            return {**copy.deepcopy(value), "cached": True}
        _CACHE.pop(key, None)
    date_basis = "requested" if day is not None else "bounded_calendar_window"
    if symbol not in COVERED:
        value = {
            **_identity(symbol),
            "status": "inventory_unavailable",
            "requested_date": observed,
            "date_basis": date_basis,
            "reason": UNCOVERED_REASONS[symbol],
            "source_attempts": [],
            "gaps": ["该品种未接入交易所仓单数据源；不估算、不补零"],
            "cache_ttl_seconds": _STATIC_TTL,
            "cached": False,
        }
        _remember(key, _STATIC_TTL, value)
        return value
    source = _SOURCE_SYMBOLS[symbol]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 15
    today = _today()
    candidates = (
        [day] if day is not None else [today - timedelta(days=offset) for offset in range(_LOOKBACK_DAYS)]
    )
    attempts: list[dict] = []
    observation = None
    try:
        async with asyncio.timeout_at(deadline):
            async with _SLOTS, public_network.async_client(timeout=8) as client:
                for candidate in candidates:
                    if loop.time() >= deadline:
                        break
                    result = await read_inventory(client, symbol, candidate, min(deadline, loop.time() + 6))
                    attempts.append(
                        {
                            "source": _SOURCE_NAMES[source],
                            "requested_date": candidate.isoformat(),
                            "status": result.get("status"),
                            "error_code": result.get("error_code"),
                            "data_as_of": result.get("data_as_of"),
                        }
                    )
                    if result.get("status") == "collected":
                        observation = result
                        break
                    if result.get("error_code") != "report_not_found":
                        break
    except (TimeoutError, httpx.HTTPError) as error:
        attempts.append(
            {
                "source": _SOURCE_NAMES[source],
                "requested_date": None,
                "status": "inventory_unavailable",
                **source_failure(error),
            }
        )
    if observation is None:
        error_code = next(
            (
                attempt["error_code"]
                for attempt in reversed(attempts)
                if attempt.get("error_code") not in (None, "report_not_found")
            ),
            "report_not_found",
        )
        value = {
            **_identity(symbol),
            "status": "inventory_unavailable",
            "requested_date": observed,
            "date_basis": date_basis,
            "error_code": error_code,
            "reason": FAILURE_REASONS.get(error_code, FAILURE_REASONS["invalid_source_data"]),
            "source_attempts": attempts,
            "gaps": ["未取得交易所仓单日报；不估算、不补零"],
            "cache_ttl_seconds": _CACHE_PARTIAL_TTL,
            "cached": False,
        }
        _remember(key, _CACHE_PARTIAL_TTL, value)
        return value
    value = {
        **_identity(symbol),
        "status": "collected",
        "requested_date": observed,
        "date_basis": date_basis,
        "data_as_of": observation["data_as_of"],
        "unit": observation["unit"],
        "quantity": observation["quantity"],
        "change": observation["change"],
        "basis": observation["basis"],
        "segment": observation["segment"],
        "label": observation.get("label"),
        "notice": observation.get("notice"),
        "change_basis": observation.get("change_basis"),
        "source": {
            "name": observation["source_name"],
            "url": observation["source_url"],
            "kind": observation.get("source_kind") or "exchange_warehouse_report",
        },
        "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source_attempts": attempts,
        "gaps": [],
        "cache_ttl_seconds": _CACHE_TTL,
        "cached": False,
    }
    _remember(key, _CACHE_TTL, value)
    return value


def _evidence_text(observation: dict, source_name: str, label: str, change_label: str) -> str:
    name, symbol, exchange = observation["name"], observation["symbol"], observation["exchange"]
    notice = observation.get("notice") if isinstance(observation.get("notice"), str) else NOTICE
    return "\n".join(
        [
            f"数据来源：{source_name}｜数据日期：{observation['data_as_of']}｜品种：{name} {symbol}（{exchange}）",
            "",
            f"【{label}】",
            f"仓单数量 {observation['quantity']} {observation['unit']}",
            f"{change_label} {observation['change']} {observation['unit']}",
            "",
            notice,
        ]
    )


def snapshot_document(observation: dict):
    """Render one collected inventory observation as an ingestable source document."""
    if not isinstance(observation, dict) or observation.get("status") != "collected":
        return None
    symbol = observation.get("symbol")
    if not isinstance(symbol, str) or symbol not in PRODUCTS:
        return None
    source = observation.get("source")
    if not isinstance(source, dict):
        return None
    source_name = source.get("name")
    url = source.get("url")
    if (
        not isinstance(source_name, str)
        or not source_name
        or not isinstance(url, str)
        or not url.startswith(("http://", "https://"))
    ):
        return None
    try:
        period = date.fromisoformat(str(observation.get("data_as_of")))
    except ValueError:
        return None
    quantity = observation.get("quantity")
    change = observation.get("change")
    unit = observation.get("unit")
    if (
        not isinstance(quantity, int)
        or isinstance(quantity, bool)
        or not isinstance(change, int)
        or isinstance(change, bool)
        or not isinstance(unit, str)
        or not unit
    ):
        return None
    name, exchange = PRODUCTS[symbol][0], PRODUCTS[symbol][1]
    basis = observation.get("basis")
    derived = observation.get("change_basis") == "delta_vs_previous_disclosed_row"
    if source.get("kind") == dce_alt.SOURCE_KIND:
        label = (
            observation.get("label") if isinstance(observation.get("label"), str) else "仓单库存（公开序列）"
        )
        title = f"{name} {symbol} 仓单库存 {period.isoformat()}"
        change_label = "仓单增减（较上一披露日）" if derived else "仓单增减"
    else:
        label = f"仓单日报（{basis}）" if isinstance(basis, str) and basis else "仓单日报"
        title = f"{name} {symbol} 仓单日报 {period.isoformat()}"
        change_label = "仓单增减"
    text = _evidence_text(observation, source_name, label, change_label)
    section = {
        "page": 1,
        "section": "warehouse_receipt",
        "label": label,
        "source": source_name,
        "url": url,
        "data_period": period.isoformat(),
        "symbol": symbol,
        "name": name,
    }
    return {
        "symbol": symbol,
        "name": name,
        "exchange": exchange,
        "title": title,
        "url": url,
        "publisher": source_name,
        "retrieved_at": str(observation.get("retrieved_at") or ""),
        "data_period": period.isoformat(),
        "status": observation["status"],
        "tradable_contract": False,
        "source": {
            "name": source_name,
            "url": url,
            "kind": source.get("kind")
            if isinstance(source.get("kind"), str)
            else "exchange_warehouse_report",
        },
        "sections": [section],
        "pages": [{"page": 1, "text": text}],
    }
