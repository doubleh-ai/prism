"""Indicator API — translate, per-user library CRUD, evaluate, scan."""
import json
import time
import uuid
import logging
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from ..database import get_db, check_rate_limit
from ..auth import require_auth
from ..config import RATE_EXPLAIN_PER_MIN
from .dsl import evaluate, is_signal, DSLError, validate
from .translator import translate
from .models import (TranslateRequest, IndicatorCreate, IndicatorUpdate,
                     EvalRequest, ScanRequest)

log = logging.getLogger("prism.indicators")
router = APIRouter(prefix="/api/indicators")

# Built-in starter indicators, copied into each user's library on first visit.
BUILTIN = [
    {"name": "均线金叉(5/20)", "kind": "signal", "description": "5日线上穿20日线",
     "dsl": {"cross_up": [{"ma": 5}, {"ma": 20}]}},
    {"name": "均线死叉(5/20)", "kind": "signal", "description": "5日线下穿20日线",
     "dsl": {"cross_down": [{"ma": 5}, {"ma": 20}]}},
    {"name": "RSI超卖反弹", "kind": "signal", "description": "RSI14上穿30",
     "dsl": {"cross_up": [{"rsi": 14}, {"const": 30}]}},
    {"name": "放量突破", "kind": "signal", "description": "成交量>5日均量2倍",
     "dsl": {"gt": [{"vol": 0}, {"mul": [2, {"ma_vol": 5}]}]}},
    {"name": "MACD-DIF", "kind": "plot", "description": "MACD DIF线",
     "dsl": {"macd_dif": [12, 26]}},
    {"name": "BOLL上轨", "kind": "plot", "description": "20日布林上轨",
     "dsl": {"boll_upper": [20, 2]}},
]


def _row_to_dict(row):
    d = dict(row)
    try:
        d["dsl"] = json.loads(d.pop("dsl_json", "{}"))
    except Exception:
        d["dsl"] = {}
        d.pop("dsl_json", None)
    return d


def _load_owned(ind_id, username):
    with get_db() as db:
        row = db.execute("SELECT * FROM indicators WHERE id=? AND username=?",
                         [ind_id, username]).fetchone()
    if not row:
        raise HTTPException(404, "指标不存在")
    return _row_to_dict(row)


_PERIOD_MAP = {"daily": "1d", "weekly": "1w", "monthly": "1m",
               "1d": "1d", "1w": "1w", "1m": "1m"}


async def _build_df(ticker, period="1d", limit=150):
    """Fetch OHLCV + fundamentals for a ticker → (df, fund_dict)."""
    import pandas as pd
    from ..market.router import get_chain
    period = _PERIOD_MAP.get(period, "1d")
    try:
        chain = await get_chain()
        kls, _ = await chain.get_kline(ticker, period=period, limit=limit)
        if not kls:
            log.warning("kline empty for ticker=%r period=%s", ticker, period)
    except Exception as exc:
        log.warning("kline fetch failed for %s: %s", ticker, exc)
        return None, {}
    if not kls:
        return None, {}
    rows = []
    for k in kls:
        d = k.model_dump()
        rows.append({"t": d.get("timestamp"), "open": d.get("open"), "high": d.get("high"),
                     "low": d.get("low"), "close": d.get("close"), "volume": d.get("volume")})
    df = pd.DataFrame(rows)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    fund = {}
    try:
        f, _ = await chain.get_fundamentals(ticker)
        fd = f.model_dump()
        fund = {"pe": fd.get("pe_ttm"), "pb": fd.get("pb"), "roe": fd.get("roe"),
                "market_cap": fd.get("market_cap"), "revenue_growth": None,
                "dividend_yield": None}
    except Exception:
        pass
    return df, fund


@router.post("/translate")
async def translate_endpoint(req: TranslateRequest, request: Request):
    u = require_auth(request)
    if not check_rate_limit(f"indtr:{u}", 8, 60):
        return JSONResponse({"error": "请求太频繁，请稍后再试"}, status_code=429,
                            headers={"Retry-After": "60"})
    out = translate(req.input)
    if not out:
        return JSONResponse({"error": "翻译失败，请换种说法或在DSL里手动编辑"}, status_code=422)
    return out


@router.get("")
async def list_indicators(request: Request):
    u = require_auth(request)
    with get_db() as db:
        cnt = db.execute("SELECT COUNT(*) FROM indicators WHERE username=?", [u]).fetchone()[0]
        if cnt == 0:
            now = int(time.time())
            for b in BUILTIN:
                db.execute(
                    "INSERT INTO indicators(id,username,name,description,dsl_json,kind,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    [uuid.uuid4().hex[:16], u, b["name"], b["description"],
                     json.dumps(b["dsl"]), b["kind"], now, now],
                )
        rows = db.execute(
            "SELECT * FROM indicators WHERE username=? ORDER BY updated_at DESC", [u]
        ).fetchall()
    return {"indicators": [_row_to_dict(r) for r in rows]}


@router.post("")
async def create_indicator(req: IndicatorCreate, request: Request):
    u = require_auth(request)
    if not validate(req.dsl):
        raise HTTPException(400, "DSL 无效（含未知指标/操作符）")
    iid = uuid.uuid4().hex[:16]
    now = int(time.time())
    with get_db() as db:
        db.execute(
            "INSERT INTO indicators(id,username,name,description,dsl_json,kind,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [iid, u, req.name, req.description, json.dumps(req.dsl), req.kind, now, now],
        )
    return {"id": iid, "ok": True}


@router.put("/{ind_id}")
async def update_indicator(ind_id: str, req: IndicatorUpdate, request: Request):
    u = require_auth(request)
    _load_owned(ind_id, u)
    sets, params = [], []
    for fld in ("name", "description", "kind"):
        v = getattr(req, fld)
        if v is not None:
            sets.append(f"{fld}=?"); params.append(v)
    if req.dsl is not None:
        if not validate(req.dsl):
            raise HTTPException(400, "DSL 无效")
        sets.append("dsl_json=?"); params.append(json.dumps(req.dsl))
    if not sets:
        return {"ok": True}
    sets.append("updated_at=?"); params.append(int(time.time()))
    params.append(ind_id); params.append(u)
    with get_db() as db:
        db.execute(f"UPDATE indicators SET {','.join(sets)} WHERE id=? AND username=?", params)
    return {"ok": True}


@router.delete("/{ind_id}")
async def delete_indicator(ind_id: str, request: Request):
    u = require_auth(request)
    with get_db() as db:
        db.execute("DELETE FROM indicators WHERE id=? AND username=?", [ind_id, u])
    return {"ok": True}


@router.post("/{ind_id}/eval")
async def eval_indicator(ind_id: str, req: EvalRequest, request: Request):
    u = require_auth(request)
    ind = _load_owned(ind_id, u)
    df, fund = await _build_df(req.ticker, req.period)
    if df is None or len(df) == 0:
        log.warning("eval %s kline: empty/failed (ticker=%r, period=%s)", ind_id, req.ticker, req.period)
        return JSONResponse({"error": "无法获取K线数据（行情源不可达或代码无效）"}, status_code=404)
    try:
        res = evaluate(ind["dsl"], df, fund)
    except DSLError as exc:
        return JSONResponse({"error": "指标计算错误: %s" % exc}, status_code=400)
    ts = df["t"].tolist()
    if is_signal(res):
        triggers = [int(ts[i]) for i in range(len(res)) if bool(res.iloc[i])]
        return {"kind": "signal", "triggers": triggers[-60:],
                "latest": bool(res.iloc[-1]) if len(res) else False}
    vals = res.tolist()
    pts = [{"t": int(ts[i]), "v": float(vals[i])} for i in range(len(vals)) if vals[i] == vals[i]]
    return {"kind": "plot", "points": pts[-150:]}


@router.post("/{ind_id}/scan")
async def scan_indicator(ind_id: str, req: ScanRequest, request: Request):
    u = require_auth(request)
    ind = _load_owned(ind_id, u)
    if ind["kind"] != "signal":
        return JSONResponse({"error": "只能扫描 signal 类指标"}, status_code=400)
    tickers = [t for t in req.tickers if t][:30]
    matches = []
    for tk in tickers:
        df, fund = await _build_df(tk, "daily", 120)
        if df is None or len(df) == 0:
            continue
        try:
            res = evaluate(ind["dsl"], df, fund)
        except DSLError:
            continue
        window = res.iloc[-5:] if len(res) >= 5 else res
        if bool(window.fillna(False).any()):
            idxs = [i for i in range(len(res)) if bool(res.iloc[i])]
            tlast = int(df["t"].iloc[idxs[-1]]) if idxs else int(df["t"].iloc[-1])
            matches.append({"ticker": tk, "triggered_at": tlast,
                            "price": float(df["close"].iloc[-1])})
    return {"matches": matches, "scanned": len(tickers)}
