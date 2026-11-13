"""Synthetic persisted history, independent of the retired Vibe executor."""

import json

from server.database import get_db


def historical_plan(prompt):
    return {
        "id": "",
        "prompt": prompt,
        "title": prompt,
        "market": "A股",
        "horizon": "swing",
        "risk_level": "balanced",
        "created_at": 1,
        "status": "running",
        "quality": {},
        "activity": [],
        "followups": [],
    }


def insert_historical_plan(username, plan):
    with get_db() as db:
        db.execute(
            "INSERT INTO vibe_plans(id, username, prompt, market, horizon, risk_level, plan_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [
                plan["id"],
                username,
                plan["prompt"],
                plan["market"],
                plan["horizon"],
                plan["risk_level"],
                json.dumps(plan, ensure_ascii=False),
                plan["created_at"],
            ],
        )


def update_historical_plan(username, plan_id, plan):
    with get_db() as db:
        db.execute(
            "UPDATE vibe_plans SET plan_json=? WHERE id=? AND username=?",
            [json.dumps(plan, ensure_ascii=False), plan_id, username],
        )
