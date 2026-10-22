"""Persistent background scanner for indicator-based A-share selection."""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import math
import re
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

from ..database import get_db
from ..work_lifecycle import WorkHandle, get_manager
from .dsl import DSLError, MissingData, evaluate, is_signal

log = logging.getLogger("prism.indicator-scanner")

_UNIVERSE_CACHE: list[dict[str, str]] = []
_UNIVERSE_CACHE_AT = 0.0
_UNIVERSE_TTL = 1800
_TASKS: dict[str, WorkHandle] = {}
_CANCEL_REQUESTED: set[str] = set()
_GATES: dict[int, asyncio.Semaphore] = {}
_SINA_KLINE_URL = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
)


def _market_for_code(code: str) -> str:
    if code.startswith("6"):
        return "SH"
    if code.startswith(("4", "8", "92")):
        return "BJ"
    return "SZ"


def _board_for_code(code: str) -> str:
    if code.startswith(("4", "8", "92")):
        return "bse"
    if code.startswith(("300", "301")):
        return "gem"
    if code.startswith(("688", "689")):
        return "star"
    return "main"


def _universe_from_frame(frame) -> list[dict[str, str]]:
    if frame is None or getattr(frame, "empty", True):
        return []
    code_col = next((c for c in ("code", "证券代码", "股票代码") if c in frame.columns), None)
    name_col = next((c for c in ("name", "证券简称", "股票简称") if c in frame.columns), None)
    if not code_col:
        return []
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for _, raw in frame.iterrows():
        code = re.sub(r"\D", "", str(raw.get(code_col, ""))).zfill(6)
        if not re.fullmatch(r"\d{6}", code) or code in seen:
            continue
        market = _market_for_code(code)
        if market not in {"SH", "SZ", "BJ"}:
            continue
        seen.add(code)
        rows.append(
            {
                "ticker": code,
                "name": str(raw.get(name_col, code) if name_col else code).strip() or code,
                "market": market,
                "board": _board_for_code(code),
            }
        )
    return rows


def _load_universe_sync() -> list[dict[str, str]]:
    try:
        from ..market.akshare_provider import _stock_list_cache

        rows = _universe_from_frame(_stock_list_cache())
        if rows:
            return rows
    except Exception as exc:
        log.warning("akshare universe unavailable: %s", exc)

    try:
        import baostock as bs

        from ..market.baostock_provider import run_serial_transaction

        def load_baostock():
            login = bs.login()
            if getattr(login, "error_code", "1") != "0":
                return []
            try:
                rs = bs.query_stock_basic()
                data = []
                fields = list(getattr(rs, "fields", []) or [])
                while getattr(rs, "error_code", "1") == "0" and rs.next():
                    values = rs.get_row_data()
                    row = dict(zip(fields, values, strict=False))
                    if row.get("type") not in (None, "", "1") or row.get("status") == "0":
                        continue
                    code = str(row.get("code", "")).split(".")[-1]
                    data.append({"code": code, "name": row.get("code_name", code)})
                return data
            finally:
                bs.logout()

        data = run_serial_transaction(load_baostock)
        if data:
            import pandas as pd

            return _universe_from_frame(pd.DataFrame(data))
    except Exception as exc:
        log.warning("baostock universe unavailable: %s", exc)
    return []


async def load_universe() -> list[dict[str, str]]:
    global _UNIVERSE_CACHE, _UNIVERSE_CACHE_AT
    now = time.time()
    if _UNIVERSE_CACHE and now - _UNIVERSE_CACHE_AT < _UNIVERSE_TTL:
        return [dict(row) for row in _UNIVERSE_CACHE]
    rows = await get_manager().run_blocking("scan", _load_universe_sync)
    if rows:
        _UNIVERSE_CACHE = rows
        _UNIVERSE_CACHE_AT = now
    return [dict(row) for row in (_UNIVERSE_CACHE or rows)]


def filter_universe(rows: list[dict[str, str]], config: dict[str, Any]) -> list[dict[str, str]]:
    markets = {str(x).upper() for x in (config.get("markets") or ["SH", "SZ", "BJ"])}
    boards = {str(x).lower() for x in (config.get("boards") or ["main", "gem", "star", "bse"])}
    exclude_st = bool(config.get("exclude_st", True))
    out = []
    for row in rows:
        name = str(row.get("name") or "")
        if row.get("market") not in markets or row.get("board") not in boards:
            continue
        if exclude_st and ("ST" in name.upper() or "退" in name):
            continue
        out.append(row)
    limit = int(config.get("max_universe") or 0)
    return out[:limit] if limit else out


async def universe_summary(config: dict[str, Any]) -> dict[str, Any]:
    rows = await load_universe()
    filtered = filter_universe(rows, config)
    return {
        "total": len(rows),
        "selected": len(filtered),
        "markets": dict(Counter(row["market"] for row in filtered)),
        "boards": dict(Counter(row["board"] for row in filtered)),
    }


def _json(raw: str | None, default):
    try:
        value = json.loads(raw or "")
        return value if isinstance(value, type(default)) else default
    except Exception:
        return default


def _job_row(row) -> dict[str, Any] | None:
    if not row:
        return None
    job = dict(row)
    job["config"] = _json(job.pop("config_json", "{}"), {})
    job["matches"] = _json(job.pop("results_json", "[]"), [])
    total = max(0, int(job.get("total") or 0))
    processed = max(0, int(job.get("processed") or 0))
    job["progress"] = round(processed / total * 100, 1) if total else 0
    return job


def create_job(username: str, indicator: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    now = int(time.time())
    job_id = uuid.uuid4().hex[:16]
    with get_db() as db:
        db.execute("DELETE FROM indicator_scan_jobs WHERE created_at < ?", [now - 30 * 86400])
        db.execute(
            """
            INSERT INTO indicator_scan_jobs(
                id,username,indicator_id,indicator_name,status,config_json,results_json,
                total,processed,matched,failed,error,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                job_id,
                username,
                indicator["id"],
                indicator.get("name", ""),
                "queued",
                json.dumps(config, ensure_ascii=False),
                "[]",
                0,
                0,
                0,
                0,
                "",
                now,
                now,
            ],
        )
    return get_job(job_id, username)


def get_job(job_id: str, username: str) -> dict[str, Any] | None:
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM indicator_scan_jobs WHERE id=? AND username=?",
            [job_id, username],
        ).fetchone()
    return _job_row(row)


def list_jobs(username: str, limit: int = 10) -> list[dict[str, Any]]:
    reconcile_jobs(username)
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM indicator_scan_jobs WHERE username=? ORDER BY created_at DESC LIMIT ?",
            [username, max(1, min(30, int(limit)))],
        ).fetchall()
    return [_job_row(row) for row in rows]


def reconcile_jobs(username: str) -> None:
    """A lost process is interrupted, never silently queued for another execution."""
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM indicator_scan_jobs WHERE username=? AND status IN ('queued','running','cancelling')",
            [username],
        ).fetchall()
    for row in rows:
        task = _TASKS.get(row["id"])
        if task is None or task.done():
            _persist_job(
                _job_row(row), status="interrupted", error="服务重启或任务已失去执行上下文", completed=True
            )


def active_job(username: str) -> dict[str, Any] | None:
    with get_db() as db:
        row = db.execute(
            """
            SELECT * FROM indicator_scan_jobs
            WHERE username=? AND status IN ('queued','running','cancelling')
            ORDER BY created_at DESC LIMIT 1
            """,
            [username],
        ).fetchone()
    job = _job_row(row)
    if job and job["id"] not in _TASKS:
        _persist_job(job, status="interrupted", error="服务重启或任务已失去执行上下文", completed=True)
        return None
    return job


def _persist_job(
    job: dict[str, Any],
    *,
    status: str | None = None,
    error: str | None = None,
    completed: bool = False,
) -> None:
    if status:
        job["status"] = status
    if job["id"] in _CANCEL_REQUESTED and job.get("status") in {"queued", "running"}:
        job["status"] = "cancelling"
    if error is not None:
        job["error"] = error
    now = int(time.time())
    completed_at = now if completed else job.get("completed_at")
    job["updated_at"] = now
    job["completed_at"] = completed_at
    with get_db() as db:
        db.execute(
            """
            UPDATE indicator_scan_jobs SET
                status=?,results_json=?,total=?,processed=?,matched=?,failed=?,error=?,
                updated_at=?,completed_at=?,evaluated=?,insufficient=?,excluded=? WHERE id=? AND username=?
            """,
            [
                job.get("status", "running"),
                json.dumps(job.get("matches", []), ensure_ascii=False),
                int(job.get("total") or 0),
                int(job.get("processed") or 0),
                int(job.get("matched") or 0),
                int(job.get("failed") or 0),
                str(job.get("error") or "")[:500],
                now,
                completed_at,
                int(job.get("evaluated") or 0),
                int(job.get("insufficient") or 0),
                int(job.get("excluded") or 0),
                job["id"],
                job["username"],
            ],
        )


def uses_fundamentals(node: Any) -> bool:
    if isinstance(node, dict):
        if "fund" in node:
            return True
        return any(uses_fundamentals(value) for value in node.values())
    if isinstance(node, (list, tuple)):
        return any(uses_fundamentals(value) for value in node)
    return False


def _coerce_param_values(indicator: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    params = indicator.get("params") or {}
    for item in params.get("items", []) if isinstance(params, dict) else []:
        if isinstance(item, dict) and item.get("key"):
            values[str(item["key"])] = item.get("value")
    values.update(override or {})
    return values


def _sina_symbol(ticker: str) -> str:
    if ticker.startswith("6"):
        return "sh" + ticker
    if ticker.startswith(("4", "8", "92")):
        return "bj" + ticker
    return "sz" + ticker


async def _fast_kline_df(client, ticker: str, period: str, lookback: int):
    """Fetch technical-scan bars through one shared keep-alive HTTP client."""
    import pandas as pd

    multiplier = 1 if period == "1d" else 6 if period == "1w" else 24
    daily_limit = max(30, min(1023, int(lookback) * multiplier))
    response = await client.get(
        _SINA_KLINE_URL,
        params={
            "symbol": _sina_symbol(ticker),
            "scale": 240,
            "ma": "no",
            "datalen": daily_limit,
        },
    )
    response.raise_for_status()
    bars = response.json()
    if not isinstance(bars, list) or not bars:
        return None
    rows = []
    for bar in bars:
        try:
            day = datetime.date.fromisoformat(str(bar["day"])[:10])
            close = float(bar["close"])
            volume = float(bar.get("volume") or 0)
            rows.append(
                {
                    "t": int(
                        datetime.datetime.combine(
                            day, datetime.time(), tzinfo=datetime.timezone(datetime.timedelta(hours=8))
                        ).timestamp()
                    ),
                    "open": float(bar["open"]),
                    "high": float(bar["high"]),
                    "low": float(bar["low"]),
                    "close": close,
                    "volume": volume,
                    "amount": volume * close,
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    if not rows:
        return None
    frame = pd.DataFrame(rows).sort_values("t")
    if period in {"1w", "1m"}:
        frame["date"] = pd.to_datetime(frame["t"], unit="s", utc=True).dt.tz_convert("Asia/Shanghai")
        frame = (
            frame.set_index("date")
            .resample("W-FRI" if period == "1w" else "ME")
            .agg(
                {
                    "t": "last",
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum",
                    "amount": "sum",
                }
            )
            .dropna(subset=["open", "high", "low", "close"])
            .reset_index(drop=True)
        )
    return frame.tail(lookback).reset_index(drop=True)


def _sort_matches(matches: list[dict[str, Any]], config: dict[str, Any], signal: bool) -> None:
    sort_key = config.get("sort") or ("strength" if signal else "latest_value")
    direction = config.get("direction", "desc")
    key = "strength"
    if sort_key == "recent_signal":
        key = "triggered_at"
    elif sort_key == "latest_value":
        key = "value"
    matches.sort(
        key=lambda row: (
            float(row.get(key) if row.get(key) is not None else -math.inf),
            float(row.get("turnover") or 0),
        ),
        reverse=direction != "asc",
    )


async def _run_job(
    job: dict[str, Any],
    indicator: dict[str, Any],
    build_df: Callable[..., Awaitable[tuple[Any, dict[str, Any]]]],
    fast_fetch: bool = True,
) -> None:
    config = job["config"]
    try:
        if job["id"] in _CANCEL_REQUESTED:
            _persist_job(job, status="cancelled", error="用户已取消扫描", completed=True)
            return
        loop_id = id(asyncio.get_running_loop())
        gate = _GATES.setdefault(loop_id, asyncio.Semaphore(1))
        async with gate:
            if job["id"] in _CANCEL_REQUESTED:
                _persist_job(job, status="cancelled", error="用户已取消扫描", completed=True)
                return
            job["status"] = "running"
            if config.get("scope") == "market":
                rows = filter_universe(await load_universe(), config)
            else:
                rows = []
                seen = set()
                for raw in config.get("tickers") or []:
                    ticker = re.sub(r"\D", "", str(raw))[-6:]
                    if not re.fullmatch(r"\d{6}", ticker) or ticker in seen:
                        continue
                    seen.add(ticker)
                    rows.append(
                        {
                            "ticker": ticker,
                            "name": str((config.get("ticker_names") or {}).get(ticker) or ticker)[:80],
                            "market": _market_for_code(ticker),
                            "board": _board_for_code(ticker),
                        }
                    )
            job["total"] = len(rows)
            _persist_job(job, status="running")
            if not rows:
                raise RuntimeError("股票池为空，请检查市场、板块或自选配置")

            queue: asyncio.Queue = asyncio.Queue()
            for row in rows:
                queue.put_nowait(row)
            signal_kind = indicator.get("kind") == "signal"
            params = _coerce_param_values(indicator, config.get("params") or {})
            needs_fund = uses_fundamentals(indicator.get("dsl"))
            trigger_window = int(config.get("trigger_window") or 5)
            min_turnover = float(config.get("min_turnover") or 0)
            max_keep = max(int(config.get("max_results") or 100) * 4, 400)
            progress_lock = asyncio.Lock()
            fast_client = None
            if fast_fetch and not needs_fund:
                import httpx

                fast_client = httpx.AsyncClient(
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=httpx.Timeout(12),
                    limits=httpx.Limits(max_connections=20, max_keepalive_connections=20),
                )

            async def worker() -> None:
                while True:
                    if job["id"] in _CANCEL_REQUESTED:
                        return
                    try:
                        row = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    try:
                        df, fund = None, {}
                        if fast_client is not None:
                            try:
                                df = await _fast_kline_df(
                                    fast_client,
                                    row["ticker"],
                                    config.get("period", "1d"),
                                    int(config.get("lookback") or 120),
                                )
                            except Exception:
                                df = None
                        if df is None:
                            df, fund = await asyncio.wait_for(
                                build_df(
                                    row["ticker"],
                                    config.get("period", "1d"),
                                    int(config.get("lookback") or 120),
                                    include_fundamentals=needs_fund,
                                ),
                                timeout=24,
                            )
                        if df is None or len(df) == 0:
                            raise RuntimeError("no kline")
                        turnover = float(df["amount"].iloc[-1]) if "amount" in df.columns else float("nan")
                        if min_turnover and not math.isfinite(turnover):
                            raise MissingData("缺少成交额，无法执行成交额门槛")
                        if min_turnover and turnover < min_turnover:
                            job["excluded"] = job.get("excluded", 0) + 1
                            continue
                        result = evaluate(indicator["dsl"], df, fund, params)
                        if result.empty or result.iloc[-1:].isna().any():
                            raise MissingData("指标预热或输入资料不足")
                        job["evaluated"] = job.get("evaluated", 0) + 1
                        match = None
                        if is_signal(result):
                            window = (
                                result.iloc[-trigger_window:] if len(result) >= trigger_window else result
                            )
                            if bool(window.fillna(False).any()):
                                indexes = [
                                    idx for idx in range(len(result)) if bool(result.fillna(False).iloc[idx])
                                ]
                                idx = indexes[-1]
                                days_ago = max(0, len(df) - 1 - idx)
                                match = {
                                    **row,
                                    "triggered_at": int(df["t"].iloc[idx]),
                                    "price": float(df["close"].iloc[-1]),
                                    "turnover": turnover,
                                    "days_ago": days_ago,
                                    "strength": max(30, 100 - days_ago * 12),
                                    "reason": f"{indicator.get('name', '指标')}：{days_ago} 根 K 线内触发",
                                }
                        else:
                            clean = result.dropna()
                            if len(clean):
                                value = float(clean.iloc[-1])
                                match = {
                                    **row,
                                    "triggered_at": int(df["t"].iloc[-1]),
                                    "price": float(df["close"].iloc[-1]),
                                    "turnover": turnover,
                                    "value": value,
                                    "strength": 0,
                                    "reason": f"{indicator.get('name', '指标')} 最新值 {value:.4g}",
                                }
                        if match:
                            async with progress_lock:
                                job["matches"].append(match)
                                job["matched"] += 1
                                _sort_matches(job["matches"], config, signal_kind)
                                if len(job["matches"]) > max_keep:
                                    del job["matches"][max_keep:]
                    except asyncio.CancelledError:
                        raise
                    except MissingData:
                        job["insufficient"] = job.get("insufficient", 0) + 1
                    except (TimeoutError, DSLError, RuntimeError):
                        job["failed"] += 1
                    except Exception as exc:
                        job["failed"] += 1
                        log.debug("scan failed for %s: %s", row.get("ticker"), exc)
                    finally:
                        job["processed"] += 1
                        queue.task_done()
                        if job["processed"] % 10 == 0 or job["processed"] == job["total"]:
                            _persist_job(job)

            async def run_workers(_lease):
                # This registered owner holds every native async request until
                # all workers and their shared client have actually finished.
                workers = [asyncio.create_task(worker()) for _ in range(min(20, len(rows)))]
                try:
                    return await asyncio.gather(*workers, return_exceptions=True)
                finally:
                    if fast_client is not None:
                        await fast_client.aclose()

            batch = get_manager().start("request", run_workers)
            outcomes = await batch.wait_result()
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    raise outcome
            _sort_matches(job["matches"], config, signal_kind)
            job["matches"] = job["matches"][: int(config.get("max_results") or 100)]
            if job["id"] in _CANCEL_REQUESTED:
                _persist_job(job, status="cancelled", error="用户已取消扫描，已有结果已保存", completed=True)
                return
            incomplete = int(job.get("failed") or 0) + int(job.get("insufficient") or 0)
            status = "completed"
            message = ""
            if incomplete:
                status = "partial" if job.get("evaluated") or job.get("excluded") else "failed"
                message = "部分股票缺少资料或读取失败，请按实际覆盖范围解读结果"
            _persist_job(job, status=status, error=message, completed=True)
    except asyncio.CancelledError:
        _persist_job(job, status="cancelled", error="用户已取消扫描", completed=True)
        # Only cancel_job's cooperative flag is an authorized normal stop. An
        # unknown owner cancellation remains blocked in the lifecycle manager.
        raise
    except Exception as exc:
        log.exception("indicator scan job %s failed", job.get("id"))
        _persist_job(job, status="failed", error=str(exc), completed=True)
    finally:
        _CANCEL_REQUESTED.discard(job["id"])


def start_job(
    job: dict[str, Any],
    indicator: dict[str, Any],
    build_df: Callable[..., Awaitable[tuple[Any, dict[str, Any]]]],
) -> None:
    async def execute(_lease):
        await _run_job(job, indicator, build_df)

    def persist_terminal(_result):
        stored = get_job(job["id"], job["username"])
        if not stored or stored["status"] not in {"completed", "partial", "failed", "cancelled"}:
            raise RuntimeError("scan_terminal_state_missing")

    handle = get_manager().start("scan", execute, persist=persist_terminal)
    _TASKS[job["id"]] = handle

    def completed(done):
        if _TASKS.get(job["id"]) is done:
            _TASKS.pop(job["id"], None)

    handle.add_done_callback(completed)


def cancel_job(job_id: str, username: str) -> bool:
    job = get_job(job_id, username)
    if not job:
        return False
    if job.get("status") not in {"queued", "running", "cancelling"}:
        return True
    task = _TASKS.get(job_id)
    if task and not task.done():
        _persist_job(job, status="cancelling", error="正在停止扫描，等待当前读取完成")
        _CANCEL_REQUESTED.add(job_id)
    elif job.get("status") in {"queued", "running", "cancelling"}:
        _persist_job(job, status="cancelled", error="用户已取消扫描", completed=True)
    return True


def delete_job(job_id: str, username: str) -> bool:
    if get_job(job_id, username) is None:
        return False
    cancel_job(job_id, username)
    handle = _TASKS.get(job_id)
    if handle is not None and not handle.done():
        from fastapi import HTTPException

        raise HTTPException(409, "扫描尚未确认停止，请等待结束后再删除")
    with get_db() as db:
        cur = db.execute(
            "DELETE FROM indicator_scan_jobs WHERE id=? AND username=?",
            [job_id, username],
        )
    return bool(cur.rowcount)
