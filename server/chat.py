"""聊天流式端点 — Claude Code JSONL 集成 + 双向交互 + 参考引用"""
import json
import time
import logging
import asyncio
import re
from pathlib import Path
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from .config import RATE_CHAT_PER_MIN, MODELS, DEFAULT_MODEL
from .database import get_db, check_rate_limit
from .auth import require_auth
from .services.claude_runner import run_claude_stream, respond_to_question, cancel_process

log = logging.getLogger("stock-app")

# Claude Code stores project data under ~/.claude/projects/
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

# Formatting & language rules are handled by the readable-ai skill (CLAUDE.md)
# loaded by Claude Code — no server-side prompt injection needed.


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
    url_pattern = re.compile(r'\[([^\]]+)\]\((https?://[^)\s]+)\)')
    seen_urls = set()
    for match in url_pattern.finditer(content_text):
        title = match.group(1).strip()
        url = match.group(2).strip()
        # Skip internal anchor links and non-web URLs
        if not url.startswith(('http://', 'https://')):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        # Extract a snippet after the link (up to 120 chars)
        after_pos = match.end()
        snippet = content_text[after_pos:after_pos + 160].strip()
        # Clean up snippet: remove leading dash/bullet
        snippet = re.sub(r'^[\s\-•·–—]+', '', snippet)
        if len(snippet) > 120:
            snippet = snippet[:120] + '…'
        refs.append({"url": url, "title": title, "snippet": snippet})
    return refs


def _read_jsonl_messages(session_id: str, project_dir: str) -> list[dict]:
    """Read Claude Code JSONL, merging consecutive assistant events into single turns.

    Returns list of {role, content} dicts where content is JSON string with {text, chunks, refs}.
    Also captures WebSearch/WebFetch tool results as 'ref' chunks.
    """
    jsonl_path = CLAUDE_PROJECTS / project_dir / f"{session_id}.jsonl"
    if not jsonl_path.exists():
        return []

    messages = []
    collected_refs = []  # Accumulate refs across the conversation

    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
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
                                result_text = str(result_content)
                            # Extract URLs from WebSearch/WebFetch results
                            refs = _extract_refs_from_tool_result(result_text)
                            collected_refs.extend(refs)
                            continue  # Skip tool_result — don't add as user message

                # Skip tool_result injection messages
                content_str = str(content_blocks) if not isinstance(content_blocks, str) else content_blocks
                if content_str and not content_str.startswith("[{'type':") and not content_str.startswith("[{'tool_use_id':"):
                    messages.append({"role": "user", "content": content_str})

            elif t == "assistant":
                blocks = ev.get("message", {}).get("content", [])
                chunks = []
                for b in blocks:
                    bt = b.get("type", "")
                    if bt == "text":
                        chunks.append({"t": "text", "c": b.get("text", "")})
                    elif bt == "tool_use":
                        name = b.get("name", "?")
                        inp = b.get("input", {})
                        detail = str(list(inp.values())[:1]) if inp else ""
                        chunks.append({"t": "tool", "c": name, "d": detail})
                    elif bt == "thinking":
                        chunks.append({"t": "think", "c": b.get("thinking", "")})

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
                    prev.setdefault("chunks", []).append({"t": "ref", "url": ref["url"], "title": ref["title"], "snippet": ref.get("snippet", "")})
                messages[i]["content"] = json.dumps(prev, ensure_ascii=False)
                break

    return messages


async def chat_stream(request: Request):
    """POST /api/chat/stream — Claude Code CLI streaming with SSE + reference citations."""
    user = require_auth(request)
    if not check_rate_limit(f"chat:{user}", RATE_CHAT_PER_MIN, 60):
        return JSONResponse({"error": "请求太频繁"}, status_code=429)

    body = await request.json()
    question = body.get("question", "").strip()
    skill_name = body.get("skill", "bottleneck-hunter")
    extra_skills = body.get("extra_skills", None)  # Additional skills via --append-system-prompt
    conv_id = body.get("conv_id", "")
    model_id = body.get("model", DEFAULT_MODEL)
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
                "SELECT session_id, project_dir FROM conversations WHERE id=? AND username=?",
                [conv_id, user]
            ).fetchone()
            if row and row["session_id"]:
                resume_session_id = row["session_id"]

    # Save user message
    if conv_id:
        with get_db() as db:
            db.execute("INSERT INTO messages (conv_id, role, content) VALUES (?,?,?)",
                       [conv_id, "user", question])
            db.execute("UPDATE conversations SET title=? WHERE id=? AND title='新对话'",
                       [question[:40], conv_id])

    async def generate():
        session_id = resume_session_id  # May be None for first message
        project_dir = None
        full_text = ""
        step_num = 0
        collected_refs = []  # References from WebSearch/WebFetch tool results
        seen_ref_urls = set()

        yield f"data: {json.dumps({'pulse': 'start'}, ensure_ascii=False)}\n\n"

        async for ev in run_claude_stream(
            question, skill_name, user=user, model_cfg=model_cfg,
            session_id=resume_session_id, conv_id=conv_id,
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
                                result_text = str(result_content)
                            # Extract URLs and send as reference citations
                            refs = _extract_refs_from_tool_result(result_text)
                            for ref in refs:
                                if ref["url"] not in seen_ref_urls:
                                    seen_ref_urls.add(ref["url"])
                                    collected_refs.append(ref)
                                    yield f"data: {json.dumps({'ref': ref}, ensure_ascii=False)}\n\n"

            # tool_use event (top-level)
            if t == "tool_use":
                tool = ev.get("tool", {})
                name = tool.get("name", "?")
                inp = tool.get("input", {})
                step_num += 1
                if name == "AskUserQuestion":
                    yield f"data: {json.dumps({'tool': name, 'n': step_num, 'ask': inp.get('question', ''), 'opts': inp.get('options', [])}, ensure_ascii=False)}\n\n"
                else:
                    detail = str(list(inp.values())[:1]) if inp else ""
                    yield f"data: {json.dumps({'tool': name, 'n': step_num, 'd': detail}, ensure_ascii=False)}\n\n"

            # assistant message
            elif t == "assistant":
                for b in ev.get("message", {}).get("content", []):
                    bt = b.get("type", "")
                    if bt == "text":
                        txt = b["text"]
                        full_text += txt
                        yield f"data: {json.dumps({'text': txt}, ensure_ascii=False)}\n\n"
                    elif bt == "tool_use":
                        step_num += 1
                        name = b.get("name", "?")
                        inp = b.get("input", {})
                        if name == "AskUserQuestion":
                            yield f"data: {json.dumps({'tool': name, 'n': step_num, 'ask': inp.get('question', ''), 'opts': inp.get('options', [])}, ensure_ascii=False)}\n\n"
                        else:
                            detail = str(list(inp.values())[:1]) if inp else ""
                            yield f"data: {json.dumps({'tool': name, 'n': step_num, 'd': detail}, ensure_ascii=False)}\n\n"
                    elif bt == "thinking":
                        yield f"data: {json.dumps({'think': b.get('thinking', '')}, ensure_ascii=False)}\n\n"

            # Error from Claude
            elif t == "error":
                log.error(f"[{user}] Claude error: {ev.get('message', '')}")
                yield f"data: {json.dumps({'error': ev.get('message', 'AI引擎异常')}, ensure_ascii=False)}\n\n"

            elif t == "result":
                log.info(f"[{user}] Claude done: {ev.get('duration_ms', 0)}ms stop={ev.get('stop_reason', '')}")

        yield "data: [DONE]\n\n"
        elapsed = time.time() - t0
        log.info(f"[{user}] DONE {elapsed:.1f}s len={len(full_text)} refs={len(collected_refs)}")

        # Save structured messages from JSONL
        if conv_id and session_id and project_dir:
            with get_db() as db:
                db.execute("UPDATE conversations SET session_id=?, project_dir=? WHERE id=?",
                           [session_id, project_dir, conv_id])
            jsonl_msgs = _read_jsonl_messages(session_id, project_dir)
            with get_db() as db:
                for m in jsonl_msgs:
                    if m["role"] == "assistant":
                        parsed = json.loads(m["content"])
                        txt_parts = [c["c"] for c in parsed.get("chunks", []) if c["t"] == "text"]
                        full = "".join(txt_parts)
                        payload = json.dumps({
                            "text": full,
                            "chunks": parsed.get("chunks", []),
                        }, ensure_ascii=False)
                        db.execute("INSERT INTO messages (conv_id, role, content) VALUES (?,?,?)",
                                   [conv_id, "assistant", payload])
            log.info(f"[{user}] Saved {len(jsonl_msgs)} messages + {len(collected_refs)} refs from JSONL")

    return StreamingResponse(generate(), media_type="text/event-stream")


async def chat_respond(request: Request):
    """POST /api/chat/respond — Send user's answer to a running AskUserQuestion."""
    user = require_auth(request)
    body = await request.json()
    conv_id = body.get("conv_id", "")
    answer = body.get("answer", "").strip()
    if not conv_id or not answer:
        return JSONResponse({"error": "缺少 conv_id 或 answer"}, status_code=400)

    ok = await respond_to_question(conv_id, answer)
    if ok:
        return {"ok": True, "message": "回答已发送"}
    else:
        return JSONResponse({"error": "没有正在运行的对话进程"}, status_code=404)


async def chat_cancel(request: Request):
    """POST /api/chat/cancel — Cancel a running Claude process."""
    user = require_auth(request)
    body = await request.json()
    conv_id = body.get("conv_id", "")
    if not conv_id:
        return JSONResponse({"error": "缺少 conv_id"}, status_code=400)

    ok = await cancel_process(conv_id)
    return {"ok": ok}
