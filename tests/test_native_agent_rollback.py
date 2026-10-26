"""Synthetic SQLite/JSONL compatibility with the actual public c997 UI helpers.

The frozen functions below are copied unchanged from c997bcff, avoiding network
or Git-history requirements in shallow CI checkouts. No legacy model/CLI runs.
"""

import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from server import conversations
from server.agent import runtime, store
from server.database import get_db
from server.research_context import parse_claude_jsonl_events

C997_CONVERSATIONS_SHA256 = "998215c6b1ff2f999c1c5236c642527c407045656ff5a9e1cf2f987c003bf796"
C997_HELPERS_SHA256 = "5ae1325fe351f5c99394f16de9074472db886edd0c76dd26e6345f89ab59b90e"
C997_HELPERS = r'''
def _read_jsonl_messages(session_id: str, project_dir: str) -> list[dict]:
    """Read historical turns with the same provenance rules as live chat."""
    jsonl_path = CLAUDE_PROJECTS / project_dir / f"{session_id}.jsonl"
    if not jsonl_path.exists():
        return []
    events = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return parse_claude_jsonl_events(events, session_id=session_id)

def _merge_saved_turns(history: list[dict], stored: list[dict]) -> list[dict]:
    """Retain the legacy JSONL prefix; modern DB turns own their research metadata."""
    anchor = None
    for item in stored:
        if item["role"] != "assistant":
            continue
        try:
            payload = json.loads(item["content"])
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("research") and payload.get("history_anchor"):
            anchor = payload["history_anchor"]
            break
    if anchor is None:
        return history or [{"role": m["role"], "content": m["content"]} for m in stored]
    boundary = int(anchor.get("db_message_id") or 0)
    prefix = []
    if history:
        users = 0
        for item in history:
            users += item["role"] == "user"
            if users > int(anchor.get("jsonl_user_turns") or 0):
                break
            prefix.append(item)
    else:
        prefix = [{"role": m["role"], "content": m["content"]} for m in stored if m["id"] <= boundary]
    return prefix + [{"role": m["role"], "content": m["content"]} for m in stored if m["id"] > boundary]

def permanent_delete_conv(cid: str, request: Request) -> dict:
    """DELETE /api/convs/{cid}/permanent — permanently delete conversation and messages."""
    u = require_auth(request)
    with get_db() as db:
        row = db.execute(
            "SELECT deleted_at FROM conversations WHERE id=? AND username=?", [cid, u]
        ).fetchone()
        if not row:
            raise HTTPException(404, "对话不存在")
        if row["deleted_at"] is None:
            raise HTTPException(409, "请先将对话移入回收站")
        db.execute("DELETE FROM conversations WHERE id=? AND username=?", [cid, u])
        db.execute("DELETE FROM messages WHERE conv_id=?", [cid])
    return {"ok": True}
'''


def old_helpers(root):
    raw = C997_HELPERS.removeprefix("\n")
    assert hashlib.sha256(raw.encode()).hexdigest() == C997_HELPERS_SHA256
    namespace = {
        "json": json,
        "CLAUDE_PROJECTS": root,
        "parse_claude_jsonl_events": parse_claude_jsonl_events,
        "get_db": get_db,
        "HTTPException": HTTPException,
        "Request": object,
        "require_auth": lambda _request: "alice",
    }
    exec(compile(raw, "<frozen-public-c997-ui-helpers>", "exec"), namespace)
    return SimpleNamespace(**namespace)


@pytest.fixture
def owners():
    with get_db() as db:
        for user in ("alice", "bob"):
            db.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", [user, "synthetic"])
            db.execute(
                "INSERT INTO conversations(id,username,title) VALUES(?,?,?)",
                [user + "-conv", user, "existing synthetic title"],
            )


def new_run(user="alice"):
    return store.create_run(user, user + "-conv", "native question", "synthetic-model", "research")


def legacy_history(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    project = root / "synthetic-project"
    project.mkdir(parents=True)
    events = [
        {"type": "user", "message": {"role": "user", "content": "old question one"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "SYNTHETIC_PRIVATE_JSONL_REASONING"},
                    {"type": "text", "text": "old public answer one"},
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "unknown-old-tool", "content": "old tool"}]
            },
        },
        {"type": "user", "message": {"role": "user", "content": "old question two"}},
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "old public answer two"}]},
        },
    ]
    path = project / "synthetic-session.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    monkeypatch.setattr(conversations, "CLAUDE_PROJECTS", root)
    return root, path


def test_native_schema_and_delete_trigger_are_one_atomic_migration(owners):
    with get_db() as db:

        def deny_trigger(action, *_args):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_TRIGGER else sqlite3.SQLITE_OK

        db.set_authorizer(deny_trigger)
        try:
            with pytest.raises(sqlite3.DatabaseError):
                store.init_agent_db()
        finally:
            db.set_authorizer(None)
        assert not db.in_transaction
        assert (
            db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'native_agent_%'").fetchone()[0]
            == 0
        )
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
    store.init_agent_db()
    with get_db() as db:
        assert {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'native_agent_%'")
        } == {
            "native_agent_runs",
            "native_agent_events",
            "native_agent_dialogues",
            "native_agent_conversation_delete",
        }


def test_schema_init_does_not_commit_a_callers_transaction(owners):
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO users(username,password_hash) VALUES('not-committed','synthetic')")
        with pytest.raises(RuntimeError):
            store.init_agent_db()
        assert db.in_transaction
        db.execute("ROLLBACK")
        assert db.execute("SELECT 1 FROM users WHERE username='not-committed'").fetchone() is None


def test_real_old_delete_handler_also_cleans_native_records_after_rollback(owners, tmp_path):
    alice, bob = new_run(), new_run("bob")
    runtime.ensure_dialogue("alice", alice)
    runtime.ensure_dialogue("bob", bob)
    store.append_event("alice", alice["id"], {"type": "text_delta", "text": "public"})
    with get_db() as db:
        db.execute("UPDATE conversations SET deleted_at=1 WHERE id='alice-conv'")
    old = old_helpers(tmp_path)
    assert old.permanent_delete_conv("alice-conv", None) == {"ok": True}
    with get_db() as db:
        for table in ("native_agent_runs", "native_agent_events", "native_agent_dialogues"):
            assert (
                db.execute(f"SELECT COUNT(*) FROM {table} WHERE run_id=?", [alice["id"]]).fetchone()[0] == 0
            )
            assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE run_id=?", [bob["id"]]).fetchone()[0] > 0
        assert db.execute("SELECT COUNT(*) FROM messages WHERE conv_id='alice-conv'").fetchone()[0] == 0
    runtime.init_runtime_db()
    assert store.get_run("bob", bob["id"])["status"] == "queued"


def test_actual_old_jsonl_reader_and_merge_preserve_prefix_and_new_sql_turns(owners, tmp_path, monkeypatch):
    root, _path = legacy_history(tmp_path, monkeypatch)
    with get_db() as db:
        db.execute(
            "UPDATE conversations SET session_id='synthetic-session',project_dir='synthetic-project' WHERE id='alice-conv'"
        )
        # The old SQL mirror may contain fewer turns than its JSONL history.
        db.execute(
            "INSERT INTO messages(conv_id,role,content) VALUES('alice-conv','user','old question one')"
        )
        db.execute(
            "INSERT INTO messages(conv_id,role,content) VALUES('alice-conv','assistant','old public answer one')"
        )
    run = new_run()
    anchor = runtime.ensure_dialogue("alice", run)
    assert anchor["jsonl_user_turns"] == 2 and anchor["native_db_takeover"] is True
    payload = {"text": "native public answer", "research": {"run_id": run["id"]}, "history_anchor": anchor}
    with get_db() as db:
        db.execute(
            "INSERT INTO messages(conv_id,role,content) VALUES('alice-conv','assistant',?)",
            [json.dumps(payload)],
        )
        stored = [
            dict(row)
            for row in db.execute(
                "SELECT id,role,content FROM messages WHERE conv_id='alice-conv' ORDER BY id"
            )
        ]
    old = old_helpers(root)
    history = old._read_jsonl_messages("synthetic-session", "synthetic-project")
    merged = old._merge_saved_turns(history, stored)
    assert len(history) == 4 and merged[:4] == history and len(merged) == 6
    assert merged[-2]["content"] == "native question"
    assert "native public answer" in merged[-1]["content"]
    # Reading only UI count metadata cannot insert JSONL reasoning into model input.
    model_history = runtime._history("alice", "alice-conv", anchor["db_message_id"])
    assert "SYNTHETIC_PRIVATE_JSONL_REASONING" not in json.dumps(model_history)
    assert len(model_history) == 2


@pytest.mark.parametrize(
    "session,project",
    [
        ("../escape", "synthetic-project"),
        ("synthetic-session", "../escape"),
        ("synthetic-session", "/absolute"),
        (None, "synthetic-project"),
    ],
)
def test_legacy_counter_refuses_invalid_paths_without_open(session, project, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(runtime.os, "open", lambda *_a, **_k: pytest.fail("unsafe path opened"))
        assert runtime._legacy_prefix_count(session, project) is None


def test_legacy_counter_does_not_follow_project_or_file_symlinks(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "session.jsonl").write_text('{"type":"user","message":{"content":"private"}}\n')
    monkeypatch.setattr(conversations, "CLAUDE_PROJECTS", root)
    (root / "project").symlink_to(outside, target_is_directory=True)
    assert runtime._legacy_prefix_count("session", "project") is None
    (root / "project").unlink()
    (root / "project").mkdir()
    (root / "project" / "session.jsonl").symlink_to(outside / "session.jsonl")
    assert runtime._legacy_prefix_count("session", "project") is None


def test_unknown_legacy_count_cannot_be_faked_as_zero_or_append_a_user(owners, tmp_path, monkeypatch):
    monkeypatch.setattr(conversations, "CLAUDE_PROJECTS", tmp_path / "missing")
    with get_db() as db:
        db.execute(
            "UPDATE conversations SET session_id='missing-session',project_dir='missing-project' WHERE id='alice-conv'"
        )
    run = new_run()
    with pytest.raises(HTTPException) as error:
        runtime.ensure_dialogue("alice", run)
    assert error.value.status_code == 409
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM native_agent_dialogues").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_existing_anchor_is_not_rewritten_and_no_legacy_reference_is_known_zero(owners):
    run = new_run()
    first = runtime.ensure_dialogue("alice", run)
    assert first["jsonl_user_turns"] == 0
    assert runtime.ensure_dialogue("alice", run) == first
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM messages WHERE conv_id='alice-conv'").fetchone()[0] == 1


def test_failed_unclaimed_initialization_releases_admission_quota_atomically(owners):
    run = store.create_run(
        "alice", "alice-conv", "question", "synthetic", "research", exclusive_conversation=True
    )
    failed = store.fail_unclaimed_run("alice", run["id"], "legacy_history_unavailable")
    assert failed["status"] == "failed" and failed["error_code"] == "legacy_history_unavailable"
    assert store.list_events("alice", run["id"])[-1]["type"] == "run_finished"
    assert store.fail_unclaimed_run("alice", run["id"]) == failed
    replacement = store.create_run(
        "alice", "alice-conv", "retry", "synthetic", "research", exclusive_conversation=True
    )
    assert replacement["status"] == "queued"


def test_failed_initialization_cannot_overwrite_claimed_or_cancelled_execution(owners):
    run = new_run()
    assert store.claim_run("alice", run["id"], "real-owned-worker")
    before = store.get_run("alice", run["id"])
    assert store.fail_unclaimed_run("alice", run["id"]) == before
    queued = new_run()
    store.request_cancel("alice", queued["id"])
    assert store.fail_unclaimed_run("alice", queued["id"])["status"] == "cancelled"
