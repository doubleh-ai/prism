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
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace

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


async def test_restore_identity_probe_uses_actual_profile_contract(client, auth_cookies):
    response = await client.get("/api/me", cookies=auth_cookies)
    assert boot.profile_matches_owner(response.status_code, response.json(), "testuser")
    assert not boot.profile_matches_owner(200, {"username": "testuser"}, "testuser")
    assert not boot.profile_matches_owner(200, {"user": "other-owner"}, "testuser")
    assert not boot.profile_matches_owner(401, {"user": "testuser"}, "testuser")


def test_boot_startup_diagnostic_cannot_retain_response_body_or_private_fields():
    diagnostic = boot.startup_diagnostic(
        200,
        {
            "nonce": "synthetic-nonce",
            "pid": 123,
            "credential": "must-not-retain",
            "checks": {
                "outbound_denied": True,
                "credential_reads_denied": True,
                "outside_writes_denied": False,
                "private_content": "must-not-retain",
            },
        },
        "synthetic-nonce",
        123,
    )
    assert diagnostic["http_status"] == 200
    assert diagnostic["nonce_matches"] and diagnostic["pid_matches"]
    assert diagnostic["isolation_checks"]["outside_writes_denied"] is False
    assert "must-not-retain" not in str(diagnostic)
    assert not any(boot.startup_diagnostic(401, ["private-content"], "nonce", 1)["isolation_checks"].values())


def test_old_backup_login_contract_preserves_both_session_and_csrf_cookies(monkeypatch):
    # The verified old auth source returns {user, csrf_token}; authentication is
    # the token cookie, followed by a separate CSRF cookie. Header duplicates
    # must survive even when the final Set-Cookie has the same exact spelling.
    class Response:
        status = 200

        def read(self, _):
            return b'{"user":"synthetic-owner","csrf_token":"synthetic-csrf"}'

        def getheaders(self):
            return [
                ("Set-Cookie", "token=synthetic-session; HttpOnly; Path=/; SameSite=lax"),
                ("Set-Cookie", "csrf_token=synthetic-csrf; Path=/; SameSite=lax"),
            ]

    class Connection:
        def __init__(self, *a, **kw):
            self.closed = False

        def request(self, *a, **kw):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            self.closed = True

    monkeypatch.setattr(boot.http.client, "HTTPConnection", Connection)
    status, result, headers = boot._request(12345, "POST", "/api/login", {})
    cookies = SimpleCookie()
    cookies.load(headers["set-cookie"])
    assert status == 200 and result["user"] == "synthetic-owner"
    assert set(cookies) == {"token", "csrf_token"}
    assert cookies["token"].value == "synthetic-session"


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
    policy = boot.sandbox_policy(
        tmp_path,
        tmp_path.parent,
        18811,
        Path("/operator"),
        workspace=Path("/workspace"),
        source_mount=tmp_path.parent,
    )
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
    result = boot.verify_boot(
        target,
        recovery,
        tmp_path,
        backup.read_manifest(target),
        backup.digest(target),
        workspace=tmp_path / "workspace",
        source_mount=tmp_path,
    )
    assert result["ok"] is False and result["error_type"] == "ValueError"
    assert result["live_models_tested"] is False
    assert backup.digest(recovery / "0") == original
    assert "recovery-value" not in json.dumps(result)


def test_plan_mode_never_reads_keychain_or_claims_boot_passed(monkeypatch, tmp_path, capsys):
    # This command deliberately requires the operator Mac, including in plan mode.
    # Model that precondition explicitly so Linux CI tests the plan's no-write contract.
    monkeypatch.setattr(backup.os, "uname", lambda: SimpleNamespace(sysname="Darwin"))
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


@pytest.mark.parametrize("execute", [False, True])
def test_linux_rejected_before_keychain_image_or_ssh_work(monkeypatch, tmp_path, capsys, execute):
    monkeypatch.setattr(backup.os, "uname", lambda: SimpleNamespace(sysname="Linux"))
    arguments = ["secure_backup.py", "--base-backup", str(tmp_path / "not-read"), "--verify-boot"]
    if execute:
        arguments.append("--execute")
    monkeypatch.setattr("sys.argv", arguments)

    def forbidden(*args, **kwargs):
        pytest.fail("Unsupported platforms must stop before authentication or subprocess work")

    monkeypatch.setattr(backup.subprocess, "run", forbidden)
    monkeypatch.setattr(backup.subprocess, "Popen", forbidden)
    monkeypatch.setattr(backup, "_hdi", forbidden)
    monkeypatch.setattr(backup, "verified_base", forbidden)
    with pytest.raises(ValueError, match="Run on the authenticated operator Mac"):
        backup.main()
    assert capsys.readouterr().out == ""


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


def test_restore_policy_requires_explicit_trusted_scopes():
    with pytest.raises(TypeError, match="workspace"):
        boot.sandbox_policy(Path("/boot"), Path("/image"), 12345, Path("/operator"))
    with pytest.raises(TypeError, match="source_mount"):
        boot.sandbox_policy(
            Path("/boot"), Path("/image"), 12345, Path("/operator"), workspace=Path("/workspace")
        )


def test_secure_backup_requires_workspace_before_any_execution_side_effect(tmp_path, monkeypatch):
    base = tmp_path / "base"
    base.mkdir()
    (base / "BACKUP-REPORT.json").write_text(json.dumps({"status": "verified and unmounted"}))
    monkeypatch.setattr(backup.os, "uname", lambda: SimpleNamespace(sysname="Darwin"))
    monkeypatch.setattr(
        "sys.argv",
        [
            "secure_backup.py",
            "--base-backup",
            str(base),
            "--backup-root",
            str(tmp_path / "output"),
            "--verify-boot",
            "--execute",
        ],
    )
    monkeypatch.setattr(
        backup, "verified_base", lambda *a: pytest.fail("Workspace must fail before base verification")
    )
    monkeypatch.setattr(backup.subprocess, "run", lambda *a, **kw: pytest.fail("No subprocess may run"))
    with pytest.raises(ValueError, match="Explicit restore workspace"):
        backup.main()


@pytest.mark.parametrize("separate_source", [False, True])
@pytest.mark.skipif(not Path("/usr/bin/sandbox-exec").is_file(), reason="Native macOS sandbox required")
def test_native_sealed_boot_helper_allows_own_copy_but_denies_sources(tmp_path, monkeypatch, separate_source):
    """Synthetic OPS layout only; no key, real archive, image or database is opened."""
    import os
    import subprocess
    import sys

    operator = tmp_path / "synthetic-operator"
    ops = operator / "Library/Application Support/Prism-Release/date"
    executor = ops / "compat-executor-synthetic"
    run = executor / "fresh-run"
    source_mount = run / ("source-mounted" if separate_source else "mounted")
    image = run / "acceptance-mounted/old-source-probe" if separate_source else source_mount
    area = image / "boot-verification"
    workspace = operator / "workspace"
    monkeypatch.setattr(boot, "__file__", str(executor / "verify_backup_boot.py"))
    denied = {
        "source_database_denied": source_mount / "recovery-databases/synthetic-db",
        "source_archive_denied": source_mount / "production.tar.gz",
        "whole_source_sibling_denied": source_mount / "other/synthetic.txt",
        "workspace_denied": workspace / "server/main.py",
        "credential_denied": operator / ".ssh/known_hosts",
        "original_backups_denied": operator / "Backups/Prism/synthetic.txt",
    }
    allowed = {
        "restored_source_readable": area / "app/server/main.py",
        "restored_database_readable": area / "data/chat.db",
    }
    write_probe = image / "outside-boot-write-probe"
    for path in (*denied.values(), *allowed.values(), write_probe):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text("synthetic-canary-only")
    assert not source_mount.is_relative_to(operator / "Backups")
    policy = area / "synthetic.sb"
    policy.write_text(
        boot.sandbox_policy(area, image, 12345, operator, workspace=workspace, source_mount=source_mount)
    )
    child = r"""
import errno,json,os,sys
from pathlib import Path
c=json.loads(sys.stdin.readline())
def denied(path,flags=os.O_RDONLY):
 try:fd=os.open(path,flags)
 except OSError as error:return error.errno in (errno.EPERM,errno.EACCES)
 else:os.close(fd);return False
checks={name:denied(path) for name,path in c['denied'].items()}
for name,path in c['allowed'].items():
 try:checks[name]=Path(path).read_text()=='synthetic-canary-only'
 except OSError:checks[name]=False
checks['outside_write_denied']=denied(c['write_probe'],os.O_WRONLY)
print(json.dumps(checks,sort_keys=True))
raise SystemExit(0 if all(checks.values()) else 3)
"""
    result = subprocess.run(
        ["/usr/bin/sandbox-exec", "-f", str(policy), sys.executable, "-I", "-B", "-c", child],
        input=json.dumps(
            {
                "denied": {k: str(v) for k, v in denied.items()},
                "allowed": {k: str(v) for k, v in allowed.items()},
                "write_probe": str(write_probe),
            }
        ).encode(),
        capture_output=True,
        timeout=15,
        env={"PATH": "/usr/bin:/bin", "HOME": str(operator), "LANG": "en_US.UTF-8"},
    )
    if result.returncode == 71 and result.stderr == b"sandbox-exec: sandbox_apply: Operation not permitted\n":
        if os.environ.get("PRISM_REQUIRE_NATIVE_SANDBOX") == "1":
            pytest.fail("native_sandbox_application_denied_by_outer_sandbox")
        pytest.skip("Outer sandbox blocks native application; no native acceptance claimed")
    assert result.returncode == 0, {
        "native_process_succeeded": result.returncode == 0,
        "stderr_empty": result.stderr == b"",
    }
    checks = json.loads(result.stdout)
    assert set(checks) == set(denied) | set(allowed) | {"outside_write_denied"}
    assert all(value is True for value in checks.values())
