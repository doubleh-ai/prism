"""Read the daily spot-versus-futures price table published by 100ppi (生意社).

The public table at ``https://www.100ppi.com/sf/day-<date>.html`` lists, for
each commodity and one calendar day, the spot quote and the near and main
futures contract quotes. This module maps the covered commodities to our
futures symbols and returns the spot price, the main-contract futures price and
the basis (``spot - main futures price``).

The first request may be answered with a ``HW_CHECK`` check page whose inline
script carries the cookie value. The value is extracted from that script,
stored as a cookie and the same address is requested again. When the check page
cannot be solved, when the table no longer lists the commodity, or when the
published difference disagrees with the two quotes, the tool returns
``basis_unavailable`` with a reason code. Prices are never estimated, converted
between quoted units, or substituted from another date.
"""

from __future__ import annotations

import asyncio
import copy
import math
import re
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

from . import futures, public_network
from .data_availability import source_failure

SHANGHAI = ZoneInfo("Asia/Shanghai")

SOURCE_NAME = "生意社现期表"
SOURCE_KIND = "spot_futures_price_table"
HOST = "www.100ppi.com"
CHALLENGE_COOKIE = "HW_CHECK"

TOTAL_SECONDS = 15
REQUEST_SECONDS = 6
CLIENT_TIMEOUT = 8

NOTICE = (
    "本页为生意社现期表公开数据快照；现货价为生意社现货均价，不是交易所结算价或"
    "交割价；基差为同日现货均价减主力期货价，来源披露的现期差仅用于核对。"
    "未做估算、单位换算或用其他日期替代。"
)

# Mapped table rows, keyed by our futures symbol. The value is the exchange
# group title and the product name as printed by the source, after stripping a
# trailing ASCII contract suffix such as ``甲醇MA``.
BASIS_PRODUCTS = {
    "CU": ("上海期货交易所", "铜"),
    "RB": ("上海期货交易所", "螺纹钢"),
    "AU": ("上海期货交易所", "黄金"),
    "AL": ("上海期货交易所", "铝"),
    "ZN": ("上海期货交易所", "锌"),
    "NI": ("上海期货交易所", "镍"),
    "AG": ("上海期货交易所", "白银"),
    "RU": ("上海期货交易所", "天然橡胶"),
    "BU": ("上海期货交易所", "石油沥青"),
    "M": ("大连商品交易所", "豆粕"),
    "C": ("大连商品交易所", "玉米"),
    "I": ("大连商品交易所", "铁矿石"),
    "P": ("大连商品交易所", "棕榈油"),
    "MA": ("郑州商品交易所", "甲醇"),
    "CF": ("郑州商品交易所", "棉花"),
    "SR": ("郑州商品交易所", "白糖"),
    "FG": ("郑州商品交易所", "玻璃"),
    "TA": ("郑州商品交易所", "PTA"),
    "RM": ("郑州商品交易所", "菜籽粕"),
    "OI": ("郑州商品交易所", "菜籽油"),
    "PF": ("郑州商品交易所", "涤纶短纤"),
    "SI": ("广州期货交易所", "工业硅"),
    "LC": ("广州期货交易所", "碳酸锂"),
    "PS": ("广州期货交易所", "多晶硅"),
    "SN": ("上海期货交易所", "锡"),
    "HC": ("上海期货交易所", "热轧卷板"),
    "SS": ("上海期货交易所", "不锈钢"),
    "SP": ("上海期货交易所", "纸浆"),
    "UR": ("郑州商品交易所", "尿素"),
    "SA": ("郑州商品交易所", "纯碱"),
    "JM": ("大连商品交易所", "焦煤"),
    "PP": ("大连商品交易所", "聚丙烯"),
    "V": ("大连商品交易所", "聚氯乙烯"),
    "A": ("大连商品交易所", "豆一"),
    "Y": ("大连商品交易所", "豆油"),
    "PG": ("大连商品交易所", "液化石油气"),
}

UNCOVERED_REASONS = {
    "SC": "生意社现期表不含上海国际能源交易中心品种，原油基差数据源尚未接入",
    "NR": "生意社现期表不含上海国际能源交易中心品种，20号胶基差数据源尚未接入",
    "LU": "生意社现期表不含上海国际能源交易中心品种，低硫燃料油基差数据源尚未接入",
    "IF": "中国金融期货交易所股指期货不在生意社现期表中",
    "IC": "中国金融期货交易所股指期货不在生意社现期表中",
    "T": "中国金融期货交易所国债期货不在生意社现期表中",
    "TF": "中国金融期货交易所国债期货不在生意社现期表中",
    "TS": "中国金融期货交易所国债期货不在生意社现期表中",
    "AP": "生意社现期表未列出苹果品种，苹果基差数据源尚未接入",
    "AO": "氧化铝尚无已核对的同品种现期报价",
    "CS": "玉米淀粉尚无已核对的同品种现期报价",
    "B": "豆二尚无已核对的同品种现期报价",
    "CJ": "红枣尚无已核对的同品种现期报价",
    "PK": "花生尚无已核对的同品种现期报价",
    "JD": "来源现货按元/公斤、期货按元/500千克披露；尚未接入同单位鸡蛋基差",
    "LH": "生猪现货与期货的重量单位需核对",
    "SH": "来源现货为32%液碱，期货为折百价；尚未接入相同浓度口径的烧碱基差",
    "EC": "集运指数（欧线）以运价指数现金交割，未接入对应的现货指数比较。",
}

FAILURE_REASONS = {
    "report_not_found": "该日期生意社未发布现期表数据（非交易日或尚未发布）",
    "access_denied": "生意社拒绝自动化读取现期表",
    "source_timeout": "读取生意社现期表超时",
    "network_error": "连接生意社现期表失败",
    "http_error": "生意社现期表返回异常状态",
    "invalid_source_data": "生意社现期表结构或数值无法识别",
    "challenge_unsolved": "未能通过生意社 HW_CHECK 安全检查，未读取现期表",
    "source_price_mismatch": "现期表现货价与主力期货价之差与其披露的现期差不一致，未计算基差",
    "product_not_listed": "该日期的生意社现期表未列出该品种",
    "symbol_not_covered": "该品种不在生意社现期表映射范围内",
}

_CACHE_TTL = 300
_CACHE_PARTIAL_TTL = 30
_STATIC_TTL = 3600
_STREAM_LIMIT = 2 * 1024 * 1024
_LOOKBACK_DAYS = 8
_CACHE: dict[tuple[str, str], tuple[float, int, dict]] = {}
_SLOTS = asyncio.Semaphore(2)

_TITLE_DATE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
_TOKEN = re.compile(r'var\s+_0x[0-9a-zA-Z]+\s*=\s*"([0-9a-fA-F]{16,64})"')
_LITERAL_COOKIE = re.compile(r'document\.cookie\s*=\s*"HW_CHECK=([0-9a-fA-F]{16,64})')
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_CONTRACT = re.compile(r"^\d{3,4}$|^[A-Za-z]{1,3}\d{3,4}$")
_GROUP_COLUMNS = 8
_HEADER_MAIN = ["商品", "现货", "最近合约", "主力合约"]
_HEADER_QUOTES = ["价格", "代码", "价格", "现期差1", "代码", "价格", "现期差2"]


class _Challenge(Exception):
    """The check page did not carry a usable cookie value."""


def page_url(day: date) -> str:
    """Return the published spot-versus-futures table for one calendar day."""
    return f"https://{HOST}/sf/day-{day.isoformat()}.html"


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("unrecognized text encoding") from None


def _decimal(text: str) -> Decimal:
    if not isinstance(text, str):
        raise ValueError("unrecognized source number")
    cleaned = text.replace("\u00a0", " ").replace(",", "").strip()
    if _NUMBER.fullmatch(cleaned) is None:
        raise ValueError("unrecognized source number")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        raise ValueError("unrecognized source number") from None


def _normalized_name(text: str) -> str:
    stripped = text.strip()
    return re.sub(r"[A-Za-z]+$", "", stripped).strip() or stripped


def _direction(basis: float) -> str:
    if basis > 0:
        return "spot_premium"
    if basis < 0:
        return "futures_premium"
    return "flat"


def _difference(cell) -> Decimal:
    nested = cell.find("table")
    if nested is None:
        raise ValueError("missing published difference")
    values = nested.find_all("td")
    if not values:
        raise ValueError("missing published difference")
    return _decimal(values[0].get_text())


def parse_basis_page(raw: bytes, day: date) -> dict:
    """Parse one dated spot-versus-futures table into quoted product rows."""
    text = _decode(raw)
    soup = BeautifulSoup(text, "html.parser")
    title = soup.title.get_text() if soup.title is not None else ""
    matched = _TITLE_DATE.search(title)
    if matched is None:
        raise ValueError("missing table date")
    published = date(int(matched.group(1)), int(matched.group(2)), int(matched.group(3)))
    if published != day:
        raise ValueError("table date mismatch")
    table = soup.find("table", id="fdata")
    if table is None:
        raise ValueError("missing spot-futures table")
    rows = [row for row in table.find_all("tr") if row.parent is table]
    if len(rows) < 2:
        raise ValueError("missing table header")
    first = [cell.get_text(strip=True) for cell in rows[0].find_all("td", recursive=False)]
    second = [cell.get_text(strip=True) for cell in rows[1].find_all("td", recursive=False)]
    if first != _HEADER_MAIN or second != _HEADER_QUOTES:
        raise ValueError("unrecognized table header")
    products: dict[str, dict] = {}
    group = None
    for row in rows[2:]:
        cells = row.find_all("td", recursive=False)
        if len(cells) == 1:
            group = cells[0].get_text(strip=True)
            continue
        if len(cells) != _GROUP_COLUMNS or group is None:
            raise ValueError("unrecognized table row")
        name_cell = cells[0]
        name = name_cell.get_text(strip=True)
        if not name:
            raise ValueError("missing product name")
        spot = _decimal(cells[1].get_text())
        near_contract = cells[2].get_text(strip=True)
        near_price = _decimal(cells[3].get_text())
        near_basis = _difference(cells[4])
        main_contract = cells[5].get_text(strip=True)
        main_price = _decimal(cells[6].get_text())
        published_basis = _difference(cells[7])
        for contract in (near_contract, main_contract):
            if _CONTRACT.fullmatch(contract) is None:
                raise ValueError("unrecognized contract code")
        if spot <= 0 or near_price <= 0 or main_price <= 0:
            raise ValueError("non-positive quoted price")
        key = _normalized_name(name)
        if key in products:
            raise ValueError("duplicate product row")
        basis = float(spot - main_price)
        products[key] = {
            "name": name,
            "group": group,
            "spot_price": float(spot),
            "near_contract": near_contract,
            "near_price": float(near_price),
            "near_basis": float(near_basis),
            "main_contract": main_contract,
            "main_price": float(main_price),
            "published_basis": float(published_basis),
            "basis": basis,
            "basis_direction": _direction(basis),
        }
    return {"data_as_of": day.isoformat(), "source_url": page_url(day), "products": products}


def _challenge_like(text: str) -> bool:
    return 'id="fdata"' not in text and (CHALLENGE_COOKIE in text or "安全检查" in text)


def _challenge_token(text: str) -> str | None:
    matched = _TOKEN.search(text) or _LITERAL_COOKIE.search(text)
    return matched.group(1) if matched is not None else None


async def _fetch(client, url: str, deadline: float) -> bytes:
    raw = bytearray()
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


async def _table_page(client, url: str, deadline: float) -> bytes:
    raw = await _fetch(client, url, deadline)
    if not _challenge_like(_decode(raw)):
        return raw
    token = _challenge_token(_decode(raw))
    if token is None:
        raise _Challenge()
    client.cookies.set(CHALLENGE_COOKIE, token, domain=urlsplit(url).hostname or HOST, path="/")
    raw = await _fetch(client, url, deadline)
    if _challenge_like(_decode(raw)):
        raise _Challenge()
    return raw


def _challenge_failure(day: date, url: str) -> dict:
    return {
        "status": "basis_unavailable",
        "requested_date": day.isoformat(),
        "source_url": url,
        "error_code": "challenge_unsolved",
    }


def _no_report(day: date, url: str) -> dict:
    return {
        "status": "basis_unavailable",
        "requested_date": day.isoformat(),
        "source_url": url,
        "error_code": "report_not_found",
    }


def _failure(day: date, url: str, error: BaseException) -> dict:
    return {
        "status": "basis_unavailable",
        "requested_date": day.isoformat(),
        "source_url": url,
        **source_failure(error),
    }


def _consistent(basis: float, published: float) -> bool:
    if not (math.isfinite(basis) and math.isfinite(published)):
        return False
    if abs(basis - published) > 1:
        return False
    return not (basis > 0 > published or basis < 0 < published)


def _basis_row(parsed, symbol, day, url):
    group, name = BASIS_PRODUCTS[symbol]
    row = parsed["products"].get(name)
    if row is None:
        return {
            "status": "basis_unavailable",
            "requested_date": day.isoformat(),
            "source_url": url,
            "error_code": "product_not_listed",
        }
    if row["group"] != group:
        raise ValueError("unexpected exchange group")
    if not _consistent(row["basis"], row["published_basis"]):
        return {
            "status": "basis_unavailable",
            "requested_date": day.isoformat(),
            "source_url": url,
            "error_code": "source_price_mismatch",
        }
    return {
        "status": "collected",
        "data_as_of": parsed["data_as_of"],
        "source_url": url,
        "group": row["group"],
        "source_name": SOURCE_NAME,
        "spot_price": row["spot_price"],
        "futures_contract": row["main_contract"],
        "futures_price": row["main_price"],
        "basis": row["basis"],
        "basis_direction": row["basis_direction"],
        "source_published_basis": row["published_basis"],
    }


async def read_basis(client, symbol: str, day: date, deadline: float) -> dict:
    """Read one dated spot-versus-futures table row for one mapped product."""
    url = page_url(day)
    if symbol not in BASIS_PRODUCTS:
        return {
            "status": "basis_unavailable",
            "requested_date": day.isoformat(),
            "source_url": url,
            "error_code": "symbol_not_covered",
        }
    try:
        raw = await _table_page(client, url, deadline)
        parsed = await asyncio.to_thread(parse_basis_page, raw, day)
        if not parsed["products"]:
            return _no_report(day, url)
        return _basis_row(parsed, symbol, day, url)
    except _Challenge:
        return _challenge_failure(day, url)
    except (ValueError, httpx.HTTPError, TimeoutError, RecursionError) as error:
        return _failure(day, url, error)


def _identity(symbol: str) -> dict:
    name, exchange = futures.PRODUCTS[symbol][0], futures.PRODUCTS[symbol][1]
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


async def latest_table(symbols, today):
    """Check one dated table for the overview, shared across all mapped products."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TOTAL_SECONDS
    try:
        async with (
            asyncio.timeout_at(deadline),
            _SLOTS,
            public_network.async_client(timeout=CLIENT_TIMEOUT) as client,
        ):
            for offset in range(_LOOKBACK_DAYS):
                day = today - timedelta(days=offset)
                url = page_url(day)
                try:
                    raw = await _table_page(client, url, min(deadline, loop.time() + REQUEST_SECONDS))
                    parsed = await asyncio.to_thread(parse_basis_page, raw, day)
                except (ValueError, httpx.HTTPError, TimeoutError, RecursionError) as error:
                    if _failure(day, url, error)["error_code"] == "report_not_found":
                        continue
                    break
                if parsed["products"]:
                    rows = {}
                    for symbol in symbols:
                        if symbol in BASIS_PRODUCTS:
                            try:
                                rows[symbol] = _basis_row(parsed, symbol, day, url)
                            except ValueError:
                                continue
                    return rows
    except (TimeoutError, httpx.HTTPError, _Challenge):
        pass
    return {}


def _remember(key: tuple[str, str], ttl: int, value: dict) -> None:
    _CACHE[key] = (time.monotonic(), ttl, copy.deepcopy(value))


async def snapshot(symbol: str, observed: str | None = None) -> dict:
    """Read the latest or requested dated spot-versus-futures row for one product.

    ``observed`` is an optional ``YYYY-MM-DD`` data date. Without it the newest
    table inside a bounded calendar window is used. Products outside the mapped
    table, unsolved check pages and inconsistent rows return
    ``basis_unavailable`` without any estimate or unit conversion.
    """
    if symbol not in futures.PRODUCTS:
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
    if symbol not in BASIS_PRODUCTS:
        value = {
            **_identity(symbol),
            "status": "basis_unavailable",
            "requested_date": observed,
            "date_basis": date_basis,
            "error_code": "symbol_not_covered",
            "reason": UNCOVERED_REASONS[symbol],
            "covered_symbols": sorted(BASIS_PRODUCTS),
            "source": None,
            "collected_at": None,
            "tradable_contract": False,
            "notice": NOTICE,
            "source_attempts": [],
            "gaps": ["该品种不在生意社现期表中；不估算、不换算"],
            "cache_ttl_seconds": _STATIC_TTL,
            "cached": False,
        }
        _remember(key, _STATIC_TTL, value)
        return value
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TOTAL_SECONDS
    today = _today()
    candidates = (
        [day] if day is not None else [today - timedelta(days=offset) for offset in range(_LOOKBACK_DAYS)]
    )
    attempts: list[dict] = []
    observation = None
    try:
        async with asyncio.timeout_at(deadline):
            async with _SLOTS, public_network.async_client(timeout=CLIENT_TIMEOUT) as client:
                for candidate in candidates:
                    if loop.time() >= deadline:
                        break
                    result = await read_basis(
                        client, symbol, candidate, min(deadline, loop.time() + REQUEST_SECONDS)
                    )
                    attempts.append(
                        {
                            "source": SOURCE_NAME,
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
                "source": SOURCE_NAME,
                "requested_date": None,
                "status": "basis_unavailable",
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
            "status": "basis_unavailable",
            "requested_date": observed,
            "date_basis": date_basis,
            "error_code": error_code,
            "reason": FAILURE_REASONS.get(error_code, FAILURE_REASONS["invalid_source_data"]),
            "covered_symbols": sorted(BASIS_PRODUCTS),
            "source": None,
            "collected_at": None,
            "tradable_contract": False,
            "notice": NOTICE,
            "source_attempts": attempts,
            "gaps": ["未取得同日现货与主力期货报价；不估算、不换算"],
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
        "spot_price": observation["spot_price"],
        "futures_contract": observation["futures_contract"],
        "futures_price": observation["futures_price"],
        "basis": observation["basis"],
        "basis_direction": observation["basis_direction"],
        "source_published_basis": observation["source_published_basis"],
        "quote_unit": futures._QUOTE_UNITS.get(symbol, "元/吨"),
        "source": {
            "name": observation["source_name"],
            "url": observation["source_url"],
            "kind": SOURCE_KIND,
        },
        "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source_attempts": attempts,
        "gaps": [],
        "cache_ttl_seconds": _CACHE_TTL,
        "cached": False,
    }
    _remember(key, _CACHE_TTL, value)
    return value


_DIRECTIONS = {
    "spot_premium": "现货升水（基差为正）",
    "futures_premium": "期货升水（基差为负）",
    "flat": "现货与期货持平（基差为零）",
}


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _basis_section(observation: dict, quote: str):
    spot = futures._evidence_number(observation["spot_price"])
    main = futures._evidence_number(observation["futures_price"])
    basis = futures._evidence_number(observation["basis"])
    published = futures._evidence_number(observation["source_published_basis"])
    direction = _DIRECTIONS[observation["basis_direction"]]
    return futures._evidence_page(
        "spot_futures_basis",
        "现货价、主力期货价与基差",
        SOURCE_NAME,
        observation["source"]["url"],
        observation["data_as_of"],
        [
            f"现货价格 {spot} {quote}",
            f"主力期货合约 {observation['futures_contract']} 价格 {main} {quote}",
            f"基差（现货减主力期货） {basis} {quote}",
            f"基差方向 {direction}",
            f"来源披露的主力合约现期差 {published} {quote}（与现货减主力期货核对一致）",
            "现货口径 生意社现货均价，不是交易所结算价或交割价",
        ],
    )


def snapshot_document(observation: dict):
    """Render one collected basis observation as an ingestable source document.

    ``None`` means the observation is incomplete or ``basis_unavailable``; the
    tool result is left untouched either way. Only a collected observation is
    registered, because a missing row must never become a number.
    """
    if not isinstance(observation, dict) or observation.get("status") != "collected":
        return None
    symbol = observation.get("symbol")
    if not isinstance(symbol, str) or symbol not in futures.PRODUCTS:
        return None
    source = observation.get("source")
    url = source.get("url") if isinstance(source, dict) else None
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    if not isinstance(observation.get("collected_at"), str):
        return None
    try:
        period = date.fromisoformat(str(observation.get("data_as_of")))
    except ValueError:
        return None
    if (
        not _finite(observation.get("spot_price"))
        or not _finite(observation.get("futures_price"))
        or not _finite(observation.get("basis"))
        or not _finite(observation.get("source_published_basis"))
        or not isinstance(observation.get("futures_contract"), str)
        or not observation["futures_contract"]
        or observation.get("basis_direction") not in _DIRECTIONS
    ):
        return None
    name, exchange = futures.PRODUCTS[symbol][0], futures.PRODUCTS[symbol][1]
    quote = observation.get("quote_unit")
    if not isinstance(quote, str) or not quote:
        quote = futures._QUOTE_UNITS.get(symbol, "元/吨")
    section = _basis_section(observation, quote)
    if section is None:
        return None
    label = section["label"]
    header = (
        f"数据来源：{section['source']}｜数据日期：{futures._chinese_day(period.isoformat())}｜"
        f"品种：{name} {symbol}（{exchange}）"
    )
    return {
        "symbol": symbol,
        "name": name,
        "exchange": exchange,
        "title": f"{name} {symbol} 现货基差快照 {period.isoformat()}",
        "url": section["url"] or url,
        "publisher": section["source"],
        "retrieved_at": observation["collected_at"],
        "data_period": period.isoformat(),
        "status": "collected",
        "tradable_contract": False,
        "source": {"name": section["source"], "url": url, "kind": SOURCE_KIND},
        "sections": [
            {
                "page": 1,
                "section": section["section"],
                "label": label,
                "source": section["source"],
                "url": section["url"],
                "data_period": period.isoformat(),
                "symbol": symbol,
                "name": name,
            }
        ],
        "pages": [
            {
                "page": 1,
                "text": "\n".join([header, f"【{label}】", *section["lines"], NOTICE]),
            }
        ],
    }
