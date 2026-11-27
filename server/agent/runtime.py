"""Prism's owned model/tool loop, durable delivery and cooperative cancellation.

HTTP readers only replay persisted events. They never own or restart execution.
No subprocess, filesystem skills, MCP client or third-party agent SDK is used.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
import weakref
from collections.abc import Awaitable
from dataclasses import dataclass, fields, replace

from fastapi import HTTPException

from .. import config
from ..capabilities import native_run_context
from ..database import get_db
from ..legacy_history import init_legacy_history_db, read_legacy_history
from ..preferences import profile_prompt
from ..research import (
    append_run_gap,
    create_research_run,
    finalize_research_run,
    ingest_tool_result,
    model_review,
    set_run_status,
    upgrade_candidates,
)
from ..research_context import EvidenceTracker, build_research_context
from ..source_urls import redact_source_text, source_display_payload
from ..work_lifecycle import Lease, Manager, WorkHandle
from . import provider, store
from .prompts import system_prompt
from .public_text import public_answer_text

log = logging.getLogger(__name__)

model_stream = provider.stream


@dataclass(frozen=True)
class Limits:
    model_calls: int = 10
    tool_calls: int = 20
    server_search_calls: int = 4
    output_tokens_per_call: int = 4096
    token_continuations: int = 4
    wall_seconds: float = 480
    output_characters: int = 60000
    context_characters: int = 200000
    concurrent_runs: int = 4


# Only the loop-budget fields above may be overridden per model. Concurrency is a
# process-wide resource, so a catalog entry cannot change it.
_MODEL_LIMIT_FIELDS = tuple(field.name for field in fields(Limits) if field.name != "concurrent_runs")


def _model_limits(base: Limits, model_cfg: dict) -> tuple[Limits, int]:
    """Overlay a model catalog entry's optional ``limits`` on the global defaults.

    A model entry carries the budgets measured for that provider; any field it
    does not name keeps the process-wide ``Limits`` default byte-for-byte, so
    models without a catalog override (DeepSeek) behave exactly as before. The
    wrap-up report budget is not a ``Limits`` field: ``wrap_up_output_tokens`` in
    the same mapping replaces ``_WRAP_UP_OUTPUT_TOKENS`` for that model only.
    Malformed values are ignored rather than trusted, so a broken catalog can
    never raise a budget.
    """
    wrap_up = _WRAP_UP_OUTPUT_TOKENS
    override = model_cfg.get("limits") if isinstance(model_cfg, dict) else None
    if not isinstance(override, dict):
        return base, wrap_up
    values = {}
    for name in _MODEL_LIMIT_FIELDS:
        value = override.get(name)
        if type(value) not in {int, float} or value <= 0:
            continue
        values[name] = value
    value = override.get("wrap_up_output_tokens")
    if type(value) is int and value > 0:
        wrap_up = value
    return (replace(base, **values) if values else base), wrap_up


def _effective_limits(base: Limits, model_cfg: dict) -> tuple[Limits, int]:
    """Merge model-catalog limits with the deployment route caps (``min``).

    Route caps are deployment policy: they may only tighten a budget, never
    loosen it. They are applied to the model's merged budgets so a cap of e.g.
    6 calls still wins over a catalog override of 12.
    """
    route = model_cfg.get("_purpose_route", {}) if isinstance(model_cfg, dict) else {}
    model_limits, wrap_up = _model_limits(base, model_cfg)
    limits = replace(
        model_limits,
        model_calls=min(model_limits.model_calls, route.get("model_calls_max", model_limits.model_calls)),
        output_tokens_per_call=min(
            model_limits.output_tokens_per_call,
            route.get("output_tokens_max", model_limits.output_tokens_per_call),
        ),
        wall_seconds=min(model_limits.wall_seconds, route.get("wall_seconds_max", model_limits.wall_seconds)),
    )
    # The wrap-up report may exceed one normal call's output budget; a
    # configured route ceiling still applies when the route sets one.
    wrap_up = min(wrap_up, route.get("output_tokens_max", wrap_up))
    return limits, wrap_up


class RunStopped(Exception):
    pass


class BudgetExceeded(Exception):
    def __init__(self, reason="unknown"):
        self.reason = reason
        super().__init__(reason)


@dataclass
class Outcome:
    user: str
    run_id: str
    executor_id: str
    status: str
    text: str = ""
    error_code: str | None = None
    provider_diagnostic: dict | None = None
    limit_reason: str | None = None
    research_revision: str | None = None
    message_id: int | None = None
    message_payload: str | None = None
    abandoned: bool = False
    observer_only: bool = False


@dataclass
class Active:
    handle: WorkHandle
    cancel: asyncio.Event


def init_runtime_db():
    # Both schema helpers are additive and idempotent after interrupted startup.
    store.init_agent_db()
    init_legacy_history_db()


def _legacy_prefix_count(db, user: str, conv_id: str, conversation):
    """The prefix count comes exclusively from an owned, imported SQL row."""
    imported = read_legacy_history(db, conv_id, user)
    if imported is not None:
        return imported["user_turn_count"]
    if conversation["session_id"] or conversation["project_dir"]:
        return None
    return 0


def ensure_dialogue(user: str, run: dict) -> dict:
    """One input message per request, including queued/cancelled requests."""
    init_runtime_db()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            conversation = db.execute(
                "SELECT c.session_id,c.project_dir FROM conversations c JOIN users u ON u.username=c.username "
                "WHERE c.id=? AND c.username=? AND c.deleted_at IS NULL AND u.disabled_at IS NULL",
                [run["conv_id"], user],
            ).fetchone()
            if not conversation:
                raise HTTPException(404, "对话不存在或不可访问")
            legacy_count = _legacy_prefix_count(db, user, run["conv_id"], conversation)
            if legacy_count is None:
                raise HTTPException(
                    409, "旧对话历史等待管理员迁入平台存储，完成后可继续此对话。原历史保持不变。"
                )
            existing = db.execute(
                "SELECT history_anchor_json FROM native_agent_dialogues WHERE run_id=? AND username=?",
                [run["run_id"], user],
            ).fetchone()
            if existing:
                anchor = json.loads(existing[0])
            else:
                boundary = db.execute(
                    "SELECT COALESCE(MAX(id),0) FROM messages WHERE conv_id=?", [run["conv_id"]]
                ).fetchone()[0]
                anchor = {
                    "db_message_id": boundary,
                    "native_db_takeover": True,
                    "jsonl_user_turns": legacy_count,
                }
                cursor = db.execute(
                    "INSERT INTO messages(conv_id,role,content) VALUES(?, 'user', ?)",
                    [run["conv_id"], run["question"]],
                )
                db.execute(
                    "INSERT INTO native_agent_dialogues(run_id,username,conv_id,user_message_id,history_anchor_json) "
                    "VALUES(?,?,?,?,?)",
                    [run["run_id"], user, run["conv_id"], cursor.lastrowid, json.dumps(anchor)],
                )
                db.execute(
                    "UPDATE conversations SET title=? WHERE id=? AND username=? AND title='新对话'",
                    [run["question"][:40], run["conv_id"], user],
                )
            db.execute("COMMIT")
            return anchor
        except BaseException:
            db.execute("ROLLBACK")
            raise


def _save_assistant_report(user: str, run: dict, payload: str, report: dict) -> int:
    """Serialize the full owner check and final dialogue/report writes together."""
    from ..reports import save_report_snapshot

    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if not db.execute(
                "SELECT 1 FROM conversations c JOIN users u ON u.username=c.username "
                "WHERE c.id=? AND c.username=? AND c.deleted_at IS NULL AND u.disabled_at IS NULL",
                [run["conv_id"], user],
            ).fetchone():
                raise HTTPException(404, "对话不存在或不可访问")
            cursor = db.execute(
                "INSERT INTO messages(conv_id,role,content) VALUES(?,'assistant',?)",
                [run["conv_id"], payload],
            )
            updated = db.execute(
                "UPDATE native_agent_dialogues SET assistant_message_id=? WHERE run_id=? AND username=?",
                [cursor.lastrowid, run["run_id"], user],
            )
            if updated.rowcount != 1:
                raise RuntimeError("native_dialogue_persistence_unconfirmed")
            save_report_snapshot(user, report)
            db.execute("COMMIT")
            return cursor.lastrowid
        except BaseException:
            db.execute("ROLLBACK")
            raise


def _history(user: str, conv_id: str, before: int) -> list[dict]:
    # History context is built only from public, owned platform records.
    from ..conversations import _merge_saved_turns

    init_legacy_history_db()
    with get_db() as db:
        conversation = db.execute(
            "SELECT 1 FROM conversations c JOIN users u ON u.username=c.username "
            "WHERE c.id=? AND c.username=? AND c.deleted_at IS NULL AND u.disabled_at IS NULL",
            [conv_id, user],
        ).fetchone()
        if not conversation:
            return []
        imported = read_legacy_history(db, conv_id, user)
        saved = [
            dict(row)
            for row in reversed(
                db.execute(
                    "SELECT id,role,content FROM messages WHERE conv_id=? AND id<=? ORDER BY id DESC LIMIT 24",
                    [conv_id, before],
                ).fetchall()
            )
        ]
        # The first takeover anchor may precede the bounded context window.
        # Let SQLite locate it without materializing the entire SQL transcript.
        first_anchor = db.execute(
            "SELECT id,role,content FROM messages WHERE conv_id=? AND id<=? AND role='assistant' "
            "AND CASE WHEN json_valid(content) THEN "
            "json_type(content,'$.history_anchor')='object' AND "
            "(json_type(content,'$.research')='object' OR json_type(content,'$.native_agent')='object') "
            "ELSE 0 END ORDER BY id LIMIT 1",
            [conv_id, before],
        ).fetchone()
        if first_anchor is not None and (not saved or first_anchor["id"] < saved[0]["id"]):
            saved.insert(0, dict(first_anchor))
        merged = _merge_saved_turns(imported["messages"] if imported else [], saved)
        rows = list(reversed(merged[-24:]))
    result, remaining = [], 40000
    # Spend the bounded context budget on the newest turns, then restore their
    # chronological order. Old long answers must not displace the last question.
    for row in rows:
        if row["role"] not in {"user", "assistant"}:
            continue
        text = row["content"]
        text = public_answer_text(text)
        if isinstance(text, str) and text.strip():
            # No private reasoning, tool internals or historical credentials enter the prompt.
            content = redact_source_text(text)[: min(16000, remaining)]
            if content:
                result.append({"role": row["role"], "content": content})
                remaining -= len(content)
                if remaining <= 0:
                    break
    result.reverse()
    while result and result[0]["role"] != "user":
        result.pop(0)
    return result


def _error_message(code: str, limit_reason: str | None = None) -> str:
    if code == "budget_exceeded":
        reason = {
            "wall_time": "本轮研究达到运行时间上限",
            "model_calls": "本轮研究达到模型调用次数上限",
            "tool_calls": "本轮研究达到工具调用次数上限",
            "search_calls": "本轮研究达到外部检索次数上限",
            "context_characters": "本轮资料达到上下文容量上限",
            "output_characters": "本轮回答达到输出长度上限",
        }.get(limit_reason, "本轮研究达到时间或用量上限")
        return reason + "。已有资料与未完成状态已保留，可缩小问题后继续研究。"
    return {
        "provider_auth_error": "模型认证失败，请管理员检查当前模型配置。已保存本轮进度。",
        "provider_rate_limited": "模型服务暂时限流，本轮没有自动重试。已保存进度。",
        "provider_timeout": "模型响应超时，已保存本轮进度。",
        "provider_stream_truncated": "模型连接提前结束，已保存收到的内容，尚不能视为完整回答。",
        "budget_exceeded": "本轮已达到研究时间或用量上限，已保存结果与未完成事项。",
        "output_incomplete": "模型未完成回答，已保存已收到的内容。",
    }.get(code, "本轮研究未完成，已保存进度。请查看记录后决定是否继续研究。")


def _evidence_blocked_message(revision: dict) -> str:
    """User-facing result for a report withheld by the hard evidence gate."""
    gate = revision.get("evidence_gate") or {}
    retained = gate.get("documents_retained") or 0
    error_code = gate.get("error_code") or revision.get("error_code") or "original_required"
    return (
        "证据不足：本轮结论涉及财务或交易数字，但服务端没有取得任何公告原文"
        f"（original_obtained=true），按证据硬约束不生成报告正文。已保留本轮抓取到的 {retained} 份资料；"
        "请先调用 source_original 取得公告 PDF 原文并记录 document_id 与命中页码，再重新撰写报告。"
        f"\n错误码：{error_code}"
    )


TOOL_RESULT_CHARACTERS = 20000
TOOL_RESULT_TURN_CHARACTERS = 30000
_TOOL_RESULT_HEAD = 15000
_TOOL_RESULT_TAIL = 5000
# Each pending tool_result keeps a minimum envelope so a later tool_use in the
# same turn can still carry its truncation marker instead of disappearing.
_TURN_RESULT_RESERVE = 64

# A whole fetched document can dwarf everything else in the prompt. The tools
# already redact and size-check their own output; this bound is applied to the
# serialized tool_result string afterwards so one oversized source cannot use
# up the run's context before the model can write an answer. The per-turn cap
# keeps one batch of parallel reads bounded: the wrap-up threshold is three
# quarters of the context limit, so context-before-turn < 150,000 plus at most
# 30,000 of tool results stays below the 200,000 hard ceiling.
_CONTEXT_WRAP_UP = (
    "上下文即将用尽：请立即基于已经获得的证据写出完整报告，停止检索和读取新资料；"
    "尚未核实的关键点明确标记为缺口，不要编造完整结论。"
)

# Source, date and scope must come from the model: a server-side extraction would
# invent provenance and pollute the claim-quality metric. Both wrap-up variants
# therefore reuse this audit, but the claim-pass variant still has the tool.
_NUMBER_VERIFICATION_RULES = (
    "收尾前必须逐条自检报告中的关键数字：财务指标（营收、净利、扣非、毛利率、增速、"
    "现金流、负债率）、交易与持仓数据（价格、成交额、持仓量、仓单量、会员排名、保证金率）、"
    "产业数据（产量、库存、开工率、份额、产能），以及任何作为结论依据的数字。"
    "每个关键数字都必须对应一条已通过 research_record_claims 登记的 claim，并逐项说明："
    "source（来源链接或公告编号，claim 的 supports 必须指向原文）、"
    "published_at（来源发布日期，不是采集日期）、scope（期间和统计口径）、"
    "verification（核验等级）。核对不到原文的数字只能写 secondhand 或 inferred；"
    "完全没有来源的写 unverified，绝不能为了好看写成 verified_source。"
    "标 verified_source 时必须填写 source_locator{document_id,page}，指向本轮 source_original 取得原文的物理页；"
    "涉及财务数字必须先原文后搜索，搜索摘要只是线索，不能作为原文或 verified_source 来源；"
    "本轮若没有任何 original_obtained 原文文档，服务端会直接拒绝 verified_source 登记并返回 original_required，"
    "应先调用 source_original 或把该数字改标 secondhand；有原文但该页文本不含该数字时仍自动降级为 secondhand。"
)

# A wrap-up call reached after the one claim pass runs with tools disabled, so it
# cannot register new claims. This prompt audits the key numbers against claims
# recorded earlier and keeps unverifiable numbers out of the conclusion.
_WRAP_UP_VERIFICATION_PROMPT = (
    _NUMBER_VERIFICATION_RULES
    + "本轮已经不能再登记 claim：已在 claims 中但缺这四项的数字，在报告中标注未核验并列入缺口；"
    "未登记的数字不得作为结论依据，应删除或列为缺口。"
    "不要为了凑数编造来源、公告编号或发布日期。"
)

# One bounded claim-registration report turn: the model still has the claim tool
# even though the run is concluding, so the reconciliation metric sees the same
# numbers the report will conclude with.
#
# Production run 8b4e43d18a6369f18158267068e9f9ab registered two claims that each
# merged an amount with its growth rate while the report carried four numbers
# (coverage 0.5, gap claim_coverage_low): one claim holds one value, so the prompt
# demands one claim per report number and forbids merging or omitting them.
_CLAIM_PASS_PROMPT = (
    "本轮进入收尾阶段，不再检索或读取新资料。本轮唯一可用工具是 research_record_claims："
    "先登记报告正文的每个数值，再直接写报告正文。不要调用其它工具、不要继续检索。"
    "一个 claim 只填一个 value：金额、百分比/增速、数量、比率各登记一条，不得合并或省略；"
    "每次调用最多登记 6 条 claim，超出的下一轮再报。"
    "证据不足的标 verification=unverified 或 inferred，但仍必须出现在 claims 中。"
    + _NUMBER_VERIFICATION_RULES
    + "已登记的数字才能在正文中作为结论依据；未登记的数字不要写进正文。"
    "不要为了凑数编造来源、公告编号或发布日期。"
)

# One bounded retry when the claim call itself is rejected for argument shape
# (pydantic error or unparseable JSON). Production run 0fc4038c lost its whole
# claim batch to one numeric ``period`` and never got another claim call, while
# run ff39e8ed recovered only because the model happened to retry on a later
# round. The retry is an ordinary model call, so it happens only when the
# report call after it also fits the model-call budget.
_CLAIM_PASS_MAX_ATTEMPTS = 2

_CLAIM_PASS_RETRY_PROMPT = (
    "上一次 research_record_claims 调用因参数结构与校验要求不符被拒绝，错误只给出字段路径和类型；"
    "请按错误路径修正参数后重新登记同一批 claim，不要改动研究结论，也不要编造字段。"
    "文本字段用字符串；supports/entities 必须是列表，单个值可以写成只含一个元素的列表；"
    "quotes 的值必须是字符串；entities 只放字符串或单个数字/布尔值，不要放对象。"
    "如果错误是参数体积超限（arguments_too_large），这次最多只登记 6 条 claim，"
    "不要合并 claim 或删减报告数字，超出的部分下一轮再报。"
)

# A registered claim batch can still fail the ledger's citation checks: a quote
# that is not verbatim, a value or unit absent from the cited excerpt, or a
# number without its period/unit basis. Production run 00d8a852 registered three
# such claims and run a1310d01 five, then had no way to know which fields to fix.
# The tool result now carries structured ``claim_details`` and this prompt spends
# the single bounded retry on them.
_CLAIM_PASS_GAP_RETRY_PROMPT = (
    "上一条 research_record_claims 已登记，但服务端引用校验发现字段缺口"
    "（见工具返回的 claim_details，每条含 claim 序号、字段名、缺口名和短原因，不含原文正文）。"
    "请只针对这些 claim 修正对应字段后重新登记同一批 claim："
    "quote_not_in_passage 表示引文不能在所属段落逐字匹配，请只引用段落原文中的连续句子；"
    "value_not_in_excerpt / unit_not_in_excerpt 表示数值或单位未在支撑段落出现，"
    "请改为段落原文包含的数值/单位，或补充真正包含它们的段落；"
    "numeric_basis_missing 表示有数值但缺少 period 或 unit，请补齐；"
    "quote_empty_ignored 无需处理，服务端已按未提供该条引文处理。"
    "不要改动没有缺口的 claim，不要编造来源或引文。"
)

# Production run e31d0af885b8ce041192e693a52ba27e wrote the report but its one
# claim call registered nothing, so every report number stayed a server-extracted
# shell (verified_claim_ratio 0.0). Finalization registers those numbers itself;
# the upgrade round only asks the model to fill the provenance fields for numbers
# the server already registered. ``claim_key`` locates the existing claim so a
# second registration cannot fork claim identity, and the server still verifies
# the cited page contains the value, was fetched this run and holds the original.
_UPGRADE_PASS_PROMPT = (
    "本轮进入来源补齐阶段，不再检索或读取新资料。报告正文中的数字服务端已为你登记好数字，"
    "你只需逐个补齐来源页：按下方列表的 claim_key 定位已登记 claim，"
    "调用 research_record_claims 为每个数字补上 source_locator{document_id,page} 和 source。"
    "claim_key、value、unit 原样回填（value/unit 是报告正文原文写法，不要换算单位），"
    "一项数字只升级列表中的那一条，不要新建重复 claim。"
    "服务端仍会校验该页原文是否包含这个数值、该文档是否本轮取得且 original_obtained；"
    "本轮没有任何原文文档时不要标 verified_source（会被拒绝），可改标 secondhand 并保留缺口。"
    "无法定位到原文页的数字保留未核验并说明缺口，不要编造 document_id、页码或发布日期。"
    "补齐后直接结束，不要重写报告正文。"
)

# The claim pass needs its own call, the excerpt review and the report call after
# it. The runtime forces a tool-free answer when at most 45 seconds of wall time
# remain, so the pass only runs with more than twice that margin: a budget stop
# degrades to the existing tool-free wrap-up instead of buying another call.
# Report-number claims and original-page provenance are a research-report
# channel: the futures workflow answers from snapshots whose numbers already
# carry their own source contract, so it keeps its bounded call count.
_UPGRADE_PASS_WORKFLOWS = frozenset({"research", "industry", "company"})

_CLAIM_PASS_MIN_REMAINING_SECONDS = 90

# Production evidence (run ec2eff606319ff065c604b59b1ed4817): the tool-free
# report turn hit the 4096-token output ceiling (stop_reason=max_tokens), then
# needed a continuation call the run could no longer afford and died as
# budget_exceeded/model_calls with engine_failed. A wrap-up report therefore
# gets one larger single-shot budget; every other turn keeps the configured
# per-call budget.
_WRAP_UP_OUTPUT_TOKENS = 8192


def _claim_arguments_retryable(error) -> bool:
    """The claim call only failed on argument shape, so one more call can fix it.

    ``arguments_too_large`` is the tool layer's oversized-batch signal: the same
    claims split across fewer per call are valid, so it is retryable like a
    pydantic rejection or unparseable JSON. Production run 8c746a5a lost its
    whole claim batch to exactly this opaque "other" rejection.
    """
    if not isinstance(error, dict):
        return False
    if error.get("kind") in {"pydantic", "arguments_too_large"}:
        return True
    return any(
        isinstance(item, dict) and item.get("type") == "invalid_json" for item in error.get("details") or ()
    )


def _claim_gaps_retryable(result) -> bool:
    """A stored claim call reported retryable citation gaps worth one correction pass.

    The ledger returns ``claim_details`` for every gap it can describe and a
    top-level ``retryable`` flag that is true only for the gaps a model can fix
    from the field paths (quote/value/unit/period basis). A successful claim call
    whose gaps are all non-retryable is never retried: the claims are stored and
    the remaining gaps are evidence limitations, not formatting mistakes.
    """
    if not isinstance(result, dict) or not result.get("ok"):
        return False
    data = result.get("data")
    if not isinstance(data, dict) or data.get("retryable") is not True:
        return False
    return any(isinstance(detail, dict) for detail in data.get("claim_details") or ())


def _release_context(messages: list, target: int) -> int:
    """Shrink the oldest tool bodies until the prompt fits under ``target``.

    A single turn of parallel tools can add more than the ceiling allows. Instead
    of failing the run, drop the bodies of the oldest tool results; the model
    keeps the identifiers, the order and everything recent, and the next call is
    turned into a wrap-up call anyway.
    """
    used = len(json.dumps(messages, ensure_ascii=False))
    if used <= target:
        return used
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if used <= target:
                return used
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            body = block.get("content")
            if not isinstance(body, str) or len(body) <= 400:
                continue
            dropped = len(body) - 400
            block["content"] = (
                body[:400] + f"\n…（较早的工具输出已释放 {dropped} 字符，以给收尾报告腾出上下文）…\n"
            )
            used -= dropped
    return len(json.dumps(messages, ensure_ascii=False))


def _truncate_tool_result(
    text: str,
    *,
    limit: int = TOOL_RESULT_CHARACTERS,
    reason: str = "工具输出过长",
) -> str:
    """Keep the head and tail of an oversized tool result inside one string cap."""
    if len(text) <= limit:
        return text
    if limit <= 0:
        return ""
    marker = f"\n…（{reason}，已截断 {len(text) - limit} 字符）…\n"
    room = limit - len(marker)
    if room <= 0:
        return marker[:limit]
    # The documented split is 15,000 head + 5,000 tail when the cap allows it;
    # the marker itself is charged to the tail so the string never exceeds the
    # cap. Smaller caps (the per-turn budget) keep the same 3:1 head/tail shape.
    head = _TOOL_RESULT_HEAD if room >= _TOOL_RESULT_HEAD else room * 3 // 4
    tail = min(_TOOL_RESULT_TAIL, room - head)
    return text[:head] + marker + (text[-tail:] if tail else "")


class NativeAgent:
    def __init__(self, manager: Manager, *, limits: Limits | None = None):
        self.manager = manager
        self.limits = limits or Limits()
        self.active: dict[str, Active] = {}
        self._slots = asyncio.Semaphore(self.limits.concurrent_runs)

    def start(self, user: str, run: dict, model_cfg: dict, *, parent: Lease) -> None:
        run_id = run["run_id"]
        if run_id in self.active or run["status"] in store.TERMINAL:
            return
        cancel = asyncio.Event()

        async def execute(lease):
            return await self._execute(user, run, model_cfg, lease, cancel)

        handle = self.manager.start("agent", execute, parent=parent, persist=self._persist)
        self.active[run_id] = Active(handle, cancel)

        def complete(_handle):
            if self.active.get(run_id) and self.active[run_id].handle is _handle:
                self.active.pop(run_id, None)

        handle.add_done_callback(complete)

    def cancel(self, user: str, run_id: str) -> dict:
        run = store.request_cancel(user, run_id)
        active = self.active.get(run_id)
        if active:
            active.cancel.set()
        return run

    async def _wait(self, value: Awaitable, user: str, run_id: str, cancel: asyncio.Event, end: float):
        """Cancel and actually await our transport wait, never the background owner."""
        task = asyncio.ensure_future(value)
        try:
            while True:
                if cancel.is_set() or store.is_cancelled(user, run_id):
                    raise RunStopped
                if time.monotonic() >= end:
                    raise BudgetExceeded("wall_time")
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(task), timeout=min(0.2, end - time.monotonic())
                    )
                except TimeoutError:
                    continue
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _execute(self, user, run, model_cfg, lease, cancel):
        route = model_cfg.get("_purpose_route", {})
        limits, wrap_up_output_tokens = _effective_limits(self.limits, model_cfg)
        run_id = run["run_id"]
        outcome = Outcome(user, run_id, lease.id, "failed")
        acquired = claimed = False
        research = None
        full_text, pending = "", ""
        chunks, refs, calls = [], [], []
        review_packet = review_result = review_skip = None
        review_pending = review_attempted = reviewing = False
        review_call_number = None
        review_packets = []
        review_batches = []
        upgrade_pass = False
        upgrade_rows = []
        started = time.monotonic()
        end = started + limits.wall_seconds
        usage = {
            "model_calls": 0,
            "tool_calls": 0,
            "server_search_calls": 0,
            "server_search_usage_complete": True,
            "input_tokens": 0,
            "output_tokens": 0,
            "usage_complete": True,
        }

        def clean(text):
            key = model_cfg.get("api_key", "")
            return redact_source_text(text.replace(key, "[redacted]") if key else text)

        def emit(kind, ui=None, **fields):
            store.append_event(user, run_id, {"type": kind, "status": "running", **fields, "ui": ui or {}})

        def flush(*, final=False):
            nonlocal full_text, pending
            if not pending:
                return
            # Persist before display; whole-token boundaries prevent credential fragments crossing frames.
            cut = (
                len(pending)
                if final
                else max((m.end() for m in re.finditer(r"[\s。，；！？、]", pending[:-256])), default=0)
            )
            if not cut:
                return
            text, pending = clean(pending[:cut]), pending[cut:]
            if text:
                full_text += text
                chunks.append({"t": "text", "c": text})
                emit("text_delta", {"text": text})

        try:
            # A queued cancellation never starts a model call.
            while not acquired:
                if cancel.is_set() or store.is_cancelled(user, run_id):
                    raise RunStopped
                if time.monotonic() >= end:
                    raise BudgetExceeded("wall_time")
                try:
                    await asyncio.wait_for(self._slots.acquire(), 0.2)
                    acquired = True
                except TimeoutError:
                    pass
            claimed = store.claim_run(user, run_id, lease.id)
            if not claimed:
                current = store.get_run(user, run_id)
                if current["status"] == "queued" and current["cancel_requested"]:
                    # Cancellation can win after our queue check but before the
                    # atomic claim. No executor can claim a cancelled queue row.
                    raise RunStopped
                outcome.observer_only = True
                return outcome
            profile = store.get_run(user, run_id).get("profile", {})
            anchor = ensure_dialogue(user, run)
            research = build_research_context(run_id=run_id, engine="prism-native")
            research["ledger_available"] = True
            research["profile"] = profile
            create_research_run(user, run["conv_id"], run["question"], research)
            set_run_status(user, run_id, "discovering")
            emit("research_started", {"research": research, "status": "整理问题与已有研究"})
            chunks.append({"t": "research", "research": research})
            messages = _history(user, run["conv_id"], anchor["db_message_id"])
            messages.append({"role": "user", "content": run["question"]})
            evidence = EvidenceTracker(run_id)
            from .tools import NativeTools

            with native_run_context(user, run["conv_id"], run_id, parent_lease=lease) as context:
                tools = NativeTools(
                    context, lease, evidence, allowed_source_hosts=config.RESEARCH_SOURCE_HOSTS
                )
                search_available = provider.supports_search(model_cfg)
                definitions = tools.definitions()
                finished = False
                token_continuations = 0
                wrap_up = wrap_up_announced = False
                claim_pass_offered = False
                claim_pass_attempts = 0
                claim_retry_pending = False
                claim_retry_mode = None
                review_prepared_after_upgrade = False

                async def review_round(batches, number):
                    """Run every bounded review batch in its own 30-second call.

                    A batch that times out or fails only loses its own claims: the
                    next batch still runs under its own timeout, and the merged
                    record keeps the completed assessments plus the timed-out claim
                    IDs. Finalization can therefore report a claim-level gap
                    instead of one incomplete review for the whole run.
                    """
                    nonlocal review_attempted, review_call_number, review_result, review_packet, review_skip

                    for position, batch in enumerate(batches):
                        if time.monotonic() >= end:
                            if review_attempted:
                                review_batches.append(
                                    {"packet": batch, "status": "failed", "reason": "review_timeout"}
                                )
                                continue
                            review_skip = "run_budget"
                            return False
                        if position == 0 and not model_review.history_current(user, run_id, batch):
                            review_skip = "history_changed"
                            return False
                        purpose_request = None
                        if "_purpose_route" in model_cfg:
                            from ..model_routes import reserve

                            purpose_request = f"research-{run_id}-review-{position}"
                            try:
                                reserve(
                                    "research",
                                    user,
                                    purpose_request,
                                    run["model_id"],
                                    expected_route_version=route["version"],
                                    minimum_available=2 if position == 0 else 1,
                                )
                            except HTTPException:
                                if position == 0:
                                    review_skip = "run_budget"
                                    return False
                                review_batches.append(
                                    {"packet": batch, "status": "failed", "reason": "run_budget"}
                                )
                                continue
                        previous_complete = usage["usage_complete"]
                        previous_search_complete = usage["server_search_usage_complete"]
                        usage["usage_complete"] = False
                        usage["server_search_usage_complete"] = False
                        current_usage = {"input_tokens": 0, "output_tokens": 0}
                        seen_usage = set()
                        current_complete = False
                        current_search_complete = True
                        review_text = ""
                        review_packet = batch
                        if not review_attempted:
                            review_attempted = True
                            review_call_number = number
                        emit(
                            "model_started",
                            {"status": "复核关键判断"},
                            attempt=number,
                            concluding=True,
                            phase="evidence_review",
                            max_tokens=limits.output_tokens_per_call,
                            **({"wrap_up": True} if wrap_up else {}),
                        )
                        usage["model_calls"] += 1
                        review_result = model_review.save(
                            user,
                            run_id,
                            batch,
                            status="running",
                            model_id=run["model_id"],
                            call_number=number,
                            batches=list(review_batches) or None,
                        )
                        completed, review_error = None, None
                        clock = build_research_context()
                        request_messages = [
                            {"role": "user", "content": json.dumps(batch, ensure_ascii=False)}
                        ]
                        if len(json.dumps(request_messages, ensure_ascii=False)) > limits.context_characters:
                            raise BudgetExceeded("context_characters")
                        call_end = min(end, time.monotonic() + model_review.REQUEST_SECONDS)
                        stream = model_stream(
                            request_messages,
                            system=model_review.SYSTEM + f"\n服务器时间：{clock['as_of']}",
                            tools=[],
                            model_cfg=model_cfg,
                            max_tokens=limits.output_tokens_per_call,
                            timeout_seconds=max(0.1, call_end - time.monotonic()),
                        )
                        try:
                            async with contextlib.aclosing(stream):
                                while True:
                                    try:
                                        event = await self._wait(
                                            anext(stream), user, run_id, cancel, call_end
                                        )
                                    except StopAsyncIteration:
                                        break
                                    kind = event.get("type")
                                    if kind == "text_delta":
                                        review_text += event.get("text", "")
                                        if len(review_text) > model_review.MAX_RESPONSE_CHARS:
                                            raise provider.ProviderError("review_output_invalid")
                                        if (
                                            len(full_text) + len(pending) + len(review_text)
                                            > limits.output_characters
                                        ):
                                            raise BudgetExceeded("output_characters")
                                    elif kind == "tool_call":
                                        raise provider.ProviderError("provider_protocol_error")
                                    elif kind == "usage":
                                        for key in current_usage:
                                            value = event.get(key)
                                            if type(value) is int and value >= 0:
                                                current_usage[key] = max(current_usage[key], value)
                                                seen_usage.add(key)
                                    elif kind == "done":
                                        completed = event
                            if completed is None:
                                raise provider.ProviderError("provider_stream_truncated")
                            current_complete = completed.get("usage_complete") is True and seen_usage == set(
                                current_usage
                            )
                        except provider.ProviderError as error:
                            if error.code in {
                                "provider_auth_error",
                                "provider_rate_limited",
                                "provider_configuration_error",
                                "provider_input_error",
                            }:
                                raise
                            review_error = (
                                "invalid_response"
                                if error.code == "review_output_invalid"
                                else "provider_error"
                            )
                        except BudgetExceeded as error:
                            if error.reason != "wall_time" or time.monotonic() >= end:
                                raise
                            review_error = "review_timeout"
                        finally:
                            if purpose_request is not None:
                                from ..model_routes import finish

                                finish(
                                    purpose_request,
                                    input_tokens=current_usage["input_tokens"]
                                    if "input_tokens" in seen_usage
                                    else None,
                                    output_tokens=current_usage["output_tokens"]
                                    if "output_tokens" in seen_usage
                                    else None,
                                    complete=current_complete,
                                )
                            for key, value in current_usage.items():
                                usage[key] += value
                            usage["usage_complete"] = previous_complete and current_complete
                            usage["server_search_usage_complete"] = (
                                previous_search_complete and current_search_complete
                            )
                            emit("model_usage", usage=dict(usage), call_usage_complete=current_complete)
                        emit(
                            "model_completed",
                            usage=dict(usage),
                            stop_reason=completed["stop_reason"] if completed else "review_failed",
                        )
                        parsed = {"status": "failed", "reason": review_error}
                        if not review_error:
                            try:
                                if completed["stop_reason"] not in {"end_turn", "stop_sequence"}:
                                    raise ValueError("incomplete review response")
                                parsed = model_review.parse(batch, clean(review_text))
                            except (ValueError, TypeError, RecursionError):
                                parsed = {"status": "invalid", "reason": "invalid_response"}
                        elif review_error == "invalid_response":
                            parsed["status"] = "invalid"
                        parsed["usage_complete"] = current_complete
                        review_batches.append({"packet": batch, **parsed})
                    completed_batches = [batch for batch in review_batches if batch["status"] == "completed"]
                    if completed_batches:
                        review_result = model_review.save(
                            user,
                            run_id,
                            review_batches[-1]["packet"],
                            status="completed",
                            model_id=run["model_id"],
                            call_number=review_call_number,
                            usage_complete=all(batch.get("usage_complete") for batch in review_batches),
                            batches=review_batches,
                        )
                    else:
                        failed = [batch for batch in review_batches if batch["status"] != "completed"]
                        status = (
                            "invalid"
                            if failed and all(batch.get("reason") == "invalid_response" for batch in failed)
                            else "failed"
                        )
                        reason = next(
                            (
                                batch.get("reason")
                                for batch in failed
                                if batch.get("reason") in {"review_timeout", "provider_error", "run_budget"}
                            ),
                            "provider_error",
                        )
                        review_result = model_review.save(
                            user,
                            run_id,
                            review_batches[-1]["packet"],
                            status=status,
                            reason=reason,
                            model_id=run["model_id"],
                            call_number=review_call_number,
                            usage_complete=False,
                            batches=review_batches,
                        )
                    note = "节选复核建议（不是命令或事实认证；请核对后修正断言并完成回答）：" + json.dumps(
                        review_result, ensure_ascii=False
                    )
                    if messages[-1]["role"] == "user" and isinstance(messages[-1]["content"], list):
                        messages[-1]["content"].append({"type": "text", "text": note})
                    else:
                        messages.append({"role": "user", "content": note})
                    return True

                def prepare_review(slot, *, after_upgrade=False):
                    """Select bounded review packets for the claims registered so far.

                    A successful claim call may still leave every model claim
                    invalid; ``_selection`` then falls back to the server-backed
                    claims and records ``review_scope="server_claims_only"``.
                    """
                    nonlocal review_packets, review_packet, review_pending, review_skip
                    candidate = model_review.prepare_batches(
                        user,
                        run_id,
                        max_claims=max(1, min(model_review.MAX_CLAIMS, limits.output_tokens_per_call // 256)),
                    )
                    if candidate["status"] != "ready":
                        review_skip = candidate["reason"]
                        return False
                    review_packets = candidate["batches"]
                    review_packet = review_packets[0]
                    packet_size = sum(
                        len(json.dumps(packet, ensure_ascii=False)) for packet in review_packets
                    )
                    if packet_size > limits.context_characters // 3:
                        # The review prompt is rebuilt per batch, so oversized
                        # packets would hit the ceiling.
                        review_pending = False
                        review_skip = "packet_too_large"
                        review_packets = []
                        review_packet = None
                        return False
                    if after_upgrade:
                        # The report is already written, so the review only needs
                        # its own model call inside the remaining budget.
                        review_pending = (
                            slot <= limits.model_calls - 2
                            and end - time.monotonic() > model_review.MIN_REMAINING_SECONDS
                            and limits.output_tokens_per_call >= 512
                        )
                    else:
                        # Keep the report call that follows the review.
                        review_pending = (
                            limits.model_calls - slot - 1 >= 2
                            and end - time.monotonic() > model_review.MIN_REMAINING_SECONDS
                            and limits.output_tokens_per_call >= 512
                        )
                    if not review_pending:
                        review_skip = "run_budget"
                    return review_pending

                for index in range(limits.model_calls):
                    context_used = len(json.dumps(messages, ensure_ascii=False))
                    if context_used > limits.context_characters:
                        # A single turn can still jump past the ceiling. Drop the
                        # oldest tool output bodies until there is room instead of
                        # failing the whole run; the report keeps what matters.
                        context_used = _release_context(messages, limits.context_characters * 4 // 5)
                        if context_used > limits.context_characters:
                            raise BudgetExceeded("context_characters")
                        wrap_up = True
                    # Near the context ceiling, stop collecting evidence and make
                    # the next call write the report instead of failing the run.
                    # This is a soft threshold: 3/4 of the hard context budget.
                    # The same wrap-up starts when only three model calls remain,
                    # so the bounded claim pass, the excerpt review (when the run
                    # registered claims) and the report each own one call. A
                    # budget-only wrap-up on the last permitted call could never
                    # offer the claim tool and left a truncated report to die on
                    # model_calls; two remaining calls forced the review to be
                    # skipped as run_budget (production run e671f546). The first
                    # call never wraps up: no evidence yet.
                    wrap_up = context_used > limits.context_characters * 3 // 4 or (
                        index > 0 and limits.model_calls - index <= 3
                    )
                    if wrap_up and not wrap_up_announced:
                        wrap_up_announced = True
                        note = {"type": "text", "text": _CONTEXT_WRAP_UP}
                        if messages[-1]["role"] == "user" and isinstance(messages[-1]["content"], list):
                            messages[-1]["content"].append(note)
                        else:
                            # The Messages API only accepts string or block-list
                            # content. Production run e09527c7 sent a bare dict here
                            # on the first wrap-up call after a max_tokens
                            # continuation and the whole request died with HTTP 422.
                            messages.append({"role": "user", "content": [note]})
                    if cancel.is_set() or store.is_cancelled(user, run_id):
                        raise RunStopped
                    if time.monotonic() >= end:
                        raise BudgetExceeded("wall_time")
                    reviewing, review_pending = review_pending, False
                    # Reserve the last permitted call for an answer. This is
                    # still the same bounded run, never an extra retry/budget.
                    concluding = wrap_up or (
                        not reviewing
                        and (
                            index == limits.model_calls - 1
                            or usage["tool_calls"] >= max(1, limits.tool_calls - 2)
                            or (index > 0 and end - time.monotonic() <= 45)
                        )
                    )
                    concluding_fallback = False
                    upgrade_round = upgrade_pass
                    upgrade_pass = False
                    if reviewing:
                        # All review batches run inside this one loop slot. Every
                        # batch keeps its own 30-second call, so one truncated
                        # batch cannot fail the other batches or the answer.
                        if await review_round(review_packets, index + 1):
                            continue
                        reviewing, concluding, concluding_fallback = False, True, True
                    purpose_request = None
                    if "_purpose_route" in model_cfg:
                        from ..model_routes import reserve

                        purpose_request = f"research-{run_id}-{index}"
                        reserve(
                            "research",
                            user,
                            purpose_request,
                            run["model_id"],
                            expected_route_version=route["version"],
                        )
                    # One bounded claim-registration pass: the model may call the
                    # claim tool once before the answer-only wrap-up, so the report
                    # numbers exist in research_claims when reconciliation runs.
                    # Only offer it when the claim call and the review/report calls
                    # after it fit the model-call, tool-call and wall budgets; a
                    # budget or history fallback, the last permitted call and a
                    # nearly exhausted wall clock keep the existing tool-free
                    # wrap-up and degrade nothing beyond the current behaviour.
                    claim_pass = (
                        concluding
                        and not concluding_fallback
                        and not reviewing
                        and (not claim_pass_offered or claim_retry_pending)
                        and not upgrade_round
                        # Keep the last permitted call for the report itself.
                        and index <= limits.model_calls - 2
                        and usage["tool_calls"] < limits.tool_calls
                        and end - time.monotonic() > _CLAIM_PASS_MIN_REMAINING_SECONDS
                    )
                    if claim_pass:
                        claim_pass_offered = True
                        claim_retry_pending = False
                        claim_pass_attempts += 1
                    max_tokens = wrap_up_output_tokens if wrap_up else limits.output_tokens_per_call
                    emit(
                        "model_started",
                        {
                            "status": "补齐来源页"
                            if upgrade_round
                            else "整理研究结论"
                            if concluding
                            else "核对资料与组织分析"
                        },
                        attempt=index + 1,
                        concluding=concluding or upgrade_round,
                        phase="claim_upgrade" if upgrade_round else "research",
                        max_tokens=max_tokens,
                        **({"wrap_up": True} if wrap_up else {}),
                        **({"claim_pass_attempt": claim_pass_attempts} if claim_pass else {}),
                    )
                    usage["model_calls"] += 1
                    previous_complete = usage["usage_complete"]
                    previous_search_complete = usage["server_search_usage_complete"]
                    usage["usage_complete"] = False
                    usage["server_search_usage_complete"] = False
                    current_usage = {"input_tokens": 0, "output_tokens": 0}
                    seen_usage = set()
                    current_complete = False
                    current_search_complete = False
                    search_allowance = min(2, limits.server_search_calls - usage["server_search_calls"])
                    call_search_available = (
                        search_available and search_allowance > 0 and not concluding and not upgrade_round
                    )
                    request_definitions = [] if concluding or upgrade_round else list(definitions)
                    if claim_pass or upgrade_round:
                        # Exactly the one tool the prompt names, nothing else: no
                        # search, no reads, no check tools during these rounds.
                        request_definitions = [
                            spec for spec in definitions if spec.get("name") == "research_record_claims"
                        ]
                    if call_search_available:
                        request_definitions.append(
                            {
                                "type": "web_search_20250305",
                                "name": "web_search",
                                "max_uses": search_allowance,
                            }
                        )
                    observed_searches = 0
                    requested, completed = [], None
                    clock = build_research_context()
                    system = system_prompt(research, run["workflow"], search_available=call_search_available)
                    system += profile_prompt(profile)
                    system += f"\n当前服务器时间：{clock['as_of']}；‘今天’以此为准，研究启动观察时点仍保留。"
                    if upgrade_round:
                        system += (
                            "\n待补齐来源的数字（服务端已登记，按 claim_key 定位，不要新建重复）：\n"
                            + json.dumps(upgrade_rows, ensure_ascii=False)
                            + "\n"
                            + _UPGRADE_PASS_PROMPT
                        )
                    elif concluding:
                        if claim_pass:
                            system += "\n" + _CLAIM_PASS_PROMPT
                            if claim_pass_attempts > 1:
                                if claim_retry_mode == "gaps":
                                    system += "\n" + _CLAIM_PASS_GAP_RETRY_PROMPT
                                else:
                                    system += "\n" + _CLAIM_PASS_RETRY_PROMPT
                        else:
                            system += (
                                "\n本轮进入收尾阶段，不再调用工具或继续搜索。基于已经取得的资料，"
                                "直接回答用户问题，区分证据、阶段性判断和未知；列出尚未查证的关键缺口。"
                                "资料不足时明确说明，不编造完整结论，不再描述下一步准备做什么。"
                            )
                    if wrap_up and not upgrade_round:
                        # The prompt above is shared with the call/time budget
                        # wrap-up; this sentence explains the context ceiling too.
                        system += "\n" + _CONTEXT_WRAP_UP
                        if not claim_pass:
                            system += "\n" + _WRAP_UP_VERIFICATION_PROMPT
                    request_messages = messages
                    call_end = end
                    stream = model_stream(
                        request_messages,
                        system=system,
                        tools=request_definitions,
                        model_cfg=model_cfg,
                        max_tokens=max_tokens,
                        timeout_seconds=max(0.1, call_end - time.monotonic()),
                    )
                    try:
                        async with contextlib.aclosing(stream):
                            while True:
                                try:
                                    event = await self._wait(anext(stream), user, run_id, cancel, call_end)
                                except StopAsyncIteration:
                                    break
                                kind = event.get("type")
                                if kind == "text_delta":
                                    if upgrade_round:
                                        # The report is already written; text from
                                        # the source-upgrade call must never leak
                                        # into the frozen report body.
                                        continue
                                    pending += event.get("text", "")
                                    if len(full_text) + len(pending) > limits.output_characters:
                                        raise BudgetExceeded("output_characters")
                                    flush()
                                elif kind == "tool_call":
                                    requested.append(event)
                                elif kind == "server_tool_use":
                                    if event["block"].get("name") != "web_search":
                                        raise provider.ProviderError("provider_protocol_error")
                                    usage["server_search_calls"] += 1
                                    observed_searches += 1
                                    if (
                                        not call_search_available
                                        or observed_searches > search_allowance
                                        or usage["server_search_calls"] > limits.server_search_calls
                                    ):
                                        raise BudgetExceeded("search_calls")
                                    evidence.record_server_call(event["block"])
                                    emit("source_search_started", {"status": "检索外部资料"})
                                elif kind == "server_tool_result":
                                    normalized = evidence.record_server_result(
                                        event["block"], retrieved_at=build_research_context()["as_of"]
                                    )
                                    if normalized:
                                        call, result, found = normalized
                                        found = ingest_tool_result(user, run_id, call, result, found)
                                        for ref in found:
                                            refs.append(ref)
                                            chunks.append({"t": "ref", **ref})
                                            emit("source_recorded", {"ref": ref})
                                elif kind == "usage":
                                    for key in current_usage:
                                        value = event.get(key)
                                        if type(value) is int and value >= 0:
                                            current_usage[key] = max(current_usage[key], value)
                                            seen_usage.add(key)
                                elif kind == "done":
                                    completed = event
                        flush(final=True)
                        if completed is None:
                            raise provider.ProviderError("provider_stream_truncated")
                        current_complete = completed.get("usage_complete") is True and seen_usage == set(
                            current_usage
                        )
                        current_search_complete = True
                    except provider.ProviderError:
                        raise
                    except BudgetExceeded:
                        raise
                    finally:
                        if purpose_request is not None:
                            from ..model_routes import finish

                            finish(
                                purpose_request,
                                input_tokens=current_usage["input_tokens"]
                                if "input_tokens" in seen_usage
                                else None,
                                output_tokens=current_usage["output_tokens"]
                                if "output_tokens" in seen_usage
                                else None,
                                complete=current_complete,
                            )
                        for key, value in current_usage.items():
                            usage[key] += value
                        usage["usage_complete"] = previous_complete and current_complete
                        usage["server_search_usage_complete"] = (
                            previous_search_complete and current_search_complete
                        )
                        # Preserve observed counters even when the stream failed.
                        # A missing counter remains unknown, never certified zero.
                        emit("model_usage", usage=dict(usage), call_usage_complete=current_complete)
                    emit(
                        "model_completed",
                        usage=dict(usage),
                        stop_reason=completed["stop_reason"] if completed else "review_failed",
                    )
                    content = completed["content"]
                    continuation = content.continuation() if hasattr(content, "continuation") else content
                    messages.append({"role": "assistant", "content": continuation})
                    if completed["stop_reason"] == "tool_use":
                        if upgrade_round and (
                            not requested
                            or any(call.get("name") != "research_record_claims" for call in requested)
                        ):
                            # The upgrade round is a bonus after the finished
                            # report: an unexpected call ends it harmlessly.
                            finished = True
                            break
                        if not upgrade_round and concluding and not claim_pass:
                            if index == limits.model_calls - 1 and (full_text + pending).strip():
                                # This was the report's only remaining call and
                                # the model answered with text plus a stray tool
                                # call. The report text already streamed; keep it
                                # (production run 4f4165a0 lost a 2272-character
                                # report and ended budget_exceeded here) and drop
                                # the call instead of failing the whole run.
                                flush(final=True)
                                append_run_gap(user, run_id, "report_tool_use_salvaged")
                                finished = True
                                break
                            raise BudgetExceeded(
                                "model_calls" if index == limits.model_calls - 1 else "tool_calls"
                            )
                        if not requested:
                            raise provider.ProviderError("provider_protocol_error")
                        if claim_pass and any(
                            call.get("name") != "research_record_claims" for call in requested
                        ):
                            # The claim pass offers exactly one tool. A different
                            # (or hallucinated) tool is not another evidence round.
                            raise BudgetExceeded("tool_calls")
                        results = []
                        outputs = []
                        claim_rejected = False
                        turn_characters = TOOL_RESULT_TURN_CHARACTERS
                        results_left = len(requested)
                        for call in requested:
                            usage["tool_calls"] += 1
                            if usage["tool_calls"] > limits.tool_calls:
                                raise BudgetExceeded("tool_calls")
                            name, arguments, call_id = call.get("name"), call.get("arguments"), call.get("id")
                            if not isinstance(call_id, str) or not re.fullmatch(
                                r"[A-Za-z0-9_-]{1,128}", call_id
                            ):
                                raise provider.ProviderError("provider_protocol_error")
                            emit("tool_started", {"tool": name, "n": usage["tool_calls"]}, tool=name)
                            result = await self._wait(
                                tools.execute(name, arguments, call_id), user, run_id, cancel, end
                            )
                            outputs.append(result)
                            if not result.get("ok"):
                                # Failure details are field paths and error types
                                # only; tool arguments may carry research content.
                                error = result.get("error") or {}
                                failure = {
                                    "tool": name,
                                    "error_code": error.get("code"),
                                    "error_kind": error.get("kind", "other"),
                                }
                                paths = [
                                    f"{item.get('loc')}:{item.get('type')}"
                                    for item in error.get("details") or ()
                                    if isinstance(item, dict)
                                ]
                                if paths:
                                    failure["error_paths"] = paths
                                emit("tool_failed", failure, tool=name)
                                if claim_pass and _claim_arguments_retryable(error):
                                    claim_rejected = True
                            if result.get("call"):
                                calls.append(result["call"])
                                chunks.append({"t": "platform_call", "call": result["call"]})
                                emit("tool_completed", {"platform_call": result["call"]}, ok=result["ok"])
                            for ref in result.get("refs", []):
                                refs.append(ref)
                                chunks.append({"t": "ref", **ref})
                                emit("source_recorded", {"ref": ref})
                            # First come first served: earlier results keep their
                            # text, later ones are bounded by what is left of this
                            # turn's budget so one batch of reads cannot leap over
                            # the hard context ceiling before the next checks.
                            allowance = max(0, turn_characters - (results_left - 1) * _TURN_RESULT_RESERVE)
                            limit = min(TOOL_RESULT_CHARACTERS, allowance)
                            tool_content = _truncate_tool_result(
                                json.dumps(result, ensure_ascii=False),
                                limit=limit,
                                reason=(
                                    "本轮工具输出总量超限"
                                    if limit < TOOL_RESULT_CHARACTERS
                                    else "工具输出过长"
                                ),
                            )
                            turn_characters -= len(tool_content)
                            results_left -= 1
                            results.append(
                                {
                                    "type": "tool_result",
                                    "tool_use_id": call_id,
                                    "content": tool_content,
                                    "is_error": not result["ok"],
                                }
                            )
                        messages.append({"role": "user", "content": results})
                        registered_here = any(
                            call.get("name") == "research_record_claims" and not result.get("is_error")
                            for call, result in zip(requested, results, strict=True)
                        )
                        if claim_pass:
                            # Two kinds of bounded correction share one retry slot:
                            # a rejected call (pydantic/JSON shape) and a stored
                            # call whose claims failed the ledger's citation checks
                            # (structured claim_details). A registration that
                            # already succeeded and reported no retryable gap is
                            # never retried: the claims are stored.
                            gap_retry = (
                                not claim_rejected
                                and not any(item["is_error"] for item in results)
                                and any(_claim_gaps_retryable(output) for output in outputs)
                                and claim_pass_attempts < _CLAIM_PASS_MAX_ATTEMPTS
                            )
                            retry_wanted = gap_retry or (
                                claim_rejected
                                and not any(not item["is_error"] for item in results)
                                and claim_pass_attempts < _CLAIM_PASS_MAX_ATTEMPTS
                            )
                            if retry_wanted:
                                claim_retry_mode = "gaps" if gap_retry else "arguments"
                            # The retry call and the report call after it must both
                            # fit the model-call budget. The report always wins: a
                            # retry that would take the report's only remaining
                            # slot is skipped and recorded as an advisory gap, so a
                            # run whose claim batch was rejected still delivers its
                            # report instead of dying as budget_exceeded.
                            claim_retry_pending = retry_wanted and index <= limits.model_calls - 3
                            if retry_wanted and not claim_retry_pending:
                                append_run_gap(user, run_id, "claim_retry_skipped_for_report")
                        if upgrade_round:
                            # The source-upgrade round is terminal for the report
                            # itself, but a successful claim call can leave
                            # server-backed claims that the earlier review could
                            # not see. Run that review now, once, inside the
                            # remaining model-call budget.
                            if registered_here and not review_attempted and not review_prepared_after_upgrade:
                                review_prepared_after_upgrade = True
                                if prepare_review(index, after_upgrade=True):
                                    await review_round(review_packets, index + 1)
                            finished = True
                            break
                        if not review_attempted and registered_here:
                            prepare_review(index)
                    elif completed["stop_reason"] in {"end_turn", "stop_sequence", "refusal"}:
                        rows = []
                        if (
                            not upgrade_round
                            and run["workflow"] in _UPGRADE_PASS_WORKFLOWS
                            and full_text.strip()
                            and index < limits.model_calls - 1
                            and usage["tool_calls"] < limits.tool_calls
                            and end - time.monotonic() > model_review.MIN_REMAINING_SECONDS
                            and not cancel.is_set()
                        ):
                            # The report is written; one bounded extra call may add
                            # source pages to the numbers the server registered for
                            # it. The report stays as written either way.
                            rows = upgrade_candidates(user, run_id, full_text)["claims"]
                        if rows:
                            upgrade_rows = rows
                            upgrade_pass = True
                            continue
                        finished = True
                        break
                    elif completed["stop_reason"] == "pause_turn":
                        continue  # A successful RAM continuation, never a failed-request retry.
                    elif completed["stop_reason"] == "max_tokens":
                        # A truncated response is not a failed request. Continue the
                        # same bounded run with the model's own partial output; this
                        # never adds budget beyond model_calls/output_characters.
                        if token_continuations >= limits.token_continuations:
                            outcome.error_code = "output_incomplete"
                            break
                        token_continuations += 1
                        emit("model_continuation", {"attempt": token_continuations}, status="running")
                        continue
                    else:
                        outcome.error_code = "output_incomplete"
                        break
                if not finished and not outcome.error_code:
                    raise BudgetExceeded("model_calls")
                if finished and not full_text.strip():
                    finished, outcome.error_code = False, "output_incomplete"
                outcome.status = "completed" if finished else "partial" if full_text else "failed"
        except RunStopped:
            outcome.status, outcome.error_code = "cancelled", "cancelled_by_user"
        except BudgetExceeded as error:
            progressed = full_text or pending or usage["server_search_calls"] or usage["tool_calls"] or refs
            outcome.status, outcome.error_code = (
                "partial" if progressed else "failed",
                "budget_exceeded",
            )
            outcome.limit_reason = error.reason
        except provider.ProviderError as error:
            outcome.status, outcome.error_code = "partial" if full_text or pending else "failed", error.code
            outcome.provider_diagnostic = error.diagnostic or None
        except HTTPException as error:
            if error.status_code in {401, 404}:
                outcome.abandoned = True
                return outcome
            outcome.status, outcome.error_code = (
                "partial" if full_text or pending else "failed",
                "research_unavailable",
            )
        except asyncio.CancelledError:
            outcome.status, outcome.error_code = "cancelled", "execution_interrupted_unknown"
        except Exception as error:
            log.error("native_execution_failed error_type=%s", type(error).__name__)
            outcome.status, outcome.error_code = (
                "partial" if full_text or pending else "failed",
                "research_unavailable",
            )
        finally:
            if acquired:
                self._slots.release()
        try:
            flush(final=True)
            if claimed and research is not None and review_result and review_result["status"] == "running":
                review_result = model_review.save(
                    user,
                    run_id,
                    review_packet,
                    status="cancelled" if outcome.status == "cancelled" else "failed",
                    reason="provider_error"
                    if outcome.error_code and outcome.error_code.startswith("provider_")
                    else "interrupted",
                    model_id=run["model_id"],
                    call_number=review_call_number,
                    usage_complete=False,
                    batches=list(review_batches) or None,
                )
            elif claimed and research is not None and not review_attempted and review_skip:
                review_result = model_review.save(
                    user,
                    run_id,
                    review_packet,
                    status="not_performed",
                    reason="interrupted" if outcome.status == "cancelled" else review_skip,
                    model_id=run["model_id"],
                )
            if outcome.error_code and outcome.status != "cancelled":
                emit(
                    "run_error",
                    {"error": _error_message(outcome.error_code, outcome.limit_reason)},
                    error_code=outcome.error_code,
                    limit_reason=outcome.limit_reason,
                    # Name the exhausted budget (context_characters, model_calls,
                    # wall_time, ...) so a bounded stop is diagnosable without
                    # reading the provider calls or the user-facing message.
                    **({"budget": outcome.limit_reason} if outcome.limit_reason else {}),
                    **(
                        {"provider_diagnostic": outcome.provider_diagnostic}
                        if outcome.provider_diagnostic
                        else {}
                    ),
                )
            elif outcome.status == "cancelled":
                emit("cancellation_acknowledged", {"status": "已停止模型等待，正在保存进度"})
            outcome.text = full_text
            if claimed and research is not None:
                revision = finalize_research_run(
                    user,
                    run_id,
                    full_text,
                    error=outcome.status in {"failed", "partial"},
                    cancelled=outcome.status == "cancelled",
                )
                reconciliation = revision.get("claim_reconciliation") or {}
                research.update(
                    status=revision["acceptance_status"],
                    revision_id=revision["revision_id"],
                    gaps=revision["gaps"],
                    claim_reconciliation={
                        "status": reconciliation.get("status", "unavailable"),
                        "verified_claim_ratio": reconciliation.get("verified_claim_ratio"),
                        "coverage": reconciliation.get("coverage"),
                        "unsupported_claims": reconciliation.get("unsupported_claims"),
                        "claims_checked": reconciliation.get("claims_checked"),
                        "report_numbers_checked": reconciliation.get("report_numbers_checked"),
                    },
                )
                outcome.research_revision = revision["revision_id"]
                report_text = full_text
                report_chunks = chunks
                if revision["acceptance_status"] == "blocked":
                    # The hard evidence gate withheld the report body. Deliver only
                    # the evidence-insufficient result: the model's draft must not
                    # survive in outcome.text or in the streamed text chunks.
                    report_text = _evidence_blocked_message(revision)
                    report_chunks = [{"t": "text", "c": report_text}]
                    outcome.text = report_text
                    outcome.error_code = revision.get("error_code") or "original_required"
                    research["error_code"] = outcome.error_code
                    research["evidence_gate"] = revision.get("evidence_gate")
                anchor = ensure_dialogue(user, run)
                payload = json.dumps(
                    source_display_payload(
                        {
                            "text": report_text,
                            "chunks": report_chunks,
                            "refs": refs,
                            "research": research,
                            "native_agent": {
                                "run_id": run_id,
                                "engine": "prism-native",
                                "status": outcome.status,
                                "limit_reason": outcome.limit_reason,
                            },
                            "history_anchor": anchor,
                            "report_status": revision["acceptance_status"],
                            "report_files": [],
                        }
                    ),
                    ensure_ascii=False,
                )
                outcome.message_id = _save_assistant_report(
                    user,
                    run,
                    payload,
                    {
                        "source": "native-agent",
                        "source_id": run_id,
                        "conv_id": run["conv_id"],
                        "title": run["question"][:100],
                        "summary": report_text[:240],
                        "report": {
                            "question": run["question"],
                            "final_decision": report_text,
                            "status": revision["acceptance_status"],
                            "run_id": run_id,
                            "model": run["model_id"],
                            "research": research,
                            "references": refs,
                            "research_revision_id": revision["revision_id"],
                        },
                    },
                )
                outcome.message_payload = payload
                emit(
                    "research_finalized",
                    {
                        "research": research,
                        "report_status": revision["acceptance_status"],
                        "report_files": [],
                        "elapsed": round(time.monotonic() - started, 1),
                    },
                    usage=usage,
                )
            return outcome
        except HTTPException as error:
            if error.status_code in {401, 404}:
                outcome.abandoned = True
                return outcome
            raise
        except Exception as error:
            # Failed persistence is not a terminal success. Keep the manager's
            # blocked lease and the reader's result_unconfirmed notice; record
            # only the phase/class, never exception text containing user input.
            log.error("native_finalization_unconfirmed error_type=%s", type(error).__name__)
            raise

    def _persist(self, outcome: Outcome):
        if outcome.abandoned:
            _retire_removed_owner(outcome)
            return
        if outcome.observer_only:
            # This task failed the atomic claim and never invoked the model.
            return
        try:
            store.get_run(outcome.user, outcome.run_id)
            if outcome.research_revision:
                with get_db() as db:
                    row = db.execute(
                        "SELECT revision_id FROM research_revisions WHERE run_id=?", [outcome.run_id]
                    ).fetchone()
                    msg = db.execute(
                        "SELECT content FROM messages WHERE id=?", [outcome.message_id]
                    ).fetchone()
                if (
                    not row
                    or row[0] != outcome.research_revision
                    or not msg
                    or msg[0] != outcome.message_payload
                ):
                    store.get_run(outcome.user, outcome.run_id)
                    raise RuntimeError("native_terminal_persistence_unconfirmed")
            terminal = store.finish_run(
                outcome.user,
                outcome.run_id,
                outcome.status,
                outcome.text,
                outcome.error_code,
                # Only a bounded stop has a budget to name; the plain terminal
                # paths keep the historical call shape.
                **({"limit_reason": outcome.limit_reason} if outcome.limit_reason else {}),
            )
            if terminal["status"] != outcome.status:
                raise RuntimeError("native_terminal_persistence_unconfirmed")
            try:
                from ..diagnostics import record_native_outcome

                record_native_outcome(outcome.user, outcome.status, outcome.error_code, run_id=outcome.run_id)
            except Exception:
                # The persisted release-bound delivery remains pending. A replay
                # or the drain-aware scheduler can project it without reexecution.
                log.warning("native_outcome_diagnostic_unavailable")

        except HTTPException as error:
            if error.status_code not in {401, 404}:
                raise
            outcome.abandoned = True
            _retire_removed_owner(outcome)
        except Exception as error:
            log.error("native_terminal_persistence_unconfirmed error_type=%s", type(error).__name__)
            raise


def _retire_removed_owner(outcome: Outcome):
    """A deleted/disabled owner stops work without recreating deleted content."""
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute(
                "SELECT * FROM native_agent_runs WHERE run_id=? AND username=?",
                [outcome.run_id, outcome.user],
            ).fetchone()
            if row is not None:
                active_owner = db.execute(
                    "SELECT 1 FROM users u JOIN conversations c ON c.username=u.username WHERE u.username=? "
                    "AND c.id=? AND u.disabled_at IS NULL AND c.deleted_at IS NULL",
                    [outcome.user, row["conv_id"]],
                ).fetchone()
                if active_owner or row["claimed_by"] not in {None, outcome.executor_id}:
                    raise RuntimeError("native_owner_retirement_unconfirmed")
                if row["status"] not in store.TERMINAL:
                    now = time.time()
                    db.execute(
                        "UPDATE native_agent_runs SET status='cancelled',finished_at=?,error_code='owner_unavailable',"
                        "updated_at=? WHERE run_id=?",
                        [now, now, outcome.run_id],
                    )
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise


_SERVICES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def get_service(manager: Manager) -> NativeAgent:
    if manager not in _SERVICES:
        _SERVICES[manager] = NativeAgent(manager)
    return _SERVICES[manager]
