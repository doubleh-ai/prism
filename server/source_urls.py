"""Public source registration and display only; no DNS or request interception.

These checks cannot establish where a hostname resolves or restrict CLI traffic.
Never apply display redaction to the actual provider's request/continuation frames.
"""

from __future__ import annotations

import html
import ipaddress
import json
import re
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

HIDDEN_SOURCE_URL = "[链接已隐藏]"
_CREDENTIAL_PARAMETERS = frozenset(
    {
        "key",
        "apikey",
        "xapikey",
        "subscriptionkey",
        "ocpapimsubscriptionkey",
        "token",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "authtoken",
        "auth",
        "authorization",
        "authentication",
        "password",
        "passwd",
        "secret",
        "clientsecret",
        "privatekey",
        "accountkey",
        "credential",
        "credentials",
        "cookie",
        "sessionid",
        "signature",
        "sig",
        "sharedaccesssignature",
        "awsaccesskeyid",
        "xamzsignature",
        "xamzcredential",
        "xamzsecuritytoken",
        "xgoogsignature",
        "xgoogcredential",
    }
)
_LOCAL_SUFFIXES = (".localhost", ".local", ".localdomain", ".internal", ".lan", ".home", ".arpa", ".onion")
_SPECIAL_V4 = tuple(
    ipaddress.ip_network(value) for value in ("192.0.0.0/24", "192.88.99.0/24", "198.18.0.0/15")
)
_SPECIAL_V6 = tuple(
    ipaddress.ip_network(value) for value in ("2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20")
)
_URL_IN_TEXT = re.compile(
    r"(?i)(?:https?(?::|%3a|&(?:colon;|#(?:x0*3a|0*58);))"
    r"|[a-z][a-z0-9+.-]*:(?:\\?/){2}"
    r"|(?<![\w:/\\])(?:\\?/){2})[^\s<>\"']+"
)


def _decoded(value: str) -> str:
    for _ in range(3):
        decoded = html.unescape(unquote(value))
        if decoded == value:
            break
        value = decoded
    return value


def _public_host(host: str) -> bool:
    host = host.rstrip(".").casefold()
    if not host or "%" in host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Browsers interpret shortened, hexadecimal and octal numeric hosts as
        # IPv4 addresses. Do not let Python treat those forms as ordinary names.
        if re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+)){0,3}", host):
            return False
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return False
        labels = host.split(".")
        return bool(
            len(host) <= 253
            and len(labels) >= 2
            and not labels[-1].isdigit()
            and not host.endswith(_LOCAL_SUFFIXES)
            and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
        )
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        return bool(
            address.ipv4_mapped is None
            and address in ipaddress.ip_network("2000::/3")
            and not any(address in network for network in _SPECIAL_V6)
        )
    return not any(address in network for network in _SPECIAL_V4)


def public_source_url(value: Any) -> str | None:
    """Accept a credential-free, public-looking absolute HTTP(S) source URL.

    Literal/private/local host checks are a display boundary, not DNS/SSRF proof.
    Return the original URL so ordinary public paths and query strings are intact.
    """
    if not isinstance(value, str) or not value or len(value) > 4000 or " " in value:
        return None
    for candidate in (value, _decoded(value)):
        if any(ord(char) < 32 or ord(char) == 127 for char in candidate) or "\\" in candidate:
            return None
        try:
            parsed = urlsplit(candidate)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or not _public_host(parsed.hostname)
            ):
                return None
            _ = parsed.port  # Reject malformed/out-of-range ports as browsers do.
            for parameters in (parsed.query, parsed.fragment, parsed.fragment.partition("?")[2]):
                for name, _ in parse_qsl(parameters, keep_blank_values=True):
                    normalized = re.sub(r"[^a-z0-9]", "", _decoded(name).casefold())
                    if normalized in _CREDENTIAL_PARAMETERS:
                        return None
        except ValueError:
            return None
    return value


def redact_source_text(value: str) -> str:
    """Hide an entire unsafe URL, including fallback labels and legacy tool text."""

    def replace(match):
        raw = match.group()
        url = raw.rstrip(")]}.,;，。；")
        tail = raw[len(url) :]
        decoded = html.unescape(url.replace("\\/", "/"))
        if decoded.startswith("//"):
            decoded = "https:" + decoded
        return (url if public_source_url(decoded) else HIDDEN_SOURCE_URL) + tail

    return _URL_IN_TEXT.sub(replace, value)


def source_display_payload(value: Any) -> Any:
    """Sanitize a display/storage copy without mutating real tool arguments."""
    if isinstance(value, str):
        clean = redact_source_text(value)
        if clean != value and value.lstrip().startswith(("{", "[")):
            # Legacy chunks may contain serialized JSON inside the message's
            # JSON. Redact decoded strings, not the enclosing escape syntax.
            try:
                decoded = json.loads(value)
            except (ValueError, TypeError):
                pass
            else:
                if isinstance(decoded, (dict, list)):
                    return json.dumps(source_display_payload(decoded), ensure_ascii=False)
        return clean
    if isinstance(value, list):
        return [source_display_payload(item) for item in value]
    if isinstance(value, dict):
        return {
            redact_source_text(key) if isinstance(key, str) else key: source_display_payload(item)
            for key, item in value.items()
        }
    return value
