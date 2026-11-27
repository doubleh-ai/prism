"""First-party disclosure fetch: cninfo search -> complete PDF -> physical pages.

This is the server-owned path that turns a public announcement query into
original page text. It only talks to the cninfo announcement index and its
static file host, rejects redirects leaving that pair, and runs the hardened
``pdf_text`` child for parsing. Every network, WAF, non-PDF, or parser failure
returns a structured status so the agent degrades to second-hand evidence
instead of claiming an original it never read.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal, DecimalException, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from ..research_context import RESEARCH_TIMEZONE
from ..source_urls import public_source_url
from .pdf_text import MAX_PDF_BYTES, extract_pdf

QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
PDF_BASE_URL = "http://static.cninfo.com.cn/"
ALLOWED_HOSTS = frozenset({"www.cninfo.com.cn", "static.cninfo.com.cn"})
TIMEOUT_SECONDS = 60
MAX_INDEX_BYTES = 2 * 1024 * 1024
MAX_PAGE_COUNT = 2000
MAX_TOTAL_CHARS = 2_000_000
MAX_QUERY_CHARS = 160
MAX_CONTAINS_CHARS = 100
MAX_MATCHES = 20
PAGE_SIZE = 30
# The parser child enforces a two-second CPU budget (server/agent/pdf_text.py),
# and every batch re-parses the whole PDF, so a batch's CPU cost grows with the
# document, not just with its page count. Batch fourteen's 20-page batches lost
# a 143-page annual report at ~2.0 s (killed as ``pdf_invalid``); an independent
# 10-page read of the same report finished in ~1.6 s. Keep every batch well
# under that budget and halve it again when the child still overruns.
_BATCH_PAGES = 10
_MIN_BATCH_PAGES = 2
_WORKER_TEXT_CHARS = 32768
_USER_AGENT = "Mozilla/5.0"
_REFERER = "http://www.cninfo.com.cn/"
_TZ = ZoneInfo(RESEARCH_TIMEZONE)
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])-?\d[\d,]*(?:\.\d+)?")
_DECIMAL = re.compile(
    r"[+-]?(?:\d{1,3}(?:,\d{3}){1,25}|\d{1,80})(?:\.\d{1,24})?(?:[eE][+-]?\d{1,3})?",
    re.ASCII,
)
_HTML_SUFFIXES = (".html", ".htm")


def _now() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


def _decimal(value) -> Decimal | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or not _DECIMAL.fullmatch(text):
        return None
    try:
        number = Decimal(text.replace(",", ""))
    except (DecimalException, InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number.copy_abs() < Decimal("1e100") else None


def _text(value, limit: int) -> str:
    if isinstance(value, int):
        return str(value)[:limit]
    if isinstance(value, str):
        return value.strip()[:limit]
    return ""


def _valid_date(value) -> bool:
    return isinstance(value, str) and bool(_ISO_DATE.fullmatch(value)) and _parse_date(value) is not None


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _announcement_date(value) -> str | None:
    if isinstance(value, int):
        seconds = value / 1000
    elif isinstance(value, str) and value.strip().isdigit():
        seconds = int(value.strip()) / 1000
    else:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=_TZ).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def safe_fetch_url(url) -> str | None:
    """Admit only the two cninfo hosts over ordinary HTTP(S) ports."""

    if not isinstance(url, str) or not 1 <= len(url) <= 4000:
        return None
    if public_source_url(url) is None:
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in ALLOWED_HOSTS:
        return None
    if port not in (None, 80, 443):
        return None
    return url


class _SafeRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if safe_fetch_url(newurl) is None:
            raise URLError("unsafe_redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read(url: str, *, data: bytes | None, limit: int) -> dict:
    request = Request(
        url,
        data=data,
        headers={"User-Agent": _USER_AGENT, "Referer": _REFERER, "Accept": "*/*"},
        method="POST" if data is not None else "GET",
    )
    opener = build_opener(_SafeRedirects())
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            status = response.getcode()
            if status != 200:
                return {"status": "http_error", "http_status": status}
            body = response.read(limit + 1)
            if len(body) > limit:
                return {"status": "too_large"}
            return {"status": "ok", "url": response.geturl(), "http_status": status, "body": body}
    except HTTPError as error:
        return {"status": "http_error", "http_status": error.code}
    except URLError as error:
        reason = str(error.reason or "")
        if "unsafe_redirect" in reason:
            return {"status": "unsafe_redirect"}
        return {"status": "network_failed", "detail": reason[:200]}
    except TimeoutError:
        return {"status": "timeout"}
    except (OSError, ValueError) as error:
        return {"status": "network_failed", "detail": type(error).__name__}


def _fetch(url: str, *, data: bytes | None = None, limit: int) -> dict:
    """Try HTTPS first, then the plain-HTTP alias of the same cninfo path."""

    attempt = _read(url, data=data, limit=limit)
    if attempt["status"] in {"network_failed", "timeout"} and url.startswith("https://"):
        alternate = "http://" + url[len("https://") :]
        if safe_fetch_url(alternate) is not None:
            retry = _read(alternate, data=data, limit=limit)
            if retry["status"] == "ok":
                retry["fallback_url"] = True
                return retry
    return attempt


def _candidate(row) -> dict | None:
    if not isinstance(row, dict):
        return None
    title = row.get("announcementTitle")
    reference = row.get("adjunctUrl")
    if not isinstance(title, str) or not title.strip() or not isinstance(reference, str):
        return None
    reference = reference.strip()
    if not reference or reference.lower().endswith(_HTML_SUFFIXES):
        return None
    pdf_url = reference if reference.startswith(("http://", "https://")) else urljoin(PDF_BASE_URL, reference)
    if safe_fetch_url(pdf_url) is None or not urlsplit(pdf_url).path.lower().endswith(".pdf"):
        return None
    size = row.get("adjunctSize")
    return {
        "announcement_id": _text(row.get("announcementId"), 40),
        "title": title.strip()[:500],
        "sec_code": _text(row.get("secCode"), 20),
        "sec_name": _text(row.get("secName"), 120),
        "publisher": "巨潮资讯网",
        "published_at": _announcement_date(row.get("announcementTime")),
        "pdf_url": pdf_url,
        "pdf_size_kb": size if isinstance(size, int) and 0 < size <= 1024 * 1024 else None,
    }


def search_announcement(query: str, *, start_date: str | None, end_date: str | None) -> dict:
    fields = {
        "pageNum": "1",
        "pageSize": str(PAGE_SIZE),
        "column": "szse",
        "tabName": "fulltext",
        "searchkey": query,
    }
    if start_date and end_date:
        fields["seDate"] = f"{start_date}~{end_date}"
    fetched = _fetch(QUERY_URL, data=urlencode(fields).encode("utf-8"), limit=MAX_INDEX_BYTES)
    if fetched["status"] != "ok":
        return fetched
    try:
        index = json.loads(fetched["body"].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {"status": "invalid_response"}
    if not isinstance(index, dict):
        return {"status": "invalid_response"}
    rows = index.get("announcements")
    candidates = [
        row_value for row in (rows if isinstance(rows, list) else []) if (row_value := _candidate(row))
    ]
    if not candidates:
        return {"status": "announcement_not_found"}
    total = index.get("totalRecordNum")
    return {
        "status": "ok",
        "announcement": candidates[0],
        "candidates": candidates[:5],
        "index": {
            "url": fetched["url"],
            "body_sha256": hashlib.sha256(fetched["body"]).hexdigest(),
            "http_status": fetched["http_status"],
            "total": total if isinstance(total, int) and total >= 0 else len(rows),
        },
    }


def download_original_pdf(pdf_url: str) -> dict:
    if safe_fetch_url(pdf_url) is None:
        return {"status": "invalid_url"}
    fetched = _fetch(pdf_url, limit=MAX_PDF_BYTES)
    if fetched["status"] != "ok":
        return fetched
    body = fetched["body"]
    # A WAF/HTML body is not a PDF even when the server answers 200; the magic
    # check happens before any parser sees the bytes.
    if not body.startswith(b"%PDF-"):
        return {"status": "not_pdf"}
    return {
        "status": "ok",
        "body": body,
        "url": fetched["url"],
        "requested_url": pdf_url,
        "http_status": fetched["http_status"],
        "pdf_sha256": hashlib.sha256(body).hexdigest(),
        "file_bytes": len(body),
        "retrieved_at": _now(),
    }


def _batch_rows(extracted: dict) -> list[dict] | None:
    rows = extracted.get("pages")
    if not isinstance(rows, list):
        return None
    clean = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("page"), int)
            or not isinstance(row.get("text"), str)
        ):
            return None
        clean.append({"page": row["page"], "text": row["text"]})
    return clean


def _extract_batch(raw: bytes, first: int, batch: int) -> tuple[dict, int]:
    """Extract one page batch, halving it while the parser reports ``pdf_invalid``.

    A readable PDF can still make the hardened child exceed its CPU budget and
    be killed, which the child reports as ``pdf_invalid``. Retrying the same
    first page with half the batch bounds the work per child, so a big report
    degrades into more, smaller batches instead of failing the whole document.
    The batch never drops below ``_MIN_BATCH_PAGES``: two failing pages still
    fail the document, so a corrupt page is never silently skipped.
    """
    while True:
        extracted = extract_pdf(raw, first_page=first, page_count=batch)
        if extracted.get("status") != "pdf_invalid" or batch <= _MIN_BATCH_PAGES:
            return extracted, batch
        batch = max(_MIN_BATCH_PAGES, batch // 2)


def read_pages(raw: bytes) -> dict:
    """Extract every page in small bounded batches; one passage per page later."""

    pages: list[dict] = []
    truncated: list[int] = []
    total = 0
    first = 1
    batch = _BATCH_PAGES
    while True:
        extracted, batch = _extract_batch(raw, first, batch)
        status = extracted.get("status")
        if status == "pdf_page_out_of_range":
            return {"status": "pdf_invalid"}
        if status != "ok":
            return {"status": status if status else "pdf_invalid"}
        count_total = extracted.get("page_count")
        if not isinstance(count_total, int) or not 1 <= count_total <= MAX_PAGE_COUNT:
            return {"status": "pdf_invalid"}
        total = count_total
        rows = _batch_rows(extracted)
        if not rows:
            return {"status": "pdf_no_text"}
        if any(row["page"] != first + offset for offset, row in enumerate(rows)):
            return {"status": "pdf_invalid"}
        if len(rows) < batch and rows[-1]["page"] < total and batch > 1:
            batch = len(rows)
            continue
        if len(rows) == batch and rows[-1]["page"] < total:
            # The worker cap can cut the last requested page. Re-read that one
            # page alone so stored text and its hash cover the complete page.
            single = extract_pdf(raw, first_page=rows[-1]["page"], page_count=1)
            single_rows = _batch_rows(single) if single.get("status") == "ok" else None
            if single_rows and single_rows[0]["page"] == rows[-1]["page"]:
                rows[-1] = single_rows[0]
                if len(single_rows[0]["text"]) >= _WORKER_TEXT_CHARS:
                    truncated.append(single_rows[0]["page"])
        pages.extend(rows)
        if sum(len(row["text"]) for row in pages) > MAX_TOTAL_CHARS:
            return {"status": "too_large"}
        if rows[-1]["page"] >= total:
            break
        first = rows[-1]["page"] + 1
        batch = _BATCH_PAGES
    if len(pages) != total:
        return {"status": "pdf_invalid"}
    return {"status": "ok", "page_count": total, "pages": pages, "truncated_pages": truncated}


def find_matches(pages: list[dict], contains: str | None) -> list[dict]:
    """Locate the claim value (numeric-equal token or literal text) on pages."""

    if not isinstance(contains, str) or not contains.strip() or not isinstance(pages, list):
        return []
    needle = contains.strip()
    target = _decimal(needle)
    matches: list[dict] = []
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("page"), int):
            continue
        text = page.get("text")
        if not isinstance(text, str):
            continue
        if target is not None:
            for found in _NUMBER.finditer(text):
                if _decimal(found.group()) != target:
                    continue
                matches.append(_match(page["page"], text, found.start(), len(found.group())))
                if len(matches) >= MAX_MATCHES:
                    return matches
        else:
            offset = text.find(needle)
            if offset >= 0:
                matches.append(_match(page["page"], text, offset, len(needle)))
                if len(matches) >= MAX_MATCHES:
                    return matches
    return matches


def _match(page: int, text: str, offset: int, length: int) -> dict:
    start = max(0, offset - 160)
    end = min(len(text), offset + length + 160)
    return {"page": page, "char_offset": offset, "snippet": text[start:end]}


def fetch_original(arguments) -> dict:
    """Search the disclosure index, download the first match, read all pages."""

    if not isinstance(arguments, dict):
        return {"status": "invalid_arguments", "detail": "参数必须是对象"}
    query = arguments.get("query")
    start_date = arguments.get("start_date")
    end_date = arguments.get("end_date")
    contains = arguments.get("contains")
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
        return {"status": "invalid_arguments", "detail": "query 须为1–160字符"}
    if (start_date is None) != (end_date is None):
        return {"status": "invalid_arguments", "detail": "start_date 与 end_date 须同时提供"}
    if start_date is not None:
        if not _valid_date(start_date) or not _valid_date(end_date):
            return {"status": "invalid_arguments", "detail": "日期须为 YYYY-MM-DD"}
        if _parse_date(start_date) > _parse_date(end_date):
            return {"status": "invalid_arguments", "detail": "start_date 不能晚于 end_date"}
    if contains is not None and (
        not isinstance(contains, str) or not contains.strip() or len(contains) > MAX_CONTAINS_CHARS
    ):
        return {"status": "invalid_arguments", "detail": "contains 须为1–100字符"}
    found = search_announcement(query.strip(), start_date=start_date, end_date=end_date)
    if found["status"] != "ok":
        return found
    announcement = found["announcement"]
    downloaded = download_original_pdf(announcement["pdf_url"])
    if downloaded["status"] != "ok":
        return downloaded
    extracted = read_pages(downloaded["body"])
    if extracted["status"] != "ok":
        return extracted
    pages = extracted["pages"]
    return {
        "status": "ok",
        "query": query.strip(),
        "start_date": start_date,
        "end_date": end_date,
        "contains": contains.strip() if contains else None,
        "announcement": announcement,
        "candidates": found.get("candidates", []),
        "index": found.get("index"),
        "pdf_url": downloaded["url"],
        "requested_pdf_url": announcement["pdf_url"],
        "file_sha256": downloaded["pdf_sha256"],
        "file_bytes": downloaded["file_bytes"],
        "page_count": extracted["page_count"],
        "pages": pages,
        "truncated_pages": extracted.get("truncated_pages", []),
        "retrieved_at": downloaded["retrieved_at"],
        "matches": find_matches(pages, contains),
    }
