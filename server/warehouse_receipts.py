"""Read a single dated SHFE warehouse report. Only publish exchange-labelled totals."""

import asyncio
import re
from datetime import date

import httpx
from bs4 import BeautifulSoup

from .data_availability import source_failure

# Match the published table and row exactly. In particular, CU excludes BC copper
# and the RB warehouse table excludes the separately reported factory warrants.
TABLES = {
    "CU": ("铜", "吨", "仓库", "期货", "总计"),
    "AU": ("黄金", "千克", "仓库", "期货", "上期所指定交割金库"),
    "RB": ("螺纹钢(仓库)", "吨", "仓库", "期货", "总计"),
    "SC": ("中质含硫原油", "桶", "交割仓库", "本日数量", "总计"),
}


def report_url(day: date):
    return f"https://www.shfe.com.cn/data/tradedata/future/stockdata/dailystock_{day:%Y%m%d}/ZH/all.html"


def parse_report(raw: bytes, symbol: str, day: date):
    title, unit, location, measure, label = TABLES[symbol]
    page = BeautifulSoup(raw.decode("utf-8"), "html.parser")
    datelines = page.select("#stock > table.content_intro tr:first-child td")
    if len(datelines) != 1 or datelines[0].get_text(strip=True).split()[:1] != [day.isoformat()]:
        raise ValueError("report date mismatch")

    def cells(row):
        return [cell.get_text(strip=True) for cell in row.find_all(["td", "th"], recursive=False)]

    matches = []
    for table in page.select("#stock > table.el-table_table"):
        heading = table.select("tr.special_row_type")
        if len(heading) == 1 and cells(heading[0]) == [title, "单位：" + unit]:
            matches.append(table)
    if len(matches) != 1:
        raise ValueError("missing or ambiguous report table")
    table = matches[0]
    if [cell.get_text(strip=True) for cell in table.select("thead th")] != [
        "地区",
        location,
        measure,
        "增减",
    ]:
        raise ValueError("unrecognized report columns")
    rows = [cells(row) for row in table.select("tr")]
    totals = [row for row in rows if row[:1] == [label]]
    if len(totals) != 1 or len(totals[0]) != 3:
        raise ValueError("missing or ambiguous report total")
    _, quantity, change = totals[0]
    if not re.fullmatch(r"[0-9]{1,15}", quantity) or not re.fullmatch(r"[+-]?[0-9]{1,15}", change):
        raise ValueError("unrecognized warrant quantity")
    return {
        "status": "collected",
        "data_as_of": day.isoformat(),
        "source_url": report_url(day),
        "table": title,
        "row": label,
        "unit": unit,
        "quantity": int(quantity),
        "change": int(change),
    }


async def read_report(client, symbol, day, deadline):
    if symbol not in TABLES:
        return {"status": "not_covered"}
    try:
        async with asyncio.timeout_at(deadline):
            raw = bytearray()
            client.cookies.clear()
            async with client.stream(
                "GET", report_url(day), headers={"Accept-Encoding": "identity"}
            ) as response:
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ValueError("unexpected encoding")
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > 512 * 1024:
                        raise ValueError("report size")
                    raw.extend(chunk)
            return await asyncio.to_thread(parse_report, bytes(raw), symbol, day)
    except (ValueError, httpx.HTTPError, TimeoutError, RecursionError) as error:
        return {
            "status": "unavailable",
            "requested_date": day.isoformat(),
            "source_url": report_url(day),
            **source_failure(error),
        }
