"""Private recovery gates never trust manifest labels or execute source by default."""

import ast
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import sqlite3
import stat
import tarfile
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "secure_backup", Path(__file__).parents[1] / "scripts/secure_backup.py"
)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)
boot_spec = importlib.util.spec_from_file_location(
    "verify_backup_boot", Path(__file__).parents[1] / "scripts/verify_backup_boot.py"
)
boot = importlib.util.module_from_spec(boot_spec)
boot_spec.loader.exec_module(boot)


def test_real_capture_modes_round_trip_without_false_source_changes(tmp_path):
    # TarFile.gettarinfo keeps file-type bits; encoding a tar header removes
    # them. Exercise the actual capture function, rather than handcrafted modes.
    source = tmp_path / "source"
    source.mkdir(mode=0o750)
    document = source / "report.py"
    document.write_bytes(b"# offline recovery fixture\n")
    document.chmod(0o640)
    (source / "report-link").symlink_to("report.py")
    target = tmp_path / "captured.tar.gz"
    tree = ast.parse(backup.REMOTE)
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in {"identity", "add"}
    ]
    manifest = {"files": [], "changed_during_capture": [], "missing_during_capture": []}
    with tarfile.open(target, "w:gz") as captured:
        scope = {
            "archive": captured,
            "manifest": manifest,
            "os": os,
            "stat": stat,
            "Reader": None,
            "changed": manifest["changed_during_capture"].append,
        }
        reader_class = next(node for node in tree.body if isinstance(node, ast.ClassDef))
        scope["hashlib"] = hashlib
        exec(compile(ast.Module(body=[reader_class, *functions], type_ignores=[]), "capture", "exec"), scope)
        scope["add"](source, "fixture")
    assert manifest["changed_during_capture"] == []
    with tarfile.open(target, "r:gz") as captured:
        for entry in manifest["files"]:
            assert captured.getmember(entry["path"]).mode == entry["mode"]
            assert entry["mode"] == stat.S_IMODE(entry["mode"])


def archive(
    tmp_path,
    corrupt=False,
    name="consistent-databases/0",
    link=False,
    *,
    missing_root=False,
    missing_main=False,
    empty_databases=False,
    changes=None,
    duplicate_manifest=False,
    source_link=False,
):
    database = tmp_path / "source.db"
    conn = sqlite3.connect(database)
    conn.execute("CREATE TABLE marker(value)")
    conn.execute("INSERT INTO marker VALUES('recovery-value')")
    for table in ("users", "sessions", "conversations", "messages"):
        conn.execute(f"CREATE TABLE {table}(id TEXT)")
    conn.commit()
    conn.close()
    content = database.read_bytes()
    entries = []
    originals = "/home/ubuntu/not-the-main.db" if missing_main else backup.MAIN_DATABASE
    manifest = {
        "schema": 2,
        "files": entries,
        "databases": [] if empty_databases else [{"original": originals, "path": name, "integrity": "ok"}],
        "changed_during_capture": changes or [],
    }
    target = tmp_path / "backup.tar.gz"
    with tarfile.open(target, "w:gz") as stream:
        if not missing_root:
            info = tarfile.TarInfo(backup.PRODUCTION_ROOT)
            info.type, info.mode = tarfile.DIRTYPE, 0o700
            stream.addfile(info)
            entries.append({"path": info.name, "type": "directory", "size": 0, "mode": info.mode})
        for relative in sorted(backup.REQUIRED_SOURCE_FILES):
            source = b"" if relative == ".env" else b"# private recovery fixture\n"
            path = backup.PRODUCTION_ROOT + "/" + relative
            info = tarfile.TarInfo(path)
            info.size, info.mode = len(source), 0o600
            stream.addfile(info, io.BytesIO(source))
            entries.append(
                {
                    "path": path,
                    "type": "file",
                    "size": len(source),
                    "mode": info.mode,
                    "sha256": hashlib.sha256(source).hexdigest(),
                }
            )
        info = tarfile.TarInfo(name)
        info.size, info.mode = len(content), 0o600
        stream.addfile(info, io.BytesIO(content if not corrupt else b"X" + content[1:]))
        entries.append(
            {
                "path": name,
                "size": len(content),
                "mode": info.mode,
                "type": "file",
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
        if link or source_link:
            path = backup.PRODUCTION_ROOT + "/server/helper.py" if source_link else "link"
            info = tarfile.TarInfo(path)
            info.type, info.linkname, info.mode = tarfile.SYMTYPE, "/elsewhere", 0o777
            stream.addfile(info)
            entries.append(
                {
                    "path": path,
                    "type": "symlink",
                    "target": "/elsewhere" if source_link else "/original",
                    "size": 0,
                    "mode": 0o777,
                }
            )
        if duplicate_manifest:
            entries.append(dict(entries[-1]))
        data = json.dumps(manifest).encode()
        info = tarfile.TarInfo("backup-manifest.json")
        info.size = len(data)
        stream.addfile(info, io.BytesIO(data))
    return target


def test_consistent_database_is_recovered_and_checked(tmp_path):
    target = archive(tmp_path)
    recovery = tmp_path / "restore"
    recovery.mkdir()
    result = backup.verify_archive(target, recovery)
    assert result["databases"] == 1 and result["files"] == len(backup.REQUIRED_SOURCE_FILES) + 1
    assert result["sqlite_restore_verified"] is True
    conn = sqlite3.connect(recovery / "0")
    assert conn.execute("SELECT value FROM marker").fetchone()[0] == "recovery-value"
    conn.close()
    assert backup.verify_archive(target, recovery, reuse_recovery=True) == result
    assert backup.verify_archive(target)["sqlite_restore_verified"] is False


@pytest.mark.parametrize(
    "options",
    [
        {"corrupt": True},
        {"name": "../escape"},
        {"name": "/absolute"},
        {"link": True},
        {"missing_root": True},
        {"missing_main": True},
        {"empty_databases": True},
        {"duplicate_manifest": True},
        {"name": "consistent-databases/nested/0"},
    ],
)
def test_forged_bytes_paths_or_incomplete_scope_do_not_verify(tmp_path, options):
    with pytest.raises(ValueError):
        backup.verify_archive(archive(tmp_path, **options))


def test_empty_manifest_cannot_be_a_verified_backup(tmp_path):
    target = tmp_path / "empty.tar.gz"
    with tarfile.open(target, "w:gz") as stream:
        data = json.dumps({"files": [], "databases": [], "changed_during_capture": []}).encode()
        info = tarfile.TarInfo("backup-manifest.json")
        info.size = len(data)
        stream.addfile(info, io.BytesIO(data))
    with pytest.raises(ValueError):
        backup.verify_archive(target)


def test_source_change_blocks_promotion_but_live_data_change_does_not_discard_sqlite_copy(tmp_path):
    changes = [
        backup.PRODUCTION_ROOT + "/server/main.py",
        backup.PRODUCTION_ROOT + "/app.log.1",
        "snapshot/home/ubuntu/.claude/projects/project/session.jsonl",
    ]
    target = archive(tmp_path, changes=changes)
    recovery = tmp_path / "restore"
    recovery.mkdir()
    result = backup.verify_archive(target, recovery)
    assert result["sqlite_restore_verified"] is True
    assert result["critical_changed"] == 1 and result["runtime_changed"] == 2
    assert result["source_unchanged"] is False


def test_reopened_recovery_files_must_remain_identical(tmp_path):
    target = archive(tmp_path)
    recovery = tmp_path / "restore"
    recovery.mkdir()
    backup.verify_archive(target, recovery)
    with (recovery / "0").open("ab") as dest:
        dest.write(b"changed")
    with pytest.raises(ValueError):
        backup.verify_archive(target, recovery, reuse_recovery=True)


def test_remote_fingerprints_detect_atomic_same_size_replacement(tmp_path):
    # Execute the actual remote fingerprint functions, with local fixture roots;
    # no SSH, SQLite writes, filesystem capture or Keychain operation is run.
    tree = ast.parse(backup.REMOTE)
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"identity", "changed", "critical", "source_fingerprints", "file_hash"}
    ]
    root = tmp_path / "stock-analyzer"
    (root / "server").mkdir(parents=True)
    source = root / "server/main.py"
    source.write_bytes(b"AAAA")
    import re

    env = {
        "pathlib": pathlib,
        "hashlib": hashlib,
        "os": os,
        "stat": __import__("stat"),
        "re": re,
        "roots": [root],
        "manifest": {"changed_during_capture": []},
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), "<remote-fingerprints>", "exec"), env)
    before = env["source_fingerprints"]()
    old_time = source.stat().st_mtime_ns
    replacement = root / "replacement"
    replacement.write_bytes(b"BBBB")
    os.utime(replacement, ns=(old_time, old_time))
    replacement.replace(source)
    after = env["source_fingerprints"]()
    key = "snapshot/" + str(source).lstrip("/")
    assert before[key] != after[key]


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".git/config",
        "data/chat.db",
        "server/__pycache__/x.pyc",
        "../server/main.py",
        "/server/main.py",
        "server/../auth.py",
    ],
)
def test_boot_source_allowlist_excludes_private_runtime_and_unsafe_paths(name):
    assert boot.source_file(name) is False


def test_boot_restore_only_copies_verified_source_not_env_or_runtime(tmp_path):
    target = archive(tmp_path)
    app = tmp_path / "boot/app"
    restored = boot.restore_sources(target, app, backup.read_manifest(target))
    assert "server/main.py" in restored
    assert (app / "server/main.py").is_file()
    assert not (app / ".env").exists() and not (app / "data").exists() and not (app / ".git").exists()


def test_boot_restore_never_follows_archive_source_links(tmp_path):
    target = archive(tmp_path, source_link=True)
    with pytest.raises(ValueError):
        boot.restore_sources(target, tmp_path / "boot/app", backup.read_manifest(target))
    assert not (tmp_path / "boot/app/server/helper.py").exists()


def test_boot_environment_does_not_inherit_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-an-actual-key")
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted.invalid")
    environment = boot.boot_environment(tmp_path, 18811)
    assert "ANTHROPIC_API_KEY" not in environment and "HTTPS_PROXY" not in environment
    assert environment["HOME"] == str(tmp_path / "home")
    assert environment["CLAUDE_BIN"] == str(tmp_path / "disabled-claude")
    policy = boot.sandbox_policy(tmp_path, tmp_path.parent, 18811, Path("/operator"))
    assert "(deny network*)" in policy and '(allow network-inbound (local ip "localhost:18811"))' in policy
    assert "network-outbound" not in policy


def test_failed_boot_is_false_does_not_spawn_or_modify_restored_database(monkeypatch, tmp_path):
    target = archive(tmp_path)
    recovery = tmp_path / "restore"
    recovery.mkdir()
    backup.verify_archive(target, recovery)
    original = backup.digest(recovery / "0")
    monkeypatch.setattr(boot, "VENV", tmp_path / "missing-runtime")
    monkeypatch.setattr(
        boot.subprocess, "Popen", lambda *a, **kw: pytest.fail("No process should be started")
    )
    result = boot.verify_boot(target, recovery, tmp_path, backup.read_manifest(target), backup.digest(target))
    assert result["ok"] is False and result["error_type"] == "ValueError"
    assert result["live_models_tested"] is False
    assert backup.digest(recovery / "0") == original
    assert "recovery-value" not in json.dumps(result)


def test_plan_mode_never_reads_keychain_or_claims_boot_passed(monkeypatch, tmp_path, capsys):
    base = tmp_path / "base"
    base.mkdir()
    (base / "BACKUP-REPORT.json").write_text(json.dumps({"status": "verified and unmounted"}))
    monkeypatch.setattr(
        "sys.argv",
        [
            "secure_backup.py",
            "--base-backup",
            str(base),
            "--backup-root",
            str(tmp_path / "backups"),
            "--verify-boot",
        ],
    )
    monkeypatch.setattr(
        backup.subprocess, "run", lambda *a, **kw: pytest.fail("Plan must perform no subprocess work")
    )
    backup.main()
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "plan" and result["verify_boot_requested"] is True
    assert result["application_restore_verified"] is False
    assert not (tmp_path / "backups").exists()


def test_base_history_receipt_is_bound_to_report_and_actual_ciphertext(tmp_path):
    base = tmp_path / "base"
    image = base / "fixture.sparsebundle"
    image.mkdir(parents=True)
    band = image / "band"
    band.write_bytes(b"offline ciphertext fixture")
    report = base / "BACKUP-REPORT.json"
    report.write_text('{"status":"verified and unmounted"}')
    entries = [
        {"path": "fixture.sparsebundle/band", "size": band.stat().st_size, "sha256": backup.digest(band)}
    ]
    receipt = {
        "status": "verified and unmounted",
        "unmounted": True,
        "base_directory": str(base),
        "base_report_sha256": backup.digest(report),
        "image_tree_sha256": hashlib.sha256(
            json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest(),
        **{
            key: True
            for key in ("archive_verified", "filesystem_verified", "databases_verified", "git_verified")
        },
    }
    path = tmp_path / "independent.json"
    path.write_text(json.dumps(receipt))
    assert backup.verified_base(base, path)["receipt_sha256"] == backup.digest(path)
    band.write_bytes(b"changed ciphertext fixture")
    with pytest.raises(ValueError, match="image changed"):
        backup.verified_base(base, path)
    band.write_bytes(b"offline ciphertext fixture")
    report.write_text('{"status":"changed"}')
    with pytest.raises(ValueError, match="absent or changed"):
        backup.verified_base(base, path)
