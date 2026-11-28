"""Versioned purpose routes to existing direct HTTP providers; never return keys.

Reservations are durable before network I/O. Failed and unknown calls consume
their reservation; configuration changes do not reset the daily usage ledger.
Only routes consumed by this application are configurable. Historical unused
settings and reservations stay intact; private operations own their own routes.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field, model_validator

from . import config
from .agent.provider import configuration_status
from .auth import require_admin, verify_csrf
from .database import get_db
from .request_validation import ClosedModel, read_model_body

router = APIRouter(prefix="/api/admin/model-routes", tags=["admin"])
Purpose = Literal["research"]
PURPOSES = {"research": "投资研究"}


class RouteUpdate(ClosedModel):
    expected_version: int = Field(ge=0)
    model_id: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    output_tokens_max: int = Field(default=8192, ge=128, le=16384)
    model_calls_max: int = Field(default=12, ge=1, le=20)
    wall_seconds_max: int = Field(default=480, ge=5, le=600)
    daily_calls_max: int = Field(default=200, ge=1, le=5000)

    @model_validator(mode="before")
    @classmethod
    def read_old_research_setting(cls, value):
        # Old deployment policies and cached admin pages included this disabled field.
        if isinstance(value, dict) and value.get("allow_private_work") is False:
            return {key: item for key, item in value.items() if key != "allow_private_work"}
        return value


_deployment_routes: dict[str, dict] | None = None


def validate_defaults(value):
    """Validate startup policy without opening or modifying any database.

    An explicit deployment policy closes unlisted purposes. Stored admin
    settings remain authoritative; reservations always use the durable ledger.
    """
    fields = set(RouteUpdate.model_fields) - {"expected_version"}
    # Existing deployment policies can still contain a retired Work setting.
    if type(value) is not dict or set(value) - (PURPOSES.keys() | {"work_planning"}):
        raise ValueError("model_route_defaults_invalid")
    result = {}
    for purpose, item in value.items():
        if purpose not in PURPOSES:
            continue
        if type(item) is not dict or fields != item.keys() - {"allow_private_work"}:
            raise ValueError("model_route_defaults_invalid")
        try:
            route = RouteUpdate.model_validate({"expected_version": 0, **item})
        except ValueError:
            raise ValueError("model_route_defaults_invalid") from None
        if route.model_id not in config.MODELS:
            raise ValueError("model_route_defaults_invalid")
        result[purpose] = route.model_dump(exclude={"expected_version"})
    return result


def defaults_installed():
    return _deployment_routes is not None


def install_defaults(value):
    """Attach one reviewed startup policy; never write an admin setting."""
    global _deployment_routes
    if defaults_installed():
        raise ValueError("model_route_defaults_already_installed")
    _deployment_routes = validate_defaults(value)


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
    if row:
        stored = json.loads(row[0])
        stored.pop("allow_private_work", None)
        return stored
    if _deployment_routes is not None:
        return {
            "purpose": purpose,
            "version": 0,
            "configured": True,
            "enabled": False,
            **_deployment_routes.get(purpose, {}),
        }
    return {"purpose": purpose, "version": 0, "configured": False}


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


def _require_purpose(purpose):
    if not isinstance(purpose, str) or purpose not in PURPOSES:
        raise HTTPException(422, "模型用途无效")


def _read_user_quota_in_tx(db, actor: str) -> dict:
    try:
        row = db.execute(
            "SELECT unlimited_quota, daily_calls_limit, monthly_budget_usd FROM user_model_settings WHERE username=?",
            [actor],
        ).fetchone()
    except Exception:
        return {"unlimited_quota": False, "daily_calls_limit": None, "monthly_budget_usd": None}
    if not row:
        return {"unlimited_quota": False, "daily_calls_limit": None, "monthly_budget_usd": None}
    return {
        "unlimited_quota": bool(row[0]),
        "daily_calls_limit": int(row[1]) if row[1] is not None else None,
        "monthly_budget_usd": float(row[2]) if row[2] is not None else None,
    }


def _actor_month_cost_in_tx(db, actor: str, month_prefix: str) -> float:
    from .billing_ops import _estimate_cost_usd

    rows = db.execute(
        """
        SELECT model_id, COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0)
        FROM model_request_reservations
        WHERE actor=? AND day LIKE ?
        GROUP BY model_id
        """,
        [actor, f"{month_prefix}%"],
    ).fetchall()
    return round(sum(_estimate_cost_usd(str(r[0] or ""), int(r[1] or 0), int(r[2] or 0)) for r in rows), 6)


def resolve(purpose, *, explicit_model=None):
    _require_purpose(purpose)
    init_model_routes_db()
    with get_db() as db:
        route = _route(db, purpose)
    if route.get("configured") and not route["enabled"]:
        raise HTTPException(503, "该用途的模型调用已停用")
    model_id = explicit_model or route.get("model_id") or config.DEFAULT_MODEL
    model = config.MODELS.get(model_id)
    if not configuration_status(model)["configured"]:
        raise HTTPException(503, "该用途尚未配置可用模型")
    return model_id, {**model, "_model_purpose": purpose}, route


def reserve(purpose, actor, request_id, model_id, *, expected_route_version, minimum_available=1):
    _require_purpose(purpose)
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
            user_quota = _read_user_quota_in_tx(db, actor)
            if not user_quota["unlimited_quota"]:
                if count + minimum_available > route.get("daily_calls_max", 1000):
                    raise HTTPException(429, "该用途已达到每日模型请求上限")
                u_daily = user_quota["daily_calls_limit"]
                if u_daily is not None:
                    u_count = db.execute(
                        "SELECT COUNT(*) FROM model_request_reservations WHERE actor=? AND day=?",
                        [actor, day],
                    ).fetchone()[0]
                    if u_count + minimum_available > u_daily:
                        raise HTTPException(
                            429, f"您今日模型调用已达专属上限（{u_daily} 次），请联系管理员调整"
                        )
                u_budget = user_quota["monthly_budget_usd"]
                if u_budget is not None:
                    m_cost = _actor_month_cost_in_tx(db, actor, day[:7])
                    if m_cost >= u_budget:
                        raise HTTPException(
                            429, f"您本月模型开销已达预算上限（${u_budget:.2f}），请联系管理员调整"
                        )
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
    _require_purpose(purpose)
    body = await read_model_body(request, RouteUpdate)
    if require_admin(request) != admin:
        raise HTTPException(401, "请重新登录")
    if body.model_id not in config.MODELS:
        raise HTTPException(422, "只能选择已登记的模型，凭证由私有配置管理")
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
