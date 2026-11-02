"""Fixed public source discovery; query results are leads, never verified facts.

SSE's current disclosure client and Sina's rolling-news client are the protocol
references. Only these anonymous, read-only HTTPS indexes are queried. No paid
search, credential, JavaScript execution, crawling loop or provider fallback.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, date, datetime, timedelta
from html import unescape
from urllib.parse import urlencode, urljoin, urlsplit
from zoneinfo import ZoneInfo

from ..source_urls import public_source_url, redact_source_text
from .sources import fetch_public_index

SHANGHAI = ZoneInfo("Asia/Shanghai")
SSE = "https://query.sse.com.cn/security/stock/queryCompanyBulletinNew.do"
SINA = "https://feed.mix.sina.com.cn/api/roll/get"
PROVIDERS = ("sse", "sina")
SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 0, "maxLength": 80},
        "providers": {
            "type": "array",
            "items": {"type": "string", "enum": list(PROVIDERS)},
            "minItems": 1,
            "maxItems": 2,
            "uniqueItems": True,
        },
        "stock_code": {"type": "string", "pattern": "^[0-9]{6}$"},
        "start_date": {"type": "string", "format": "date"},
        "end_date": {"type": "string", "format": "date"},
        "page": {"type": "integer", "minimum": 1, "maximum": 5, "default": 1},
        "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 12},
    },
    "anyOf": [
        {"required": ["stock_code"]},
        {"required": ["query"], "properties": {"query": {"minLength": 1}}},
    ],
    "additionalProperties": False,
}


def validate(arguments, *, now=None):
    if (
        type(arguments) is not dict
        or not set(arguments) <= set(SCHEMA["properties"])
        or not set(arguments) & {"query", "stock_code"}
    ):
        raise ValueError("invalid discovery arguments")
    query = arguments.get("query", "")
    if (
        type(query) is not str
        or len(query) > 80
        or any(ord(c) < 32 for c in query)
        or redact_source_text(query) != query
        or re.search(r"(?i)\b(?:api[_-]?key|password|token|secret)\s*[:=]", query)
    ):
        raise ValueError("invalid discovery query")
    code = arguments.get("stock_code")
    if code is not None and (type(code) is not str or not re.fullmatch(r"[0-9]{6}", code)):
        raise ValueError("invalid stock code")
    if not query.strip() and not code:
        raise ValueError("missing discovery query")
    providers = arguments.get("providers", list(PROVIDERS) if query.strip() else ["sse"])
    if (
        type(providers) is not list
        or not 1 <= len(providers) <= 2
        or any(type(p) is not str or p not in PROVIDERS for p in providers)
        or len(set(providers)) != len(providers)
    ):
        raise ValueError("invalid discovery providers")
    if not query.strip() and providers != ["sse"]:
        raise ValueError("news discovery requires a public keyword")
    page, limit = arguments.get("page", 1), arguments.get("limit", 12)
    if type(page) is not int or not 1 <= page <= 5 or type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("invalid discovery bounds")
    now = now or datetime.now(SHANGHAI)
    today = now.astimezone(SHANGHAI).date()
    dates = []
    for key, default in [("start_date", today - timedelta(days=30)), ("end_date", today)]:
        value = arguments.get(key, default.isoformat())
        if type(value) is not str or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("invalid discovery date")
        dates.append(date.fromisoformat(value))
    start, end = dates
    if start > end or end > today or (end - start).days > (1095 if code else 30):
        raise ValueError("invalid discovery date range")
    return {
        "query": query.strip(),
        "providers": providers,
        "stock_code": code,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "page": page,
        "limit": limit,
    }


def _clean(value, limit=500):
    if type(value) is not str or len(value) > 12000:
        return ""
    value = unescape(re.sub(r"<[^>]*>", " ", value))
    value = re.sub(r"\s+", " ", value).strip()
    return redact_source_text(value)[:limit]


def _article(url, provider):
    if type(url) is not str:
        return None
    url = urljoin("https://www.sse.com.cn/", url) if provider == "sse" else url
    if not public_source_url(url):
        return None
    try:
        parsed = urlsplit(url)
        allowed = "sse.com.cn" if provider == "sse" else "finance.sina.com.cn"
        if (
            len(url) > 2048
            or parsed.scheme != "https"
            or parsed.port not in (None, 443)
            or not (parsed.hostname == allowed or parsed.hostname.endswith("." + allowed))
        ):
            return None
    except (ValueError, AttributeError):
        return None
    return url


def index_url(provider, args):
    if provider == "sse":
        fields = {
            "isPagination": "true",
            "pageHelp.pageSize": 25,
            "pageHelp.cacheSize": 1,
            "pageHelp.pageNo": args["page"],
            "START_DATE": args["start_date"],
            "END_DATE": args["end_date"],
            "SECURITY_CODE": args["stock_code"] or "",
            "TITLE": args["query"],
            "BULLETIN_TYPE": "",
            "stockType": "",
        }
        return SSE + "?" + urlencode(fields)
    # The current rolling feed does not reliably filter by keyword. Never claim
    # that its latest page is a full-text or full-period news search.
    lower = int(
        datetime.combine(date.fromisoformat(args["start_date"]), datetime.min.time(), SHANGHAI).timestamp()
    )
    upper = (
        int(
            datetime.combine(
                date.fromisoformat(args["end_date"]) + timedelta(days=1), datetime.min.time(), SHANGHAI
            ).timestamp()
        )
        - 1
    )
    return (
        SINA
        + "?"
        + urlencode(
            {
                "pageid": 384,
                "lid": 2519,
                "num": 50,
                "page": args["page"],
                "etime": lower,
                "stime": upper,
                "ctime": upper,
            }
        )
    )


def _rows(payload, provider, page):
    if type(payload) is not dict:
        raise ValueError("invalid source index")
    if provider == "sse":
        groups, pagination = payload.get("result"), payload.get("pageHelp")
        if type(groups) is not list or len(groups) > 1000 or type(pagination) is not dict:
            raise ValueError("invalid disclosure index")
        if type(pagination.get("pageNo")) is not int or pagination["pageNo"] != page:
            raise ValueError("mismatched disclosure page")
        rows = []
        for group in groups:
            if type(group) is dict:
                rows.append(group)
            elif type(group) is list and len(group) <= 500 and all(type(r) is dict for r in group):
                rows.extend(group)
            else:
                raise ValueError("invalid disclosure group")
        count = pagination.get("pageCount")
        more = page < count if type(count) is int and count >= 0 else None
    else:
        result = payload.get("result")
        if (
            type(result) is not dict
            or type(result.get("status")) is not dict
            or type(result["status"].get("code")) is not int
            or result["status"]["code"] != 0
        ):
            raise ValueError("news index refused")
        rows = result.get("data")
        if type(rows) is not list or len(rows) > 1000 or any(type(r) is not dict for r in rows):
            raise ValueError("invalid news index")
        total, end = result.get("total"), result.get("end")
        more = end < total if type(total) is int and type(end) is int and 0 <= end <= total else None
    if len(rows) > 2000:
        raise ValueError("index row limit")
    return rows, more


def parse_index(payload, provider, args, *, now):
    rows, more = _rows(payload, provider, args["page"])
    items, seen, rejected = [], set(), 0
    available_dates = []
    for row in rows:
        try:
            if provider == "sse":
                title = _clean(row.get("TITLE"))
                code, company = row.get("SECURITY_CODE"), _clean(row.get("SECURITY_NAME"), 100)
                if type(code) is not str or not re.fullmatch(r"[0-9]{6}", code):
                    raise ValueError("invalid security code")
                stamp = row.get("SSEDATE")
                if type(stamp) is not str or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", stamp):
                    raise ValueError("invalid disclosure date")
                publication = date.fromisoformat(stamp).isoformat()
                day, precision = publication, "day"
                url = _article(row.get("URL"), provider)
                snippet = f"{code} {company} · {title}"[:400]
                if args["stock_code"] and args["stock_code"] != code:
                    continue
            else:
                title = _clean(row.get("title"))
                stamp = row.get("ctime")
                if type(stamp) not in (str, int) or not re.fullmatch(r"[0-9]{1,12}", str(stamp)):
                    raise ValueError("invalid news date")
                instant = datetime.fromtimestamp(int(stamp), UTC).astimezone(SHANGHAI)
                if not 1990 <= instant.year <= now.year or instant > now:
                    raise ValueError("future news")
                publication, precision = instant.isoformat(timespec="seconds"), "second"
                day = instant.date().isoformat()
                url = _article(row.get("url"), provider)
                company, code = "", None
                snippet = _clean(row.get("summary") or row.get("intro"), 280) or title
            if not title or not url:
                raise ValueError("invalid source entry")
            available_dates.append(day)
            if not args["start_date"] <= day <= args["end_date"]:
                continue
            terms = args["query"].casefold().split()
            searchable = " ".join([title, company, code or "", snippet]).casefold()
            if not all(term in searchable for term in terms) or url in seen:
                continue
            seen.add(url)
            items.append(
                {
                    "url": url,
                    "title": title,
                    "snippet": snippet,
                    "provider": provider,
                    "source_class": "official_disclosure" if provider == "sse" else "secondary_news",
                    "published_at": publication,
                    "publication_precision": precision,
                    "published_at_basis": "source_index_metadata",
                    "stock_code": code,
                    "original_obtained": False,
                    "original_verified": False,
                    "content_scope": "search_snippet",
                }
            )
        except (ValueError, TypeError, OverflowError, OSError):
            rejected += 1
    return items, {
        "rows_received": len(rows),
        "rejected_entries": rejected,
        "matching_entries": len(items),
        "available_date_start": min(available_dates) if available_dates else None,
        "available_date_end": max(available_dates) if available_dates else None,
        "more_pages_available": more,
        "complete_search": False,
    }


def find(arguments):
    now = datetime.now(SHANGHAI)
    args = validate(arguments, now=now)
    deadline = time.monotonic() + 8
    items, coverage = [], []
    for provider in args["providers"]:
        url = index_url(provider, args)
        record = {
            "provider": provider,
            "index_url": url,
            "requested_start": args["start_date"],
            "requested_end": args["end_date"],
            "page": args["page"],
            "complete_search": False,
            "query_mode": "provider_title_query" if provider == "sse" else "recent_index_local_filter",
        }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            coverage.append({**record, "status": "timeout"})
            continue
        result = fetch_public_index(
            url,
            timeout_seconds=min(4, remaining),
            referer="https://www.sse.com.cn/" if provider == "sse" else None,
        )
        if result.get("status") != "ok":
            coverage.append({**record, "status": result.get("status", "unavailable")})
            continue
        try:
            host = urlsplit(result["url"]).hostname or ""
            family = "sse.com.cn" if provider == "sse" else "sina.com.cn"
            if host != family and not host.endswith("." + family):
                raise ValueError("index origin mismatch")
            found, details = parse_index(result["data"], provider, args, now=now)
            if details["rows_received"] and details["rejected_entries"] == details["rows_received"]:
                raise ValueError("no valid index entries")
        except (ValueError, TypeError, KeyError, OverflowError):
            coverage.append({**record, "status": "invalid_index"})
            continue
        receipt = {
            "index_url": result["url"],
            "index_body_sha256": result["body_sha256"],
            "retrieved_at": result["retrieved_at"],
        }
        items.extend({**item, **receipt} for item in found)
        coverage.append({**record, **details, **receipt, "status": "collected"})
    items.sort(
        key=lambda v: (v["source_class"] == "official_disclosure", v["published_at"], v["url"]), reverse=True
    )
    unique = list({v["url"]: v for v in reversed(items)}.values())
    unique.sort(
        key=lambda v: (v["source_class"] == "official_disclosure", v["published_at"], v["url"]), reverse=True
    )
    return {
        "status": "ok" if any(c["status"] == "collected" for c in coverage) else "unavailable",
        "query": args["query"],
        "as_of": now.isoformat(timespec="seconds"),
        "items": unique[: args["limit"]],
        "coverage": coverage,
        "result_limit_reached": len(unique) > args["limit"],
        "complete_search": False,
        "automatic_retry": False,
        "notice": "只取得本页公开索引线索；新浪为有界滚动列表内匹配，不是全文或全时段搜索。日期来自来源索引，不能代替原文落款、数据所属期间或独立事实核验。请用 source_read 读取相关原文，其他来源尚未完成匿名索引适配。",
    }
