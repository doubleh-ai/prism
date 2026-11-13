"""Read retained Vibe research without connecting to the retired engine."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .auth import require_auth
from .config import VIBE_ENGINE_REVISION, VIBE_ENGINE_VERSION
from .database import get_db
from .vibe_engine import safe_engine_payload

router = APIRouter(prefix="/api/vibe")

_ENGINE_NAME = "HKUDS/Vibe-Trading"
_EXECUTION_NOTICE = "Vibe 新研究与追问暂不可用：隔离执行环境尚待验收。可查看本账号已保存的历史。"
_ACTIVE_STATUSES = {"queued", "running", "cancelling"}


def require_vibe_execution() -> None:
    """Fail closed until a separately isolated sidecar is accepted in a future release.

    This is a release policy, deliberately not an environment-variable switch.
    The legacy sidecar can execute generated code and shares a cross-user index.
    """
    raise HTTPException(503, _EXECUTION_NOTICE)


def execution_status() -> dict[str, Any]:
    return {
        "status": "execution_unavailable",
        "execution_available": False,
        "history_available": True,
        "detail": _EXECUTION_NOTICE,
    }


def _engine_meta() -> dict[str, str]:
    return {
        "name": _ENGINE_NAME,
        "version": VIBE_ENGINE_VERSION,
        "revision": VIBE_ENGINE_REVISION,
        "mode": "official_sidecar",
    }


def _row_to_plan(row: Any) -> dict[str, Any]:
    try:
        plan = json.loads(row["plan_json"])
    except Exception:
        plan = {}
    if not isinstance(plan, dict):
        plan = {}
    plan = safe_engine_payload(plan)
    # Older stored plans may contain raw provider failure bodies. Classify them
    # on read without rewriting the saved report or migrating historical data.
    if plan.get("status") == "failed":
        plan["stage_label"] = "研究引擎未能完成任务"
    for item in plan.get("activity") or []:
        if isinstance(item, dict) and item.get("status") in {"error", "failed", "warning"}:
            for key in ("detail", "summary", "result"):
                if key in item:
                    item[key] = "工具或引擎未返回可用结果，已保留数据缺口"
    plan["id"] = row["id"]
    plan["created_at"] = row["created_at"]
    # Missing/corrupt historical metadata is not evidence that execution finished.
    plan.setdefault("status", "unknown")
    plan.setdefault("engine", _engine_meta())
    plan.setdefault("activity", [])
    plan.setdefault("followups", [])
    if not isinstance(plan.get("quality"), dict):
        plan["quality"] = {}
    # Old persisted heuristic grades are not evidence of source verification either.
    if plan["quality"].get("grade") in {"证据较充分", "有工具验证"}:
        plan["quality"].update(
            grade="来源待核验", grade_basis="legacy_heuristic_not_verification", verified_sources=0
        )
    return plan


def _load_vibe_plan(plan_id: str, username: str | None = None) -> dict[str, Any] | None:
    with get_db() as db:
        if username is None:
            row = db.execute(
                "SELECT id, username, plan_json, created_at FROM vibe_plans WHERE id=?",
                [plan_id],
            ).fetchone()
        else:
            row = db.execute(
                "SELECT id, username, plan_json, created_at FROM vibe_plans WHERE id=? AND username=?",
                [plan_id, username],
            ).fetchone()
    if not row:
        return None
    plan = _row_to_plan(row)
    plan["_username"] = row["username"]
    return plan


def get_owned_plan(username: str, plan_id: str, *, recover: bool = True) -> dict:
    plan = _load_vibe_plan(plan_id, username)
    if not plan:
        raise HTTPException(404, "plan not found")
    # History reads never resume, poll, or reconcile the shared legacy sidecar.
    # Keep the persisted status unchanged: absence of monitoring is not cancellation.
    plan.pop("_username", None)
    return plan


def list_owned_plans(username: str, limit: int = 30) -> list[dict]:
    with get_db() as db:
        rows = db.execute(
            "SELECT id,plan_json,created_at FROM vibe_plans WHERE username=? ORDER BY created_at DESC LIMIT ?",
            [username, max(1, min(100, limit))],
        ).fetchall()
    plans = [_row_to_plan(row) for row in rows]
    for plan in plans:
        plan.pop("_username", None)
    return plans


@router.get("/engine")
async def get_vibe_engine(request: Request):
    require_auth(request)
    return {"engine": _engine_meta(), **execution_status()}


@router.get("/plans")
async def list_vibe_plans(request: Request, limit: int = 30):
    return {"plans": list_owned_plans(require_auth(request), limit)}


@router.get("/plans/{plan_id}")
async def get_vibe_plan(plan_id: str, request: Request):
    return {"plan": get_owned_plan(require_auth(request), plan_id)}


@router.post("/plans/{plan_id}/cancel")
async def cancel_vibe_plan(plan_id: str, request: Request):
    return await cancel_owned_plan(require_auth(request), plan_id)


async def cancel_owned_plan(username: str, plan_id: str) -> dict:
    """Historical sidecar records never authorize contacting a retired engine."""
    plan = get_owned_plan(username, plan_id, recover=False)
    return {
        "plan": plan,
        "cancelled": False,
        "cancel_requested": False,
        "detail": "旧研究引擎已停用，无法通过本平台确认旧任务是否停止；历史状态保持不变。",
    }


@router.delete("/plans/{plan_id}")
async def delete_vibe_plan(plan_id: str, request: Request):
    username = require_auth(request)
    plan = _load_vibe_plan(plan_id, username)
    if not plan:
        raise HTTPException(404, "plan not found")
    if plan.get("status") in _ACTIVE_STATUSES:
        raise HTTPException(409, "旧任务是否停止尚未确认，请先明确请求停止；暂不删除记录。")
    # Delete only this account's local history; never mutate shared sidecar data.
    with get_db() as db:
        db.execute("DELETE FROM vibe_plans WHERE id=? AND username=?", [plan_id, username])
    return {"ok": True}
