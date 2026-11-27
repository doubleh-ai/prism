"""Account-scoped watchlists shared by the UI and research capabilities."""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from .auth import require_auth
from .database import get_db

router = APIRouter(prefix="/api")


class WatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str = Field(pattern=r"^[A-Za-z0-9.\-]{1,16}$")
    name: str = Field(default="", max_length=80)


class WatchlistUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[WatchItem] = Field(max_length=200)


def get_watchlist(username: str) -> list[dict]:
    with get_db() as db:
        rows = db.execute(
            "SELECT ticker,name FROM user_watchlist WHERE username=? ORDER BY position,ticker", [username]
        ).fetchall()
    return [dict(row) for row in rows]


def set_watchlist(username: str, items: list[dict]) -> list[dict]:
    request = WatchlistUpdate(items=items)
    unique = {item.ticker: item for item in request.items}
    now = int(time.time())
    with get_db() as db:
        # One transaction prevents a failed replace from silently dropping items.
        db.execute("SAVEPOINT watchlist_replace")
        try:
            db.execute("DELETE FROM user_watchlist WHERE username=?", [username])
            db.executemany(
                "INSERT INTO user_watchlist(username,ticker,name,position,updated_at) VALUES(?,?,?,?,?)",
                [(username, item.ticker, item.name, i, now) for i, item in enumerate(unique.values())],
            )
            db.execute("RELEASE watchlist_replace")
        except BaseException:
            db.execute("ROLLBACK TO watchlist_replace")
            db.execute("RELEASE watchlist_replace")
            raise
    return get_watchlist(username)


@router.get("/watchlist")
async def api_watchlist(request: Request):
    return {"items": get_watchlist(require_auth(request))}


@router.put("/watchlist")
async def api_replace_watchlist(body: WatchlistUpdate, request: Request):
    return {"items": set_watchlist(require_auth(request), [item.model_dump() for item in body.items])}
