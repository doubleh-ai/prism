"""Shared request contracts for research, portfolio and administration."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]")
_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})\Z")


def clean_text(value: str) -> str:
    if _CONTROL.search(value):
        raise ValueError("control character")
    result = value.strip()
    if not result:
        raise ValueError("empty text")
    return result


def canonical_instant(value: str | None, zone: str) -> str | None:
    """UTC is an explicit instant; other offsets must describe a real zoned wall time.

    Fall-back wall times are accepted only with an explicit valid offset (which
    selects the fold). Naive dates and spring-forward gaps are never guessed.
    """
    if value is None:
        return None
    if not _INSTANT.fullmatch(value):
        raise ValueError("explicit instant required")
    if not value.endswith("Z"):
        # fromisoformat normalizes out-of-range offset minutes (08:60 -> 09:00).
        # Validate the original components before that information is lost.
        offset = value[-6:]
        if int(offset[1:3]) > 23 or int(offset[4:6]) > 59:
            raise ValueError("invalid offset components")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    utc = parsed.astimezone(UTC)
    if not value.endswith(("Z", "+00:00")):
        local = utc.astimezone(ZoneInfo(zone))
        if local.utcoffset() != parsed.utcoffset() or local.replace(tzinfo=None) != parsed.replace(
            tzinfo=None
        ):
            raise ValueError("offset does not describe this time zone")
    return utc.isoformat(timespec="microseconds").replace("+00:00", "Z")


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


async def read_model_body(request, model, *, maximum=32768):
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > maximum:
            raise HTTPException(413, "请求内容过长")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result

    try:
        value = json.loads(content, object_pairs_hook=unique)
        return model.model_validate(value)
    except (ValueError, TypeError, RecursionError, OverflowError, ValidationError):
        raise HTTPException(422, "请求内容格式无效，请检查字段、长度、时间与时区") from None
