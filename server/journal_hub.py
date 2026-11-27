"""Unified cross-asset judgment & auto-reconcile ledger (Futures V2 + A-Share Equities)."""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from . import futures, futures_panel
from .auth import require_account_context, require_auth
from .database import check_rate_limit, get_db

router = APIRouter(prefix="/api/journal", tags=["journal"])
SHANGHAI = ZoneInfo("Asia/Shanghai")


def init_journal_hub_db(db=None) -> None:
    sql = """
    CREATE TABLE IF NOT EXISTS equity_research_journal (
        id TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        ticker TEXT NOT NULL,
        name TEXT NOT NULL,
        direction TEXT NOT NULL,
        reference_price REAL NOT NULL,
        invalidation_price REAL,
        target_price REAL,
        horizon_days INTEGER NOT NULL DEFAULT 10,
        thesis TEXT NOT NULL,
        as_of TEXT NOT NULL,
        saved_at TEXT NOT NULL,
        review_json TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_equity_journal_owner
        ON equity_research_journal(username, saved_at DESC);
    CREATE TRIGGER IF NOT EXISTS equity_journal_account_delete AFTER DELETE ON users BEGIN
        DELETE FROM equity_research_journal WHERE username=OLD.username;
    END;
    """
    if db is not None:
        db.executescript(sql)
        return
    with get_db() as conn:
        conn.executescript(sql)


def _table_exists(db, table_name: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        [table_name],
    ).fetchone()
    return row is not None


def _decode_json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except (TypeError, ValueError):
        return {}


def _futures_name(symbol: str) -> str:
    return str(futures_panel._MARKET_NAMES.get(symbol) or (futures.PRODUCTS.get(symbol) or (symbol,))[0])


def _format_futures_row(row, symbol: str) -> dict | None:
    doc = _decode_json(row["snapshot_json"])
    choices = doc.get("choices") if isinstance(doc.get("choices"), list) else []
    choice = next((c for c in choices if isinstance(c, dict) and c.get("id") == row["choice_id"]), None)
    if not choice:
        return None
    raw_rule = choice.get("rule")
    if isinstance(raw_rule, dict):
        rule_summary = str(raw_rule.get("summary") or raw_rule.get("label") or choice.get("summary") or "")
    elif isinstance(raw_rule, str):
        rule_summary = raw_rule
    else:
        rule_summary = str(choice.get("summary") or "")
    review = _decode_json(row["review_json"]) if row["review_json"] else None
    status = review.get("status") if isinstance(review, dict) else "pending"
    return {
        "id": row["id"],
        "asset_class": "futures",
        "code": symbol,
        "name": _futures_name(symbol),
        "session": row["session"],
        "as_of": row["as_of"],
        "saved_at": row["saved_at"],
        "headline": str(doc.get("headline") or doc.get("question") or ""),
        "question": str(doc.get("question") or ""),
        "choice_label": str(choice.get("label") or ""),
        "rule_summary": rule_summary,
        "status": status if status in {"supported", "contradicted", "observed"} else "pending",
        "review": review,
        "href": f"/futures?symbol={symbol}",
    }


def _format_equity_row(row) -> dict:
    review = _decode_json(row["review_json"]) if row["review_json"] else None
    status = review.get("status") if isinstance(review, dict) else "pending"
    direction_labels = {"bullish": "看多", "bearish": "看空", "watch": "观察等待"}
    parts = [f"参考价 {row['reference_price']:.2f}"]
    if row["target_price"] is not None:
        parts.append(f"目标 {row['target_price']:.2f}")
    if row["invalidation_price"] is not None:
        parts.append(f"失效 {row['invalidation_price']:.2f}")
    parts.append(f"观察窗 {row['horizon_days']} 日")
    return {
        "id": row["id"],
        "asset_class": "equity",
        "code": row["ticker"],
        "name": row["name"] or row["ticker"],
        "direction": row["direction"],
        "as_of": row["as_of"],
        "saved_at": row["saved_at"],
        "headline": row["thesis"],
        "question": f"{row['name']} ({row['ticker']}) · {direction_labels.get(row['direction'], row['direction'])}",
        "choice_label": f"{direction_labels.get(row['direction'], row['direction'])} · {' / '.join(parts)}",
        "reference_price": row["reference_price"],
        "invalidation_price": row["invalidation_price"],
        "target_price": row["target_price"],
        "horizon_days": row["horizon_days"],
        "status": status if status in {"supported", "contradicted", "observed"} else "pending",
        "review": review,
        "href": f"/prism?mode=market&ticker={row['ticker']}",
    }


def collect_user_journal_in_tx(db, owner: str, *, limit: int = 80) -> tuple[dict, list[dict]]:
    items: list[dict] = []
    if _table_exists(db, "jm_research_journal"):
        for r in db.execute(
            "SELECT * FROM jm_research_journal WHERE username=? AND saved_at IS NOT NULL ORDER BY saved_at DESC LIMIT ?",
            [owner, limit],
        ).fetchall():
            parsed = _format_futures_row(r, "JM")
            if parsed:
                items.append(parsed)
    if _table_exists(db, "futures_research_journal"):
        for r in db.execute(
            "SELECT * FROM futures_research_journal WHERE username=? AND saved_at IS NOT NULL ORDER BY saved_at DESC LIMIT ?",
            [owner, limit],
        ).fetchall():
            parsed = _format_futures_row(r, str(r["symbol"] or "").upper())
            if parsed:
                items.append(parsed)
    if _table_exists(db, "equity_research_journal"):
        for r in db.execute(
            "SELECT * FROM equity_research_journal WHERE username=? ORDER BY saved_at DESC LIMIT ?",
            [owner, limit],
        ).fetchall():
            items.append(_format_equity_row(r))

    items.sort(key=lambda item: str(item.get("saved_at") or ""), reverse=True)
    items = items[:limit]

    counts = {"supported": 0, "contradicted": 0, "observed": 0, "pending": 0}
    futures_count = 0
    equity_count = 0
    for item in items:
        st = item.get("status") or "pending"
        counts[st if st in counts else "pending"] += 1
        if item["asset_class"] == "futures":
            futures_count += 1
        else:
            equity_count += 1

    decided = counts["supported"] + counts["contradicted"]
    win_rate = round(counts["supported"] * 100.0 / decided, 1) if decided > 0 else None
    if counts["contradicted"] >= 2 and counts["contradicted"] > counts["supported"]:
        bias_hint = "近期证伪触发次数高于印证次数，建议收紧追涨分位阈值并预留更明确的安全边际。"
    elif decided >= 3 and win_rate is not None and win_rate >= 65.0:
        bias_hint = "近期已对账判断印证率较高，继续保持预设失效线与周期纪律。"
    elif len(items) > 0:
        bias_hint = "已有在途跟踪判断，建议结合最新基差、量价或财报披露验证预设失效条件。"
    else:
        bias_hint = "尚未记录多空判断；可在个股看盘或期货品种页随手记下观点，系统将按真实行情自动对账。"

    stats = {
        "total": len(items),
        "futures_total": futures_count,
        "equity_total": equity_count,
        "reconciled": counts["supported"] + counts["contradicted"] + counts["observed"],
        "supported": counts["supported"],
        "contradicted": counts["contradicted"],
        "observed": counts["observed"],
        "pending": counts["pending"],
        "win_rate_pct": win_rate,
        "bias_hint": bias_hint,
    }
    return stats, items


def summarize_user_journal_in_tx(db, owner: str) -> dict:
    stats, _ = collect_user_journal_in_tx(db, owner, limit=60)
    return stats


def _owner(request: Request, response: Response) -> str:
    response.headers["Cache-Control"] = "private, no-store"
    owner = require_auth(request)
    require_account_context(request, owner)
    return owner


Owner = Annotated[str, Depends(_owner)]


class EquityJudgmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    ticker: str = Field(pattern=r"^[A-Za-z0-9.\-]{1,16}$")
    name: str = Field(default="", max_length=64)
    direction: Literal["bullish", "bearish", "watch"]
    reference_price: float = Field(gt=0, le=1_000_000)
    invalidation_price: float | None = Field(default=None, gt=0, le=1_000_000)
    target_price: float | None = Field(default=None, gt=0, le=1_000_000)
    horizon_days: int = Field(default=10, ge=1, le=365)
    thesis: str = Field(min_length=2, max_length=500)


@router.get("/overview")
async def journal_overview(owner: Owner, limit: int = 60):
    bounded = max(1, min(int(limit or 60), 120))
    init_journal_hub_db()
    with get_db() as db:
        stats, items = collect_user_journal_in_tx(db, owner, limit=bounded)
    return {"stats": stats, "items": items}


@router.post("/equity")
async def create_equity_judgment(body: EquityJudgmentCreate, owner: Owner):
    if not check_rate_limit(f"equity_journal:{owner}", max_req=15, window=60):
        raise HTTPException(429, "记录过于频繁，请稍后再试")
    if (
        body.direction == "bullish"
        and body.invalidation_price
        and body.invalidation_price >= body.reference_price
    ):
        raise HTTPException(422, "看多判断的失效止损价须低于当前参考价")
    if (
        body.direction == "bearish"
        and body.invalidation_price
        and body.invalidation_price <= body.reference_price
    ):
        raise HTTPException(422, "看空判断的失效止损价须高于当前参考价")

    init_journal_hub_db()
    now_iso = datetime.now(SHANGHAI).isoformat(timespec="seconds")
    jid = secrets.token_hex(16)
    ticker = body.ticker.upper()
    with get_db() as db:
        inserted = db.execute(
            """INSERT INTO equity_research_journal(
                   id, username, ticker, name, direction, reference_price,
                   invalidation_price, target_price, horizon_days, thesis, as_of, saved_at
               )
               SELECT ?, username, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
               FROM users WHERE username=? AND disabled_at IS NULL""",
            [
                jid,
                ticker,
                body.name or ticker,
                body.direction,
                round(body.reference_price, 4),
                round(body.invalidation_price, 4) if body.invalidation_price is not None else None,
                round(body.target_price, 4) if body.target_price is not None else None,
                body.horizon_days,
                body.thesis,
                now_iso,
                now_iso,
                owner,
            ],
        )
        if not inserted.rowcount:
            raise HTTPException(401, "请重新登录")
        row = db.execute(
            "SELECT * FROM equity_research_journal WHERE id=? AND username=?", [jid, owner]
        ).fetchone()
    return {"ok": True, "item": _format_equity_row(row)}


@router.post("/equity/{judgment_id}/reconcile")
async def reconcile_equity_judgment(judgment_id: str, owner: Owner):
    if not check_rate_limit(f"equity_reconcile:{owner}", max_req=20, window=60):
        raise HTTPException(429, "对账请求过于频繁，请稍后重试")
    init_journal_hub_db()
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM equity_research_journal WHERE id=? AND username=?",
            [judgment_id, owner],
        ).fetchone()
    if not row:
        raise HTTPException(404, "判断记录不存在或无权访问")

    from .market.factory import get_chain

    chain = get_chain()
    quote, _stale = await chain.get_quote(row["ticker"])
    if not quote or quote.price is None or quote.price <= 0:
        raise HTTPException(503, "暂未获取到最新核验报价，保留原状态")

    latest = float(quote.price)
    ref = float(row["reference_price"])
    inv = float(row["invalidation_price"]) if row["invalidation_price"] is not None else None
    tgt = float(row["target_price"]) if row["target_price"] is not None else None
    move_pct = round((latest / ref - 1.0) * 100.0, 2)
    now_iso = datetime.now(SHANGHAI).isoformat(timespec="seconds")

    status = "observed"
    if row["direction"] == "bullish":
        if inv is not None and latest <= inv:
            status = "contradicted"
            summary = f"最新价 {latest:.2f} 已触及或跌破预设失效位 {inv:.2f}（区间变动 {move_pct:+.2f}%）。"
        elif (tgt is not None and latest >= tgt) or (tgt is None and move_pct >= 3.0):
            status = "supported"
            summary = f"最新价 {latest:.2f} 较记录时上涨 {move_pct:+.2f}%，印证看多方向。"
        else:
            summary = f"最新价 {latest:.2f}（较参考价 {move_pct:+.2f}%），尚未触发目标或失效边界。"
    elif row["direction"] == "bearish":
        if inv is not None and latest >= inv:
            status = "contradicted"
            summary = f"最新价 {latest:.2f} 已触及或突破预设失效位 {inv:.2f}（区间变动 {move_pct:+.2f}%）。"
        elif (tgt is not None and latest <= tgt) or (tgt is None and move_pct <= -3.0):
            status = "supported"
            summary = f"最新价 {latest:.2f} 较记录时回落 {move_pct:+.2f}%，印证看空回避判断。"
        else:
            summary = f"最新价 {latest:.2f}（较参考价 {move_pct:+.2f}%），仍在预设观察区间内。"
    else:
        summary = f"观察标的最新价 {latest:.2f}（较记录参考价变动 {move_pct:+.2f}%）。"

    review = {
        "status": status,
        "summary": summary,
        "latest_price": round(latest, 4),
        "move_pct": move_pct,
        "quote_source": quote.source,
        "as_of": now_iso,
        "auto_reconciled": True,
    }
    encoded = json.dumps(review, ensure_ascii=False, separators=(",", ":"))
    with get_db() as db:
        db.execute(
            "UPDATE equity_research_journal SET review_json=? WHERE id=? AND username=?",
            [encoded, judgment_id, owner],
        )
        updated = db.execute(
            "SELECT * FROM equity_research_journal WHERE id=? AND username=?",
            [judgment_id, owner],
        ).fetchone()
    if not updated:
        raise HTTPException(404, "判断记录不存在")
    return {"ok": True, "item": _format_equity_row(updated)}


@router.delete("/equity/{judgment_id}", status_code=204)
async def delete_equity_judgment(judgment_id: str, owner: Owner):
    init_journal_hub_db()
    with get_db() as db:
        deleted = db.execute(
            "DELETE FROM equity_research_journal WHERE id=? AND username=?",
            [judgment_id, owner],
        )
    if not deleted.rowcount:
        raise HTTPException(404, "判断记录不存在或无权访问")
