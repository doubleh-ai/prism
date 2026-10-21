"""Pydantic request/response schemas.

Defense-in-depth: every endpoint that accepts JSON should validate the
input shape and reject malformed/malicious payloads at the boundary
rather than deep in business logic.
"""
import re
from typing import Any, List, Optional
from pydantic import BaseModel, Field, field_validator, ValidationError


def validation_error_message(e: Exception) -> str:
    """Return a brief, safe error message from a Pydantic ValidationError.

    The raw exception's str() can include the full input value, which
    can be very large (e.g. a 32 KB question). Echoing it back leaks
    data and bloats the response. We only echo the field path and a
    short reason.
    """
    if not isinstance(e, ValidationError):
        # Not a Pydantic error — return a generic message
        return "request payload invalid"
    parts = []
    for err in e.errors()[:5]:
        loc = ".".join(str(x) for x in err.get("loc", []))
        msg = err.get("msg", "invalid")
        # Truncate the msg in case it embeds the input
        if len(msg) > 80:
            msg = msg[:80] + "…"
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts) if parts else "request payload invalid"


# ── Auth ──
class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=512)


class AdminUserCreateRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: str = Field(..., min_length=8, max_length=512)


class AdminPasswordResetRequest(BaseModel):
    password: str = Field(..., min_length=8, max_length=512)


class AdminUserStatusRequest(BaseModel):
    disabled: bool


class ActivityTrackRequest(BaseModel):
    feature: str = Field(..., min_length=1, max_length=32, pattern=r"^[a-z][a-z0-9_-]*$")
    action: str = Field(..., min_length=1, max_length=48, pattern=r"^[a-z][a-z0-9_-]*$")
    target_type: Optional[str] = Field(default="", max_length=32, pattern=r"^[a-z0-9_-]*$")
    target_id: Optional[str] = Field(default="", max_length=120)
    target_label: Optional[str] = Field(default="", max_length=120)


class DiagnosticEventRequest(BaseModel):
    kind: str = Field(..., min_length=1, max_length=48, pattern=r"^[a-z][a-z0-9_-]*$")
    severity: str = Field(default="error", pattern=r"^(info|warning|error|critical)$")
    component: Optional[str] = Field(default="", max_length=120)
    page: Optional[str] = Field(default="", max_length=240)
    message: str = Field(..., min_length=1, max_length=2000)
    stack: Optional[str] = Field(default="", max_length=8000)
    version: Optional[str] = Field(default="", max_length=40)
    context: dict[str, Any] = Field(default_factory=dict)

    @field_validator("context")
    @classmethod
    def _validate_context(cls, value):
        if len(value) > 12:
            raise ValueError("too many context fields")
        clean = {}
        for key, item in value.items():
            key = str(key)[:48]
            if not re.match(r"^[A-Za-z0-9_-]+$", key):
                raise ValueError("invalid context field")
            if isinstance(item, (str, int, float, bool)) or item is None:
                clean[key] = str(item)[:240] if isinstance(item, str) else item
        return clean


# ── Saved reports ──
class SavedReportCreateRequest(BaseModel):
    source: str = Field(default="manual", min_length=1, max_length=32)
    source_id: Optional[str] = Field(default=None, max_length=128)
    conv_id: Optional[str] = Field(default=None, max_length=128)
    ticker: Optional[str] = Field(default="", max_length=32)
    name: Optional[str] = Field(default="", max_length=80)
    title: Optional[str] = Field(default="", max_length=160)
    signal: Optional[str] = Field(default="", max_length=32)
    summary: Optional[str] = Field(default="", max_length=500)
    report: dict[str, Any] = Field(default_factory=dict)


class SavedReportSaveRequest(BaseModel):
    title: Optional[str] = Field(default="", max_length=160)
    summary: Optional[str] = Field(default="", max_length=500)


# ── Vibe Trading ──
class VibePlanRequest(BaseModel):
    prompt: str = Field(..., min_length=2, max_length=2000)
    market: str = Field(default="A股", min_length=1, max_length=32)
    horizon: str = Field(default="swing", pattern=r"^(intraday|swing|position|long)$")
    risk_level: str = Field(default="balanced", pattern=r"^(defensive|balanced|aggressive)$")
    research_depth: str = Field(default="deep", pattern=r"^(focused|deep|swarm)$")
    agent: bool = True


class VibeFollowUpRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=3000)


# ── Conversations ──
class CreateConvRequest(BaseModel):
    skill: Optional[str] = Field(default="bottleneck-hunter", max_length=64)


class UpdateSkillRequest(BaseModel):
    skill: str = Field(..., min_length=1, max_length=64)


class BatchDeleteRequest(BaseModel):
    ids: List[str] = Field(..., min_length=1, max_length=200)

    @field_validator("ids")
    @classmethod
    def _validate_ids(cls, v: List[str]) -> List[str]:
        for cid in v:
            if not isinstance(cid, str) or len(cid) == 0 or len(cid) > 128:
                raise ValueError("invalid conversation id")
        return v


# ── Chat ──
class ChatStreamRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=32000)
    skill: str = Field(default="bottleneck-hunter", max_length=64)
    conv_id: Optional[str] = Field(default=None, max_length=128)
    model: Optional[str] = Field(default=None, max_length=64)
    extra_skills: Optional[List[str]] = Field(default=None, max_length=16)

    @field_validator("extra_skills")
    @classmethod
    def _validate_extra(cls, v):
        if v is None:
            return v
        for s in v:
            if not isinstance(s, str) or len(s) == 0 or len(s) > 64:
                raise ValueError("invalid skill id")
        return v


class ChatRespondRequest(BaseModel):
    conv_id: str = Field(..., min_length=1, max_length=128)
    answer: str = Field(..., min_length=1, max_length=4000)


class ChatCancelRequest(BaseModel):
    conv_id: str = Field(..., min_length=1, max_length=128)


# ── Term explanation ──
class ExplainTermRequest(BaseModel):
    term: str = Field(..., min_length=1, max_length=80)
    context: Optional[str] = Field(default="", max_length=200)
