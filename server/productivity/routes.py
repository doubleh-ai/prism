"""Feature-gated Work API with authentication and no private input in errors."""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from ..auth import require_auth, verify_csrf
from . import store
from .models import CreateSpace, CreateTask, UpdateTask

router = APIRouter(prefix="/api/work", tags=["work"])
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


def _owner(request):
    store.require_enabled()
    owner = require_auth(request)
    verify_csrf(request)
    return owner


def _id(value):
    if not _UUID.fullmatch(value):
        raise HTTPException(404, "记录不存在")
    return value


def _query_int(value, default=None):
    if value is None:
        return default
    if not re.fullmatch(r"[0-9]{1,16}", value):
        raise HTTPException(422, "分页参数无效")
    return int(value)


async def _body(request, model):
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > 32768:
            raise HTTPException(413, "工作内容过长")

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
        raise HTTPException(422, "工作内容格式无效，请检查字段、长度、时间与时区") from None


@router.get("/overview")
async def get_overview(request: Request):
    owner = _owner(request)
    return store.overview(owner, limit=_query_int(request.query_params.get("limit"), 50))


@router.post("/spaces")
async def post_space(request: Request):
    owner = _owner(request)
    body = await _body(request, CreateSpace)
    if _owner(request) != owner:
        raise HTTPException(401, "请重新登录")
    return store.create_space(owner, body)


@router.get("/spaces/{workspace_id}/tasks")
async def get_tasks(workspace_id: str, request: Request):
    owner = _owner(request)
    return store.list_tasks(
        owner,
        _id(workspace_id),
        limit=_query_int(request.query_params.get("limit"), 50),
        before=_query_int(request.query_params.get("before")),
        status=request.query_params.get("status"),
    )


@router.post("/spaces/{workspace_id}/tasks")
async def post_task(workspace_id: str, request: Request):
    owner = _owner(request)
    body = await _body(request, CreateTask)
    if _owner(request) != owner:
        raise HTTPException(401, "请重新登录")
    return store.create_task(owner, _id(workspace_id), body)


@router.get("/spaces/{workspace_id}/tasks/{task_id}")
async def get_task(workspace_id: str, task_id: str, request: Request):
    return store.task_detail(_owner(request), _id(workspace_id), _id(task_id))


@router.patch("/spaces/{workspace_id}/tasks/{task_id}")
async def patch_task(workspace_id: str, task_id: str, request: Request):
    owner = _owner(request)
    body = await _body(request, UpdateTask)
    if _owner(request) != owner:
        raise HTTPException(401, "请重新登录")
    return store.update_task(owner, _id(workspace_id), _id(task_id), body)
