"""Versioned purpose routes to existing direct HTTP providers; never return keys.

Reservations are durable before network I/O. Failed and unknown calls consume
their reservation; configuration changes do not reset the daily usage ledger.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field

from . import config
from .agent.provider import configuration_status
from .auth import require_admin, verify_csrf
from .database import get_db
from .productivity.models import ClosedModel
from .productivity.routes import _body

router = APIRouter(prefix="/api/admin/model-routes", tags=["admin"])
Purpose = Literal["research", "work_planning", "feedback_triage", "suggestion_review", "validation"]
PURPOSES = {
    "research": "投资研究",
    "work_planning": "工作安排",
    "feedback_triage": "异常归类",
    "suggestion_review": "建议评审",
    "validation": "模型验收",
}


class RouteUpdate(ClosedModel):
    expected_version: int = Field(ge=0)
    model_id: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    allow_private_work: bool = False
    output_tokens_max: int = Field(default=1024, ge=128, le=2048)
    model_calls_max: int = Field(default=1, ge=1, le=6)
    wall_seconds_max: int = Field(default=60, ge=5, le=180)
    daily_calls_max: int = Field(default=50, ge=1, le=1000)


def init_model_routes_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS model_routes (
            purpose TEXT PRIMARY KEY, version INTEGER NOT NULL, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS model_request_reservations (
            request_id TEXT PRIMARY KEY, purpose TEXT NOT NULL, actor TEXT NOT NULL,
            route_version INTEGER NOT NULL, model_id TEXT NOT NULL, day TEXT NOT NULL,
            status TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER,
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS model_purpose_usage ON model_request_reservations(purpose,day);
        """)


def _route(db, purpose):
    row = db.execute("SELECT payload FROM model_routes WHERE purpose=?", [purpose]).fetchone()
    return json.loads(row[0]) if row else {"purpose": purpose, "version": 0, "configured": False}


def _choices():
    return [
        {
            "id": key,
            "name": value.get("name", key),
            **{
                k: v
                for k, v in configuration_status(value).items()
                if k in {"configured", "protocol", "endpoint_host"}
            },
        }
        for key, value in config.MODELS.items()
    ]


def resolve(purpose, *, explicit_model=None, private_work=False):
    if purpose not in PURPOSES:
        raise HTTPException(422, "模型用途无效")
    init_model_routes_db()
    with get_db() as db:
        route = _route(db, purpose)
    if route.get("configured") and not route["enabled"]:
        raise HTTPException(503, "该用途的模型调用已停用")
    if private_work and (not route.get("configured") or not route.get("allow_private_work")):
        raise HTTPException(409, "工作内容的模型处理尚未获配置授权，可继续手动安排")
    model_id = explicit_model or route.get("model_id") or config.DEFAULT_MODEL
    model = config.MODELS.get(model_id)
    if not configuration_status(model)["configured"]:
        raise HTTPException(503, "该用途尚未配置可用模型")
    return model_id, dict(model), route


def reserve(purpose, actor, request_id, model_id, *, expected_route_version, minimum_available=1):
    if type(minimum_available) is not int or minimum_available not in {1, 2}:
        raise HTTPException(422, "模型预留范围无效")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", request_id):
        raise HTTPException(422, "模型请求编号无效")
    init_model_routes_db()
    day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            route = _route(db, purpose)
            if route["version"] != expected_route_version or (
                route.get("configured") and not route["enabled"]
            ):
                raise HTTPException(409, "模型路由已更新，请重新核对")
            old = db.execute(
                "SELECT 1 FROM model_request_reservations WHERE request_id=?", [request_id]
            ).fetchone()
            if old:
                raise HTTPException(409, "该模型请求已经预留，不会自动再次发送")
            count = db.execute(
                "SELECT COUNT(*) FROM model_request_reservations WHERE purpose=? AND day=?", [purpose, day]
            ).fetchone()[0]
            if count + minimum_available > route.get("daily_calls_max", 1000):
                raise HTTPException(429, "该用途已达到每日模型请求上限")
            db.execute(
                "INSERT INTO model_request_reservations VALUES(?,?,?,?,?,?,'unknown',NULL,NULL,?)",
                [request_id, purpose, actor, expected_route_version, model_id, day, int(time.time())],
            )
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise


def finish(request_id, *, input_tokens=None, output_tokens=None, complete=False):
    # Missing values stay NULL. They are never fabricated from estimates.
    if any(v is not None and (type(v) is not int or v < 0) for v in (input_tokens, output_tokens)):
        raise ValueError("invalid model usage")
    with get_db() as db:
        db.execute(
            "UPDATE model_request_reservations SET status=?,input_tokens=?,output_tokens=? "
            "WHERE request_id=? AND status='unknown'",
            ["complete" if complete else "unknown", input_tokens, output_tokens, request_id],
        )


def snapshot():
    init_model_routes_db()
    day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    with get_db() as db:
        routes = []
        for purpose, label in PURPOSES.items():
            route = _route(db, purpose)
            usage = db.execute(
                "SELECT COUNT(*) AS calls,SUM(status='unknown') AS unknown_calls,"
                "SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens "
                "FROM model_request_reservations WHERE purpose=? AND day=?",
                [purpose, day],
            ).fetchone()
            routes.append({**route, "label": label, "usage": dict(usage)})
        return {
            "routes": routes,
            "models": _choices(),
            "usage_day": day,
            "notice": "请求配额按真实预留计数；未知用量保留。套餐覆盖和金额未验证时不展示精确费用。",
        }


@router.get("")
async def list_routes(request: Request):
    require_admin(request)
    return snapshot()


@router.put("/{purpose}")
async def save_route(purpose: Purpose, request: Request):
    admin = require_admin(request)
    verify_csrf(request)
    body = await _body(request, RouteUpdate)
    if require_admin(request) != admin:
        raise HTTPException(401, "请重新登录")
    if body.model_id not in config.MODELS:
        raise HTTPException(422, "只能选择已登记的模型，凭证由私有配置管理")
    if body.allow_private_work and purpose != "work_planning":
        raise HTTPException(422, "只有工作安排用途可以申请处理本次工作要求")
    init_model_routes_db()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            current = _route(db, purpose)
            if current["version"] != body.expected_version:
                raise HTTPException(409, "模型设置已在其他页面更新，请重新读取")
            value = {
                **body.model_dump(exclude={"expected_version"}),
                "purpose": purpose,
                "configured": True,
                "version": current["version"] + 1,
                "updated_at": int(time.time()),
            }
            db.execute(
                "INSERT INTO model_routes VALUES(?,?,?) ON CONFLICT(purpose) DO UPDATE SET "
                "version=excluded.version,payload=excluded.payload",
                [purpose, value["version"], json.dumps(value)],
            )
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    return {"route": value}
