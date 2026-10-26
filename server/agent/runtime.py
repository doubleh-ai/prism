"""Prism's owned model/tool loop, durable delivery and cooperative cancellation.

HTTP readers only replay persisted events. They never own or restart execution.
No subprocess, filesystem skills, MCP client or third-party agent SDK is used.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import stat
import time
import weakref
from collections.abc import Awaitable
from dataclasses import dataclass

from fastapi import HTTPException

from .. import config
from ..capabilities import native_run_context
from ..database import get_db
from ..research import create_research_run, finalize_research_run, ingest_tool_result, set_run_status
from ..research_context import EvidenceTracker, build_research_context
from ..source_urls import redact_source_text, source_display_payload
from ..work_lifecycle import Lease, Manager, WorkHandle
from . import provider, store
from .prompts import system_prompt
from .public_text import public_answer_text

model_stream = provider.stream


@dataclass(frozen=True)
class Limits:
    model_calls: int = 6
    tool_calls: int = 16
    server_search_calls: int = 4
    output_tokens_per_call: int = 2048
    wall_seconds: float = 180
    output_characters: int = 60000
    context_characters: int = 100000
    concurrent_runs: int = 4


class RunStopped(Exception):
    pass


class BudgetExceeded(Exception):
    pass


@dataclass
class Outcome:
    user: str
    run_id: str
    executor_id: str
    status: str
    text: str = ""
    error_code: str | None = None
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
    # All native tables and the old-version-compatible delete trigger are one
    # additive schema transaction, including on a first interrupted startup.
    store.init_agent_db()


def _legacy_prefix_count(session_id, project_dir):
    """Count UI-visible old user turns; never return history to the model.

    Only a bounded existing JSONL file below the configured UI-history root is
    read, through no-follow directory descriptors. Unknown is never invented
    as zero. This discovers no skills, instructions, keys or arbitrary paths.
    """
    if not session_id and not project_dir:
        return 0
    if (
        not isinstance(session_id, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id) is None
        or not isinstance(project_dir, str)
        or project_dir in {".", ".."}
        or re.fullmatch(r"[A-Za-z0-9_.-]{1,240}", project_dir) is None
    ):
        return None
    from ..conversations import CLAUDE_PROJECTS
    from ..research_context import parse_claude_jsonl_events

    descriptors = []
    try:
        root = os.open(CLAUDE_PROJECTS, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        descriptors.append(root)
        project = os.open(
            project_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root
        )
        descriptors.append(project)
        name = session_id + ".jsonl"
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=project)
        descriptors.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 16 * 1024 * 1024:
            return None
        chunks, remaining = [], before.st_size + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        after = os.fstat(fd)
        named = os.stat(name, dir_fd=project, follow_symlinks=False)
        if len(raw) != before.st_size or any(
            getattr(before, key) != getattr(after, key) or getattr(before, key) != getattr(named, key)
            for key in fields
        ):
            return None
        # Match the existing UI parser's handling of malformed JSON lines.
        lines = raw.decode("utf-8").splitlines()
        if len(lines) > 20000 or any(len(line) > 512 * 1024 for line in lines):
            return None
        events = []
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                return None
            events.append(event)
        messages = parse_claude_jsonl_events(events, session_id=session_id)
        return sum(message.get("role") == "user" for message in messages)
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        return None
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


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
            existing = db.execute(
                "SELECT history_anchor_json FROM native_agent_dialogues WHERE run_id=? AND username=?",
                [run["run_id"], user],
            ).fetchone()
            if existing:
                anchor = json.loads(existing[0])
            else:
                legacy_count = _legacy_prefix_count(conversation["session_id"], conversation["project_dir"])
                if legacy_count is None:
                    raise HTTPException(409, "旧对话历史暂无法核对；请新建对话继续研究。原历史保持不变。")
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
    # The new engine reads owned platform records, never ~/.claude or skill directories.
    with get_db() as db:
        rows = db.execute(
            "SELECT m.role,m.content FROM messages m JOIN conversations c ON c.id=m.conv_id "
            "WHERE c.id=? AND c.username=? AND c.deleted_at IS NULL AND m.id<=? "
            "ORDER BY m.id DESC LIMIT 24",
            [conv_id, user, before],
        ).fetchall()
    result, remaining = [], 40000
    # Spend the bounded context budget on the newest turns, then restore their
    # chronological order. Old long answers must not displace the last question.
    for row in rows:
        if row["role"] not in {"user", "assistant"}:
            continue
        text = row["content"]
        if row["role"] == "assistant":
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


def _error_message(code: str) -> str:
    return {
        "provider_auth_error": "模型认证失败，请管理员检查当前模型配置。已保存本轮进度。",
        "provider_rate_limited": "模型服务暂时限流，本轮没有自动重试。已保存进度。",
        "provider_timeout": "模型响应超时，已保存本轮进度。",
        "provider_stream_truncated": "模型连接提前结束，已保存收到的内容，尚不能视为完整回答。",
        "budget_exceeded": "本轮已达到研究时间或用量上限，已保存结果与未完成事项。",
        "output_incomplete": "模型未完成回答，已保存已收到的内容。",
    }.get(code, "本轮研究未完成，已保存进度。请查看记录后决定是否继续研究。")


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
                    raise BudgetExceeded
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
        run_id = run["run_id"]
        outcome = Outcome(user, run_id, lease.id, "failed")
        acquired = claimed = False
        research = None
        full_text, pending = "", ""
        chunks, refs, calls = [], [], []
        started = time.monotonic()
        end = started + self.limits.wall_seconds
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
                    raise BudgetExceeded
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
            anchor = ensure_dialogue(user, run)
            research = build_research_context(run_id=run_id, engine="prism-native")
            research["ledger_available"] = True
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
                search_available = model_cfg["base_url"].rstrip("/") in {
                    "https://api.deepseek.com/anthropic",
                    "https://api.deepseek.com/anthropic/v1",
                }
                definitions = tools.definitions()
                finished = False
                for index in range(self.limits.model_calls):
                    if len(json.dumps(messages, ensure_ascii=False)) > self.limits.context_characters:
                        raise BudgetExceeded
                    if cancel.is_set() or store.is_cancelled(user, run_id):
                        raise RunStopped
                    if time.monotonic() >= end:
                        raise BudgetExceeded
                    emit("model_started", {"status": "核对资料与组织分析"}, attempt=index + 1)
                    usage["model_calls"] += 1
                    previous_complete = usage["usage_complete"]
                    previous_search_complete = usage["server_search_usage_complete"]
                    usage["usage_complete"] = False
                    usage["server_search_usage_complete"] = False
                    current_usage = {"input_tokens": 0, "output_tokens": 0}
                    seen_usage = set()
                    current_complete = False
                    current_search_complete = False
                    search_allowance = min(2, self.limits.server_search_calls - usage["server_search_calls"])
                    call_search_available = search_available and search_allowance > 0
                    request_definitions = list(definitions)
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
                    system += f"\n当前服务器时间：{clock['as_of']}；‘今天’以此为准，研究启动观察时点仍保留。"
                    stream = model_stream(
                        messages,
                        system=system,
                        tools=request_definitions,
                        model_cfg=model_cfg,
                        max_tokens=self.limits.output_tokens_per_call,
                        timeout_seconds=max(0.1, end - time.monotonic()),
                    )
                    try:
                        async with contextlib.aclosing(stream):
                            while True:
                                try:
                                    event = await self._wait(anext(stream), user, run_id, cancel, end)
                                except StopAsyncIteration:
                                    break
                                kind = event.get("type")
                                if kind == "text_delta":
                                    pending += event.get("text", "")
                                    if len(full_text) + len(pending) > self.limits.output_characters:
                                        raise BudgetExceeded
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
                                        or usage["server_search_calls"] > self.limits.server_search_calls
                                    ):
                                        raise BudgetExceeded
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
                    finally:
                        for key, value in current_usage.items():
                            usage[key] += value
                        usage["usage_complete"] = previous_complete and current_complete
                        usage["server_search_usage_complete"] = (
                            previous_search_complete and current_search_complete
                        )
                        # Preserve observed counters even when the stream failed.
                        # A missing counter remains unknown, never certified zero.
                        emit("model_usage", usage=dict(usage), call_usage_complete=current_complete)
                    emit("model_completed", usage=dict(usage), stop_reason=completed["stop_reason"])
                    content = completed["content"]
                    continuation = content.continuation() if hasattr(content, "continuation") else content
                    messages.append({"role": "assistant", "content": continuation})
                    if completed["stop_reason"] == "tool_use":
                        if not requested:
                            raise provider.ProviderError("provider_protocol_error")
                        results = []
                        for call in requested:
                            usage["tool_calls"] += 1
                            if usage["tool_calls"] > self.limits.tool_calls:
                                raise BudgetExceeded
                            name, arguments, call_id = call.get("name"), call.get("arguments"), call.get("id")
                            if not isinstance(call_id, str) or not re.fullmatch(
                                r"[A-Za-z0-9_-]{1,128}", call_id
                            ):
                                raise provider.ProviderError("provider_protocol_error")
                            emit("tool_started", {"tool": name, "n": usage["tool_calls"]}, tool=name)
                            result = await self._wait(
                                tools.execute(name, arguments, call_id), user, run_id, cancel, end
                            )
                            if result.get("call"):
                                calls.append(result["call"])
                                chunks.append({"t": "platform_call", "call": result["call"]})
                                emit("tool_completed", {"platform_call": result["call"]}, ok=result["ok"])
                            for ref in result.get("refs", []):
                                refs.append(ref)
                                chunks.append({"t": "ref", **ref})
                                emit("source_recorded", {"ref": ref})
                            results.append(
                                {
                                    "type": "tool_result",
                                    "tool_use_id": call_id,
                                    "content": json.dumps(result, ensure_ascii=False),
                                    "is_error": not result["ok"],
                                }
                            )
                        messages.append({"role": "user", "content": results})
                    elif completed["stop_reason"] in {"end_turn", "stop_sequence", "refusal"}:
                        finished = True
                        break
                    elif completed["stop_reason"] == "pause_turn":
                        continue  # A successful RAM continuation, never a failed-request retry.
                    else:
                        outcome.error_code = "output_incomplete"
                        break
                if not finished and not outcome.error_code:
                    raise BudgetExceeded
                if finished and not full_text.strip():
                    finished, outcome.error_code = False, "output_incomplete"
                outcome.status = "completed" if finished else "partial" if full_text else "failed"
        except RunStopped:
            outcome.status, outcome.error_code = "cancelled", "cancelled_by_user"
        except BudgetExceeded:
            progressed = full_text or pending or usage["server_search_calls"] or usage["tool_calls"] or refs
            outcome.status, outcome.error_code = (
                "partial" if progressed else "failed",
                "budget_exceeded",
            )
        except provider.ProviderError as error:
            outcome.status, outcome.error_code = "partial" if full_text or pending else "failed", error.code
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
        except Exception:
            outcome.status, outcome.error_code = (
                "partial" if full_text or pending else "failed",
                "research_unavailable",
            )
        finally:
            if acquired:
                self._slots.release()
        try:
            flush(final=True)
            if outcome.error_code and outcome.status != "cancelled":
                emit(
                    "run_error", {"error": _error_message(outcome.error_code)}, error_code=outcome.error_code
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
                research.update(
                    status=revision["acceptance_status"],
                    revision_id=revision["revision_id"],
                    gaps=revision["gaps"],
                )
                outcome.research_revision = revision["revision_id"]
                anchor = ensure_dialogue(user, run)
                payload = json.dumps(
                    source_display_payload(
                        {
                            "text": full_text,
                            "chunks": chunks,
                            "refs": refs,
                            "research": research,
                            "native_agent": {
                                "run_id": run_id,
                                "engine": "prism-native",
                                "status": outcome.status,
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
                        "summary": full_text[:240],
                        "report": {
                            "question": run["question"],
                            "final_decision": full_text,
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
                outcome.user, outcome.run_id, outcome.status, outcome.text, outcome.error_code
            )
            if terminal["status"] != outcome.status:
                raise RuntimeError("native_terminal_persistence_unconfirmed")

        except HTTPException as error:
            if error.status_code not in {401, 404}:
                raise
            outcome.abandoned = True
            _retire_removed_owner(outcome)


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
