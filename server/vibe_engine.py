"""Redaction of retained Vibe research payloads."""

from __future__ import annotations

import re
from typing import Any

_SECRET_FIELDS = re.compile(
    r"(?i)(?:^|[_-])(?:password|secret|api[ _-]?key|(?:access|refresh|id|auth)[_-]?token|token|cookie|authorization)$"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)([\"']?(?:api[ _-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|token|password|secret|authorization|cookie)[\"']?\s*[=:]\s*[\"']?)[^\s&;\"'<>]+"
)
_SECRET_TEXT = re.compile(
    r"(?:sk-(?:ant-|proj-)?[\w-]{16,}|gh[pousr]_[\w]{20,}|(?i:Bearer|Basic)\s+[^\s\"'<>]+)"
)
_URL_AUTH = re.compile(r"(?i)(https?://)[^\s/@]+:[^\s/@]+@")
_PRIVATE_KEY = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|$)")


def redact_engine_text(value: str) -> str:
    """Remove credential-shaped values without rewriting research prose."""
    value = _PRIVATE_KEY.sub("[redacted]", value)
    value = _URL_AUTH.sub(r"\1[redacted]@", value)
    value = _SECRET_TEXT.sub("[redacted]", value)
    return _SECRET_ASSIGNMENT.sub(r"\1[redacted]", value)


def safe_engine_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "研究引擎返回错误"
                if key in {"error", "exception", "traceback", "stack_trace"} and item
                else safe_engine_payload(item)
            )
            for key, item in value.items()
            if not _SECRET_FIELDS.search(str(key))
        }
    if isinstance(value, list):
        return [safe_engine_payload(item) for item in value]
    return redact_engine_text(value) if isinstance(value, str) else value
