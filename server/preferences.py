"""Explicit, versioned research preferences; never inferred from conversations."""

from __future__ import annotations

import json
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .auth import require_auth, verify_csrf
from .database import get_db

router = APIRouter(prefix="/api/preferences", tags=["preferences"])


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: int = Field(ge=0)
    style: Literal["conservative", "balanced", "aggressive"]
    horizon: Literal["short", "medium", "long"]
    max_position_pct: int = Field(ge=1, le=100)
    max_drawdown_pct: int = Field(ge=1, le=50)
    allow_leverage: bool = False


def init_preferences_db():
    with get_db() as db:
        if db.in_transaction:
            raise RuntimeError("profile schema requires its own transaction")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS research_profiles (
            username TEXT PRIMARY KEY, version INTEGER NOT NULL,
            profile_json TEXT NOT NULL, updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS native_run_profiles (
            run_id TEXT PRIMARY KEY, username TEXT NOT NULL, profile_json TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS profile_account_delete AFTER DELETE ON users BEGIN
            DELETE FROM research_profiles WHERE username=OLD.username;
            DELETE FROM native_run_profiles WHERE username=OLD.username;
        END;
        """)


def profile_in_transaction(db, owner):
    row = db.execute("SELECT profile_json FROM research_profiles WHERE username=?", [owner]).fetchone()
    return json.loads(row[0]) if row else {"configured": False, "version": 0}


def snapshot_run(db, owner, run_id):
    value = profile_in_transaction(db, owner)
    db.execute("INSERT INTO native_run_profiles VALUES(?,?,?)", [run_id, owner, json.dumps(value)])


def run_profile(db, owner, run_id):
    row = db.execute(
        "SELECT profile_json FROM native_run_profiles WHERE run_id=? AND username=?", [run_id, owner]
    ).fetchone()
    return json.loads(row[0]) if row else {"configured": False, "version": 0}


def profile_prompt(value):
    if not value.get("configured"):
        return "\n用户尚未设置研究偏好，不推断其承受能力；涉及个人仓位时先说明缺少风险约束。"
    return (
        "\n以下是用户主动保存、本轮固定的研究偏好（不是账户资金或持仓证明）："
        + json.dumps(value, ensure_ascii=False, sort_keys=True)
        + "。按这些约束比较情景；风险上限不能解释为可保证的止损或收益。"
        "缺少实际组合、可用资金、合约保证金或最新行情时，风险检查必须标记未评估。"
        "禁止因风格激进而跳过证据、忽略风险上限或建议超限；不允许杠杆时不要推荐杠杆交易。"
    )


def _owner(request):
    owner = require_auth(request)
    if request.headers.get("X-Prism-Account", owner) != owner:
        raise HTTPException(409, "账号已经切换，请刷新页面")
    return owner


@router.get("/research")
async def read_profile(request: Request):
    owner = _owner(request)
    init_preferences_db()
    with get_db() as db:
        return {"profile": profile_in_transaction(db, owner), "portfolio_risk_status": "not_evaluated"}


@router.put("/research")
async def save_profile(request: Request):
    from .productivity.routes import _body

    owner = _owner(request)
    verify_csrf(request)
    body = await _body(request, ProfileUpdate)
    if require_auth(request) != owner:
        raise HTTPException(401, "请重新登录")
    init_preferences_db()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if not db.execute(
                "SELECT 1 FROM users WHERE username=? AND disabled_at IS NULL", [owner]
            ).fetchone():
                raise HTTPException(401, "请重新登录")
            current = profile_in_transaction(db, owner)
            if current["version"] != body.expected_version:
                raise HTTPException(409, "偏好已在其他设备更新，请重新读取后修改")
            value = {
                **body.model_dump(exclude={"expected_version"}),
                "version": current["version"] + 1,
                "configured": True,
                "updated_at": int(time.time()),
            }
            db.execute(
                "INSERT INTO research_profiles VALUES(?,?,?,?) ON CONFLICT(username) DO UPDATE SET "
                "version=excluded.version,profile_json=excluded.profile_json,updated_at=excluded.updated_at",
                [owner, value["version"], json.dumps(value), value["updated_at"]],
            )
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    return {"profile": value, "portfolio_risk_status": "not_evaluated"}
