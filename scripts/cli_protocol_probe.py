#!/usr/bin/env python3
"""Local Anthropic protocol fixture, never a live model/search acceptance test.

The reviewed controller must first prove isolation and publish a root allow
bound to this SAME PID/nonce, then compile/call this helper in that process.
Missing or another process's bootstrap proof is blocked. No bootstrap is
generated here. CLI sessions contain public synthetic text only and are erased.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib
import json
import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

MARKER = "PRISM_PROTOCOL_FIXTURE_OK"
PROMPT = "Prism public protocol fixture. Reply exactly " + MARKER + "."
RESUME_PROMPT = "Prism public protocol fixture continuation. Reply exactly " + MARKER + "."
MODEL = "deepseek-v4-flash"
TOKEN = "PRISM_PUBLIC_SYNTHETIC_PROTOCOL_TOKEN"
PHASES = ("stream_new", "stream_resume", "auxiliary")
NATIVE_TOOLS = "WebSearch,WebFetch,AskUserQuestion"
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
NONCE = re.compile(r"[0-9a-f]{32}")
SESSION = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
BOOT_CHECKS = {
    "production_environment_denied",
    "production_process_environment_denied",
    "production_http_denied",
    "sidecar_http_denied",
    "candidate_write_denied",
    "staging_directory_denied",
}


class FixtureError(RuntimeError):
    """Safe category; never includes request bodies, child output or headers."""


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def locked(path):
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise FixtureError("locked_source_required")


def fixed_paths(candidate, cli_sha256, workdir):
    if not SHA.fullmatch(candidate) or not DIGEST.fullmatch(cli_sha256):
        raise FixtureError("invalid_identity")
    work = Path(workdir)
    if (
        str(work) != workdir
        or work.parent.parent != Path("/var/lib/prism-operations/integration")
        or work.name != "cli-protocol"
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", work.parent.name)
    ):
        raise FixtureError("fixed_workdir_required")
    return (
        Path("/opt/prism/releases") / candidate,
        Path("/opt/prism/tools/claude") / cli_sha256 / "claude",
        work,
    )


def read_record(path, *, root=False, limit=8192):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != (0 if root else os.getuid())
            or stat.S_IMODE(info.st_mode) != (0o444 if root else 0o600)
            or info.st_size > limit
        ):
            raise FixtureError("bootstrap_record_rejected")
        raw = stream.read(limit + 1)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise FixtureError("bootstrap_record_rejected")
    return value, raw


def require_bootstrap(args, candidate, cli, work, *, now=time.time):
    """Consume only a fresh controller proof/allow for this exact worker PID."""
    if (
        sys.platform != "linux"
        or not sys.flags.isolated
        or not sys.flags.no_site
        or not sys.flags.dont_write_bytecode
        or Path(sys.executable) != candidate / ".venv/bin/python"
        or not NONCE.fullmatch(args.nonce)
        or not DIGEST.fullmatch(args.probe_sha256)
    ):
        raise FixtureError("isolated_interpreter_required")
    account = pwd.getpwuid(os.getuid())
    if (
        account.pw_uid == 0
        or account.pw_name != "prism-integration"
        or account.pw_shell != "/usr/sbin/nologin"
        or any(group != account.pw_gid for group in os.getgroups())
    ):
        raise FixtureError("dedicated_user_required")
    for path in (candidate, candidate / ".venv", cli, work, work / "probe.py"):
        locked(path)
    if stat.S_IMODE(work.stat().st_mode) != 0o755:
        raise FixtureError("private_workdir_required")
    for name in ("output", "home", "tmp", "data"):
        path = work / name
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise FixtureError("private_workdir_required")
        if name != "output" and any(path.iterdir()):
            raise FixtureError("synthetic_workdir_not_empty")
    source = work / "probe.py"
    if Path(__file__) != source or hashlib.sha256(source.read_bytes()).hexdigest() != args.probe_sha256:
        raise FixtureError("helper_source_rejected")
    cli_info = cli.stat()
    if (
        not stat.S_ISREG(cli_info.st_mode)
        or cli_info.st_nlink != 1
        or stat.S_IMODE(cli_info.st_mode) != 0o755
    ):
        raise FixtureError("cli_source_rejected")
    with cli.open("rb") as stream:
        if stream.read(4) != b"\x7fELF":
            raise FixtureError("cli_source_rejected")
        stream.seek(0)
        if hashlib.file_digest(stream, "sha256").hexdigest() != args.cli_sha256:
            raise FixtureError("cli_source_rejected")
    for name, expected in ((".deploy-revision", args.candidate), (".cli-revision", args.cli_sha256)):
        path = candidate / name
        locked(path)
        if not path.is_file() or path.stat().st_size > 128 or path.read_text().strip() != expected:
            raise FixtureError("candidate_identity_rejected")
    proof, raw = read_record(work / "output/bootstrap-proof.json")
    allow, _ = read_record(work / "root-allow.json", root=True)
    expected_keys = {
        "schema",
        "candidate",
        "engine",
        "nonce",
        "pid",
        "production_pid",
        "issued_at",
        "checked_at",
        "probe_sha256",
        "checks",
    }
    if set(proof) != expected_keys or set(proof.get("checks", {})) != BOOT_CHECKS:
        raise FixtureError("same_pid_bootstrap_required")
    if (
        type(proof.get("schema")) is not int
        or proof["schema"] != 1
        or type(proof.get("pid")) is not int
        or proof["pid"] != os.getpid()
        or type(proof.get("production_pid")) is not int
        or proof["production_pid"] <= 1
        or proof["production_pid"] == os.getpid()
        or proof.get("candidate") != args.candidate
        or proof.get("nonce") != args.nonce
        or proof.get("probe_sha256") != args.probe_sha256
        or not isinstance(proof.get("engine"), str)
        or not DIGEST.fullmatch(proof["engine"])
        or any(type(proof.get(key)) is not int for key in ("issued_at", "checked_at"))
        or not proof["issued_at"] <= proof["checked_at"] <= now() <= proof["issued_at"] + 120
        or any(value is not True for value in proof["checks"].values())
    ):
        raise FixtureError("same_pid_bootstrap_required")
    expected = {key: proof[key] for key in ("schema", "candidate", "engine", "nonce", "pid", "probe_sha256")}
    expected["bootstrap_proof_sha256"] = hashlib.sha256(raw).hexdigest()
    if (
        set(allow) != {*expected, "allowed_at"}
        or any(
            type(allow.get(key)) is not type(value) or allow[key] != value for key, value in expected.items()
        )
        or type(allow.get("allowed_at")) is not int
        or not proof["checked_at"] <= allow["allowed_at"] <= now() <= proof["issued_at"] + 120
    ):
        raise FixtureError("same_pid_bootstrap_required")
    return proof["issued_at"]


def prepare_imports(candidate, cli, work):
    if any(re.search(r"(?i)(?:password|secret|token|api.?key|authorization)", name) for name in os.environ):
        raise FixtureError("real_credentials_rejected")
    os.environ.clear()
    os.environ.update(
        PATH="/usr/bin:/bin",
        HOME=str(work / "home"),
        TMPDIR=str(work / "tmp"),
        STOCK_DATA_DIR=str(work / "data"),
        STOCK_LOG_FILE=str(work / "output/application.log"),
        CLAUDE_BIN=str(cli),
        CLAUDE_SKILLS_HOME=str(work / "home/skills"),
        PRISM_ASTOCK_PROJECT="/nonexistent/prism-cli-fixture-astock",
        PRISM_VIBE_ENGINE_URL="http://127.0.0.1:9",
        PYTHONDONTWRITEBYTECODE="1",
        TZ="Asia/Shanghai",
    )
    os.chdir(work)
    tempfile.tempdir = str(work / "tmp")
    sites = list((candidate / ".venv/lib").glob("python*/site-packages"))
    if len(sites) != 1 or any(
        path.name.endswith(".pth") or path.name.startswith(("sitecustomize", "usercustomize", "patch_ssl"))
        for path in sites[0].rglob("*")
    ):
        raise FixtureError("startup_hook_rejected")
    import site

    site.main()
    allowed = (Path(sys.base_prefix) / "lib", candidate / ".venv")
    if Path(sys.prefix) != candidate / ".venv" or any(
        not path or not any(Path(path).is_relative_to(root) for root in allowed) for path in sys.path
    ):
        raise FixtureError("candidate_site_prefix_rejected")
    sys.path.insert(0, str(candidate))
    modules = [
        importlib.import_module(name)
        for name in ("server.config", "server.services.claude_runner", "server.services.auxiliary_model")
    ]
    for module in modules:
        if not Path(module.__file__).resolve().is_relative_to(candidate):
            raise FixtureError("candidate_module_source_rejected")
    return modules


class FixtureState:
    def __init__(self):
        self.lock = threading.Lock()
        self.phase = ""
        self.requests = {phase: {"messages": 0, "count_tokens": 0} for phase in PHASES}
        self.checks = dict.fromkeys(
            (
                "paths_allowlisted",
                "request_body_bounded",
                "synthetic_auth_only",
                "synthetic_model_only",
                "request_budget_respected",
                "auxiliary_http_tools_absent",
                "requests_well_formed",
            ),
            True,
        )

    def accept(self, path, headers, body):
        parsed = urlsplit(path)
        operation = {
            "/messages": "messages",
            "/v1/messages": "messages",
            "/messages/count_tokens": "count_tokens",
            "/v1/messages/count_tokens": "count_tokens",
        }.get(parsed.path)
        with self.lock:
            if not operation or parsed.query not in {"", "beta=true"} or self.phase not in PHASES:
                self.checks["paths_allowlisted"] = False
                raise FixtureError("fixture_path_rejected")
            authorization, api_key = headers.get("Authorization"), headers.get("x-api-key")
            auth = (
                (authorization == "Bearer " + TOKEN or api_key == TOKEN)
                and authorization in {None, "Bearer " + TOKEN}
                and api_key in {None, TOKEN}
            )
            self.checks["synthetic_auth_only"] &= auth
            model = isinstance(body, dict) and body.get("model") == MODEL
            self.checks["synthetic_model_only"] &= model
            if not auth or not model:
                raise FixtureError("fixture_request_rejected")
            self.requests[self.phase][operation] += 1
            total = sum(sum(counts.values()) for counts in self.requests.values())
            if total > 24 or self.requests[self.phase][operation] > 8:
                self.checks["request_budget_respected"] = False
                raise FixtureError("fixture_budget_exceeded")
            if self.phase == "auxiliary" and body.get("tools"):
                self.checks["auxiliary_http_tools_absent"] = False
                raise FixtureError("auxiliary_tools_rejected")
        return operation


def message(body):
    return {
        "id": "msg_prism_" + uuid.uuid4().hex,
        "type": "message",
        "role": "assistant",
        "model": body["model"],
        "content": [{"type": "text", "text": MARKER}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 16, "output_tokens": 8},
    }


def streamed_message(body):
    start = message(body)
    start.update(content=[], stop_reason=None, usage={"input_tokens": 16, "output_tokens": 0})
    events = [
        {"type": "message_start", "message": start},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": MARKER}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 8},
        },
        {"type": "message_stop"},
    ]
    return b"".join(
        b"event: " + item["type"].encode() + b"\ndata: " + encoded(item) + b"\n\n" for item in events
    )


@contextlib.contextmanager
def protocol_server(state):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass  # No request/header/body or credential logging.

        def setup(self):
            super().setup()
            self.connection.settimeout(3)

        def do_POST(self):
            try:
                raw_size = self.headers.get("Content-Length", "")
                if (
                    not raw_size.isdecimal()
                    or not 0 < int(raw_size) <= 512 * 1024
                    or self.headers.get("Transfer-Encoding")
                ):
                    state.checks["request_body_bounded"] = False
                    raise FixtureError("fixture_body_rejected")
                raw = self.rfile.read(int(raw_size))
                if len(raw) != int(raw_size):
                    state.checks["request_body_bounded"] = False
                    raise FixtureError("fixture_body_rejected")
                body = json.loads(raw)
                operation = state.accept(self.path, self.headers, body)
                if operation == "count_tokens":
                    payload, content_type = encoded({"input_tokens": 16}), "application/json"
                elif body.get("stream") is True:
                    payload, content_type = streamed_message(body), "text/event-stream"
                else:
                    payload, content_type = encoded(message(body)), "application/json"
                self.send_response(200)
            except Exception:
                state.checks["requests_well_formed"] = False
                payload, content_type = (
                    encoded(
                        {"error": {"type": "invalid_request_error", "message": "fixture request rejected"}}
                    ),
                    "application/json",
                )
                self.send_response(400)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True

        def do_GET(self):
            state.checks["paths_allowlisted"] = False
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.end_headers()
            self.close_connection = True

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_GET

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        if thread.is_alive():
            raise FixtureError("fixture_cleanup_failed")


class ChildBoundary:
    """Observe the real Popen call; never substitute a fake CLI or HTTP client."""

    def __init__(self, original, cli, work, endpoint, state):
        self.original, self.cli, self.work, self.endpoint, self.state = original, cli, work, endpoint, state
        self.children, self.phases = [], []
        self.checks = {"cli_argv_and_environment": True, "only_three_cli_children": True}

    def verify_child(self, child):
        process = Path("/proc") / str(child.pid)
        if (
            process.stat().st_uid != os.getuid()
            or Path(os.readlink(process / "exe")) != self.cli
            or os.getpgid(child.pid) != child.pid
        ):
            raise FixtureError("cli_child_identity_rejected")

    def spawn(self, command, *args, **kwargs):
        phase = self.state.phase
        env = kwargs.get("env", {})
        try:
            if not isinstance(command, (list, tuple)) or not command or command[0] != str(self.cli):
                raise FixtureError("fixed_cli_required")
            if phase not in PHASES or phase in self.phases or len(self.children) >= 3 or kwargs.get("shell"):
                self.checks["only_three_cli_children"] = False
                raise FixtureError("cli_child_budget_rejected")

            def value(flag):
                return command[command.index(flag) + 1]

            if (
                TOKEN in encoded(list(command)).decode()
                or value("--model") != MODEL
                or value("--setting-sources") != ""
                or "--strict-mcp-config" not in command
                or json.loads(value("--mcp-config")) != {"mcpServers": {}}
                or env.get("ANTHROPIC_AUTH_TOKEN") != TOKEN
                or env.get("ANTHROPIC_BASE_URL") != self.endpoint
                or env.get("DISABLE_UPDATES") != "1"
                or env.get("DISABLE_AUTOUPDATER") != "1"
                or "PRISM_CAPABILITY_TOKEN" in env
                or set(env)
                - {
                    "PATH",
                    "LANG",
                    "LC_ALL",
                    "LC_CTYPE",
                    "TZ",
                    "SystemRoot",
                    "HOME",
                    "TMPDIR",
                    "TMP",
                    "TEMP",
                    "NO_COLOR",
                    "DISABLE_UPDATES",
                    "DISABLE_AUTOUPDATER",
                    "ANTHROPIC_AUTH_TOKEN",
                    "ANTHROPIC_BASE_URL",
                }
                or not Path(env.get("HOME", "/")).resolve().is_relative_to(self.work / "home")
                and not Path(env.get("HOME", "/")).resolve().is_relative_to(self.work / "tmp")
            ):
                raise FixtureError("cli_boundary_rejected")
            if phase == "auxiliary":
                if (
                    value("--tools") != ""
                    or value("--disallowedTools") != "mcp__*"
                    or value("--max-turns") != "1"
                    or "--no-session-persistence" not in command
                    or "--max-tokens" in command
                ):
                    raise FixtureError("auxiliary_boundary_rejected")
            elif (
                value("--tools") != NATIVE_TOOLS
                or value("--allowedTools") != NATIVE_TOOLS
                or value("--output-format") != "stream-json"
                or ("--resume" in command) is not (phase == "stream_resume")
            ):
                raise FixtureError("stream_boundary_rejected")
            kwargs["start_new_session"] = True
            child = self.original(command, *args, **kwargs)
            self.children.append(child)
            self.verify_child(child)
            self.phases.append(phase)
            return child
        except Exception:
            self.checks["cli_argv_and_environment"] = False
            raise FixtureError("cli_boundary_rejected") from None

    def cleanup(self):
        for child in self.children:
            if child.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=2)
        return all(child.poll() is not None for child in self.children)


async def consume_stream(runner, model, *, session_id=None):
    session, delivered, success, count, total = None, False, False, 0, 0
    async with asyncio.timeout(12):
        async with contextlib.aclosing(
            runner.run_claude_stream(
                RESUME_PROMPT if session_id else PROMPT,
                user="anon",
                conv_id=None,
                model_cfg=model,
                session_id=session_id,
            )
        ) as events:
            async for event in events:
                count += 1
                total += len(encoded(event))
                if count > 500 or total > 1024 * 1024 or not isinstance(event, dict):
                    raise FixtureError("cli_events_unbounded")
                if event.get("type") in {"error", "cancelled"}:
                    raise FixtureError("cli_stream_failed")
                if event.get("type") == "system" and event.get("subtype") == "init":
                    observed = event.get("session_id")
                    if isinstance(observed, str) and SESSION.fullmatch(observed):
                        session = observed
                if event.get("type") == "assistant":
                    delivered |= MARKER in encoded(event).decode()
                if event.get("type") == "result":
                    success = event.get("is_error") is False and event.get("subtype") == "success"
                    delivered |= MARKER in str(event.get("result", ""))
    if not session or not delivered or not success or session_id and session != session_id:
        raise FixtureError("cli_stream_contract_rejected")
    return session


def clear_synthetic_home(work):
    for path in (work / "home").iterdir():
        if path.is_symlink() or not path.is_dir():
            path.unlink()
        else:
            shutil.rmtree(path)


@contextlib.contextmanager
def private_log(work):
    path = work / "output/cli-protocol.log"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as log:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            yield
        log.flush()  # fd1/2 stay private, including native atexit output.


async def run_protocol_fixture(candidate, cli_sha256, work, *, nonce, probe_sha256, importer=prepare_imports):
    """Require the controller's same-PID gate even for direct function callers."""
    cli = Path("/opt/prism/tools/claude") / cli_sha256 / "claude"
    result = {
        "schema": 1,
        "probe": "cli_protocol_fixture",
        "candidate": candidate.name,
        "cli_sha256": cli_sha256,
        "nonce": nonce if NONCE.fullmatch(nonce) else "invalid",
        "probe_sha256": probe_sha256 if DIGEST.fullmatch(probe_sha256) else "invalid",
        "pid": os.getpid(),
        "checked_at": int(time.time()),
        "passed": False,
        "cli_protocol_fixture_verified": False,
        "checks": {},
        "requests": {},
        "error_class": None,
        "fixture_model_configuration_override": True,
        "live_model_verified": False,
        "native_search_verified": False,
        "native_fetch_verified": False,
    }
    state, boundary = FixtureState(), None
    validated = False
    config = None
    previous = None
    try:
        checked_candidate, checked_cli, checked_work = fixed_paths(candidate.name, cli_sha256, str(work))
        if (checked_candidate, checked_cli, checked_work) != (candidate, cli, work):
            raise FixtureError("invalid_identity")
        args = argparse.Namespace(
            candidate=candidate.name, cli_sha256=cli_sha256, nonce=nonce, probe_sha256=probe_sha256
        )
        issued_at = require_bootstrap(args, candidate, cli, work)
        validated = True
        with private_log(work), protocol_server(state) as endpoint:
            config, runner, helper = importer(candidate, cli, work)
            model = {"model": MODEL, "base_url": endpoint, "api_key": TOKEN}
            previous = (config.MODELS, config.DEFAULT_MODEL, config.CLAUDE_BIN)
            config.MODELS, config.DEFAULT_MODEL, config.CLAUDE_BIN = (
                {"protocol-fixture": model},
                "protocol-fixture",
                str(cli),
            )
            boundary = ChildBoundary(subprocess.Popen, cli, work, endpoint, state)
            original = subprocess.Popen
            subprocess.Popen = boundary.spawn
            try:
                remaining = min(40, issued_at + 55 - time.time())
                if remaining <= 0:
                    raise FixtureError("fixture_deadline_expired")
                async with asyncio.timeout(remaining):
                    state.phase = "stream_new"
                    session = await consume_stream(runner, model)
                    result["checks"]["real_runner_new_session"] = True
                    state.phase = "stream_resume"
                    await consume_stream(runner, model, session_id=session)
                    result["checks"]["real_runner_resume"] = True
                    state.phase = "auxiliary"
                    text = helper.run_text(
                        PROMPT, timeout=10, output_format="text", model_id="protocol-fixture"
                    )
                    result["checks"]["real_auxiliary_no_tools"] = text == MARKER
            finally:
                subprocess.Popen = original
                result["checks"]["synthetic_children_reaped"] = boundary.cleanup()
            result["checks"].update(boundary.checks)
            result["checks"].update(state.checks)
            result["checks"]["three_real_cli_invocations"] = boundary.phases == list(PHASES)
            result["checks"]["all_phases_reached_fixture"] = all(
                state.requests[phase]["messages"] > 0 for phase in PHASES
            )
            if not all(result["checks"].values()):
                raise FixtureError("fixture_checks_failed")
            result["passed"] = result["cli_protocol_fixture_verified"] = True
    except Exception:
        result["error_class"] = "cli_protocol_fixture_failed" if validated else "same_pid_bootstrap_required"
    finally:
        result["checks"].update(state.checks)
        if boundary is not None:
            result["checks"].update(boundary.checks)
        if config is not None and previous is not None:
            config.MODELS, config.DEFAULT_MODEL, config.CLAUDE_BIN = previous
        if validated:
            try:
                clear_synthetic_home(work)
            except OSError:
                result["passed"] = result["cli_protocol_fixture_verified"] = False
                result["error_class"] = "synthetic_cleanup_failed"
        result["requests"] = state.requests
        result["checked_at"] = int(time.time())
    if validated:
        try:
            write_result(work, result)
        except OSError:
            result["passed"] = result["cli_protocol_fixture_verified"] = False
            result["error_class"] = "private_result_write_failed"
    return result


def write_result(work, result):
    descriptor = os.open(
        work / "output/cli-result.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded(result))
        stream.flush()
        os.fsync(stream.fileno())


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise FixtureError("invalid_arguments")


def main(argv=None):
    summary = os.dup(1)
    parser = Parser(description=__doc__)
    for name in ("candidate", "cli-sha256", "workdir", "nonce", "probe-sha256"):
        parser.add_argument("--" + name, required=True)
    work = None
    result = {
        "schema": 1,
        "probe": "cli_protocol_fixture",
        "passed": False,
        "error_class": "same_pid_bootstrap_required",
    }
    try:
        args = parser.parse_args(argv)
        candidate, cli, candidate_work = fixed_paths(args.candidate, args.cli_sha256, args.workdir)
        work = candidate_work
        result = asyncio.run(
            run_protocol_fixture(
                candidate, args.cli_sha256, work, nonce=args.nonce, probe_sha256=args.probe_sha256
            )
        )
    except Exception:
        pass  # Fixed safe failure, never echo an argument/request/provider body.
    try:
        os.write(summary, encoded(result) + b"\n")
    finally:
        os.close(summary)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
