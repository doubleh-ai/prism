"""Astock analysis engine — wraps TradingAgentsGraph with async SSE streaming.

Each analysis runs in a thread-pool executor.  LangGraph stream events are
pushed into an asyncio.Queue so the SSE endpoint can consume them without
polling or blocking the event loop.
"""
import asyncio
import json
import logging
import sys
import time
import uuid
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
)

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


def _build_config() -> dict:
    """Build astock DEFAULT_CONFIG overrides from Prism config."""
    from tradingagents.default_config import DEFAULT_CONFIG

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


async def start_analysis(ticker: str, name: str = "", date: str = "",
                        username: str = "") -> str:
    """Launch an astock analysis in a background thread.

    Returns an *analysis_id* that can be used to stream progress via
    :func:`stream_events` or fetch the final report via :func:`get_report`.

    *username* is stored so stream/report endpoints can enforce ownership.
    """
    _ensure_astock()
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    aid = uuid.uuid4().hex[:12]
    queue: asyncio.Queue = asyncio.Queue()
    _store[aid] = {
        "id": aid,
        "ticker": ticker,
        "name": name,
        "username": username,
        "status": "starting",
        "queue": queue,
        "report": None,
        "signal": None,
        "error": None,
        "started_at": time.time(),
    }

    trade_date = date or time.strftime("%Y-%m-%d")

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

        # Known graph node → stage mapping
        _STAGES = {
            "Market Analyst": "analysts", "Social Media Analyst": "analysts",
            "News Analyst": "analysts", "Fundamentals Analyst": "analysts",
            "Policy Analyst": "analysts", "Hot Money Analyst": "analysts",
            "Lockup Analyst": "analysts",
            "Bull Researcher": "debate", "Bear Researcher": "debate",
            "Research Manager": "research",
            "Trader": "trader",
            "Aggressive Analyst": "risk", "Conservative Analyst": "risk",
            "Neutral Analyst": "risk",
            "Portfolio Manager": "decision",
        }
        _STAGE_ORDER = ["analysts", "debate", "research", "trader", "risk", "decision"]
        _STAGE_PCT = {"analysts": 40, "debate": 55, "research": 65, "trader": 75, "risk": 90, "decision": 100}
        _seen_stages = set()

        # Emit progress IMMEDIATELY so the UI isn't frozen at 0% while the
        # engine builds and the first analyst grinds through data-fetch + LLM.
        # The frontend ticks its elapsed timer off the heartbeats we send
        # every 0.5s, so the user sees it's alive even before node 1 completes.
        _emit("stage", stage="analysts", progress_pct=_STAGE_PCT["analysts"])
        _seen_stages.add("analysts")
        _emit("status", message="正在初始化分析引擎…")

        try:
            cfg = _build_config()
            ta = TradingAgentsGraph(debug=True, config=cfg)
            _store[aid]["status"] = "running"
            _emit("status", message="正在拉取行情数据（K线 / 财报 / 新闻），预计 1-3 分钟…")

            init_state, graph_args, _ = ta.prepare_graph_run(ticker, trade_date)
            final_state = {}
            for chunk in ta.graph.stream(init_state, **graph_args):
                node_name = list(chunk.keys())[0] if chunk else ""
                if node_name:
                    stage = _STAGES.get(node_name)
                    if stage and stage not in _seen_stages:
                        _seen_stages.add(stage)
                        _emit("stage", stage=stage, progress_pct=_STAGE_PCT.get(stage, 0))
                    agent_key = _AGENT_KEY.get(node_name) or node_name.lower().replace(" ", "_").replace("_analyst", "")
                    _emit("agent_done", agent=agent_key, node=node_name)
                    # Stream the node's own output text so the UI can show what
                    # each analyst/trader is producing in real time (then fold
                    # it away when the final dashboard renders).
                    snippet = _node_snippet(chunk.get(node_name))
                    if snippet:
                        _emit("agent_output", agent=agent_key, node=node_name, text=snippet)
                final_state = chunk

            signal = ta.finalize_graph_run(ticker, trade_date, final_state)
            report = _extract_report(final_state, ticker, name)
            _store[aid]["report"] = report
            _store[aid]["signal"] = signal
            _store[aid]["status"] = "done"
            _emit("done", report=report, signal=signal)

            # Post-analysis structured extraction (dashboard upgrade). Emits a
            # separate "structured" event so the UI can render the plain report
            # fast, then fill in the dashboard. Failure is non-fatal.
            try:
                structured = _extract_structured(report, ticker, name)
                if structured:
                    report["structured"] = structured
                    _emit("structured", structured=structured)
            except Exception as exc:
                log.warning("structured extraction failed: %s", exc)
        except Exception as exc:
            log.exception("Analysis %s failed: %s", aid, exc)
            _store[aid]["status"] = "error"
            _store[aid]["error"] = str(exc)
            _emit("error", message=str(exc))

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
                yield _sse({"type": "error",
                            "message": "分析超时（15 分钟无进展）已中止。"
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
        "status": entry["status"],
        "signal": entry["signal"],
        "report": entry["report"],
        "error": entry["error"],
        "elapsed": round(time.time() - entry["started_at"], 1),
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


def _get_last_message(obj) -> str:
    """Extract the string content from a LangGraph message object/tuple/list."""
    if not obj:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, (list, tuple)):
        # LangGraph messages: take the last one's content
        for item in reversed(obj):
            content = _get_last_message(item)
            if content:
                return content
        return ""
    # LangChain message object
    if hasattr(obj, "content"):
        content = obj.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, dict):
                    parts.append(c.get("text", ""))
                elif isinstance(c, str):
                    parts.append(c)
            return "\n".join(parts)
    return str(obj) if obj else ""


# Stable agent keys — the frontend grid uses these. (The naive
# lower/replace turned "Social Media Analyst" into "social_media", which never
# matched the grid's "social" key.)
_AGENT_KEY = {
    "Market Analyst": "market", "Social Media Analyst": "social",
    "News Analyst": "news", "Fundamentals Analyst": "fundamentals",
    "Policy Analyst": "policy", "Hot Money Analyst": "hot_money",
    "Lockup Analyst": "lockup",
}

# State fields that hold a node's main prose output, in priority order.
_REPORT_FIELDS = (
    "market_report", "sentiment_report", "news_report", "fundamentals_report",
    "policy_report", "hot_money_report", "lockup_report",
    "trader_investment_plan", "final_trade_decision", "investment_plan",
)


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
