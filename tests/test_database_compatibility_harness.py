"""Synthetic-only checks; no encrypted image, Keychain or actual backup is opened."""

import ast
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "scripts/verify_database_compatibility.py"
SPEC = importlib.util.spec_from_file_location("database_compatibility_harness", PATH)
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


def database(tmp_path):
    path = tmp_path / "chat.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE users(username TEXT, password_hash TEXT)")
        db.execute("INSERT INTO users VALUES('original-owner','original-password-hash')")
        db.execute("CREATE TABLE messages(id INTEGER,conv_id TEXT,content BLOB)")
        db.executemany(
            "INSERT INTO messages VALUES(1,'original-conversation',?)", [(b"private original",)] * 2
        )
        db.execute("CREATE TABLE sessions(token TEXT)")
        db.execute("INSERT INTO sessions VALUES('synthetic-original-session')")
        db.execute("CREATE TABLE capability_runs(run_id TEXT,username TEXT,revoked INTEGER)")
        db.execute("INSERT INTO capability_runs VALUES('original-run','original-owner',0)")
        db.execute("CREATE TABLE retired_usernames(username TEXT,retired_at INTEGER)")
        db.execute("INSERT INTO retired_usernames VALUES('original-retired',1)")
    return path


def test_added_schema_and_rows_preserve_original_common_field_multiset(tmp_path):
    path = database(tmp_path)
    baseline = harness.history_snapshot(path)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE messages ADD COLUMN new_field TEXT DEFAULT 'default'")
        db.execute("INSERT INTO users VALUES('new-owner','new-password-hash')")
        db.execute("DELETE FROM sessions")
        db.execute("UPDATE capability_runs SET revoked=1")
    assert harness.preserved(baseline, harness.history_snapshot(path, baseline))
    assert "sessions" not in baseline
    assert baseline["capability_runs"]["columns"] == ["run_id", "username"]
    assert "private original" not in str(baseline)
    assert sum(baseline["messages"]["rows"].values()) == 2


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM messages WHERE rowid=1",
        "UPDATE users SET password_hash='modified'",
        "UPDATE capability_runs SET username='other-owner'",
        "DELETE FROM retired_usernames",
    ],
)
def test_original_row_loss_or_changes_cannot_pass_as_added_schema(tmp_path, statement):
    path = database(tmp_path)
    baseline = harness.history_snapshot(path)
    with sqlite3.connect(path) as db:
        db.execute(statement)
    assert not harness.preserved(baseline, harness.history_snapshot(path, baseline))


def test_original_columns_cannot_be_removed(tmp_path):
    path = database(tmp_path)
    baseline = harness.history_snapshot(path)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE users DROP COLUMN password_hash")
    with pytest.raises(ValueError, match="original_columns_missing"):
        harness.history_snapshot(path, baseline)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE research_report_revisions SET narrative='changed' WHERE revision_id='same-revision'",
        "DELETE FROM research_report_revisions",
        "DROP TABLE research_report_revisions",
        "DELETE FROM retired_usernames WHERE username='new-retired-owner'",
    ],
)
def test_cumulative_baseline_protects_new_research_and_tombstones(tmp_path, statement):
    path = database(tmp_path)
    original = harness.history_snapshot(path)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE research_report_revisions(revision_id TEXT,narrative TEXT)")
        db.execute("INSERT INTO research_report_revisions VALUES('same-revision','immutable new narrative')")
        db.execute("INSERT INTO retired_usernames VALUES('new-retired-owner',2)")
    accumulated = harness.history_snapshot(path)
    with sqlite3.connect(path) as db:
        db.execute(statement)
    # The original-production-only baseline cannot detect a changed new ledger.
    assert harness.preserved(original, harness.history_snapshot(path, original))
    try:
        after = harness.history_snapshot(path, accumulated)
    except ValueError as error:
        assert str(error) == "original_table_missing"
    else:
        assert not harness.preserved(accumulated, after)


def test_cumulative_baseline_allows_new_rows_and_columns_without_rewriting_previous_history(tmp_path):
    path = database(tmp_path)
    accumulated = harness.history_snapshot(path)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE users ADD COLUMN added_migration_field TEXT DEFAULT 'new default'")
        db.execute("INSERT INTO users(username,password_hash) VALUES('later-owner','later-hash')")
        db.execute("CREATE TABLE research_new_table(id TEXT)")
        db.execute("INSERT INTO research_new_table VALUES('later-new-record')")
    assert harness.preserved(accumulated, harness.history_snapshot(path, accumulated))


def test_positive_source_allowlist_never_reads_env_history_or_data():
    for name in (
        ".env",
        ".git/objects/private",
        "data/chat.db",
        "server/.env",
        "server/__pycache__/private.pyc",
        "../server/main.py",
    ):
        assert not harness.source_file(name)
    assert harness.source_file("server/recovery.py") and harness.source_file("server/research/ledger.py")


def test_process_environment_has_no_model_credentials_or_global_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-inherit")
    monkeypatch.setenv("ADMIN_PASS", "must-not-inherit")
    monkeypatch.setenv("PRISM_SINGLE_WORKER", "1")
    env = harness.environment(tmp_path, 12345)
    assert "DEEPSEEK_API_KEY" not in env and "ADMIN_PASS" not in env and "PRISM_SINGLE_WORKER" not in env
    assert env["HOME"] == str(tmp_path / "home")
    assert env["CLAUDE_BIN"].endswith("disabled-claude")
    assert env["PRISM_VIBE_ENGINE_URL"] == "http://127.0.0.1:9"


def test_bootstrap_and_policy_keep_live_tasks_and_external_writes_disabled(tmp_path):
    ast.parse(harness.BOOTSTRAP)
    text = harness.sandbox_policy(
        tmp_path / "acceptance",
        tmp_path,
        12345,
        Path("/operator"),
        Path("/workspace"),
        source_mount=tmp_path / "source-mounted",
    )
    assert "(deny network*)" in text
    assert text.count("localhost:12345") == 2
    for scope in ("/workspace", "/operator/.ssh", "/operator/Library/Keychains", "/operator/Backups"):
        assert scope in text
    assert "(deny file-write*" in text
    assert '(subpath "' + str(tmp_path / "acceptance/new-app") + '")' in text
    assert '(subpath "' + str(tmp_path / "acceptance/recovery-app") + '")' in text
    assert "(deny file-read-data (require-all" in text
    assert "(deny file-read-metadata (require-all" in text
    assert '(require-not (literal "' + str(tmp_path) + '"))' in text


@pytest.mark.skipif(not Path("/usr/bin/sandbox-exec").is_file(), reason="Mac policy parser required")
def test_actual_macos_policy_parser_accepts_synthetic_only_policy(tmp_path):
    import subprocess

    policy = tmp_path / "synthetic.sb"
    policy.write_text(
        harness.sandbox_policy(
            tmp_path / "acceptance",
            tmp_path,
            12345,
            Path("/synthetic-operator"),
            Path("/synthetic-workspace"),
            source_mount=tmp_path / "source-mounted",
        )
    )
    result = subprocess.run(
        ["/usr/bin/sandbox-exec", "-f", str(policy), "/usr/bin/true"], capture_output=True
    )
    # The test runner is itself sandboxed, so a second sandbox may compile but
    # cannot be applied. Syntax errors fail earlier and must never be accepted.
    assert result.returncode == 0 or (
        result.returncode == 71 and result.stderr == b"sandbox-exec: sandbox_apply: Operation not permitted\n"
    ), result.stderr.decode()


def test_failed_old_boot_diagnostic_remains_a_failure_and_contains_no_private_log(tmp_path):
    import json

    boot = tmp_path / "boot-verification"
    boot.mkdir()
    (boot / "result.json").write_text(
        json.dumps(
            {
                "ok": False,
                "checks": {"login_and_owner_profile": False, "empty_conversation_read": True},
                "password": "never-return",
            }
        )
    )
    (boot / "logs").mkdir()
    (boot / "logs/process.log").write_text(
        'File "/private/source/main.py", line 123\nRuntimeError: never-return-private-content'
    )
    result = harness.backup_boot_diagnostic(tmp_path)
    assert result["recorded_ok"] is False
    assert result["checks"]["login_and_owner_profile"] is False
    assert "never-return" not in str(result)
    assert result["log_exception_classes"] == ["RuntimeError"]
    assert result["log_source_frames"] == [{"file": "main.py", "line": 123}]


def test_source_commit_cannot_be_a_git_option(tmp_path):
    with pytest.raises(ValueError, match="exact_source_commit_required"):
        harness.export_source(tmp_path, "--remote=" + "x" * 31, tmp_path / "copy")


def test_relative_image_is_bound_to_its_report_directory(tmp_path):
    backup = tmp_path / "backup"
    image = backup / "encrypted.sparsebundle"
    image.mkdir(parents=True)
    assert harness.source_image(backup, image.name) == image.resolve()
    assert harness.source_image(backup, image) == image.resolve()
    with pytest.raises(ValueError, match="source_image_scope"):
        harness.source_image(backup, "../")


def test_parent_history_reader_really_closes_its_sqlite_connection(tmp_path, monkeypatch):
    path = database(tmp_path)
    connect = sqlite3.connect
    states = []

    class Spy:
        def __init__(self, connection):
            self.connection, self.closed = connection, False
            states.append(self)

        def execute(self, *args):
            return self.connection.execute(*args)

        def close(self):
            self.closed = True
            self.connection.close()

    monkeypatch.setattr(harness.sqlite3, "connect", lambda *a, **kw: Spy(connect(*a, **kw)))
    harness.history_snapshot(path)
    assert len(states) == 1 and states[0].closed


def test_checkpoint_seals_new_rows_before_immutable_read(tmp_path):
    path = database(tmp_path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        db.execute("INSERT INTO users VALUES('new-owner','new-password-hash')")
    # The connection context commits, but deliberately remains open here.
    assert Path(str(path) + "-wal").is_file()
    db.close()
    assert all(harness.seal_stopped_database(path).values())
    sealed = harness.history_snapshot(path, immutable=True)
    assert sum(sealed["users"]["rows"].values()) == 2
    assert sealed == harness.history_snapshot(path, immutable=True)
    assert not Path(str(path) + "-shm").exists()


def test_checkpoint_with_live_reader_or_sidecar_dependency_is_rejected(tmp_path, monkeypatch):
    path = database(tmp_path)
    Path(str(path) + "-wal").write_bytes(b"not-a-checkpointed-database")

    class Connection:
        def execute(self, _):
            return self

        def fetchone(self):
            return (0, 0, 0)

        def close(self):
            pass

    monkeypatch.setattr(harness.sqlite3, "connect", lambda *a, **kw: Connection())
    with pytest.raises(ValueError, match="sidecar_dependency"):
        harness.seal_stopped_database(path)
    monkeypatch.setattr(Connection, "fetchone", lambda self: (1, 3, 0))
    with pytest.raises(ValueError, match="checkpoint_busy"):
        harness.seal_stopped_database(path)


def test_source_export_uses_raw_blobs_ignoring_archive_attributes(tmp_path):
    import subprocess

    repo = tmp_path / "source"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            [
                "/usr/bin/git",
                "-c",
                "user.name=Prism Test",
                "-c",
                "user.email=synthetic@example.invalid",
                "-C",
                str(repo),
                *args,
            ],
            check=True,
            capture_output=True,
        ).stdout

    git("init", "-q")
    (repo / "server").mkdir()
    for name in ("auth.py", "config.py", "database.py"):
        (repo / "server" / name).write_text("# inert source $Format:%H$\n")
    (repo / ".gitattributes").write_text("server/auth.py export-ignore\nserver/config.py export-subst\n")
    (repo / ".env").write_text("SYNTHETIC_ONLY=must-not-export\n")
    git("add", ".")
    git("commit", "-qm", "synthetic source")
    commit = git("rev-parse", "HEAD").decode().strip()
    target = tmp_path / "export"
    binding = harness.export_source(repo, commit, target)
    assert (target / "server/auth.py").is_file()
    assert (target / "server/config.py").read_bytes() == (repo / "server/config.py").read_bytes()
    assert binding["commit"] == commit and not (target / ".env").exists()
    assert not (target / ".git").exists()


@pytest.mark.parametrize(
    ("mac_passed", "original_passed", "requested", "expected"),
    [
        (False, False, False, 2),
        (False, True, True, 2),
        (True, False, True, 2),
        (True, True, True, 0),
        (True, False, False, 0),
    ],
)
def test_process_success_requires_all_requested_mac_checks(mac_passed, original_passed, requested, expected):
    result = {
        "mac_compatibility_verified": mac_passed,
        "original_application_restore_verified": original_passed,
        "linux_execution_verified": False,
        "release_gate_ready": False,
    }
    assert harness.verification_exit_code(result, original_boot_requested=requested) == expected


def test_failed_execution_writes_receipt_and_detaches_before_nonzero_exit(tmp_path, monkeypatch, capsys):
    import json
    from types import SimpleNamespace

    backup, output, runtime = (tmp_path / name for name in ("backup", "output", "runtime"))
    backup.mkdir()
    (backup / "synthetic.sparsebundle").mkdir()
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin/python").write_text("inert synthetic runtime")
    (backup / "BACKUP-REPORT.json").write_text(
        json.dumps(
            {
                "status": "verified and unmounted",
                "sqlite_restore_verified": True,
                "backup_image": "synthetic.sparsebundle",
                "keychain_service": "synthetic-service",
                "keychain_account": "synthetic-account",
            }
        )
    )
    calls, attached = [], set()

    def fake_hdi(*args, password=None):
        calls.append(args[0])
        if args[0] == "attach":
            attached.add(args[args.index("-mountpoint") + 1])
        elif args[0] == "detach":
            attached.remove(args[1])
        return b""

    monkeypatch.setattr(harness, "VENV", runtime)
    monkeypatch.setattr(harness.os, "uname", lambda: SimpleNamespace(sysname="Darwin"))
    monkeypatch.setattr(
        harness.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=b"synthetic-not-a-credential\n")
    )
    monkeypatch.setattr(harness, "hdi", fake_hdi)
    monkeypatch.setattr(harness, "mounted", lambda path: path in attached)
    monkeypatch.setattr(
        "sys.argv",
        [
            "verify_database_compatibility.py",
            "--backup-dir",
            str(backup),
            "--new-source",
            str(tmp_path / "new-source"),
            "--new-commit",
            "a" * 40,
            "--recovery-source",
            str(tmp_path / "recovery-source"),
            "--recovery-commit",
            "b" * 40,
            "--output-root",
            str(output),
            "--execute",
        ],
    )
    with pytest.raises(SystemExit) as error:
        harness.main()
    assert error.value.code == 2
    reports = list(output.glob("*/COMPATIBILITY-REPORT.json"))
    assert len(reports) == 1 and calls == ["attach", "detach"] and not attached
    receipt = json.loads(reports[0].read_text())
    assert receipt["mac_compatibility_verified"] is False
    assert receipt["source_unmounted"] is True and receipt["acceptance_unmounted"] is True
    assert receipt["error_type"] == "FileNotFoundError"
    summary = json.loads(capsys.readouterr().out)
    assert summary["receipt"] == str(reports[0]) and summary["mac_compatibility_verified"] is False
    assert "synthetic-not-a-credential" not in reports[0].read_text()


def test_source_mount_is_required_and_forwarded_from_the_trusted_parent():
    with pytest.raises(TypeError, match="source_mount"):
        harness.sandbox_policy(Path("/area"), Path("/image"), 12345, Path("/operator"), Path("/workspace"))
    parsed = ast.parse(PATH.read_text())
    calls = [node for node in ast.walk(parsed) if isinstance(node, ast.Call)]
    for name in ("run_phase", "sandbox_policy"):
        trusted_calls = [node for node in calls if isinstance(node.func, ast.Name) and node.func.id == name]
        assert trusted_calls
        assert all(
            any(
                keyword.arg == "source_mount"
                and isinstance(keyword.value, ast.Name)
                and keyword.value.id == "source_mount"
                for keyword in node.keywords
            )
            for node in trusted_calls
        )
    assert (
        "'source_backup_reads_denied':blocked(lambda:os.open(c['backup_probe'],os.O_RDONLY))"
        in harness.BOOTSTRAP
    )
    assert "if not all(checks.values()):raise RuntimeError('isolation_probe_failed')" in harness.BOOTSTRAP


@pytest.mark.skipif(not Path("/usr/bin/sandbox-exec").is_file(), reason="Native macOS sandbox required")
def test_native_ops_output_root_denies_whole_source_mount_and_preserves_acceptance_reads(tmp_path):
    """All files are synthetic; no Keychain, encrypted image or real backup is used."""
    operator = tmp_path / "synthetic-operator"
    output = operator / "Library/Application Support/Prism-Release/compat-executor-synthetic"
    run = output / "fresh-run"
    source_mount, mount = run / "source-mounted", run / "acceptance-mounted"
    area, workspace = mount / "acceptance", operator / "workspace"
    denied = {
        "source_database_denied": source_mount / "recovery-databases/synthetic-db",
        "source_archive_denied": source_mount / "production.tar.gz",
        "source_extra_sibling_denied": source_mount / "other/source-canary.txt",
        "original_backups_denied": operator / "Backups/Prism/synthetic-canary.txt",
        "workspace_denied": workspace / "server/main.py",
    }
    allowed = {
        "acceptance_database_readable": area / "data/chat.db",
        "new_source_readable": area / "new-app/server/main.py",
        "recovery_source_readable": area / "recovery-app/server/recovery.py",
    }
    for path in (*denied.values(), *allowed.values()):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text("synthetic-canary-only")
    assert not source_mount.is_relative_to(operator / "Backups")
    policy = area / "synthetic.sb"
    policy.write_text(
        harness.sandbox_policy(area, mount, 12345, operator, workspace, source_mount=source_mount)
    )
    child = r"""
import errno,json,os,sys
from pathlib import Path
c=json.loads(sys.stdin.readline())
def denied(path):
 try:
  fd=os.open(path,os.O_RDONLY)
 except OSError as error:
  return error.errno in (errno.EPERM,errno.EACCES)
 else:
  os.close(fd)
  return False
checks={name:denied(path) for name,path in c['denied'].items()}
checks.update({name:Path(path).read_text()=='synthetic-canary-only' for name,path in c['allowed'].items()})
print(json.dumps(checks,sort_keys=True))
raise SystemExit(0 if all(checks.values()) else 3)
"""
    result = subprocess.run(
        ["/usr/bin/sandbox-exec", "-f", str(policy), sys.executable, "-I", "-B", "-c", child],
        input=json.dumps(
            {
                "denied": {k: str(v) for k, v in denied.items()},
                "allowed": {k: str(v) for k, v in allowed.items()},
            }
        ).encode(),
        capture_output=True,
        timeout=15,
        env={"PATH": "/usr/bin:/bin", "HOME": str(operator), "LANG": "en_US.UTF-8"},
    )
    if result.returncode == 71 and result.stderr == b"sandbox-exec: sandbox_apply: Operation not permitted\n":
        if os.environ.get("PRISM_REQUIRE_NATIVE_SANDBOX") == "1":
            pytest.fail("native_sandbox_application_denied_by_outer_sandbox")
        pytest.skip("Outer sandbox prevents native policy application; no native acceptance claimed")
    assert result.returncode == 0, {
        "native_process_succeeded": result.returncode == 0,
        "stderr_empty": result.stderr == b"",
    }
    checks = json.loads(result.stdout)
    assert set(checks) == set(denied) | set(allowed)
    assert all(value is True for value in checks.values())
