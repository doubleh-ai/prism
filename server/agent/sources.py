"""Credential-free public research excerpts with pinned DNS/TLS connections.

Deployment-controlled additional IR hosts are not a model/tool argument. All DNS
answers must be public; the connection uses one literal address and verifies its
peer before and after hostname-checked TLS. Redirects repeat the same checks.

The ten-second deadline covers socket operations and is checked before/after DNS.
The system getaddrinfo call cannot be safely interrupted in Python: its caller
must retain the task-manager auxiliary lease until this owned worker actually
returns. No detached resolver thread or claim of instantaneous cancellation is
created here. This module performs no network operation at import time.
"""

from __future__ import annotations

import contextlib
import http.client
import io
import ipaddress
import os
import re
import socket
import ssl
import time
from datetime import date, datetime
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from ..source_urls import public_source_url, redact_source_text

MAX_BODY_BYTES = 512 * 1024
MAX_TEXT_CHARS = 32 * 1024
MAX_REDIRECTS = 2
_OFFICIAL = (
    "sse.com.cn",
    "szse.cn",
    "bse.cn",
    "hkex.com.hk",
    "cninfo.com.cn",
    "shfe.cn",
    "shfe.com.cn",
    "dce.com.cn",
    "ine.cn",
)
_NEWS = ("cls.cn", "jin10.com", "finance.sina.com.cn")
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\Z")
_REDIRECTS = {301, 302, 303, 307, 308}
_CHARSETS = {"utf-8", "utf8", "gb18030", "gbk", "gb2312", "big5", "iso-8859-1", "windows-1252"}
_CODES = frozenset(
    {
        "invalid_url",
        "host_not_allowed",
        "invalid_host_configuration",
        "invalid_timeout",
        "timeout",
        "dns_failed",
        "unsafe_dns",
        "peer_mismatch",
        "tls_failed",
        "network_failed",
        "redirect_limit",
        "redirect_missing_location",
        "invalid_headers",
        "http_error",
        "body_too_large",
        "unsupported_content_encoding",
        "unsupported_content_type",
        "unsupported_pdf",
        "empty_content",
        "invalid_html",
    }
)


class _Rejected(ValueError):
    pass


def _need(value, code):
    if not value:
        raise _Rejected(code)


def _now():
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    _need(remaining > 0, "timeout")
    return remaining


def _extra_hosts(value):
    if value is None:
        value = os.environ.get("PRISM_RESEARCH_SOURCE_HOSTS", "")
    if isinstance(value, str):
        _need(len(value) <= 4096, "invalid_host_configuration")
        value = [host.strip() for host in value.split(",") if host.strip()]
    _need(type(value) in (list, tuple, set, frozenset) and len(value) <= 32, "invalid_host_configuration")
    hosts = set()
    for host in value:
        _need(isinstance(host, str), "invalid_host_configuration")
        try:
            host = host.rstrip(".").casefold().encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise _Rejected("invalid_host_configuration") from exc
        _need(
            bool(_HOST.fullmatch(host)) and public_source_url("https://" + host + "/"),
            "invalid_host_configuration",
        )
        try:
            ipaddress.ip_address(host)
        except ValueError:
            hosts.add(host)
        else:
            raise _Rejected("invalid_host_configuration")
    return hosts


def _admit_url(value, extras):
    _need(isinstance(value, str) and public_source_url(value) is not None, "invalid_url")
    parsed = urlsplit(value)
    _need(parsed.scheme == "https" and parsed.port in (None, 443), "invalid_url")
    host = parsed.hostname.rstrip(".").casefold().encode("idna").decode("ascii")
    source_class = None
    for family, kind in ((_OFFICIAL, "official_disclosure"), (_NEWS, "secondary_news")):
        if any(host == item or host.endswith("." + item) for item in family):
            source_class = kind
            break
    if source_class is None and host in extras:
        source_class = "company_ir"
    _need(source_class is not None, "host_not_allowed")
    # Only ASCII request-target bytes are sent. Existing percent escapes remain
    # unchanged; fragments never form part of an HTTP request.
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="/%?:@!$&'()*+,;=-._~[]")
    return urlunsplit(("https", host, path, query, "")), host, source_class


def _global_address(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return bool(
        address.is_global
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
        and not (isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None)
        and public_source_url("https://" + (f"[{address}]" if address.version == 6 else str(address)) + "/")
    )


def _resolve(host, deadline):
    _remaining(deadline)
    try:
        rows = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise _Rejected("dns_failed") from exc
    _remaining(deadline)
    _need(0 < len(rows) <= 64, "unsafe_dns")
    addresses = []
    for family, kind, proto, _canonical, sockaddr in rows:
        _need(
            family in (socket.AF_INET, socket.AF_INET6)
            and kind == socket.SOCK_STREAM
            and proto == socket.IPPROTO_TCP
            and _global_address(sockaddr[0]),
            "unsafe_dns",
        )
        _need(sockaddr[1] == 443, "unsafe_dns")
        row = (family, str(ipaddress.ip_address(sockaddr[0])))
        if row not in addresses:
            addresses.append(row)
    return addresses


def _peer(sock, expected):
    try:
        actual = str(ipaddress.ip_address(sock.getpeername()[0]))
    except (ValueError, OSError) as exc:
        raise _Rejected("peer_mismatch") from exc
    _need(actual == expected and _global_address(actual), "peer_mismatch")


def _tls_socket(host, address, deadline):
    family, ip = address
    raw = tls = None
    try:
        raw = socket.socket(family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        raw.settimeout(_remaining(deadline))
        raw.connect((ip, 443, 0, 0) if family == socket.AF_INET6 else (ip, 443))
        _peer(raw, ip)
        context = ssl.create_default_context()
        _need(context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED, "tls_failed")
        raw.settimeout(_remaining(deadline))
        tls = context.wrap_socket(raw, server_hostname=host)
        raw = None  # The TLS socket now owns this descriptor.
        _peer(tls, ip)
        _remaining(deadline)
        return tls
    except BaseException:
        if tls is not None:
            tls.close()
        if raw is not None:
            raw.close()
        raise


@contextlib.contextmanager
def _open_response(host, address, target, deadline):
    sock = _tls_socket(host, address, deadline)
    response = None
    try:
        request = (
            f"GET {target} HTTP/1.1\r\nHost: {host}\r\n"
            "User-Agent: PrismResearch/1.0\r\nAccept: text/html, text/plain;q=0.9\r\n"
            "Accept-Encoding: identity\r\nConnection: close\r\n\r\n"
        ).encode("ascii")
        sock.settimeout(_remaining(deadline))
        sock.sendall(request)
        sock.settimeout(_remaining(deadline))
        response = http.client.HTTPResponse(_DeadlineSocket(sock, deadline), method="GET")
        response.begin()
        # The socket remains owned here throughout body reads, including when
        # the response declares Connection: close. No implicit hostname lookup.
        response._prism_socket = sock
        yield response
    finally:
        if response is not None:
            response.close()
        sock.close()


class _DeadlineReader(io.RawIOBase):
    """Apply the total deadline to every recv, including HTTP header reads."""

    def __init__(self, sock, deadline):
        self.sock, self.deadline, self.received = sock, deadline, 0

    def readable(self):
        return True

    def readinto(self, buffer):
        self.sock.settimeout(_remaining(self.deadline))
        count = self.sock.recv_into(buffer)
        _remaining(self.deadline)
        self.received += count
        _need(self.received <= MAX_BODY_BYTES + 65536, "body_too_large")
        return count


class _DeadlineSocket:
    def __init__(self, sock, deadline):
        self.sock, self.deadline = sock, deadline

    def makefile(self, mode):
        _need(mode == "rb", "network_failed")
        return io.BufferedReader(_DeadlineReader(self.sock, self.deadline), buffer_size=16384)


def _headers(response):
    values = {}
    for name, value in response.getheaders():
        values.setdefault(name.casefold(), []).append(value)
    for name in ("content-length", "content-type", "content-encoding", "location", "transfer-encoding"):
        _need(len(values.get(name, [])) <= 1, "invalid_headers")
    _need(not ("content-length" in values and "transfer-encoding" in values), "invalid_headers")
    _need(
        not values.get("transfer-encoding") or values["transfer-encoding"][0].strip().casefold() == "chunked",
        "invalid_headers",
    )
    return {name: rows[0] for name, rows in values.items()}


def _body(response, headers, deadline):
    length = headers.get("content-length")
    if length is not None:
        _need(length.isdecimal(), "invalid_headers")
        _need(int(length) <= MAX_BODY_BYTES, "body_too_large")
    chunks, size = [], 0
    while True:
        response._prism_socket.settimeout(_remaining(deadline))
        # read1 returns after at most one underlying buffered read. A slow peer
        # cannot restart the total budget with a series of small chunks.
        chunk = response.read1(min(16384, MAX_BODY_BYTES + 1 - size))
        _remaining(deadline)
        if not chunk:
            break
        size += len(chunk)
        _need(size <= MAX_BODY_BYTES, "body_too_large")
        chunks.append(chunk)
    if length is not None:
        _need(size == int(length), "network_failed")
    return b"".join(chunks)


def _publication(value):
    if not isinstance(value, str) or len(value) > 80:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value).isoformat()
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.isoformat(timespec="seconds") if parsed.tzinfo is not None else None
    except ValueError:
        return None


class _Excerpt(HTMLParser):
    _IGNORED = {"script", "style", "nav", "noscript", "template", "header", "footer", "form", "svg", "iframe"}
    _BLOCKS = {"p", "div", "article", "section", "main", "li", "tr", "br", "h1", "h2", "h3", "h4"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = []
        self.in_title = False
        self.title = ""
        self.parts = []
        self.characters = 0
        self.truncated = False
        self.published_at = None

    def _append(self, text):
        remaining = MAX_TEXT_CHARS - self.characters
        self.truncated |= len(text) > remaining
        if remaining > 0:
            self.parts.append(text[:remaining])
            self.characters += min(len(text), remaining)

    def handle_starttag(self, tag, attrs):
        if tag in self._IGNORED:
            _need(len(self.hidden) < 256, "invalid_html")
            self.hidden.append(tag)
        if self.hidden:
            return
        if tag == "title":
            self.in_title = True
        elif tag == "meta":
            attrs = dict(attrs)
            key = (attrs.get("property") or attrs.get("name") or "").casefold()
            if key in {"article:published_time", "datepublished", "pubdate", "publishdate"}:
                self.published_at = self.published_at or _publication(attrs.get("content"))
        elif tag == "time" and self.published_at is None:
            # A generic time element may describe an event rather than a
            # publication date; only explicit datePublished markup is accepted.
            attrs = dict(attrs)
            if attrs.get("itemprop") == "datePublished":
                self.published_at = _publication(attrs.get("datetime"))
        if tag in self._BLOCKS:
            self._append("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.hidden:
            if tag in self.hidden:
                index = len(self.hidden) - 1 - self.hidden[::-1].index(tag)
                del self.hidden[index:]
            return
        if tag == "title":
            self.in_title = False
        if tag in self._BLOCKS:
            self._append("\n")

    def handle_data(self, data):
        if self.hidden:
            return
        if self.in_title:
            self.title = (self.title + data)[:500]
        else:
            self._append(data)


def _extract(raw, content_type):
    match = re.search(r"charset\s*=\s*[\"']?([^;\s\"']+)", content_type, re.I)
    charset = match.group(1).casefold() if match else "utf-8"
    charset = charset if charset in _CHARSETS else "utf-8"
    decoded = raw.decode(charset, errors="replace")
    if content_type.partition(";")[0].strip().casefold() == "text/plain":
        text, title, published, truncated = decoded[:MAX_TEXT_CHARS], "", None, len(decoded) > MAX_TEXT_CHARS
    else:
        parser = _Excerpt()
        parser.feed(decoded)
        parser.close()
        text = "".join(parser.parts)
        title, published, truncated = parser.title, parser.published_at, parser.truncated
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return redact_source_text(text), redact_source_text(title.strip()), published, truncated


def fetch_public_source(url, *, timeout_seconds=10, allowed_hosts=None):
    """Return an untrusted extracted excerpt, never an automatically verified fact.

    allowed_hosts is deployment configuration, not untrusted tool input. No
    cookie, authorization, proxy, redirect retry or external asset is used.
    original_obtained describes a complete raw HTTP body received in memory;
    original_preserved stays False and content_scope remains an excerpt.
    """
    current = None
    source_class = None
    redirects = 0
    http_status = None
    try:
        _need(type(timeout_seconds) in (int, float) and 0 < timeout_seconds <= 10, "invalid_timeout")
        deadline = time.monotonic() + timeout_seconds
        extras = _extra_hosts(allowed_hosts)
        candidate = url
        while True:
            _remaining(deadline)
            current, host, source_class = _admit_url(candidate, extras)
            addresses = _resolve(host, deadline)
            parsed = urlsplit(current)
            target = parsed.path + ("?" + parsed.query if parsed.query else "")
            with _open_response(host, addresses[0], target, deadline) as response:
                http_status = response.status
                headers = _headers(response)
                if http_status in _REDIRECTS:
                    _need(redirects < MAX_REDIRECTS, "redirect_limit")
                    _need(bool(headers.get("location")), "redirect_missing_location")
                    candidate = urljoin(current, headers["location"])
                    _admit_url(candidate, extras)
                    redirects += 1
                    continue
                _need(200 <= http_status < 300, "http_error")
                content_type = headers.get("content-type", "").strip()
                mime = content_type.partition(";")[0].strip().casefold()
                _need(mime != "application/pdf", "unsupported_pdf")
                _need(
                    mime in {"text/html", "text/plain", "application/xhtml+xml"}, "unsupported_content_type"
                )
                _need(
                    headers.get("content-encoding", "identity").strip().casefold() in {"", "identity"},
                    "unsupported_content_encoding",
                )
                raw = _body(response, headers, deadline)
                text, title, published, truncated = _extract(raw, content_type)
                _remaining(deadline)
            return {
                "status": "ok" if text else "empty_content",
                "url": current,
                "title": title,
                "text": text,
                "retrieved_at": _now(),
                "published_at": published,
                "published_at_basis": "page_metadata" if published else None,
                "original_obtained": True,
                "original_preserved": False,
                "content_scope": "server_extracted_excerpt",
                "text_truncated": truncated,
                "source_class": source_class,
                "semantic_status": "unreviewed",
                "trust_level": "untrusted_external_content",
                "http_status": http_status,
                "body_bytes": len(raw),
                "redirect_count": redirects,
                "automatic_retry": False,
            }
    except _Rejected as exc:
        code = exc.args[0] if exc.args and exc.args[0] in _CODES else "network_failed"
    except TimeoutError:
        code = "timeout"
    except ssl.SSLError:
        code = "tls_failed"
    except (OSError, http.client.HTTPException, UnicodeError, ValueError):
        code = "network_failed"
    return {
        "status": code,
        "url": current,
        "title": "",
        "text": "",
        "retrieved_at": _now(),
        "published_at": None,
        "original_obtained": False,
        "original_preserved": False,
        "content_scope": "unavailable",
        "source_class": source_class,
        "semantic_status": "unreviewed",
        "trust_level": "untrusted_external_content",
        "http_status": http_status,
        "redirect_count": redirects,
        "automatic_retry": False,
    }
