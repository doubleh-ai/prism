"""Compatibility route names for Prism Native; no external agent execution."""

from fastapi import Request
from fastapi.responses import JSONResponse

from .agent.api import cancel as chat_cancel
from .agent.api import chat_stream
from .auth import require_auth


async def chat_respond(request: Request):
    """Old interactive clients continue through a new native user message."""
    require_auth(request)
    return JSONResponse({"error": "请在当前对话中发送补充信息，继续本次研究"}, status_code=409)


__all__ = ["chat_stream", "chat_cancel", "chat_respond"]
