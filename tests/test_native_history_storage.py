"""Public platform history: staged import, durable prefix and native continuation."""

import builtins
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

from server import conversations, legacy_history
from server.agent import runtime, store
from server.database import delete_user_account, get_db


@pytest.fixture
def history_owner(monkeypatch):
    with get_db() as db:
        db.execute("INSERT INTO users(username,password_hash) VALUES('alice','synthetic')")
        db.execute("INSERT INTO users(username,password_hash) VALUES('bob','synthetic')")
        db.execute(
            "INSERT INTO conversations(id,username,title,project_dir,session_id) VALUES(?,?,?,?,?)",
            ["legacy", "alice", "原始标题", "historical-project", "historical-session"],
        )
        db.execute("INSERT INTO conversations(id,username) VALUES('fresh','alice')")
    runtime.init_runtime_db()
    monkeypatch.setattr(conversations, "require_auth", lambda request: "alice")
    return "alice"


def staged_source(tmp_path, *, name="historical-session", project="historical-project", question="原问题"):
    root = tmp_path / "staged-projects"
    directory = root / project
    directory.mkdir(parents=True, exist_ok=True)
    events = [
        {"type": "user", "message": {"content": question}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "PRIVATE_REASONING"},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "PRIVATE_COMMAND"}},
                    {"type": "text", "text": "公开答案。 api_key=HISTORICAL_SECRET Bearer historical-token"},
                ]
            },
        },
    ]
    path = directory / (name + ".jsonl")
    path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in events), encoding="utf-8")
    return root, path


def imported(tmp_path):
    root, path = staged_source(tmp_path)
    with get_db() as db:
        result = legacy_history.import_staged_history(db, root)
    assert result["imported"] == 1
    return root, path


def test_staged_import_idempotent_public_and_preserves_messages(history_owner, tmp_path):
    with get_db() as db:
        db.execute("INSERT INTO messages(conv_id,role,content) VALUES('legacy','user','saved SQL')")
        before = [tuple(row) for row in db.execute("SELECT * FROM messages")]
    root, _path = imported(tmp_path)
    with get_db() as db:
        prefix = legacy_history.read_legacy_history(db, "legacy", "alice")
        assert prefix["user_turn_count"] == 1
        public = json.dumps(prefix, ensure_ascii=False)
        assert "原问题" in public and "公开答案" in public
        for private in ("PRIVATE_REASONING", "PRIVATE_COMMAND", "HISTORICAL_SECRET", "historical-token"):
            assert private not in public
        assert legacy_history.read_legacy_history(db, "legacy", "bob") is None
        result = legacy_history.import_staged_history(db, root)
        assert result["imported"] == 0 and result["unchanged"] == 1
        assert [tuple(row) for row in db.execute("SELECT * FROM messages")] == before


def test_conflict_rejects_without_overwriting_prefix_or_anchors(history_owner, tmp_path):
    root, path = imported(tmp_path)
    run = store.create_run("alice", "legacy", "新问题", "synthetic", "research")
    anchor = runtime.ensure_dialogue("alice", run)
    assert anchor["jsonl_user_turns"] == 1
    with get_db() as db:
        before = tuple(db.execute("SELECT * FROM legacy_conversation_history").fetchone())
        native = tuple(db.execute("SELECT * FROM native_agent_dialogues").fetchone())
    path.write_text(path.read_text() + '\n{"type":"user","message":{"content":"changed"}}')
    with get_db() as db:
        with pytest.raises(legacy_history.HistoryImportError, match="conflict"):
            legacy_history.import_staged_history(db, root)
        assert tuple(db.execute("SELECT * FROM legacy_conversation_history").fetchone()) == before
        assert tuple(db.execute("SELECT * FROM native_agent_dialogues").fetchone()) == native


def test_pending_history_is_visible_and_blocks_native_without_losing_sql(history_owner):
    with get_db() as db:
        db.execute("INSERT INTO messages(conv_id,role,content) VALUES('legacy','user','SQL可见问题')")
    assert conversations.list_convs(None)[-1]["history_migration"]["status"] in {
        "pending_import",
        "not_required",
    }
    result = conversations.get_conv("legacy", None)
    assert result["history_migration"]["status"] == "pending_import"
    assert result["messages"] == [{"role": "user", "content": "SQL可见问题"}]
    run = store.create_run("alice", "legacy", "不能丢弃旧历史", "synthetic", "research")
    with pytest.raises(HTTPException) as error:
        runtime.ensure_dialogue("alice", run)
    assert error.value.status_code == 409
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM messages WHERE conv_id='legacy'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM native_agent_dialogues").fetchone()[0] == 0


def test_imported_prefix_merges_native_sql_and_model_context(history_owner, tmp_path):
    imported(tmp_path)
    run = store.create_run("alice", "legacy", "原生追问", "synthetic", "research")
    anchor = runtime.ensure_dialogue("alice", run)
    answer = {"text": "原生答案", "native_agent": {"run_id": run["run_id"]}, "history_anchor": anchor}
    with get_db() as db:
        cursor = db.execute(
            "INSERT INTO messages(conv_id,role,content) VALUES('legacy','assistant',?)", [json.dumps(answer)]
        )
        before = cursor.lastrowid
        db.execute(
            "UPDATE native_agent_dialogues SET assistant_message_id=? WHERE run_id=?", [before, run["run_id"]]
        )
    result = conversations.get_conv("legacy", None)
    assert result["history_migration"]["status"] == "imported"
    assert [item["role"] for item in result["messages"]] == ["user", "assistant", "user", "assistant"]
    history = runtime._history("alice", "legacy", before)
    assert [item["content"] for item in history][::2] == ["原问题", "原生追问"]
    assert history[-1]["content"] == "原生答案"
    assert runtime._history("bob", "legacy", before) == []


def test_runtime_and_conversation_reads_never_open_external_home(history_owner, tmp_path, monkeypatch):
    home = tmp_path / "empty-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    real_open = builtins.open
    real_os_open = os.open

    def check_path(value):
        if isinstance(value, (str, bytes, os.PathLike)):
            assert ".claude" not in str(value) and "/skills/" not in str(value)

    def guarded_open(path, *args, **kwargs):
        check_path(path)
        return real_open(path, *args, **kwargs)

    def guarded_os_open(path, *args, **kwargs):
        check_path(path)
        return real_os_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    monkeypatch.setattr(
        Path, "home", classmethod(lambda cls: (_ for _ in ()).throw(AssertionError("external HOME lookup")))
    )
    run = store.create_run("alice", "fresh", "独立运行", "synthetic", "research")
    assert runtime.ensure_dialogue("alice", run)["jsonl_user_turns"] == 0
    assert conversations.get_conv("fresh", None)["history_migration"]["status"] == "not_required"
    conversations.get_conv("legacy", None)
    conversations.list_convs(None)
    assert runtime._history("alice", "fresh", 999)
    assert list(home.iterdir()) == []


def test_owner_and_account_cleanup_are_transactional(history_owner, tmp_path, monkeypatch):
    imported(tmp_path)
    monkeypatch.setattr(conversations, "require_auth", lambda request: "bob")
    with pytest.raises(HTTPException) as error:
        conversations.get_conv("legacy", None)
    assert error.value.status_code == 404
    with get_db() as db:
        db.execute("SAVEPOINT trial_account_delete")
        db.execute("DELETE FROM users WHERE username='alice'")
        assert db.execute("SELECT COUNT(*) FROM legacy_conversation_history").fetchone()[0] == 0
        db.execute("ROLLBACK TO trial_account_delete")
        db.execute("RELEASE trial_account_delete")
        assert db.execute("SELECT COUNT(*) FROM legacy_conversation_history").fetchone()[0] == 1
    assert delete_user_account("alice")
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM legacy_conversation_history").fetchone()[0] == 0


def test_schema_helper_keeps_caller_transaction_open(history_owner):
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO messages(conv_id,role,content) VALUES('fresh','user','rollback')")
        legacy_history.init_legacy_history_db(db)
        assert db.in_transaction
        db.execute("ROLLBACK")
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


@pytest.mark.parametrize(
    "damage",
    [
        "malformed",
        "non_object",
        "file_symlink",
        "project_symlink",
        "hardlink",
        "too_large",
        "too_many_lines",
        "long_line",
    ],
)
def test_import_rejects_unsafe_or_unbounded_history(history_owner, tmp_path, monkeypatch, damage):
    root, path = staged_source(tmp_path)
    if damage == "malformed":
        path.write_text('{"type":"user"}\n{BROKEN')
    elif damage == "non_object":
        path.write_text("[]")
    elif damage == "file_symlink":
        actual = tmp_path / "actual.jsonl"
        path.rename(actual)
        path.symlink_to(actual)
    elif damage == "project_symlink":
        actual = tmp_path / "actual-project"
        path.parent.rename(actual)
        path.parent.symlink_to(actual, target_is_directory=True)
    elif damage == "hardlink":
        os.link(path, tmp_path / "another-link.jsonl")
    elif damage == "too_large":
        monkeypatch.setattr(legacy_history, "MAX_FILE_BYTES", 8)
    elif damage == "too_many_lines":
        monkeypatch.setattr(legacy_history, "MAX_LINES", 1)
    elif damage == "long_line":
        monkeypatch.setattr(legacy_history, "MAX_LINE_BYTES", 8)
    with get_db() as db:
        with pytest.raises(legacy_history.HistoryImportError):
            legacy_history.import_staged_history(db, root)
        assert not db.in_transaction
        assert db.execute("SELECT COUNT(*) FROM legacy_conversation_history").fetchone()[0] == 0


def test_mapping_traversal_and_unavailable_owner_are_denied(history_owner, tmp_path):
    root, _path = staged_source(tmp_path)
    with get_db() as db:
        with pytest.raises(legacy_history.HistoryImportError, match="owner"):
            legacy_history.import_staged_history(db, root, username="unknown")
        db.execute("UPDATE conversations SET project_dir='../escape' WHERE id='legacy'")
        with pytest.raises(legacy_history.HistoryImportError, match="mapping"):
            legacy_history.import_staged_history(db, root)


def test_multiple_conversation_import_is_all_or_nothing(history_owner, tmp_path):
    root, _path = staged_source(tmp_path)
    with get_db() as db:
        db.execute(
            "INSERT INTO conversations(id,username,project_dir,session_id) VALUES('zz-missing','alice','missing','session')"
        )
        with pytest.raises(legacy_history.HistoryImportError):
            legacy_history.import_staged_history(db, root)
        assert db.execute("SELECT COUNT(*) FROM legacy_conversation_history").fetchone()[0] == 0


def test_explicit_offline_command_imports_only_staged_copy(history_owner, tmp_path):
    root, _path = staged_source(tmp_path)
    staged_db = tmp_path / "staged-chat.db"
    with get_db() as db:
        copied = sqlite3.connect(staged_db)
        db.backup(copied)
        copied.close()
    home = tmp_path / "offline-home"
    home.mkdir()
    repository = Path(__file__).resolve().parents[1]
    # This command has no application import, provider key, .env or model process.
    code = (
        "import runpy,sys; script=sys.argv[1]; sys.argv=['import_legacy_history.py','--staged-db',sys.argv[2],"
        "'--staged-project-root',sys.argv[3]]; "
        "module=runpy.run_path(script); "
        "assert 'server.config' not in sys.modules; "
        "assert module['main']()==0; assert 'server.config' not in sys.modules"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            code,
            str(repository / "scripts/import_legacy_history.py"),
            str(staged_db),
            str(root),
        ],
        env={"HOME": str(home), "PATH": os.defpath},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["imported"] == 1
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM legacy_conversation_history").fetchone()[0] == 0
    with sqlite3.connect(staged_db) as db:
        assert db.execute("SELECT COUNT(*) FROM legacy_conversation_history").fetchone()[0] == 1
    assert list(home.iterdir()) == []


def test_import_rejects_root_symlink_and_preserves_existing_sql(history_owner, tmp_path):
    root, _path = staged_source(tmp_path)
    symlink = tmp_path / "linked-projects"
    symlink.symlink_to(root, target_is_directory=True)
    with get_db() as db:
        with pytest.raises(OSError):
            legacy_history.import_staged_history(db, symlink)
        assert db.execute("SELECT COUNT(*) FROM legacy_conversation_history").fetchone()[0] == 0


def test_private_user_blocks_and_tool_result_frames_never_become_history(history_owner, tmp_path):
    root, path = staged_source(tmp_path)
    extra = [
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "content": "PRIVATE_TOOL_RESULT"},
                    {"type": "text", "text": "UNTRUSTED_TOOL_WRAPPER"},
                ]
            },
        },
        {
            "type": "user",
            "message": json.dumps(
                {
                    "content": [
                        {"type": "thinking", "thinking": "PRIVATE_SERIALIZED_WRAPPER"},
                        {"type": "text", "text": "序列化后续问题"},
                    ]
                }
            ),
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "PRIVATE_USER_WRAPPER"},
                    {"type": "text", "text": "真实后续问题"},
                ]
            },
        },
    ]
    path.write_text(path.read_text() + "\n" + "\n".join(json.dumps(event) for event in extra))
    with get_db() as db:
        legacy_history.import_staged_history(db, root)
        prefix = legacy_history.read_legacy_history(db, "legacy", "alice")
        assert prefix["user_turn_count"] == 3
        assert [item["content"] for item in prefix["messages"][-2:]] == ["序列化后续问题", "真实后续问题"]
        assert "PRIVATE_" not in json.dumps(prefix)
        assert "UNTRUSTED_TOOL_WRAPPER" not in json.dumps(prefix)
