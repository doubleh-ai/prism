#!/usr/bin/env python3
"""Credential-free, fixed-route market integration probe.

Only a reviewed external supervisor may execute the worker after proving the
OS network/filesystem restrictions. The optional Mac launcher supplies a real
Seatbelt profile and requires actual denial controls before candidate import.
The provider parser, chain, cache, router and capability dispatch are unchanged.
Only transport, one-provider configuration and synthetic auth/data are scoped.
No model, credential reader, SSH, production write, redirect or retry exists.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import errno
import hashlib
import http.client
import importlib
import ipaddress
import json
import math
import os
import re
import socket
import ssl
import stat
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

CANDIDATE = "f0897e84cdc0fa31871eeee90ed2e2511cc72a13"
MAC_REPO = Path("/Users/huayu/Documents/ChatGPT/website/prism-evolution-v0200")
MAC_PYTHON = Path("/private/tmp/prism-dependency-audit-20261021/venv/bin/python")
MAC_SITES = Path("/private/tmp/prism-dependency-audit-20261021/venv/lib/python3.12/site-packages")
MAC_MANIFEST = Path(
    "/Users/huayu/Library/Application Support/Prism-Release/2026-10-22/candidate-f0897e84/"
    "prism-v0.20.0-f0897e84cdc0fa31871eeee90ed2e2511cc72a13.manifest.json"
)
MANIFEST_SHA256 = "73f130b02b12750c7d9d6c5a53be1735656f66ea025f25a72cf3efa3a1e5edf6"
ROUTES = {
    "quote": "https://qt.gtimg.cn/q=sh600519",
    "kline": "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol=sh600519&scale=240&ma=no&datalen=10",
}
MAX_BODY = 512 * 1024
ERROR_CODES = {
    "actual_os_denial_controls_failed",
    "candidate_manifest_rejected",
    "candidate_module_source_rejected",
    "candidate_source_changed",
    "fixed_private_workspace_required",
    "fixed_public_peer_required",
    "fixed_read_required",
    "fixed_request_budget_exhausted",
    "isolated_interpreter_required",
    "pinned_peer_changed",
    "synthetic_database_required",
    "tls_verification_required",
    "upstream_body_limit",
    "upstream_response_rejected",
}
PRIVATE_ROOTS = (
    "/Users/huayu/.ssh",
    "/Users/huayu/.aws",
    "/Users/huayu/.claude",
    "/Users/huayu/.codex",
    "/Users/huayu/.config",
    "/Users/huayu/Backups",
    "/Users/huayu/Library/Keychains",
    "/Users/huayu/Library/Application Support/Prism-Release",
    "/home",
    "/root",
)


class ProbeError(RuntimeError):
    """Fixed safe categories; never serialize an upstream exception or body."""


def safe_error(error):
    if (
        isinstance(error, ProbeError)
        and len(error.args) == 1
        and isinstance(error.args[0], str)
        and error.args[0] in ERROR_CODES
    ):
        return error.args[0]
    return "operating_system_error" if isinstance(error, OSError) else "unexpected_error"


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def write_private(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded(value))
        stream.flush()
        os.fsync(stream.fileno())


def public_addresses(host, values):
    if host not in {urlsplit(value).hostname for value in ROUTES.values()} or not values:
        raise ProbeError("fixed_public_peer_required")
    result = set()
    for value in values:
        if not isinstance(value, str) or "%" in value:
            raise ProbeError("fixed_public_peer_required")
        address = ipaddress.ip_address(value)
        if (
            not address.is_global
            or address.is_multicast
            or address.is_reserved
            or isinstance(address, ipaddress.IPv6Address)
            and (
                address.ipv4_mapped
                or address.sixtofour
                or address.teredo
                or address.is_site_local
                or address in ipaddress.ip_network("64:ff9b::/96")
            )
        ):
            raise ProbeError("fixed_public_peer_required")
        result.add(str(address))
    return sorted(result, key=lambda item: (":" in item, item))


def validate_pins(pins):
    hosts = {urlsplit(value).hostname for value in ROUTES.values()}
    if type(pins) is not dict or set(pins) != hosts:
        raise ProbeError("fixed_public_peer_required")
    return {host: public_addresses(host, values) for host, values in pins.items()}


class Response:
    def __init__(self, connection, response, transport, operation):
        self.connection, self.response = connection, response
        self.transport, self.operation = transport, operation

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.connection.close()

    def read(self, count=-1):
        if count != -1:
            raise ProbeError("fixed_read_required")
        body = self.response.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            raise ProbeError("upstream_body_limit")
        self.transport.receipts[self.operation] = {
            "https_verified": True,
            "redirect_followed": False,
            "response_sha256": hashlib.sha256(body).hexdigest(),
            "response_bytes": len(body),
        }
        return body


class FixedTransport:
    """Budget and TLS wrapper only; real candidate parsers consume the bytes."""

    def __init__(self, pins, *, connector=None):
        self.pins = validate_pins(pins)
        self.counts = dict.fromkeys(ROUTES, 0)
        self.denied = 0
        self.receipts = {}
        self.closed = False
        self.connector = connector or self.connect

    def connect(self, host, pin, timeout):
        channel = socket.socket(socket.AF_INET6 if ":" in pin else socket.AF_INET, socket.SOCK_STREAM)
        try:
            channel.settimeout(min(12, timeout))
            channel.connect((pin, 443))  # One pinned peer, no DNS/fallback/retry here.
            context = ssl.create_default_context()
            if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
                raise ProbeError("tls_verification_required")
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            secured = context.wrap_socket(channel, server_hostname=host)
            if secured.getpeername()[0] != pin:
                secured.close()
                raise ProbeError("pinned_peer_changed")
            return secured
        except BaseException:
            channel.close()
            raise

    def urlopen(self, request, timeout=12, **kwargs):
        operation = next((key for key, value in ROUTES.items() if request.full_url == value), None)
        if (
            operation is None
            or self.closed
            or self.counts[operation] != 0
            or kwargs
            or request.get_method() != "GET"
            or request.data is not None
            or any(key.casefold() != "user-agent" for key in request.headers)
        ):
            self.denied += 1
            raise ProbeError("fixed_request_budget_exhausted")
        self.counts[operation] += 1  # Failed requests still consume the one slot.
        parsed = urlsplit(request.full_url)
        host = parsed.hostname
        connection = http.client.HTTPSConnection(host, timeout=min(12, timeout))
        try:
            connection.sock = self.connector(host, self.pins[host][0], timeout)
            target = parsed.path + ("?" + parsed.query if parsed.query else "")
            connection.request(
                "GET", target, headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"}
            )
            response = connection.getresponse()
            if (
                response.status != 200
                or response.getheader("Content-Encoding", "identity").lower() != "identity"
            ):
                raise ProbeError("upstream_response_rejected")
            return Response(connection, response, self, operation)
        except BaseException:
            connection.close()
            raise


def denied_connect(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as channel:
        channel.settimeout(0.25)
        result = channel.connect_ex((host, port))
    return result in {errno.EACCES, errno.EPERM}


def denied_open(path, *, write=False):
    try:
        descriptor = os.open(path, (os.O_WRONLY if write else os.O_RDONLY) | os.O_NOFOLLOW)
    except OSError as error:
        return error.errno in {errno.EACCES, errno.EPERM, errno.EROFS}
    os.close(descriptor)  # Never read or write a byte.
    return False


def bootstrap(work):
    if not sys.flags.isolated or not sys.flags.no_site or not sys.flags.dont_write_bytecode:
        raise ProbeError("isolated_interpreter_required")
    checks = {
        "localhost_denied": denied_connect("127.0.0.1", 8088),
        "production_ip_denied": denied_connect("124.223.168.224", 22),
        "metadata_denied": denied_connect("169.254.169.254", 80),
        "private_ip_denied": denied_connect("10.0.0.1", 443),
        "other_public_ip_denied": denied_connect("8.8.8.8", 443),
        "credential_directory_denied": denied_open("/Users/huayu/.codex"),
        "backup_directory_denied": denied_open("/Users/huayu/Backups"),
        "env_sentinel_denied": denied_open(work / ".env"),
        "candidate_write_denied": denied_open(MAC_REPO / "server/config.py", write=True),
        "synthetic_empty_data": not any((work / "data").iterdir()),
    }
    if not all(checks.values()):
        raise ProbeError("actual_os_denial_controls_failed")
    return checks


def candidate_modules(work):
    os.environ.clear()
    os.environ.update(
        PATH="/usr/bin:/bin",
        HOME=str(work / "home"),
        TMPDIR=str(work / "tmp"),
        STOCK_DATA_DIR=str(work / "data"),
        STOCK_LOG_FILE=str(work / "output/application.log"),
        CLAUDE_BIN="/nonexistent/prism-market-probe",
        CLAUDE_SKILLS_HOME=str(work / "home/skills"),
        CLAUDE_SKILL_FALLBACKS=str(work / "home/fallback"),
        PRISM_VIBE_ENGINE_URL="http://127.0.0.1:9",
        PRISM_ASTOCK_PROJECT="/nonexistent/prism-market-probe",
        PRISM_LOG_LEVEL="WARNING",
        PYTHONDONTWRITEBYTECODE="1",
        TZ="Asia/Shanghai",
    )
    os.chdir(work)
    # Explicit package path under -S; no .pth, sitecustomize or user startup runs.
    sys.path[:0] = [str(MAC_REPO), str(MAC_SITES)]
    names = (
        "server.main",
        "server.auth",
        "server.database",
        "server.capabilities",
        "server.research",
        "server.research_context",
        "server.market.factory",
        "server.market.router",
        "httpx",
    )
    modules = {name: importlib.import_module(name) for name in names}
    for name, module in tuple(sys.modules.items()):
        if (name == "server" or name.startswith("server.")) and (
            not getattr(module, "__file__", None)
            or not Path(module.__file__).resolve().is_relative_to(MAC_REPO)
        ):
            raise ProbeError("candidate_module_source_rejected")
    if Path(modules["server.database"].DB_PATH) != work / "data/chat.db":
        raise ProbeError("synthetic_database_required")
    return modules


def quote_valid(value, started, finished):
    return (
        isinstance(value, dict)
        and value.get("ticker") == "600519"
        and value.get("source") == "tencent"
        and value.get("stale") is False
        and value.get("timestamp_kind") == "trade"
        and value.get("data_kind") == "quote"
        and type(value.get("price")) in {int, float}
        and math.isfinite(value["price"])
        and value["price"] > 0
        and type(value.get("retrieved_at")) is int
        and started <= value["retrieved_at"] <= finished
        and type(value.get("data_timestamp")) is int
        and 0 < value["data_timestamp"] <= finished + 5
        and value.get("timestamp") == value["data_timestamp"]
    )


def bars_valid(value, finished):
    if not isinstance(value, list) or not 1 <= len(value) <= 10:
        return False
    previous = 0
    for bar in value:
        if (
            not isinstance(bar, dict)
            or type(bar.get("timestamp")) is not int
            or not previous < bar["timestamp"] <= finished + 5
        ):
            return False
        numbers = [bar.get(key) for key in ("open", "high", "low", "close", "volume")]
        if any(type(item) not in {int, float} or not math.isfinite(item) or item < 0 for item in numbers):
            return False
        if not bar["low"] <= min(bar["open"], bar["close"]) <= max(bar["open"], bar["close"]) <= bar["high"]:
            return False
        previous = bar["timestamp"]
    return True


async def exercise(modules, transport):
    from server.market.cache import MarketCache
    from server.market.chain import ProviderChain
    from server.market.tencent_provider import TencentProvider

    factory = modules["server.market.factory"]
    previous_chain = factory._chain
    factory._chain = ProviderChain([TencentProvider()], MarketCache(), {"quote": 300, "kline": 300})
    database, auth, caps, ledger, context = (
        modules[name]
        for name in (
            "server.database",
            "server.auth",
            "server.capabilities",
            "server.research",
            "server.research_context",
        )
    )
    owner, cid = "public-market-probe", "public-market-probe-conversation"
    with database.get_db() as db:
        db.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", [owner, "synthetic-no-login"])
        db.execute("INSERT INTO conversations(id,username) VALUES(?,?)", [cid, owner])
    research = context.build_research_context()
    ledger.create_research_run(owner, cid, "Public credential-free market probe", research)
    token = caps.issue_run_token(owner, cid, research["run_id"])
    session = auth.create_session(owner)
    httpx = modules["httpx"]
    checks, received = {}, {}
    started = int(time.time())
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=modules["server.main"].app, client=("127.0.0.1", 0)),
            base_url="http://127.0.0.1",
            follow_redirects=False,
        ) as client:
            # Check auth without consuming either public request if a missing
            # auth guard incorrectly enters the provider. This failure is an
            # explicit known f089 gap, not a fabricated 401.
            transport.closed = True
            denied = await client.get("/api/quote/600519")
            transport.closed = False
            checks["unauthenticated_api_denied"] = denied.status_code == 401
            headers = {"Cookie": "token=" + session}
            for operation, path in (
                ("quote", "/api/quote/600519"),
                ("kline", "/api/quote/600519/kline?period=1d&limit=10"),
            ):
                response = await client.get(path, headers=headers)
                data = response.json()
                received[operation] = data if response.status_code == 200 else None
                checks[operation + "_real_provider"] = response.status_code == 200 and (
                    quote_valid(data, started, int(time.time()))
                    if operation == "quote"
                    else bars_valid(data.get("kline"), int(time.time()))
                )
                for_request = {
                    "name": "market_quote" if operation == "quote" else "market_kline",
                    "arguments": {
                        "ticker": "600519",
                        **({"period": "1d", "limit": 10} if operation == "kline" else {}),
                    },
                    "request_id": hashlib.sha256(operation.encode()).hexdigest(),
                }
                called = await client.post(
                    "/api/internal/capabilities/call",
                    headers={"Authorization": "Bearer " + token, "X-Requested-With": "XMLHttpRequest"},
                    json=for_request,
                )
                call = called.json()
                payload = call.get("data", {})
                checks[operation + "_owned_capability_cache_readback"] = bool(
                    data
                    and called.status_code == 200
                    and call.get("ok") is True
                    and call.get("call", {}).get("run_id") == research["run_id"]
                    and (
                        payload.get("value") == data
                        if operation == "quote"
                        else payload.get("bars") == data.get("kline")
                    )
                )
            transport.closed = True  # Explicit transport failure injection; no second public request.
            with database.get_db() as db:
                db.execute("UPDATE market_cache SET expires_at=?", [time.time() - 1])
            for operation, path in (
                ("quote", "/api/quote/600519"),
                ("kline", "/api/quote/600519/kline?period=1d&limit=10"),
            ):
                response = await client.get(path, headers=headers)
                checks[operation + "_retained_stale_after_injected_failure"] = bool(
                    received[operation]
                    and response.status_code == 200
                    and response.json().get("stale") is True
                )
            with database.get_db() as db:
                db.execute("DELETE FROM market_cache")
            empty = await client.get("/api/quote/600519", headers=headers)
            checks["no_cache_failure_explicit_503"] = (
                empty.status_code == 503 and empty.json().get("error") == "no_provider_available"
            )
    finally:
        caps.revoke_run_token(research["run_id"])
        with database.get_db() as db:
            db.execute("DELETE FROM sessions WHERE username=?", [owner])
        factory._chain = previous_chain
    kline = received.get("kline") or {}
    missing = [key for key in ("source", "retrieved_at", "data_timestamp") if key not in kline]
    return {
        "checks": checks,
        "request_counts": dict(transport.counts),
        "https": dict(transport.receipts),
        "cached_result_sha256": {
            key: hashlib.sha256(encoded(value)).hexdigest()
            for key, value in received.items()
            if value is not None
        },
        "kline_provenance_missing": missing,
        "kline_adapter_is_tencent_actual_endpoint_is_sina": True,
        "failure_injection_disclosed": True,
        "fetched_at_is_not_trade_date": True,
    }


def checked_work(work):
    work = Path(work)
    if (
        not work.is_absolute()
        or work.parent != Path("/private/tmp")
        or not re.fullmatch(r"prism-market-[a-z0-9-]{8,40}", work.name)
    ):
        raise ProbeError("fixed_private_workspace_required")
    for directory in (work, *(work / name for name in ("home", "data", "tmp", "output", "denied"))):
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ProbeError("fixed_private_workspace_required")
    return work


def worker(work, *, check_only=False):
    work = checked_work(work)
    result = {
        "schema": 1,
        "candidate": CANDIDATE,
        "probe": "credential_free_market",
        "passed": False,
        "production_verified": False,
        "research_quality_verified": False,
        "linux_runtime_verified": False,
        "release_ready": False,
        "error_class": None,
        "error_detail": None,
        "market_provider_verified": False,
    }
    try:
        result["os_denial_controls"] = bootstrap(work)
        if check_only:
            result.update(probe="market_os_denial_controls_only", passed=True, market_provider_verified=False)
            write_private(work / "output/receipt.json", result)
            return result
        pins = validate_pins(json.loads((work / "pins.json").read_bytes()))
        verify_mac_candidate(work / "candidate-manifest.json")
        modules = candidate_modules(work)
        transport = FixedTransport(pins)
        import urllib.request

        original = urllib.request.urlopen
        fd = os.open(work / "output/private.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            urllib.request.urlopen = transport.urlopen
            try:
                result.update(asyncio.run(exercise(modules, transport)))
            finally:
                urllib.request.urlopen = original
        result["passed"] = all(result["checks"].values()) and transport.counts == {"quote": 1, "kline": 1}
        result["market_provider_verified"] = all(
            result["checks"].get(name) is True for name in ("quote_real_provider", "kline_real_provider")
        )
        if not result["passed"]:
            result["error_class"] = "market_acceptance_incomplete"
    except Exception as error:
        result["error_class"] = "market_probe_failed"
        result["error_detail"] = safe_error(error)
    write_private(work / "output/receipt.json", result)
    return result


def verify_mac_candidate(manifest_path=MAC_MANIFEST):
    # Read only the verified public candidate manifest. No private Git history,
    # backup payload or configuration content is needed by this launcher.
    descriptor = os.open(manifest_path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if os.fstat(stream.fileno()).st_size > 2 * 1024 * 1024:
            raise ProbeError("candidate_manifest_rejected")
        raw = stream.read(2 * 1024 * 1024 + 1)
    if hashlib.sha256(raw).hexdigest() != MANIFEST_SHA256:
        raise ProbeError("candidate_manifest_rejected")
    manifest = json.loads(raw)
    if manifest.get("commit") != CANDIDATE or len(manifest.get("files", [])) != 87:
        raise ProbeError("candidate_source_changed")
    names = set()
    for item in manifest["files"]:
        name = item["path"]
        if name.startswith("/") or ".." in Path(name).parts or name in names:
            raise ProbeError("candidate_manifest_rejected")
        names.add(name)
        target = MAC_REPO / name
        if (
            target.resolve() != target
            or not target.is_file()
            or hashlib.sha256(target.read_bytes()).hexdigest() != item["sha256"]
        ):
            raise ProbeError("candidate_source_changed")
    if {str(path.relative_to(MAC_REPO)) for path in (MAC_REPO / "server").rglob("*.py")} - names:
        raise ProbeError("candidate_source_changed")
    return MANIFEST_SHA256


def mac_profile(work, pins):
    literals = " ".join(
        "(remote ip " + json.dumps(("[" + value + "]" if ":" in value else value) + ":443") + ")"
        for values in pins.values()
        for value in values
    )
    denied = " ".join("(subpath " + json.dumps(value) + ")" for value in PRIVATE_ROOTS)
    return (
        "(version 1)\n(allow default)\n(deny network*)\n(deny process-fork)\n(allow network-outbound "
        + literals
        + ")\n"
        "(deny file-write* (require-all (require-not (subpath "
        + json.dumps(str(work))
        + ')) (require-not (literal "/dev/null"))))\n'
        "(deny file-read* "
        + denied
        + ' (literal "/Users/huayu/.claude.json") (subpath '
        + json.dumps(str(work / "denied"))
        + ') (regex #"(^|/)[.]env($|[.])"))\n'
    )


def launch_mac(work, *, check_only=False):
    if (
        sys.platform != "darwin"
        or not work.is_absolute()
        or work.parent != Path("/private/tmp")
        or not re.fullmatch(r"prism-market-[a-z0-9-]{8,40}", work.name)
    ):
        raise ProbeError("fixed_private_workspace_required")
    source_sha = verify_mac_candidate()
    work.mkdir(mode=0o700)
    for name in ("home", "data", "tmp", "output", "denied"):
        (work / name).mkdir(mode=0o700)
    (work / "denied/.env").write_text("SYNTHETIC_NO_CREDENTIAL=1\n")
    (work / ".env").write_text("SYNTHETIC_NO_CREDENTIAL=1\n")
    manifest_raw = MAC_MANIFEST.read_bytes()
    if hashlib.sha256(manifest_raw).hexdigest() != MANIFEST_SHA256:
        raise ProbeError("candidate_manifest_rejected")
    descriptor = os.open(
        work / "candidate-manifest.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(manifest_raw)
        stream.flush()
        os.fsync(stream.fileno())
    pins = {
        host: public_addresses(
            host, [row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]
        )
        for host in {urlsplit(value).hostname for value in ROUTES.values()}
    }
    write_private(work / "pins.json", pins)
    profile = work / "sandbox.sb"
    profile.write_text(mac_profile(work, pins))
    profile.chmod(0o600)
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(work / "home"),
        "TMPDIR": str(work / "tmp"),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    command = [
        "/usr/bin/sandbox-exec",
        "-f",
        str(profile),
        str(MAC_PYTHON),
        "-I",
        "-B",
        "-S",
        str(Path(__file__).resolve()),
        "--worker",
        str(work),
    ]
    if check_only:
        command.append("--check-only")
    process = subprocess.run(command, env=environment, capture_output=True, timeout=70)
    receipt = (
        json.loads((work / "output/receipt.json").read_bytes())
        if (work / "output/receipt.json").exists()
        else {"passed": False, "error_class": "worker_no_receipt"}
    )
    receipt.update(
        candidate_runtime_source_manifest_sha256=source_sha,
        sandbox_profile_sha256=hashlib.sha256(profile.read_bytes()).hexdigest(),
        stdout_bytes=len(process.stdout),
        stderr_bytes=len(process.stderr),
        worker_exit_code=process.returncode,
    )
    write_private(work / "mac-supervisor-receipt.json", receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description="Reviewed fixed public market probe")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--worker", type=Path)
    mode.add_argument("--mac-sandbox", type=Path)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    try:
        result = (
            worker(args.worker, check_only=args.check_only)
            if args.worker
            else launch_mac(args.mac_sandbox, check_only=args.check_only)
        )
    except Exception as error:
        result = {
            "passed": False,
            "error_class": "market_probe_supervisor_failed",
            "error_detail": safe_error(error),
        }
        if args.mac_sandbox:
            with contextlib.suppress(Exception):
                directory = checked_work(args.mac_sandbox)
                write_private(directory / "mac-supervisor-failure.json", result)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
