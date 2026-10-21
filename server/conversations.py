"""对话历史 CRUD + 回收站 — Claude Code JSONL 集成"""

import json
import re
import secrets
from pathlib import Path

from fastapi import HTTPException, Request

from .auth import require_auth
from .database import get_db

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


def _clean_object_artifacts(text: str) -> str:
    """Remove JavaScript object-stringification artifacts from Claude CLI output."""
    if not text:
        return ""
    cleaned = str(text).replace("[object Object]", "")
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _content_to_text(value) -> str:
    """Convert Claude content blocks into readable text."""
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


def _sanitize_json_value(value):
    """Clean stored JSON message payloads without changing their structure."""
    if value is None:
        return ""
    if isinstance(value, str):
        return _clean_object_artifacts(value)
    if isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, list):
        cleaned = []
        for item in value:
            val = _sanitize_json_value(item)
            if val is not None:
                cleaned.append(val)
        return cleaned
    if isinstance(value, dict):
        cleaned = {str(k): _sanitize_json_value(v) for k, v in value.items()}
        if cleaned.get("t") in {"think", "text"} and not str(cleaned.get("c", "")).strip():
            return None
        return cleaned
    return _clean_object_artifacts(str(value))


def _sanitize_stored_content(content: str) -> str:
    """Clean legacy DB messages that predate renderer-side artifact stripping."""
    if not isinstance(content, str):
        return _content_to_text(content)
    text = _clean_object_artifacts(content)
    if not text.startswith("{"):
        return text
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return text
    cleaned = _sanitize_json_value(payload)
    return json.dumps(cleaned, ensure_ascii=False)


def _list_jsonl_sessions(project_dir: str) -> list[str]:
    """List session IDs from a project directory, sorted by mtime desc."""
    proj = CLAUDE_PROJECTS / project_dir
    if not proj.exists():
        return []
    files = sorted(proj.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    return [f.stem for f in files]


def _read_jsonl_user_content(jsonl_path: Path) -> str | None:
    """Extract first user message content from JSONL for title."""
    if not jsonl_path.exists():
        return None
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "user":
                msg = ev.get("message", "")
                if isinstance(msg, dict):
                    content = msg.get("content", "")
                else:
                    try:
                        msg_dict = json.loads(str(msg).replace("'", '"'))
                        content = msg_dict.get("content", str(msg))
                    except Exception:
                        content = str(msg)
                if (
                    content
                    and not str(content).startswith("[{'type':")
                    and not str(content).startswith("[{'tool_use_id':")
                ):
                    return _content_to_text(content)
    return None


def _read_jsonl_messages(session_id: str, project_dir: str) -> list[dict]:
    """Read full messages from Claude Code JSONL, merging consecutive assistant events."""
    jsonl_path = CLAUDE_PROJECTS / project_dir / f"{session_id}.jsonl"
    if not jsonl_path.exists():
        return []

    messages = []
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
                    content = msg.get("content", "")
                else:
                    try:
                        msg_dict = json.loads(str(msg).replace("'", '"'))
                        content = msg_dict.get("content", str(msg))
                    except Exception:
                        content = str(msg)
                if (
                    content
                    and not str(content).startswith("[{'type':")
                    and not str(content).startswith("[{'tool_use_id':")
                ):
                    messages.append({"role": "user", "content": _content_to_text(content)})

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
    return messages


# ═══════════════════════════════════════
# Conversation CRUD
# ═══════════════════════════════════════


def list_convs(request: Request) -> list[dict]:
    """GET /api/convs — list active (non-deleted) conversations."""
    u = require_auth(request)
    with get_db() as db:
        rows = db.execute(
            "SELECT id, title, skill, session_id, project_dir, created_at FROM conversations "
            "WHERE username=? AND deleted_at IS NULL ORDER BY created_at DESC",
            [u],
        ).fetchall()

    result = []
    for r in rows:
        c = {"id": r["id"], "title": r["title"], "skill": r["skill"]}
        if r["session_id"] and r["project_dir"]:
            jsonl_path = CLAUDE_PROJECTS / r["project_dir"] / f"{r['session_id']}.jsonl"
            user_content = _read_jsonl_user_content(jsonl_path)
            if user_content:
                c["title"] = user_content[:40]
        result.append(c)
    return result


def get_conv(cid: str, request: Request) -> dict:
    """GET /api/convs/{cid} — get a single conversation with messages."""
    u = require_auth(request)
    with get_db() as db:
        conv = db.execute(
            "SELECT * FROM conversations WHERE id=? AND username=? AND deleted_at IS NULL", [cid, u]
        ).fetchone()
        if not conv:
            raise HTTPException(404)

        messages = []
        if conv["session_id"] and conv["project_dir"]:
            messages = _read_jsonl_messages(conv["session_id"], conv["project_dir"])

        if not messages:
            msgs = db.execute(
                "SELECT role, content FROM messages WHERE conv_id=? ORDER BY id", [cid]
            ).fetchall()
            messages = [{"role": m["role"], "content": _sanitize_stored_content(m["content"])} for m in msgs]

    return {
        "id": cid,
        "title": conv["title"],
        "skill": conv["skill"],
        "messages": messages,
    }


def delete_conv(cid: str, request: Request) -> dict:
    """DELETE /api/convs/{cid} — soft delete (move to trash)."""
    u = require_auth(request)
    import logging
    import time

    log = logging.getLogger("stock-app")
    with get_db() as db:
        # First, check what state the conversation is in
        row = db.execute("SELECT id, deleted_at, username FROM conversations WHERE id=?", [cid]).fetchone()
        if not row:
            log.warning(f"delete_conv: conv {cid} NOT FOUND for user {u}")
            raise HTTPException(404, "对话不存在")
        if row["username"] != u:
            log.warning(f"delete_conv: conv {cid} username mismatch: {row['username']} != {u}")
            raise HTTPException(404, "对话不存在")
        if row["deleted_at"] is not None:
            log.warning(f"delete_conv: conv {cid} already deleted at {row['deleted_at']}")
            raise HTTPException(404, "对话已在回收站中")
        # OK, safe to delete
        cur = db.execute(
            "UPDATE conversations SET deleted_at=? WHERE id=? AND username=? AND deleted_at IS NULL",
            [int(time.time()), cid, u],
        )
        if cur.rowcount == 0:
            log.error(f"delete_conv: conv {cid} rowcount=0 after UPDATE despite passing pre-check")
            raise HTTPException(500, "删除失败，请重试")
        log.info(f"delete_conv: conv {cid} deleted by {u}")
    return {"ok": True}


def restore_conv(cid: str, request: Request) -> dict:
    """POST /api/convs/{cid}/restore — restore from trash."""
    u = require_auth(request)
    with get_db() as db:
        db.execute(
            "UPDATE conversations SET deleted_at=NULL WHERE id=? AND username=? AND deleted_at IS NOT NULL",
            [cid, u],
        )
    return {"ok": True}


def permanent_delete_conv(cid: str, request: Request) -> dict:
    """DELETE /api/convs/{cid}/permanent — permanently delete conversation and messages."""
    u = require_auth(request)
    with get_db() as db:
        db.execute("DELETE FROM conversations WHERE id=? AND username=?", [cid, u])
        db.execute("DELETE FROM messages WHERE conv_id=?", [cid])
    return {"ok": True}


def list_trash(request: Request) -> list[dict]:
    """GET /api/trash — list deleted (trashed) conversations."""
    u = require_auth(request)
    with get_db() as db:
        rows = db.execute(
            "SELECT id, title, skill, deleted_at FROM conversations "
            "WHERE username=? AND deleted_at IS NOT NULL ORDER BY deleted_at DESC",
            [u],
        ).fetchall()
    return [
        {"id": r["id"], "title": r["title"], "skill": r["skill"], "deleted_at": r["deleted_at"]} for r in rows
    ]


async def update_skill(cid: str, request: Request) -> dict:
    """PUT /api/convs/{cid}/skill — update conversation skill."""
    from .models import UpdateSkillRequest, validation_error_message

    u = require_auth(request)
    body = await request.json()
    try:
        req = UpdateSkillRequest(**(body or {}))
    except Exception as e:
        raise HTTPException(422, validation_error_message(e))
    with get_db() as db:
        db.execute("UPDATE conversations SET skill=? WHERE id=? AND username=?", [req.skill, cid, u])
    return {"ok": True}


async def batch_delete_convs(request: Request) -> dict:
    """POST /api/convs/batch-delete — soft delete multiple conversations at once."""
    from .models import BatchDeleteRequest, validation_error_message

    u = require_auth(request)
    import time

    body = await request.json()
    try:
        req = BatchDeleteRequest(**(body or {}))
    except Exception as e:
        raise HTTPException(422, validation_error_message(e))
    ids = req.ids
    now = int(time.time())
    with get_db() as db:
        for cid in ids:
            db.execute(
                "UPDATE conversations SET deleted_at=? WHERE id=? AND username=? AND deleted_at IS NULL",
                [now, cid, u],
            )
    return {"ok": True, "count": len(ids)}


async def create_conv(request: Request) -> dict:
    """POST /api/convs — create new conversation."""
    from .models import CreateConvRequest, validation_error_message

    u = require_auth(request)
    body = await request.json()
    try:
        req = CreateConvRequest(**(body or {}))
    except Exception as e:
        raise HTTPException(422, validation_error_message(e))
    cid = secrets.token_hex(12)
    skill = req.skill or "bottleneck-hunter"
    with get_db() as db:
        db.execute(
            "INSERT INTO conversations (id, username, title, skill) VALUES (?,?,?,?)",
            [cid, u, "新对话", skill],
        )
    return {"id": cid}
