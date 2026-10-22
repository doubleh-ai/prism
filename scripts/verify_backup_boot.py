#!/usr/bin/env python3
"""Boot a restored Prism copy inside its encrypted image, without live engines.

This module never mounts images, reads Keychain, installs packages or touches a
production service. The capture controller must already have verified the tar
and restored consistent databases. All evidence and logs stay in the image.
"""

from __future__ import annotations

import ast
import hashlib
import http.client
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import tarfile
import time
from contextlib import suppress
from http.cookies import SimpleCookie
from pathlib import Path, PurePosixPath

VENV = Path("/private/tmp/prism-dependency-audit-20261021/venv")
PREFIX = "snapshot/home/ubuntu/stock-analyzer/"
MAIN_DATABASE = "/home/ubuntu/stock-analyzer/data/chat.db"
MAX_CODE_FILE = 64 * 1024 * 1024
MAX_CODE_TOTAL = 256 * 1024 * 1024
ROOT_FILES = {"CHANGELOG.md", "requirements.txt", "requirements-market.txt", "pyproject.toml"}


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_file(name: str) -> bool:
    """Positive source allowlist; no env, Git history or runtime artifacts."""
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != name:
        return False
    if any(part.startswith(".") for part in path.parts) or "__pycache__" in path.parts:
        return False
    if name in ROOT_FILES:
        return True
    if len(path.parts) < 2:
        return False
    if path.parts[0] == "server":
        return path.suffix == ".py"
    if path.parts[0] == "templates":
        return path.suffix == ".html"
    if path.parts[0] == "static":
        return path.suffix.lower() in {
            ".js",
            ".css",
            ".svg",
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".ico",
            ".woff",
            ".woff2",
            ".ttf",
        }
    if path.parts[0] == "skills":
        return path.name in {"CLAUDE.md", "SKILL.md"}
    return False


def restore_sources(archive_path: Path, app: Path, manifest: dict) -> dict:
    """Write verified regular source bytes only; never extract archive links."""
    expected = {entry["path"]: entry for entry in manifest["files"] if entry.get("type") == "file"}
    written, total = {}, 0
    app.mkdir(mode=0o700, parents=True, exist_ok=False)
    with tarfile.open(archive_path, "r:gz") as archive:
        for item in archive:
            if not item.name.startswith(PREFIX):
                continue
            relative = item.name[len(PREFIX) :]
            if not relative or not source_file(relative):
                continue
            if not item.isfile() or item.name not in expected or relative in written:
                raise ValueError("Boot source must be a unique verified regular file")
            total += item.size
            if item.size > MAX_CODE_FILE or total > MAX_CODE_TOTAL:
                raise ValueError("Boot source exceeds extraction resource limits")
            target = app / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if target.is_symlink():
                raise ValueError("Boot target must not be a link")
            with archive.extractfile(item) as source, target.open("xb") as dest:
                shutil.copyfileobj(source, dest)
            os.chmod(target, 0o600)
            if (
                target.stat().st_size != expected[item.name]["size"]
                or _digest(target) != expected[item.name]["sha256"]
            ):
                raise ValueError("Boot source differs from its receipt")
            written[relative] = expected[item.name]["sha256"]
    if not {"server/main.py", "server/auth.py", "server/database.py", "server/config.py"} <= written.keys():
        raise ValueError("Boot application source is incomplete")
    return written


def sandbox_policy(
    boot: Path, image: Path, port: int, operator_home: Path, *, workspace: Path, source_mount: Path
) -> str:
    def quoted(path):
        return json.dumps(str(path))

    def outside_reads(tree):
        # SQLite resolves each ancestor before opening its private database.
        # Allow only metadata on that exact chain, never external file data.
        scope = f"(subpath {quoted(tree)}) (require-not (subpath {quoted(boot)}))"
        ancestors = " ".join(f"(require-not (literal {quoted(parent)}))" for parent in boot.parents)
        return (
            f"(deny file-read-data (require-all {scope}))\n"
            f"(deny file-read-metadata (require-all {scope} {ancestors}))\n"
        )

    protected = [
        operator_home / ".ssh",
        operator_home / ".aws",
        operator_home / ".claude",
        operator_home / ".config",
        operator_home / ".codex",
        workspace,
        operator_home / "Library/Keychains",
    ]
    reads = " ".join("(subpath " + quoted(path) + ")" for path in protected)
    return (
        "(version 1)\n(allow default)\n(deny network*)\n"
        f'(allow network-bind (local ip "localhost:{port}"))\n'
        f'(allow network-inbound (local ip "localhost:{port}"))\n'
        f'(deny file-write* (require-all (require-not (subpath {quoted(boot)})) (require-not (literal "/dev/null"))))\n'
        f"(deny file-read* {reads} (literal {quoted(operator_home / '.claude.json')}))\n"
        + outside_reads(operator_home / "Backups")
        + outside_reads(source_mount)
        + outside_reads(image)
    )


def boot_environment(boot: Path, port: int) -> dict:
    return {
        "PATH": str(VENV / "bin") + ":/usr/bin:/bin",
        "HOME": str(boot / "home"),
        "TMPDIR": str(boot / "tmp"),
        "LANG": "en_US.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "STOCK_DATA_DIR": str(boot / "data"),
        "STOCK_LOG_FILE": str(boot / "logs/app.log"),
        "PRISM_LOG_LEVEL": "WARNING",
        "CLAUDE_BIN": str(boot / "disabled-claude"),
        "CLAUDE_SKILLS_HOME": str(boot / "home/skills"),
        "CLAUDE_SKILL_FALLBACKS": str(boot / "home/fallback-skills"),
        "PRISM_ASTOCK_PROJECT": str(boot / "disabled-astock"),
        "PRISM_VIBE_ENGINE_URL": "http://127.0.0.1:9",
        "PRISM_MARKET_PROVIDERS": "",
        "PRISM_INTERNAL_API_URL": f"http://127.0.0.1:{port}",
        "PRISM_COOKIE_SECURE": "0",
        "PRISM_SINGLE_WORKER": "1",
        "PRISM_ENVIRONMENT": "local",
    }


BOOTSTRAP = r"""
import errno,json,os,socket,sys
from pathlib import Path
configuration=json.loads(sys.stdin.readline())
def blocked(call):
 try:
  descriptor=call()
  if isinstance(descriptor,int):os.close(descriptor)
  elif hasattr(descriptor,'close'):descriptor.close()
 except PermissionError:return True
 except OSError as error:return error.errno in (errno.EPERM,errno.EACCES)
 return False
checks={
 'outbound_denied':blocked(lambda:socket.create_connection(('127.0.0.1',9),timeout=1)),
 'credential_reads_denied':blocked(lambda:os.open(configuration['credential_probe'],os.O_RDONLY)),
 'outside_writes_denied':blocked(lambda:os.open(configuration['write_probe'],os.O_WRONLY)),
}
if not all(checks.values()):raise RuntimeError('isolation probe failed')
from server.main import app
from server.database import create_user,user_exists
from server.auth import hash_password
if user_exists(configuration['username']):raise RuntimeError('probe account collision')
create_user(configuration['username'],hash_password(configuration['password']))
@app.get('/__restore_probe/'+configuration['nonce'])
async def probe():return {'nonce':configuration['nonce'],'pid':os.getpid(),'checks':checks}
import uvicorn
uvicorn.run(app,host='127.0.0.1',port=configuration['port'],workers=1,log_level='warning',access_log=False)
"""


def _request(
    port: int, method: str, path: str, body: dict | None = None, cookie: str = ""
) -> tuple[int, dict, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    headers = {"X-Requested-With": "XMLHttpRequest"}
    if cookie:
        headers["Cookie"] = cookie
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    try:
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Restore probe response too large")
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            result = {}
        response_headers = response.getheaders()
        headers = {key: value for key, value in response_headers if key.lower() != "set-cookie"}
        # Login sets both the session and CSRF cookies. A dict of header pairs
        # silently discarded the earlier session cookie in the actual backup.
        cookies = [value for key, value in response_headers if key.lower() == "set-cookie"]
        if cookies:
            headers["set-cookie"] = "\n".join(cookies)
        return response.status, result, headers
    finally:
        connection.close()


def _expected_version(app: Path) -> str | None:
    path = app / "server/release.py"
    if not path.is_file():
        return None
    for node in ast.parse(path.read_text()).body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "APP_VERSION" for target in node.targets)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    return None


def profile_matches_owner(status: int, profile: dict, expected: str) -> bool:
    # The existing /api/me contract uses "user", not "username".
    return status == 200 and isinstance(profile, dict) and profile.get("user") == expected


def startup_diagnostic(status: int, response, nonce: str, pid: int) -> dict:
    """Keep only fixed booleans/status; never retain an HTTP body or its text."""
    body = response if isinstance(response, dict) else {}
    raw_checks = body.get("checks") if isinstance(body.get("checks"), dict) else {}
    return {
        "http_status": status,
        "nonce_matches": body.get("nonce") == nonce,
        "pid_matches": body.get("pid") == pid,
        "isolation_checks": {
            key: raw_checks.get(key) is True
            for key in ("outbound_denied", "credential_reads_denied", "outside_writes_denied")
        },
    }


def verify_boot(
    archive_path: Path,
    recovery: Path,
    image: Path,
    manifest: dict,
    expected_archive_sha256: str,
    *,
    workspace: Path,
    source_mount: Path,
) -> dict:
    """Return honest boolean evidence; failures retain private logs and backup."""
    result = {
        "requested": True,
        "ok": False,
        "live_models_tested": False,
        "production_untouched": True,
        "stage": "validate_source",
    }
    process = None
    boot = image / "boot-verification"
    original_hashes = {}
    source_receipt = {}
    try:
        if os.uname().sysname != "Darwin" or not (VENV / "bin/python").is_file():
            raise ValueError("Required isolated Mac Python runtime is unavailable")
        if _digest(archive_path) != expected_archive_sha256:
            raise ValueError("Backup archive changed before restore probe")
        boot.mkdir(mode=0o700, exist_ok=False)
        for name in ("home", "tmp", "data", "logs"):
            (boot / name).mkdir(mode=0o700)
        source_receipt = restore_sources(archive_path, boot / "app", manifest)
        (boot / "source-receipt.json").write_text(json.dumps(source_receipt, sort_keys=True))
        for db in manifest["databases"]:
            path = recovery / PurePosixPath(db["path"]).name
            original_hashes[path] = _digest(path)
        main = next(db for db in manifest["databases"] if db["original"] == MAIN_DATABASE)
        shutil.copyfile(recovery / PurePosixPath(main["path"]).name, boot / "data/chat.db")
        os.chmod(boot / "data/chat.db", 0o600)
        with socket.socket() as probe_socket:
            probe_socket.bind(("127.0.0.1", 0))
            port = probe_socket.getsockname()[1]
        configuration = {
            "port": port,
            "nonce": secrets.token_hex(24),
            "username": "restore_probe_" + secrets.token_hex(12),
            "password": secrets.token_urlsafe(32),
            "credential_probe": str(Path.home() / ".ssh/known_hosts"),
            "write_probe": str(image / "outside-boot-write-probe"),
        }
        write_probe = image / "outside-boot-write-probe"
        write_probe.write_text("unchanged")
        policy = boot / "sandbox.sb"
        policy.write_text(
            sandbox_policy(boot, image, port, Path.home(), workspace=workspace, source_mount=source_mount)
        )
        result["stage"] = "isolated_startup"
        with (boot / "logs/process.log").open("wb") as log:
            process = subprocess.Popen(
                ["/usr/bin/sandbox-exec", "-f", str(policy), str(VENV / "bin/python"), "-c", BOOTSTRAP],
                cwd=boot / "app",
                env=boot_environment(boot, port),
                start_new_session=True,
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=log,
            )
            process.stdin.write(json.dumps(configuration).encode() + b"\n")
            process.stdin.close()
            deadline = time.monotonic() + 40
            connected = False
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    status, response, _ = _request(port, "GET", "/__restore_probe/" + configuration["nonce"])
                    result["startup_probe"] = startup_diagnostic(
                        status, response, configuration["nonce"], process.pid
                    )
                    if (
                        status == 200
                        and response.get("nonce") == configuration["nonce"]
                        and response.get("pid") == process.pid
                        and all(
                            response.get("checks", {}).get(key) is True
                            for key in ("outbound_denied", "credential_reads_denied", "outside_writes_denied")
                        )
                    ):
                        connected = True
                        break
                except (OSError, ValueError, http.client.HTTPException):
                    pass
                time.sleep(0.15)
            if not connected:
                raise RuntimeError("Restored application did not pass its isolation and identity probes")
            checks = {"isolated_startup": True}
            result["checks"] = checks
            result["stage"] = "anonymous_authentication"
            checks["protected_api_denies_anonymous"] = _request(port, "GET", "/api/me")[0] == 401
            result["stage"] = "synthetic_login"
            status, _, headers = _request(
                port,
                "POST",
                "/api/login",
                {"username": configuration["username"], "password": configuration["password"]},
            )
            cookies = SimpleCookie()
            cookies.load(headers.get("set-cookie", headers.get("Set-Cookie", "")))
            result["login_http_status"] = status
            result["login_cookie_present"] = "token" in cookies
            if status != 200 or "token" not in cookies:
                raise RuntimeError("Restored login failed")
            cookie = "token=" + cookies["token"].value
            result["stage"] = "synthetic_owner_profile"
            status, profile, _ = _request(port, "GET", "/api/me", cookie=cookie)
            checks["login_and_owner_profile"] = profile_matches_owner(
                status, profile, configuration["username"]
            )
            result["stage"] = "empty_conversation_create"
            status, created, _ = _request(port, "POST", "/api/convs", {}, cookie)
            conv_id = created.get("id") if isinstance(created, dict) else None
            result["conversation_create_http_status"] = status
            result["conversation_create_id_valid"] = (
                isinstance(conv_id, str) and conv_id.replace("-", "").isalnum()
            )
            if status != 200 or not isinstance(conv_id, str) or not conv_id.replace("-", "").isalnum():
                raise RuntimeError("Restored empty conversation creation failed")
            result["stage"] = "empty_conversation_crud"
            status, conversation, _ = _request(port, "GET", "/api/convs/" + conv_id, cookie=cookie)
            checks["empty_conversation_read"] = (
                status == 200 and isinstance(conversation, dict) and not conversation.get("messages")
            )
            status, _, _ = _request(
                port, "PUT", "/api/convs/" + conv_id + "/skill", {"skill": "bottleneck-hunter"}, cookie
            )
            checks["empty_conversation_update"] = status == 200
            status, _, _ = _request(port, "DELETE", "/api/convs/" + conv_id, cookie=cookie)
            checks["empty_conversation_delete"] = (
                status == 200 and _request(port, "GET", "/api/convs/" + conv_id, cookie=cookie)[0] == 404
            )
            expected_version = _expected_version(boot / "app")
            if expected_version:
                status, release, _ = _request(port, "GET", "/api/releases", cookie=cookie)
                checks["served_source_version"] = status == 200 and release.get("version") == expected_version
            result.update(checks=checks, ok=all(checks.values()))
            result["stage"] = "completed_http_checks"
        result["outside_probe_unchanged"] = write_probe.read_text() == "unchanged"
        if not result["outside_probe_unchanged"]:
            result["ok"] = False
    except Exception as error:
        result.update(ok=False, error_type=type(error).__name__)
    finally:
        if process is not None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            # A child can outlive its parent; terminate any remaining members
            # of the dedicated session as well. No other jobs share this group.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        result["process_stopped"] = process is None or process.poll() is not None
        result["original_snapshots_unchanged"] = all(
            path.is_file() and _digest(path) == value for path, value in original_hashes.items()
        ) and bool(original_hashes)
        result["source_files_unchanged"] = bool(source_receipt) and all(
            (boot / "app" / name).is_file() and _digest(boot / "app" / name) == value
            for name, value in source_receipt.items()
        )
        result["archive_unchanged"] = (
            archive_path.is_file() and _digest(archive_path) == expected_archive_sha256
        )
        result["ok"] = bool(
            result.get("ok")
            and result["process_stopped"]
            and result["original_snapshots_unchanged"]
            and result["archive_unchanged"]
            and result["source_files_unchanged"]
        )
        if boot.exists():
            (boot / "result.json").write_text(json.dumps(result, sort_keys=True, indent=2))
    return result
