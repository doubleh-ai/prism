#!/usr/bin/env python3
"""Same-PID live integration worker. Never run without the reviewed controller.

The CLI is real; only its public synthetic prompts, broker origin/token, turn
limits and read-only MCP surface are probe-specific. Candidate source and DB
remain separate: only empty private data/HOME are initialized. Root must verify
the stdlib bootstrap proof and current kernel identities before publishing the
immutable permit. The broker independently consumes that permit before taking
a real key from its supervisor. No real key enters this worker.

Native search frames, source metadata, application ledger ingestion, server
clock and owned MCP roundtrips are separate evidence. Native WebFetch remains
unverified because there is deliberately no CONNECT or public worker egress.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import errno
import grp
import hashlib
import hmac
import importlib
import importlib.util
import json
import os
import pwd
import re
import resource
import signal
import socket
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

POLICY_SHA256 = "c6fa205466800636fe6fbe557eb8f625785567a129b405ebb4935b7b65e325d9"
TOOLS = {"indicator_list", "research_read"}
STARTUP_NAMES = ("sitecustomize", "usercustomize", "patch_ssl")
BOOT_CHECKS = {
    "production_home_denied",
    "production_proc_denied",
    "production_8088_denied",
    "vibe_8899_denied",
    "metadata_denied",
    "private_network_denied",
    "direct_public_egress_denied",
    "candidate_write_denied",
    "config_write_denied",
    "staging_directory_denied",
    "synthetic_empty_data",
}
PROMPTS = (
    "这是公开合成联调，不是投资研究。请先实际调用 Prism indicator_list 和 research_read，各一次、参数均为空对象；"
    "只报告本轮服务器提供的上海日期 YYYY-MM-DD、时区和两次真实调用是否完成。不要研究股票、不要联网搜索、不要启动任务。",
    "这是公开合成联调。只做一次原生 WebSearch，查询 site:sse.com.cn 上交所公开上市公司公告线索。"
    "返回一个本轮工具实际返回的上交所官网 URL，并明确这里只获得搜索元数据、原文尚未核验。"
    "不要 WebFetch、不要行情研究、不要启动任何任务。",
)


class WorkerError(RuntimeError):
    """Fixed categories only; raw third-party errors stay in private logs."""


def encode(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()


def root_source(path, expected):
    if not isinstance(expected, str) or not re.fullmatch("[0-9a-f]{64}", expected):
        raise WorkerError("approved_source_required")
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise WorkerError("approved_source_required")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > 131072
            or info.st_uid != 0
            or info.st_mode & 0o022
        ):
            raise WorkerError("approved_source_required")
        raw = stream.read(131073)
    if hashlib.sha256(raw).hexdigest() != expected:
        raise WorkerError("approved_source_required")
    return raw


def load_transport(work, expected):
    source = work / "transport.py"
    raw = root_source(source, expected)
    spec = importlib.util.spec_from_loader("_prism_approved_transport", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(source)
    exec(compile(raw, str(source), "exec"), module.__dict__)
    return module


def denied_open(path, *, create=False, write=False):
    flags = os.O_CLOEXEC | os.O_NOFOLLOW | (os.O_WRONLY if write or create else os.O_RDONLY)
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as error:
        allowed = (
            {errno.EACCES, errno.EPERM, errno.EROFS}
            if create or write
            else {errno.EACCES, errno.EPERM, errno.ENOENT}
        )
        return error.errno in allowed
    os.close(fd)  # Never read or write existing data, including the env canary.
    if create:
        os.unlink(path)  # Only the nonce-bound exclusive candidate sentinel.
    return False


def denied_connect(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as channel:
        channel.settimeout(0.5)
        code = channel.connect_ex((host, port))
    return code in {errno.EACCES, errno.EPERM, errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH}


def bootstrap(args):
    """No site/candidate imports before the actual root handshake in this PID."""
    candidate = Path("/opt/prism/releases") / args.candidate
    cli = Path("/opt/prism/tools/claude") / args.cli_sha256 / "claude"
    work = Path(args.workdir)
    if (
        not re.fullmatch("[0-9a-f]{40}", args.candidate)
        or any(
            not re.fullmatch("[0-9a-f]{64}", value)
            for value in (args.cli_sha256, args.probe_sha256, args.transport_sha256)
        )
        or not re.fullmatch("[0-9a-f]{32}", args.nonce)
        or type(args.production_pid) is not int
        or args.production_pid <= 1
        or type(args.broker_pid) is not int
        or args.broker_pid <= 1
        or args.broker_pid in {os.getpid(), args.production_pid}
        or not 0 <= time.time() - args.issued_at <= 120
        or sys.platform != "linux"
        or not sys.flags.isolated
        or not sys.flags.no_site
        or not sys.flags.dont_write_bytecode
        or Path(sys.executable) != candidate / ".venv/bin/python"
    ):
        raise WorkerError("bootstrap_identity_rejected")
    account = pwd.getpwnam("prism-integration")
    broker = pwd.getpwnam("prism-integration-broker")
    group = grp.getgrnam("prism-integration-bridge")
    if (
        os.getuid() != account.pw_uid
        or os.getuid() == 0
        or broker.pw_uid in {0, account.pw_uid}
        or account.pw_shell != "/usr/sbin/nologin"
        or broker.pw_shell != "/usr/sbin/nologin"
        or type(args.bridge_gid) is not int
        or args.bridge_gid <= 0
        or args.bridge_gid != group.gr_gid
    ):
        raise WorkerError("dedicated_uid_required")
    if any(group not in {account.pw_gid, args.bridge_gid} for group in os.getgroups()):
        raise WorkerError("dedicated_uid_required")
    if Path(__file__) != work / "worker.py":
        raise WorkerError("approved_source_required")
    root_source(work / "worker.py", args.probe_sha256)
    transport = load_transport(work, args.transport_sha256)
    transport.fixed_work(work)
    for name in ("output", "home", "tmp", "data"):
        path = work / name
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != account.pw_uid
            or stat.S_IMODE(info.st_mode) != 0o700
            or any(path.iterdir())
        ):
            raise WorkerError("synthetic_workdir_required")
    if any(re.search(r"(?i)(?:password|secret|token|api.?key|authorization)", key) for key in os.environ):
        raise WorkerError("credential_environment_rejected")
    # Root-protected immutable identities, not a PATH-selected executable.
    for name, expected in ((".deploy-revision", args.candidate), (".cli-revision", args.cli_sha256)):
        revision = candidate / name
        transport.locked(revision)
        if (
            not revision.is_file()
            or revision.stat().st_nlink != 1
            or revision.stat().st_size > 128
            or revision.read_text().strip() != expected
        ):
            raise WorkerError("candidate_revision_rejected")
    verify_cli(cli, args.cli_sha256)
    with cli.open("rb") as stream:
        if stream.read(4) != b"\x7fELF":
            raise WorkerError("cli_source_rejected")
    binding = {
        "candidate": args.candidate,
        "cli_sha256": args.cli_sha256,
        "probe_sha256": args.probe_sha256,
        "nonce": args.nonce,
        "worker_pid": os.getpid(),
        "broker_pid": args.broker_pid,
    }
    proof = {
        "schema": 1,
        "binding": binding,
        "production_pid": args.production_pid,
        "issued_at": args.issued_at,
        "checked_at": int(time.time()),
        "sources": {"worker": args.probe_sha256, "transport": args.transport_sha256, "policy": POLICY_SHA256},
        "checks": {
            "production_home_denied": denied_open("/home/ubuntu/stock-analyzer/.env"),
            "production_proc_denied": denied_open(f"/proc/{args.production_pid}/environ"),
            "production_8088_denied": denied_connect("127.0.0.1", 8088),
            "vibe_8899_denied": denied_connect("127.0.0.1", 8899),
            "metadata_denied": denied_connect("169.254.169.254", 80),
            "private_network_denied": denied_connect("10.0.0.1", 443),
            "direct_public_egress_denied": denied_connect("8.8.8.8", 443),
            "candidate_write_denied": denied_open(candidate / (".live-canary-" + args.nonce), create=True),
            "config_write_denied": denied_open("/home/ubuntu/stock-analyzer/.env", write=True),
            "staging_directory_denied": denied_open("/var/lib/prism-operations/staging"),
            "synthetic_empty_data": not any((work / "data").iterdir()),
        },
    }
    transport.write_private(work / "output/bootstrap-proof.json", proof)
    if set(proof["checks"]) != BOOT_CHECKS or not all(proof["checks"].values()):
        raise WorkerError("bootstrap_isolation_rejected")
    deadline = time.monotonic() + 20
    while not (work / "root-allow.json").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    policy = transport.load_policy(work / "policy.py")
    kernel = transport.verify_allow(work, binding, policy, proof_raw=encode(proof))
    transport.self_identity(kernel, binding, "worker")
    return candidate, cli, work, binding, broker.pw_uid, transport, policy


def verify_cli(path, digest):
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise WorkerError("cli_source_rejected")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o755:
        raise WorkerError("cli_source_rejected")
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise WorkerError("cli_source_rejected")


def activate_candidate_site(candidate):
    """Activate the venv without executing system/user hooks or any .pth."""
    sites = list((candidate / ".venv/lib").glob("python*/site-packages"))
    if len(sites) != 1 or any(
        path.name.endswith(".pth") or path.name.startswith(STARTUP_NAMES) for path in sites[0].rglob("*")
    ):
        raise WorkerError("startup_hook_rejected")
    if any(name.startswith(STARTUP_NAMES) for name in sys.modules):
        raise WorkerError("startup_hook_imported")
    import site

    original_site, original_user, original_package = (
        site.execsitecustomize,
        site.execusercustomize,
        site.addpackage,
    )

    def reject_package(*args, **kwargs):
        raise WorkerError("startup_hook_rejected")

    try:
        site.execsitecustomize = lambda: None
        site.execusercustomize = lambda: None
        site.addpackage = reject_package
        site.main()
    finally:
        site.execsitecustomize = original_site
        site.execusercustomize = original_user
        site.addpackage = original_package
    if any(name.startswith(STARTUP_NAMES) for name in sys.modules):
        raise WorkerError("startup_hook_imported")


def prepare_candidate(candidate, cli, work):
    os.environ.clear()
    os.environ.update(
        PATH="/usr/bin:/bin",
        HOME=str(work / "home"),
        TMPDIR=str(work / "tmp"),
        STOCK_DATA_DIR=str(work / "data"),
        STOCK_LOG_FILE=str(work / "output/application.log"),
        CLAUDE_BIN=str(cli),
        CLAUDE_SKILLS_HOME=str(work / "home/skills"),
        PRISM_ASTOCK_PROJECT="/nonexistent/prism-live-probe-astock",
        PRISM_VIBE_ENGINE_URL="http://127.0.0.1:9",
        PYTHONDONTWRITEBYTECODE="1",
        PRISM_LOG_LEVEL="WARNING",
        TZ="Asia/Shanghai",
    )
    os.chdir(work)
    tempfile.tempdir = str(work / "tmp")
    resource.setrlimit(resource.RLIMIT_FSIZE, (4 * 1024 * 1024, 4 * 1024 * 1024))
    activate_candidate_site(candidate)
    allowed = (Path(sys.base_prefix) / "lib", candidate / ".venv")
    if Path(sys.prefix) != candidate / ".venv" or any(
        not path or not any(Path(path).is_relative_to(root) for root in allowed) for path in sys.path
    ):
        raise WorkerError("candidate_site_prefix_rejected")
    sys.path.insert(0, str(candidate))
    names = (
        "server.main",
        "server.chat",
        "server.config",
        "server.services.claude_runner",
        "server.research_context",
        "server.research",
        "server.database",
        "server.capabilities",
        "server.auth",
        "httpx",
    )
    modules = {name: importlib.import_module(name) for name in names}
    for name, module in tuple(sys.modules.items()):
        if (name == "server" or name.startswith("server.")) and (
            not getattr(module, "__file__", None)
            or not Path(module.__file__).resolve().is_relative_to(candidate)
        ):
            raise WorkerError("candidate_module_source_rejected")
    if Path(modules["server.database"].DB_PATH) != work / "data/chat.db":
        raise WorkerError("synthetic_database_required")
    return modules


def scrub(value, secrets):
    if isinstance(value, dict):
        return {key: scrub(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
    return value


class CapabilityGateway:
    """CLI HTTP -> actual candidate ASGI routes/auth/dispatch, only two read tools."""

    def __init__(self, modules, transport, policy, owner):
        self.modules, self.transport, self.policy, self.owner = modules, transport, policy, owner
        self.run_id = None
        self.conv_id = None
        self.token_hash = None
        self.calls = []
        self.total = 0
        self.stop = threading.Event()
        self.listener = self.thread = None

    def grant(self, token, owner, conv_id, run_id):
        if owner != self.owner or run_id != self.run_id or not conv_id or not isinstance(token, str):
            raise WorkerError("owned_scope_required")
        self.conv_id = conv_id
        self.token_hash = hashlib.sha256(token.encode()).hexdigest()

    async def request(self, method, path, headers, raw):
        httpx = self.modules["httpx"]
        asgi = httpx.ASGITransport(app=self.modules["server.main"].app, client=("127.0.0.1", 0))
        async with httpx.AsyncClient(
            transport=asgi, base_url="http://127.0.0.1", follow_redirects=False
        ) as client:
            return await client.request(method, path, headers=headers, content=raw)

    def handle(self, channel, port):
        try:
            method, path, headers, raw = self.transport.parse_http(
                self.transport.SocketReader(channel, time.monotonic() + 5),
                methods=("GET", "POST"),
                maximum=32768,
            )
            if (
                headers.get("host") != f"127.0.0.1:{port}"
                or set(headers)
                - {
                    "host",
                    "authorization",
                    "content-type",
                    "content-length",
                    "connection",
                    "accept-encoding",
                    "user-agent",
                    "x-requested-with",
                }
                or not headers.get("authorization", "").startswith("Bearer ")
                or self.total >= 8
            ):
                raise WorkerError("mcp_gateway_rejected")
            token_hash = hashlib.sha256(headers["authorization"][7:].encode()).hexdigest()
            if self.token_hash is None or not hmac.compare_digest(self.token_hash, token_hash):
                raise WorkerError("owned_scope_required")
            self.total += 1
            call = None
            if method == "GET" and path == "/api/internal/capabilities/tools":
                pass
            elif method == "POST" and path == "/api/internal/capabilities/call":
                call = self.transport.strict_json(raw)
                if (
                    not isinstance(call, dict)
                    or set(call) != {"name", "arguments", "request_id"}
                    or call["name"] not in TOOLS
                    or not isinstance(call["request_id"], str)
                    or not re.fullmatch("[0-9a-f]{64}", call["request_id"])
                ):
                    raise WorkerError("mcp_gateway_rejected")
                self.policy.owned_mcp_call(
                    call["name"],
                    call["arguments"],
                    {
                        "owner": self.owner,
                        "authenticated_owner": self.owner,
                        "synthetic_data_only": True,
                        "run_id": self.run_id,
                    },
                )
            else:
                raise WorkerError("mcp_gateway_rejected")
            response = asyncio.run(
                self.request(
                    method,
                    path,
                    {
                        "Authorization": headers["authorization"],
                        "X-Requested-With": "XMLHttpRequest",
                        "Content-Type": "application/json",
                    },
                    raw,
                )
            )
            if len(response.content) > 131072 or response.status_code != 200:
                raise WorkerError("mcp_gateway_rejected")
            result = response.json()
            if call is None:
                result = {"tools": [tool for tool in result.get("tools", []) if tool.get("name") in TOOLS]}
                if {tool["name"] for tool in result["tools"]} != TOOLS:
                    raise WorkerError("mcp_gateway_rejected")
            elif result.get("ok") is True:
                if (
                    result.get("call", {}).get("run_id") != self.run_id
                    or result.get("call", {}).get("conv_id") != self.conv_id
                    or result.get("call", {}).get("capability_id") != call["name"]
                ):
                    raise WorkerError("owned_scope_required")
                if (
                    call["name"] == "research_read"
                    and result.get("data", {}).get("run", {}).get("run_id") != self.run_id
                ):
                    raise WorkerError("owned_run_readback_rejected")
                self.calls.append(
                    {
                        "name": call["name"],
                        "run_id": self.run_id,
                        "call_id": result.get("call", {}).get("call_id"),
                    }
                )
            body = encode(result)
            channel.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
        except Exception:
            self.transport.error_response(channel)

    def __enter__(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.listener.settimeout(0.2)
        port = self.listener.getsockname()[1]

        def run():
            while not self.stop.is_set():
                try:
                    channel, address = self.listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    return
                with channel:
                    if address[0] == "127.0.0.1":
                        self.handle(channel, port)

        self.thread = threading.Thread(target=run, name="prism-owned-mcp", daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{port}"

    def __exit__(self, *_):
        self.stop.set()
        self.listener.close()
        self.thread.join(timeout=7)
        if self.thread.is_alive():
            raise WorkerError("gateway_cleanup_failed")


class FrameObserver:
    def __init__(self):
        self.calls, self.completed, self.urls = {}, set(), set()
        self.native_frames = False
        self.server_frames = False
        self.model_text = ""
        self.success = False

    def observe(self, event):
        if event.get("type") == "error":
            raise WorkerError("model_run_failed")
        if event.get("type") == "result":
            self.success = event.get("is_error") is not True and event.get("subtype") == "success"
        message = event.get("message", {})
        content = message.get("content", []) if isinstance(message, dict) else []
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text" and isinstance(block.get("text"), str):
                self.model_text += block["text"][:8192]
            if (
                kind in {"tool_use", "server_tool_use"}
                and block.get("name") in {"WebSearch", "web_search"}
                and isinstance(block.get("id"), str)
            ):
                self.calls.setdefault(block["id"], kind)
            if kind not in {"tool_result", "web_search_tool_result"}:
                continue
            call_id = block.get("tool_use_id")
            if call_id not in self.calls or call_id in self.completed:
                continue
            self.completed.add(call_id)
            if block.get("is_error"):
                continue
            if kind == "web_search_tool_result":
                rows = block.get("content")
                if (
                    self.calls[call_id] != "server_tool_use"
                    or not isinstance(rows, list)
                    or not rows
                    or any(
                        not isinstance(row, dict) or row.get("type") != "web_search_result" for row in rows
                    )
                ):
                    continue
                urls = [row.get("url") for row in rows]
                self.server_frames = True
            else:
                # Real candidate parsing, called separately by run_worker, establishes ordinary refs.
                self.native_frames = self.calls[call_id] == "tool_use"
                continue
            for url in urls:
                try:
                    parsed = urlsplit(url)
                    if (
                        parsed.scheme == "https"
                        and parsed.hostname in {"www.sse.com.cn", "static.sse.com.cn"}
                        and parsed.port in {None, 443}
                        and not parsed.username
                        and not parsed.password
                        and not parsed.query
                        and not parsed.fragment
                    ):
                        self.urls.add(url)
                        self.native_frames = True
                except (TypeError, ValueError):
                    pass


@contextlib.contextmanager
def private_log(work):
    fd = os.open(work / "output/live.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as log:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            yield
        log.flush()  # Late/native output remains private for this one-shot process.


async def exercise(modules, work, binding, endpoint, gateway, policy):
    runner = modules["server.services.claude_runner"]
    chat = modules["server.chat"]
    ledger = modules["server.research"]
    database = modules["server.database"]
    capabilities = modules["server.capabilities"]
    auth = modules["server.auth"]
    owner, foreign = "probe-" + binding["nonce"][:12], "foreign-" + binding["nonce"][:12]
    conv_ids = ["probe-" + binding["nonce"] + "-" + str(index) for index in range(2)]
    foreign_conv = "foreign-" + binding["nonce"]
    with database.get_db() as db:
        for user in (owner, foreign):
            db.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", [user, "synthetic-no-login"])
        for cid, user in [*((cid, owner) for cid in conv_ids), (foreign_conv, foreign)]:
            db.execute("INSERT INTO conversations(id,username) VALUES(?,?)", [cid, user])
    gateway.owner = owner
    token = "PRISM_LIVE_SYNTHETIC_" + binding["nonce"]
    model = {"model": policy.MODEL, "base_url": endpoint, "api_key": token}
    secrets = [token]
    spawned, observations, research_ids = [], [], []
    spawn = asyncio.create_subprocess_exec
    issue = runner.issue_run_token
    actual_runner = runner.run_claude_stream
    old_chat_runner = chat.run_claude_stream
    old_models = [
        (modules[name], modules[name].MODELS)
        for name in ("server.config", "server.main", "server.chat", "server.services.claude_runner")
    ]
    previous_url = runner.CAPABILITY_INTERNAL_URL
    phase = [0]
    cli = Path("/opt/prism/tools/claude") / binding["cli_sha256"] / "claude"

    def issue_scoped(*args, **kwargs):
        scoped = issue(*args, **kwargs)
        secrets.append(scoped)
        gateway.grant(scoped, *args[:3])
        return scoped

    async def bounded_spawn(*argv, **kwargs):
        if (
            len(spawned) >= 2
            or not argv
            or Path(argv[0]) != cli
            or "--strict-mcp-config" not in argv
            or argv[argv.index("--setting-sources") + 1] != ""
            or "--resume" in argv
        ):
            raise WorkerError("cli_launch_rejected")
        command = list(argv)
        command[command.index("--tools") + 1] = "WebSearch" if phase[0] else ""
        command[command.index("--allowedTools") + 1] = (
            "WebSearch" if phase[0] else "mcp__prism__indicator_list,mcp__prism__research_read"
        )
        command += ["--max-turns", str((4, 2)[phase[0]]), "--disallowedTools", "WebFetch,AskUserQuestion"]
        env = kwargs.get("env", {})
        if (
            env.get("ANTHROPIC_AUTH_TOKEN") != token
            or env.get("ANTHROPIC_BASE_URL") != endpoint
            or env.get("PRISM_CAPABILITY_URL") != gateway_url[0]
            or env.get("PRISM_CAPABILITY_TOKEN") not in secrets
            or any(key.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"} for key in env)
        ):
            raise WorkerError("synthetic_cli_environment_required")
        process = await spawn(*command, **{**kwargs, "start_new_session": True})
        spawned.append(process)
        return process

    gateway_url = [runner.CAPABILITY_INTERNAL_URL]
    runner.issue_run_token = issue_scoped
    asyncio.create_subprocess_exec = bounded_spawn
    for module, _ in old_models:
        module.MODELS = {"bounded-live-probe": model}
    owner_token = auth.create_session(owner)
    secrets.append(owner_token)
    raw_path = work / "output/model-events.jsonl"
    descriptor = os.open(raw_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    raw_bytes = 0
    try:
        with os.fdopen(descriptor, "wb") as raw_file:
            for index, prompt in enumerate(PROMPTS):
                phase[0] = index
                observer = FrameObserver()
                current = []
                started = int(time.time())

                async def observed_runner(
                    *args, _current=current, _expected=conv_ids[index], _observer=observer, **kwargs
                ):
                    nonlocal raw_bytes
                    if _current or kwargs.get("user") != owner or kwargs.get("conv_id") != _expected:
                        raise WorkerError("chat_runner_scope_rejected")
                    context = kwargs["research_context"]
                    _current.append(context)
                    research_ids.append(context["run_id"])
                    gateway.run_id = context["run_id"]
                    async with contextlib.aclosing(actual_runner(*args, **kwargs)) as stream:
                        async for event in stream:
                            row = encode(scrub(event, secrets)) + b"\n"
                            raw_bytes += len(row)
                            if raw_bytes > 1024 * 1024:
                                raise WorkerError("private_event_limit")
                            raw_file.write(row)
                            _observer.observe(event)
                            yield event  # Original candidate chat performs ingestion/SSE; no probe adapter.

                chat.run_claude_stream = observed_runner
                response = await gateway.request(
                    "POST",
                    "/api/chat/stream",
                    {
                        "Cookie": "token=" + owner_token,
                        "X-Requested-With": "XMLHttpRequest",
                        "Content-Type": "application/json",
                    },
                    encode(
                        {
                            "question": prompt,
                            "skill": "prism-research",
                            "conv_id": conv_ids[index],
                            "model": "bounded-live-probe",
                        }
                    ),
                )
                if response.status_code != 200 or len(current) != 1 or len(response.content) > 1024 * 1024:
                    raise WorkerError("actual_chat_http_failed")
                context = current[0]
                safe_sse = scrub(response.text, secrets).encode()
                fd = os.open(
                    work / f"output/chat-sse-{index}.txt",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
                with os.fdopen(fd, "wb") as destination:
                    destination.write(safe_sse)
                refs = []
                for line in response.text.splitlines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        event = json.loads(line[6:])
                        if isinstance(event, dict) and isinstance(event.get("ref"), dict):
                            refs.append(event["ref"])
                observer.urls.update(
                    ref["url"]
                    for ref in refs
                    if ref.get("run_id") == context["run_id"]
                    and ref.get("tool") == "WebSearch"
                    and ref.get("tool_call_id") in observer.calls
                    and urlsplit(ref["url"]).hostname in {"www.sse.com.cn", "static.sse.com.cn"}
                )
                finished = int(time.time())
                saved = ledger.read_research_run(owner, context["run_id"])
                document_ids = {document["document_id"] for document in saved["documents"]}
                reference_ids = {ref.get("document_id") for ref in refs if ref.get("document_id")}
                observations.append(
                    {
                        "model_completed": observer.success,
                        "chat_http_verified": True,
                        "chat_sse_sha256": hashlib.sha256(safe_sse).hexdigest(),
                        "chat_sse_ledger_match": bool(reference_ids) and reference_ids <= document_ids,
                        "server_clock_verified": policy.shanghai_clock(context, started, finished),
                        "model_date_echo_verified": context["date"] in observer.model_text
                        if index == 0
                        else False,
                        "native_search_frames_observed": observer.native_frames,
                        "search_call_count": len(observer.calls),
                        "server_search_frames_observed": observer.server_frames,
                        "official_source_urls": sorted(observer.urls),
                        "source_ledger_persisted": bool(saved["documents"]),
                        "server_search_unhandled_by_candidate": observer.server_frames
                        and not saved["documents"],
                        "run_id": context["run_id"],
                    }
                )
        # Real HTTP/ASGI owner check, separate from model/MCP result assertions.
        foreign_token = auth.create_session(foreign)
        secrets.append(foreign_token)
        denied = await gateway.request(
            "GET", "/api/research/runs/" + research_ids[0], {"Cookie": "token=" + foreign_token}, b""
        )
        calls = capabilities.list_calls(owner, run_id=research_ids[0])
        completed = {call["capability_id"] for call in calls if call["status"] == "completed"}
        gateway_completed = {
            call["name"] for call in gateway.calls if call["run_id"] == research_ids[0] and call["call_id"]
        }
        return {
            "phases": observations,
            "owned_mcp_roundtrip_verified": completed >= TOOLS and gateway_completed >= TOOLS,
            "foreign_owner_http_404_verified": denied.status_code == 404,
            "real_runner_invocations": len(spawned),
            "private_event_bytes": raw_bytes,
        }
    finally:
        asyncio.create_subprocess_exec = spawn
        runner.issue_run_token = issue
        chat.run_claude_stream = old_chat_runner
        for module, models in old_models:
            module.MODELS = models
        runner.CAPABILITY_INTERNAL_URL = previous_url
        for process in spawned:
            if process.pid == os.getpgrp():
                continue
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), timeout=2)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        for run_id in research_ids:
            capabilities.revoke_run_token(run_id)
        with database.get_db() as db:
            db.execute("DELETE FROM sessions WHERE username IN (?,?)", [owner, foreign])


async def run_worker(args):
    candidate, cli, work, binding, broker_uid, transport, policy = bootstrap(args)
    result = {
        "schema": 1,
        "probe": "bounded_live_integration",
        "binding": binding,
        "checked_at": int(time.time()),
        "passed": False,
        "live_model_verified": False,
        "native_search_verified": False,
        "native_fetch_verified": False,
        "market_provider_verified": False,
        "production_verified": False,
        "research_quality_verified": False,
        "checks": {},
        "error_class": None,
        "stage": "candidate_import",
        "blocked_provider_header_names": [],
        "probe_mutations": [
            "broker origin and synthetic token",
            "bounded turns",
            "two read-only MCP tools",
            "synthetic data and HOME",
        ],
    }
    bridge = None
    try:
        with private_log(work):
            modules = prepare_candidate(candidate, cli, work)
            gateway = CapabilityGateway(modules, transport, policy, "pending-synthetic-owner")
            bridge = transport.LoopbackBridge(work, binding, broker_uid, args.bridge_gid)
            result["stage"] = "bridge_start"
            with (
                bridge as endpoint,
                gateway as gateway_url,
            ):
                modules["server.services.claude_runner"].CAPABILITY_INTERNAL_URL = gateway_url
                result["stage"] = "public_qa"
                async with asyncio.timeout(min(160, args.issued_at + 175 - time.time())):
                    result["checks"] = await exercise(modules, work, binding, endpoint, gateway, policy)
            result["stage"] = "result_validation"
            phases = result["checks"]["phases"]
            result["live_model_verified"] = len(phases) == 2 and all(
                phase["model_completed"] for phase in phases
            )
            result["native_search_verified"] = bool(
                phases[1]["native_search_frames_observed"] and phases[1]["official_source_urls"]
            )
            result["passed"] = bool(
                result["live_model_verified"]
                and result["native_search_verified"]
                and phases[1]["search_call_count"] == 1
                and phases[1]["source_ledger_persisted"]
                and phases[1]["chat_sse_ledger_match"]
                and all(phase["server_clock_verified"] for phase in phases)
                and phases[0]["model_date_echo_verified"]
                and result["checks"]["owned_mcp_roundtrip_verified"]
                and result["checks"]["foreign_owner_http_404_verified"]
            )
            if not result["passed"]:
                result["error_class"] = "live_acceptance_incomplete"
                result["stage"] = "acceptance_incomplete"
            else:
                result["stage"] = "complete"
    except Exception:
        result["error_class"] = "bounded_live_probe_failed"
    if bridge is not None:
        result["blocked_provider_header_names"] = sorted(bridge.unknown_header_names)
    result["checked_at"] = int(time.time())
    transport.write_private(work / "output/live-result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description="Controller-only bounded live integration worker")
    for name in ("candidate", "cli-sha256", "probe-sha256", "transport-sha256", "nonce", "workdir"):
        parser.add_argument("--" + name, required=True)
    for name in ("broker-pid", "production-pid", "issued-at", "bridge-gid"):
        parser.add_argument("--" + name, type=int, required=True)
    args = parser.parse_args()
    descriptor = os.dup(1)
    os.set_inheritable(descriptor, False)
    try:
        result = asyncio.run(run_worker(args))
        summary = {
            key: result[key]
            for key in (
                "schema",
                "probe",
                "passed",
                "live_model_verified",
                "native_search_verified",
                "native_fetch_verified",
                "market_provider_verified",
                "production_verified",
                "research_quality_verified",
                "error_class",
                "stage",
            )
        }
        status = 0 if result["passed"] else 1
    except Exception:
        summary = {
            "schema": 1,
            "probe": "bounded_live_integration",
            "passed": False,
            "error_class": "same_pid_bootstrap_or_private_result_failed",
            "stage": "bootstrap_or_private_result",
            "production_verified": False,
        }
        status = 1
    with os.fdopen(descriptor, "wb") as output:
        output.write(encode(summary) + b"\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
