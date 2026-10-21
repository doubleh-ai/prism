#!/usr/bin/env python3
"""Fixed-host release RPC. Never run on import; stdin is private, output is redacted.

Only release_controller may invoke this reviewed code. Candidate application code
is not imported by validation. No arbitrary command, host, unit or path is accepted.
"""

from __future__ import annotations

import base64
import fcntl
import grp
import hashlib
import http.client
import io
import json
import os
import pwd
import re
import secrets
import select
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from contextlib import closing
from http.cookies import SimpleCookie
from pathlib import Path, PurePosixPath

ACTIONS = {
    "inspect",
    "prepare",
    "diagnose_build",
    "quarantine_failed_prepare",
    "stage",
    "quiesce",
    "switch",
    "verify",
    "rollback",
    "resume",
}
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
RELEASE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")
SERVICE = "stock-app.service"
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_INPUT = 384 * 1024 * 1024
MAX_DEPENDENCIES = 192 * 1024 * 1024
RESERVE = 2 * 1024 * 1024 * 1024
NGINX_SITES = ("stock-analyzer", "stock-app")
HOLD_MESSAGE = "系统升级中，请稍后重试"
CLI_ENTRY = "home/ubuntu/.npm-global/bin/claude"
CLI_SOURCE = "home/ubuntu/.npm-global/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
MAX_CLI_BINARY = 512 * 1024 * 1024

# Passed verbatim to the isolated interpreter, never supplied by a candidate.
# -S keeps even site initialization behind the probes; site.main() subsequently
# enables the already verified venv. Probe output contains no file contents.
STAGE_BOOTSTRAP = r"""
import errno
import json
import os
import re
import socket
import sys
import time


def denied_open(path, writable=False):
    flags = os.O_CLOEXEC | os.O_NOFOLLOW
    flags |= (os.O_WRONLY | os.O_CREAT | os.O_EXCL) if writable else os.O_RDONLY
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        categories = {errno.EACCES: "permission_denied", errno.EPERM: "operation_denied"}
        if writable:
            categories[errno.EROFS] = "read_only_filesystem"
        else:
            categories[errno.ENOENT] = "not_visible"
        return {"blocked": exc.errno in categories, "errno": categories.get(exc.errno, "unexpected")}
    os.close(fd)  # Never read bytes, including in the unsafe-success case.
    if writable:
        os.unlink(path)  # Only the new O_EXCL canary, never an existing file.
    return {"blocked": False, "errno": "opened"}


def denied_connect(port):
    categories = {
        errno.ECONNREFUSED: "connection_refused",
        errno.ENETUNREACH: "network_unreachable",
        errno.EHOSTUNREACH: "host_unreachable",
        errno.EACCES: "permission_denied",
        errno.EPERM: "operation_denied",
    }
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.settimeout(0.5)
            code = connection.connect_ex(("127.0.0.1", port))
    except OSError as exc:
        code = exc.errno
    return {"blocked": code in categories, "errno": categories.get(code, "unexpected")}


def main(arguments):
    if len(arguments) != 6:
        raise RuntimeError("stage_probe_identity_rejected")
    commit, release_id, production_pid, nonce, issued_at, application = arguments
    if (
        not re.fullmatch(r"[0-9a-f]{40}", commit)
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", release_id)
        or not re.fullmatch(r"[1-9][0-9]{0,9}", production_pid)
        or int(production_pid) <= 1
        or not re.fullmatch(r"[0-9a-f]{32}", nonce)
        or not re.fullmatch(r"[0-9]{10}", issued_at)
        or not 0 <= time.time() - int(issued_at) <= 120
        or application not in {"server.main:app", "server.recovery:app"}
    ):
        raise RuntimeError("stage_probe_identity_rejected")
    release = "/opt/prism/releases/" + commit
    stage = "/var/lib/prism-operations/staging/" + release_id
    checks = {
        "production_environment": denied_open("/home/ubuntu/stock-analyzer/.env"),
        "production_process_environment": denied_open("/proc/" + production_pid + "/environ"),
        "production_http": denied_connect(8088),
        "sidecar_http": denied_connect(8899),
        "candidate_write": denied_open(release + "/.stage-write-probe-" + nonce, writable=True),
    }
    proof = {
        "schema": 1,
        "commit": commit,
        "release_id": release_id,
        "nonce": nonce,
        "production_pid": int(production_pid),
        "stage_pid": os.getpid(),
        "issued_at": int(issued_at),
        "checked_at": int(time.time()),
        "application": application,
        "checks": checks,
    }
    fd = os.open(stage + "/isolation-proof.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as output:
        json.dump(proof, output, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        output.flush()
        os.fsync(output.fileno())
    if not all(result["blocked"] is True for result in checks.values()):
        raise RuntimeError("stage_isolation_probe_failed")
    # This is the first environment/candidate import point, in the probe PID.
    import site
    site.main()
    sys.path.insert(0, release)
    import uvicorn
    uvicorn.run(application, uds=stage + "/stage.sock", workers=1)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception:
        # No raw path, provider exception or credential is sent to the journal.
        raise SystemExit("stage_bootstrap_failed") from None
"""


def admission_config(raw):
    """Only insert a fixed block at one direct server/location / declaration."""
    text = raw.decode("utf-8")
    tokens = list(re.finditer(r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\#[^\n]*|[{};]|[^\s{};]+""", text))
    stack, header, matches = [], [], []
    for token in tokens:
        value = token[0]
        if value.startswith("#"):
            continue
        if value == "{":
            words = [item[0] for item in header]
            if words and words[0] == "location":
                if any(word in {"~", "~*"} for word in words[1:]):
                    raise ReleaseError("regex_location_requires_review")
                if any("/api" in word.lower() for word in words[1:]):
                    raise ReleaseError("existing_api_location")
                if words == ["location", "/"]:
                    if not stack or stack[-1] != "server":
                        raise ReleaseError("root_location_not_direct_server")
                    matches.append(header[0].start())
            stack.append(words[0] if words else "")
            header = []
        elif value == "}":
            if not stack or header:
                raise ReleaseError("nginx_structure_rejected")
            stack.pop()
        elif value == ";":
            header = []
        else:
            header.append(token)
    if stack or header or len(matches) != 1:
        raise ReleaseError("unique_root_location_required")
    position = matches[0]
    start = text.rfind("\n", 0, position) + 1
    indent = text[start:position]
    if indent.strip():
        raise ReleaseError("standalone_root_location_required")
    block = (
        "# Prism controlled admission hold\n"
        + indent
        + "location ^~ /api/ {\n"
        + indent
        + "    default_type application/json;\n"
        + indent
        + "    add_header Retry-After 60 always;\n"
        + indent
        + '    return 503 \'{"error":"'
        + HOLD_MESSAGE
        + "\"}';\n"
        + indent
        + "}\n"
        + indent
    )
    return (text[:position] + block + text[position:]).encode()


class ReleaseError(RuntimeError):
    """A safe category, never an upstream error body, command or credential."""


def failure_code(error):
    code = str(error) if isinstance(error, ReleaseError) else ""
    return code if re.fullmatch(r"[a-z0-9_]{1,96}", code) else "executor_failed"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def tree_digest(path, *, entries=None):
    """Hash package files and link text; external contents need a separate manifest."""
    inventory, size = [], 0
    for item in sorted(path.rglob("*")):
        relative = item.relative_to(path).as_posix()
        if item.is_symlink():
            inventory.append([relative, "link", os.readlink(item)])
        elif item.is_dir():
            inventory.append([relative, "directory", stat.S_IMODE(item.stat().st_mode)])
        elif item.is_file():
            hasher = hashlib.sha256()
            with item.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(block)
            size += item.stat().st_size
            inventory.append(
                [relative, item.stat().st_size, stat.S_IMODE(item.stat().st_mode), hasher.hexdigest()]
            )
    if entries is not None:
        entries.extend(inventory)
    return digest(encoded(inventory)), size


def safe_path(value):
    path = PurePosixPath(value)
    if (
        not value
        or not value.isascii()
        or "\\" in value
        or path.is_absolute()
        or any(p in {"", ".", ".."} for p in value.split("/"))
    ):
        raise ReleaseError("unsafe_path")
    if any(p.startswith(".") for p in path.parts):
        raise ReleaseError("hidden_path_forbidden")
    return value


def validate_archive(raw, expected, manifest_digest):
    """Independently verify all bytes/modes; extraction occurs only after this returns."""
    if len(raw) > MAX_ARCHIVE or digest(raw) != expected:
        raise ReleaseError("archive_digest_mismatch")
    files, total = {}, 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for member in archive:
            safe_path(member.name)
            if (
                not member.isfile()
                or member.name in files
                or member.mode not in {0o644, 0o755}
                or member.size > 8 * 1024 * 1024
            ):
                raise ReleaseError("archive_member_rejected")
            total += member.size
            if total > MAX_ARCHIVE or len(files) > 4096:
                raise ReleaseError("archive_limits")
            content = archive.extractfile(member).read()
            if len(content) != member.size:
                raise ReleaseError("archive_truncated")
            files[member.name] = (content, member.mode)
    manifest_raw = files.pop("release-manifest.json", (b"", 0))[0]
    if digest(manifest_raw) != manifest_digest:
        raise ReleaseError("manifest_digest_mismatch")
    manifest = json.loads(manifest_raw)
    entries = manifest.get("files", [])
    if (
        manifest.get("kind") != "candidate"
        or manifest.get("deployed") is not False
        or not SHA.fullmatch(manifest.get("commit", ""))
    ):
        raise ReleaseError("manifest_identity")
    if len(entries) != len(files) or {item["path"] for item in entries} != set(files):
        raise ReleaseError("manifest_inventory")
    for item in entries:
        content, mode = files[item["path"]]
        if digest(content) != item["sha256"] or len(content) != item["size"] or f"{mode:04o}" != item["mode"]:
            raise ReleaseError("manifest_content_mismatch")
    return manifest, files, manifest_raw


def run_command(args, *, env=None, timeout=120):
    """Suppress package/systemd logs: they can contain paths or configuration."""
    capture = args[:2] == ["/usr/bin/systemctl", "show"]
    result = subprocess.run(
        args,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
    )
    missing_stage = (
        capture
        and args[2:3] == ["--property=LoadState,ActiveState"]
        and len(args) == 4
        and re.fullmatch(
            r"prism-(?:stage-[0-9a-f]{12}|diagnose-[0-9a-f]{12}-(?:namespace|check|dryrun)-[0-9a-f]{8})\.service",
            args[-1],
        )
        and b"LoadState=not-found" in (result.stdout or b"").splitlines()
    )
    if result.returncode and not missing_stage:
        raise ReleaseError("command_failed")
    if result.stdout and len(result.stdout) > 65536:
        raise ReleaseError("command_output_limit")
    return result.stdout or b""


def run_diagnostic(args, *, timeout=75):
    """Keep at most 256 KiB in memory; never persist or print package output."""
    process = subprocess.Popen(
        args,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output, truncated, timed_out = bytearray(), False, False
    deadline = time.monotonic() + timeout
    try:
        while True:
            if time.monotonic() >= deadline:
                timed_out = True
                break
            if not select.select([process.stdout], [], [], 0.1)[0]:
                continue
            block = os.read(process.stdout.fileno(), 65536)
            if not block:
                break
            remaining = 256 * 1024 - len(output)
            output.extend(block[:remaining])
            if len(block) > remaining:
                truncated = True
                break
        if truncated or timed_out:
            process.terminate()  # The caller also stops only this fixed unit.
        try:
            code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            code = process.wait(timeout=5)
        return {"return_code": code, "output": bytes(output), "truncated": truncated, "timed_out": timed_out}
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def safe_build_diagnostics(raw, *, release, packages):
    """Extract fixed categories and already inventoried package identifiers only."""
    categories, named, paths, system_paths, unknown = set(), set(), set(), set(), 0
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if len(line) > 4096:
            continue
        lower = line.lower()
        for token, code in (
            ("no broken requirements found", "dependencies_consistent"),
            ("which is not installed", "missing_dependency"),
            ("has requirement", "dependency_conflict"),
            ("conflicting dependencies", "resolver_conflict"),
            ("resolutionimpossible", "resolver_conflict"),
            ("no matching distribution found", "wheel_not_found"),
            ("could not find a version", "wheel_not_found"),
            ("permission denied", "permission_denied"),
            ("read-only file system", "read_only_filesystem"),
            ("do not match the hashes", "hash_mismatch"),
            ("ignoring invalid distribution", "invalid_distribution"),
            ("would install", "dry_run_ready"),
            ("226/namespace", "namespace_setup_failed"),
            ("status=226", "namespace_setup_failed"),
            ("mount namespacing", "namespace_setup_failed"),
            ("step namespace", "namespace_setup_failed"),
            ("network namespacing", "network_namespace_setup_failed"),
            ("operation not permitted", "operation_not_permitted"),
            ("no such file or directory", "missing_path"),
            ("invalid argument", "invalid_property_or_mount_argument"),
        ):
            if token in lower:
                categories.add(code)
        references = []
        if re.search(r"(?:has requirement|requires .+, which is not installed)", line):
            match = re.match(r"([A-Za-z][A-Za-z0-9_.-]{0,79})\s", line)
            if match:
                references.append(match[1])
        for pattern in (
            r"(?:has requirement|requires|requirement|distribution found for)\s+([A-Za-z][A-Za-z0-9_.-]{0,79})",
        ):
            references.extend(re.findall(pattern, line))
        for name in references:
            normalized = re.sub(r"[-_.]+", "-", name).lower()
            if normalized in packages:
                named.add(normalized)
            else:
                unknown += 1
        if "permission denied" in lower or "read-only file system" in lower:
            for value in re.findall(r"['\"]([^'\"\r\n]+)['\"]", line):
                prefix = str(release) + "/"
                if value.startswith(prefix):
                    relative = value[len(prefix) :]
                    parts = PurePosixPath(relative).parts
                    if (
                        parts
                        and all(part not in {".", ".."} for part in parts)
                        and len(relative) <= 240
                        and re.fullmatch(r"[A-Za-z0-9_./+\-]+", relative)
                    ):
                        paths.add(relative)
        if any(
            token in lower
            for token in ("namespac", "mount", "permission denied", "no such file", "invalid argument")
        ):
            for value in re.findall(r"/[A-Za-z0-9_./+\-]+", line):
                value = value.rstrip("./")
                if value in {
                    "/run",
                    "/run/user",
                    "/run/systemd/unit-root",
                    "/run/systemd/unit-root/run",
                    "/run/systemd/unit-root/run/user",
                    "/run/systemd/inaccessible/dir",
                    "/run/systemd/inaccessible/reg",
                    "/run/systemd/inaccessible/sock",
                }:
                    system_paths.add(value)
                elif value.startswith("/run/systemd/"):
                    system_paths.add("systemd_runtime_mount")
                elif value == "/var/lib/prism-operations/staging":
                    system_paths.add("staging_directory")
                elif value.startswith(str(release) + "/"):
                    relative = value[len(str(release)) + 1 :]
                    if len(relative) <= 240 and all(
                        part not in {".", ".."} for part in PurePosixPath(relative).parts
                    ):
                        paths.add(relative)
    return {
        "categories": sorted(categories) or ["unclassified_package_output"],
        "packages": [{"name": name, "installed_version": packages[name]} for name in sorted(named)[:16]],
        "candidate_relative_paths": sorted(paths)[:8],
        "unrecognized_package_references": unknown,
        "sandbox_system_paths": sorted(system_paths)[:8],
    }


class RemoteRelease:
    def __init__(
        self,
        root=Path("/"),
        runner=run_command,
        permit=None,
        control=None,
        now=time.time,
        diagnostic_runner=run_diagnostic,
    ):
        self.root, self.run, self.permit, self.control, self.now = Path(root), runner, permit, control, now
        self.production = self.root / "home/ubuntu/stock-analyzer"
        self.old_venv = self.root / "home/ubuntu/stock-venv"
        self.releases = self.root / "opt/prism/releases"
        self.operations = self.root / "var/lib/prism-operations"
        self.override = self.root / "etc/systemd/system/stock-app.service.d/90-prism-release.conf"
        self.record = None
        self.diagnostic_runner = diagnostic_runner

    def checked(self, path):
        path = Path(path)
        for ancestor in [path, *path.parents]:
            if ancestor == self.root.parent:
                break
            if ancestor.is_symlink():
                raise ReleaseError("symlink_path_rejected")
        return path

    def stop(self):
        if (self.control and self.control()) or self.checked(self.operations / "STOP").exists():
            raise ReleaseError("operator_stop")

    def boundary(self, label):
        self.stop()
        nonce = secrets.token_hex(16)
        if not self.permit or self.permit(nonce, label) != nonce:
            raise ReleaseError("fresh_operator_permit_required")
        self.stop()

    def space(self, path, needed):
        while not path.exists():
            path = path.parent
        if shutil.disk_usage(path).free < needed + RESERVE:
            raise ReleaseError("insufficient_disk_reserve")

    def public_directory(self, path):
        self.checked(path)
        path.mkdir(parents=True, exist_ok=True, mode=0o755)
        if self.root == Path("/") and path.stat().st_uid != 0:
            raise ReleaseError("root_owned_directory_required")
        path.chmod(0o755)

    def write(self, path, content, mode=0o600):
        self.checked(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(path.name + ".pending")
        self.checked(temporary)
        with temporary.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(mode)
        temporary.replace(path)

    def journal(self, stage, outcome, *, failure=None):
        previous = (
            json.loads((self.record / "status.json").read_bytes())
            if (self.record / "status.json").exists()
            else {}
        )
        cutover = (
            json.loads((self.record / "cutover.json").read_bytes())
            if (self.record / "cutover.json").exists()
            else {}
        )
        current_time = int(self.now())
        event = {
            "release_id": self.request["release_id"],
            "commit": self.request["commit"],
            "archive_sha256": self.request.get("archive_sha256"),
            "stage": stage,
            "outcome": outcome,
            "status": "running" if outcome == "started" else outcome,
            "at": current_time,
            "started_at": previous.get("started_at", current_time),
            "updated_at": current_time,
            "operator": "Prism release controller",
            "operator_issue": "https://github.com/doubleh-ai/prism/issues/9",
            "previous_commit": cutover.get("previous_runtime", {}).get("served_revision"),
            "recovery_commit": self.request.get("receipts", {}).get("recovery", {}).get("commit"),
            "last_completed_stage": stage if outcome == "completed" else previous.get("last_completed_stage"),
            "cutover_occurred": previous.get("cutover_occurred", False) or stage == "cutover_config_written",
            "steps": previous.get("steps", [])[-49:]
            + [{"stage": stage, "status": outcome, "at": current_time}],
            "service_verified": (self.record / "verified.json").exists()
            and not (self.record / "rollback.json").exists(),
            "production_verified": outcome == "completed"
            and stage == "resume"
            and (self.record / "verified.json").exists()
            and not (self.record / "rollback.json").exists(),
        }
        private_event = dict(event)
        if failure is not None:
            private_event["failure_code"] = failure_code(failure)
        path = self.checked(self.record / "journal.jsonl")
        with path.open("ab") as output:
            output.write(encoded(private_event) + b"\n")
            output.flush()
            os.fsync(output.fileno())
        path.chmod(0o600)
        self.write(self.record / "status.json", encoded(private_event))
        self.write(self.operations / "status.json", encoded(event), 0o644)
        return event

    def receipt(self, name):
        receipt = self.request.get("receipts", {})
        raw = base64.b64decode(self.request.get("receipts_raw", ""), validate=True)
        if digest(raw) != self.request.get("receipts_sha256") or json.loads(raw) != receipt:
            raise ReleaseError("receipt_digest_mismatch")
        if (
            receipt.get("commit") != self.request["commit"]
            or receipt.get("archive_sha256") != self.request["archive_sha256"]
        ):
            raise ReleaseError("receipt_not_bound")
        if not DIGEST.fullmatch(self.request.get("receipts_sha256", "")):
            raise ReleaseError("receipt_digest_required")
        item = receipt.get(name, {})
        if item.get("verified") is not True or not DIGEST.fullmatch(item.get("receipt_sha256", "")):
            raise ReleaseError("reviewed_receipt_required_" + name)
        if not 0 <= self.now() - item.get("checked_at", 0) <= (120 if name == "activity" else 86400):
            raise ReleaseError("receipt_expired_" + name)
        if name == "compatibility" and item.get("database_forward_and_backward") is not True:
            raise ReleaseError("database_compatibility_required")
        return item

    def runtime(self):
        cwd = (
            self.run(["/usr/bin/systemctl", "show", "--value", "--property=WorkingDirectory", SERVICE])
            .decode()
            .strip()
        )
        command = self.run(["/usr/bin/systemctl", "show", "--value", "--property=ExecStart", SERVICE])
        pid = (
            self.run(["/usr/bin/systemctl", "show", "--value", "--property=MainPID", SERVICE])
            .decode()
            .strip()
        )
        path = self.checked(Path(cwd))
        if path != self.production and not (path.parent == self.releases and SHA.fullmatch(path.name)):
            raise ReleaseError("unexpected_current_working_directory")
        if not pid.isdigit() or pid == "0" or Path(os.readlink(self.root / "proc" / pid / "cwd")) != path:
            raise ReleaseError("runtime_directory_not_reconciled")
        # Legacy v0.19.7 has no readiness route. The PID/source fingerprint remains
        # real; an unavailable served revision is explicitly unknown, not inferred.
        try:
            revision = self.ready().get("deployed_revision")
        except Exception:
            revision = None
        result = {"working_directory": cwd, "exec_start_sha256": digest(command), "served_revision": revision}
        result["fingerprint"] = digest(encoded(result))
        return result

    def ready(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise ReleaseError("readiness_redirect_rejected")

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open("http://127.0.0.1:8088/health/ready", timeout=10) as response:
            raw = response.read(65537)
            if len(raw) > 65536 or response.status != 200:
                raise ReleaseError("readiness_unavailable")
        return json.loads(raw)

    def source_inventory(self, supplied, source=None):
        """Hash declared files and independently detect added application source files."""
        expected = supplied.get("files", [])
        source = source or self.production
        names = {safe_path(item["path"]) for item in expected}
        if not names or len(names) != len(expected):
            raise ReleaseError("source_inventory_required")
        found = set()
        excluded = {"data", "logs", "reports", "uploads", "backups", "__pycache__", "node_modules", "venv"}
        for directory, folders, filenames in os.walk(source, followlinks=False):
            folders[:] = [name for name in folders if not name.startswith(".") and name not in excluded]
            for name in folders + filenames:
                path = Path(directory) / name
                if path.is_symlink() and not name.startswith("."):
                    raise ReleaseError("production_source_symlink")
            for name in filenames:
                path = Path(directory) / name
                if not name.startswith(".") and path.suffix in {
                    ".py",
                    ".js",
                    ".css",
                    ".html",
                    ".json",
                    ".svg",
                    ".md",
                    ".pth",
                    ".so",
                }:
                    found.add(path.relative_to(source).as_posix())
        if found - names:
            raise ReleaseError("new_production_source")
        for item in expected:
            path = self.checked(source / item["path"])
            if (
                not path.is_file()
                or path.stat().st_size != item["size"]
                or digest(path.read_bytes()) != item["sha256"]
                or f"{stat.S_IMODE(path.stat().st_mode):04o}" != item["mode"]
            ):
                raise ReleaseError("production_source_drift")
        return digest(encoded(supplied))

    def no_active_tasks(self):
        activity = self.receipt("activity")
        if activity.get("admission_paused") is not True:
            raise ReleaseError("reviewed_admission_pause_required")
        database = self.checked(self.production / "data/chat.db")
        if not database.is_file():
            raise ReleaseError("production_database_missing")
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("analysis_runs", "indicator_scan_jobs", "capability_calls"):
                if (
                    table in tables
                    and db.execute(
                        f"SELECT 1 FROM {table} WHERE status IN ('queued','starting','running','cancelling') LIMIT 1"
                    ).fetchone()
                ):
                    raise ReleaseError("active_tasks")
            if "vibe_plans" in tables:
                for row in db.execute("SELECT plan_json FROM vibe_plans"):
                    if json.loads(row[0]).get("status") in {"queued", "running", "cancelling"}:
                        raise ReleaseError("active_tasks")
            if (
                "capability_runs" in tables
                and db.execute(
                    "SELECT 1 FROM capability_runs WHERE revoked=0 AND expires_at>? LIMIT 1",
                    [int(self.now())],
                ).fetchone()
            ):
                raise ReleaseError("active_tasks")
            if "research_runs" in tables:
                for row in db.execute("SELECT payload FROM research_runs"):
                    if json.loads(row[0]).get("status") not in {
                        "completed",
                        "partial",
                        "failed",
                        "cancelled",
                    }:
                        raise ReleaseError("active_tasks")

    def verify_tree(self):
        if not (self.record / "prepared.json").exists():
            raise ReleaseError("prepare_not_completed")
        if self.root == Path("/") and (
            self.release.stat().st_uid != 0 or self.release.stat().st_mode & 0o022
        ):
            raise ReleaseError("prepared_tree_writable_by_nonroot")
        manifest_raw = (self.record / "manifest.json").read_bytes()
        manifest = json.loads(manifest_raw)
        identity = json.loads((self.record / "identity.json").read_bytes())
        prepared = json.loads((self.record / "prepared.json").read_bytes())
        if digest(manifest_raw) != identity.get("manifest_sha256") or any(
            item.get("commit") != self.request["commit"] for item in (manifest, identity, prepared)
        ):
            raise ReleaseError("prepared_manifest_not_bound")
        allowed = {item["path"] for item in manifest["files"]} | {
            ".deploy-revision",
            ".engine-revision",
            ".cli-revision",
        }
        for path in self.release.rglob("*"):
            relative = path.relative_to(self.release)
            if (
                self.root == Path("/")
                and not path.is_symlink()
                and (path.stat().st_uid != 0 or stat.S_IMODE(path.stat().st_mode) & 0o022)
            ):
                raise ReleaseError("prepared_tree_writable_by_nonroot")
            if relative.parts[0] == ".venv":
                continue
            self.checked(path)
            if path.is_file() and relative.as_posix() not in allowed:
                raise ReleaseError("unexpected_prepared_file")
        if (self.release / ".deploy-revision").read_text().strip() != self.request["commit"]:
            raise ReleaseError("prepared_revision_mismatch")
        if (self.release / ".engine-revision").read_text().strip() != prepared.get("external_sources_sha256"):
            raise ReleaseError("prepared_engine_revision_mismatch")
        for item in manifest["files"]:
            path = self.checked(self.release / item["path"])
            if (
                not path.is_file()
                or digest(path.read_bytes()) != item["sha256"]
                or path.stat().st_size != item["size"]
                or f"{stat.S_IMODE(path.stat().st_mode):04o}" != item["mode"]
            ):
                raise ReleaseError("prepared_source_drift")
        if tree_digest(self.release / ".venv")[0] != prepared.get("environment_sha256"):
            raise ReleaseError("prepared_environment_drift")
        self.verify_external(self.record)
        self.verify_cli(self.record, self.release)

    @staticmethod
    def cli_identity(info):
        return tuple(
            getattr(info, name)
            for name in (
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
        )

    def cli_target(self, expected):
        if not isinstance(expected, str) or not DIGEST.fullmatch(expected):
            raise ReleaseError("cli_binary_receipt_required")
        return self.checked(self.root / "opt/prism/tools/claude" / expected / "claude")

    def check_cli_binary(self, expected):
        target = self.cli_target(expected)
        info = target.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o755:
            raise ReleaseError("cli_binary_not_root_locked")
        if self.root == Path("/"):
            for path in (target, *target.parents):
                info = path.lstat()
                if info.st_uid != 0 or info.st_mode & 0o022:
                    raise ReleaseError("cli_binary_not_root_locked")
        with target.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ReleaseError("cli_binary_drift")
        return target

    def verify_cli(self, record, release):
        prepared = json.loads(self.checked(record / "prepared.json").read_bytes())
        expected = prepared.get("cli_binary_sha256")
        if self.checked(release / ".cli-revision").read_text().strip() != expected:
            raise ReleaseError("prepared_cli_revision_mismatch")
        return self.check_cli_binary(expected)

    def prepare_cli(self):
        expected = self.request["receipts"].get("dependencies", {}).get("cli_binary_sha256")
        target = self.cli_target(expected)
        entry, source = self.root / CLI_ENTRY, self.checked(self.root / CLI_SOURCE)
        self.checked(entry.parent)
        if not entry.is_symlink() or entry.resolve(strict=True) != source:
            raise ReleaseError("fixed_cli_source_required")
        entry_identity, link = self.cli_identity(entry.lstat()), os.readlink(entry)
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            original = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(original.st_mode)
                # The official installation can have multiple source hard
                # links. Read this fixed inode only as data; every identity
                # field (including link count) and its hash must remain stable.
                or original.st_nlink < 1
                or original.st_mode & 0o7000
                or not original.st_mode & 0o111
                or not 4 <= original.st_size <= MAX_CLI_BINARY
            ):
                raise ReleaseError("cli_source_not_regular_binary")
            identity = self.cli_identity(original)

            def source_unchanged():
                return (
                    self.cli_identity(os.fstat(stream.fileno())) == identity
                    and self.cli_identity(source.lstat()) == identity
                    and self.cli_identity(entry.lstat()) == entry_identity
                    and os.readlink(entry) == link
                    and entry.resolve(strict=True) == source
                )

            if stream.read(4) != b"\x7fELF":
                raise ReleaseError("cli_source_not_regular_binary")
            stream.seek(0)
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected or not source_unchanged():
                raise ReleaseError("cli_source_drift")
            self.space(target.parent, original.st_size)
            self.boundary("prepare_fixed_cli_binary")
            if not source_unchanged():
                raise ReleaseError("cli_source_drift")
            for path in reversed(target.parents):
                if path.is_relative_to(self.root / "opt/prism/tools"):
                    self.public_directory(path)
            if not target.exists():
                pending = self.checked(target.with_name("claude.pending"))
                created_pending = False
                try:
                    stream.seek(0)
                    copied = hashlib.sha256()
                    with pending.open("xb") as output:
                        created_pending = True
                        while block := stream.read(1024 * 1024):
                            self.stop()
                            output.write(block)
                            copied.update(block)
                        output.flush()
                        os.fsync(output.fileno())
                    if copied.hexdigest() != expected or not source_unchanged():
                        raise ReleaseError("cli_source_drift")
                    stream.seek(0)
                    if (
                        hashlib.file_digest(stream, "sha256").hexdigest() != expected
                        or not source_unchanged()
                    ):
                        raise ReleaseError("cli_source_drift")
                    pending.chmod(0o755)
                    pending.replace(target)
                finally:
                    if created_pending and pending.exists():
                        pending.unlink()
            if not source_unchanged():
                raise ReleaseError("cli_source_drift")
        self.check_cli_binary(expected)
        return {"sha256": expected, "source_identity": identity, "entry_identity": entry_identity}

    def external_files(self, manifest):
        if manifest.get("source_root") != "/home/ubuntu/TradingAgents-astock" or not SHA.fullmatch(
            manifest.get("git_commit", "")
        ):
            raise ReleaseError("fixed_external_source_required")
        entries = manifest.get("files", [])
        paths = []
        forbidden = {"data", "cache", "logs", "log", "reports", "venv", "__pycache__", "node_modules"}
        for item in entries:
            path = safe_path(item.get("path", ""))
            if any(part in forbidden for part in PurePosixPath(path).parts) or Path(path).suffix in {
                ".pyc",
                ".db",
                ".sqlite",
                ".log",
                ".pem",
                ".key",
                ".env",
            }:
                raise ReleaseError("external_runtime_or_secret_forbidden")
            if (
                item.get("mode") not in {"0644", "0755"}
                or not DIGEST.fullmatch(item.get("sha256", ""))
                or not isinstance(item.get("size"), int)
                or not 0 <= item["size"] <= 8 * 1024 * 1024
            ):
                raise ReleaseError("invalid_external_manifest")
            paths.append(path)
        if (
            not entries
            or len(entries) > 4096
            or len(set(paths)) != len(paths)
            or sum(item["size"] for item in entries) > MAX_ARCHIVE
        ):
            raise ReleaseError("external_manifest_limits")
        return entries

    def external_target(self, manifest):
        return self.checked(self.root / "opt/prism/engines/astock" / digest(encoded(manifest)))

    def verify_external(self, record, expected=None):
        raw = self.checked(record / "external-source-manifest.json").read_bytes()
        manifest = json.loads(raw)
        if expected is None:
            expected = json.loads((record / "prepared.json").read_bytes()).get("external_sources_sha256")
        if digest(encoded(manifest)) != expected:
            raise ReleaseError("prepared_external_manifest_changed")
        entries = self.external_files(manifest)
        target = self.external_target(manifest)
        if (
            not target.is_dir()
            or self.root == Path("/")
            and (target.stat().st_uid != 0 or target.stat().st_mode & 0o022)
        ):
            raise ReleaseError("external_tree_not_root_locked")
        expected = {item["path"] for item in entries}
        found = set()
        for path in target.rglob("*"):
            self.checked(path)
            if self.root == Path("/") and (path.stat().st_uid != 0 or path.stat().st_mode & 0o022):
                raise ReleaseError("external_tree_not_root_locked")
            if path.is_file():
                found.add(path.relative_to(target).as_posix())
        if found != expected:
            raise ReleaseError("external_inventory_changed")
        for item in entries:
            path = target / item["path"]
            if (
                path.stat().st_size != item["size"]
                or digest(path.read_bytes()) != item["sha256"]
                or f"{stat.S_IMODE(path.stat().st_mode):04o}" != item["mode"]
            ):
                raise ReleaseError("external_source_drift")
        return target

    def prepare_external(self):
        manifest = self.request.get("external_source_manifest", {})
        expected = self.request["receipts"]["dependencies"].get("external_sources_sha256")
        if digest(encoded(manifest)) != expected:
            raise ReleaseError("external_source_receipt_required")
        entries = self.external_files(manifest)
        source = self.checked(self.root / "home/ubuntu/TradingAgents-astock")
        names = {item["path"] for item in entries}
        excluded = {"data", "cache", "logs", "reports", "venv", "__pycache__", "node_modules"}
        for directory, folders, filenames in os.walk(source, followlinks=False):
            ignored = excluded | ({"tests", "examples", "scripts"} if Path(directory) == source else set())
            folders[:] = [name for name in folders if not name.startswith(".") and name not in ignored]
            for name in folders + filenames:
                path = Path(directory) / name
                if path.is_symlink() and not name.startswith("."):
                    raise ReleaseError("external_source_symlink")
                if path.suffix == ".py" and path.relative_to(source).as_posix() not in names:
                    raise ReleaseError("unregistered_external_python")
        contents = {}
        for item in entries:
            path = self.checked(source / item["path"])
            if (
                not path.is_file()
                or path.stat().st_size != item["size"]
                or path.stat().st_mode & 0o7000
                or bool(path.stat().st_mode & 0o111) != (item["mode"] == "0755")
            ):
                raise ReleaseError("external_source_drift")
            content = path.read_bytes()
            if digest(content) != item["sha256"]:
                raise ReleaseError("external_source_drift")
            contents[item["path"]] = content
        target = self.external_target(manifest)
        self.space(target, sum(item["size"] for item in entries))
        self.boundary("prepare_fixed_external_source")
        self.public_directory(target.parent.parent)
        self.public_directory(target.parent)
        if not target.exists():
            target.mkdir(mode=0o755)
            target.chmod(0o755)
            for item in entries:
                path = target / item["path"]
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                for parent in path.parents:
                    if parent == target.parent:
                        break
                    parent.chmod(0o755)
                self.write(path, contents[item["path"]], int(item["mode"], 8))
        self.write(self.record / "external-source-manifest.json", encoded(manifest))
        self.verify_external(self.record, expected)
        return expected

    def prepare(self):
        backup = self.receipt("backup")
        if backup.get("recovery_verified") is not True:
            raise ReleaseError("recovery_receipt_required")
        self.receipt("compatibility")
        raw = base64.b64decode(self.request["archive"], validate=True)
        manifest, files, manifest_raw = validate_archive(
            raw, self.request["archive_sha256"], self.request["manifest_sha256"]
        )
        if manifest["commit"] != self.request["commit"] or self.release.exists():
            raise ReleaseError("release_identity_or_existing_path")
        dependencies = self.request.get("dependencies", [])
        dependency_size = sum(len(item.get("content", "")) * 3 // 4 for item in dependencies)
        if dependency_size > MAX_DEPENDENCIES:
            raise ReleaseError("dependency_limits")
        inherited_entries = []
        inherited, inherited_size = tree_digest(self.old_venv, entries=inherited_entries)
        if inherited != self.request["receipts"].get("dependencies", {}).get("base_environment_sha256"):
            raise ReleaseError("base_environment_changed")
        self.space(self.releases, inherited_size * 2 + dependency_size * 3 + MAX_ARCHIVE)
        self.space(self.operations, dependency_size)
        self.boundary("prepare_candidate")
        external_digest = self.prepare_external()
        cli = self.prepare_cli()
        self.public_directory(self.releases.parent)
        self.public_directory(self.releases)
        self.release.mkdir(parents=True, mode=0o755)
        self.release.chmod(0o755)
        for name, (content, mode) in files.items():
            path = self.release / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            for parent in path.parents:
                if parent == self.release.parent:
                    break
                parent.chmod(0o755)
            self.write(path, content, mode)
        self.write(self.release / ".deploy-revision", (manifest["commit"] + "\n").encode(), 0o644)
        self.write(self.release / ".engine-revision", (external_digest + "\n").encode(), 0o644)
        self.write(self.release / ".cli-revision", (cli["sha256"] + "\n").encode(), 0o644)
        self.write(self.record / "manifest.json", manifest_raw)
        inputs = self.record / "dependencies"
        inputs.mkdir(mode=0o700)
        for item in dependencies:
            name = item.get("name", "")
            if name != "core.lock" and not re.fullmatch(r"[A-Za-z0-9_.+\-]+\.whl", name):
                raise ReleaseError("dependency_filename_rejected")
            content = base64.b64decode(item["content"], validate=True)
            if digest(content) != item["sha256"] or (inputs / name).exists():
                raise ReleaseError("dependency_digest_mismatch")
            self.write(inputs / name, content)
        if not (inputs / "core.lock").is_file():
            raise ReleaseError("dependency_lock_required")
        lock_digest = digest((inputs / "core.lock").read_bytes())
        lock_entry = next((item for item in manifest["files"] if item["path"] == "requirements.lock"), {})
        if lock_digest != lock_entry.get("sha256") or lock_digest != self.request["receipts"][
            "dependencies"
        ].get("lock_sha256"):
            raise ReleaseError("candidate_dependency_lock_mismatch")
        self.boundary("prepare_environment")
        self.stage_account()
        env = {
            "PATH": "/usr/bin:/bin",
            "PIP_CONFIG_FILE": "/dev/null",
            "PIP_NO_INPUT": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        target = self.release / ".venv"
        self.run(["/usr/bin/python3.12", "-I", "-B", "-S", "-m", "venv", "--copies", str(target)], env=env)
        sources = list((self.old_venv / "lib").glob("python*/site-packages"))
        destinations = list((target / "lib").glob("python*/site-packages"))
        if (
            len(sources) != 1
            or len(destinations) != 1
            or sources[0].parent.name != destinations[0].parent.name
        ):
            raise ReleaseError("python_environment_mismatch")
        for path in sources[0].rglob("*"):
            if path.is_symlink() and (
                not path.resolve(strict=True).is_relative_to(sources[0]) or path.is_dir()
            ):
                raise ReleaseError("package_symlink_requires_review")
        inventory = {item[0]: item for item in inherited_entries}

        def copy_verified(source, destination):
            source = Path(source)
            expected = inventory.get(source.relative_to(self.old_venv).as_posix())
            if source.is_symlink():
                if expected is None or expected[1:] != ["link", os.readlink(source)]:
                    raise ReleaseError("inherited_package_drift")
                source = source.resolve(strict=True)
                if not source.is_relative_to(sources[0]):
                    raise ReleaseError("package_symlink_requires_review")
                expected = inventory.get(source.relative_to(self.old_venv).as_posix())
            if expected is None or len(expected) != 4 or not isinstance(expected[1], int):
                raise ReleaseError("inherited_package_drift")
            hasher = hashlib.sha256()
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as original, Path(destination).open("wb") as copied:
                info = os.fstat(original.fileno())
                if not stat.S_ISREG(info.st_mode) or (info.st_size, stat.S_IMODE(info.st_mode)) != tuple(
                    expected[1:3]
                ):
                    raise ReleaseError("inherited_package_drift")
                for block in iter(lambda: original.read(1024 * 1024), b""):
                    hasher.update(block)
                    copied.write(block)
            if hasher.hexdigest() != expected[3]:
                raise ReleaseError("inherited_package_drift")
            Path(destination).chmod(expected[2])
            return str(destination)

        # This explicit projection removes the reviewed unused UI distribution
        # and the editable installation replaced by the fixed engine manifest.
        # Exact observed root names avoid pruning unrelated/transitive packages.
        projected_out = {
            "streamlit": "unused_ui",
            "streamlit-1.58.0.dist-info": "unused_ui_metadata",
            "tradingagents_astock-0.2.16.dist-info": "replaced_editable_metadata",
            "~radingagents_astock-0.2.15.dist-info": "retired_editable_metadata",
            "__editable__.tradingagents_astock-0.2.16.pth": "replaced_editable_hook",
            "__editable___tradingagents_astock_0_2_16_finder.py": "replaced_editable_finder",
            "~ttpx-0.25.2.dist-info": "retired_httpx_metadata",
        }
        excluded_roots = []
        startup_patterns = (
            "*.pth",
            "__pycache__",
            "*.pyc",
            "*.pyo",
            "sitecustomize*",
            "usercustomize*",
            "patch_ssl*",
        )
        ignore_startup = shutil.ignore_patterns(*startup_patterns)

        def ignore_inherited(directory, names):
            ignored = set(ignore_startup(directory, names))
            if Path(directory) == sources[0]:
                for name in sorted(set(names) & projected_out.keys()):
                    ignored.add(name)
                    excluded_roots.append({"name": name, "reason": projected_out[name]})
            return ignored

        # Internal regular-file links are materialized; no link back to writable
        # old packages, TLS bypass hook, or legacy editable startup hook survives.
        shutil.copytree(
            sources[0],
            destinations[0],
            dirs_exist_ok=True,
            symlinks=False,
            copy_function=copy_verified,
            ignore=ignore_inherited,
        )
        if tree_digest(self.old_venv)[0] != inherited:
            raise ReleaseError("base_environment_changed_during_copy")
        if self.request["receipts"]["dependencies"].get("allowed_pth"):
            raise ReleaseError("legacy_editable_hooks_rejected_use_fixed_engine_path")
        build_inputs = self.checked(self.operations / "build-inputs" / self.request["release_id"])
        self.public_directory(build_inputs.parent)
        build_inputs.mkdir(mode=0o750)
        build_inputs.chmod(0o750)
        self.run(["/usr/bin/chown", "root:prism-stage", str(build_inputs)])
        for item in inputs.iterdir():
            self.write(build_inputs / item.name, item.read_bytes(), 0o640)
            self.run(["/usr/bin/chown", "root:prism-stage", str(build_inputs / item.name)])
        self.run(["/usr/bin/chown", "-hR", "prism-stage:prism-stage", str(target)])
        self.boundary("offline_dependency_install")
        self.build_command(
            target,
            "install",
            [
                "install",
                "--no-cache-dir",
                "--no-index",
                "--find-links",
                str(build_inputs),
                "--require-hashes",
                "--only-binary=:all:",
                "--upgrade",
                "-r",
                str(build_inputs / "core.lock"),
            ],
        )
        self.stop()
        self.build_command(target, "check", ["check"])
        self.run(["/usr/bin/chown", "-hR", "root:root", str(target)])
        self.stop()
        target.chmod(0o755)
        for path in target.rglob("*"):
            if not path.is_symlink():
                path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
        self.write(
            self.record / "prepared.json",
            encoded(
                {
                    "commit": manifest["commit"],
                    "receipt": self.request["receipts_sha256"],
                    "environment_sha256": tree_digest(target)[0],
                    "inherited_environment_sha256": inherited,
                    "external_sources_sha256": external_digest,
                    "cli_binary_sha256": cli["sha256"],
                    "cli_source_identity": cli["source_identity"],
                    "cli_entry_identity": cli["entry_identity"],
                    "inherited_package_projection": {
                        "policy": "reviewed_legacy_ui_and_editable_v1",
                        "excluded_roots": excluded_roots,
                        "excluded_startup_and_bytecode_patterns": list(startup_patterns),
                        "replacement_engine_manifest_sha256": external_digest,
                        "source_inventory": "entire old environment before and after copy",
                    },
                }
            ),
        )
        self.verify_external(self.record)
        self.verify_cli(self.record, self.release)

    def no_stage_processes(self):
        uid = pwd.getpwnam("prism-stage").pw_uid
        for process in (self.root / "proc").iterdir():
            if not process.name.isdigit():
                continue
            try:
                # Non-dumpable processes can expose a root-owned /proc/PID.
                # Only the kernel's real/effective/saved/fs UIDs establish UID.
                with (process / "status").open("rb") as source:
                    values = None
                    for _ in range(64):
                        line = source.readline(4097)
                        if not line or len(line) > 4096:
                            break
                        if line.startswith(b"Uid:"):
                            match = re.fullmatch(
                                rb"Uid:[\t ]+([0-9]{1,10})[\t ]+([0-9]{1,10})[\t ]+([0-9]{1,10})[\t ]+([0-9]{1,10})[\t ]*\n?",
                                line,
                            )
                            if match:
                                values = tuple(int(value) for value in match.groups())
                            break
                if values is None or any(value > 4294967295 for value in values):
                    raise ReleaseError("failed_prepare_process_status_rejected")
                if uid in values:
                    raise ReleaseError("failed_prepare_stage_process_active")
            except FileNotFoundError:
                try:
                    process.stat()  # Only disappearance, never directory ownership.
                except FileNotFoundError:
                    continue
                except OSError:
                    raise ReleaseError("failed_prepare_process_inspection_failed") from None
                raise ReleaseError("failed_prepare_process_status_rejected") from None
            except OSError:
                raise ReleaseError("failed_prepare_process_inspection_failed") from None

    def failed_prepare_guard(self, *, candidate_required=True):
        forbidden = (
            "prepared.json",
            "staged.json",
            "previous.json",
            "cutover.json",
            "switched.json",
            "recovery-intent.json",
            "rollback.json",
            "verified.json",
            "admission.json",
        )
        if any(self.checked(self.record / name).exists() for name in forbidden):
            raise ReleaseError("failed_prepare_only")
        raw = self.checked(self.record / "journal.jsonl").read_bytes()
        if len(raw) > 2 * 1024 * 1024:
            raise ReleaseError("failed_prepare_journal_limit")
        history = [json.loads(line) for line in raw.splitlines() if line]
        prepares = [
            row
            for row in history
            if row.get("stage") == "prepare" and row.get("outcome") in {"failed", "completed", "cancelled"}
        ]
        if (
            not prepares
            or prepares[-1].get("outcome") != "failed"
            or any(row.get("outcome") == "completed" for row in prepares)
        ):
            raise ReleaseError("failed_prepare_record_required")
        manifest_raw = self.checked(self.record / "manifest.json").read_bytes()
        manifest = json.loads(manifest_raw)
        if (
            digest(manifest_raw) != self.request["manifest_sha256"]
            or manifest.get("commit") != self.request["commit"]
            or str(manifest.get("version", "")).endswith("-recovery")
            or self.request.get("receipts", {}).get("recovery", {}).get("commit") == self.request["commit"]
        ):
            raise ReleaseError("failed_prepare_identity_rejected")
        if self.runtime()["working_directory"] != str(self.production):
            raise ReleaseError("failed_prepare_original_runtime_required")
        self.no_stage_processes()
        if candidate_required:
            if not self.release.is_dir():
                raise ReleaseError("failed_prepare_candidate_required")
            if self.root == Path("/") and (
                self.release.stat().st_uid != 0 or self.release.stat().st_mode & 0o022
            ):
                raise ReleaseError("failed_prepare_source_drift")
            for item in manifest["files"]:
                path = self.checked(self.release / safe_path(item["path"]))
                info = path.stat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_size != item["size"]
                    or digest(path.read_bytes()) != item["sha256"]
                    or f"{stat.S_IMODE(info.st_mode):04o}" != item["mode"]
                    or self.root == Path("/")
                    and (info.st_uid != 0 or info.st_mode & 0o022)
                ):
                    raise ReleaseError("failed_prepare_source_drift")
            if self.checked(self.release / ".deploy-revision").read_text().strip() != self.request["commit"]:
                raise ReleaseError("failed_prepare_identity_rejected")
            dependencies = self.request.get("receipts", {}).get("dependencies", {})
            external = dependencies.get("external_sources_sha256")
            self.verify_external(self.record, external)
            self.check_cli_binary(dependencies.get("cli_binary_sha256"))
            if self.checked(
                self.release / ".engine-revision"
            ).read_text().strip() != external or self.checked(
                self.release / ".cli-revision"
            ).read_text().strip() != dependencies.get("cli_binary_sha256"):
                raise ReleaseError("failed_prepare_identity_rejected")
        return manifest

    def diagnostic_packages(self, lock, target):
        packages = {"pip": "unknown", "setuptools": "unknown", "wheel": "unknown"}
        for name in re.findall(r"^([A-Za-z][A-Za-z0-9_.-]{0,79})==", lock.decode("ascii"), re.M):
            packages[re.sub(r"[-_.]+", "-", name).lower()] = "not_installed"
        for metadata in target.glob("lib/python3.12/site-packages/*.dist-info/METADATA"):
            self.checked(metadata)
            if not metadata.is_file() or metadata.stat().st_size > 1024 * 1024:
                raise ReleaseError("diagnostic_metadata_rejected")
            with metadata.open("rb") as source:
                header = source.read(65536).split(b"\n\n", 1)[0].decode("utf-8", errors="replace")
            name = re.search(r"^Name: ([A-Za-z][A-Za-z0-9_.-]{0,79})$", header, re.M)
            version = re.search(r"^Version: ([A-Za-z0-9.!+_\-]{1,64})$", header, re.M)
            if name and version:
                packages[re.sub(r"[-_.]+", "-", name[1]).lower()] = version[1]
                for requirement in re.findall(
                    r"^Requires-Dist: ([A-Za-z][A-Za-z0-9_.-]{0,79})(?:\W|$)", header, re.M
                ):
                    packages.setdefault(re.sub(r"[-_.]+", "-", requirement).lower(), "not_installed")
        return packages

    def stop_diagnostic_unit(self, unit):
        raw = self.run(["/usr/bin/systemctl", "show", "--property=LoadState,ActiveState", unit]).decode()
        values = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
        if values.get("LoadState") == "not-found":
            self.no_stage_processes()
            return
        if values.get("ActiveState") not in {
            "active",
            "activating",
            "deactivating",
            "reloading",
            "inactive",
            "failed",
        }:
            raise ReleaseError("diagnostic_unit_state_unknown")
        self.run(["/usr/bin/systemctl", "stop", unit])
        self.no_stage_processes()

    def diagnose_build(self):
        self.failed_prepare_guard()
        target = self.checked(self.release / ".venv")
        binary = self.checked(target / "bin/python")
        system = self.checked(self.root / "usr/bin/python3.12")
        if not binary.is_file() or digest(binary.read_bytes()) != digest(system.read_bytes()):
            raise ReleaseError("diagnostic_system_python_copy_required")
        inputs = self.checked(self.operations / "build-inputs" / self.request["release_id"])
        lock = self.checked(inputs / "core.lock").read_bytes()
        expected_lock = self.request.get("receipts", {}).get("dependencies", {}).get("lock_sha256")
        if digest(lock) != expected_lock or lock != (self.release / "requirements.lock").read_bytes():
            raise ReleaseError("diagnostic_lock_mismatch")
        packages = self.diagnostic_packages(lock, target)
        report = {
            "schema": 1,
            "commit": self.request["commit"],
            "checked_at": int(self.now()),
            "dry_run_only": True,
            "prepare_verified": False,
            "production_changed": False,
            "commands": {},
        }
        attempt = secrets.token_hex(4)
        report["attempt"] = attempt
        for phase, arguments in (
            ("namespace", []),
            ("check", ["check"]),
            (
                "dryrun",
                [
                    "install",
                    "--dry-run",
                    "--no-cache-dir",
                    "--no-index",
                    "--find-links",
                    str(inputs),
                    "--require-hashes",
                    "--only-binary=:all:",
                    "--upgrade",
                    "-r",
                    str(inputs / "core.lock"),
                ],
            ),
        ):
            self.boundary("diagnose_build_" + phase)
            self.no_stage_processes()
            unit = "prism-diagnose-" + self.request["commit"][:12] + "-" + phase + "-" + attempt + ".service"
            command = [
                "/usr/bin/systemd-run",
                "--wait",
                "--pipe",
                "--quiet",
                "--collect",
                "--unit=" + unit,
                "--uid=prism-stage",
                "--gid=prism-stage",
                "--property=PrivateNetwork=yes",
                "--property=ProtectHome=yes",
                "--property=ProtectProc=invisible",
                "--property=NoNewPrivileges=yes",
                "--property=PrivateTmp=yes",
                "--property=ProtectSystem=strict",
                "--property=InaccessiblePaths=/run -" + str(self.operations / "staging"),
                "--property=MemoryMax=768M",
                "--property=TasksMax=64",
                "--property=CPUQuota=100%",
                "--property=RuntimeMaxSec=60",
                "--property=KillMode=control-group",
                "--property=UMask=0077",
                "/usr/bin/env",
                "-i",
                "PATH=/usr/bin:/bin",
                "HOME=/nonexistent",
                "PIP_CONFIG_FILE=/dev/null",
                "PIP_NO_INPUT=1",
                "PYTHONDONTWRITEBYTECODE=1",
            ]
            if phase == "namespace":
                command.insert(command.index("/usr/bin/env"), "--property=ReadWritePaths=" + str(target))
                command.append("/usr/bin/true")
            else:
                command.extend([str(binary), "-I", "-B", "-m", "pip", *arguments])
            try:
                observed = self.diagnostic_runner(command)
                if observed["truncated"] or observed["timed_out"]:
                    self.stop_diagnostic_unit(unit)
                summary = safe_build_diagnostics(observed["output"], release=self.release, packages=packages)
                summary.update({key: observed[key] for key in ("return_code", "truncated", "timed_out")})
                journal = self.diagnostic_runner(
                    [
                        "/usr/bin/journalctl",
                        "--no-pager",
                        "-o",
                        "cat",
                        "-n",
                        "40",
                        "--since",
                        "@" + str(int(self.now()) - 120),
                        "-u",
                        unit,
                    ],
                    timeout=10,
                )
                summary["unit_journal"] = safe_build_diagnostics(
                    journal["output"], release=self.release, packages=packages
                )
                summary["unit_journal"].update(
                    {key: journal[key] for key in ("return_code", "truncated", "timed_out")}
                )
                report["commands"][phase] = summary
            finally:
                self.stop_diagnostic_unit(unit)
            if phase == "namespace" and (
                observed["return_code"] != 0 or observed["truncated"] or observed["timed_out"]
            ):
                report["pip_checks_skipped"] = "namespace_preflight_failed"
                break
        report["service_stopped"] = True
        filename = "build-diagnostic-" + secrets.token_hex(8) + ".json"
        self.write(self.record / filename, encoded(report))
        self.action_report = report

    def quarantine_failed_prepare(self):
        pointer = self.checked(self.record / "quarantine-intent.json")
        intent = json.loads(pointer.read_bytes()) if pointer.exists() else None
        resuming = isinstance(intent, dict) and intent.get("completed") is False
        self.failed_prepare_guard(candidate_required=not resuming)
        if not resuming:
            nonce = secrets.token_hex(8)
            private = self.checked(self.operations / "failed-prepares" / self.request["release_id"] / nonce)
            failed = self.checked(self.root / "opt/prism/failed")
            for path in (failed, private):
                self.checked(path).mkdir(parents=True, exist_ok=True, mode=0o700)
                path.chmod(0o700)
                if self.root == Path("/") and path.stat().st_uid != 0:
                    raise ReleaseError("root_owned_directory_required")
            sources = [
                self.release,
                self.record / "dependencies",
                self.operations / "build-inputs" / self.request["release_id"],
            ]
            destinations = [
                failed / (self.request["commit"] + "-" + nonce),
                private / "dependencies",
                private / "build-inputs",
            ]
            items = []
            for source, destination in zip(sources, destinations, strict=True):
                info = self.checked(source).lstat()
                if not stat.S_ISDIR(info.st_mode) or self.checked(destination).exists():
                    raise ReleaseError("quarantine_directory_rejected")
                if info.st_dev != destination.parent.stat().st_dev:
                    raise ReleaseError("quarantine_cross_device_rejected")
                items.append(
                    {
                        "source": str(source),
                        "destination": str(destination),
                        "device": info.st_dev,
                        "inode": info.st_ino,
                    }
                )
            intent = {
                "schema": 1,
                "commit": self.request["commit"],
                "manifest_sha256": self.request["manifest_sha256"],
                "nonce": nonce,
                "completed": False,
                "items": items,
            }
            self.write(pointer, encoded(intent))
        nonce = intent.get("nonce", "")
        private = self.operations / "failed-prepares" / self.request["release_id"] / nonce
        expected_pairs = [
            (self.release, self.root / "opt/prism/failed" / (self.request["commit"] + "-" + nonce)),
            (self.record / "dependencies", private / "dependencies"),
            (self.operations / "build-inputs" / self.request["release_id"], private / "build-inputs"),
        ]
        if (
            intent.get("schema") != 1
            or intent.get("commit") != self.request["commit"]
            or intent.get("manifest_sha256") != self.request["manifest_sha256"]
            or not re.fullmatch(r"[0-9a-f]{16}", nonce)
            or not isinstance(intent.get("items"), list)
            or len(intent["items"]) != 3
        ):
            raise ReleaseError("quarantine_intent_rejected")
        self.boundary("quarantine_failed_prepare")
        labels = ("quarantine_candidate", "quarantine_dependencies", "quarantine_build_inputs")
        for label, item, (source, destination) in zip(labels, intent["items"], expected_pairs, strict=True):
            if item.get("source") != str(source) or item.get("destination") != str(destination):
                raise ReleaseError("quarantine_intent_rejected")
            self.checked(source)
            self.checked(destination)
            if source.exists() == destination.exists():
                raise ReleaseError("quarantine_state_requires_reconciliation")
            current = source if source.exists() else destination
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (
                item.get("device"),
                item.get("inode"),
            ):
                raise ReleaseError("quarantine_identity_changed")
            if source.exists():
                self.boundary(label)
                if self.runtime()["working_directory"] != str(self.production):
                    raise ReleaseError("failed_prepare_original_runtime_required")
                self.no_stage_processes()
                self.checked(source)
                self.checked(destination)
                if not source.exists() or destination.exists():
                    raise ReleaseError("quarantine_state_requires_reconciliation")
                info = source.lstat()
                if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (
                    item.get("device"),
                    item.get("inode"),
                ):
                    raise ReleaseError("quarantine_identity_changed")
                # Rename only: EXDEV or any conflict fails closed, never copy/delete.
                source.rename(destination)
        completed = dict(intent, completed=True, checked_at=int(self.now()))
        self.write(self.record / ("quarantine-" + nonce + ".json"), encoded(completed))
        self.write(pointer, encoded(completed))

    def build_command(self, target, phase, arguments):
        self.run(
            [
                "/usr/bin/systemd-run",
                "--wait",
                "--pipe",
                "--quiet",
                "--collect",
                "--unit=prism-build-" + self.request["commit"][:12] + "-" + phase,
                "--uid=prism-stage",
                "--gid=prism-stage",
                "--property=PrivateNetwork=yes",
                "--property=ProtectHome=yes",
                "--property=ProtectProc=invisible",
                "--property=NoNewPrivileges=yes",
                "--property=PrivateTmp=yes",
                "--property=ProtectSystem=strict",
                "--property=ReadWritePaths=" + str(target),
                "--property=InaccessiblePaths=/run -" + str(self.operations / "staging"),
                "--property=MemoryMax=768M",
                "--property=TasksMax=64",
                "--property=CPUQuota=100%",
                "--property=RuntimeMaxSec=1200",
                "--property=KillMode=control-group",
                "/usr/bin/env",
                "-i",
                "PATH=/usr/bin:/bin",
                "HOME=/nonexistent",
                "PIP_CONFIG_FILE=/dev/null",
                "PIP_NO_INPUT=1",
                "PYTHONDONTWRITEBYTECODE=1",
                str(target / "bin/python"),
                "-I",
                "-B",
                "-m",
                "pip",
                *arguments,
            ],
            timeout=1250,
        )

    def stage_account(self):
        try:
            account = pwd.getpwnam("prism-stage")
        except KeyError:
            self.run(
                [
                    "/usr/sbin/useradd",
                    "--system",
                    "--user-group",
                    "--no-create-home",
                    "--shell",
                    "/usr/sbin/nologin",
                    "prism-stage",
                ]
            )
            account = pwd.getpwnam("prism-stage")
        ubuntu = pwd.getpwnam("ubuntu")
        if (
            account.pw_uid == ubuntu.pw_uid
            or account.pw_uid == 0
            or account.pw_shell != "/usr/sbin/nologin"
            or grp.getgrgid(account.pw_gid).gr_name != "prism-stage"
            or any("prism-stage" in group.gr_mem for group in grp.getgrall())
        ):
            raise ReleaseError("stage_account_not_isolated")

    def seed_stage_probes(self, stage):
        """Create disposable owners only in the not-yet-started private DB copy."""
        expected = self.checked(self.operations / "staging" / self.request["release_id"])
        database = self.checked(stage / "data/chat.db")
        if stage != expected or database == self.production / "data/chat.db":
            raise ReleaseError("stage_probe_database_rejected")
        if not database.is_file() or (stage / "data/secret_key").exists():
            raise ReleaseError("stage_probe_copy_required")
        secret = secrets.token_hex(32)
        accounts = [
            {
                "username": "stage_" + secrets.token_hex(12),
                "password": secrets.token_urlsafe(32),
                "conversation": secrets.token_hex(12),
                "report": secrets.token_hex(12),
            }
            for _ in range(2)
        ]
        # This is the application's existing legacy hash contract. Real HTTP
        # login upgrades each synthetic hash to bcrypt inside the isolated app.
        # No candidate or inherited package is imported by this root process.
        self.write(stage / "data/secret_key", secret.encode())
        with closing(sqlite3.connect(database)) as db, db:
            for account in accounts:
                db.execute(
                    "INSERT INTO users(username,password_hash) VALUES(?,?)",
                    [account["username"], digest((account["password"] + secret).encode())],
                )
                db.execute(
                    "INSERT INTO conversations(id,username,title,skill) VALUES(?,?,?,'prism-research')",
                    [account["conversation"], account["username"], "Prism isolated acceptance"],
                )
                db.execute(
                    "INSERT INTO messages(conv_id,role,content) VALUES(?,'assistant',?)",
                    [account["conversation"], "Prism synthetic acceptance narrative"],
                )
                db.execute(
                    "INSERT INTO saved_reports(id,username,conv_id,title,report_json,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,1,1)",
                    [
                        account["report"],
                        account["username"],
                        account["conversation"],
                        "Prism isolated acceptance",
                        '{"summary":"Prism synthetic acceptance narrative"}',
                    ],
                )
        database.chmod(0o600)
        self.write(self.record / "stage-probe-credentials.json", encoded({"accounts": accounts}))
        return accounts

    def stage_http(self, stage, method, path, body=None, cookie=""):
        """Bounded stdlib HTTP over the fixed private socket; never TCP/proxies."""
        if (
            stage != self.operations / "staging" / self.request["release_id"]
            or method not in {"GET", "POST", "PUT", "DELETE"}
            or not re.fullmatch(r"/[a-zA-Z0-9/_\-.]+", path)
        ):
            raise ReleaseError("stage_http_target_rejected")
        connection = http.client.HTTPConnection("localhost", timeout=5)
        connection.auto_open = 0  # Never fall back to TCP if the fixed UDS closes.
        channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            channel.settimeout(5)
            channel.connect(str(self.stage_socket(stage)))
            connection.sock = channel
            headers = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
            if cookie:
                headers["Cookie"] = cookie
            payload = None if body is None else encoded(body)
            if payload is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ReleaseError("stage_http_response_limit")
            cookies = SimpleCookie()
            for key, value in response.getheaders():
                if key.lower() == "set-cookie":
                    cookies.load(value)
            # Responses and cookie values never enter the public or private
            # result record. Retain only the application's auth cookie in memory.
            token = cookies.get("token")
            result_cookie = "token=" + token.value if token is not None and token.value else ""
            if result_cookie and not re.fullmatch(r"token=[0-9a-f]{64}", result_cookie):
                raise ReleaseError("stage_http_cookie_rejected")
            try:
                data = json.loads(raw) if raw else {}
            except (ValueError, UnicodeError):
                # Keep the actual status for a failed/non-JSON response without
                # retaining its body (which may contain provider diagnostics).
                data = {}
            return response.status, data, result_cookie
        finally:
            connection.close()
            channel.close()

    def stage_journeys(self, stage, accounts, application):
        """Exercise only synthetic owners through real authenticated routes."""
        checks = {}
        result = {
            "schema": 1,
            "commit": self.request["commit"],
            "served_commit": "unknown",
            "checked_at": int(self.now()),
            "synthetic_owners": len(accounts),
            "checks": checks,
            "passed": False,
            "external_integrations_verified": False,
            "http_status_by_check": {},
        }
        last_status = None

        def request(method, path, body=None, cookie=""):
            nonlocal last_status
            self.stop()
            response = self.stage_http(stage, method, path, body, cookie)
            last_status = response[0]
            return response

        def require(name, condition):
            checks[name] = condition is True
            if last_status is not None:
                result["http_status_by_check"][name] = last_status
            if not checks[name]:
                raise ReleaseError("stage_journey_failed")

        try:
            require("two_synthetic_owners", len(accounts) == 2)
            status, ready, _ = request("GET", "/health/ready")
            observed = ready.get("deployed_revision") if isinstance(ready, dict) else None
            if isinstance(observed, str) and SHA.fullmatch(observed):
                result["served_commit"] = observed
            require(
                "ready_exact_commit",
                status == 200
                and ready.get("status") == "ready"
                and ready.get("deployed_revision") == self.request["commit"],
            )
            require("anonymous_denied", request("GET", "/api/me")[0] == 401)
            for index, account in enumerate(accounts):
                prefix = "owner_" + str(index + 1) + "_"
                other = accounts[1 - index]
                status, _, cookie = request(
                    "POST",
                    "/api/login",
                    {
                        "username": account["username"],
                        "password": account["password"],
                    },
                )
                require(prefix + "login", status == 200 and bool(cookie))
                status, profile, _ = request("GET", "/api/me", cookie=cookie)
                require(
                    prefix + "identity",
                    status == 200
                    and profile.get("user") == account["username"]
                    and profile.get("is_admin") is False,
                )
                status, conversations, _ = request("GET", "/api/convs", cookie=cookie)
                require(
                    prefix + "conversation_list",
                    status == 200
                    and isinstance(conversations, list)
                    and [row.get("id") for row in conversations] == [account["conversation"]],
                )
                status, history, _ = request("GET", "/api/convs/" + account["conversation"], cookie=cookie)
                require(
                    prefix + "history",
                    status == 200
                    and history.get("id") == account["conversation"]
                    and any(
                        row.get("content") == "Prism synthetic acceptance narrative"
                        for row in history.get("messages", [])
                    ),
                )
                status, reports, _ = request("GET", "/api/reports/library", cookie=cookie)
                require(
                    prefix + "report_list",
                    status == 200
                    and [row.get("id") for row in reports.get("reports", [])] == [account["report"]],
                )
                status, report, _ = request("GET", "/api/reports/library/" + account["report"], cookie=cookie)
                require(
                    prefix + "report",
                    status == 200
                    and report.get("id") == account["report"]
                    and report.get("report", {}).get("summary") == "Prism synthetic acceptance narrative",
                )
                require(
                    prefix + "foreign_conversation",
                    request("GET", "/api/convs/" + other["conversation"], cookie=cookie)[0] == 404,
                )
                require(
                    prefix + "foreign_report",
                    request("GET", "/api/reports/library/" + other["report"], cookie=cookie)[0] == 404,
                )
                if application == "server.recovery:app":
                    for method, path in (
                        ("POST", "/api/convs"),
                        ("PUT", "/api/watchlist"),
                        ("DELETE", "/api/reports/library/" + account["report"]),
                    ):
                        require(prefix + "recovery_" + method, request(method, path, {}, cookie)[0] == 503)
                    require(
                        prefix + "shared_file_denied",
                        request(
                            "GET",
                            "/api/reports/" + account["conversation"] + "/stage-shared.html",
                            cookie=cookie,
                        )[0]
                        == 404,
                    )
                elif application == "server.main:app":
                    status, created, _ = request("POST", "/api/convs", {}, cookie)
                    created_id = created.get("id", "")
                    require(
                        prefix + "create", status == 200 and bool(re.fullmatch(r"[0-9a-f]{24}", created_id))
                    )
                    path = "/api/convs/" + created_id
                    require(prefix + "created_read", request("GET", path, cookie=cookie)[0] == 200)
                    require(
                        prefix + "update",
                        request("PUT", path + "/skill", {"skill": "prism-research"}, cookie)[0] == 200,
                    )
                    require(prefix + "delete", request("DELETE", path, cookie=cookie)[0] == 200)
                    require(prefix + "deleted_hidden", request("GET", path, cookie=cookie)[0] == 404)
                else:
                    raise ReleaseError("stage_application_rejected")
                require(prefix + "logout", request("POST", "/api/logout", {}, cookie)[0] == 200)
                require(prefix + "session_revoked", request("GET", "/api/me", cookie=cookie)[0] == 401)
            result["passed"] = True
        except Exception as error:
            # No HTTP body, exception message, synthetic password or cookie.
            result["check_count"] = len(checks)
            self.write(self.record / "stage-journeys.json", encoded(result))
            if isinstance(error, ReleaseError) and str(error) in {
                "operator_stop",
                "fresh_operator_permit_required",
                "permit_timeout",
            }:
                raise
            raise ReleaseError("stage_journey_failed_check_private_record") from None
        result["check_count"] = len(checks)
        self.write(self.record / "stage-journeys.json", encoded(result))
        return digest(encoded(result))

    def stage(self):
        self.verify_tree()
        stage = self.checked(self.operations / "staging" / self.request["release_id"])
        socket = self.stage_socket(stage)
        if stage.exists():
            raise ReleaseError("stage_already_exists")
        source_db = self.checked(self.production / "data/chat.db")
        self.space(self.operations, source_db.stat().st_size * 3)
        self.boundary("stage_private_copy")
        self.stage_account()
        self.public_directory(stage.parent)
        stage.mkdir(mode=0o700)
        data = stage / "data"
        data.mkdir(mode=0o700)
        with (
            closing(sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(data / "chat.db")) as destination,
            destination,
        ):
            source.backup(destination)
            tables = {
                row[0] for row in destination.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "sessions" in tables:
                destination.execute("DELETE FROM sessions")
            if "capability_runs" in tables:
                destination.execute("UPDATE capability_runs SET revoked=1")
        accounts = self.seed_stage_probes(stage)
        # A copied DB is for migration/auth/history checks only. Runtime state is not copied.
        home = stage / "home"
        home.mkdir(mode=0o700)
        self.run(["/usr/bin/chown", "-R", "prism-stage:prism-stage", str(stage)])
        self.run(["/usr/bin/chown", "prism-stage:ubuntu", str(stage)])
        stage.chmod(0o750)
        self.boundary("stage_isolated_service")
        production_pid = self.process_id(SERVICE)
        nonce = secrets.token_hex(16)
        issued_at = int(self.now())
        application = self.application(self.release, self.record)
        self.run(
            [
                "/usr/bin/systemd-run",
                "--unit=prism-stage-" + self.request["commit"][:12],
                "--collect",
                "--uid=prism-stage",
                "--gid=prism-stage",
                "--property=WorkingDirectory=" + str(self.release),
                "--property=NoNewPrivileges=yes",
                "--property=PrivateNetwork=yes",
                "--property=ProtectHome=yes",
                "--property=ProtectProc=invisible",
                "--property=PrivateTmp=yes",
                "--property=ProtectSystem=strict",
                "--property=ReadWritePaths=" + str(stage),
                "--property=InaccessiblePaths=/run",
                "--property=MemoryMax=768M",
                "--property=TasksMax=64",
                "--property=CPUQuota=100%",
                "--property=RuntimeMaxSec=1800",
                "--property=KillMode=control-group",
                "--property=UMask=0077",
                "/usr/bin/env",
                "-i",
                "PATH=/usr/bin:/bin",
                "HOME=" + str(home),
                "STOCK_DATA_DIR=" + str(data),
                "STOCK_LOG_FILE=" + str(stage / "app.log"),
                "CLAUDE_BIN=/nonexistent/prism-stage-claude",
                "CLAUDE_SKILLS_HOME=" + str(home / "skills"),
                "PRISM_ASTOCK_PROJECT=/nonexistent/prism-stage-astock",
                "PRISM_VIBE_ENGINE_URL=http://127.0.0.1:9",
                "PRISM_INTERNAL_API_URL=http://127.0.0.1:18888",
                "PYTHONDONTWRITEBYTECODE=1",
                "PRISM_SINGLE_WORKER=1",
                "PRISM_ENVIRONMENT=staging",
                "PRISM_COOKIE_SECURE=0",
                str(self.release / ".venv/bin/python"),
                "-I",
                "-B",
                "-S",
                "-c",
                STAGE_BOOTSTRAP,
                self.request["commit"],
                self.request["release_id"],
                str(production_pid),
                nonce,
                str(issued_at),
                application,
            ]
        )
        deadline = time.monotonic() + 30
        while not socket.exists() and time.monotonic() < deadline:
            self.stop()
            time.sleep(0.1)
        if not self.is_socket(socket):
            raise ReleaseError("stage_socket_unavailable")
        identity = self.stage_process_identity(production_pid)
        proof = self.stage_proof(stage, nonce, issued_at, production_pid, identity["stage_pid"], application)
        self.write(self.record / "stage-isolation.json", encoded({"proof": proof, "identity": identity}))
        self.publish_stage_socket(socket)
        journeys_digest = self.stage_journeys(stage, accounts, application)
        self.write(
            self.record / "staged.json",
            encoded(
                {
                    "socket": str(socket),
                    "external_integrations_verified": False,
                    "isolation_proof_sha256": digest(encoded(proof)),
                    "journeys_sha256": journeys_digest,
                }
            ),
        )

    def process_id(self, unit):
        value = self.run(["/usr/bin/systemctl", "show", "--value", "--property=MainPID", unit]).strip()
        if not re.fullmatch(rb"[1-9][0-9]{0,9}", value) or int(value) <= 1:
            raise ReleaseError("stage_process_identity_unavailable")
        return int(value)

    def stage_process_identity(self, production_pid):
        unit = "prism-stage-" + self.request["commit"][:12]
        stage_pid = self.process_id(unit)
        user = self.run(["/usr/bin/systemctl", "show", "--value", "--property=User", unit]).strip()
        process = self.root / "proc" / str(stage_pid)
        production = self.root / "proc" / str(production_pid)
        if (
            self.process_id(SERVICE) != production_pid
            or stage_pid == production_pid
            or user != b"prism-stage"
            or process.stat().st_uid != pwd.getpwnam("prism-stage").pw_uid
            or (process / "ns/net").readlink() == (production / "ns/net").readlink()
        ):
            raise ReleaseError("stage_process_identity_mismatch")
        return {
            "stage_pid": stage_pid,
            "production_pid": production_pid,
            "dedicated_user": True,
            "separate_network_namespace": True,
            "checked_at": int(self.now()),
        }

    def stage_proof(self, stage, nonce, issued_at, production_pid, stage_pid, application):
        path = self.checked(stage / "isolation-proof.json")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096 or stat.S_IMODE(info.st_mode) != 0o600:
                raise ReleaseError("stage_isolation_proof_rejected")
            proof = json.loads(source.read(4097))
        if not isinstance(proof, dict):
            raise ReleaseError("stage_isolation_proof_rejected")
        expected = {
            "schema": 1,
            "commit": self.request["commit"],
            "release_id": self.request["release_id"],
            "nonce": nonce,
            "issued_at": issued_at,
            "production_pid": production_pid,
            "stage_pid": stage_pid,
            "application": application,
        }
        allowed = {
            "production_environment": {"permission_denied", "operation_denied", "not_visible"},
            "production_process_environment": {"permission_denied", "operation_denied", "not_visible"},
            "production_http": {
                "connection_refused",
                "network_unreachable",
                "host_unreachable",
                "permission_denied",
                "operation_denied",
            },
            "sidecar_http": {
                "connection_refused",
                "network_unreachable",
                "host_unreachable",
                "permission_denied",
                "operation_denied",
            },
            "candidate_write": {"permission_denied", "operation_denied", "read_only_filesystem"},
        }
        checks = proof.get("checks", {})
        if (
            set(proof) != {*expected, "checked_at", "checks"}
            or any(
                type(proof.get(key)) is not type(value) or proof[key] != value
                for key, value in expected.items()
            )
            or type(proof.get("checked_at")) is not int
            or not issued_at <= proof["checked_at"] <= int(self.now()) <= issued_at + 120
            or not isinstance(checks, dict)
            or set(checks) != set(allowed)
            or any(
                not isinstance(checks[name], dict)
                or set(checks[name]) != {"blocked", "errno"}
                or checks[name]["blocked"] is not True
                or not isinstance(checks[name]["errno"], str)
                or checks[name]["errno"] not in categories
                for name, categories in allowed.items()
            )
        ):
            raise ReleaseError("stage_isolation_proof_rejected")
        return proof

    @staticmethod
    def stage_socket(stage):
        socket = stage / "stage.sock"
        if len(str(socket).encode()) >= 104:
            raise ReleaseError("stage_socket_path_too_long")
        return socket

    @staticmethod
    def is_socket(path):
        return stat.S_ISSOCK(path.lstat().st_mode)

    def publish_stage_socket(self, path):
        # The stage UID can replace this filename. Pin an inode with Linux O_PATH
        # and mutate only our own proc-fd reference, never the replaceable name.
        if not hasattr(os, "O_PATH"):
            raise ReleaseError("stage_socket_pinning_unavailable")
        descriptor = os.open(path, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            original = os.fstat(descriptor)
            uid = pwd.getpwnam("prism-stage").pw_uid

            def same_socket():
                current = path.lstat()
                return stat.S_ISSOCK(current.st_mode) and (current.st_dev, current.st_ino) == (
                    original.st_dev,
                    original.st_ino,
                )

            if (
                not stat.S_ISSOCK(original.st_mode)
                or original.st_uid != uid
                or original.st_nlink != 1
                or not same_socket()
            ):
                raise ReleaseError("stage_socket_identity_rejected")
            pinned = "/proc/self/fd/" + str(descriptor)
            os.chown(pinned, uid, grp.getgrnam("ubuntu").gr_gid)
            os.chmod(pinned, 0o660)
            if not same_socket() or os.fstat(descriptor).st_nlink != 1:
                raise ReleaseError("stage_socket_identity_rejected")
        finally:
            os.close(descriptor)

    def switch(self):
        self.verify_tree()
        self.receipt("backup")
        self.receipt("compatibility")
        stage = self.receipt("stage")
        if (
            stage.get("isolated") is not True
            or stage.get("journeys_passed") is not True
            or not (self.record / "staged.json").exists()
        ):
            raise ReleaseError("stage_verification_required")
        self.assert_admission()
        expected = self.request["receipts"].get("expected_source_manifest_sha256")
        current = self.runtime()
        if current["fingerprint"] != self.request["receipts"].get("expected_current_fingerprint"):
            raise ReleaseError("current_runtime_changed")
        if (
            self.source_inventory(self.request.get("source_manifest", {}), Path(current["working_directory"]))
            != expected
        ):
            raise ReleaseError("source_manifest_receipt_mismatch")
        self.no_active_tasks()
        self.recovery_target()
        self.run(["/usr/bin/systemctl", "is-active", "--quiet", SERVICE])
        if (self.record / "previous.json").exists():
            raise ReleaseError("switch_already_attempted")
        previous = {
            "existed": self.checked(self.override).exists(),
            "content": base64.b64encode(self.override.read_bytes()).decode()
            if self.override.exists()
            else "",
        }
        self.boundary("production_override")
        # Must remain private: an old drop-in may contain credentials.
        self.write(self.record / "previous.json", encoded(previous))
        override = self.dropin(self.release)
        self.write(
            self.record / "cutover.json",
            encoded(
                {"at": int(self.now()), "previous_runtime": current, "override_sha256": digest(override)}
            ),
        )
        self.write(self.override, override, 0o600)
        self.journal("cutover_config_written", "completed")
        self.boundary("production_daemon_reload")
        self.run(["/usr/bin/systemctl", "daemon-reload"])
        self.boundary("production_restart")
        self.run(["/usr/bin/systemctl", "restart", SERVICE])
        self.write(self.record / "switched.json", encoded({"at": int(self.now())}))

    def application(self, release, record):
        manifest = json.loads((record / "manifest.json").read_bytes())
        if manifest.get("commit") != release.name:
            raise ReleaseError("application_identity_mismatch")
        if str(manifest.get("version", "")).endswith("-recovery"):
            if not any(item.get("path") == "server/recovery.py" for item in manifest.get("files", [])):
                raise ReleaseError("restricted_recovery_entry_required")
            return "server.recovery:app"
        return "server.main:app"

    def dropin(self, release):
        external = (release / ".engine-revision").read_text().strip()
        if not DIGEST.fullmatch(external):
            raise ReleaseError("fixed_engine_revision_required")
        engine = self.checked(self.root / "opt/prism/engines/astock" / external)
        recovery = self.request["receipts"].get("recovery", {})
        if release.name == self.request["commit"]:
            record = self.record
        elif release.name == recovery.get("commit"):
            record = self.checked(self.operations / recovery["release_id"])
        else:
            raise ReleaseError("application_identity_mismatch")
        application = self.application(release, record)
        cli = self.verify_cli(record, release)
        return (
            "[Service]\nNoNewPrivileges=yes\nUMask=0077\nWorkingDirectory="
            + str(release)
            + "\nExecStart=\nExecStart=/usr/bin/env CLAUDE_BIN="
            + str(cli)
            + " "
            + str(release / ".venv/bin/python")
            + " -I -B -m uvicorn "
            + application
            + " --app-dir "
            + str(release)
            + " --host 127.0.0.1 --port 8088 --workers 1\nEnvironmentFile="
            + str(self.production / ".env")
            + "\nEnvironment=STOCK_DATA_DIR="
            + str(self.production / "data")
            + "\nEnvironment=STOCK_LOG_FILE="
            + str(self.production / "app.log")
            + "\nEnvironment=PRISM_INTERNAL_API_URL=http://127.0.0.1:8088\nEnvironment=PRISM_ASTOCK_PROJECT="
            + str(engine)
            + "\nEnvironment=PRISM_SINGLE_WORKER=1\nEnvironment=PRISM_ENVIRONMENT=production\nEnvironment=PYTHONDONTWRITEBYTECODE=1\n"
        ).encode()

    def recovery_target(self):
        recovery = self.receipt("recovery")
        if (
            not SHA.fullmatch(recovery.get("commit", ""))
            or recovery["commit"] == self.request["commit"]
            or not RELEASE_ID.fullmatch(recovery.get("release_id", ""))
        ):
            raise ReleaseError("safe_recovery_identity_required")
        if not all(
            recovery.get(key) is True
            for key in ("security_verified", "journeys_passed", "database_forward_and_backward")
        ):
            raise ReleaseError("safe_recovery_acceptance_required")
        saved = self.release, self.record, self.request
        try:
            self.release = self.checked(self.releases / recovery["commit"])
            self.record = self.checked(self.operations / recovery["release_id"])
            self.request = dict(self.request, commit=recovery["commit"])
            identity = json.loads((self.record / "identity.json").read_bytes())
            if (
                any(
                    identity.get(key) != recovery.get(key)
                    for key in ("commit", "archive_sha256", "manifest_sha256")
                )
                or not (self.record / "staged.json").exists()
            ):
                raise ReleaseError("recovery_candidate_not_bound")
            self.verify_tree()
            if self.application(self.release, self.record) != "server.recovery:app":
                raise ReleaseError("restricted_recovery_entry_required")
            return self.release
        finally:
            self.release, self.record, self.request = saved

    def verify(self):
        if not (self.record / "switched.json").exists():
            raise ReleaseError("cutover_not_recorded")
        self.verify_tree()
        journey = self.receipt("journeys")
        if (
            journey.get("served_commit") != self.request["commit"]
            or journey.get("journeys_passed") is not True
        ):
            raise ReleaseError("core_journeys_required")
        data = self.ready()
        if (
            data.get("status") not in {"ok", "ready"}
            or data.get("deployed_revision") != self.request["commit"]
        ):
            raise ReleaseError("served_revision_not_verified")
        self.write(
            self.record / "verified.json",
            encoded(
                {
                    "commit": self.request["commit"],
                    "at": int(self.now()),
                    "receipt": self.request["receipts_sha256"],
                }
            ),
        )

    def nginx_paths(self):
        result = {}
        for name in NGINX_SITES:
            expected = self.root / "etc/nginx/sites-available" / name
            enabled = self.root / "etc/nginx/sites-enabled" / name
            if enabled.resolve(strict=True) != expected or self.checked(expected).stat().st_size > 65536:
                raise ReleaseError("nginx_fixed_target_mismatch")
            if self.root == Path("/") and expected.stat().st_uid != 0:
                raise ReleaseError("nginx_root_owner_required")
            result[name] = expected
        return result

    def probe_admission(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise ReleaseError("admission_redirect_rejected")

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        for url in ("https://doubleh.site/api/me", "http://124.223.168.224/api/me"):
            try:
                response = opener.open(url, timeout=10)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                data = response.read(4097)
                if (
                    response.code != 503
                    or response.headers.get("Retry-After") != "60"
                    or len(data) > 4096
                    or json.loads(data).get("error") != HOLD_MESSAGE
                ):
                    raise ReleaseError("admission_hold_not_observed")

    def assert_admission(self):
        record = json.loads((self.record / "admission.json").read_bytes())
        if (self.record / "resumed.json").exists():
            raise ReleaseError("admission_already_resumed")
        for name, path in self.nginx_paths().items():
            if digest(path.read_bytes()) != record["files"][name]["hold_sha256"]:
                raise ReleaseError("admission_configuration_changed")
        self.probe_admission()

    def nginx_write(self, record, destination):
        paths = self.nginx_paths()
        allowed = {
            name: {item["original_sha256"], item["hold_sha256"]} for name, item in record["files"].items()
        }
        for name, path in paths.items():
            if digest(path.read_bytes()) not in allowed[name]:
                raise ReleaseError("nginx_configuration_changed")
        for name, path in paths.items():
            item = record["files"][name]
            content = base64.b64decode(item[destination], validate=True)
            if digest(content) != item[destination + "_sha256"]:
                raise ReleaseError("private_nginx_record_corrupted")
            self.write(path, content, item["mode"])
            self.run(
                ["/usr/bin/chown", "--no-dereference", str(item["uid"]) + ":" + str(item["gid"]), str(path)]
            )

    def quiesce(self):
        self.verify_tree()
        recovery = self.recovery_target()
        stage = self.receipt("stage")
        if (
            not stage.get("isolated")
            or not stage.get("journeys_passed")
            or not (self.record / "staged.json").exists()
        ):
            raise ReleaseError("stage_verification_required")
        self.boundary("stop_accepted_staging_services")
        for commit in (self.request["commit"], recovery.name):
            unit = "prism-stage-" + commit[:12] + ".service"
            state = self.run(
                ["/usr/bin/systemctl", "show", "--property=LoadState,ActiveState", unit]
            ).decode()
            values = dict(line.split("=", 1) for line in state.splitlines() if "=" in line)
            if values.get("ActiveState") in {"active", "activating", "deactivating", "reloading"}:
                self.run(["/usr/bin/systemctl", "stop", unit])
            elif (
                values.get("ActiveState") not in {"inactive", "failed"}
                and values.get("LoadState") != "not-found"
            ):
                raise ReleaseError("staging_stop_state_unknown")
        admission = self.receipt("admission")
        path = self.record / "admission.json"
        if path.exists():
            record = json.loads(path.read_bytes())
            if any(
                record["files"][name]["original_sha256"] != admission.get("config_sha256", {}).get(name)
                for name in NGINX_SITES
            ):
                raise ReleaseError("nginx_baseline_receipt_changed")
        else:
            record = {"runtime": self.runtime(), "files": {}}
            for name, target in self.nginx_paths().items():
                original = target.read_bytes()
                if digest(original) != admission.get("config_sha256", {}).get(name):
                    raise ReleaseError("nginx_baseline_drift")
                hold = admission_config(original)
                record["files"][name] = {
                    "original": base64.b64encode(original).decode(),
                    "hold": base64.b64encode(hold).decode(),
                    "original_sha256": digest(original),
                    "hold_sha256": digest(hold),
                    "mode": stat.S_IMODE(target.stat().st_mode),
                    "uid": target.stat().st_uid,
                    "gid": target.stat().st_gid,
                }
            self.write(path, encoded(record))  # root 0700 record; contents never public
        self.boundary("admission_hold_config")
        if (self.record / "resumed.json").exists():
            (self.record / "resumed.json").unlink()
        self.nginx_write(record, "hold")
        try:
            self.run(["/usr/sbin/nginx", "-t"])
        except Exception:
            self.nginx_write(record, "original")
            raise ReleaseError("nginx_hold_test_failed_original_restored") from None
        self.boundary("admission_hold_reload")
        self.run(["/usr/bin/systemctl", "reload", "nginx.service"])
        self.assert_admission()

    def resume(self):
        record = json.loads((self.record / "admission.json").read_bytes())
        if (self.record / "resumed.json").exists():
            raise ReleaseError("admission_already_resumed")
        if (self.record / "rollback.json").exists():
            receipt = self.receipt("recovery_journeys")
            expected = self.request["receipts"]["recovery"]["commit"]
            if receipt.get("served_commit") != expected or receipt.get("journeys_passed") is not True:
                raise ReleaseError("recovery_journeys_required")
        elif (self.record / "verified.json").exists():
            expected = self.request["commit"]
        elif (
            not (self.record / "cutover.json").exists()
            and self.runtime()["fingerprint"] == record["runtime"]["fingerprint"]
        ):
            expected = None  # aborted before any service cutover; legacy runtime unchanged
        else:
            raise ReleaseError("resume_verification_required")
        if expected:
            ready = self.ready()
            if ready.get("status") not in {"ok", "ready"} or ready.get("deployed_revision") != expected:
                raise ReleaseError("resume_revision_not_ready")
        self.boundary("admission_resume_config")
        self.nginx_write(record, "original")
        try:
            self.run(["/usr/sbin/nginx", "-t"])
        except Exception:
            self.nginx_write(record, "hold")
            raise ReleaseError("nginx_resume_test_failed_hold_preserved") from None
        try:
            self.boundary("admission_resume_reload")
            self.run(["/usr/bin/systemctl", "reload", "nginx.service"])
        except Exception as error:
            self.nginx_write(record, "hold")
            if isinstance(error, ReleaseError) and str(error) in {
                "operator_stop",
                "fresh_operator_permit_required",
                "permit_timeout",
            }:
                raise
            raise ReleaseError("nginx_resume_reload_unconfirmed_reconcile_required") from None
        self.write(self.record / "resumed.json", encoded({"at": int(self.now())}))

    def rollback(self):
        self.receipt("compatibility")
        self.assert_admission()
        if not (self.record / "cutover.json").exists():
            raise ReleaseError("cutover_not_attempted")
        cutover = json.loads((self.record / "cutover.json").read_bytes())
        if (self.record / "rollback.json").exists():
            raise ReleaseError("rollback_already_completed")
        recovery = self.recovery_target()
        recovery_override = self.dropin(recovery)
        intent = {
            "recovery_commit": recovery.name,
            "override_sha256": digest(recovery_override),
            "cutover_sha256": cutover["override_sha256"],
        }
        intent_path = self.record / "recovery-intent.json"
        if intent_path.exists() and json.loads(intent_path.read_bytes()) != intent:
            raise ReleaseError("recovery_intent_changed")
        if not self.checked(self.override).is_file() or digest(self.override.read_bytes()) not in {
            cutover.get("override_sha256"),
            digest(recovery_override),
        }:
            raise ReleaseError("rollback_configuration_changed")
        self.boundary("recovery_override")
        self.write(intent_path, encoded(intent))
        self.write(self.override, recovery_override, 0o600)
        self.journal("recovery_config_written", "completed")
        self.boundary("recovery_daemon_reload")
        self.run(["/usr/bin/systemctl", "daemon-reload"])
        self.boundary("recovery_restart")
        self.run(["/usr/bin/systemctl", "restart", SERVICE])
        self.write(
            self.record / "rollback.json",
            encoded({"at": int(self.now()), "recovery_commit": recovery.name, "verification_required": True}),
        )
        # A completed rollback action is not a verified release. No DB restore.

    def execute(self, request):
        if (
            request.get("action") not in ACTIONS
            or not SHA.fullmatch(request.get("commit", ""))
            or not RELEASE_ID.fullmatch(request.get("release_id", ""))
        ):
            raise ReleaseError("invalid_request")
        self.request = request
        self.action_report = None
        self.release = self.checked(self.releases / request["commit"])
        self.record = self.checked(self.operations / request["release_id"])
        if request["action"] == "inspect":
            return {
                "runtime": self.runtime(),
                "release_exists": self.release.exists(),
                "stop_requested": (self.operations / "STOP").exists(),
                "claude_binary_exists": (self.root / "home/ubuntu/.npm-global/bin/claude").is_file(),
                "production_verified": False,
            }
        self.receipt("backup")
        self.receipt("compatibility")
        self.boundary("begin_" + request["action"])
        self.public_directory(self.operations)
        lock = self.checked(self.operations / "writer.lock").open("a")
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ReleaseError("another_writer_active") from None
            self.record.mkdir(mode=0o700, exist_ok=True)
            if self.root == Path("/") and self.record.stat().st_uid != 0:
                raise ReleaseError("root_owned_record_required")
            self.record.chmod(0o700)
            identity = self.record / "identity.json"
            bound = {key: request.get(key) for key in ("commit", "archive_sha256", "manifest_sha256")}
            if identity.exists() and json.loads(identity.read_bytes()) != bound:
                raise ReleaseError("release_identity_changed")
            if not identity.exists():
                self.write(identity, encoded(bound))
            self.journal(request["action"], "started")
            try:
                getattr(self, request["action"])()
                result = self.journal(
                    "verified" if request["action"] == "verify" else request["action"], "completed"
                )
                if request["action"] == "diagnose_build":
                    result["diagnostics"] = self.action_report
                return result
            except Exception as error:
                self.journal(
                    request["action"],
                    "cancelled"
                    if isinstance(error, ReleaseError)
                    and str(error) in {"operator_stop", "fresh_operator_permit_required", "permit_timeout"}
                    else "failed",
                    failure=error,
                )
                raise
        finally:
            lock.close()


def main():
    if os.geteuid() != 0 or sys.platform != "linux":
        raise ReleaseError("root_executor_required")
    length = json.loads(sys.stdin.buffer.readline(128)).get("length", 0)
    if not isinstance(length, int) or not 0 < length <= MAX_INPUT:
        raise ReleaseError("input_limit")
    memory = Path("/proc/meminfo").read_text()
    available = int(re.search(r"^MemAvailable:\s+(\d+) kB$", memory, re.M)[1]) * 1024
    if available < length * 3 + 256 * 1024 * 1024:
        raise ReleaseError("insufficient_memory_reserve")
    raw = sys.stdin.buffer.read(length)
    if len(raw) != length or sys.stdin.buffer.read(1) != b"\n":
        raise ReleaseError("input_truncated")

    def read_control():
        line = sys.stdin.buffer.readline(1024)
        if not line:
            raise ReleaseError("controller_disconnected")
        return json.loads(line)

    def control():
        if select.select([sys.stdin.buffer], [], [], 0)[0]:
            if read_control().get("stop") is True:
                return True
            raise ReleaseError("unexpected_control_message")
        return False

    def permit(nonce, boundary):
        print(json.dumps({"permit_required": nonce, "boundary": boundary}), flush=True)
        if not select.select([sys.stdin.buffer], [], [], 30)[0]:
            raise ReleaseError("permit_timeout")
        return read_control().get("permit")

    result = RemoteRelease(permit=permit, control=control).execute(json.loads(raw))
    print(json.dumps({"result": result}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            json.dumps({"error": failure_code(error)}),
            flush=True,
        )
        raise SystemExit(1) from None
