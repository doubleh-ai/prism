"""Indicator API: translate, per-user library CRUD, evaluate, scan."""

import json
import logging
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..auth import require_auth
from ..config import RATE_EXPLAIN_PER_MIN
from ..database import check_rate_limit, get_db
from . import scanner
from .dsl import DSLError, MissingData, evaluate, is_signal, validate
from .metadata import enrich_indicator
from .models import EvalRequest, IndicatorCreate, IndicatorUpdate, ScanRequest, TranslateRequest
from .translator import translate

log = logging.getLogger("prism.indicators")
router = APIRouter(prefix="/api/indicators")


def _param(key, label, value, min_v, max_v, step=1, unit=""):
    return {
        "key": key,
        "label": label,
        "value": value,
        "min": min_v,
        "max": max_v,
        "step": step,
        "unit": unit,
    }


def _default_plan(kind="signal"):
    return {
        "universe": "watchlist",
        "period": "1d",
        "lookback": 120,
        "trigger_window": 5,
        "combine": "all",
        "score": "recent_signal" if kind == "signal" else "latest_value",
        "direction": "desc",
        "markets": ["SH", "SZ", "BJ"],
        "boards": ["main", "gem", "star", "bse"],
        "exclude_st": True,
        "min_turnover": 0,
        "max_results": 100,
        "steps": ["当前 K 线验证", "自选股最近 5 根 K 线扫描", "结合流动性和基本面复核"],
        "risk_controls": ["避免只看单一技术信号", "异常停牌或无量数据需要人工复核"],
    }


def _default_visual(kind="signal", color=None):
    return {
        "style": "signal_track" if kind == "signal" else "overlay",
        "color": color or ("#22c55e" if kind == "signal" else "#60a5fa"),
        "marker": "▲" if kind == "signal" else "",
        "track": kind == "signal",
        "zone": False,
    }


INDICATOR_SEED_VERSION = 1


BUILTIN = [
    {
        "name": "多级均线放量共振(5/10/20)",
        "kind": "signal",
        "description": "MA5 上穿 MA10、MA10 上穿 MA20，且当日成交量大于前 5 日均量 3 倍",
        "dsl": {
            "and": [
                {
                    "and": [
                        {
                            "within": [
                                {
                                    "cross_up": [
                                        {"ma": {"param": ["fast", 5]}},
                                        {"ma": {"param": ["mid", 10]}},
                                    ]
                                },
                                {"param": ["sync_window", 1]},
                            ]
                        },
                        {
                            "within": [
                                {
                                    "cross_up": [
                                        {"ma": {"param": ["mid", 10]}},
                                        {"ma": {"param": ["slow", 20]}},
                                    ]
                                },
                                {"param": ["sync_window", 1]},
                            ]
                        },
                    ]
                },
                {
                    "gt": [
                        {"vol": 0},
                        {
                            "mul": [
                                {"param": ["multiplier", 3]},
                                {
                                    "ref": [
                                        {"ma_vol": {"param": ["baseline_window", 5]}},
                                        1,
                                    ]
                                },
                            ]
                        },
                    ]
                },
            ]
        },
        "params": {
            "items": [
                _param("fast", "短期均线", 5, 2, 30, 1, "日"),
                _param("mid", "中期均线", 10, 3, 120, 1, "日"),
                _param("slow", "长期均线", 20, 5, 250, 1, "日"),
                _param("sync_window", "交叉共振窗口", 1, 1, 10, 1, "日"),
                _param("baseline_window", "前序均量窗口", 5, 2, 60, 1, "日"),
                _param("multiplier", "放量倍数", 3, 1, 10, 0.1, "x"),
            ]
        },
        "plan": dict(
            _default_plan("signal"),
            universe="market",
            lookback=180,
            trigger_window=5,
            steps=[
                "计算三条均线",
                "检查两次上穿是否在共振窗口内",
                "用前序均量验证当日放量",
                "扫描全市场并按最近信号排序",
            ],
            risk_controls=[
                "三条均线参数建议保持短期 < 中期 < 长期",
                "成交量基准不包含触发当日",
                "停牌、复牌首日和除权数据需要人工复核",
            ],
        ),
        "visual": _default_visual("signal", "#d6332f"),
        "seed_version": 1,
    },
    {
        "name": "均线金叉(5/20)",
        "kind": "signal",
        "description": "5 日均线上穿 20 日均线",
        "dsl": {"cross_up": [{"ma": {"param": ["fast", 5]}}, {"ma": {"param": ["slow", 20]}}]},
        "params": {
            "items": [
                _param("fast", "短均线", 5, 2, 60, 1, "日"),
                _param("slow", "长均线", 20, 5, 250, 1, "日"),
            ]
        },
        "visual": _default_visual("signal", "#22c55e"),
    },
    {
        "name": "均线死叉(5/20)",
        "kind": "signal",
        "description": "5 日均线下穿 20 日均线",
        "dsl": {"cross_down": [{"ma": {"param": ["fast", 5]}}, {"ma": {"param": ["slow", 20]}}]},
        "params": {
            "items": [
                _param("fast", "短均线", 5, 2, 60, 1, "日"),
                _param("slow", "长均线", 20, 5, 250, 1, "日"),
            ]
        },
        "visual": _default_visual("signal", "#ef4444"),
    },
    {
        "name": "RSI超卖反弹",
        "kind": "signal",
        "description": "RSI 上穿低位阈值",
        "dsl": {"cross_up": [{"rsi": {"param": ["window", 14]}}, {"param": ["threshold", 30]}]},
        "params": {
            "items": [
                _param("window", "RSI周期", 14, 2, 60, 1, "日"),
                _param("threshold", "阈值", 30, 5, 50, 1),
            ]
        },
        "visual": _default_visual("signal", "#f59e0b"),
    },
    {
        "name": "放量突破",
        "kind": "signal",
        "description": "成交量超过 5 日均量 2 倍",
        "dsl": {
            "gt": [
                {"vol": 0},
                {"mul": [{"param": ["multiplier", 2]}, {"ma_vol": {"param": ["vol_window", 5]}}]},
            ]
        },
        "params": {
            "items": [
                _param("vol_window", "均量窗口", 5, 2, 60, 1, "日"),
                _param("multiplier", "放量倍数", 2, 1, 5, 0.1, "x"),
            ]
        },
        "visual": _default_visual("signal", "#ef4444"),
    },
    {
        "name": "MACD-DIF",
        "kind": "plot",
        "description": "MACD DIF 线",
        "dsl": {"macd_dif": [{"param": ["fast", 12]}, {"param": ["slow", 26]}]},
        "params": {
            "items": [
                _param("fast", "快线 EMA", 12, 3, 30, 1, "日"),
                _param("slow", "慢线 EMA", 26, 5, 80, 1, "日"),
            ]
        },
        "visual": _default_visual("plot", "#60a5fa"),
    },
    {
        "name": "BOLL上轨",
        "kind": "plot",
        "description": "20 日布林带上轨",
        "dsl": {"boll_upper": [{"param": ["window", 20]}, {"param": ["std", 2]}]},
        "params": {
            "items": [
                _param("window", "布林周期", 20, 5, 120, 1, "日"),
                _param("std", "标准差倍数", 2, 1, 4, 0.1),
            ]
        },
        "visual": _default_visual("plot", "#f59e0b"),
    },
]


def _json_load(raw, default):
    try:
        data = json.loads(raw or "")
        return data if isinstance(data, type(default)) else default
    except Exception:
        return default


def _json_dump(value):
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"))


def _insert_builtin(db, username, definition, now):
    db.execute(
        """
        INSERT INTO indicators(
            id,username,name,description,dsl_json,params_json,plan_json,visual_json,source,kind,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            uuid.uuid4().hex[:16],
            username,
            definition["name"],
            definition["description"],
            _json_dump(definition["dsl"]),
            _json_dump(definition.get("params")),
            _json_dump(definition.get("plan") or _default_plan(definition["kind"])),
            _json_dump(definition.get("visual") or _default_visual(definition["kind"])),
            "builtin",
            definition["kind"],
            now,
            now,
        ],
    )


def _ensure_builtin_library(db, username):
    """Seed new accounts and backfill newly released templates exactly once."""
    count = db.execute("SELECT COUNT(*) FROM indicators WHERE username=?", [username]).fetchone()[0]
    seed_row = db.execute(
        "SELECT version FROM indicator_seed_versions WHERE username=?", [username]
    ).fetchone()
    current = int(seed_row["version"] or 0) if seed_row else 0

    if seed_row is None and count == 0:
        pending = BUILTIN
    else:
        pending = [definition for definition in BUILTIN if int(definition.get("seed_version") or 0) > current]

    now = int(time.time())
    for definition in pending:
        exists = db.execute(
            "SELECT 1 FROM indicators WHERE username=? AND name=?",
            [username, definition["name"]],
        ).fetchone()
        if not exists:
            _insert_builtin(db, username, definition, now)

    db.execute(
        """
        INSERT INTO indicator_seed_versions(username,version,updated_at)
        VALUES(?,?,?)
        ON CONFLICT(username) DO UPDATE SET version=excluded.version,updated_at=excluded.updated_at
        """,
        [username, INDICATOR_SEED_VERSION, now],
    )


def _coerce_num(value):
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        s = value.strip()
        try:
            n = float(s)
            return int(n) if n.is_integer() else n
        except Exception:
            return value
    return value


def _param_values(params, override=None):
    out = {}
    if isinstance(params, dict):
        items = params.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("key"):
                    out[str(item["key"])] = _coerce_num(item.get("value"))
        else:
            for k, v in params.items():
                out[str(k)] = _coerce_num(v)
    if isinstance(override, dict):
        if isinstance(override.get("items"), list):
            for item in override["items"]:
                if isinstance(item, dict) and item.get("key"):
                    out[str(item["key"])] = _coerce_num(item.get("value"))
        else:
            for k, v in override.items():
                out[str(k)] = _coerce_num(v)
    return out


def _row_to_dict(row):
    d = dict(row)
    try:
        d["dsl"] = json.loads(d.pop("dsl_json", "{}") or "{}")
    except Exception:
        d["dsl"] = {}
        d.pop("dsl_json", None)
    d["params"] = _json_load(d.pop("params_json", "{}"), {})
    d["plan"] = _json_load(d.pop("plan_json", "{}"), {}) or _default_plan(d.get("kind", "signal"))
    d["visual"] = _json_load(d.pop("visual_json", "{}"), {}) or _default_visual(d.get("kind", "signal"))
    d["source"] = d.get("source") or ""
    return enrich_indicator(d)


def _load_owned(ind_id, username):
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM indicators WHERE id=? AND username=?",
            [ind_id, username],
        ).fetchone()
    if not row:
        raise HTTPException(404, "指标不存在")
    return _row_to_dict(row)


_PERIOD_MAP = {
    "daily": "1d",
    "weekly": "1w",
    "monthly": "1m",
    "1d": "1d",
    "1w": "1w",
    "1m": "1m",
}


def _df_from_kline_rows(kline_rows, limit=500):
    """Build an OHLCV DataFrame from already-rendered chart data."""
    if not kline_rows:
        return None
    import pandas as pd

    rows = []
    for k in kline_rows[-limit:]:
        if not isinstance(k, dict):
            continue
        close = k.get("close")
        volume = k.get("volume")
        amount = k.get("amount", k.get("turnover"))
        rows.append(
            {
                "t": k.get("timestamp") or k.get("ts") or k.get("t"),
                "open": k.get("open"),
                "high": k.get("high"),
                "low": k.get("low"),
                "close": close,
                "volume": volume,
                "amount": amount if amount is not None else None,
            }
        )
    if not rows:
        return None
    df = pd.DataFrame(rows)
    for c in ("t", "open", "high", "low", "close", "volume", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["t", "open", "high", "low", "close", "volume"])
    if df.empty:
        return None
    df = df.sort_values("t").tail(limit).reset_index(drop=True)
    df["t"] = df["t"].astype(int)
    df["amount"] = df["amount"].fillna(df["volume"] * df["close"])
    return df


async def _fund_for_ticker(chain, ticker):
    try:
        f, _ = await chain.get_fundamentals(ticker)
        fd = f.model_dump()
        return {
            "pe": fd.get("pe_ttm"),
            "pb": fd.get("pb"),
            "roe": fd.get("roe"),
            "market_cap": fd.get("market_cap"),
            "revenue_growth": None,
            "dividend_yield": None,
        }
    except Exception:
        return {}


async def _build_df(ticker, period="1d", limit=150, include_fundamentals=True):
    """Fetch OHLCV + fundamentals for a ticker -> (df, fund_dict)."""
    from ..market.router import get_chain

    period = _PERIOD_MAP.get(period, "1d")
    chain = get_chain()
    fallback_limits = []
    for n in (limit, 120):
        if n and n not in fallback_limits:
            fallback_limits.append(n)

    kls = []
    for fetch_limit in fallback_limits:
        try:
            kls, _ = await chain.get_kline(ticker, period=period, limit=fetch_limit)
        except Exception as exc:
            log.warning("kline fetch failed for %s period=%s limit=%s: %s", ticker, period, fetch_limit, exc)
            continue
        if kls:
            break
        log.warning("kline empty for ticker=%r period=%s limit=%s", ticker, period, fetch_limit)

    if not kls:
        return None, {}

    rows = []
    for k in kls:
        d = k.model_dump()
        rows.append(
            {
                "t": d.get("timestamp"),
                "open": d.get("open"),
                "high": d.get("high"),
                "low": d.get("low"),
                "close": d.get("close"),
                "volume": d.get("volume"),
                "amount": d.get("turnover"),
            }
        )
    df = _df_from_kline_rows(rows, limit=max(fallback_limits))
    if df is None:
        return None, {}

    fund = await _fund_for_ticker(chain, ticker) if include_fundamentals else {}
    return df, fund


def _event_reason(ind, idx, df, res):
    days_ago = max(0, len(df) - 1 - idx)
    if days_ago == 0:
        when = "最新 K 线触发"
    elif days_ago == 1:
        when = "上一根 K 线触发"
    else:
        when = f"{days_ago} 根 K 线前触发"
    price = float(df["close"].iloc[idx])
    return f"{ind['name']}：{when}，收盘价 {price:.2f}"


def _signal_events(ind, df, res, limit=60):
    res = res.fillna(False)
    visual = ind.get("visual") or {}
    color = visual.get("color") or "#22c55e"
    marker = visual.get("marker") or "▲"
    idxs = [i for i in range(len(res)) if bool(res.iloc[i])]
    events = []
    for idx in idxs[-limit:]:
        days_ago = max(0, len(df) - 1 - idx)
        events.append(
            {
                "t": int(df["t"].iloc[idx]),
                "label": marker,
                "name": ind["name"],
                "reason": _event_reason(ind, idx, df, res),
                "strength": max(30, 100 - days_ago * 12),
                "price": float(df["close"].iloc[idx]),
                "color": color,
            }
        )
    return events


def _signal_zones(ind, df, res, limit=10):
    res = res.fillna(False)
    visual = ind.get("visual") or {}
    color = visual.get("color") or "#22c55e"
    zones = []
    start = None
    for i in range(len(res)):
        hit = bool(res.iloc[i])
        if hit and start is None:
            start = i
        if start is not None and (not hit or i == len(res) - 1):
            end = i if hit and i == len(res) - 1 else i - 1
            zones.append(
                {
                    "start": int(df["t"].iloc[start]),
                    "end": int(df["t"].iloc[end]),
                    "name": ind["name"],
                    "color": color,
                }
            )
            start = None
    return zones[-limit:]


@router.post("/translate")
async def translate_endpoint(req: TranslateRequest, request: Request):
    u = require_auth(request)
    if not check_rate_limit(f"indtr:{u}", 8, RATE_EXPLAIN_PER_MIN):
        return JSONResponse(
            {"error": "请求太频繁，请稍后再试"}, status_code=429, headers={"Retry-After": "60"}
        )
    out = translate(req.input)
    if not out:
        return JSONResponse({"error": "翻译失败，请换种说法或在 DSL 里手动编辑"}, status_code=422)
    return enrich_indicator(out)


@router.get("")
async def list_indicators(request: Request):
    u = require_auth(request)
    with get_db() as db:
        _ensure_builtin_library(db, u)
        rows = db.execute(
            "SELECT * FROM indicators WHERE username=? ORDER BY updated_at DESC", [u]
        ).fetchall()
    return {"indicators": [_row_to_dict(r) for r in rows]}


@router.post("")
async def create_indicator(req: IndicatorCreate, request: Request):
    return create_owned_indicator(require_auth(request), req)


def create_owned_indicator(u: str, req: IndicatorCreate) -> dict:
    """Shared creation contract for the page and scoped platform tools."""
    if not validate(req.dsl, _param_values(req.params)):
        raise HTTPException(400, "DSL 无效（含未知指标/操作符或参数）")
    iid = uuid.uuid4().hex[:16]
    now = int(time.time())
    with get_db() as db:
        db.execute(
            """
            INSERT INTO indicators(
                id,username,name,description,dsl_json,params_json,plan_json,visual_json,source,kind,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                iid,
                u,
                req.name,
                req.description,
                _json_dump(req.dsl),
                _json_dump(req.params),
                _json_dump(req.plan or _default_plan(req.kind)),
                _json_dump(req.visual or _default_visual(req.kind)),
                req.source or "custom",
                req.kind,
                now,
                now,
            ],
        )
    return {"id": iid, "ok": True}


@router.put("/{ind_id}")
async def update_indicator(ind_id: str, req: IndicatorUpdate, request: Request):
    u = require_auth(request)
    current = _load_owned(ind_id, u)
    sets, params = [], []
    for fld in ("name", "description", "kind", "source"):
        v = getattr(req, fld)
        if v is not None:
            sets.append(f"{fld}=?")
            params.append(v)
    new_dsl = req.dsl if req.dsl is not None else current["dsl"]
    new_params = req.params if req.params is not None else current.get("params", {})
    if (req.dsl is not None or req.params is not None) and not validate(new_dsl, _param_values(new_params)):
        raise HTTPException(400, "DSL 无效")
    if req.dsl is not None:
        sets.append("dsl_json=?")
        params.append(_json_dump(req.dsl))
    if req.params is not None:
        sets.append("params_json=?")
        params.append(_json_dump(req.params))
    if req.plan is not None:
        sets.append("plan_json=?")
        params.append(_json_dump(req.plan))
    if req.visual is not None:
        sets.append("visual_json=?")
        params.append(_json_dump(req.visual))
    if not sets:
        return {"ok": True}
    sets.append("updated_at=?")
    params.append(int(time.time()))
    params.append(ind_id)
    params.append(u)
    with get_db() as db:
        db.execute(f"UPDATE indicators SET {','.join(sets)} WHERE id=? AND username=?", params)
    return {"ok": True}


@router.get("/universe/summary")
async def indicator_universe_summary(
    request: Request,
    markets: str = "SH,SZ,BJ",
    boards: str = "main,gem,star,bse",
    exclude_st: bool = True,
    max_universe: int = 0,
):
    require_auth(request)
    config = {
        "markets": [x.strip().upper() for x in markets.split(",") if x.strip()],
        "boards": [x.strip().lower() for x in boards.split(",") if x.strip()],
        "exclude_st": exclude_st,
        "max_universe": max(0, min(6000, max_universe)),
    }
    return await scanner.universe_summary(config)


@router.get("/scan-jobs")
async def indicator_scan_jobs(request: Request, limit: int = 10):
    u = require_auth(request)
    return {"jobs": scanner.list_jobs(u, limit)}


@router.get("/scan-jobs/{job_id}")
async def indicator_scan_job(job_id: str, request: Request):
    u = require_auth(request)
    scanner.reconcile_jobs(u)
    job = scanner.get_job(job_id, u)
    if not job:
        raise HTTPException(404, "扫描任务不存在")
    return {"job": job}


@router.delete("/scan-jobs/{job_id}")
async def delete_indicator_scan_job(job_id: str, request: Request):
    u = require_auth(request)
    if not scanner.delete_job(job_id, u):
        raise HTTPException(404, "扫描任务不存在")
    return {"ok": True}


@router.post("/{ind_id}/scan-jobs")
async def start_indicator_scan_job(ind_id: str, req: ScanRequest, request: Request):
    u = require_auth(request)
    ind = _load_owned(ind_id, u)
    if req.scope == "watchlist" and not req.tickers:
        raise HTTPException(400, "自选股票池为空")
    markets = {x.upper() for x in req.markets}
    boards = {x.lower() for x in req.boards}
    if not markets or not markets.issubset({"SH", "SZ", "BJ"}):
        raise HTTPException(400, "市场配置无效")
    if not boards or not boards.issubset({"main", "gem", "star", "bse"}):
        raise HTTPException(400, "板块配置无效")
    pvals = _param_values(ind.get("params"), req.params)
    if not validate(ind["dsl"], pvals):
        raise HTTPException(400, "指标参数无效")
    active = scanner.active_job(u)
    if active:
        return JSONResponse(
            status_code=409,
            content={"error": "已有选股任务运行中", "job": active},
        )
    config = req.model_dump()
    job = scanner.create_job(u, ind, config)
    scanner.start_job(job, ind, _build_df)
    return JSONResponse(status_code=202, content={"job": job})


@router.post("/scan-jobs/{job_id}/cancel")
async def cancel_indicator_scan_job(job_id: str, request: Request):
    u = require_auth(request)
    if not scanner.cancel_job(job_id, u):
        raise HTTPException(404, "扫描任务不存在")
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
    df = _df_from_kline_rows(req.kline or [], limit=500)
    fund = {}
    if df is None:
        df, fund = await _build_df(
            req.ticker, req.period, include_fundamentals=scanner.uses_fundamentals(ind["dsl"])
        )
    elif scanner.uses_fundamentals(ind["dsl"]):
        from ..market.router import get_chain

        fund = await _fund_for_ticker(get_chain(), req.ticker)
    if df is None or len(df) == 0:
        log.warning("eval %s kline: empty/failed (ticker=%r, period=%s)", ind_id, req.ticker, req.period)
        return JSONResponse({"error": "无法获取K线数据（行情源不可达或代码无效）"}, status_code=404)
    pvals = _param_values(ind.get("params"), req.params)
    try:
        res = evaluate(ind["dsl"], df, fund, pvals)
    except MissingData as exc:
        return JSONResponse({"error": str(exc), "status": "data_insufficient"}, status_code=422)
    except DSLError as exc:
        return JSONResponse({"error": f"指标计算错误: {exc!s}"}, status_code=400)
    if res.empty or bool(res.iloc[-1:].isna().any()):
        return JSONResponse(
            {"error": "最新指标预热或输入资料不足", "status": "data_insufficient"}, status_code=422
        )
    ts = df["t"].tolist()
    if is_signal(res):
        events = _signal_events(ind, df, res)
        triggers = [ev["t"] for ev in events]
        latest = bool(res.iloc[-1]) if len(res) else False
        return {
            "kind": "signal",
            "triggers": triggers,
            "events": events,
            "tracks": [
                {
                    "name": ind["name"],
                    "color": (ind.get("visual") or {}).get("color") or "#22c55e",
                    "events": events,
                }
            ],
            "zones": _signal_zones(ind, df, res),
            "latest": latest,
            "reason": events[-1]["reason"] if events else "最近没有触发",
            "strength": events[-1]["strength"] if events else 0,
            "visual": ind.get("visual") or _default_visual("signal"),
            "params": pvals,
        }
    vals = res.tolist()
    pts = [{"t": int(ts[i]), "v": float(vals[i])} for i in range(len(vals)) if vals[i] == vals[i]]
    latest = pts[-1]["v"] if pts else None
    return {
        "kind": "plot",
        "points": pts[-180:],
        "latest_value": latest,
        "visual": ind.get("visual") or _default_visual("plot"),
        "params": pvals,
    }


@router.post("/{ind_id}/scan")
async def scan_indicator(ind_id: str, req: ScanRequest, request: Request):
    u = require_auth(request)
    ind = _load_owned(ind_id, u)
    if req.scope == "market":
        return JSONResponse(
            {"error": "全市场扫描请使用后台 scan-jobs 接口"},
            status_code=409,
        )
    if ind["kind"] != "signal":
        return JSONResponse({"error": "只能扫描 signal 类指标"}, status_code=400)
    tickers = [t for t in req.tickers if t][:100]
    pvals = _param_values(ind.get("params"), req.params)
    matches = []
    for tk in tickers:
        df, fund = await _build_df(
            tk,
            req.period,
            req.lookback,
            include_fundamentals=scanner.uses_fundamentals(ind["dsl"]),
        )
        if df is None or len(df) == 0:
            continue
        turnover = float(df["amount"].iloc[-1]) if "amount" in df.columns else 0.0
        if req.min_turnover and turnover < req.min_turnover:
            continue
        try:
            res = evaluate(ind["dsl"], df, fund, pvals)
        except DSLError:
            continue
        window = res.iloc[-req.trigger_window :] if len(res) >= req.trigger_window else res
        if bool(window.fillna(False).any()):
            idxs = [i for i in range(len(res)) if bool(res.fillna(False).iloc[i])]
            idx = idxs[-1] if idxs else len(res) - 1
            days_ago = max(0, len(df) - 1 - idx)
            matches.append(
                {
                    "ticker": tk,
                    "triggered_at": int(df["t"].iloc[idx]),
                    "price": float(df["close"].iloc[-1]),
                    "turnover": turnover,
                    "strength": max(30, 100 - days_ago * 12),
                    "reason": _event_reason(ind, idx, df, res),
                }
            )
    matches.sort(key=lambda x: (x.get("strength", 0), x.get("triggered_at", 0)), reverse=True)
    return {"matches": matches, "scanned": len(tickers)}
