"""Pydantic request/response schemas.

Defense-in-depth: every endpoint that accepts JSON should validate the
input shape and reject malformed/malicious payloads at the boundary
rather than deep in business logic.
"""
from typing import List, Optional
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
