"""聊天流式端点 — Claude Code JSONL 集成 + 双向交互 + 参考引用"""

import json
import logging
import re
import time
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from .analysis_graph import persist_graph
from .auth import require_auth
from .config import DEFAULT_MODEL, MODELS, RATE_CHAT_PER_MIN
from .database import check_rate_limit, get_db
from .services.claude_runner import (
    _resolve_skill_dir,
    cancel_process,
    respond_to_question,
    run_claude_stream,
)

log = logging.getLogger("stock-app")

# Claude Code stores project data under ~/.claude/projects/
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

# Formatting & language rules are handled by the readable-ai skill (CLAUDE.md)
# loaded by Claude Code — no server-side prompt injection needed.


def _clean_object_artifacts(text: str) -> str:
    """Remove JavaScript object-stringification artifacts from Claude CLI output."""
    if not text:
        return ""
    cleaned = str(text).replace("[object Object]", "")
    cleaned = re.sub(
        r'([^\n])\s*["\']>\s*(?=[\u3400-\u9fffA-Za-z0-9])',
        r"\1 ",
        cleaned,
    )
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


_REPORT_DELIVERY_RE = re.compile(
    r"^#{1,4}\s*(?:报告已交付|最终报告|最终结论|投资结论|分析结论|核心发现)\b.*$",
    re.IGNORECASE | re.MULTILINE,
)
_REPORT_FAILURE_RE = re.compile(
    r"API Error|operation timed out|timed out|请求超时|任务中止",
    re.IGNORECASE,
)
_REPORT_FILE_EXTENSIONS = {".html", ".htm", ".pdf", ".md"}


def _prepare_chat_report(text: str, skill_name: str = "") -> tuple[str, str]:
    """Keep delivered research while marking interrupted skill runs honestly."""
    cleaned = _clean_object_artifacts(text)
    matches = list(_REPORT_DELIVERY_RE.finditer(cleaned))
    if matches:
        return cleaned[matches[-1].start() :].strip(), "complete"
    if skill_name or _REPORT_FAILURE_RE.search(cleaned):
        return cleaned, "incomplete"
    return cleaned, "complete"


def _skill_report_snapshot(skill_name: str) -> dict[str, tuple[int, int]]:
    """Return safe metadata for report files produced by the active skill."""
    try:
        from .config import SKILL_MAP

        info = SKILL_MAP.get(skill_name)
        if not info:
            return {}
        skill_dir, _ = _resolve_skill_dir(info["dir"])
        reports_dir = skill_dir / "reports" if skill_dir else None
        if not reports_dir or not reports_dir.exists():
            return {}
        snapshot = {}
        for path in reports_dir.iterdir():
            if not path.is_file() or path.suffix.lower() not in _REPORT_FILE_EXTENSIONS:
                continue
            stat = path.stat()
            snapshot[path.name] = (stat.st_mtime_ns, stat.st_size)
        return snapshot
    except OSError:
        return {}


def _changed_report_files(before: dict, after: dict) -> list[str]:
    changed = [name for name, meta in after.items() if before.get(name) != meta and meta[1] > 0]
    return sorted(changed, key=lambda name: after[name][0], reverse=True)


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
    """Extract web references from tool_result content (WebSearch/WebFetch).

    WebSearch results typically contain markdown links like:
      1. [Title](https://url) - snippet
    WebFetch returns a markdown page with the URL as context.
    """
    refs = []
    # Match markdown links: [title](url)
    url_pattern = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
    seen_urls = set()
    for match in url_pattern.finditer(content_text):
        title = match.group(1).strip()
        url = match.group(2).strip()
        # Skip internal anchor links and non-web URLs
        if not url.startswith(("http://", "https://")):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        # Extract a snippet after the link (up to 120 chars)
        after_pos = match.end()
        snippet = content_text[after_pos : after_pos + 160].strip()
        # Clean up snippet: remove leading dash/bullet
        snippet = re.sub(r"^[\s\-•·–—]+", "", snippet)
        if len(snippet) > 120:
            snippet = snippet[:120] + "…"
        refs.append({"url": url, "title": title, "snippet": snippet})
    return refs


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
    """Read Claude Code JSONL, merging consecutive assistant events into single turns.

    Async (uses aiofiles) so it doesn't block the event loop when called
    from inside the SSE generator in chat_stream.

    Returns list of {role, content} dicts where content is JSON string with
    {text, chunks, refs}. Also captures WebSearch/WebFetch tool results
    as 'ref' chunks.
    """
    import aiofiles

    jsonl_path = CLAUDE_PROJECTS / project_dir / f"{session_id}.jsonl"
    if not jsonl_path.exists():
        return []

    messages = []
    collected_refs = []  # Accumulate refs across the conversation

    async with aiofiles.open(jsonl_path, encoding="utf-8") as f:
        async for line in f:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = ev.get("type", "")

            if t == "user":
                msg = ev.get("message", "")
                if isinstance(msg, dict):
                    content_blocks = msg.get("content", "")
                else:
                    try:
                        msg_dict = json.loads(str(msg).replace("'", '"'))
                        content_blocks = msg_dict.get("content", str(msg))
                    except Exception:
                        content_blocks = str(msg)

                # Check if this is a tool_result message
                if isinstance(content_blocks, list):
                    for block in content_blocks:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            result_content = block.get("content", "")
                            if isinstance(result_content, list):
                                result_text = ""
                                for rc in result_content:
                                    if isinstance(rc, dict) and rc.get("type") == "text":
                                        result_text += rc.get("text", "")
                                    elif isinstance(rc, str):
                                        result_text += rc
                            elif isinstance(result_content, str):
                                result_text = result_content
                            else:
                                result_text = _content_to_text(result_content)
                            # Extract URLs from WebSearch/WebFetch results
                            refs = _extract_refs_from_tool_result(result_text)
                            collected_refs.extend(refs)
                            continue  # Skip tool_result — don't add as user message

                content_str = _content_to_text(content_blocks)
                if (
                    content_str
                    and not content_str.startswith("[{'type':")
                    and not content_str.startswith("[{'tool_use_id':")
                ):
                    messages.append({"role": "user", "content": content_str})

            elif t == "assistant":
                blocks = ev.get("message", {}).get("content", [])
                chunks = []
                for b in blocks:
                    bt = b.get("type", "")
                    if bt == "text":
                        text = _content_to_text(b.get("text", ""))
                        if text:
                            chunks.append({"t": "text", "c": text})
                    elif bt == "tool_use":
                        name = b.get("name", "?")
                        inp = b.get("input", {})
                        detail = _content_to_text(list(inp.values())[:1]) if inp else ""
                        chunks.append({"t": "tool", "c": name, "d": detail})
                    elif bt == "thinking":
                        thought = _content_to_text(b.get("thinking", ""))
                        if thought:
                            chunks.append({"t": "think", "c": thought})

                if not chunks:
                    continue

                # Merge consecutive assistant messages
                if messages and messages[-1]["role"] == "assistant":
                    prev = json.loads(messages[-1]["content"])
                    prev["chunks"].extend(chunks)
                    txts = [c["c"] for c in prev["chunks"] if c["t"] == "text"]
                    prev["text"] = "".join(txts)
                    messages[-1]["content"] = json.dumps(prev, ensure_ascii=False)
                else:
                    txts = [c["c"] for c in chunks if c["t"] == "text"]
                    content = json.dumps({"text": "".join(txts), "chunks": chunks}, ensure_ascii=False)
                    messages.append({"role": "assistant", "content": content})

    # Attach collected references to the last assistant message
    if collected_refs and messages:
        for i in range(len(messages) - 1, -1, -1):
            if messages[i]["role"] == "assistant":
                prev = json.loads(messages[i]["content"])
                for ref in collected_refs:
                    prev.setdefault("chunks", []).append(
                        {
                            "t": "ref",
                            "url": ref["url"],
                            "title": ref["title"],
                            "snippet": ref.get("snippet", ""),
                        }
                    )
                messages[i]["content"] = json.dumps(prev, ensure_ascii=False)
                break

    return messages


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
    model_cfg = MODELS.get(model_id, MODELS[DEFAULT_MODEL])
    if not question:
        return JSONResponse({"error": "空问题"}, status_code=400)

    t0 = time.time()
    log.info(f"[{user}] Q({skill_name}): {question[:80]}...")

    # Determine if we should resume a previous session
    resume_session_id = None
    if conv_id:
        with get_db() as db:
            row = db.execute(
                "SELECT session_id, project_dir FROM conversations WHERE id=? AND username=?", [conv_id, user]
            ).fetchone()
            if row and row["session_id"]:
                resume_session_id = row["session_id"]

    # Save user message
    if conv_id:
        with get_db() as db:
            db.execute(
                "INSERT INTO messages (conv_id, role, content) VALUES (?,?,?)", [conv_id, "user", question]
            )
            db.execute(
                "UPDATE conversations SET title=? WHERE id=? AND title='新对话'", [question[:40], conv_id]
            )

    async def generate():
        session_id = resume_session_id  # May be None for first message
        project_dir = None

        def _emit(event):
            """Emit JSON-safe SSE data without lossy object stringification."""

            def _json_safe(v):
                if v is None:
                    return ""
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
            return f"data: {json.dumps(clean, ensure_ascii=False)}\n\n"

        full_text = ""
        step_num = 0
        collected_refs = []
        seen_ref_urls = set()
        stream_error = ""
        delivery_note = ""
        report_files_before = _skill_report_snapshot(skill_name)

        yield _emit({"pulse": "start"})

        async for ev in run_claude_stream(
            question,
            skill_name,
            user=user,
            model_cfg=model_cfg,
            session_id=resume_session_id,
            conv_id=conv_id,
            extra_skills=extra_skills,
        ):
            t = ev.get("type", "")

            # Capture session info from system/init (first message only)
            if t == "system" and ev.get("subtype") == "init":
                new_sid = ev.get("session_id", "")
                cwd = ev.get("cwd", "")
                if new_sid and cwd:
                    session_id = new_sid
                    project_dir = _cwd_to_project_dir(cwd)
                    log.info(f"[{user}] session={session_id} project={project_dir}")

            # tool_result events — capture WebSearch/WebFetch results as references
            if t == "user":
                msg = ev.get("message", {})
                if isinstance(msg, dict):
                    content = msg.get("content", [])
                else:
                    content = []
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            result_content = block.get("content", "")
                            if isinstance(result_content, list):
                                result_text = ""
                                for rc in result_content:
                                    if isinstance(rc, dict) and rc.get("type") == "text":
                                        result_text += rc.get("text", "")
                                    elif isinstance(rc, str):
                                        result_text += rc
                            elif isinstance(result_content, str):
                                result_text = result_content
                            else:
                                result_text = _content_to_text(result_content)
                            # Extract URLs and send as reference citations
                            refs = _extract_refs_from_tool_result(result_text)
                            for ref in refs:
                                if ref["url"] not in seen_ref_urls:
                                    seen_ref_urls.add(ref["url"])
                                    collected_refs.append(ref)
                                    yield _emit({"ref": ref})

            # tool_use event (top-level)
            if t == "tool_use":
                tool = ev.get("tool", {})
                name = tool.get("name", "?")
                inp = tool.get("input", {})
                step_num += 1
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
                    if bt == "text":
                        txt = _content_to_text(b.get("text", ""))
                        if not txt:
                            continue
                        full_text += txt
                        yield _emit({"text": txt})
                    elif bt == "tool_use":
                        step_num += 1
                        name = b.get("name", "?")
                        inp = b.get("input", {})
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
                        thought = _content_to_text(b.get("thinking", ""))
                        if thought:
                            yield _emit({"think": thought})

            # Error from Claude
            elif t == "error":
                stream_error = _content_to_text(ev.get("message")) or "AI引擎异常"
                log.error(f"[{user}] Claude error: {stream_error}")
                yield _emit({"error": stream_error})

            elif t == "result":
                log.info(
                    f"[{user}] Claude done: {ev.get('duration_ms', 0)}ms stop={ev.get('stop_reason', '')}"
                )
                terminal_text = _result_payload_text(ev)
                if terminal_text and terminal_text not in full_text:
                    if full_text and terminal_text.startswith(full_text):
                        terminal_text = terminal_text[len(full_text) :]
                    elif full_text.endswith(terminal_text):
                        terminal_text = ""
                    if terminal_text:
                        addition = terminal_text if not full_text else "\n\n" + terminal_text
                        full_text += addition
                        yield _emit({"text": addition, "recovered": True})

            elif t == "heartbeat":
                yield _emit({"pulse": "wait", "idle_ms": ev.get("idle_ms", 0)})

        report_files_after = _skill_report_snapshot(skill_name)
        changed_reports = _changed_report_files(report_files_before, report_files_after)
        delivery_reports = _prioritize_report_files(changed_reports)
        if skill_name and not _REPORT_DELIVERY_RE.search(full_text):
            if delivery_reports:
                filename = delivery_reports[0]
                delivery_note = f"\n\n## 报告已交付\n\n完整报告：reports/{filename}"
            else:
                reason = _clean_object_artifacts(stream_error) or "Agent 在形成最终结论前结束"
                delivery_note = (
                    f"\n\n> **研究未完成**：{reason}。已保留研究过程，可在本对话中发送“继续完成报告”恢复。"
                )
            full_text += delivery_note
            yield _emit({"text": delivery_note, "delivery": True})

        elapsed = time.time() - t0
        log.info(f"[{user}] DONE {elapsed:.1f}s len={len(full_text)} refs={len(collected_refs)}")

        report_text, report_status = _prepare_chat_report(full_text, skill_name)

        if conv_id and full_text.strip():
            try:
                from .reports import save_report_snapshot

                clean_text = " ".join(report_text.split())
                title = question[:80] + ("..." if len(question) > 80 else "")
                save_report_snapshot(
                    user,
                    {
                        "source": "chat",
                        "source_id": conv_id,
                        "conv_id": conv_id,
                        "title": title or "Chat report",
                        "summary": (
                            clean_text[:240]
                            if report_status == "complete"
                            else "研究过程已保存，但任务在形成最终报告前中止。"
                        ),
                        "report": {
                            "question": question,
                            "final_decision": report_text,
                            "status": report_status,
                            "model": model_id,
                            "skill": skill_name,
                            "references": collected_refs,
                        },
                    },
                )
            except Exception as e:
                log.warning(f"[{user}] Failed to save chat report snapshot: {e}")

        # Save structured messages from JSONL
        if conv_id and session_id and project_dir:
            with get_db() as db:
                db.execute(
                    "UPDATE conversations SET session_id=?, project_dir=? WHERE id=?",
                    [session_id, project_dir, conv_id],
                )
            jsonl_msgs = await _read_jsonl_messages(session_id, project_dir)
            assistant_indices = [i for i, item in enumerate(jsonl_msgs) if item.get("role") == "assistant"]
            if delivery_note and assistant_indices:
                last_index = assistant_indices[-1]
                parsed = json.loads(jsonl_msgs[last_index]["content"])
                parsed.setdefault("chunks", []).append({"t": "text", "c": delivery_note})
                parsed["text"] = (parsed.get("text") or "") + delivery_note
                jsonl_msgs[last_index]["content"] = json.dumps(parsed, ensure_ascii=False)
            with get_db() as db:
                for message_index, m in enumerate(jsonl_msgs):
                    if m["role"] == "assistant":
                        parsed = json.loads(m["content"])
                        txt_parts = [c["c"] for c in parsed.get("chunks", []) if c["t"] == "text"]
                        full = "".join(txt_parts)
                        payload = json.dumps(
                            {
                                "text": full,
                                "chunks": parsed.get("chunks", []),
                                "report_status": report_status
                                if message_index == assistant_indices[-1]
                                else "",
                                "report_files": delivery_reports[:5]
                                if message_index == assistant_indices[-1]
                                else [],
                            },
                            ensure_ascii=False,
                        )
                        db.execute(
                            "INSERT INTO messages (conv_id, role, content) VALUES (?,?,?)",
                            [conv_id, "assistant", payload],
                        )
                if not assistant_indices and full_text.strip():
                    payload = json.dumps(
                        {
                            "text": full_text,
                            "chunks": [{"t": "text", "c": full_text}],
                            "report_status": report_status,
                            "report_files": delivery_reports[:5],
                        },
                        ensure_ascii=False,
                    )
                    db.execute(
                        "INSERT INTO messages (conv_id, role, content) VALUES (?,?,?)",
                        [conv_id, "assistant", payload],
                    )
            log.info(f"[{user}] Saved {len(jsonl_msgs)} messages + {len(collected_refs)} refs from JSONL")

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

    return StreamingResponse(generate(), media_type="text/event-stream")


async def chat_respond(request: Request):
    """POST /api/chat/respond — Send user's answer to a running AskUserQuestion."""
    from .models import ChatRespondRequest, validation_error_message

    require_auth(request)
    body = await request.json()
    try:
        req = ChatRespondRequest(**(body or {}))
    except Exception as e:
        return JSONResponse({"error": validation_error_message(e)}, status_code=422)
    conv_id = req.conv_id
    answer = req.answer.strip()
    if not conv_id or not answer:
        return JSONResponse({"error": "缺少 conv_id 或 answer"}, status_code=400)

    ok = await respond_to_question(conv_id, answer)
    if ok:
        return {"ok": True, "message": "回答已发送"}
    else:
        return JSONResponse({"error": "没有正在运行的对话进程"}, status_code=404)


async def chat_cancel(request: Request):
    """POST /api/chat/cancel — Cancel a running Claude process."""
    from .models import ChatCancelRequest, validation_error_message

    require_auth(request)
    body = await request.json()
    try:
        req = ChatCancelRequest(**(body or {}))
    except Exception as e:
        return JSONResponse({"error": validation_error_message(e)}, status_code=422)
    conv_id = req.conv_id
    if not conv_id:
        return JSONResponse({"error": "缺少 conv_id"}, status_code=400)

    ok = await cancel_process(conv_id)
    return {"ok": ok}
