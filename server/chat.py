"""聊天流式端点 — Claude Code JSONL 集成 + 双向交互 + 参考引用"""

import json
import logging
import re
import time
from contextlib import aclosing
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from .analysis_graph import persist_graph
from .auth import require_auth
from .config import DEFAULT_MODEL, MODELS, RATE_CHAT_PER_MIN
from .database import check_rate_limit, get_db
from .research import (
    create_research_run,
    finalize_research_run,
    ingest_tool_result,
    set_run_status,
)
from .research_context import (
    EvidenceTracker,
    build_research_context,
    extract_web_refs,
    parse_claude_jsonl_events,
)
from .services.claude_runner import (
    cancel_process,
    respond_to_question,
    run_claude_stream,
)
from .source_urls import redact_source_text, source_display_payload

log = logging.getLogger("stock-app")

# Claude Code stores project data under ~/.claude/projects/
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

# Formatting & language rules are handled by the readable-ai skill (CLAUDE.md)
# loaded by Claude Code — no server-side prompt injection needed.


def _clean_object_artifacts(text: str, *, strip_edges: bool = True) -> str:
    """Remove JavaScript object-stringification artifacts from Claude CLI output."""
    if not text:
        return ""
    cleaned = str(text).replace("[object Object]", "")
    cleaned = re.sub(
        r'([^\n])\s*["\']>\s*(?=[\u3400-\u9fffA-Za-z0-9])',
        r"\1 ",
        cleaned,
    )
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip() if strip_edges else cleaned


_REPORT_DELIVERY_RE = re.compile(
    r"^#{1,4}\s*(?:报告已交付|最终报告|最终结论|投资结论|分析结论|核心发现)\b.*$",
    re.IGNORECASE | re.MULTILINE,
)
_REPORT_FAILURE_RE = re.compile(
    r"API Error|operation timed out|timed out|请求超时|任务中止",
    re.IGNORECASE,
)


def _prepare_chat_report(text: str, skill_name: str = "") -> tuple[str, str]:
    """Keep delivered research while marking interrupted skill runs honestly."""
    cleaned = _clean_object_artifacts(text)
    matches = list(_REPORT_DELIVERY_RE.finditer(cleaned))
    if matches:
        return cleaned[matches[-1].start() :].strip(), "complete"
    if skill_name or _REPORT_FAILURE_RE.search(cleaned):
        return cleaned, "incomplete"
    return cleaned, "complete"


def _prioritize_report_files(files: list[str]) -> list[str]:
    """Put directly readable reports before supporting CSV/JSON artifacts."""
    priority = {".html": 0, ".htm": 0, ".pdf": 1, ".md": 2, ".csv": 3, ".json": 4}
    indexed = list(enumerate(files))
    indexed.sort(key=lambda item: (priority.get(Path(item[1]).suffix.lower(), 9), item[0]))
    return [name for _, name in indexed]


def _result_payload_text(event: dict) -> str:
    """Recover final text carried only by Claude CLI's terminal result event."""
    for key in ("result", "content", "message"):
        text = _content_to_text(event.get(key))
        if text:
            return text
    return ""


def _cwd_to_project_dir(cwd: str) -> str:
    """Convert /home/ubuntu/.claude/skills/foo → -home-ubuntu--claude-skills-foo"""
    segments = cwd.strip("/").split("/")
    converted = []
    for seg in segments:
        if seg.startswith("."):
            seg = "-" + seg[1:]  # .claude → -claude
        converted.append(seg)
    return "-" + "-".join(converted)


def _extract_refs_from_tool_result(content_text: str) -> list[dict]:
    """Parse links; provenance is established separately by EvidenceTracker."""
    return extract_web_refs(content_text)


def _content_to_text(value) -> str:
    """Convert Claude content blocks into readable text, never object reprs."""
    if value is None:
        return ""
    if isinstance(value, str):
        return _clean_object_artifacts(value)
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(part for part in (_content_to_text(v) for v in value) if part)
    if isinstance(value, dict):
        typ = value.get("type", "")
        if typ == "tool_use":
            name = value.get("name", "tool")
            detail = _content_to_text(value.get("input", ""))
            return f"{name}: {detail}" if detail else str(name)
        for key in ("text", "thinking", "content", "question", "summary"):
            if key in value:
                text = _content_to_text(value.get(key))
                if text:
                    return text
        try:
            return _clean_object_artifacts(json.dumps(value, ensure_ascii=False))
        except TypeError:
            return _clean_object_artifacts(str(value))
    return _clean_object_artifacts(str(value))


async def _read_jsonl_messages(session_id: str, project_dir: str) -> list[dict]:
    """Read historical turns asynchronously using the shared provenance parser."""
    import aiofiles

    jsonl_path = CLAUDE_PROJECTS / project_dir / f"{session_id}.jsonl"
    if not jsonl_path.exists():
        return []
    events = []
    async with aiofiles.open(jsonl_path, encoding="utf-8") as f:
        async for line in f:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return parse_claude_jsonl_events(events, session_id=session_id)


async def chat_stream(request: Request):
    """POST /api/chat/stream — Claude Code CLI streaming with SSE + reference citations.
    Returns: ``StreamingResponse`` with the SSE stream, or ``JSONResponse`` for
    early validation/rate-limit errors."""
    from .models import ChatStreamRequest, validation_error_message

    user = require_auth(request)
    if not check_rate_limit(f"chat:{user}", RATE_CHAT_PER_MIN, 60):
        return JSONResponse({"error": "请求太频繁"}, status_code=429)

    body = await request.json()
    try:
        req = ChatStreamRequest(**(body or {}))
    except Exception as e:
        return JSONResponse({"error": validation_error_message(e)}, status_code=422)
    question = req.question.strip()
    skill_name = req.skill
    extra_skills = req.extra_skills  # Additional skills via --append-system-prompt
    conv_id = req.conv_id or ""
    model_id = req.model or DEFAULT_MODEL
    if model_id not in MODELS:
        return JSONResponse({"error": "所选模型不存在，请重新选择"}, status_code=422)
    model_cfg = MODELS[model_id]
    if not model_cfg.get("api_key"):
        return JSONResponse({"error": "所选模型尚未配置认证，请选择可用模型或联系管理员"}, status_code=503)
    if not question:
        return JSONResponse({"error": "空问题"}, status_code=400)

    t0 = time.time()
    log.info(f"[{user}] Q({skill_name}): {redact_source_text(question[:80])}...")

    # Validate ownership before reading session history, writing, or starting an engine.
    resume_session_id = None
    resume_project_dir = None
    history_anchor = {"db_message_id": 0, "jsonl_user_turns": 0}
    if conv_id:
        with get_db() as db:
            row = db.execute(
                "SELECT session_id, project_dir FROM conversations "
                "WHERE id=? AND username=? AND deleted_at IS NULL",
                [conv_id, user],
            ).fetchone()
            if not row:
                return JSONResponse({"error": "对话不存在"}, status_code=404)
            resume_session_id = row["session_id"] or None
            resume_project_dir = row["project_dir"] or None
            history_anchor["db_message_id"] = db.execute(
                "SELECT COALESCE(MAX(id), 0) FROM messages WHERE conv_id=?", [conv_id]
            ).fetchone()[0]
        if resume_session_id and resume_project_dir:
            history = await _read_jsonl_messages(resume_session_id, resume_project_dir)
            history_anchor["jsonl_user_turns"] = sum(m["role"] == "user" for m in history)
        with get_db() as db:
            # Re-check within the write transaction after the asynchronous history read.
            if not db.execute(
                "SELECT 1 FROM conversations WHERE id=? AND username=? AND deleted_at IS NULL",
                [conv_id, user],
            ).fetchone():
                return JSONResponse({"error": "对话不存在"}, status_code=404)
            db.execute(
                "INSERT INTO messages (conv_id, role, content) VALUES (?,?,?)",
                [conv_id, "user", redact_source_text(question)],
            )
            db.execute(
                "UPDATE conversations SET title=? WHERE id=? AND username=? AND title='新对话'",
                [redact_source_text(question)[:40], conv_id, user],
            )
    research = build_research_context(engine="chat")
    create_research_run(user, conv_id, redact_source_text(question), research)
    research["ledger_available"] = True
    research["status"] = "queued"
    progress = {"text": "", "finished": False}

    async def generate():
        session_id = resume_session_id  # May be None for first message
        project_dir = resume_project_dir

        def _emit(event):
            """Emit JSON-safe SSE data without lossy object stringification."""

            def _json_safe(v):
                if v is None:
                    return None
                if isinstance(v, str):
                    return _content_to_text(v)
                if isinstance(v, (int, float, bool)):
                    return v
                if isinstance(v, list):
                    return [_json_safe(x) for x in v]
                if isinstance(v, dict):
                    return {str(k): _json_safe(val) for k, val in v.items()}
                return _content_to_text(v)

            clean = {str(k): _json_safe(v) for k, v in event.items()}
            if isinstance(event.get("text"), str):
                # Preserve paragraph boundaries when the client appends text;
                # apply the same artifact filter without trimming the edges.
                clean["text"] = _clean_object_artifacts(event["text"], strip_edges=False)
            return f"data: {json.dumps(source_display_payload(clean), ensure_ascii=False)}\n\n"

        full_text = ""
        step_num = 0
        collected_refs = []
        evidence = EvidenceTracker(research["run_id"])
        live_chunks = [{"t": "research", "research": research}]
        platform_calls = {}
        stream_error = ""
        stream_cancelled = False
        terminal_result_seen = False
        delivery_note = ""
        # Shared skill directories have no provable owner/run binding. Only
        # explicitly registered artifacts may ever appear as report files.
        delivery_reports = []

        yield _emit({"research": research})
        yield _emit({"pulse": "start"})
        set_run_status(user, research["run_id"], "discovering")

        engine = run_claude_stream(
            question,
            skill_name,
            user=user,
            model_cfg=model_cfg,
            session_id=resume_session_id,
            conv_id=conv_id,
            extra_skills=extra_skills,
            research_context=research,
        )
        progress["engine"] = engine
        async for ev in engine:
            t = ev.get("type", "")

            # Capture session info from system/init (first message only)
            if t == "system" and ev.get("subtype") == "init":
                new_sid = ev.get("session_id", "")
                cwd = ev.get("cwd", "")
                if new_sid and cwd:
                    session_id = new_sid
                    project_dir = _cwd_to_project_dir(cwd)
                    log.info(f"[{user}] session={session_id} project={project_dir}")

            # Only a successful result associated with this turn's known call is a source.
            if t == "user":
                msg = ev.get("message", {})
                content = msg.get("content", []) if isinstance(msg, dict) else []
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            refs = evidence.record_result(
                                block, retrieved_at=build_research_context()["as_of"]
                            )
                            if refs:
                                call = evidence.calls.get(str(block.get("tool_use_id") or ""), {})
                                refs = ingest_tool_result(user, research["run_id"], call, block, refs)
                            for ref in refs:
                                collected_refs.append(ref)
                                live_chunks.append({"t": "ref", **ref})
                                yield _emit({"ref": ref})

            if t == "platform_call":
                call = ev.get("call") or {}
                if call.get("call_id") and call.get("run_id") == research["run_id"]:
                    platform_calls[call["call_id"]] = call
                    yield _emit({"platform_call": call})

            # tool_use event (top-level)
            if t == "tool_use":
                tool = ev.get("tool", {})
                evidence.record_call(tool)
                name = tool.get("name", "?")
                inp = tool.get("input", {})
                step_num += 1
                live_chunks.append({"t": "tool", "c": name, "d": redact_source_text(_content_to_text(inp))})
                if name == "AskUserQuestion":
                    yield _emit(
                        {
                            "tool": name,
                            "n": step_num,
                            "ask": inp.get("question", ""),
                            "opts": inp.get("options", []),
                        }
                    )
                else:
                    detail = _content_to_text(list(inp.values())[:1]) if inp else ""
                    yield _emit({"tool": name, "n": step_num, "d": detail})

            # assistant message
            elif t == "assistant":
                for b in ev.get("message", {}).get("content", []):
                    bt = b.get("type", "")
                    if bt == "server_tool_use":
                        evidence.record_server_call(b)
                    elif bt == "web_search_tool_result":
                        normalized = evidence.record_server_result(
                            b, retrieved_at=build_research_context()["as_of"]
                        )
                        if normalized:
                            call, result_block, refs = normalized
                            refs = ingest_tool_result(user, research["run_id"], call, result_block, refs)
                            for ref in refs:
                                collected_refs.append(ref)
                                live_chunks.append({"t": "ref", **ref})
                                yield _emit({"ref": ref})
                    elif bt == "text":
                        txt = redact_source_text(_content_to_text(b.get("text", "")))
                        if not txt:
                            continue
                        full_text += txt
                        progress["text"] = full_text
                        live_chunks.append({"t": "text", "c": txt})
                        yield _emit({"text": txt})
                    elif bt == "tool_use":
                        evidence.record_call(b)
                        step_num += 1
                        name = b.get("name", "?")
                        inp = b.get("input", {})
                        live_chunks.append(
                            {"t": "tool", "c": name, "d": redact_source_text(_content_to_text(inp))}
                        )
                        if name == "AskUserQuestion":
                            yield _emit(
                                {
                                    "tool": name,
                                    "n": step_num,
                                    "ask": inp.get("question", ""),
                                    "opts": inp.get("options", []),
                                }
                            )
                        else:
                            detail = _content_to_text(list(inp.values())[:1]) if inp else ""
                            yield _emit({"tool": name, "n": step_num, "d": detail})
                    elif bt == "thinking":
                        thought = redact_source_text(_content_to_text(b.get("thinking", "")))
                        if thought:
                            live_chunks.append({"t": "think", "c": thought})
                            yield _emit({"think": thought})

            elif t == "status":
                message = _content_to_text(ev.get("message"))
                if message:
                    live_chunks.append({"t": "status", "c": message})
                    yield _emit({"status": message})

            # Error from Claude
            elif t == "cancelled":
                stream_cancelled = True
                message = "用户已取消本轮研究"
                live_chunks.append({"t": "status", "c": message})
                yield _emit({"status": message})

            elif t == "error":
                stream_error = redact_source_text(_content_to_text(ev.get("message"))) or "AI引擎异常"
                log.error(f"[{user}] Claude error: {stream_error}")
                yield _emit({"error": stream_error})

            elif t == "result":
                terminal_result_seen = True
                if ev.get("is_error") or str(ev.get("subtype", "")).startswith("error"):
                    stream_error = stream_error or "研究引擎返回未完成状态"
                log.info(
                    f"[{user}] Claude done: {ev.get('duration_ms', 0)}ms stop={ev.get('stop_reason', '')}"
                )
                terminal_text = redact_source_text(_result_payload_text(ev))
                if terminal_text and terminal_text not in full_text:
                    if full_text and terminal_text.startswith(full_text):
                        terminal_text = terminal_text[len(full_text) :]
                    elif full_text.endswith(terminal_text):
                        terminal_text = ""
                    if terminal_text:
                        addition = terminal_text if not full_text else "\n\n" + terminal_text
                        full_text += addition
                        progress["text"] = full_text
                        live_chunks.append({"t": "text", "c": addition})
                        yield _emit({"text": addition, "recovered": True})

            elif t == "heartbeat":
                yield _emit({"pulse": "wait", "idle_ms": ev.get("idle_ms", 0)})

        if not terminal_result_seen and not stream_error and not stream_cancelled:
            stream_error = "研究引擎未返回结束状态，已保留收到的内容"
        if skill_name and (not _REPORT_DELIVERY_RE.search(full_text) or stream_error or stream_cancelled):
            reason = "用户已取消本轮研究" if stream_cancelled else _clean_object_artifacts(stream_error)
            delivery_note = "\n\n**本轮尚未形成完整可核查报告**。"
            if reason:
                delivery_note += reason + "。"
            delivery_note += "已保留回答与研究过程，可在本对话中发送“继续完成报告”发起后续研究。"
            full_text += delivery_note
            live_chunks.append({"t": "text", "c": delivery_note})
            yield _emit({"text": delivery_note, "delivery": True})

        elapsed = time.time() - t0
        log.info(f"[{user}] DONE {elapsed:.1f}s len={len(full_text)} refs={len(collected_refs)}")

        report_text, _ = _prepare_chat_report(full_text, skill_name)
        revision = finalize_research_run(
            user, research["run_id"], report_text, error=bool(stream_error), cancelled=stream_cancelled
        )
        progress["finished"] = True
        report_status = revision["acceptance_status"]
        research.update(
            status=report_status,
            revision_id=revision["revision_id"],
            gaps=revision["gaps"],
            information_cutoff=revision.get("information_cutoff"),
            cutoff_precision=revision.get("cutoff_precision", "unknown"),
            requested_period=revision.get("requested_period"),
        )
        yield _emit({"research": research})

        if conv_id and full_text.strip():
            try:
                from .reports import save_report_snapshot

                clean_text = " ".join(report_text.split())
                title = redact_source_text(question)[:80] + ("..." if len(question) > 80 else "")
                save_report_snapshot(
                    user,
                    {
                        "source": "chat",
                        "source_id": research["run_id"],
                        "conv_id": conv_id,
                        "title": title or "Chat report",
                        "summary": clean_text[:240],
                        "report": {
                            "question": redact_source_text(question),
                            "final_decision": report_text,
                            "status": report_status,
                            "model": model_id,
                            "skill": skill_name,
                            "references": collected_refs,
                            "research": research,
                            "research_revision_id": revision["revision_id"],
                            "run_id": research["run_id"],
                        },
                    },
                )
            except Exception as e:
                log.warning(f"[{user}] Failed to save chat report snapshot: {e}")

        # Persist only this turn. Re-importing every JSONL assistant duplicates old research.
        if conv_id:
            live_chunks.extend({"t": "platform_call", "call": call} for call in platform_calls.values())
            payload = json.dumps(
                source_display_payload(
                    {
                        "text": full_text,
                        "chunks": live_chunks,
                        "refs": collected_refs,
                        "research": research,
                        "history_anchor": history_anchor,
                        "report_status": report_status,
                        "report_files": delivery_reports[:5],
                    }
                ),
                ensure_ascii=False,
            )
            with get_db() as db:
                if db.execute(
                    "SELECT 1 FROM conversations WHERE id=? AND username=? AND deleted_at IS NULL",
                    [conv_id, user],
                ).fetchone():
                    if session_id and project_dir:
                        db.execute(
                            "UPDATE conversations SET session_id=?, project_dir=? WHERE id=? AND username=?",
                            [session_id, project_dir, conv_id, user],
                        )
                    db.execute(
                        "INSERT INTO messages (conv_id, role, content) VALUES (?,?,?)",
                        [conv_id, "assistant", payload],
                    )

        # Auto-extract analysis graph from saved messages
        if conv_id:
            try:
                skill = ""
                with get_db() as db:
                    row = db.execute("SELECT skill FROM conversations WHERE id=?", [conv_id]).fetchone()
                    if row:
                        skill = row["skill"]
                with get_db() as db:
                    rows = db.execute(
                        "SELECT content, role FROM messages WHERE conv_id=? ORDER BY id", [conv_id]
                    ).fetchall()
                    messages_json = []
                    for r in rows:
                        try:
                            parsed = json.loads(r["content"])
                        except Exception:
                            parsed = {"text": r["content"]}
                        parsed["role"] = r["role"]
                        messages_json.append(parsed)
                persist_graph(conv_id, messages_json, skill)
            except Exception as e:
                log.warning(f"[{user}] Failed to extract analysis graph: {e}")

        yield _emit(
            {
                "report_status": report_status,
                "report_files": delivery_reports[:5],
                "elapsed": round(elapsed, 1),
            }
        )
        yield "data: [DONE]\n\n"

    async def tracked_generate():
        failed = False
        try:
            async with aclosing(generate()) as events:
                async for event in events:
                    yield event
        except Exception:
            failed = True
            raise
        finally:
            try:
                engine = progress.get("engine")
                if engine is not None:
                    await engine.aclose()
            finally:
                if not progress["finished"]:
                    try:
                        finalize_research_run(
                            user, research["run_id"], progress["text"], error=failed, cancelled=not failed
                        )
                    except Exception:
                        # A deleted/reassigned conversation must not be revived or
                        # written through just because an engine was still running.
                        log.warning("Research interrupted; finalization unavailable")

    return StreamingResponse(tracked_generate(), media_type="text/event-stream")


async def chat_respond(request: Request):
    """POST /api/chat/respond — Send user's answer to a running AskUserQuestion."""
    from .models import ChatRespondRequest, validation_error_message

    user = require_auth(request)
    body = await request.json()
    try:
        req = ChatRespondRequest(**(body or {}))
    except Exception as e:
        return JSONResponse({"error": validation_error_message(e)}, status_code=422)
    conv_id = req.conv_id
    answer = req.answer.strip()
    if not conv_id or not answer:
        return JSONResponse({"error": "缺少 conv_id 或 answer"}, status_code=400)

    with get_db() as db:
        if not db.execute(
            "SELECT 1 FROM conversations WHERE id=? AND username=? AND deleted_at IS NULL",
            [conv_id, user],
        ).fetchone():
            return JSONResponse({"error": "对话不存在"}, status_code=404)
    ok = await respond_to_question(conv_id, answer)
    if ok:
        return {"ok": True, "message": "回答已发送"}
    else:
        return JSONResponse({"error": "没有正在运行的对话进程"}, status_code=404)


async def chat_cancel(request: Request):
    """POST /api/chat/cancel — Cancel a running Claude process."""
    from .models import ChatCancelRequest, validation_error_message

    user = require_auth(request)
    body = await request.json()
    try:
        req = ChatCancelRequest(**(body or {}))
    except Exception as e:
        return JSONResponse({"error": validation_error_message(e)}, status_code=422)
    conv_id = req.conv_id
    if not conv_id:
        return JSONResponse({"error": "缺少 conv_id"}, status_code=400)

    with get_db() as db:
        if not db.execute(
            "SELECT 1 FROM conversations WHERE id=? AND username=? AND deleted_at IS NULL",
            [conv_id, user],
        ).fetchone():
            return JSONResponse({"error": "对话不存在"}, status_code=404)
    ok = await cancel_process(conv_id)
    return {"ok": ok}
