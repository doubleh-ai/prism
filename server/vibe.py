"""Prism integration for the official HKUDS/Vibe-Trading research engine."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .auth import require_auth
from .config import (
    VIBE_ENGINE_REVISION,
    VIBE_ENGINE_TIMEOUT_SECONDS,
    VIBE_ENGINE_VERSION,
)
from .database import get_db
from .models import VibeFollowUpRequest, VibePlanRequest, validation_error_message
from .research_context import build_research_context, build_research_prompt, source_timestamp
from .vibe_engine import (
    VibeEngineError,
    engine_cancel,
    engine_create_session,
    engine_delete_session,
    engine_events,
    engine_get_messages,
    engine_health,
    engine_send_message,
    redact_engine_text,
    safe_engine_event,
    safe_engine_payload,
)

router = APIRouter(prefix="/api/vibe")
log = logging.getLogger("prism.vibe")

_ENGINE_NAME = "HKUDS/Vibe-Trading"
_ACTIVE_STATUSES = {"queued", "running", "cancelling"}
_TASKS: dict[str, asyncio.Task] = {}
_CANCEL_INFLIGHT: set[str] = set()
_A_SHARE_CODE_RE = re.compile(r"^[036]\d{5}(?:\.(?:SH|SZ|SS))?$", re.I)
_URL_RE = re.compile(r"https?://[^\s)>\]]+", re.I)

_TOOL_LABELS = {
    "load_skill": "加载研究方法",
    "web_search": "检索公开证据",
    "get_market_data": "获取行情数据",
    "get_stock_news": "核验公司新闻",
    "get_financial_statements": "核验财务报表",
    "get_fund_flow": "检查资金流",
    "get_sector_info": "获取板块数据",
    "backtest": "执行策略回测",
    "factor_analysis": "执行因子分析",
    "run_swarm": "启动多 Agent 协作",
}
_SAFE_ARGUMENT_KEYS = {
    "q",
    "query",
    "symbol",
    "ticker",
    "skill",
    "name",
    "topic",
    "start_date",
    "end_date",
    "period",
    "interval",
    "preset_name",
}


def _now() -> int:
    return int(time.time())


def _structured_text(value: Any) -> str:
    """Extract human text from sidecar content blocks without object reprs."""
    if value is None:
        return ""
    if isinstance(value, str):
        return redact_engine_text(value.replace("[object Object]", ""))
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(part for part in (_structured_text(item) for item in value) if part)
    if isinstance(value, dict):
        for key in ("markdown", "text", "content", "answer", "result", "message", "preview", "summary"):
            if key in value:
                text = _structured_text(value.get(key))
                if text:
                    return text
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return ""
    return str(value).replace("[object Object]", "")


def _clean_text(value: Any, limit: int = 2000) -> str:
    return re.sub(r"\s+", " ", _structured_text(value).strip())[:limit]


def _risk_label(value: str) -> str:
    return {"defensive": "防守", "balanced": "均衡", "aggressive": "进攻"}.get(value, value)


def _depth_label(value: str) -> str:
    return {"focused": "聚焦研究", "deep": "深度研究", "swarm": "多 Agent"}.get(value, value)


def _horizon_label(value: str) -> str:
    return {"intraday": "日内", "swing": "波段", "position": "中线", "long": "长期"}.get(value, value)


_ENGINE_ITERATION_BUDGETS = {
    "focused": 6,
    "deep": 14,
    "swarm": 28,
}

_ENGINE_DURATION_LABELS = {
    "focused": "约 1-2 分钟",
    "deep": "约 3-6 分钟",
    "swarm": "约 6-12 分钟",
}


def _engine_iteration_budget(depth: str) -> int:
    return _ENGINE_ITERATION_BUDGETS.get(str(depth or "deep"), 14)


def _engine_meta() -> dict[str, str]:
    return {
        "name": _ENGINE_NAME,
        "version": VIBE_ENGINE_VERSION,
        "revision": VIBE_ENGINE_REVISION,
        "mode": "official_sidecar",
    }


def build_vibe_plan(req: VibePlanRequest) -> dict[str, Any]:
    """Build the persisted research brief before the official engine starts."""
    prompt = _clean_text(req.prompt)
    if not prompt:
        raise HTTPException(400, "empty prompt")
    title = prompt[:42] + ("..." if len(prompt) > 42 else "")
    now = _now()
    return {
        "id": "",
        "title": title,
        "prompt": prompt,
        "research": build_research_context(engine="vibe"),
        "market": req.market,
        "horizon": req.horizon,
        "risk_level": req.risk_level,
        "risk_label": _risk_label(req.risk_level),
        "research_depth": req.research_depth,
        "depth_label": _depth_label(req.research_depth),
        "iteration_budget": _engine_iteration_budget(req.research_depth),
        "estimated_duration_label": _ENGINE_DURATION_LABELS.get(req.research_depth, "约 3-6 分钟"),
        "tags": [
            req.market,
            _horizon_label(req.horizon),
            _risk_label(req.risk_level),
            _depth_label(req.research_depth),
        ],
        "engine": _engine_meta(),
        "engine_status": "queued" if req.agent else "not_started",
        "agent_status": "official_queued" if req.agent else "not_started",
        "agent_summary": "官方研究引擎已排队，尚未产生结论。"
        if req.agent
        else "仅保存研究题目，未运行引擎。",
        "status": "queued" if req.agent else "draft",
        "stage": "queued" if req.agent else "draft",
        "stage_label": "正在建立研究任务" if req.agent else "研究题目草稿",
        "progress": 2 if req.agent else 0,
        "activity": [
            {
                "id": secrets.token_hex(5),
                "kind": "system",
                "label": "研究任务已创建",
                "status": "queued",
                "summary": "等待官方 Vibe-Trading 引擎接手",
                "at": now,
            }
        ]
        if req.agent
        else [],
        "candidates": [],
        "report_markdown": "",
        "followups": [],
        "quality": {
            "tool_calls": 0,
            "tool_success": 0,
            "tool_errors": 0,
            "tool_degraded": 0,
            "source_links": 0,
            "grade": "待研究",
            "warnings": [],
        },
        "created_at": now,
        "updated_at": now,
    }


def _research_prompt(plan: dict[str, Any]) -> str:
    depth = plan.get("research_depth") or "deep"
    if depth == "focused":
        depth_spec = (
            "这是聚焦研究：优先回答最关键的一个范围，按关键事实选择真实数据/检索工具，"
            "给出 3-5 个候选，并明确哪些问题尚未验证。达到证据门槛后立即输出报告。"
        )
    elif depth == "swarm":
        depth_spec = (
            "这是多 Agent 研究：使用 swarm 工具组织产业链、数据核验和反方审查角色，"
            "围绕核心事实分工核验，合并冲突证据后给出有依据的候选；不得为了数量编造候选。"
        )
    else:
        depth_spec = (
            "这是深度研究：按研究问题调用市场数据、财务、新闻或网页检索工具，"
            "交叉核验关键结论，并给出 6-10 个候选。不要重复调用同一失败来源；"
            "达到证据门槛后立即收敛并输出报告。"
        )

    prefix = ""
    if depth == "swarm":
        prefix = (
            "[Swarm Team Mode] Use the swarm tool to assemble the best specialist team for this task. "
            "Auto-select the most appropriate preset.\n\n"
        )

    return (
        prefix
        + f"""你是 Prism 中的研究型交易 Agent。请深入研究下面的用户问题，不执行交易，不给个性化投资指令。

用户问题：{plan.get("prompt", "")}
市场：{plan.get("market", "A股")}
研究周期：{_horizon_label(str(plan.get("horizon", "swing")))}
风险观察偏好：{plan.get("risk_label", "均衡")}
{build_research_prompt(plan.get("research") or build_research_context(engine="vibe"))}

{depth_spec}

研究协议（必须遵守）：
1. 先做范围澄清。如果输入有多义、上下游或相邻赛道，列出不同解释，让数据决定研究重点；不得把关键词直接套入预设行业分类。
2. 必须调用可用的真实工具。行情、财务、新闻、行业与网页证据不足时明确写“未验证”，不得用模型记忆补成事实。
3. 所有高影响数字都写原始值、单位、数据日期和来源。百分比与“倍数”必须区分；派生数字写计算口径，并做数量级合理性检查。
4. 每项核心结论尽量由两个独立来源交叉确认。网页证据使用可点击 Markdown 链接；若工具未返回 URL，明确标记来源名和“无可点击链接”。
5. 工具报错、数据过期、付费墙或口径冲突必须进入“数据质量与缺口”，不得静默略过。
6. 候选标的说明产业链角色、主题纯度、证据、催化、反方观点和可验证失效条件。区分事实、推断和待验证假设。
7. 不输出“买入/卖出/加仓/清仓”等操作指令，改用正向验证、负向验证和研究优先级。

最终用中文 Markdown 输出完整报告，至少包含：
- 范围澄清与本次研究边界
- 核心结论摘要
- 需求驱动与产业链分层
- 真正的约束、卡点及价值迁移
- 证据账本（事实或推断/来源链接/发布时间或未知/数据所属期间/是否交叉验证）
- 候选池（代码、名称、角色、纯度、证据、催化、风险）
- 多方与反方论证
- 正向验证、负向验证与失效条件
- 数据质量、工具失败与尚未验证的问题
- 下一步可执行的研究动作
- 本次实际使用的工具、成功/失败情况和数据截止日期
"""
    )


def _followup_prompt(plan: dict[str, Any], question: str) -> str:
    return f"""{build_research_prompt(plan.get("research") or build_research_context(engine="vibe"))}

这是对同一研究会话的追问，请沿用此前上下文，但只回答新增问题：

{question}

要求：需要新数据时继续调用工具；关键数字保留原始单位、日期和可点击来源；若先前报告有错误或证据不足，直接纠正并说明原因。用中文 Markdown 输出增量研究，不要机械重复整份旧报告。"""


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
    plan.setdefault("status", "complete")
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


def _insert_vibe_plan(username: str, plan: dict[str, Any]) -> None:
    payload = {k: v for k, v in plan.items() if not k.startswith("_")}
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
                json.dumps(payload, ensure_ascii=False),
                plan["created_at"],
            ],
        )


def _update_vibe_plan(username: str, plan_id: str, plan: dict[str, Any]) -> bool:
    plan["updated_at"] = _now()
    payload = {k: v for k, v in plan.items() if not k.startswith("_")}
    with get_db() as db:
        cur = db.execute(
            "UPDATE vibe_plans SET plan_json=? WHERE id=? AND username=?",
            [json.dumps(payload, ensure_ascii=False), plan_id, username],
        )
    return cur.rowcount > 0


def _tool_label(tool: str) -> str:
    return _TOOL_LABELS.get(tool, tool.replace("_", " ") if tool else "研究工具")


def _argument_summary(arguments: Any) -> str:
    if not isinstance(arguments, dict):
        return ""
    parts = []
    for key, value in arguments.items():
        if key not in _SAFE_ARGUMENT_KEYS or value in (None, ""):
            continue
        parts.append(f"{key}: {_clean_text(value, 120)}")
        if len(parts) >= 3:
            break
    return " · ".join(parts)


def _tool_result_summary(tool: str, preview: str, status: str) -> str:
    """Turn provider payloads into short user-facing research status text."""
    payload: Any = None
    try:
        payload = json.loads(preview)
    except Exception:
        payload = None
    lower = preview.lower()

    if status == "error":
        if any(
            token in lower
            for token in (
                "httpconnectionpool",
                "max retries exceeded",
                "connection refused",
                "name or service not known",
                "temporary failure in name resolution",
            )
        ):
            return "外部数据源暂时不可达，已跳过并记录缺口"
        if "timed out" in lower or "timeout" in lower:
            return "外部数据源响应超时，已记录为数据缺口"
        return "工具调用失败，已记录为数据缺口"

    if status == "warning":
        no_results = "no results found" in lower
        if isinstance(payload, dict) and payload.get("results") == []:
            no_results = True
        if no_results:
            return "本次检索未找到可用公开证据"
        if "timed out" in lower or "timeout" in lower:
            return "数据源响应超时，本次结果未采用"
        return "工具返回空结果或降级数据，本次结果未采用"

    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, list):
            return f"获取到 {len(results)} 条有效结果" if results else "工具已完成"
        rows = payload.get("data")
        if isinstance(rows, list):
            return f"获取到 {len(rows)} 条有效数据" if rows else "工具已完成"
    return "工具已返回有效数据"


def _append_activity(plan: dict[str, Any], item: dict[str, Any]) -> None:
    item.setdefault("id", secrets.token_hex(5))
    item.setdefault("at", _now())
    activity = list(plan.get("activity") or [])
    activity.append(item)
    plan["activity"] = activity[-80:]


def _begin_attempt(plan: dict[str, Any], attempt_id: str) -> None:
    """Telemetry is scoped to an attempt; reconnecting the same attempt preserves it."""
    if (plan.get("quality") or {}).get("attempt_id") == attempt_id:
        return
    plan["quality"] = {
        "attempt_id": attempt_id,
        "tool_calls": 0,
        "tool_success": 0,
        "tool_errors": 0,
        "tool_degraded": 0,
        "tool_unmatched": 0,
        "source_links": 0,
        "verified_sources": 0,
        "grade": "来源待核验",
        "warnings": [],
    }
    plan["attempt_event_ids"] = []
    plan["last_engine_event_id"] = ""


def _apply_engine_event(plan: dict[str, Any], event_type: str, data: dict[str, Any], event_id: str) -> None:
    data = safe_engine_event(event_type, data)
    attempt_id = str(plan.get("engine_attempt_id") or "")
    event_attempt = str(data.get("attempt_id") or data.get("linked_attempt_id") or "")
    if event_attempt and attempt_id and event_attempt != attempt_id:
        return
    quality = dict(plan.get("quality") or {})
    seen = list(plan.get("attempt_event_ids") or [])
    call_id = str(data.get("tool_call_id") or data.get("call_id") or "")
    event_key = (
        f"{event_type}:{call_id}" if call_id and event_type in {"tool_call", "tool_result"} else event_id
    )
    if event_key and event_key in seen:
        return
    if event_key:
        plan["attempt_event_ids"] = [*seen, event_key]
    quality.setdefault("tool_calls", 0)
    quality.setdefault("tool_success", 0)
    quality.setdefault("tool_errors", 0)
    quality.setdefault("tool_degraded", 0)
    tool = _clean_text(data.get("tool"), 80)

    if event_type == "attempt.started":
        plan.update(
            stage="planning", stage_label="正在拆解研究问题", progress=max(6, int(plan.get("progress") or 0))
        )
    elif event_type == "tool_call":
        quality["tool_calls"] += 1
        plan.update(
            stage="researching",
            stage_label=f"正在{_tool_label(tool)}",
            progress=min(78, 12 + quality["tool_calls"] * 4),
        )
        _append_activity(
            plan,
            {
                "kind": "tool",
                "tool": tool,
                "label": _tool_label(tool),
                "status": "running",
                "summary": _argument_summary(data.get("arguments")),
                "event_id": event_id,
                "attempt_id": attempt_id,
                "tool_call_id": call_id or None,
            },
        )
    elif event_type == "tool_result":
        preview = _structured_text(data.get("preview"))
        failed_payload = bool(
            data.get("status") in {"error", "failed"}
            or re.search(r'(?i)"(?:ok|success)"\s*:\s*false|"status"\s*:\s*"error"', preview)
        )
        degraded_payload = bool(
            not failed_payload
            and (
                not preview.strip()
                or re.search(
                    r'(?i)no results found|"error"\s*:|not available|unavailable|timed out|无(?:可用)?(?:结果|数据)|未(?:找到|获取)|不可达|超时',
                    preview,
                )
            )
        )
        status = "error" if failed_payload else ("warning" if degraded_payload else "complete")
        result_summary = _tool_result_summary(tool, preview, status)
        result_detail = _clean_text(preview, 800) if status == "complete" else result_summary
        activity = list(plan.get("activity") or [])
        running = [
            item
            for item in activity
            if item.get("kind") == "tool"
            and item.get("tool") == tool
            and item.get("status") == "running"
            and item.get("attempt_id", "") == attempt_id
            and (not call_id or item.get("tool_call_id") == call_id)
        ]
        # Tool names alone cannot distinguish simultaneous calls to the same provider.
        matched = len(running) == 1
        if matched:
            item = running[0]
            item.update(
                status=status,
                elapsed_ms=int(data.get("elapsed_ms") or 0),
                result=result_summary,
                detail=result_detail,
                retrieved_at=source_timestamp(data.get("timestamp")),
                observed_at=build_research_context()["as_of"],
                published_at=None,
            )
            key = {"error": "tool_errors", "warning": "tool_degraded", "complete": "tool_success"}[status]
            quality[key] += 1
        else:
            quality["tool_unmatched"] = int(quality.get("tool_unmatched") or 0) + 1
        if not matched:
            _append_activity(
                plan,
                {
                    "kind": "tool",
                    "tool": tool,
                    "label": _tool_label(tool),
                    "status": status,
                    "result": result_summary,
                    "detail": result_detail,
                    "elapsed_ms": int(data.get("elapsed_ms") or 0),
                    "event_id": event_id,
                    "attempt_id": attempt_id,
                    "tool_call_id": call_id or None,
                    "association": "unmatched",
                    "retrieved_at": source_timestamp(data.get("timestamp")),
                    "observed_at": build_research_context()["as_of"],
                    "published_at": None,
                },
            )
        else:
            plan["activity"] = activity[-80:]
    elif event_type == "tool_progress":
        message = _clean_text(data.get("message") or data.get("stage"), 180)
        if message:
            plan["stage_label"] = message
    elif event_type == "thinking_done":
        plan.update(stage="synthesizing", stage_label="正在整理采集记录并撰写报告", progress=88)
    elif event_type == "attempt.completed":
        plan.update(stage="finalizing", stage_label="正在整理完整报告", progress=96)
    elif event_type in {"attempt.failed", "attempt.cancelled"}:
        plan["stage_label"] = _clean_text(data.get("error") or data.get("summary"), 300) or "研究任务未完成"

    plan["quality"] = quality
    if event_id:
        plan["last_engine_event_id"] = event_id
    started = int(plan.get("started_at") or plan.get("created_at") or _now())
    plan["elapsed_seconds"] = max(0, _now() - started)


def _extract_report_title(report: str) -> str:
    for line in report.splitlines():
        if line.startswith("# "):
            title = re.sub(r"[*_`#]", "", line[2:]).strip()
            title = re.sub(r"^[^\w\u4e00-\u9fff]+", "", title)
            if title:
                return title[:120]
    return ""


def _extract_report_summary(report: str) -> str:
    for line in report.splitlines():
        text = line.lstrip("> ").strip()
        text = re.sub(r"[*_`]", "", text)
        if line.lstrip().startswith(">") and len(text) >= 40:
            return text[:600]
    for block in re.split(r"\n\s*\n", report):
        text = re.sub(r"[#>*_`|\-]", " ", block)
        text = _clean_text(text, 600)
        if len(text) >= 60:
            return text
    return "官方引擎已完成研究，请查看完整报告。"


def _clean_markdown_cell(value: str, limit: int = 180) -> str:
    value = re.sub(r"[*_`~]", "", value or "")
    return _clean_text(value, limit)


def _extract_candidates(report: str) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    lines = report.splitlines()

    # The official engine commonly emits one heading per candidate instead of
    # a single table: "#### 候选1：澜起科技（688008.SH）".
    heading_re = re.compile(
        r"^#{2,6}\s*候选\s*\d+\s*[:：]\s*(?P<name>[^（(\n]{1,60})[（(]"
        r"(?P<code>[036]\d{5})(?:\.(?:SH|SZ|SS))?[）)]",
        re.I,
    )
    for index, line in enumerate(lines):
        match = heading_re.match(line.strip())
        if not match:
            continue
        code = match.group("code")
        if code in seen:
            continue
        detail_lines = []
        role = "报告候选"
        for following in lines[index + 1 : index + 7]:
            if following.lstrip().startswith("#"):
                break
            raw_following = following.strip()
            if raw_following.startswith("|"):
                cells = [
                    _clean_markdown_cell(cell, 220)
                    for cell in raw_following.strip("|").split("|")
                    if _clean_markdown_cell(cell, 220)
                ]
                if len(cells) < 2 or cells[0] in {"维度", "内容"}:
                    continue
                label, value = cells[0], " / ".join(cells[1:])
                if set(label + value) <= {"-", ":", " "}:
                    continue
                detail = f"{label}：{value}"
            else:
                detail = _clean_markdown_cell(following, 260)
            if not detail or set(detail) <= {"-", "|", ":", " "}:
                continue
            if "产业链角色" in detail or detail.startswith("角色"):
                role = detail.split("：", 1)[-1].split(":", 1)[-1].strip()[:160]
            detail_lines.append(detail)
            if len(detail_lines) >= 2:
                break
        seen.add(code)
        candidates.append(
            {
                "ticker": code,
                "name": _clean_markdown_cell(match.group("name"), 80),
                "role": role or "报告候选",
                "why": "；".join(detail_lines)[:500] or "详见完整报告中的证据与风险说明。",
                "source": "official_report",
            }
        )
        if len(candidates) >= 12:
            return candidates

    if candidates:
        return candidates

    for line in lines:
        if not line.lstrip().startswith("|"):
            continue
        cells = [_clean_markdown_cell(cell) for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        code_index = -1
        code = ""
        for index, cell in enumerate(cells[:4]):
            match = re.fullmatch(r"([036]\d{5})(?:\.(SH|SZ|SS))?", cell, re.I)
            if match:
                code = match.group(1)
                code_index = index
                break
        if not code or code in seen or code_index + 1 >= len(cells):
            continue
        name = cells[code_index + 1]
        if not name or _A_SHARE_CODE_RE.match(name):
            name = code
        role = cells[code_index + 3] if code_index + 3 < len(cells) else "研究候选"
        detail_cells = [cell for cell in cells[code_index + 4 :] if cell]
        why = "；".join(detail_cells[:2])[:500]
        seen.add(code)
        candidates.append(
            {
                "ticker": code,
                "name": name[:80],
                "role": role[:160] or "研究候选",
                "why": why or "详见完整报告中的证据与风险说明。",
                "source": "official_report",
            }
        )
        if len(candidates) >= 12:
            break
    return candidates


def _quality_after_report(plan: dict[str, Any], report: str) -> dict[str, Any]:
    quality = dict(plan.get("quality") or {})
    links = len(set(_URL_RE.findall(report)))
    quality["source_links"] = links
    warnings = []
    calls = int(quality.get("tool_calls") or 0)
    success = int(quality.get("tool_success") or 0)
    errors = int(quality.get("tool_errors") or 0)
    degraded = int(quality.get("tool_degraded") or 0)
    quality.update(
        tool_calls=calls,
        tool_success=success,
        tool_errors=errors,
        tool_degraded=degraded,
    )
    gap_section = re.search(
        r"(?ims)^#{1,4}\s*(?:数据质量|工具失败|数据缺口)[^\n]*\n(?P<body>.*?)(?=^#{1,4}\s|\Z)",
        report,
    )
    gap_lines = []
    if gap_section:
        for line in gap_section.group("body").splitlines():
            cleaned = re.sub(r"[|>*_`#\-:]", " ", line).strip()
            if len(cleaned) >= 8 and re.search(r"失败|不可达|未获取|未验证|超时|跳过|降级", cleaned):
                gap_lines.append(cleaned)
    reported_gaps = min(99, len(dict.fromkeys(gap_lines)))
    quality["reported_gaps"] = reported_gaps
    warnings.append("报告中的链接与工具次数不代表事实已经核验；关键结论仍需对照原始披露和数据期间复核。")
    if not links:
        warnings.append("正文未提供可追溯链接，来源待补充。")
    if quality.get("tool_unmatched"):
        warnings.append("部分工具返回无法关联到本轮具体调用，未计为成功采集。")
    if errors:
        warnings.append(f"有 {errors} 次工具调用失败，报告已要求保留数据缺口。")
    if degraded:
        warnings.append(f"有 {degraded} 次工具返回空结果或降级数据，不计为有效成功。")
    if reported_gaps:
        warnings.append(f"报告披露 {reported_gaps} 项数据源或验证缺口，请结合对应章节复核。")
    quality.update(
        grade="来源待核验",
        grade_basis="unverified_report_links",
        reported_links=links,
        verified_sources=0,
        warnings=warnings,
    )
    return quality


async def _answer_for_attempt(session_id: str, attempt_id: str, retries: int = 15) -> str:
    retries = max(1, retries)
    for index in range(retries):
        messages = await engine_get_messages(session_id, limit=100)
        assistants = [
            item
            for item in messages
            if item.get("role") == "assistant" and item.get("linked_attempt_id") == attempt_id
        ]
        if assistants:
            return _structured_text(assistants[-1].get("content")).strip()
        if index < retries - 1:
            await asyncio.sleep(min(1.0, 0.25 * (index + 1)))
    return ""


def _finish_with_report(plan: dict[str, Any], report: str, attempt_id: str) -> None:
    turn_id = str(plan.get("current_turn_id") or "")
    if turn_id:
        followups = list(plan.get("followups") or [])
        for item in followups:
            if item.get("id") == turn_id:
                item.update(
                    answer_markdown=report,
                    status="complete",
                    completed_at=_now(),
                    attempt_id=attempt_id,
                    research=plan.get("research"),
                    quality=_quality_after_report(plan, report),
                )
                break
        plan["followups"] = followups
    else:
        plan["report_markdown"] = report
        plan["report_research"] = plan.get("research")
        plan["report_quality"] = _quality_after_report(plan, report)
        report_title = _extract_report_title(report)
        if report_title:
            plan["title"] = report_title
        plan["agent_summary"] = _extract_report_summary(report)

    extracted = _extract_candidates(report)
    if extracted:
        existing = {str(item.get("ticker")): item for item in (plan.get("candidates") or [])}
        for item in extracted:
            existing[item["ticker"]] = item
        plan["candidates"] = list(existing.values())[:12]

    plan["quality"] = _quality_after_report(plan, report)
    plan.update(
        status="complete",
        engine_status="complete",
        agent_status="official_complete",
        stage="complete",
        stage_label="研究完成",
        progress=100,
        completed_at=_now(),
        current_turn_id="",
    )
    started = int(plan.get("started_at") or plan.get("created_at") or _now())
    plan["elapsed_seconds"] = max(0, _now() - started)


def _mark_failed(plan: dict[str, Any], message: str) -> None:
    message = _clean_text(message, 500) or "官方研究引擎未能完成任务"
    turn_id = str(plan.get("current_turn_id") or "")
    if turn_id:
        followups = list(plan.get("followups") or [])
        for item in followups:
            if item.get("id") == turn_id:
                item.update(status="failed", error=message, completed_at=_now())
                break
        plan["followups"] = followups
    plan.update(
        status="failed",
        engine_status="failed",
        agent_status="official_failed",
        agent_summary="官方引擎没有返回最终报告；Prism 未用规则结果冒充分析。",
        stage="failed",
        stage_label=message,
        current_turn_id="",
    )
    _append_activity(
        plan,
        {
            "kind": "system",
            "label": "研究未完成",
            "status": "error",
            "summary": message,
        },
    )


async def _monitor_attempt(plan_id: str, username: str, session_id: str, attempt_id: str) -> None:
    plan = _load_vibe_plan(plan_id, username)
    if not plan or plan.get("status") not in _ACTIVE_STATUSES:
        return

    answer = await _answer_for_attempt(session_id, attempt_id, retries=1)
    if answer:
        _finish_with_report(plan, answer, attempt_id)
        _update_vibe_plan(username, plan_id, plan)
        return

    terminal_type = ""
    async for event_type, data, event_id in engine_events(session_id):
        plan = _load_vibe_plan(plan_id, username)
        if not plan or plan.get("status") not in _ACTIVE_STATUSES:
            return
        event_attempt = str(data.get("attempt_id") or data.get("linked_attempt_id") or "")
        if event_attempt and event_attempt != attempt_id:
            continue
        _apply_engine_event(plan, event_type, data, event_id)
        _update_vibe_plan(username, plan_id, plan)
        if event_type in {"attempt.completed", "attempt.failed", "attempt.cancelled"}:
            terminal_type = event_type
            break

    plan = _load_vibe_plan(plan_id, username)
    if not plan or plan.get("status") not in _ACTIVE_STATUSES:
        return
    if terminal_type == "attempt.completed":
        answer = await _answer_for_attempt(session_id, attempt_id)
        if answer:
            _finish_with_report(plan, answer, attempt_id)
        else:
            _mark_failed(plan, "引擎已结束，但没有找到最终报告")
    elif terminal_type == "attempt.cancelled":
        _finish_cancelled(plan)
    elif terminal_type == "attempt.failed":
        _mark_failed(plan, "研究引擎未能完成任务")
    else:
        answer = await _answer_for_attempt(session_id, attempt_id)
        if answer:
            _finish_with_report(plan, answer, attempt_id)
        else:
            _mark_failed(plan, "官方引擎事件流提前结束，未返回报告")
    _update_vibe_plan(username, plan_id, plan)


async def _run_official_research(plan_id: str, username: str, resume: bool = False) -> None:
    plan = _load_vibe_plan(plan_id, username)
    if not plan or plan.get("status") not in _ACTIVE_STATUSES:
        return
    try:
        session_id = str(plan.get("engine_session_id") or "")
        attempt_id = str(plan.get("engine_attempt_id") or "")
        if resume and session_id and attempt_id:
            started = int(plan.get("started_at") or plan.get("updated_at") or _now())
            if _now() - started > VIBE_ENGINE_TIMEOUT_SECONDS + 120:
                _mark_failed(plan, "研究任务在 Prism 重启后已超过恢复时限")
                _update_vibe_plan(username, plan_id, plan)
                return
            _begin_attempt(plan, attempt_id)
            _update_vibe_plan(username, plan_id, plan)
            await _monitor_attempt(plan_id, username, session_id, attempt_id)
            return

        if not session_id:
            plan.update(
                status="running",
                engine_status="connecting",
                agent_status="official_running",
                stage="connecting",
                stage_label="正在连接官方 Vibe-Trading 引擎",
                progress=4,
                started_at=_now(),
            )
            _update_vibe_plan(username, plan_id, plan)
            session = await engine_create_session(
                str(plan.get("title") or "Prism Vibe research"),
                {
                    "source": "prism",
                    "research_depth": plan.get("research_depth") or "deep",
                    "max_iterations": _engine_iteration_budget(str(plan.get("research_depth") or "deep")),
                },
            )
            session_id = str(session.get("session_id") or "")
            if not session_id:
                raise VibeEngineError("engine did not return a session id")
            plan = _load_vibe_plan(plan_id, username) or plan
            plan["engine_session_id"] = session_id
            _update_vibe_plan(username, plan_id, plan)

        plan.setdefault("research", build_research_context(engine="vibe"))
        _update_vibe_plan(username, plan_id, plan)
        turn_id = str(plan.get("current_turn_id") or "")
        if turn_id:
            followup = next((item for item in plan.get("followups") or [] if item.get("id") == turn_id), None)
            if not followup:
                raise VibeEngineError("follow-up turn was not found")
            content = _followup_prompt(plan, str(followup.get("question") or ""))
        else:
            content = _research_prompt(plan)
        sent = await engine_send_message(session_id, content)
        attempt_id = str(sent.get("attempt_id") or "")
        if not attempt_id:
            raise VibeEngineError("engine did not return an attempt id")
        plan = _load_vibe_plan(plan_id, username) or plan
        _begin_attempt(plan, attempt_id)
        plan.update(
            status="running",
            engine_status="running",
            agent_status="official_running",
            agent_summary="官方引擎正在调用真实研究工具，最终报告尚未生成。",
            engine_attempt_id=attempt_id,
            stage="planning",
            stage_label="正在拆解研究问题",
            progress=max(6, int(plan.get("progress") or 0)),
        )
        if turn_id:
            for item in plan.get("followups") or []:
                if item.get("id") == turn_id:
                    item.update(status="running", attempt_id=attempt_id)
                    break
        _update_vibe_plan(username, plan_id, plan)
        await _monitor_attempt(plan_id, username, session_id, attempt_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning("official vibe research failed plan=%s", plan_id)
        plan = _load_vibe_plan(plan_id, username)
        if plan and plan.get("status") in _ACTIVE_STATUSES:
            _mark_failed(plan, "研究引擎调用失败，请稍后重试")
            _update_vibe_plan(username, plan_id, plan)


def _start_task(plan_id: str, username: str, resume: bool = False, force_after: bool = False) -> None:
    existing = _TASKS.get(plan_id)
    if existing and not existing.done():
        if not force_after:
            return

        async def _after_existing() -> None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await existing
            await _run_official_research(plan_id, username, resume=resume)

        task = asyncio.create_task(_after_existing())
    else:
        task = asyncio.create_task(_run_official_research(plan_id, username, resume=resume))
    _TASKS[plan_id] = task

    def _cleanup(done: asyncio.Task) -> None:
        if _TASKS.get(plan_id) is done:
            _TASKS.pop(plan_id, None)
        if not done.cancelled():
            with contextlib.suppress(Exception):
                done.exception()

    task.add_done_callback(_cleanup)


def _ensure_task(plan: dict[str, Any], username: str) -> None:
    if plan.get("status") not in _ACTIVE_STATUSES:
        return
    existing = _TASKS.get(str(plan.get("id")))
    if existing and not existing.done():
        return
    if plan.get("engine_session_id") and plan.get("engine_attempt_id"):
        _start_task(str(plan["id"]), username, resume=True)
        return
    # A GET must never resend a model request after an uncertain process loss.
    plan.update(
        status="interrupted",
        stage="interrupted",
        stage_label="原任务已中断",
        agent_summary="原执行进程已结束，无法确认引擎 attempt；未自动重新发起研究。",
    )
    _update_vibe_plan(username, str(plan["id"]), plan)


def get_owned_plan(username: str, plan_id: str, *, recover: bool = True) -> dict:
    plan = _load_vibe_plan(plan_id, username)
    if not plan:
        raise HTTPException(404, "plan not found")
    if recover:
        _ensure_task(plan, username)
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
        _ensure_task(plan, username)
        plan.pop("_username", None)
    return plans


@router.get("/engine")
async def get_vibe_engine(request: Request):
    require_auth(request)
    try:
        health = await engine_health()
        status = "healthy" if health.get("status") == "healthy" else "unknown"
        return {"engine": _engine_meta(), "status": status}
    except VibeEngineError:
        return {"engine": _engine_meta(), "status": "unavailable", "detail": "研究引擎暂时不可用"}


@router.post("/plan")
async def create_vibe_plan(request: Request):
    username = require_auth(request)
    try:
        body = await request.json()
        req = VibePlanRequest(**(body or {}))
    except Exception as exc:
        raise HTTPException(422, validation_error_message(exc))
    plan = build_vibe_plan(req)
    plan["id"] = secrets.token_hex(10)
    _insert_vibe_plan(username, plan)
    if req.agent:
        _start_task(plan["id"], username)
    return {"plan": plan}


@router.get("/plans")
async def list_vibe_plans(request: Request, limit: int = 30):
    return {"plans": list_owned_plans(require_auth(request), limit)}


@router.get("/plans/{plan_id}")
async def get_vibe_plan(plan_id: str, request: Request):
    return {"plan": get_owned_plan(require_auth(request), plan_id)}


@router.post("/plans/{plan_id}/follow-up")
async def follow_up_vibe_plan(plan_id: str, request: Request):
    username = require_auth(request)
    try:
        body = await request.json()
        req = VibeFollowUpRequest(**(body or {}))
    except Exception as exc:
        raise HTTPException(422, validation_error_message(exc))
    return {"plan": start_owned_followup(username, plan_id, req.question)}


def start_owned_followup(username: str, plan_id: str, question: str) -> dict:
    req = VibeFollowUpRequest(question=question)
    plan = _load_vibe_plan(plan_id, username)
    if not plan:
        raise HTTPException(404, "plan not found")
    if plan.get("status") in _ACTIVE_STATUSES or plan_id in _CANCEL_INFLIGHT:
        raise HTTPException(409, "research is already running")
    if not plan.get("engine_session_id") or not plan.get("report_markdown"):
        raise HTTPException(409, "complete the first official research report before following up")
    plan.setdefault("report_research", plan.get("research"))
    plan.setdefault("report_quality", dict(plan.get("quality") or {}))
    turn_id = secrets.token_hex(8)
    research = build_research_context(engine="vibe")
    _begin_attempt(plan, "")
    followups = list(plan.get("followups") or [])
    followups.append(
        {
            "id": turn_id,
            "question": _clean_text(req.question, 3000),
            "answer_markdown": "",
            "research": research,
            "status": "queued",
            "created_at": _now(),
        }
    )
    plan.update(
        followups=followups,
        research=research,
        engine_attempt_id="",
        current_turn_id=turn_id,
        status="queued",
        engine_status="queued",
        agent_status="official_queued",
        stage="followup_queued",
        stage_label="追问已排队",
        progress=3,
        started_at=_now(),
    )
    _append_activity(
        plan,
        {
            "kind": "turn",
            "label": "开始上下文追问",
            "status": "queued",
            "summary": _clean_text(req.question, 180),
        },
    )
    _update_vibe_plan(username, plan_id, plan)
    _start_task(plan_id, username, force_after=True)
    plan.pop("_username", None)
    return plan


@router.post("/plans/{plan_id}/cancel")
async def cancel_vibe_plan(plan_id: str, request: Request):
    return await cancel_owned_plan(require_auth(request), plan_id)


def _finish_cancelled(plan: dict) -> None:
    plan.update(
        status="cancelled",
        engine_status="cancelled",
        agent_status="official_cancelled",
        stage="cancelled",
        stage_label="研究已停止",
        current_turn_id="",
    )
    for item in plan.get("followups") or []:
        if item.get("status") in {"queued", "running"}:
            item.update(status="cancelled", completed_at=_now())


async def cancel_owned_plan(username: str, plan_id: str) -> dict:
    plan = get_owned_plan(username, plan_id, recover=False)
    if plan.get("status") not in _ACTIVE_STATUSES:
        return {"plan": plan, "cancelled": False, "cancel_requested": False}
    session_id = str(plan.get("engine_session_id") or "")
    if not session_id:
        task = _TASKS.pop(plan_id, None)
        if task and not task.done():
            task.cancel()
        _finish_cancelled(plan)
        requested, confirmed = True, True
    else:
        requested, confirmed = False, False
        # Prevent a follow-up from starting while the cancellation response for
        # this exact attempt is still in flight.
        plan.update(status="cancelling")
        _update_vibe_plan(username, plan_id, plan)
        _CANCEL_INFLIGHT.add(plan_id)
        try:
            response = await engine_cancel(session_id)
            requested = True
            confirmed = response.get("cancelled") is True or response.get("status") == "cancelled"
        except VibeEngineError:
            # Preserve monitoring and the exact attempt while cancellation is unknown.
            pass
        finally:
            _CANCEL_INFLIGHT.discard(plan_id)
        latest = get_owned_plan(username, plan_id, recover=False)
        if latest.get("status") not in _ACTIVE_STATUSES:
            _update_vibe_plan(username, plan_id, latest)
            return {
                "plan": latest,
                "cancelled": latest.get("status") == "cancelled",
                "cancel_requested": requested,
            }
        plan = latest
        if confirmed:
            task = _TASKS.pop(plan_id, None)
            if task and not task.done():
                task.cancel()
            _finish_cancelled(plan)
        else:
            plan.update(
                status="cancelling",
                stage="cancelling",
                stage_label="正在核实停止状态",
                agent_summary="已请求引擎停止，等待确认。"
                if requested
                else "取消请求未确认，仍保留原任务监控。",
            )
    _append_activity(
        plan,
        {
            "kind": "system",
            "label": "研究已停止" if confirmed else "请求停止研究",
            "status": "cancelled" if confirmed else "cancelling",
            "summary": "引擎确认停止" if confirmed else "未确认停止，不视为已取消",
        },
    )
    _update_vibe_plan(username, plan_id, plan)
    return {"plan": plan, "cancelled": confirmed, "cancel_requested": requested}


@router.delete("/plans/{plan_id}")
async def delete_vibe_plan(plan_id: str, request: Request):
    username = require_auth(request)
    plan = _load_vibe_plan(plan_id, username)
    if not plan:
        raise HTTPException(404, "plan not found")
    task = _TASKS.pop(plan_id, None)
    if task and not task.done():
        task.cancel()
    session_id = str(plan.get("engine_session_id") or "")
    if session_id:
        try:
            await engine_cancel(session_id)
            await engine_delete_session(session_id)
        except VibeEngineError as exc:
            log.warning("failed to remove official vibe session %s: %s", session_id, exc)
    with get_db() as db:
        db.execute("DELETE FROM vibe_plans WHERE id=? AND username=?", [plan_id, username])
    return {"ok": True}
