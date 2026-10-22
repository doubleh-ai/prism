#!/usr/bin/env python3
"""Mac-only encrypted new/recovery/new database acceptance. Never runs on import.

This is not a deployment, a model test, or Linux compatibility evidence. Real
database bytes, subprocess logs and row hashes remain inside AES-256 images.
No production password, old session or environment file is used for login.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import importlib.util
import json
import os
import plistlib
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import tarfile
import time
from collections import Counter
from contextlib import closing, suppress
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from pathlib import Path, PurePosixPath

VENV = Path("/private/tmp/prism-dependency-audit-20261021/venv")
MAIN_DATABASE = "/home/ubuntu/stock-analyzer/data/chat.db"
PRIVATE_TABLES = {
    "users",
    "conversations",
    "messages",
    "saved_reports",
    "indicators",
    "vibe_plans",
    "indicator_scan_jobs",
    "analysis_runs",
    "analysis_cache",
    "user_watchlist",
    "owned_report_files",
    "analysis_nodes",
    "analysis_edges",
    "capability_calls",
    "capability_runs",
    "retired_usernames",
}


class PhaseFailure(RuntimeError):
    def __init__(self, details):
        super().__init__("isolated_phase_not_accepted")
        self.details = details


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()


def quoted(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def row_value(value):
    if isinstance(value, bytes):
        return ["bytes", base64.b64encode(value).decode()]
    return [type(value).__name__, value]


def history_snapshot(database, baseline=None, *, immutable=False):
    """Keep original common-column rows as a multiset, including duplicate rows."""
    result = {}
    uri = Path(database).as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.execute("PRAGMA temp_store=MEMORY")
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("database_integrity")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in sorted(tables):
            columns = [row[1] for row in db.execute("PRAGMA table_info(" + quoted(table) + ")")]
            if table == "sessions":
                continue
            if table == "capability_runs":
                columns = [name for name in columns if name != "revoked"]
            private = (
                table in PRIVATE_TABLES
                or table.startswith("research_")
                or bool({"username", "conv_id"} & set(columns))
            )
            if not private or table in {"activity_events", "rate_limits"}:
                continue
            if baseline is not None:
                if table not in baseline:
                    continue
                expected = baseline[table]["columns"]
                if not set(expected) <= set(columns):
                    raise ValueError("original_columns_missing")
                columns = expected
            hashes = Counter()
            for row in db.execute("SELECT " + ",".join(map(quoted, columns)) + " FROM " + quoted(table)):
                hashes[hashlib.sha256(encoded([row_value(v) for v in row])).hexdigest()] += 1
            result[table] = {"columns": columns, "rows": dict(hashes)}
        if baseline is not None and set(result) != set(baseline):
            raise ValueError("original_table_missing")
    return result


def seal_stopped_database(database):
    """Checkpoint only the isolated copy, after all application children stop."""
    with closing(sqlite3.connect(database)) as db:
        checkpoint = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if not checkpoint or checkpoint[0] != 0:
            raise ValueError("acceptance_database_checkpoint_busy")
    wal, shm = Path(str(database) + "-wal"), Path(str(database) + "-shm")
    checks = {
        "checkpoint_not_busy": checkpoint[0] == 0,
        "wal_absent_or_empty": not wal.exists() or wal.stat().st_size == 0,
        "shared_memory_absent": not shm.exists(),
    }
    if not all(checks.values()):
        raise ValueError("acceptance_database_sidecar_dependency")
    return checks


def preserved(baseline, current):
    return set(baseline) <= set(current) and all(
        current[table]["columns"] == original["columns"]
        and not (Counter(original["rows"]) - Counter(current[table]["rows"]))
        for table, original in baseline.items()
    )


def source_file(name):
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or path.as_posix() != name
        or ".." in path.parts
        or any(p.startswith(".") for p in path.parts)
    ):
        return False
    if "__pycache__" in path.parts or len(path.parts) < 2:
        return False
    return (
        (path.parts[0] == "server" and path.suffix == ".py")
        or (path.parts[0] == "templates" and path.suffix == ".html")
        or (
            path.parts[0] == "static"
            and path.suffix.lower()
            in {".js", ".css", ".svg", ".png", ".jpg", ".jpeg", ".ico", ".webp", ".woff", ".woff2", ".ttf"}
        )
        or (path.parts[0] == "skills" and path.name in {"CLAUDE.md", "SKILL.md"})
    )


def export_source(checkout, commit, target):
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("exact_source_commit_required")

    def git(*args):
        return subprocess.run(
            [
                "/usr/bin/git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "protocol.allow=never",
                "-c",
                "core.attributesFile=/dev/null",
                "-C",
                str(checkout),
                *args,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={
                "PATH": "/usr/bin:/bin",
                "LANG": "en_US.UTF-8",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_SYSTEM": "/dev/null",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_ATTR_NOSYSTEM": "1",
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
            },
        ).stdout

    actual = git("rev-parse", commit + "^{commit}").decode().strip()
    if len(commit) != 40 or actual != commit:
        raise ValueError("exact_source_commit_required")
    tree = git("rev-parse", commit + "^{tree}").decode().strip()
    target.mkdir(mode=0o700)
    files, total = {}, 0
    for entry in git("ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
        if not entry:
            continue
        header, raw_name = entry.split(b"\t", 1)
        name = raw_name.decode("utf-8")
        if not source_file(name):
            continue
        mode, kind, object_id = header.decode("ascii").split()
        if mode not in {"100644", "100755"} or kind != "blob" or name in files:
            raise ValueError("non_regular_source")
        content = git("cat-file", "blob", object_id)
        if hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest() != object_id:
            raise ValueError("source_blob_mismatch")
        total += len(content)
        if len(content) > 64 * 1024 * 1024 or total > 256 * 1024 * 1024:
            raise ValueError("source_limits")
        path = target / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open("xb") as dest:
            dest.write(content)
        path.chmod(0o600)
        files[name] = digest(path)
    if not {"server/auth.py", "server/config.py", "server/database.py"} <= files.keys():
        raise ValueError("source_incomplete")
    return {
        "commit": actual,
        "tree": tree,
        "source_sha256": hashlib.sha256(encoded(files)).hexdigest(),
        "files": files,
    }


def sandbox_policy(area, image, port, operator_home, workspace, *, source_mount):
    def q(value):
        return json.dumps(str(value))

    def outside_reads(tree):
        # SQLite canonicalizes every ancestor before opening its database.
        # Permit metadata for that exact chain, never directory listings or
        # file contents outside the private acceptance subtree.
        scope = f"(subpath {q(tree)}) (require-not (subpath {q(area)}))"
        ancestors = " ".join(f"(require-not (literal {q(parent)}))" for parent in area.parents)
        return (
            f"(deny file-read-data (require-all {scope}))\n"
            f"(deny file-read-metadata (require-all {scope} {ancestors}))\n"
        )

    denied = [
        operator_home / name for name in (".ssh", ".aws", ".claude", ".config", ".codex", "Library/Keychains")
    ]
    denied += [workspace]
    return (
        "(version 1)\n(allow default)\n(deny network*)\n"
        f'(allow network-bind (local ip "localhost:{port}"))\n'
        f'(allow network-inbound (local ip "localhost:{port}"))\n'
        f'(deny file-write* (require-all (require-not (subpath {q(area)})) (require-not (literal "/dev/null"))))\n'
        f"(deny file-write* (subpath {q(area / 'new-app')}) (subpath {q(area / 'recovery-app')}))\n"
        "(deny file-read* "
        + " ".join("(subpath " + q(path) + ")" for path in denied)
        + f" (literal {q(operator_home / '.claude.json')}))\n"
        + outside_reads(operator_home / "Backups")
        + outside_reads(source_mount)
        + outside_reads(image)
    )


def environment(area, port):
    return {
        "PATH": str(VENV / "bin") + ":/usr/bin:/bin",
        "HOME": str(area / "home"),
        "TMPDIR": str(area / "tmp"),
        "LANG": "en_US.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "STOCK_DATA_DIR": str(area / "data"),
        "STOCK_LOG_FILE": str(area / "logs/app.log"),
        "PRISM_LOG_LEVEL": "WARNING",
        "CLAUDE_BIN": str(area / "disabled-claude"),
        "CLAUDE_SKILLS_HOME": str(area / "home/skills"),
        "CLAUDE_SKILL_FALLBACKS": str(area / "home/fallback-skills"),
        "PRISM_ASTOCK_PROJECT": str(area / "disabled-astock"),
        "PRISM_VIBE_ENGINE_URL": "http://127.0.0.1:9",
        "PRISM_MARKET_PROVIDERS": "",
        "PRISM_INTERNAL_API_URL": f"http://127.0.0.1:{port}",
        "PRISM_COOKIE_SECURE": "0",
        "PRISM_ENVIRONMENT": "local",
    }


BOOTSTRAP = r"""
import errno,hashlib,importlib,json,os,socket,sqlite3,sys
from pathlib import Path
c=json.loads(sys.stdin.readline())
def blocked(call):
 try:
  item=call()
  if isinstance(item,int):os.close(item)
  elif hasattr(item,'close'):item.close()
 except PermissionError:return True
 except OSError as e:return e.errno in (errno.EPERM,errno.EACCES)
 return False
checks={
 'outbound_denied':blocked(lambda:socket.create_connection(('127.0.0.1',9),timeout=1)),
 'credential_reads_denied':blocked(lambda:os.open(c['credential_probe'],os.O_RDONLY)),
 'workspace_reads_denied':blocked(lambda:os.open(c['workspace_probe'],os.O_RDONLY)),
 'outside_writes_denied':blocked(lambda:os.open(c['write_probe'],os.O_WRONLY)),
 'source_backup_reads_denied':blocked(lambda:os.open(c['backup_probe'],os.O_RDONLY)),
 'source_writes_denied':blocked(lambda:os.open(c['source_write_probe'],os.O_WRONLY)),
}
if not all(checks.values()):raise RuntimeError('isolation_probe_failed')
if c['seed']:
 import bcrypt
 with sqlite3.connect(c['database']) as db:
  db.execute('INSERT INTO users(username,password_hash) VALUES(?,?)',[c['username'],bcrypt.hashpw(c['password'].encode(),bcrypt.gensalt()).decode()])
  db.execute("INSERT INTO conversations(id,username,title,skill) VALUES(?,?,?,'prism-research')",[c['old_conv'],c['username'],'compatibility old history'])
  db.execute("INSERT INTO messages(conv_id,role,content) VALUES(?,'assistant',?)",[c['old_conv'],'compatibility legacy narrative'])
  db.execute("INSERT INTO saved_reports(id,username,conv_id,title,report_json,created_at,updated_at) VALUES(?,?,?,?,?,1,1)",[c['old_report'],c['username'],c['old_conv'],'compatibility legacy report',json.dumps({'summary':'compatibility legacy narrative'})])
  db.execute('INSERT INTO users(username,password_hash) VALUES(?,?)',[c['other_username'],bcrypt.hashpw(c['password'].encode(),bcrypt.gensalt()).decode()])
  db.execute("INSERT INTO conversations(id,username,title,skill) VALUES(?,?,?,'prism-research')",[c['other_conv'],c['other_username'],'compatibility foreign history'])
  db.execute("INSERT INTO saved_reports(id,username,conv_id,title,report_json,created_at,updated_at) VALUES(?,?,?,?,?,1,1)",[c['other_report'],c['other_username'],c['other_conv'],'compatibility foreign report',json.dumps({'summary':'compatibility foreign narrative'})])
module=importlib.import_module(c['module'])
app=module.app
identities={}
if c['seed']:
 from server.database import create_user,delete_user_account,get_db
 from server.research import create_research_run,finalize_research_run
 from server.research_context import build_research_context
 from server.reports import save_report_snapshot
 with get_db() as db:
  db.execute("INSERT INTO conversations(id,username,title,skill) VALUES(?,?,?,'prism-research')",[c['new_conv'],c['username'],'compatibility new history'])
  db.execute("INSERT INTO messages(conv_id,role,content) VALUES(?,'assistant',?)",[c['new_conv'],'compatibility current narrative'])
 context=build_research_context(engine='compatibility')
 create_research_run(c['username'],c['new_conv'],'compatibility fixture',context)
 revision=finalize_research_run(c['username'],context['run_id'],'compatibility current narrative')
 report=save_report_snapshot(c['username'],{'source':'chat','source_id':context['run_id'],'conv_id':c['new_conv'],'title':'compatibility current report','report':{'final_decision':'compatibility current narrative','run_id':context['run_id'],'research_revision_id':revision['revision_id']}})
 identities={'new_report':report['id'],'run_id':context['run_id'],'revision_id':revision['revision_id']}
 create_user(c['retired_username'],'synthetic unusable hash')
 delete_user_account(c['retired_username'])
from fastapi import HTTPException
from server.database import create_user,get_db
try:create_user(c['retired_username'],'synthetic unusable hash')
except HTTPException as error:checks['retired_business_reclaim_denied']=error.status_code==409
else:checks['retired_business_reclaim_denied']=False
try:
 with get_db() as db:db.execute('INSERT INTO users(username,password_hash) VALUES(?,?)',[c['retired_username'],'synthetic unusable hash'])
except sqlite3.IntegrityError:checks['retired_legacy_sql_reclaim_denied']=True
else:checks['retired_legacy_sql_reclaim_denied']=False
if not all(checks.values()):raise RuntimeError('tombstone_compatibility_failed')
@app.get('/__compatibility_probe/'+c['nonce'])
async def probe():return {'nonce':c['nonce'],'pid':os.getpid(),'checks':checks,'identities':identities,'module_sha256':hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()}
import uvicorn
uvicorn.run(app,host='127.0.0.1',port=c['port'],workers=1,log_level='warning',access_log=False)
"""


def request(port, method, path, body=None, cookie=""):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    headers = {"X-Requested-With": "XMLHttpRequest"}
    if cookie:
        headers["Cookie"] = cookie
    payload = None if body is None else encoded(body)
    if payload is not None:
        headers["Content-Type"] = "application/json"
    try:
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("response_limit")
        cookies = SimpleCookie()
        for name, value in response.getheaders():
            if name.lower() == "set-cookie":
                cookies.load(value)
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            data = {}
        return response.status, data, cookies
    finally:
        connection.close()


def stop(process):
    if process is None:
        return
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


def run_phase(label, area, image, app, configuration, workspace, identities, *, source_mount):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    c = {
        **configuration,
        "port": port,
        "nonce": secrets.token_hex(24),
        "seed": label == "new_first",
        "module": "server.recovery" if label == "recovery" else "server.main",
        "source_write_probe": str(app / "server" / ("recovery.py" if label == "recovery" else "main.py")),
    }
    policy = area / (label + ".sb")
    policy.write_text(sandbox_policy(area, image, port, Path.home(), workspace, source_mount=source_mount))
    process, checks, diagnostic = None, {}, {}
    expected_module = digest(app / "server" / ("recovery.py" if label == "recovery" else "main.py"))
    try:
        with (area / "logs" / (label + ".log")).open("wb") as log:
            process = subprocess.Popen(
                ["/usr/bin/sandbox-exec", "-f", str(policy), str(VENV / "bin/python"), "-c", BOOTSTRAP],
                cwd=app,
                env=environment(area, port),
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            process.stdin.write(encoded(c) + b"\n")
            process.stdin.close()
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    status, probe, _ = request(port, "GET", "/__compatibility_probe/" + c["nonce"])
                    diagnostic = {
                        "http_status": status,
                        "nonce_matches": probe.get("nonce") == c["nonce"],
                        "pid_matches": probe.get("pid") == process.pid,
                        "module_hash_matches": probe.get("module_sha256") == expected_module,
                        "child_checks": {
                            k: v for k, v in probe.get("checks", {}).items() if isinstance(v, bool)
                        },
                    }
                    if (
                        status == 200
                        and probe.get("nonce") == c["nonce"]
                        and probe.get("pid") == process.pid
                        and probe.get("module_sha256") == expected_module
                        and all(probe.get("checks", {}).values())
                        and len(probe.get("checks", {})) == 8
                    ):
                        break
                except (OSError, ValueError, http.client.HTTPException):
                    pass
                time.sleep(0.15)
            else:
                raise PhaseFailure(
                    {"startup_probe": diagnostic, "process_exited": process.poll() is not None}
                )
            identities.update(probe["identities"])
            checks = {
                "isolated_pid_nonce_and_source": True,
                "tombstone_business_and_sql_denied": True,
                "anonymous_denied": request(port, "GET", "/api/me")[0] == 401,
            }
            status, _, cookies = request(
                port, "POST", "/api/login", {"username": c["username"], "password": c["password"]}
            )
            if status != 200 or "token" not in cookies:
                raise PhaseFailure({"login_http_status": status, "login_cookie_present": "token" in cookies})
            cookie = "token=" + cookies["token"].value
            status, profile, _ = request(port, "GET", "/api/me", cookie=cookie)
            checks["real_login_and_owner"] = status == 200 and profile.get("user") == c["username"]
            for name in ("old_conv", "new_conv"):
                status, content, _ = request(port, "GET", "/api/convs/" + c[name], cookie=cookie)
                expected = (
                    "compatibility legacy narrative"
                    if name == "old_conv"
                    else "compatibility current narrative"
                )
                checks[name + "_read"] = (
                    status == 200 and bool(content.get("messages")) and expected in json.dumps(content)
                )
            for name in ("old_report", "new_report"):
                report_id = c[name] if name in c else identities[name]
                status, content, _ = request(port, "GET", "/api/reports/library/" + report_id, cookie=cookie)
                expected = (
                    "compatibility legacy narrative"
                    if name == "old_report"
                    else "compatibility current narrative"
                )
                checks[name + "_read"] = (
                    status == 200
                    and content.get("id") == report_id
                    and bool(content.get("report"))
                    and expected in json.dumps(content)
                )
            checks["foreign_conversation_denied"] = (
                request(port, "GET", "/api/convs/" + c["other_conv"], cookie=cookie)[0] == 404
            )
            checks["foreign_report_denied"] = (
                request(port, "GET", "/api/reports/library/" + c["other_report"], cookie=cookie)[0] == 404
            )
            if label == "recovery":
                for method, path in (
                    ("POST", "/api/convs"),
                    ("POST", "/api/chat/stream"),
                    ("DELETE", "/api/reports/library/" + c["old_report"]),
                    ("PUT", "/api/watchlist"),
                ):
                    checks["write_" + method + "_" + path.split("/")[2]] = (
                        request(port, method, path, {}, cookie)[0] == 503
                    )
                checks["shared_file_denied"] = request(
                    port, "GET", "/api/reports/" + c["old_conv"] + "/legacy-shared.html", cookie=cookie
                )[0] in {403, 404}
            else:
                status, research, _ = request(
                    port, "GET", "/api/research/runs/" + identities["run_id"], cookie=cookie
                )
                checks["new_ledger_revision_read"] = status == 200 and any(
                    row.get("revision_id") == identities["revision_id"]
                    for row in research.get("report_revisions", [])
                )
                status, created, _ = request(port, "POST", "/api/convs", {}, cookie)
                checks["new_empty_conversation_write"] = status == 200 and isinstance(created.get("id"), str)
            if not all(checks.values()):
                raise PhaseFailure({"http_checks": checks})
            return checks
    finally:
        stop(process)
        checks["process_stopped"] = process is None or process.poll() is not None


def backup_boot_diagnostic(mount):
    """Return booleans/error class only; do not copy or print the private log."""
    result = {}
    path = mount / "boot-verification/result.json"
    if path.is_file():
        value = json.loads(path.read_text())
        result["recorded_ok"] = value.get("ok") is True
        result["checks"] = {
            key: item for key, item in value.get("checks", {}).items() if isinstance(item, bool)
        }
        error = value.get("error_type")
        if isinstance(error, str) and error.isidentifier():
            result["error_type"] = error
    log = mount / "boot-verification/logs/process.log"
    if log.is_file():
        with log.open("rb") as stream:
            stream.seek(max(log.stat().st_size - 32768, 0))
            tail = stream.read(32768).decode("utf-8", errors="replace")
        result["log_exception_classes"] = list(
            dict.fromkeys(re.findall(r"(?m)^([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception)):", tail))
        )
        result["log_source_frames"] = [
            {"file": Path(name).name, "line": int(line)}
            for name, line in re.findall(r'File "([^"\n]+)", line (\d+)', tail)
            if re.fullmatch(r"[A-Za-z0-9_]+\.py", Path(name).name)
        ][-8:]
    return result


def verify_original_boot(archive, recovery, mount, manifest, archive_sha256):
    """Run the reviewed source-restoration helper in a separate encrypted copy."""
    helper = Path(__file__).with_name("verify_backup_boot.py")
    spec = importlib.util.spec_from_file_location("prism_original_boot_probe", helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    area = mount / "old-source-probe"
    area.mkdir(mode=0o700, exist_ok=False)
    result = module.verify_boot(archive, recovery, area, manifest, archive_sha256)
    return {"verifier_sha256": digest(helper), **result}


def hdi(*arguments, password=None):
    return subprocess.run(
        ["/usr/bin/hdiutil", *map(str, arguments)],
        input=password,
        check=True,
        capture_output=True,
    ).stdout


def encrypted(value):
    return (
        isinstance(value, dict)
        and any((str(k).lower() == "encrypted" and v is True) or encrypted(v) for k, v in value.items())
        or isinstance(value, list)
        and any(map(encrypted, value))
    )


def no_git(path):
    if any((parent / ".git").exists() for parent in (path, *path.parents)):
        raise ValueError("acceptance_area_must_be_outside_git")


def source_image(backup, value):
    path = Path(value)
    path = (path if path.is_absolute() else backup / path).resolve()
    if not path.is_relative_to(backup.resolve()) or not path.is_dir():
        raise ValueError("source_image_scope")
    return path


def mounted(path):
    def contains(value):
        if isinstance(value, dict):
            return value.get("mount-point") == str(path) or any(contains(v) for v in value.values())
        return isinstance(value, list) and any(contains(v) for v in value)

    return contains(plistlib.loads(hdi("info", "-plist")))


def verification_exit_code(result, *, original_boot_requested):
    """A completed process is not acceptance of failed requested checks."""
    passed = result.get("mac_compatibility_verified") is True and (
        not original_boot_requested or result.get("original_application_restore_verified") is True
    )
    return 0 if passed else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--new-source", type=Path, required=True)
    parser.add_argument("--new-commit", required=True)
    parser.add_argument("--recovery-source", type=Path, required=True)
    parser.add_argument("--recovery-commit", required=True)
    parser.add_argument("--output-root", type=Path, default=Path.home() / "Backups/Prism")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--verify-original-boot", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print(
            json.dumps({"executed": False, "requires_explicit_execute": True, "data_remains_encrypted": True})
        )
        return
    if os.uname().sysname != "Darwin" or not (VENV / "bin/python").is_file():
        raise ValueError("audited_mac_runtime_required")
    backup = args.backup_dir.resolve()
    no_git(backup)
    no_git(args.output_root.resolve())
    report = json.loads((backup / "BACKUP-REPORT.json").read_text())
    if report.get("status") != "verified and unmounted" or report.get("sqlite_restore_verified") is not True:
        raise ValueError("verified_consistent_backup_required")
    image_source = source_image(backup, report["backup_image"])
    run = datetime.now(UTC).strftime("%Y-%m-%d-prism-db-compatibility-%H%M%S-") + secrets.token_hex(4)
    folder = args.output_root / run
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    source_mount, mount = folder / "source-mounted", folder / "acceptance-mounted"
    source_mount.mkdir(mode=0o700)
    mount.mkdir(mode=0o700)
    image = folder / (run + ".sparsebundle")
    result = {
        "kind": "actual_mac_database_compatibility",
        "mac_compatibility_verified": False,
        "linux_execution_verified": False,
        "live_models_tested": False,
        "production_untouched": True,
        "release_gate_ready": False,
        "original_sessions_reused": False,
        "original_application_restore_requested": args.verify_original_boot,
        "original_application_restore_verified": False,
        "verifier_sha256": digest(Path(__file__)),
        "source_backup_report_sha256": digest(backup / "BACKUP-REPORT.json"),
    }
    password, source_attached, attached = None, False, False
    archive, original, archive_before, database_before = None, None, None, None
    try:
        password = (
            subprocess.run(
                [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-s",
                    report["keychain_service"],
                    "-a",
                    report["keychain_account"],
                    "-w",
                ],
                check=True,
                capture_output=True,
            ).stdout.rstrip(b"\n")
            + b"\0"
        )
        result["stage"] = "mount_readonly_source"
        hdi(
            "attach",
            "-readonly",
            "-stdinpass",
            "-nobrowse",
            "-mountpoint",
            source_mount,
            image_source,
            password=password,
        )
        source_attached = True
        result["source_boot_diagnostic"] = backup_boot_diagnostic(source_mount)
        archive = source_mount / "production.tar.gz"
        archive_before = digest(archive)
        result["source_archive_sha256"] = archive_before
        if archive_before != report["archive_sha256"]:
            raise ValueError("source_archive_hash")
        with tarfile.open(archive, "r:gz") as package:
            member = package.getmember("backup-manifest.json")
            if not member.isfile() or member.size > 32 * 1024 * 1024:
                raise ValueError("manifest_scope")
            manifest = json.load(package.extractfile(member))
        db_entry = next(m for m in manifest["databases"] if m["original"] == MAIN_DATABASE)
        db_name = PurePosixPath(db_entry["path"])
        if (
            len(db_name.parts) != 2
            or db_name.parts[0] != "consistent-databases"
            or not db_name.name.isdigit()
        ):
            raise ValueError("consistent_database_scope")
        original = source_mount / "recovery-databases" / db_name.name
        database_before = digest(original)
        result["source_consistent_database_sha256"] = database_before
        expected = next(m["sha256"] for m in manifest["files"] if m["path"] == db_entry["path"])
        if database_before != expected:
            raise ValueError("consistent_database_hash")
        result["stage"] = "create_encrypted_acceptance"
        hdi(
            "create",
            "-size",
            "8g",
            "-type",
            "SPARSEBUNDLE",
            "-fs",
            "APFS",
            "-volname",
            "Prism compatibility acceptance",
            "-encryption",
            "AES-256",
            "-stdinpass",
            image,
            password=password,
        )
        result["acceptance_image_encrypted"] = encrypted(
            plistlib.loads(hdi("imageinfo", "-stdinpass", "-plist", image, password=password))
        )
        if not result["acceptance_image_encrypted"]:
            raise ValueError("encryption_not_confirmed")
        hdi("attach", "-stdinpass", "-nobrowse", "-mountpoint", mount, image, password=password)
        attached = True
        area = mount / "acceptance"
        area.mkdir(mode=0o700)
        for name in ("data", "home", "tmp", "logs"):
            (area / name).mkdir(mode=0o700)
        database = area / "data/chat.db"
        shutil.copyfile(original, database)
        database.chmod(0o600)
        baseline = history_snapshot(database)
        (area / "original-history-hashes.json").write_bytes(encoded(baseline))
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("PRAGMA temp_store=MEMORY")
            result["copied_sessions_revoked"] = db.execute("DELETE FROM sessions").rowcount
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='capability_runs'"
            ).fetchone():
                result["copied_capability_grants_revoked"] = db.execute(
                    "UPDATE capability_runs SET revoked=1 WHERE revoked=0"
                ).rowcount
        new = export_source(args.new_source, args.new_commit, area / "new-app")
        recovery = export_source(args.recovery_source, args.recovery_commit, area / "recovery-app")
        result["sources"] = {
            "new": {k: v for k, v in new.items() if k != "files"},
            "recovery": {k: v for k, v in recovery.items() if k != "files"},
        }
        configuration = {
            "username": "compat_probe_" + secrets.token_hex(12),
            "other_username": "compat_other_" + secrets.token_hex(12),
            "retired_username": "compat_retired_" + secrets.token_hex(12),
            "password": secrets.token_urlsafe(32),
            "old_conv": secrets.token_hex(12),
            "new_conv": secrets.token_hex(12),
            "old_report": secrets.token_hex(12),
            "other_conv": secrets.token_hex(12),
            "other_report": secrets.token_hex(12),
            "database": str(database),
            "credential_probe": str(Path.home() / ".ssh/known_hosts"),
            "workspace_probe": str(args.new_source / "server/main.py"),
            "write_probe": str(mount / "outside-write-probe"),
            "backup_probe": str(original),
        }
        (mount / "outside-write-probe").write_text("unchanged")
        identities, stages, accumulated = {}, {}, None
        result["phase_rows_sha256"], result["phase_checkpoints"] = {}, {}
        for label, app in (
            ("new_first", area / "new-app"),
            ("recovery", area / "recovery-app"),
            ("new_return", area / "new-app"),
        ):
            result["stage"] = label
            stages[label] = run_phase(
                label,
                area,
                mount,
                app,
                configuration,
                args.new_source.resolve().parent,
                identities,
                source_mount=source_mount,
            )
            result["phase_checkpoints"][label] = seal_stopped_database(database)
            stages[label]["database_checkpointed"] = all(result["phase_checkpoints"][label].values())
            snapshot = history_snapshot(database, baseline, immutable=True)
            if not preserved(baseline, snapshot):
                raise ValueError("original_history_not_preserved")
            stages[label]["original_rows_preserved"] = True
            if accumulated is not None and not preserved(
                accumulated, history_snapshot(database, accumulated, immutable=True)
            ):
                raise ValueError("accumulated_business_history_not_preserved")
            stages[label]["cumulative_business_rows_preserved"] = True
            accumulated = history_snapshot(database, immutable=True)
            (area / (label + "-history-hashes.json")).write_bytes(encoded(accumulated))
            result["phase_rows_sha256"][label] = hashlib.sha256(encoded(accumulated)).hexdigest()
        result["phases"] = stages
        if args.verify_original_boot:
            result["stage"] = "verify_original_production_boot"
            result["original_boot_verification"] = verify_original_boot(
                archive, source_mount / "recovery-databases", mount, manifest, archive_before
            )
            result["original_application_restore_verified"] = (
                result["original_boot_verification"].get("ok") is True
            )
        result["original_row_counts"] = {
            name: sum(value["rows"].values()) for name, value in baseline.items()
        }
        result["preservation_scope"] = {
            "common_column_multiset": True,
            "existing_tombstones_included": True,
            "copied_sessions_are_revoked": True,
            "copied_capability_revoked_column_is_exempt": True,
            "operational_activity_and_rate_limit_rows_are_exempt": True,
        }
        result["original_rows_sha256"] = hashlib.sha256(encoded(baseline)).hexdigest()
        result["source_files_unchanged"] = all(
            digest(area / name / path) == value
            for name, entry in (("new-app", new), ("recovery-app", recovery))
            for path, value in entry["files"].items()
        )
        result["original_archive_unchanged"] = digest(archive) == archive_before
        result["original_consistent_database_unchanged"] = digest(original) == database_before
        result["outside_probe_unchanged"] = (mount / "outside-write-probe").read_text() == "unchanged"
        result["stage"] = "seal_stopped_database"
        result["sealed_database"] = seal_stopped_database(database)
        sealed = history_snapshot(database, immutable=True)
        (area / "sealed-history-hashes.json").write_bytes(encoded(sealed))
        result["sealed_rows_sha256"] = hashlib.sha256(encoded(sealed)).hexdigest()
        result["working_database_sha256"] = digest(database)
        (area / "acceptance-result.json").write_bytes(encoded(result))
        hdi("detach", mount)
        attached = False
        hdi("attach", "-readonly", "-stdinpass", "-nobrowse", "-mountpoint", mount, image, password=password)
        attached = True
        result["stage"] = "verify_readonly_acceptance"
        result["acceptance_readonly_recheck"] = digest(database) == result[
            "working_database_sha256"
        ] and preserved(baseline, history_snapshot(database, baseline, immutable=True))
        result["sealed_original_and_new_rows_unchanged"] = (
            history_snapshot(database, immutable=True) == sealed
            and (
                not Path(str(database) + "-wal").exists() or Path(str(database) + "-wal").stat().st_size == 0
            )
            and not Path(str(database) + "-shm").exists()
        )
        result["mac_compatibility_verified"] = all(
            result.get(k) is True
            for k in (
                "acceptance_image_encrypted",
                "source_files_unchanged",
                "original_archive_unchanged",
                "original_consistent_database_unchanged",
                "outside_probe_unchanged",
                "acceptance_readonly_recheck",
                "sealed_original_and_new_rows_unchanged",
            )
        ) and all(all(phase.values()) for phase in stages.values())
        result["stage"] = "verified_mac_database_compatibility"
    except Exception as error:
        result["error_type"] = type(error).__name__
        if isinstance(error, PhaseFailure):
            result["failure_diagnostic"] = error.details
    finally:
        if source_attached:
            with suppress(Exception):
                result["original_archive_unchanged"] = bool(
                    archive_before and digest(archive) == archive_before
                )
                result["original_consistent_database_unchanged"] = bool(
                    database_before and digest(original) == database_before
                )
        if attached:
            with suppress(Exception):
                hdi("detach", mount)
        if source_attached:
            with suppress(Exception):
                hdi("detach", source_mount)
        with suppress(Exception):
            result["acceptance_unmounted"] = not mounted(mount)
            result["source_unmounted"] = not mounted(source_mount)
        result["mac_compatibility_verified"] = result["mac_compatibility_verified"] and all(
            result.get(k) is True
            for k in (
                "acceptance_unmounted",
                "source_unmounted",
                "original_archive_unchanged",
                "original_consistent_database_unchanged",
            )
        )
        password = None
        (folder / "COMPATIBILITY-REPORT.json").write_bytes(encoded(result))
        print(
            json.dumps(
                {
                    "mac_compatibility_verified": result["mac_compatibility_verified"],
                    "linux_execution_verified": False,
                    "receipt": str(folder / "COMPATIBILITY-REPORT.json"),
                    "error_type": result.get("error_type"),
                }
            )
        )
    exit_code = verification_exit_code(result, original_boot_requested=args.verify_original_boot)
    if exit_code:
        # Cleanup and the failure receipt above must finish before a caller
        # observes a failed process; neither output nor exit status grants Linux acceptance.
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
