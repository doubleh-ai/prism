"""Account-owned, point-in-time futures research and judgment records."""

import json
from datetime import datetime
from typing import Annotated
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from . import (
    futures_freight_research,
    futures_generic_research,
    futures_jm_research,
    futures_meal_research,
    futures_ontology,
    futures_rb_research,
)
from .auth import require_account_context, require_auth
from .database import check_rate_limit, get_db

router = APIRouter(prefix="/api/futures/{symbol}", tags=["futures"])
SHANGHAI = ZoneInfo("Asia/Shanghai")
REPLAY_WINDOWS = {
    "JM": (datetime(2026, 11, 19, 16, 30, tzinfo=SHANGHAI), datetime(2026, 11, 20, 16, 30, tzinfo=SHANGHAI)),
    "RB": (datetime(2026, 11, 16, 16, 30, tzinfo=SHANGHAI), datetime(2026, 11, 21, 16, 30, tzinfo=SHANGHAI)),
    "M": (datetime(2026, 11, 16, 18, tzinfo=SHANGHAI), datetime(2026, 11, 23, 18, tzinfo=SHANGHAI)),
    "EC": (datetime(2026, 11, 17, 16, 30, tzinfo=SHANGHAI), datetime(2026, 11, 24, 16, 30, tzinfo=SHANGHAI)),
}
# Keep the exact original evaluators for the 4 weekly-survey varieties, and bind ontology engines to the other 42.
RULES = {
    **{
        sym: {1: futures_generic_research.for_symbol(sym)}
        for sym in futures_ontology.ONTOLOGY
        if sym not in {"JM", "RB", "M", "EC"}
    },
    "JM": {1: futures_jm_research},
    "RB": {1: futures_rb_research},
    "M": {1: futures_meal_research},
    "EC": {1: futures_freight_research},
}
DRAFT_SECONDS = 3600


def _instrument(symbol: str) -> str:
    code = symbol.upper()
    if code not in RULES:
        raise HTTPException(404, "此品种尚未开放判断记录")
    return code


Instrument = Annotated[str, Depends(_instrument)]


def _journal(symbol):
    # The old application reads every row of its dedicated table as a JM record.
    # Keeping JM there also preserves drafts and pending reviews across rollback.
    if symbol == "JM":
        return "jm_research_journal", "username=:owner"
    return "futures_research_journal", "username=:owner AND symbol=:symbol"


def _check_session(symbol: str, session: str):
    if session == "current":
        return
    if symbol in REPLAY_WINDOWS and session == REPLAY_WINDOWS[symbol][0].date().isoformat():
        return
    raise HTTPException(422, "此品种不支持该回放日期")


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=140)
    session: str = "current"


class SaveJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_id: str = Field(pattern="^[0-9a-f]{32}$")
    choice_id: str = Field(min_length=1, max_length=64)


def _owner(request: Request, response: Response) -> str:
    response.headers["Cache-Control"] = "private, no-store"
    owner = require_auth(request)
    require_account_context(request, owner)
    return owner


Owner = Annotated[str, Depends(_owner)]


def _now() -> datetime:
    return datetime.now(SHANGHAI)


def _missing() -> HTTPException:
    return HTTPException(404, "此记录不存在或不能访问")


def _encode(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _saved(row, symbol, *, include_review: bool = False) -> dict:
    doc = json.loads(row["snapshot_json"])
    choice = next(c for c in doc["choices"] if c["id"] == row["choice_id"])
    item = {
        "id": row["id"],
        "symbol": symbol,
        "rule_version": doc.get("rule_version", 1),
        "session": row["session"],
        "as_of": row["as_of"],
        "saved_at": row["saved_at"],
        "question": doc["question"],
        "headline": doc["headline"],
        "explanation": doc["explanation"],
        "evidence": doc["evidence"],
        "choice_id": choice["id"],
        "choice_label": choice["label"],
        "rule": choice["rule"],
    }
    if row["review_json"] is not None:
        review = json.loads(row["review_json"])
        if include_review or review.get("auto_reconciled"):
            item["auto_review"] = review
    return item


@router.post("/research")
async def research(body: ResearchRequest, owner: Owner, symbol: Instrument):
    _check_session(symbol, body.session)
    if not check_rate_limit(f"{symbol.lower()}_research:" + owner, max_req=12, window=60):
        raise HTTPException(429, "研究请求过于频繁，请稍后重试")
    if body.session == "current":
        cutoff = _now()
        end = None
    else:
        start, end = REPLAY_WINDOWS[symbol]
        cutoff = start
    version = max(RULES[symbol])
    analysis = await RULES[symbol][version].analyze(body.question, cutoff)
    document = {
        **analysis,
        "symbol": symbol,
        "rule_version": version,
        "question": body.question,
        "session": body.session,
        "as_of": cutoff.isoformat(),
    }
    if end is not None:
        document["review_as_of"] = end.isoformat()
    if not document["choices"]:
        return {**document, "analysis_id": None}

    key = uuid4().hex
    created = int(_now().timestamp())
    table, _ = _journal(symbol)
    columns, values = ("", "") if symbol == "JM" else (",symbol", ",:symbol")
    with get_db() as db:
        db.execute(f"DELETE FROM {table} WHERE expires_at<=?", (created,))
        inserted = db.execute(
            f"""INSERT INTO {table}(id,username,session,as_of,snapshot_json,expires_at{columns})
                SELECT :id,username,:session,:as_of,:snapshot,:expires_at{values}
                FROM users WHERE username=:owner AND disabled_at IS NULL""",
            {
                "id": key,
                "session": body.session,
                "as_of": document["as_of"],
                "snapshot": _encode(document),
                "expires_at": created + DRAFT_SECONDS,
                "owner": owner,
                "symbol": symbol,
            },
        )
        if not inserted.rowcount:
            raise HTTPException(401)
    return {**document, "analysis_id": key}


@router.post("/judgments")
async def save_judgment(body: SaveJudgment, owner: Owner, symbol: Instrument):
    now = _now()
    table, scope = _journal(symbol)
    query = f"SELECT * FROM {table} WHERE id=:id AND {scope}"
    params = {
        "id": body.analysis_id,
        "owner": owner,
        "symbol": symbol,
        "choice": body.choice_id,
        "saved": now.isoformat(),
        "now": int(now.timestamp()),
    }
    with get_db() as db:
        row = db.execute(query, params).fetchone()
        if not row:
            raise _missing()
        if not any(c["id"] == body.choice_id for c in json.loads(row["snapshot_json"])["choices"]):
            raise HTTPException(422, "请选择此次研究给出的判断")
        # The first writer wins. A retry with the same choice reads back the same record.
        saved = db.execute(
            f"""UPDATE {table} SET choice_id=:choice,saved_at=:saved,expires_at=NULL
                WHERE id=:id AND {scope} AND choice_id IS NULL AND expires_at>:now RETURNING *""",
            params,
        ).fetchone()
        if saved:
            return _saved(saved, symbol)
        current = db.execute(query, params).fetchone()
        if not current:
            raise _missing()
        if current["choice_id"] == body.choice_id:
            return _saved(current, symbol)
        if current["choice_id"] is not None:
            raise HTTPException(409, "这份判断已经保存，请重新查证以记录新的判断")
        raise HTTPException(410, "本次查证已过期，请重新查证")


@router.get("/judgments")
async def list_judgments(
    owner: Owner,
    symbol: Instrument,
    session: str = "current",
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    cursor: Annotated[str | None, Query(pattern="^[0-9a-f]{32}$")] = None,
    include_review: bool = False,
):
    _check_session(symbol, session)
    table, scope = _journal(symbol)
    sql = f"SELECT * FROM {table} WHERE {scope} AND session=:session AND saved_at IS NOT NULL"
    params = {"owner": owner, "symbol": symbol, "session": session, "cursor": cursor, "limit": limit + 1}
    with get_db() as db:
        if cursor:
            anchor = db.execute(sql + " AND id=:cursor", params).fetchone()
            if not anchor:
                raise _missing()
            sql += " AND (saved_at,id)<(:anchor,:cursor)"
            params["anchor"] = anchor["saved_at"]
        rows = db.execute(sql + " ORDER BY saved_at DESC,id DESC LIMIT :limit", params).fetchall()
    return {
        "items": [_saved(r, symbol, include_review=include_review) for r in rows[:limit]],
        "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
    }


@router.get("/judgments/{judgment_id}/review")
async def review_judgment(judgment_id: str, owner: Owner, symbol: Instrument):
    table, scope = _journal(symbol)
    query = f"SELECT * FROM {table} WHERE id=:id AND {scope} AND saved_at IS NOT NULL"
    params = {"id": judgment_id, "owner": owner, "symbol": symbol}
    with get_db() as db:
        row = db.execute(query, params).fetchone()
    if not row:
        raise _missing()
    if row["review_json"] is not None:
        return json.loads(row["review_json"])
    if not check_rate_limit(f"{symbol.lower()}_review:" + owner, max_req=12, window=60):
        raise HTTPException(429, "复核请求过于频繁，请稍后重试")

    current = row["session"] == "current"
    snapshot = json.loads(row["snapshot_json"])
    if current:
        cutoff = _now()
    elif "review_as_of" in snapshot:
        cutoff = datetime.fromisoformat(snapshot["review_as_of"])
    elif symbol == "JM" and row["session"] == "2026-11-19" and "rule_version" not in snapshot:
        # Pre-upgrade JM rows used this fixed historical cutoff.
        cutoff = datetime(2026, 11, 20, 16, 30, tzinfo=SHANGHAI)
    else:
        raise HTTPException(409, "缺少这条判断的历史复盘截止时间")
    engine = RULES[symbol].get(snapshot.get("rule_version", 1))
    if engine is None:
        raise HTTPException(409, "这条判断的原复盘规则暂不可用")
    choice = next(c for c in snapshot["choices"] if c["id"] == row["choice_id"])
    choice = {**choice, "tracking_started_at": row["saved_at"] if current else row["as_of"]}
    result = {**await engine.review(choice, cutoff), "as_of": cutoff.isoformat()}
    with get_db() as db:
        if result["status"] in {"supported", "contradicted", "observed"}:
            db.execute(
                f"""UPDATE {table} SET review_json=:result
                    WHERE id=:id AND {scope} AND saved_at IS NOT NULL AND review_json IS NULL""",
                {**params, "result": _encode(result)},
            )
        # Concurrent reviews share the first final outcome. A cancellation during lookup wins.
        latest = db.execute(query, params).fetchone()
        if not latest:
            raise _missing()
        return json.loads(latest["review_json"]) if latest["review_json"] is not None else result


@router.delete("/judgments/{judgment_id}", status_code=204)
async def remove_judgment(judgment_id: str, owner: Owner, symbol: Instrument):
    table, scope = _journal(symbol)
    with get_db() as db:
        deleted = db.execute(
            f"DELETE FROM {table} WHERE id=:id AND {scope} AND saved_at IS NOT NULL",
            {"id": judgment_id, "owner": owner, "symbol": symbol},
        )
        if not deleted.rowcount:
            raise _missing()
