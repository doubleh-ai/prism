#!/usr/bin/env python3
"""Credential-free Astock probe, invoked only by the reviewed Linux controller.

Run with the prepared candidate's Python -I -B -S. The controller must verify
the real systemd identity/network namespace before publishing root-allow.json.
This program never constructs a graph/LLM or certifies live market/model access.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import importlib
import json
import os
import pwd
import re
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

COMMIT = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
NONCE = re.compile(r"[0-9a-f]{32}")
RELEASE_ID = re.compile(r"[a-z0-9][a-z0-9-]{7,63}")
OPERATIONS = Path("/var/lib/prism-operations/integration")
STARTUP_NAMES = ("sitecustomize", "usercustomize", "patch_ssl")
TLS_CLIENTS = ("urllib", "requests", "httpx")
ERROR_DETAILS = frozenset(
    {
        "astock_import_contract_rejected",
        "bootstrap_identity_rejected",
        "bootstrap_isolation_rejected",
        "candidate_site_packages_required",
        "candidate_site_prefix_mismatch",
        "credential_environment_rejected",
        "dedicated_integration_user_required",
        "engine_output_path_rejected",
        "fixed_workdir_required",
        "invalid_arguments",
        "invalid_identity",
        "isolated_candidate_interpreter_required",
        "module_source_mismatch",
        "outside_database_rejected",
        "outside_network_rejected",
        "outside_write_rejected",
        "private_source_read_rejected",
        "private_workdir_required",
        "root_allow_rejected",
        "root_allow_unavailable",
        "source_revision_mismatch",
        "startup_hook_imported",
        "startup_hook_rejected",
        "subprocess_rejected",
        "symlink_path_rejected",
        "synthetic_certificate_failed",
        "synthetic_configuration_rejected",
        "synthetic_tls_cleanup_failed",
        "tls_verification_rejected",
        "trusted_probe_source_required",
        "unlocked_source_rejected",
        "untrusted_python_path",
        "workdir_reused",
        "module_not_found",
        "import_error",
        "operating_system_error",
        "unexpected_error",
        "private_result_write_failed",
    }
)


class ProbeError(RuntimeError):
    """Only fixed safe categories cross the controller boundary."""


def safe_error_detail(error):
    """Fixed enum only; never serialize exception text, module names or paths."""
    if isinstance(error, ProbeError):
        detail = error.args[0] if len(error.args) == 1 else None
        return detail if isinstance(detail, str) and detail in ERROR_DETAILS else "unexpected_error"
    if isinstance(error, ModuleNotFoundError):
        return "module_not_found"
    if isinstance(error, ImportError):
        return "import_error"
    if isinstance(error, OSError):
        return "operating_system_error"
    return "unexpected_error"


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def write_private(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded(value))
        stream.flush()
        os.fsync(stream.fileno())


def no_links(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ProbeError("symlink_path_rejected")


def root_locked(path):
    no_links(path)
    for part in (path, *path.parents):
        info = part.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ProbeError("unlocked_source_rejected")


def fixed_paths(candidate, engine, workdir):
    if not COMMIT.fullmatch(candidate) or not DIGEST.fullmatch(engine):
        raise ProbeError("invalid_identity")
    work = Path(workdir)
    if (
        not work.is_absolute()
        or work.parent.parent != OPERATIONS
        or work.name != "astock"
        or not RELEASE_ID.fullmatch(work.parent.name)
        or str(work) != workdir
    ):
        raise ProbeError("fixed_workdir_required")
    return Path("/opt/prism/releases") / candidate, Path("/opt/prism/engines/astock") / engine, work


def validate_runtime(candidate, engine, work):
    if (
        sys.platform != "linux"
        or not sys.flags.isolated
        or not sys.flags.no_site
        or not sys.flags.dont_write_bytecode
        or Path(sys.executable) != candidate / ".venv/bin/python"
    ):
        raise ProbeError("isolated_candidate_interpreter_required")
    account = pwd.getpwuid(os.getuid())
    if (
        account.pw_name != "prism-integration"
        or account.pw_uid == 0
        or account.pw_shell != "/usr/sbin/nologin"
        or any(group != account.pw_gid for group in os.getgroups())
    ):
        raise ProbeError("dedicated_integration_user_required")
    for source in (candidate, engine, candidate / ".venv"):
        root_locked(source)
    for name in (".deploy-revision", ".engine-revision"):
        path = candidate / name
        root_locked(path)
        if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > 128:
            raise ProbeError("source_revision_mismatch")
    if (candidate / ".deploy-revision").read_text().strip() != candidate.name or (
        candidate / ".engine-revision"
    ).read_text().strip() != engine.name:
        raise ProbeError("source_revision_mismatch")
    root_locked(work)
    if not work.is_dir() or stat.S_IMODE(work.stat().st_mode) != 0o755:
        raise ProbeError("private_workdir_required")
    for name in ("output", "home", "tmp", "data"):
        path = work / name
        no_links(path)
        info = path.stat()
        if not path.is_dir() or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ProbeError("private_workdir_required")
        if any(path.iterdir()):
            raise ProbeError("workdir_reused")


def validate_probe_source(work, expected):
    path = work / "probe.py"
    root_locked(path)
    if (
        not DIGEST.fullmatch(expected)
        or not path.is_file()
        or path.stat().st_nlink != 1
        or hashlib.sha256(path.read_bytes()).hexdigest() != expected
        or Path(__file__) != path
    ):
        raise ProbeError("trusted_probe_source_required")


@contextlib.contextmanager
def private_log(path):
    """Keep native/late output private for the rest of this one-shot process."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as log:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)
        try:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                yield
        finally:
            log.flush()


def denied_open(path, *, writable=False):
    flags = os.O_CLOEXEC | os.O_NOFOLLOW
    flags |= os.O_WRONLY | os.O_CREAT | os.O_EXCL if writable else os.O_RDONLY
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        allowed = (
            {errno.EACCES, errno.EPERM, errno.EROFS}
            if writable
            else {
                errno.EACCES,
                errno.EPERM,
                errno.ENOENT,
            }
        )
        return error.errno in allowed
    os.close(descriptor)  # Deliberately do not read even one byte.
    if writable:
        os.unlink(path)  # Only our freshly created exclusive canary.
    return False


def denied_connect(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as channel:
        channel.settimeout(0.5)
        code = channel.connect_ex(("127.0.0.1", port))
    return code in {errno.EACCES, errno.EPERM, errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH}


def bootstrap_gate(args, candidate, work, *, now=time.time, pause=time.sleep):
    if (
        not NONCE.fullmatch(args.nonce)
        or type(args.production_pid) is not int
        or args.production_pid <= 1
        or not 0 <= now() - args.issued_at <= 120
    ):
        raise ProbeError("bootstrap_identity_rejected")
    proof = {
        "schema": 1,
        "candidate": args.candidate,
        "engine": args.engine,
        "nonce": args.nonce,
        "pid": os.getpid(),
        "production_pid": args.production_pid,
        "issued_at": args.issued_at,
        "checked_at": int(now()),
        "probe_sha256": args.probe_sha256,
        "checks": {
            "production_environment_denied": denied_open("/home/ubuntu/stock-analyzer/.env"),
            "production_process_environment_denied": denied_open(f"/proc/{args.production_pid}/environ"),
            "production_http_denied": denied_connect(8088),
            "sidecar_http_denied": denied_connect(8899),
            "candidate_write_denied": denied_open(
                candidate / (".integration-canary-" + args.nonce), writable=True
            ),
            "staging_directory_denied": denied_open("/var/lib/prism-operations/staging"),
        },
    }
    write_private(work / "output/bootstrap-proof.json", proof)
    if not all(proof["checks"].values()):
        raise ProbeError("bootstrap_isolation_rejected")
    expected = {
        "schema": 1,
        "candidate": args.candidate,
        "engine": args.engine,
        "nonce": args.nonce,
        "pid": os.getpid(),
        "bootstrap_proof_sha256": hashlib.sha256(encoded(proof)).hexdigest(),
        "probe_sha256": args.probe_sha256,
    }
    allow = work / "root-allow.json"
    deadline = time.monotonic() + 20
    while not allow.exists() and time.monotonic() < deadline:
        pause(0.05)
    try:
        descriptor = os.open(allow, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o444
                or info.st_size > 4096
            ):
                raise ProbeError("root_allow_rejected")
            actual = json.loads(stream.read(4097))
    except (OSError, ValueError):
        raise ProbeError("root_allow_unavailable") from None
    if not isinstance(actual, dict) or set(actual) != {*expected, "allowed_at"}:
        raise ProbeError("root_allow_rejected")
    if (
        any(
            type(actual.get(key)) is not type(value) or actual[key] != value
            for key, value in expected.items()
        )
        or type(actual.get("allowed_at")) is not int
        or not args.issued_at <= actual["allowed_at"] <= now() <= args.issued_at + 120
    ):
        raise ProbeError("root_allow_rejected")


def synthetic_environment(work, engine):
    if any(re.search(r"(?i)(?:password|secret|token|api.?key|authorization)", key) for key in os.environ):
        raise ProbeError("credential_environment_rejected")
    os.environ.clear()
    os.environ.update(
        PATH="/usr/bin:/bin",
        HOME=str(work / "home"),
        TMPDIR=str(work / "tmp"),
        TZ="Asia/Shanghai",
        LANG="C.UTF-8",
        PYTHONDONTWRITEBYTECODE="1",
        STOCK_DATA_DIR=str(work / "data"),
        STOCK_LOG_FILE=str(work / "output/probe.log"),
        ASTOCK_RESULTS_DIR=str(work / "data/results"),
        PRISM_ASTOCK_PROJECT=str(engine),
        CLAUDE_BIN="/nonexistent/prism-integration-claude",
        CLAUDE_SKILLS_HOME=str(work / "home/skills"),
        PRISM_VIBE_ENGINE_URL="http://127.0.0.1:9",
    )
    os.chdir(work)


def startup_clean(candidate):
    sites = list((candidate / ".venv/lib").glob("python*/site-packages"))
    if len(sites) != 1:
        raise ProbeError("candidate_site_packages_required")
    for path in sites[0].rglob("*"):
        if path.name.endswith(".pth") or path.name.startswith(STARTUP_NAMES):
            raise ProbeError("startup_hook_rejected")
    if any(name.startswith(STARTUP_NAMES) for name in sys.modules):
        raise ProbeError("startup_hook_imported")
    return sites[0]


def audit_policy(work, state):
    """Additional Python guard; never presented as kernel isolation evidence."""

    def private(path):
        resolved = Path(os.fsdecode(path)).resolve()
        return any(resolved.is_relative_to(work / name) for name in ("output", "home", "tmp", "data"))

    def guard(event, args):
        if event == "open" and not isinstance(args[0], int):
            path = Path(os.fsdecode(args[0])).absolute()
            resolved = path.resolve()
            if path == Path("/dev/null"):
                return
            if (
                path.is_relative_to("/home")
                or resolved.is_relative_to("/home")
                or path.name == ".env"
                or resolved.name == ".env"
                or path.suffix in {".db", ".sqlite", ".sqlite3"}
                and not private(path)
            ):
                raise ProbeError("private_source_read_rejected")
            flags = args[2]
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND) and not private(
                path
            ):
                raise ProbeError("outside_write_rejected")
        elif event == "sqlite3.connect" and (not isinstance(args[0], str) or not private(args[0])):
            raise ProbeError("outside_database_rejected")
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.truncate"}:
            if not private(args[0]):
                raise ProbeError("outside_write_rejected")
        elif event in {"os.rename", "os.link", "os.symlink"}:
            if not all(private(path) for path in args[:2]):
                raise ProbeError("outside_write_rejected")
        elif event == "socket.connect":
            if args[1] != ("127.0.0.1", state.get("tls_port")):
                raise ProbeError("outside_network_rejected")
        elif event == "subprocess.Popen":
            if args[0] != "/usr/bin/openssl" or tuple(args[1]) != state.get("openssl_argv"):
                raise ProbeError("subprocess_rejected")
        elif event in {"os.system", "os.exec", "os.posix_spawn", "os.fork", "os.forkpty"}:
            raise ProbeError("subprocess_rejected")

    return guard


def import_engine(candidate, engine, work):
    sites = startup_clean(candidate)
    import site

    # -S avoided automatic startup. Do not let explicit site.main run Ubuntu's
    # global apport/sitecustomize, user hooks or any inherited .pth file.
    original_site, original_user, original_package = (
        site.execsitecustomize,
        site.execusercustomize,
        site.addpackage,
    )

    def reject_package(*args, **kwargs):
        raise ProbeError("startup_hook_rejected")

    try:
        site.execsitecustomize = lambda: None
        site.execusercustomize = lambda: None
        site.addpackage = reject_package
        site.main()
    finally:
        site.execsitecustomize = original_site
        site.execusercustomize = original_user
        site.addpackage = original_package
    if Path(sys.prefix) != candidate / ".venv" or str(sites) not in sys.path:
        raise ProbeError("candidate_site_prefix_mismatch")
    # No user site, old editable source, CWD, inherited PYTHONPATH or /home.
    allowed = [Path(sys.base_prefix) / "lib", candidate / ".venv"]
    if any(not item or not any(Path(item).is_relative_to(root) for root in allowed) for item in sys.path):
        raise ProbeError("untrusted_python_path")
    sys.path[:0] = [str(candidate), str(engine)]
    analysis = importlib.import_module("server.analysis_engine")
    analysis._ensure_astock()
    defaults = importlib.import_module("tradingagents.default_config")
    graph = importlib.import_module("tradingagents.graph.trading_graph")
    if not isinstance(defaults.DEFAULT_CONFIG, dict) or not isinstance(graph.TradingAgentsGraph, type):
        raise ProbeError("astock_import_contract_rejected")
    for name, module in tuple(sys.modules.items()):
        if name.startswith(STARTUP_NAMES):
            raise ProbeError("startup_hook_imported")
        if name == "server" or name.startswith("server."):
            root = candidate
        elif name == "tradingagents" or name.startswith("tradingagents."):
            root = engine
        else:
            continue
        if not getattr(module, "__file__", None) or not Path(module.__file__).resolve().is_relative_to(root):
            raise ProbeError("module_source_mismatch")
    config = analysis._build_config("fast", analysis_id="0" * 32)
    for key in ("results_dir", "data_cache_dir"):
        path = Path(config[key])
        if not path.resolve(strict=True).is_relative_to(work / "data"):
            raise ProbeError("engine_output_path_rejected")
    app_config = importlib.import_module("server.config")
    database = importlib.import_module("server.database")
    if (
        Path(app_config.DATA_DIR) != work / "data"
        or Path(app_config.ASTOCK_PROJECT) != engine
        or Path(app_config.LOG_FILE) != work / "output/probe.log"
        or Path(database.DB_PATH) != work / "data/chat.db"
        or Path.home() != work / "home"
        or Path.cwd() != work
        or config.get("checkpoint_enabled") is not False
        or config.get("memory_log_path") is not None
    ):
        raise ProbeError("synthetic_configuration_rejected")


def certificate_rejection(error):
    pending, seen = [error], set()
    while pending and len(seen) < 64:
        item = pending.pop()
        if not isinstance(item, BaseException) or id(item) in seen:
            continue
        if isinstance(item, ssl.SSLCertVerificationError):
            return True
        seen.add(id(item))
        pending.extend((item.__cause__, item.__context__, getattr(item, "reason", None)))
        pending.extend(arg for arg in item.args if isinstance(arg, BaseException))
    return False


def make_certificate(work, state):
    tls = work / "output/tls"
    tls.mkdir(mode=0o700)
    config = tls / "openssl.cnf"
    descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(b"[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=prism-integration.invalid\n")
    command = [
        "/usr/bin/openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-sha256",
        "-days",
        "1",
        "-nodes",
        "-config",
        str(config),
        "-subj",
        "/CN=prism-integration.invalid",
        "-keyout",
        str(tls / "key.pem"),
        "-out",
        str(tls / "cert.pem"),
    ]
    root_locked(Path(command[0]))
    state["openssl_argv"] = tuple(command)
    try:
        completed = subprocess.run(
            command,
            env={"PATH": "/usr/bin:/bin", "HOME": str(work / "home"), "OPENSSL_CONF": str(config)},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    finally:
        state.pop("openssl_argv", None)
    if completed.returncode:
        raise ProbeError("synthetic_certificate_failed")
    for path in (tls / "key.pem", tls / "cert.pem"):
        no_links(path)
        path.chmod(0o600)
    return tls / "cert.pem", tls / "key.pem"


@contextlib.contextmanager
def tls_fixture(work, state):
    cert, key = make_certificate(work, state)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    stop = threading.Event()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(3)
        listener.settimeout(0.2)
        port = listener.getsockname()[1]
        state["tls_port"] = port

        def serve():
            while not stop.is_set():
                try:
                    channel, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    return
                with channel:
                    channel.settimeout(2)
                    try:
                        with context.wrap_socket(channel, server_side=True) as secured:
                            secured.recv(4096)
                            secured.sendall(
                                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
                            )
                    except (OSError, ValueError):
                        pass

        thread = threading.Thread(target=serve, name="synthetic-tls", daemon=True)
        thread.start()
        try:
            yield f"https://127.0.0.1:{port}/"
        finally:
            stop.set()
            thread.join(timeout=3)
            state.pop("tls_port", None)
            if thread.is_alive():
                raise ProbeError("synthetic_tls_cleanup_failed")


def tls_clients():
    requests = importlib.import_module("requests")
    httpx = importlib.import_module("httpx")
    contexts = (ssl.create_default_context(), ssl._create_default_https_context())
    with requests.Session() as session:
        defaults = session.verify is True and all(
            context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname for context in contexts
        )

    def urllib_fetch(url):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=3) as response:
            response.read(16)

    def requests_fetch(url):
        with requests.Session() as session:
            session.trust_env = False
            with session.get(url, timeout=3, allow_redirects=False) as response:
                response.raise_for_status()

    def httpx_fetch(url):
        with httpx.Client(trust_env=False, timeout=3, follow_redirects=False) as client:
            client.get(url).raise_for_status()

    return defaults, {"urllib": urllib_fetch, "requests": requests_fetch, "httpx": httpx_fetch}


def verify_tls(work, state):
    defaults, clients = tls_clients()
    checks = {"default_tls_verification": defaults}
    with tls_fixture(work, state) as url:
        for name in TLS_CLIENTS:
            try:
                clients[name](url)
            except Exception as error:
                checks["tls_" + name + "_rejected"] = certificate_rejection(error)
            else:
                checks["tls_" + name + "_rejected"] = False
    return checks


def run_probe(args):
    result = {
        "schema": 1,
        "probe": "astock_import_tls",
        "candidate": args.candidate if COMMIT.fullmatch(args.candidate) else "invalid",
        "engine": args.engine if DIGEST.fullmatch(args.engine) else "invalid",
        "nonce": args.nonce if NONCE.fullmatch(args.nonce) else "invalid",
        "pid": os.getpid(),
        "production_pid": args.production_pid,
        "probe_sha256": args.probe_sha256 if DIGEST.fullmatch(args.probe_sha256) else "invalid",
        "checked_at": int(time.time()),
        "passed": False,
        "checks": {},
        "error_class": None,
        "error_detail": None,
        "network_namespace_verified": False,
        "live_model_verified": False,
        "live_market_verified": False,
    }
    work = None
    failure = "identity_rejected"
    try:
        candidate, engine, candidate_work = fixed_paths(args.candidate, args.engine, args.workdir)
        validate_runtime(candidate, engine, candidate_work)
        validate_probe_source(candidate_work, args.probe_sha256)
        work = candidate_work
        with private_log(work / "output/probe.log"):
            failure = "credential_environment_rejected"
            synthetic_environment(work, engine)
            result["checks"]["credential_free_synthetic_environment"] = True
            failure = "bootstrap_rejected"
            bootstrap_gate(args, candidate, work)
            result["checks"]["root_bootstrap_gate_accepted"] = True
            state = {}
            sys.addaudithook(audit_policy(work, state))
            failure = "astock_import_rejected"
            import_engine(candidate, engine, work)
            result["checks"]["astock_import_and_fixed_sources"] = True
            failure = "tls_verification_rejected"
            result["checks"].update(verify_tls(work, state))
            if not all(result["checks"].values()):
                raise ProbeError("tls_verification_rejected")
            result["passed"] = True
    except Exception as error:
        result["error_class"] = failure if isinstance(error, ProbeError) else "probe_execution_failed"
        result["error_detail"] = safe_error_detail(error)
    result["checked_at"] = int(time.time())
    if work is not None:
        try:
            write_private(work / "output/result.json", result)
        except (OSError, ProbeError):
            result["passed"] = False
            result["error_class"] = "private_result_write_failed"
            result["error_detail"] = "private_result_write_failed"
    return result


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ProbeError("invalid_arguments")


def main(argv=None):
    # Preserve exactly one non-inheritable protocol FD before private logging.
    # Never restore fd 1/2: native atexit output must remain in the private log.
    summary = os.dup(1)
    parser = Parser(description=__doc__)
    for name in ("candidate", "engine", "workdir", "nonce", "probe-sha256"):
        parser.add_argument("--" + name, required=True)
    for name in ("production-pid", "issued-at"):
        parser.add_argument("--" + name, required=True, type=int)
    try:
        args = parser.parse_args(argv)
        result = run_probe(args)
    except ProbeError:
        result = {
            "schema": 1,
            "probe": "astock_import_tls",
            "passed": False,
            "error_class": "invalid_arguments",
            "error_detail": "invalid_arguments",
        }
    try:
        os.write(summary, encoded(result) + b"\n")
    finally:
        os.close(summary)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
