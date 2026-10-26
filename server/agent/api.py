"""Authenticated HTTP delivery for the Prism-owned Agent event protocol."""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import config
from ..auth import require_auth
from ..database import check_rate_limit
from ..models import validation_error_message
from ..source_urls import source_display_payload
from ..work_lifecycle import LifecycleError, get_manager
from . import store
from .prompts import workflow_catalog, workflow_id
from .runtime import ensure_dialogue, get_service

router = APIRouter(prefix="/api/agent")


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=20000)
    conv_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    model: str | None = Field(default=None, max_length=64)
    skill: str = Field(default="prism-research", max_length=64)
    workflow: str | None = Field(default=None, max_length=32)
    extra_skills: list[str] | None = Field(default=None, max_length=16)
    request_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conv_id: str = Field(min_length=1, max_length=128)
    run_id: str | None = Field(default=None, max_length=128)


def event_payload(event: dict, run: dict) -> dict:
    # Status belongs to this persisted event, not the latest run status. Replaying
    # a completed run must not mark its first historical frame terminal.
    ui = event.get("ui") if isinstance(event.get("ui"), dict) else {}
    status = event.get("status", "running")
    if event["type"] == "run_finished" and status == "cancelled":
        ui = {**ui, "status": "本轮研究已停止，进度已保存"}
    elif event["type"] == "run_interrupted":
        ui = {**ui, "status": "研究执行被中断，已保存进度；未自动重新调用模型"}
    return source_display_payload(
        {
            **ui,
            "agent": {
                "run_id": run["run_id"],
                "conv_id": run["conv_id"],
                "seq": event["seq"],
                "status": status,
                "event": event["type"],
                "engine": "prism-native",
                "workflow": run["workflow"],
            },
        }
    )


def stream_run(user: str, run_id: str, *, after_seq: int = 0) -> StreamingResponse:
    initial = store.get_run(user, run_id)
    if after_seq < 0 or after_seq > initial["last_seq"]:
        raise HTTPException(422, "事件序号无效")

    async def replay():
        cursor, pulse_at = after_seq, time.monotonic()
        while True:
            try:
                run = store.get_run(user, run_id)
                events = store.list_events(user, run_id, cursor)
            except HTTPException as error:
                if error.status_code in {401, 404}:
                    return
                raise
            for event in events:
                cursor = event["seq"]
                payload = event_payload(event, run)
                yield f"id: {cursor}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            if run["status"] in store.TERMINAL and cursor >= run["last_seq"]:
                yield "data: [DONE]\n\n"
                return
            if events:
                continue
            if time.monotonic() - pulse_at >= 5:
                yield 'data: {"pulse":"wait"}\n\n'
                pulse_at = time.monotonic()
            await asyncio.sleep(0.1)

    return StreamingResponse(
        replay(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/workflows")
async def workflows(request: Request):
    require_auth(request)
    return workflow_catalog()


@router.get("/tools")
async def tools(request: Request):
    require_auth(request)
    from .tools import native_catalog

    return {"capabilities": native_catalog()}


@router.post("/chat/stream")
async def chat_stream(request: Request):
    user = require_auth(request)
    try:
        req = ChatRequest(**(await request.json() or {}))
        workflow = workflow_id(req.workflow or req.skill)
    except Exception as error:
        return JSONResponse({"error": validation_error_message(error)}, status_code=422)
    if not req.question.strip():
        return JSONResponse({"error": "请输入研究问题"}, status_code=422)
    if not check_rate_limit(f"native-chat:{user}", config.RATE_CHAT_PER_MIN, 60):
        return JSONResponse({"error": "请求太频繁，请稍后再试"}, status_code=429)
    model_id = req.model or config.DEFAULT_MODEL
    model_cfg = config.MODELS.get(model_id)
    if model_cfg is None:
        return JSONResponse({"error": "所选模型不存在，请重新选择"}, status_code=422)
    if not model_cfg.get("api_key"):
        return JSONResponse({"error": "所选模型尚未配置认证，请选择可用模型或联系管理员"}, status_code=503)
    manager = get_manager(getattr(request, "scope", None))
    try:
        request_lease = manager.admit("request", persist_required=False)
    except LifecycleError:
        return JSONResponse({"error": "服务维护中，请稍后重试"}, status_code=503)
    with request_lease:
        run = store.create_run(
            user,
            req.conv_id,
            req.question.strip(),
            model_id,
            workflow,
            request_id=req.request_id,
            exclusive_conversation=True,
        )
        try:
            ensure_dialogue(user, run)
        except Exception:
            # A failed initial history check must not strand an unclaimed run
            # in the conversation/user admission quota. Never finish a reused
            # or already-claimed run on behalf of its actual executor.
            if run["created"]:
                try:
                    store.fail_unclaimed_run(user, run["run_id"])
                except HTTPException as cleanup_error:
                    if cleanup_error.status_code != 404:
                        raise
                    from .runtime import Outcome, _retire_removed_owner

                    _retire_removed_owner(Outcome(user, run["run_id"], "unclaimed-http", "cancelled"))
            raise
        # Duplicate running or terminal requests only replay. An unclaimed queued
        # request may be scheduled again; the transaction claim prevents execution twice.
        if run["status"] == "queued":
            get_service(manager).start(user, run, dict(model_cfg), parent=request_lease)
    return stream_run(user, run["run_id"])


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request):
    return {"run": store.get_run(require_auth(request), run_id)}


@router.get("/runs/{run_id}/events")
async def events(run_id: str, request: Request, after_seq: int = 0):
    user = require_auth(request)
    run = store.get_run(user, run_id)
    return {"run": run, "events": store.list_events(user, run_id, after_seq), "last_seq": run["last_seq"]}


@router.get("/runs/{run_id}/stream")
async def stream(run_id: str, request: Request, after_seq: int = 0):
    if after_seq < 0:
        raise HTTPException(422, "事件序号无效")
    return stream_run(require_auth(request), run_id, after_seq=after_seq)


@router.post("/chat/cancel")
async def cancel(request: Request):
    user = require_auth(request)
    try:
        req = CancelRequest(**(await request.json() or {}))
    except Exception as error:
        return JSONResponse({"error": validation_error_message(error)}, status_code=422)
    if req.run_id:
        run = store.get_run(user, req.run_id)
        if run["conv_id"] != req.conv_id:
            raise HTTPException(404, "研究任务不存在或不可访问")
    else:
        runs = store.list_runs(user, req.conv_id, limit=1)
        if not runs:
            raise HTTPException(404, "研究任务不存在或不可访问")
        run = runs[0]
    service = get_service(get_manager(getattr(request, "scope", None)))
    current = service.cancel(user, run["run_id"])
    return {
        "ok": True,
        "run_id": run["run_id"],
        "status": current["status"],
        "cancel_requested": current["cancel_requested"],
        "stopped": current["status"] in store.TERMINAL,
    }
