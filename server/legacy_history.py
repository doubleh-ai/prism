"""Platform-owned public legacy history and an explicit, offline-only importer.

Serving history never opens an external project directory. The importer accepts
only operator-staged inputs and does not import application configuration, model
clients, executable skills, or settings from the historical directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from pathlib import Path

from .agent.public_text import public_answer_text
from .research_context import parse_claude_jsonl_events

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_LINE_BYTES = 512 * 1024
MAX_LINES = 20000
MAX_CONVERSATIONS = 10000
MAX_TOTAL_BYTES = 256 * 1024 * 1024
_PROJECT = re.compile(r"[A-Za-z0-9_.-]{1,240}\Z")
_SESSION = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS legacy_conversation_history (
        conv_id TEXT NOT NULL, username TEXT NOT NULL,
        public_messages_json TEXT NOT NULL, user_turn_count INTEGER NOT NULL CHECK(user_turn_count>=0),
        source_sha256 TEXT NOT NULL, source_project_dir TEXT NOT NULL, source_session_id TEXT NOT NULL,
        imported_at INTEGER NOT NULL, PRIMARY KEY(conv_id,username)
    )""",
    """CREATE TRIGGER IF NOT EXISTS legacy_history_conversation_delete
    AFTER DELETE ON conversations BEGIN
        DELETE FROM legacy_conversation_history WHERE conv_id=OLD.id AND username=OLD.username;
    END""",
    """CREATE TRIGGER IF NOT EXISTS legacy_history_account_delete
    AFTER DELETE ON users BEGIN
        DELETE FROM legacy_conversation_history WHERE username=OLD.username;
    END""",
)


class HistoryImportError(ValueError):
    """A staged history import is incomplete, ambiguous, or changed."""


def init_legacy_history_db(db=None):
    """Add one table and two cleanup triggers without committing caller work."""
    if db is None:
        from .database import get_db

        with get_db() as connection:
            init_legacy_history_db(connection)
        return
    db.execute("SAVEPOINT legacy_history_schema")
    try:
        for statement in _SCHEMA:
            db.execute(statement)
        db.execute("RELEASE legacy_history_schema")
    except BaseException:
        db.execute("ROLLBACK TO legacy_history_schema")
        db.execute("RELEASE legacy_history_schema")
        raise


def read_legacy_history(db, conv_id: str, username: str) -> dict | None:
    """Read an owned, imported prefix; never consult external history storage."""
    row = db.execute(
        "SELECT h.* FROM legacy_conversation_history h JOIN conversations c "
        "ON c.id=h.conv_id AND c.username=h.username JOIN users u ON u.username=c.username "
        "WHERE h.conv_id=? AND h.username=? AND u.disabled_at IS NULL "
        "AND h.source_project_dir=COALESCE(c.project_dir,'') "
        "AND h.source_session_id=COALESCE(c.session_id,'')",
        [conv_id, username],
    ).fetchone()
    if row is None:
        return None
    payload = dict(row)
    try:
        messages = json.loads(payload.pop("public_messages_json"))
        if not isinstance(messages, list) or len(messages) > MAX_LINES:
            return None
        if any(
            not isinstance(item, dict)
            or set(item) != {"role", "content"}
            or item["role"] not in {"user", "assistant"}
            or not isinstance(item["content"], str)
            for item in messages
        ):
            return None
        if sum(item["role"] == "user" for item in messages) != payload["user_turn_count"]:
            return None
    except (TypeError, ValueError, RecursionError):
        return None
    payload["messages"] = messages
    return payload


def migration_status(conversation, imported: dict | None) -> dict:
    if imported is not None:
        return {"status": "imported", "user_turn_count": imported["user_turn_count"]}
    if conversation["session_id"] or conversation["project_dir"]:
        return {
            "status": "pending_import",
            "message": "旧对话历史等待管理员迁入平台存储；当前展示已保存的平台消息。",
        }
    return {"status": "not_required"}


def _open_directory(path: Path) -> int:
    """Resolve each absolute path component using no-follow directory handles."""
    if not path.is_absolute() or ".." in path.parts:
        raise HistoryImportError("staged root must be an absolute directory")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in path.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_staged_jsonl(root_fd: int, project_dir: str, session_id: str) -> tuple[bytes, list[dict]]:
    if (
        not isinstance(project_dir, str)
        or not _PROJECT.fullmatch(project_dir)
        or project_dir in {".", ".."}
        or not isinstance(session_id, str)
        or not _SESSION.fullmatch(session_id)
    ):
        raise HistoryImportError("invalid historical conversation mapping")
    descriptors = []
    try:
        project = os.open(
            project_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root_fd
        )
        descriptors.append(project)
        filename = session_id + ".jsonl"
        fd = os.open(filename, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=project)
        descriptors.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_FILE_BYTES:
            raise HistoryImportError("historical file must be bounded, regular and single-link")
        chunks, remaining = [], before.st_size + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        named = os.stat(filename, dir_fd=project, follow_symlinks=False)
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
        if len(raw) != before.st_size or any(
            getattr(before, field) != getattr(after, field) or getattr(before, field) != getattr(named, field)
            for field in fields
        ):
            raise HistoryImportError("historical file changed during import")
        lines = raw.splitlines()
        if len(lines) > MAX_LINES or any(len(line) > MAX_LINE_BYTES for line in lines):
            raise HistoryImportError("historical JSONL exceeds parser bounds")
        events = []
        for line in lines:
            if not line.strip():
                continue
            event = json.loads(line.decode("utf-8"))
            if not isinstance(event, dict):
                raise HistoryImportError("historical JSONL must contain objects")
            events.append(event)
        return raw, events
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as exc:
        if isinstance(exc, HistoryImportError):
            raise
        raise HistoryImportError("historical JSONL is missing, malformed or unsafe") from exc
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _public_prefix(events: list[dict], session_id: str) -> list[dict]:
    # Preserve canonical turn boundaries while retaining only public prose.
    # Strip private blocks before the canonical parser can flatten a user block
    # or accumulate tool records. Historical instructions are ordinary data.
    public_events = []
    for event in events:
        message = event.get("message")
        if not isinstance(message, dict):
            if event.get("type") == "user" and isinstance(message, str):
                if message.startswith(("[{'type':", "[{'tool_use_id':")):
                    continue
                try:
                    message = json.loads(message)
                except (TypeError, ValueError):
                    try:
                        message = json.loads(message.replace("'", '"'))
                    except (TypeError, ValueError):
                        message = {"content": message}
                if not isinstance(message, dict):
                    message = {"content": message}
            else:
                continue
        content = message.get("content")
        if isinstance(content, list):
            if event.get("type") == "user" and any(
                isinstance(block, dict) and block.get("type") == "tool_result" for block in content
            ):
                continue
            content = [
                {"type": "text", "text": block["text"]}
                for block in content
                if isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ]
        elif event.get("type") == "assistant":
            # The historical format requires assistant content blocks.
            raise HistoryImportError("historical assistant content is malformed")
        elif not isinstance(content, str):
            continue
        public_events.append({"type": event.get("type"), "message": {"content": content}})
    messages = []
    for item in parse_claude_jsonl_events(public_events, session_id=session_id):
        text = public_answer_text(item["content"]).replace("[object Object]", "").strip()
        if text:
            messages.append({"role": item["role"], "content": text})
    return messages


def import_staged_history(db: sqlite3.Connection, staged_project_root: str | Path, *, username=None) -> dict:
    """Atomically import all mapped conversations into an already-staged DB.

    Existing SQL messages, session metadata and native anchors are untouched.
    Repeating the same source fingerprint is a no-op; any conflict aborts the
    whole import. Ownership is rechecked within the write transaction.
    """
    init_legacy_history_db(db)
    if db.in_transaction:
        raise HistoryImportError("import requires an independent staged-database transaction")
    root_fd = None
    try:
        root_fd = _open_directory(Path(staged_project_root))
        db.execute("BEGIN IMMEDIATE")
        if (
            username is not None
            and not db.execute(
                "SELECT 1 FROM users WHERE username=? AND disabled_at IS NULL", [username]
            ).fetchone()
        ):
            raise HistoryImportError("staged import owner is unavailable")
        rows = db.execute(
            "SELECT c.id,c.username,c.project_dir,c.session_id FROM conversations c "
            "JOIN users u ON u.username=c.username WHERE u.disabled_at IS NULL "
            "AND (COALESCE(c.session_id,'')<>'' OR COALESCE(c.project_dir,'')<>'') "
            + ("AND c.username=? " if username is not None else "")
            + "ORDER BY c.id LIMIT ?",
            ([username] if username is not None else []) + [MAX_CONVERSATIONS + 1],
        ).fetchall()
        if len(rows) > MAX_CONVERSATIONS:
            raise HistoryImportError("too many historical conversations for one staged import")
        inserted = unchanged = total_bytes = 0
        for row in rows:
            conv_id, owner, project_dir, session_id = tuple(row)
            raw, events = _read_staged_jsonl(root_fd, project_dir, session_id)
            total_bytes += len(raw)
            if total_bytes > MAX_TOTAL_BYTES:
                raise HistoryImportError("staged import exceeds total input bound")
            fingerprint = hashlib.sha256(raw).hexdigest()
            try:
                messages = _public_prefix(events, session_id)
            except (TypeError, ValueError, KeyError, RecursionError) as exc:
                raise HistoryImportError("historical public projection is malformed or oversized") from exc
            serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
            turns = sum(item["role"] == "user" for item in messages)
            existing = db.execute(
                "SELECT source_sha256,source_project_dir,source_session_id,public_messages_json,user_turn_count "
                "FROM legacy_conversation_history WHERE conv_id=? AND username=?",
                [conv_id, owner],
            ).fetchone()
            expected = (fingerprint, project_dir, session_id, serialized, turns)
            if existing is not None:
                if tuple(existing) != expected:
                    raise HistoryImportError(
                        "historical import fingerprint conflict; existing prefix retained"
                    )
                unchanged += 1
                continue
            if not db.execute(
                "SELECT 1 FROM conversations c JOIN users u ON u.username=c.username "
                "WHERE c.id=? AND c.username=? AND c.project_dir=? AND c.session_id=? AND u.disabled_at IS NULL",
                [conv_id, owner, project_dir, session_id],
            ).fetchone():
                raise HistoryImportError("conversation ownership or history mapping changed")
            db.execute(
                "INSERT INTO legacy_conversation_history VALUES(?,?,?,?,?,?,?,?)",
                [conv_id, owner, serialized, turns, fingerprint, project_dir, session_id, int(time.time())],
            )
            inserted += 1
        db.execute("COMMIT")
        return {"imported": inserted, "unchanged": unchanged, "input_bytes": total_bytes}
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        if root_fd is not None:
            os.close(root_fd)
