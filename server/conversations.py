"""Conversation CRUD and trash backed exclusively by platform-owned SQLite."""

import json
import re
import secrets

from fastapi import HTTPException, Request

from .agent.public_text import public_answer_text
from .auth import require_auth
from .database import get_db
from .legacy_history import init_legacy_history_db, migration_status, read_legacy_history
from .source_urls import source_display_payload


def _clean_object_artifacts(text: str) -> str:
    """Clean public display prose, including pre-native formatting artifacts."""
    if not text:
        return ""
    cleaned = public_answer_text(str(text)).replace("[object Object]", "")
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _content_to_text(value) -> str:
    """Project recognized public prose without flattening private blocks."""
    if value is None:
        return ""
    if isinstance(value, str):
        return _clean_object_artifacts(value)
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(part for part in (_content_to_text(v) for v in value) if part)
    if isinstance(value, dict):
        return public_answer_text(value)
    return _clean_object_artifacts(str(value))


def _sanitize_json_value(value):
    """Clean stored JSON message payloads without changing their structure."""
    if value is None:
        return None
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
        kind = value.get("t", value.get("type", ""))
        if kind in {
            "think",
            "thinking",
            "reasoning",
            "analysis",
            "tool",
            "tool_use",
            "tool_result",
            "server_tool_use",
        }:
            return None
        private = {
            "thinking",
            "reasoning",
            "reasoningcontent",
            "chainofthought",
            "signature",
            "encryptedcontent",
            "apikey",
            "authorization",
            "authentication",
            "password",
            "passwd",
            "secret",
            "credentials",
            "cookie",
            "cookies",
            "token",
            "accesstoken",
            "refreshtoken",
            "headers",
            "env",
            "environment",
            "raw",
            "rawrequest",
            "rawresponse",
            "rawframe",
            "rawframes",
            "stdout",
            "stderr",
            "toolinput",
            "tooloutput",
        }
        cleaned = {
            str(k): _sanitize_json_value(v)
            for k, v in value.items()
            if re.sub(r"[^a-z0-9]", "", str(k).casefold()) not in private
        }
        if cleaned.get("t") == "text" and not str(cleaned.get("c", "")).strip():
            return None
        return cleaned
    return _clean_object_artifacts(str(value))


def _sanitize_stored_content(content: str) -> str:
    """Clean legacy DB messages that predate renderer-side artifact stripping."""
    if not isinstance(content, str):
        return _content_to_text(content)
    if not content.lstrip().startswith("{"):
        return _clean_object_artifacts(content)
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return _clean_object_artifacts(content)
    cleaned = _sanitize_json_value(payload)
    return json.dumps(cleaned, ensure_ascii=False)


def _merge_saved_turns(history: list[dict], stored: list[dict]) -> list[dict]:
    """Merge an imported SQLite prefix with saved turns using existing anchors."""
    anchor = None
    for item in stored:
        if item["role"] != "assistant":
            continue
        try:
            payload = json.loads(item["content"])
        except (TypeError, ValueError):
            continue
        if (
            isinstance(payload, dict)
            and (payload.get("research") or payload.get("native_agent"))
            and isinstance(payload.get("history_anchor"), dict)
        ):
            anchor = payload["history_anchor"]
            break
    if anchor is None:
        return history or [{"role": m["role"], "content": m["content"]} for m in stored]
    try:
        boundary = max(0, int(anchor.get("db_message_id") or 0))
        legacy_turns = max(0, int(anchor.get("jsonl_user_turns") or 0))
    except (TypeError, ValueError, OverflowError):
        # An invalid old display anchor cannot discard either stored source.
        return history + [{"role": m["role"], "content": m["content"]} for m in stored]
    if anchor.get("native_db_takeover") is True:
        # Imported legacy turns are an immutable prefix. Native turns live in
        # platform SQL, and existing message boundaries remain authoritative.
        prefix = history or [
            {"role": m["role"], "content": m["content"]} for m in stored if m["id"] <= boundary
        ]
        return prefix + [{"role": m["role"], "content": m["content"]} for m in stored if m["id"] > boundary]
    prefix = []
    if history:
        users = 0
        for item in history:
            users += item["role"] == "user"
            if users > legacy_turns:
                break
            prefix.append(item)
    else:
        prefix = [{"role": m["role"], "content": m["content"]} for m in stored if m["id"] <= boundary]
    return prefix + [{"role": m["role"], "content": m["content"]} for m in stored if m["id"] > boundary]


# ═══════════════════════════════════════
# Conversation CRUD
# ═══════════════════════════════════════


def list_convs(request: Request) -> list[dict]:
    """GET /api/convs — list owned active conversations from platform storage."""
    u = require_auth(request)
    init_legacy_history_db()
    with get_db() as db:
        rows = db.execute(
            "SELECT id,title,skill,session_id,project_dir FROM conversations "
            "WHERE username=? AND deleted_at IS NULL ORDER BY created_at DESC",
            [u],
        ).fetchall()
        result = []
        for row in rows:
            imported = read_legacy_history(db, row["id"], u)
            result.append(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "skill": row["skill"],
                    "history_migration": migration_status(row, imported),
                }
            )
    return source_display_payload(result)


def get_conv(cid: str, request: Request) -> dict:
    """GET /api/convs/{cid} — get a single conversation with messages."""
    u = require_auth(request)
    init_legacy_history_db()
    with get_db() as db:
        conv = db.execute(
            "SELECT * FROM conversations WHERE id=? AND username=? AND deleted_at IS NULL", [cid, u]
        ).fetchone()
        if not conv:
            raise HTTPException(404)

        imported = read_legacy_history(db, cid, u)
        messages = imported["messages"] if imported is not None else []

        msgs = db.execute(
            "SELECT id, role, content FROM messages WHERE conv_id=? ORDER BY id", [cid]
        ).fetchall()
        stored = [
            {"id": m["id"], "role": m["role"], "content": _sanitize_stored_content(m["content"])}
            for m in msgs
        ]
        # A browser refresh can show persisted progress even before the final
        # assistant message exists, including a run interrupted by a restart.
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='native_agent_dialogues'").fetchone():
            pending = db.execute(
                "SELECT d.*,r.status FROM native_agent_dialogues d JOIN native_agent_runs r ON r.run_id=d.run_id "
                "WHERE d.conv_id=? AND d.username=? AND d.assistant_message_id IS NULL ORDER BY d.user_message_id",
                [cid, u],
            ).fetchall()
            for run in pending:
                chunks, text, refs, research = [], "", [], None
                for event in db.execute(
                    "SELECT payload FROM native_agent_events WHERE run_id=? ORDER BY seq LIMIT 4000",
                    [run["run_id"]],
                ):
                    item = json.loads(event[0]).get("ui", {})
                    if item.get("text"):
                        text += item["text"]
                        chunks.append({"t": "text", "c": item["text"]})
                    if item.get("ref"):
                        refs.append(item["ref"])
                        chunks.append({"t": "ref", **item["ref"]})
                    if item.get("status"):
                        chunks.append({"t": "status", "c": item["status"]})
                    if item.get("research"):
                        research = item["research"]
                    if item.get("platform_call"):
                        chunks.append({"t": "platform_call", "call": item["platform_call"]})
                    if item.get("error"):
                        error_text = ("\n\n" if text else "") + "⚠ " + item["error"]
                        text += error_text
                        chunks.append({"t": "text", "c": error_text})
                payload = {
                    "text": text,
                    "chunks": chunks,
                    "refs": refs,
                    "research": research,
                    "native_agent": {
                        "run_id": run["run_id"],
                        "status": run["status"],
                        "engine": "prism-native",
                    },
                    "history_anchor": json.loads(run["history_anchor_json"]),
                    "report_status": run["status"],
                }
                stored.append(
                    {
                        "id": run["user_message_id"] + 0.5,
                        "role": "assistant",
                        "content": json.dumps(payload, ensure_ascii=False),
                    }
                )
            stored.sort(key=lambda item: item["id"])
        messages = _merge_saved_turns(messages, stored)

    return source_display_payload(
        {
            "id": cid,
            "title": conv["title"],
            "skill": conv["skill"],
            "messages": messages,
            "history_migration": migration_status(conv, imported),
        }
    )


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
    init_legacy_history_db()
    with get_db() as db:
        db.execute("SAVEPOINT delete_conversation")
        try:
            row = db.execute(
                "SELECT deleted_at FROM conversations WHERE id=? AND username=?", [cid, u]
            ).fetchone()
            if not row:
                raise HTTPException(404, "对话不存在")
            if row["deleted_at"] is None:
                raise HTTPException(409, "请先将对话移入回收站")
            # Native event replay and input anchors are conversation data as well.
            # A still-running owner observes the missing conversation and retires;
            # it must never recreate these records after permanent deletion.
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='native_agent_runs'").fetchone():
                db.execute(
                    "DELETE FROM native_agent_events WHERE run_id IN "
                    "(SELECT run_id FROM native_agent_runs WHERE conv_id=? AND username=?)",
                    [cid, u],
                )
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='native_agent_dialogues'").fetchone():
                    db.execute("DELETE FROM native_agent_dialogues WHERE conv_id=? AND username=?", [cid, u])
                db.execute("DELETE FROM native_agent_runs WHERE conv_id=? AND username=?", [cid, u])
            db.execute("DELETE FROM conversations WHERE id=? AND username=?", [cid, u])
            db.execute("DELETE FROM messages WHERE conv_id=?", [cid])
            db.execute("DELETE FROM legacy_conversation_history WHERE conv_id=? AND username=?", [cid, u])
            db.execute("RELEASE delete_conversation")
        except BaseException:
            db.execute("ROLLBACK TO delete_conversation")
            db.execute("RELEASE delete_conversation")
            raise
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
    return source_display_payload(
        [
            {"id": r["id"], "title": r["title"], "skill": r["skill"], "deleted_at": r["deleted_at"]}
            for r in rows
        ]
    )


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
    skill = req.skill or "prism-research"
    with get_db() as db:
        db.execute(
            "INSERT INTO conversations (id, username, title, skill) VALUES (?,?,?,?)",
            [cid, u, "新对话", skill],
        )
    return {"id": cid}
