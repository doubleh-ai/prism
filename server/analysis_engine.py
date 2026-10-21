"""Astock analysis engine — wraps TradingAgentsGraph with async SSE streaming.

Each analysis runs in a thread-pool executor.  LangGraph stream events are
pushed into an asyncio.Queue so the SSE endpoint can consume them without
polling or blocking the event loop.
"""
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from contextlib import contextmanager
from typing import Dict, Optional

from .config import (
    ASTOCK_PROJECT,
    ASTOCK_LLM_QUICK,
    ASTOCK_LLM_DEEP,
    ASTOCK_LLM_PROVIDER,
    ASTOCK_LLM_BASE_URL,
    ASTOCK_LLM_API_KEY,
    ASTOCK_DATA_VENDOR,
    ASTOCK_MAX_DEBATE_ROUNDS,
    ASTOCK_OUTPUT_LANGUAGE,
    ASTOCK_RESULTS_DIR,
    ASTOCK_HARD_TIMEOUT_SECONDS,
    ASTOCK_CACHE_TTL_SECONDS,
    ASTOCK_FAST_ANALYSTS,
    ASTOCK_DEEP_ANALYSTS,
    ASTOCK_FAST_DEBATE_ROUNDS,
    ASTOCK_FAST_RISK_ROUNDS,
    ASTOCK_NO_PROXY_DOMAINS,
)
from .database import get_db

log = logging.getLogger("prism.analysis")

# Lazy-import astock so the module loads even if astock isn't installed yet.
_astock_imported = False


def _ensure_astock():
    global _astock_imported
    if _astock_imported:
        return
    if ASTOCK_PROJECT not in sys.path:
        sys.path.insert(0, ASTOCK_PROJECT)
    _astock_imported = True


# ── In-memory store for running/completed analyses ──
_store: Dict[str, dict] = {}

_VALID_ANALYSTS = {"market", "social", "news", "fundamentals", "policy", "hot_money", "lockup"}


def _normalize_mode(mode: str = "") -> str:
    return "fast" if str(mode or "").lower() in {"fast", "quick", "lite"} else "deep"


def _selected_analysts(mode: str) -> list[str]:
    raw = ASTOCK_FAST_ANALYSTS if _normalize_mode(mode) == "fast" else ASTOCK_DEEP_ANALYSTS
    out = [a for a in raw if a in _VALID_ANALYSTS]
    return out or ["market", "fundamentals"]


def _cache_key_user(username: str) -> str:
    return username or "__anonymous__"


def _load_cached_analysis(username: str, ticker: str, trade_date: str, mode: str) -> Optional[dict]:
    if ASTOCK_CACHE_TTL_SECONDS <= 0:
        return None
    cutoff = int(time.time()) - ASTOCK_CACHE_TTL_SECONDS
    try:
        with get_db() as db:
            row = db.execute(
                """
                SELECT id, name, signal, report_json, timings_json, elapsed, updated_at
                FROM analysis_cache
                WHERE username=? AND ticker=? AND trade_date=? AND mode=? AND updated_at>=?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (_cache_key_user(username), ticker, trade_date, mode, cutoff),
            ).fetchone()
        if not row:
            return None
        return {
            "cache_id": row["id"],
            "name": row["name"] or "",
            "signal": row["signal"] or "",
            "report": json.loads(row["report_json"] or "{}"),
            "timings": json.loads(row["timings_json"] or "{}"),
            "elapsed": row["elapsed"] or 0,
            "updated_at": row["updated_at"],
        }
    except Exception as exc:
        log.warning("analysis cache read failed: %s", exc)
        return None


def _save_cached_analysis(username: str, ticker: str, trade_date: str, mode: str,
                          name: str, signal: str, report: dict,
                          timings: dict, elapsed: float) -> None:
    if ASTOCK_CACHE_TTL_SECONDS <= 0 or not report:
        return
    now = int(time.time())
    try:
        with get_db() as db:
            db.execute(
                """
                INSERT INTO analysis_cache
                    (id, username, ticker, trade_date, mode, name, signal,
                     report_json, timings_json, elapsed, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username, ticker, trade_date, mode) DO UPDATE SET
                    name=excluded.name,
                    signal=excluded.signal,
                    report_json=excluded.report_json,
                    timings_json=excluded.timings_json,
                    elapsed=excluded.elapsed,
                    updated_at=excluded.updated_at
                """,
                (
                    uuid.uuid4().hex[:12],
                    _cache_key_user(username),
                    ticker,
                    trade_date,
                    mode,
                    name or "",
                    signal or "",
                    json.dumps(report, ensure_ascii=False),
                    json.dumps(timings or {}, ensure_ascii=False),
                    elapsed,
                    now,
                    now,
                ),
            )
    except Exception as exc:
        log.warning("analysis cache write failed: %s", exc)


@contextmanager
def _astock_no_proxy_env():
    """Bypass local proxy for flaky A-share data sources during one run."""
    keys = ("no_proxy", "NO_PROXY")
    old = {k: os.environ.get(k) for k in keys}
    merged = set()
    for k in keys:
        for part in (old.get(k) or "").split(","):
            part = part.strip()
            if part:
                merged.add(part)
    merged.update(ASTOCK_NO_PROXY_DOMAINS)
    value = ",".join(sorted(merged))
    for k in keys:
        os.environ[k] = value
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _build_config(mode: str = "deep") -> dict:
    """Build astock DEFAULT_CONFIG overrides from Prism config."""
    from tradingagents.default_config import DEFAULT_CONFIG

    normalized = _normalize_mode(mode)
    cfg = DEFAULT_CONFIG.copy()
    cfg["llm_provider"] = ASTOCK_LLM_PROVIDER
    cfg["deep_think_llm"] = ASTOCK_LLM_DEEP
    cfg["quick_think_llm"] = ASTOCK_LLM_QUICK
    if ASTOCK_LLM_BASE_URL:
        cfg["backend_url"] = ASTOCK_LLM_BASE_URL.rstrip("/") + "/v1"
    if ASTOCK_LLM_API_KEY:
        os_key = f"{ASTOCK_LLM_PROVIDER.upper()}_API_KEY"
        import os as _os
        if not _os.environ.get(os_key):
            _os.environ[os_key] = ASTOCK_LLM_API_KEY
    cfg["output_language"] = ASTOCK_OUTPUT_LANGUAGE
    if normalized == "fast":
        cfg["max_debate_rounds"] = ASTOCK_FAST_DEBATE_ROUNDS
        cfg["max_risk_discuss_rounds"] = ASTOCK_FAST_RISK_ROUNDS
    else:
        cfg["max_debate_rounds"] = ASTOCK_MAX_DEBATE_ROUNDS
        cfg["max_risk_discuss_rounds"] = ASTOCK_MAX_DEBATE_ROUNDS
    cfg["results_dir"] = ASTOCK_RESULTS_DIR
    cfg["data_vendors"] = {
        "core_stock_apis": ASTOCK_DATA_VENDOR,
        "technical_indicators": ASTOCK_DATA_VENDOR,
        "fundamental_data": ASTOCK_DATA_VENDOR,
        "news_data": ASTOCK_DATA_VENDOR,
        "signal_data": ASTOCK_DATA_VENDOR,
    }
    return cfg


_ASTOCK_SNAPSHOT_KEYS = {
    "messages",
    "company_of_interest",
    "trade_date",
    "market_report",
    "fundamentals_report",
    "investment_debate_state",
    "risk_debate_state",
    "final_trade_decision",
}


def _merge_astock_stream_state(previous: dict, chunk, initial_state: Optional[dict] = None) -> dict:
    """Normalize TradingAgents stream output without flattening state containers.

    Current TradingAgents-astock releases stream with ``stream_mode='values'``;
    every chunk is therefore a complete state snapshot.  Older releases can
    emit ``{node: update}`` deltas instead.  The previous implementation
    treated snapshots as deltas and flattened nested values such as
    ``investment_debate_state``, which made upstream finalization fail.
    """
    if not isinstance(chunk, dict):
        return dict(previous or initial_state or {})

    is_snapshot = bool(_ASTOCK_SNAPSHOT_KEYS.intersection(chunk))
    if is_snapshot:
        merged = dict(initial_state or {})
        merged.update(chunk)
        return merged

    merged = dict(initial_state or {})
    merged.update(previous or {})
    for node, update in chunk.items():
        if isinstance(update, dict):
            merged.update(update)
        else:
            merged[node] = update
    return merged


async def start_analysis(ticker: str, name: str = "", date: str = "",
                         username: str = "", mode: str = "deep",
                         force_refresh: bool = False) -> str:
    """Launch an astock analysis in a background thread.

    Returns an *analysis_id* that can be used to stream progress via
    :func:`stream_events` or fetch the final report via :func:`get_report`.

    *username* is stored so stream/report endpoints can enforce ownership.
    """
    ticker = (ticker or "").strip().upper()
    mode = _normalize_mode(mode)
    trade_date = date or time.strftime("%Y-%m-%d")
    selected_analysts = _selected_analysts(mode)
    aid = uuid.uuid4().hex[:12]
    queue: asyncio.Queue = asyncio.Queue()
    _store[aid] = {
        "id": aid,
        "ticker": ticker,
        "name": name,
        "username": username,
        "mode": mode,
        "trade_date": trade_date,
        "selected_analysts": selected_analysts,
        "cached": False,
        "timings": {},
        "status": "starting",
        "queue": queue,
        "report": None,
        "signal": None,
        "error": None,
        "started_at": time.time(),
    }

    cached = None if force_refresh else _load_cached_analysis(username, ticker, trade_date, mode)
    if cached:
        report = cached["report"]
        meta = report.setdefault("_analysis_meta", {})
        meta.update({
            "mode": mode,
            "cached": True,
            "trade_date": trade_date,
            "selected_analysts": selected_analysts,
            "cache_updated_at": cached.get("updated_at"),
            "timings": cached.get("timings") or {},
        })
        _store[aid].update({
            "status": "done",
            "report": report,
            "signal": cached.get("signal"),
            "cached": True,
            "timings": cached.get("timings") or {},
            "elapsed": cached.get("elapsed") or 0,
        })
        queue.put_nowait({"type": "status", "elapsed": 0, "message": "使用今日缓存报告，已跳过重复 Astock 分析。"})
        queue.put_nowait({"type": "stage", "elapsed": 0, "stage": "decision", "progress_pct": 100})
        queue.put_nowait({
            "type": "done",
            "elapsed": round(cached.get("elapsed") or 0, 1),
            "report": report,
            "signal": cached.get("signal"),
            "mode": mode,
            "cached": True,
            "timings": cached.get("timings") or {},
        })
        return aid

    _ensure_astock()
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    def _run():
        # Define _emit + stage maps FIRST so the except handler below can
        # always notify the frontend — even if graph CONSTRUCTION throws
        # (missing LLM API key, bad config, import error). Previously the
        # TradingAgentsGraph(...) call sat ABOVE the try, so its exception
        # escaped into an un-awaited executor Future ("Future exception was
        # never retrieved") and no error event ever reached the SSE stream
        # → the frontend pumped heartbeats forever and the UI froze.
        start = time.time()

        def _emit(event_type: str, **kwargs):
            try:
                queue.put_nowait({"type": event_type, "elapsed": round(time.time() - start, 1), **kwargs})
            except asyncio.QueueFull:
                pass

        _STAGE_ORDER = ["analysts", "debate", "research", "trader", "risk", "decision"]
        _STAGE_PCT = {"analysts": 40, "debate": 55, "research": 65, "trader": 75, "risk": 90, "decision": 100}
        _seen_stages = set()
        _seen_nodes = set()
        _last_node_text = {}
        timings = {
            "mode": mode,
            "trade_date": trade_date,
            "selected_analysts": selected_analysts,
            "nodes": [],
        }
        last_node_at = start

        # Emit progress IMMEDIATELY so the UI isn't frozen at 0% while the
        # engine builds and the first analyst grinds through data-fetch + LLM.
        # The frontend ticks its elapsed timer off the heartbeats we send
        # every 0.5s, so the user sees it's alive even before node 1 completes.
        _emit("stage", stage="analysts", progress_pct=_STAGE_PCT["analysts"])
        _seen_stages.add("analysts")
        _emit("status", message="正在初始化分析引擎…")

        proxy_ctx = _astock_no_proxy_env()
        proxy_entered = False
        ta = None
        try:
            proxy_ctx.__enter__()
            proxy_entered = True
            cfg = _build_config(mode)
            import inspect
            graph_kwargs = {"debug": True, "config": cfg}
            if "selected_analysts" in inspect.signature(TradingAgentsGraph).parameters:
                graph_kwargs["selected_analysts"] = selected_analysts
            ta = TradingAgentsGraph(**graph_kwargs)
            _store[aid]["status"] = "running"
            if mode == "fast":
                _emit("status", message="正在快速分析（行情 + 基本面，跳过舆情/新闻/政策等慢节点）…")
            else:
                _emit("status", message="正在深度分析（7 agent 全链路），预计 3-8 分钟…")

            init_state, graph_args, _ = ta.prepare_graph_run(ticker, trade_date)
            final_state = dict(init_state or {})
            for chunk in ta.graph.stream(init_state, **graph_args):
                previous_state = final_state.copy()
                final_state = _merge_astock_stream_state(previous_state, chunk, init_state)

                for semantic in _semantic_stream_updates(chunk, previous_state):
                    node_name = semantic["node"]
                    node_update = semantic.get("update") or {}
                    label = semantic.get("label") or _node_label(node_name)
                    now = time.time()
                    stage = semantic.get("stage") or _STAGE_FOR_NODE.get(node_name, "")
                    if stage and stage not in _seen_stages:
                        _seen_stages.add(stage)
                        _emit("stage", stage=stage, progress_pct=_STAGE_PCT.get(stage, 0))
                    agent_key = semantic.get("agent") or _agent_key_for_node(node_name)
                    if node_name not in _seen_nodes:
                        node_timing = {
                            "node": node_name,
                            "label": label,
                            "agent": agent_key,
                            "stage": stage or "",
                            "elapsed": round(now - start, 1),
                            "duration": round(now - last_node_at, 1),
                        }
                        timings["nodes"].append(node_timing)
                        _emit("timing", **node_timing)
                        last_node_at = now
                        _seen_nodes.add(node_name)
                        _emit("agent_done", agent=agent_key, node=node_name, label=label)
                    # Stream the node's own output text so the UI can show what
                    # each analyst/trader is producing in real time (then fold
                    # it away when the final dashboard renders).
                    snippet = semantic.get("text") or _node_snippet(node_update)
                    if snippet and snippet != _last_node_text.get(node_name):
                        _last_node_text[node_name] = snippet
                        _emit("agent_output", agent=agent_key, node=node_name, label=label, text=snippet)

            signal = ta.finalize_graph_run(ticker, trade_date, final_state)
            report = _extract_report(final_state, ticker, name)
            timings["total_seconds"] = round(time.time() - start, 1)
            report["_analysis_meta"] = {
                "mode": mode,
                "cached": False,
                "trade_date": trade_date,
                "selected_analysts": selected_analysts,
                "timings": timings,
            }
            _store[aid]["report"] = report
            _store[aid]["signal"] = signal
            _store[aid]["timings"] = timings
            _store[aid]["elapsed"] = timings["total_seconds"]
            _store[aid]["status"] = "done"
            _save_cached_analysis(username, ticker, trade_date, mode, name, signal, report, timings, timings["total_seconds"])
            _emit("done", report=report, signal=signal, mode=mode, cached=False, timings=timings)

            # Post-analysis structured extraction (dashboard upgrade). Emits a
            # separate "structured" event so the UI can render the plain report
            # fast, then fill in the dashboard. Failure is non-fatal.
            try:
                structured = _extract_structured(report, ticker, name)
                if structured:
                    report["structured"] = structured
                    _save_cached_analysis(username, ticker, trade_date, mode, name, signal, report, timings, timings["total_seconds"])
                    _emit("structured", structured=structured)
            except Exception as exc:
                log.warning("structured extraction failed: %s", exc)
        except Exception as exc:
            log.exception("Analysis %s failed: %s", aid, exc)
            _store[aid]["status"] = "error"
            _store[aid]["error"] = str(exc)
            _emit("error", message=str(exc))
        finally:
            if ta is not None:
                close_graph_run = getattr(ta, "close_graph_run", None)
                if callable(close_graph_run):
                    try:
                        close_graph_run()
                    except Exception as exc:
                        log.warning("Analysis %s graph cleanup failed: %s", aid, exc)
            if proxy_entered:
                proxy_ctx.__exit__(None, None, None)

    loop = asyncio.get_event_loop()
    future = loop.run_in_executor(None, _run)

    def _on_run_done(fut):
        # _run catches its own exceptions and emits an "error" event, so this
        # should normally be a no-op. Retrieve the result anyway so an
        # unexpected crash surfaces in the log instead of as a silent
        # "Future exception was never retrieved" warning.
        try:
            fut.result()
        except Exception as exc:
            log.exception("Analysis executor crashed unexpectedly: %s", exc)

    future.add_done_callback(_on_run_done)
    return aid


async def stream_events(analysis_id: str):
    """Async generator yielding SSE-formatted events for an ongoing analysis."""
    entry = _store.get(analysis_id)
    if not entry:
        yield _sse({"type": "error", "message": "分析不存在"})
        return

    queue: asyncio.Queue = entry["queue"]
    started = entry.get("started_at") or time.time()
    last_activity = time.time()
    warned_slow = False
    SLOW_IDLE = 60            # no real event for 60s → tell user it's slow
    HARD_TIMEOUT = 15 * 60    # 15 min total with no done/error → abort

    HARD_TIMEOUT = max(60, ASTOCK_HARD_TIMEOUT_SECONDS)

    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            now = time.time()
            # Warn once when data fetching stalls (typical when mootdx/eastmoney
            # are blocked — each call burns its full timeout before failing).
            if now - last_activity > SLOW_IDLE and not warned_slow:
                warned_slow = True
                yield _sse({"type": "slow",
                            "message": "数据源响应较慢，仍在拉取中…（行情接口偶有阻塞）"})
            # Hard ceiling so a hung run can't spin forever silently.
            if now - started > HARD_TIMEOUT:
                entry["status"] = "error"
                entry["error"] = f"analysis timeout after {HARD_TIMEOUT}s"
                timeout_min = max(1, round(HARD_TIMEOUT / 60))
                yield _sse({"type": "error",
                            "message": f"分析超时（{timeout_min} 分钟无进展）已中止。"
                                       "通常因行情数据源（mootdx/eastmoney）网络不通，"
                                       "请检查服务器出口或更换数据源。"})
                break
            # Heartbeat keeps the SSE connection alive and lets the
            # frontend tick its elapsed timer every 0.5s.
            yield _sse({"type": "heartbeat"})
            continue

        last_activity = time.time()
        warned_slow = False
        yield _sse(event)
        if event["type"] in ("done", "error"):
            break


def get_report(analysis_id: str) -> Optional[dict]:
    """Return the completed analysis report, or None if still running."""
    entry = _store.get(analysis_id)
    if not entry or entry["status"] not in ("done", "error"):
        return None
    return {
        "id": entry["id"],
        "ticker": entry["ticker"],
        "name": entry["name"],
        "mode": entry.get("mode", "deep"),
        "cached": bool(entry.get("cached")),
        "trade_date": entry.get("trade_date"),
        "selected_analysts": entry.get("selected_analysts") or [],
        "status": entry["status"],
        "signal": entry["signal"],
        "report": entry["report"],
        "timings": entry.get("timings") or {},
        "error": entry["error"],
        "elapsed": entry.get("elapsed") or round(time.time() - entry["started_at"], 1),
    }


def _extract_report(final_state: dict, ticker: str, name: str) -> dict:
    """Pull the key sections out of a completed LangGraph state dict."""
    report = {
        "ticker": ticker,
        "name": name,
        "analyst_reports": {},
        "investment_debate": {},
        "trader_plan": "",
        "risk_debate": {},
        "final_decision": "",
        "signal": "",
    }

    # Analyst reports
    for key in ("market_report", "sentiment_report", "news_report",
                "fundamentals_report", "policy_report",
                "hot_money_report", "lockup_report"):
        val = _get_last_message(final_state.get(key, ""))
        if val:
            report["analyst_reports"][key.replace("_report", "")] = val

    # Investment debate
    debate = final_state.get("investment_debate_state", {})
    if isinstance(debate, dict):
        report["investment_debate"] = {
            "bull_history": _get_last_message(debate.get("bull_history", "")),
            "bear_history": _get_last_message(debate.get("bear_history", "")),
            "judge_decision": _get_last_message(debate.get("judge_decision", "")),
        }

    # Trader plan
    report["trader_plan"] = _get_last_message(final_state.get("trader_investment_plan", ""))

    # Risk debate
    risk = final_state.get("risk_debate_state", {})
    if isinstance(risk, dict):
        report["risk_debate"] = {
            "aggressive": _get_last_message(risk.get("aggressive_history", "")),
            "conservative": _get_last_message(risk.get("conservative_history", "")),
            "neutral": _get_last_message(risk.get("neutral_history", "")),
            "judge_decision": _get_last_message(risk.get("judge_decision", "")),
        }

    # Final decision
    report["final_decision"] = _get_last_message(final_state.get("final_trade_decision", ""))
    report["signal"] = final_state.get("signal", "")

    return report


def _strip_object_artifacts(text: str) -> str:
    lines = []
    for line in str(text or "").splitlines():
        if line.strip() in {"[object Object]", "object Object"}:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _join_nonempty(parts) -> str:
    return "\n".join(p for p in (_strip_object_artifacts(x) for x in parts) if p)


def _get_last_message(obj) -> str:
    """Extract readable text from LangGraph/LangChain message-like values."""
    if not obj:
        return ""
    if isinstance(obj, str):
        return _strip_object_artifacts(obj)
    if isinstance(obj, (list, tuple)):
        if all(isinstance(item, (str, dict)) and not hasattr(item, "content") for item in obj):
            joined = _join_nonempty(_get_last_message(item) for item in obj)
            if joined:
                return joined
        # LangGraph messages: take the last message's content.
        for item in reversed(obj):
            content = _get_last_message(item)
            if content:
                return content
        return ""
    if isinstance(obj, dict):
        for key in ("text", "content", "result", "output", "message", "summary",
                    "final_trade_decision", "final_decision", "trader_investment_plan"):
            if key in obj:
                content = _get_last_message(obj.get(key))
                if content:
                    return content
        parts = []
        for value in obj.values():
            content = _get_last_message(value)
            if content:
                parts.append(content)
        return _join_nonempty(parts)
    # LangChain message object
    if hasattr(obj, "content"):
        content = obj.content
        if isinstance(content, str):
            return _strip_object_artifacts(content)
        return _get_last_message(content)
    return _strip_object_artifacts(str(obj)) if obj else ""


# Stable agent keys — the frontend grid uses these. (The naive
# lower/replace turned "Social Media Analyst" into "social_media", which never
# matched the grid's "social" key.)
_AGENT_KEY = {
    "Market Analyst": "market", "Social Media Analyst": "social",
    "News Analyst": "news", "Fundamentals Analyst": "fundamentals",
    "Policy Analyst": "policy", "Hot Money Analyst": "hot_money",
    "Lockup Analyst": "lockup",
    "Bull Researcher": "bull_researcher", "Bear Researcher": "bear_researcher",
    "Research Manager": "research_manager", "Trader": "trader",
    "Aggressive Analyst": "aggressive", "Conservative Analyst": "conservative",
    "Neutral Analyst": "neutral", "Risk Committee": "risk",
    "Portfolio Manager": "portfolio_manager",
}

_NODE_LABELS = {
    "Market Analyst": "市场分析师",
    "Social Media Analyst": "舆情分析师",
    "News Analyst": "新闻分析师",
    "Fundamentals Analyst": "基本面分析师",
    "Policy Analyst": "政策分析师",
    "Hot Money Analyst": "游资追踪师",
    "Lockup Analyst": "解禁监控师",
    "Bull Researcher": "多方研究员",
    "Bear Researcher": "空方研究员",
    "Research Manager": "研判整合",
    "Trader": "交易方案",
    "Aggressive Analyst": "激进风险评估",
    "Conservative Analyst": "保守风险评估",
    "Neutral Analyst": "中性风险评估",
    "Risk Committee": "风险评估",
    "Portfolio Manager": "最终决策",
}

_STAGE_FOR_NODE = {
    "Market Analyst": "analysts", "Social Media Analyst": "analysts",
    "News Analyst": "analysts", "Fundamentals Analyst": "analysts",
    "Policy Analyst": "analysts", "Hot Money Analyst": "analysts",
    "Lockup Analyst": "analysts",
    "Bull Researcher": "debate", "Bear Researcher": "debate",
    "Research Manager": "research",
    "Trader": "trader",
    "Aggressive Analyst": "risk", "Conservative Analyst": "risk",
    "Neutral Analyst": "risk", "Risk Committee": "risk",
    "Portfolio Manager": "decision",
}

_STATE_FIELD_NODE = {
    "market_report": "Market Analyst",
    "sentiment_report": "Social Media Analyst",
    "news_report": "News Analyst",
    "fundamentals_report": "Fundamentals Analyst",
    "policy_report": "Policy Analyst",
    "hot_money_report": "Hot Money Analyst",
    "lockup_report": "Lockup Analyst",
    "investment_debate_state": "Research Manager",
    "trader_investment_plan": "Trader",
    "risk_debate_state": "Risk Committee",
    "final_trade_decision": "Portfolio Manager",
}

# State fields that hold a node's main prose output, in priority order.
_REPORT_FIELDS = (
    "market_report", "sentiment_report", "news_report", "fundamentals_report",
    "policy_report", "hot_money_report", "lockup_report",
    "investment_debate_state", "trader_investment_plan",
    "risk_debate_state", "final_trade_decision", "investment_plan",
)


def _agent_key_for_node(node_name: str) -> str:
    return _AGENT_KEY.get(node_name) or str(node_name or "").lower().replace(" ", "_").replace("_analyst", "")


def _node_label(node_name: str) -> str:
    return _NODE_LABELS.get(node_name) or str(node_name or "")


def _semantic_stream_updates(chunk, previous_state: Optional[dict] = None) -> list[dict]:
    """Normalize LangGraph stream chunks into user-facing analysis events.

    TradingAgents-astock debug streams usually emit state snapshots whose first
    key is ``messages``. Showing that raw key produced the repeated "messages"
    rows in the UI. This helper only exposes real graph nodes or meaningful
    state fields such as ``market_report`` / ``final_trade_decision``.
    """
    if not isinstance(chunk, dict):
        return []

    updates = []
    for node_name, raw_update in chunk.items():
        if node_name not in _STAGE_FOR_NODE:
            continue
        update = raw_update if isinstance(raw_update, dict) else {node_name: raw_update}
        updates.append({
            "node": node_name,
            "label": _node_label(node_name),
            "agent": _agent_key_for_node(node_name),
            "stage": _STAGE_FOR_NODE.get(node_name, ""),
            "update": update,
            "text": _node_snippet(update),
        })
    if updates:
        return updates

    previous_state = previous_state or {}
    for field, value in chunk.items():
        node_name = _STATE_FIELD_NODE.get(field)
        if not node_name:
            continue
        text = _get_last_message(value)
        if not text:
            continue
        if text == _get_last_message(previous_state.get(field, "")):
            continue
        update = {field: value}
        updates.append({
            "node": node_name,
            "label": _node_label(node_name),
            "agent": _agent_key_for_node(node_name),
            "stage": _STAGE_FOR_NODE.get(node_name, ""),
            "update": update,
            "text": _node_snippet(update),
        })
    return updates


def _node_snippet(state_update, limit=500):
    """Pull a short text snippet of a node's output from its state update dict,
    so the UI can stream what each analyst/trader is producing."""
    if not isinstance(state_update, dict):
        return ""
    for fld in _REPORT_FIELDS:
        v = state_update.get(fld)
        if v:
            s = _get_last_message(v)
            if s:
                return s[:limit]
    for v in state_update.values():  # fallback: first substantial string
        s = _get_last_message(v)
        if len(s) > 40:
            return s[:limit]
    return ""


def _parse_json_lenient(raw: str):
    """Parse JSON tolerantly. ``claude -p --output-format json`` returns an
    envelope like ``{"type":"result","result":"<actual output>",...}``; when
    our prompt asks for JSON, that inner ``result`` is itself JSON — unwrap it.
    Also tolerates raw JSON and JSON embedded in prose."""
    import json
    import re
    if not raw:
        return None
    data = None
    try:
        data = json.loads(raw)
    except Exception:
        data = None
    if isinstance(data, dict):
        inner = data.get("result")
        if isinstance(inner, str):
            # Unwrap the claude envelope's result field.
            try:
                return json.loads(inner)
            except Exception:
                m = re.search(r"\{.*\}", inner, re.S)
                if m:
                    try:
                        return json.loads(m.group(0))
                    except Exception:
                        pass
            # result is plain text, not JSON — keep only if data itself matches schema
            return data if (data.get("signal") or data.get("thesis")) else None
        return data
    # Not valid JSON → extract first {...} block (embedded in prose)
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None


def _extract_structured(report: dict, ticker: str, name: str) -> Optional[dict]:
    """Post-analysis LLM extraction: turn the free-text astock report into a
    structured JSON the frontend renders as a dashboard (signal, confidence,
    metrics, trade plan, per-analyst digests, bull/bear, verdict).

    Returns the structured dict, or None on any failure — the frontend then
    falls back to the plain-text report. Runs synchronously in the analysis
    executor thread; bounded by a subprocess timeout so it can't hang.
    """
    import subprocess
    from .config import CLAUDE_BIN

    def _t(v):
        return _get_last_message(v)

    ar = report.get("analyst_reports", {}) or {}
    sections = [f"### {k}\n{_t(v)[:1500]}" for k, v in ar.items()]
    debate = report.get("investment_debate", {}) or {}
    risk = report.get("risk_debate", {}) or {}
    bundle = "\n\n".join([
        f"股票: {ticker} {name}",
        "## 分析师研报\n" + "\n\n".join(sections),
        f"## 多方论点\n{_t(debate.get('bull_history', ''))[:1000]}",
        f"## 空方论点\n{_t(debate.get('bear_history', ''))[:1000]}",
        f"## 多空裁决\n{_t(debate.get('judge_decision', ''))[:600]}",
        f"## 交易方案\n{_t(report.get('trader_plan', ''))[:1000]}",
        f"## 激进方\n{_t(risk.get('aggressive', ''))[:600]}",
        f"## 保守方\n{_t(risk.get('conservative', ''))[:600]}",
        f"## 最终决策\n{_t(report.get('final_decision', ''))[:2000]}",
    ])
    schema = (
        '严格只输出JSON，schema：'
        '{"signal":"买入|持有|卖出|观望",'
        '"confidence":0到100的整数,'
        '"thesis":"1-2句核心论点",'
        '"metrics":{"valuation":"高|中|低|合理","growth":"高|中|低",'
        '"risk_score":0到10的整数,"momentum":"强|中|弱"},'
        '"trade":{"target_price":"文字","stop_loss":"文字","position":"文字",'
        '"horizon":"文字","rationale":"文字"},'
        '"analyst_digests":[{"agent":"market|social|news|fundamentals|policy|hot_money|lockup",'
        '"verdict":"多|空|中","bullets":["要点1","要点2"]}],'
        '"bull_bear":{"bull":"多方摘要","bear":"空方摘要","judge":"偏多|偏空|中性"},'
        '"verdict_summary":"最终结论摘要"}'
        '只输出JSON，不要任何额外文字。'
    )
    prompt = ("你是投研分析师。根据以下 astock 多智能体分析报告，提炼成结构化JSON仪表盘数据。\n"
              + schema + "\n\n报告内容：\n" + bundle)

    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "-p", prompt,
             "--model", "minimax-m2.7-fast",
             "--max-tokens", "1500",
             "--output-format", "json"],
            capture_output=True, timeout=40,
        )
    except Exception as exc:
        log.warning("structured extraction subprocess failed: %s", exc)
        return None

    data = _parse_json_lenient(proc.stdout.decode("utf-8", errors="replace").strip())
    if not isinstance(data, dict):
        return None
    # Require at least a signal or thesis — otherwise treat as garbage.
    if not data.get("signal") and not data.get("thesis"):
        return None
    return data


def _sse(data: dict) -> str:
    """Format a dict as an SSE event."""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
