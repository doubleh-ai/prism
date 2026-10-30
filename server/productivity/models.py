"""Closed request contracts and explicit instant/time-zone handling."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TaskStatus = Literal["todo", "doing", "done", "cancelled"]
TaskPriority = Literal["low", "normal", "high"]
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]")
_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})\Z")


def clean_text(value: str, *, empty: bool = False) -> str:
    if _CONTROL.search(value):
        raise ValueError("control character")
    result = value.strip()
    if not result and not empty:
        raise ValueError("empty text")
    return result


def zone_name(value: str) -> str:
    if value != value.strip() or len(value) > 100 or not value or value.startswith(("/", ".")):
        raise ValueError("invalid time zone")
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError("invalid time zone") from None
    return value


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


class CreateSpace(ClosedModel):
    request_id: str = Field(min_length=36, max_length=36)
    name: str = Field(min_length=1, max_length=80)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=100)

    @field_validator("request_id")
    @classmethod
    def request_uuid(cls, value):
        if str(UUID(value)) != value:
            raise ValueError("canonical UUID required")
        return value

    @field_validator("name")
    @classmethod
    def name_text(cls, value):
        return clean_text(value)

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, value):
        return zone_name(value)


class CreateTask(ClosedModel):
    request_id: str = Field(min_length=36, max_length=36)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=6000)
    status: TaskStatus = "todo"
    priority: TaskPriority = "normal"
    due_at: str | None = Field(default=None, max_length=40)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=100)

    _uuid = field_validator("request_id")(CreateSpace.request_uuid.__func__)

    @field_validator("title")
    @classmethod
    def title_text(cls, value):
        return clean_text(value)

    @field_validator("description")
    @classmethod
    def description_text(cls, value):
        return clean_text(value, empty=True)

    _zone = field_validator("timezone")(CreateSpace.valid_zone.__func__)

    @model_validator(mode="after")
    def explicit_due(self):
        self.due_at = canonical_instant(self.due_at, self.timezone)
        return self


class UpdateTask(ClosedModel):
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=6000)
    status: TaskStatus | None = None
    priority: TaskPriority | None = None
    due_at: str | None = Field(default=None, max_length=40)
    timezone: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def nonempty(self):
        keys = self.model_fields_set - {"expected_version"}
        if not keys or any(getattr(self, key) is None for key in keys - {"due_at"}):
            raise ValueError("nonempty patch required")
        if "title" in keys:
            self.title = clean_text(self.title)
        if "description" in keys:
            self.description = clean_text(self.description, empty=True)
        if "timezone" in keys:
            self.timezone = zone_name(self.timezone)
        # The existing row supplies the time zone when it is absent in a patch.
        return self
