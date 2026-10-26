"""Offline SQLite recovery compatibility; recovery never resumes a legacy CLI.

A compatible recovery reader consumes the same additive SQL prefix and saved
native anchors. The pre-native Claude-backed application is not a recovery
candidate for the native-only runtime.
"""

import json
import sqlite3

import pytest
from fastapi import HTTPException

from server import conversations, legacy_history
from server.agent import runtime, store
from server.database import get_db


@pytest.fixture
def owners(monkeypatch):
    with get_db() as db:
        for owner in ("alice", "bob"):
            db.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", [owner, "synthetic-only"])
        db.execute(
            "INSERT INTO conversations(id,username,session_id,project_dir) VALUES('legacy','alice','session','project')"
        )
        db.execute("INSERT INTO conversations(id,username) VALUES('fresh','alice')")
    runtime.init_runtime_db()
    monkeypatch.setattr(conversations, "require_auth", lambda request: "alice")


def import_prefix(tmp_path):
    root = tmp_path / "staged-projects"
    directory = root / "project"
    directory.mkdir(parents=True)
    events = [
        {"type": "user", "message": {"content": "first historical question"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "private historical reasoning"},
                    {"type": "text", "text": "first historical answer"},
                ]
            },
        },
        {"type": "user", "message": {"content": "second historical question"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "second historical answer"}]}},
    ]
    (directory / "session.jsonl").write_text("\n".join(json.dumps(event) for event in events))
    with get_db() as db:
        legacy_history.import_staged_history(db, root)
    return root


def compatible_sql_reader(db, owner, conv_id):
    """Minimal recovery data reader: explicit owner gate, SQL and public merge."""
    if not db.execute(
        "SELECT 1 FROM conversations c JOIN users u ON u.username=c.username "
        "WHERE c.id=? AND c.username=? AND c.deleted_at IS NULL AND u.disabled_at IS NULL",
        [conv_id, owner],
    ).fetchone():
        return None
    prefix = legacy_history.read_legacy_history(db, conv_id, owner)
    saved = [
        dict(row)
        for row in db.execute("SELECT id,role,content FROM messages WHERE conv_id=? ORDER BY id", [conv_id])
    ]
    return conversations._merge_saved_turns(prefix["messages"] if prefix else [], saved)


def test_native_turns_and_anchors_survive_offline_sql_recovery_copy(owners, tmp_path):
    import_prefix(tmp_path)
    for number in (1, 2):
        run = store.create_run("alice", "legacy", f"native question {number}", "synthetic", "research")
        anchor = runtime.ensure_dialogue("alice", run)
        assert anchor["jsonl_user_turns"] == 2 and anchor["native_db_takeover"] is True
        with get_db() as db:
            cursor = db.execute(
                "INSERT INTO messages(conv_id,role,content) VALUES('legacy','assistant',?)",
                [
                    json.dumps(
                        {
                            "text": f"native answer {number}",
                            "native_agent": {"run_id": run["run_id"]},
                            "history_anchor": anchor,
                        }
                    )
                ],
            )
            db.execute(
                "UPDATE native_agent_dialogues SET assistant_message_id=? WHERE run_id=?",
                [cursor.lastrowid, run["run_id"]],
            )
    with get_db() as db:
        expected = compatible_sql_reader(db, "alice", "legacy")
        anchors = [tuple(row) for row in db.execute("SELECT * FROM native_agent_dialogues ORDER BY run_id")]
        source_messages = [tuple(row) for row in db.execute("SELECT * FROM messages ORDER BY id")]
        recovery = sqlite3.connect(tmp_path / "offline-recovery.db")
        recovery.row_factory = sqlite3.Row
        db.backup(recovery)
    try:
        assert compatible_sql_reader(recovery, "alice", "legacy") == expected
        assert compatible_sql_reader(recovery, "bob", "legacy") is None
        assert [
            tuple(row) for row in recovery.execute("SELECT * FROM messages ORDER BY id")
        ] == source_messages
        assert [
            tuple(row) for row in recovery.execute("SELECT * FROM native_agent_dialogues ORDER BY run_id")
        ] == anchors
        assert len(expected) == 8
        assert "private historical reasoning" not in json.dumps(expected)
    finally:
        recovery.close()


def test_schema_init_is_idempotent_and_additive(owners):
    with get_db() as db:
        db.execute("INSERT INTO messages(conv_id,role,content) VALUES('fresh','user','preserve me')")
        before = [tuple(row) for row in db.execute("SELECT * FROM messages")]
        schema = [
            tuple(row) for row in db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")
        ]
    for _ in range(2):
        runtime.init_runtime_db()
    with get_db() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM messages")] == before
        assert [
            tuple(row) for row in db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")
        ] == schema


def test_schema_helper_never_commits_callers_work(owners):
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO messages(conv_id,role,content) VALUES('fresh','user','uncommitted')")
        with pytest.raises(RuntimeError, match="own transaction"):
            store.init_agent_db()
        legacy_history.init_legacy_history_db(db)
        assert db.in_transaction
        db.execute("ROLLBACK")
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_pending_import_blocks_new_native_turn_and_existing_dialogue(owners):
    run = store.create_run("alice", "legacy", "unknown historical prefix", "synthetic", "research")
    with pytest.raises(HTTPException) as error:
        runtime.ensure_dialogue("alice", run)
    assert error.value.status_code == 409
    with get_db() as db:
        db.execute(
            "INSERT INTO native_agent_dialogues VALUES(?,?,?,?,?,?)",
            [
                run["run_id"],
                "alice",
                "legacy",
                123,
                None,
                json.dumps({"db_message_id": 0, "jsonl_user_turns": 2}),
            ],
        )
    with pytest.raises(HTTPException) as error:
        runtime.ensure_dialogue("alice", run)
    assert error.value.status_code == 409
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_fresh_history_works_without_staged_or_external_project_root(owners):
    run = store.create_run("alice", "fresh", "fully native question", "synthetic", "research")
    first = runtime.ensure_dialogue("alice", run)
    assert first["jsonl_user_turns"] == 0
    assert runtime.ensure_dialogue("alice", run) == first
    assert conversations.get_conv("fresh", None)["messages"][0]["content"] == "fully native question"
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_compatible_recovery_conversation_delete_removes_imported_and_native_data(owners, tmp_path):
    import_prefix(tmp_path)
    run = store.create_run("alice", "legacy", "delete owned historical question", "synthetic", "research")
    runtime.ensure_dialogue("alice", run)
    store.append_event("alice", run["run_id"], {"type": "status", "ui": {"status": "saved progress"}})
    with get_db() as db:
        db.execute("DELETE FROM conversations WHERE id='legacy' AND username='alice'")
        for table in (
            "legacy_conversation_history",
            "native_agent_runs",
            "native_agent_dialogues",
            "native_agent_events",
        ):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    with pytest.raises(HTTPException) as error:
        runtime.ensure_dialogue("alice", run)
    assert error.value.status_code == 404


def test_permanent_delete_is_transactional_on_message_failure(owners, tmp_path):
    import_prefix(tmp_path)
    with get_db() as db:
        db.execute(
            "INSERT INTO messages(conv_id,role,content) VALUES('legacy','user','preserved on failure')"
        )
        db.execute("UPDATE conversations SET deleted_at=1 WHERE id='legacy'")
        db.execute(
            "CREATE TRIGGER fixture_deny_message_delete BEFORE DELETE ON messages BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conversations.permanent_delete_conv("legacy", None)
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM conversations WHERE id='legacy'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM legacy_conversation_history").fetchone()[0] == 1
        db.execute("DROP TRIGGER fixture_deny_message_delete")
    assert conversations.permanent_delete_conv("legacy", None) == {"ok": True}
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM legacy_conversation_history").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
