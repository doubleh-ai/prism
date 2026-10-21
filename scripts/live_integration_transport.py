#!/usr/bin/env python3
"""Bounded live-probe transport, used only by a reviewed external supervisor.

No credential files are read. ``Broker.authorize`` calls a trusted root IPC
gatekeeper and reads its immutable, PID-bound permit BEFORE key_provider().
The root gatekeeper must establish actual kernel/egress facts; JSON alone is
not an oracle. The broker runs as prism-integration-broker, never as root.
The worker stays in a different private network namespace and reaches the
broker only through a group-protected UDS and its own loopback HTTP relay.

No CONNECT, redirect, automatic retry, generic URL, proxy or market fetch is
implemented. This module does not certify native WebFetch or research quality.
The supervisor must enforce the 180s unit lifetime/control-group cleanup and
verify its real filesystem/network restrictions; these helpers never attest
systemd properties from their requested names alone.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import http.client
import importlib.util
import io
import json
import os
import pwd
import re
import socket
import ssl
import stat
import struct
import sys
import threading
import time
from pathlib import Path

POLICY_SHA256 = "c6fa205466800636fe6fbe557eb8f625785567a129b405ebb4935b7b65e325d9"
COMMIT = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
NONCE = re.compile(r"[0-9a-f]{32}")
MAX_BODY = 512 * 1024
MAX_HEADERS = 16384
MAX_RESPONSE = 1024 * 1024
MAX_TOTAL_REQUEST = 2 * 1024 * 1024
MAX_TOTAL_RESPONSE = 8 * 1024 * 1024
HOST = "api.deepseek.com"
ROUTES = {"/anthropic/v1/messages", "/anthropic/v1/messages/count_tokens"}
SDK_HEADERS = {
    "host",
    "content-length",
    "content-type",
    "accept",
    "accept-encoding",
    "connection",
    "authorization",
    "x-api-key",
    "anthropic-version",
    "anthropic-beta",
    "user-agent",
    "x-app",
    "anthropic-dangerous-direct-browser-access",
    "x-stainless-lang",
    "x-stainless-package-version",
    "x-stainless-os",
    "x-stainless-arch",
    "x-stainless-runtime",
    "x-stainless-runtime-version",
    "x-stainless-retry-count",
    "x-stainless-timeout",
}
KERNEL_KEYS = {
    "worker_uids",
    "broker_uids",
    "worker_netns_inode",
    "broker_netns_inode",
    "host_netns_inode",
    "worker_startticks",
    "broker_startticks",
}


class TransportError(RuntimeError):
    """Only fixed categories; never attach provider bodies, headers or secrets."""


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()


def locked(path):
    path = Path(path)
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise TransportError("root_locked_source_required")


def read_record(path, *, owner=0, mode=0o444, maximum=16384):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != owner
            or stat.S_IMODE(info.st_mode) != mode
            or info.st_size > maximum
        ):
            raise TransportError("private_record_rejected")
        raw = stream.read(maximum + 1)
    try:
        value = strict_json(raw)
    except TransportError:
        raise TransportError("private_record_rejected") from None
    if not isinstance(value, dict):
        raise TransportError("private_record_rejected")
    return value, raw


def write_private(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())


def strict_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError
            value[key] = item
        return value

    def constant(_):
        raise ValueError

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise TransportError("json_rejected") from None


def load_policy(path):
    """Only the reviewed frozen dependency; never import a user-supplied module."""
    locked(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 32768:
            raise TransportError("policy_source_rejected")
        raw = stream.read(32769)
    if hashlib.sha256(raw).hexdigest() != POLICY_SHA256:
        raise TransportError("policy_source_rejected")
    spec = importlib.util.spec_from_loader("_prism_frozen_live_policy", loader=None)
    module = importlib.util.module_from_spec(spec)
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


def validate_binding(binding):
    sizes = {"candidate": 40, "cli_sha256": 64, "probe_sha256": 64, "nonce": 32}
    if (
        not isinstance(binding, dict)
        or set(binding) != {*sizes, "worker_pid", "broker_pid"}
        or any(
            not isinstance(binding.get(k), str) or not re.fullmatch("[0-9a-f]{" + str(n) + "}", binding[k])
            for k, n in sizes.items()
        )
        or any(type(binding.get(k)) is not int or binding[k] <= 1 for k in ("worker_pid", "broker_pid"))
        or binding["worker_pid"] == binding["broker_pid"]
    ):
        raise TransportError("binding_rejected")


def fixed_work(work):
    work = Path(work)
    if (
        str(work) != os.fspath(work)
        or work.name != "live"
        or work.parent.parent != Path("/var/lib/prism-operations/integration")
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{7,39}", work.parent.name)
    ):
        raise TransportError("fixed_workdir_required")
    locked(work)
    if stat.S_IMODE(work.stat().st_mode) != 0o755:
        raise TransportError("fixed_workdir_required")
    return work


def verify_allow(work, binding, policy, *, proof_raw=None, now=time.time):
    validate_binding(binding)
    allow, _ = read_record(Path(work) / "root-allow.json")
    if (
        set(allow)
        != {
            "schema",
            "binding",
            "policy_sha256",
            "bootstrap_proof_sha256",
            "allowed_at",
            "witness",
            "kernel",
            "sources",
            "broker_memory_sealed",
        }
        or type(allow.get("schema")) is not int
        or allow["schema"] != 1
        or allow.get("binding") != binding
        or allow.get("policy_sha256") != POLICY_SHA256
        or allow.get("broker_memory_sealed") is not True
        or not isinstance(allow.get("bootstrap_proof_sha256"), str)
        or not DIGEST.fullmatch(allow["bootstrap_proof_sha256"])
        or type(allow.get("allowed_at")) is not int
        or not 0 <= now() - allow["allowed_at"] <= 120
    ):
        raise TransportError("root_allow_rejected")
    sources = allow.get("sources")
    if (
        not isinstance(sources, dict)
        or set(sources) != {"worker", "transport", "policy"}
        or sources.get("policy") != POLICY_SHA256
        or sources.get("worker") != binding["probe_sha256"]
        or any(not isinstance(value, str) or not DIGEST.fullmatch(value) for value in sources.values())
    ):
        raise TransportError("root_allow_rejected")
    for name in sources:
        path = Path(work) / (name + ".py")
        locked(path)
        if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > 131072:
            raise TransportError("approved_source_required")
        if hashlib.sha256(path.read_bytes()).hexdigest() != sources[name]:
            raise TransportError("approved_source_required")
    if Path(__file__) != Path(work) / "transport.py":
        raise TransportError("approved_source_required")
    if proof_raw is not None and hashlib.sha256(proof_raw).hexdigest() != allow["bootstrap_proof_sha256"]:
        raise TransportError("root_allow_rejected")
    try:
        policy.require_release_witnesses(allow["witness"], binding, now=int(now()))
    except (policy.PolicyError, TypeError, KeyError):
        raise TransportError("root_allow_rejected") from None
    kernel = allow["kernel"]
    worker = pwd.getpwnam("prism-integration").pw_uid
    broker = pwd.getpwnam("prism-integration-broker").pw_uid
    if (
        not isinstance(kernel, dict)
        or set(kernel) != KERNEL_KEYS
        or 0 in {worker, broker}
        or worker == broker
        or kernel.get("worker_uids") != [worker] * 4
        or kernel.get("broker_uids") != [broker] * 4
        or any(
            type(kernel.get(k)) is not int or kernel[k] <= 0
            for k in KERNEL_KEYS - {"worker_uids", "broker_uids"}
        )
        or kernel["worker_netns_inode"] == kernel["broker_netns_inode"]
        or kernel["broker_netns_inode"] != kernel["host_netns_inode"]
    ):
        raise TransportError("kernel_identity_rejected")
    if not allow["witness"]["checked_at"] <= allow["allowed_at"]:
        raise TransportError("root_allow_rejected")
    return kernel


def self_identity(kernel, binding, role):
    """Actual current-process checks, in addition to root's other-process proof."""
    account = pwd.getpwnam("prism-integration" if role == "worker" else "prism-integration-broker")
    if (
        sys.platform != "linux"
        or os.getuid() == 0
        or os.getuid() != account.pw_uid
        or account.pw_shell != "/usr/sbin/nologin"
        or os.getpid() != binding[role + "_pid"]
    ):
        raise TransportError("dedicated_uid_required")
    status = Path("/proc/self/status").read_text()
    row = next((line for line in status.splitlines() if line.startswith("Uid:")), "")
    if [int(value) for value in row.split()[1:]] != kernel[role + "_uids"]:
        raise TransportError("kernel_identity_rejected")
    if os.stat("/proc/self/ns/net").st_ino != kernel[role + "_netns_inode"]:
        raise TransportError("kernel_identity_rejected")
    fields = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()
    if int(fields[19]) != kernel[role + "_startticks"]:
        raise TransportError("kernel_identity_rejected")


def synthetic_token(binding):
    validate_binding(binding)
    return "PRISM_LIVE_SYNTHETIC_" + binding["nonce"]


def peer_identity(channel, pid, uid):
    if sys.platform != "linux" or not hasattr(socket, "SO_PEERCRED"):
        raise TransportError("unix_peer_required")
    actual_pid, actual_uid, _ = struct.unpack(
        "3i", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    )
    if (actual_pid, actual_uid) != (pid, uid):
        raise TransportError("unix_peer_rejected")


def parse_http(stream, *, methods=("POST",), maximum=MAX_BODY):
    """One request per connection; reject smuggling/ambiguous framing before I/O."""
    line = stream.readline(4097)
    if len(line) > 4096 or not line.endswith(b"\r\n"):
        raise TransportError("http_request_rejected")
    try:
        method, target, version = line[:-2].decode("ascii").split(" ")
    except (UnicodeError, ValueError):
        raise TransportError("http_request_rejected") from None
    if (
        method not in methods
        or version != "HTTP/1.1"
        or not target.startswith("/")
        or target.startswith("//")
        or any(ord(c) <= 32 or ord(c) >= 127 for c in target)
    ):
        raise TransportError("http_request_rejected")
    headers, length = {}, len(line)
    while True:
        raw = stream.readline(4097)
        length += len(raw)
        if not raw.endswith(b"\r\n") or len(raw) > 4096 or length > MAX_HEADERS:
            raise TransportError("http_headers_rejected")
        if raw == b"\r\n":
            break
        if raw[:1] in {b" ", b"\t"} or b":" not in raw:
            raise TransportError("http_headers_rejected")
        name, value = raw[:-2].split(b":", 1)
        try:
            name, value = name.decode("ascii").lower(), value.decode("ascii").strip(" ")
        except UnicodeError:
            raise TransportError("http_headers_rejected") from None
        if (
            not re.fullmatch(r"[a-z0-9!#$%&'*+.^_`|~-]+", name)
            or name in headers
            or len(headers) >= 40
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise TransportError("http_headers_rejected")
        headers[name] = value
    if (
        "transfer-encoding" in headers
        or "expect" in headers
        or "trailer" in headers
        or "host" not in headers
        or method == "POST"
        and "content-length" not in headers
    ):
        raise TransportError("http_framing_rejected")
    value = headers.get("content-length", "0")
    if (
        not re.fullmatch(r"0|[1-9][0-9]{0,6}", value)
        or int(value) > maximum
        or method == "GET"
        and int(value)
    ):
        raise TransportError("http_framing_rejected")
    body = stream.read(int(value))
    if len(body) != int(value):
        raise TransportError("http_body_rejected")
    return method, target, headers, body


class SocketReader:
    """Exact bounded framing with a total deadline, including slow header/body input."""

    def __init__(self, channel, deadline):
        self.channel, self.deadline, self.buffer = channel, deadline, bytearray()

    def receive(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TransportError("http_deadline")
        self.channel.settimeout(remaining)
        return self.channel.recv(8192)

    def readline(self, maximum):
        while b"\n" not in self.buffer and len(self.buffer) < maximum:
            chunk = self.receive()
            if not chunk:
                break
            self.buffer.extend(chunk)
        end = self.buffer.find(b"\n") + 1 or len(self.buffer)
        end = min(end, maximum)
        line = bytes(self.buffer[:end])
        del self.buffer[:end]
        return line

    def read(self, count):
        while len(self.buffer) < count:
            chunk = self.receive()
            if not chunk:
                break
            self.buffer.extend(chunk)
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        return result


@contextlib.contextmanager
def socket_deadline(channel, deadline):
    def expire():
        with contextlib.suppress(OSError):
            channel.shutdown(socket.SHUT_RDWR)

    timer = threading.Timer(max(0, deadline - time.monotonic()), expire)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()


def provider_request(request, token, policy):
    method, target, headers, raw = request
    path = target.partition("?")[0]
    if method != "POST" or path not in ROUTES or set(headers) - SDK_HEADERS:
        raise TransportError("provider_request_rejected")
    if (
        headers.get("host") != "prism-model-broker"
        or headers.get("content-type", "").lower() != "application/json"
        or headers.get("x-stainless-retry-count", "0") != "0"
    ):
        raise TransportError("provider_request_rejected")
    supplied = [headers[key] for key in ("authorization", "x-api-key") if key in headers]
    expected = "Bearer " + token if "authorization" in headers else token
    if len(supplied) != 1 or not hmac.compare_digest(supplied[0], expected):
        raise TransportError("synthetic_auth_required")
    try:
        operation = policy.https_target("https://" + HOST + target, role="model")
        body = strict_json(raw)
        if isinstance(body, dict):
            tools = body.get("tools", [])
            allowed = {"WebSearch", "web_search", "mcp__prism__indicator_list", "mcp__prism__research_read"}
            if (
                not isinstance(tools, list)
                or len(tools) > 4
                or any(not isinstance(tool, dict) or tool.get("name") not in allowed for tool in tools)
            ):
                raise TransportError("provider_tools_rejected")
        if operation == "count_tokens":
            if not isinstance(body, dict) or "max_tokens" in body:
                raise TransportError("provider_request_rejected")
            body = policy.bounded_model_body({**body, "max_tokens": 1024})
            body.pop("max_tokens")
        else:
            body = policy.bounded_model_body(body)
        raw = canonical(body)
    except (policy.PolicyError, TypeError, ValueError):
        raise TransportError("provider_request_rejected") from None
    forwarded = {"Content-Type": "application/json", "Accept-Encoding": "identity"}
    for key in ("accept", "anthropic-version", "anthropic-beta"):
        if key in headers:
            if len(headers[key]) > 512 or not re.fullmatch(r"[A-Za-z0-9,_. /;=+*-]+", headers[key]):
                raise TransportError("provider_header_rejected")
            forwarded[key] = headers[key]
    return target, operation, raw, forwarded


class PinnedUpstream:
    """No environment proxies, redirects or retries; TLS SNI stays provider host."""

    def __init__(self, policy):
        self.policy = policy

    def connect(self, deadline):
        if any(
            name.upper()
            in {
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "SSLKEYLOGFILE",
                "OPENSSL_CONF",
                "OPENSSL_MODULES",
                "HTTPS_PROXY",
                "HTTP_PROXY",
                "ALL_PROXY",
                "LD_PRELOAD",
                "LD_LIBRARY_PATH",
            }
            for name in os.environ
        ):
            raise TransportError("upstream_environment_rejected")
        if time.monotonic() >= deadline:
            raise TransportError("upstream_deadline")
        answers = socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM)
        try:
            pins = self.policy.public_peers(HOST, [row[4][0] for row in answers])
        except self.policy.PolicyError:
            raise TransportError("upstream_dns_rejected") from None
        if time.monotonic() >= deadline:
            raise TransportError("upstream_deadline")
        pin = sorted(pins, key=lambda item: ":" in item)[0]
        channel = socket.socket(socket.AF_INET6 if ":" in pin else socket.AF_INET, socket.SOCK_STREAM)
        try:
            channel.settimeout(max(0.01, min(25, deadline - time.monotonic())))
            channel.connect((pin, 443))
            context = ssl.create_default_context()
            if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
                raise TransportError("upstream_tls_rejected")
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            secured = context.wrap_socket(channel, server_hostname=HOST)
            if secured.getpeername()[0] != pin:
                secured.close()
                raise TransportError("upstream_peer_rejected")
            return secured
        except BaseException:
            channel.close()
            raise

    def preflight(self, deadline):
        with self.connect(deadline):
            return {"public_dns_pinned": True, "upstream_tls_verified": True}

    @contextlib.contextmanager
    def request(self, target, body, headers, key, deadline):
        connection = http.client.HTTPConnection(
            HOST, 443, timeout=max(0.01, min(25, deadline - time.monotonic()))
        )
        connection.sock = self.connect(deadline)
        try:
            outgoing = {**headers, "Host": HOST, "Authorization": "Bearer " + key, "Connection": "close"}
            with socket_deadline(connection.sock, deadline):
                connection.request("POST", target, body=body, headers=outgoing)
                response = connection.getresponse()
                content_type = response.getheader("Content-Type", "").partition(";")[0].strip().lower()
                if response.status != 200 or content_type not in {"application/json", "text/event-stream"}:
                    raise TransportError("upstream_response_rejected")
                if response.getheader("Content-Encoding", "identity").lower() != "identity":
                    raise TransportError("upstream_response_rejected")
                yield response, content_type
        finally:
            connection.close()


def redacted_stream(chunks, secret):
    """Never forward the real key, including across SSE/socket chunk boundaries."""
    pending = b""
    for chunk in chunks:
        pending += chunk
        # Replace before cutting: no prefix of a complete key can escape.
        pending = pending.replace(secret, b"[redacted]")
        safe = max(0, len(pending) - len(secret) + 1)
        if safe:
            yield pending[:safe]
            pending = pending[safe:]
    if pending:
        yield pending.replace(secret, b"[redacted]")


def error_response(channel):
    body = b'{"error":{"type":"prism_integration_blocked","message":"request blocked"}}'
    with contextlib.suppress(OSError):
        channel.sendall(
            b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body
        )


class Broker:
    def __init__(self, work, binding):
        self.work, self.binding = fixed_work(work), dict(binding)
        self.policy = load_policy(self.work / "policy.py")
        validate_binding(binding)
        self.token = synthetic_token(binding)
        self.budget = self.policy.RequestBudget()
        self.upstream = PinnedUpstream(self.policy)
        self.lock = threading.Lock()
        self.deadline = time.monotonic() + 180
        self.request_bytes = self.response_bytes = 0
        self._key = None
        self.authorized = False
        self.authorization_attempted = False
        self.ready = threading.Event()
        self.completed = self.failed = 0
        self.unknown_header_names = set()

    def authorize(self, *, gatekeeper, key_provider):
        """Callbacks are trusted supervisor IPC closures, not candidate code."""
        fixed_work(self.work)
        if (
            self.authorization_attempted
            or self.authorized
            or self._key is not None
            or not callable(gatekeeper)
            or not callable(key_provider)
        ):
            raise TransportError("broker_authorization_rejected")
        self.authorization_attempted = True
        account = pwd.getpwnam("prism-integration-broker")
        if (
            os.getpid() != self.binding["broker_pid"]
            or os.getuid() == 0
            or os.getuid() != account.pw_uid
            or account.pw_shell != "/usr/sbin/nologin"
            or any(
                re.search(r"(?i)(?:password|secret|token|api.?key|authorization)", name)
                for name in os.environ
            )
        ):
            raise TransportError("dedicated_uid_required")
        facts = self.upstream.preflight(min(self.deadline, time.monotonic() + 25))
        try:
            gatekeeper(dict(self.binding), facts)  # Must establish actual kernel facts as root.
        except Exception:
            raise TransportError("root_gatekeeper_unavailable") from None
        kernel = verify_allow(self.work, self.binding, self.policy)
        self_identity(kernel, self.binding, "broker")
        if time.monotonic() >= self.deadline:
            raise TransportError("broker_deadline")
        try:
            key = key_provider()  # The first and only real credential acquisition point.
        except Exception:
            raise TransportError("broker_key_unavailable") from None
        if not isinstance(key, str) or not 16 <= len(key) <= 4096 or not re.fullmatch(r"[!-~]+", key):
            raise TransportError("broker_key_rejected")
        # Reconcile after IPC; PID/namespace changes or expired permits fail closed.
        if time.monotonic() >= self.deadline:
            raise TransportError("broker_deadline")
        kernel = verify_allow(self.work, self.binding, self.policy)
        self_identity(kernel, self.binding, "broker")
        self._key = bytearray(key.encode("ascii"))
        self.authorized = True
        self.ready.set()

    def close(self):
        self.authorized = False
        self.ready.clear()
        if self._key is not None:
            self._key[:] = b"\0" * len(self._key)
            self._key = None

    def summary(self):
        return {
            "requests": dict(self.budget.counts),
            "request_bytes": self.request_bytes,
            "response_bytes": self.response_bytes,
            "completed": self.completed,
            "failed": self.failed,
            "unknown_header_names": sorted(self.unknown_header_names),
            "native_fetch_verified": False,
            "research_quality_verified": False,
        }

    def handle(self, channel):
        sent = False
        try:
            peer_identity(channel, self.binding["worker_pid"], pwd.getpwnam("prism-integration").pw_uid)
            channel.settimeout(max(0.01, min(25, self.deadline - time.monotonic())))
            request = parse_http(SocketReader(channel, min(self.deadline, time.monotonic() + 25)))
            self.unknown_header_names.update(
                name for name in set(request[2]) - SDK_HEADERS if re.fullmatch("[a-z][a-z0-9-]{0,63}", name)
            )
            target, operation, body, headers = provider_request(request, self.token, self.policy)
            self.ready.wait(timeout=max(0, min(5, self.deadline - time.monotonic())))
            # Serialize upstream calls, reserving before any request I/O.
            with self.lock:
                if not self.authorized or self._key is None or time.monotonic() >= self.deadline:
                    raise TransportError("broker_not_authorized")
                if self.request_bytes + len(body) > MAX_TOTAL_REQUEST:
                    raise TransportError("total_request_limit")
                self.budget.reserve(operation)
                self.request_bytes += len(body)
                deadline = min(self.deadline, time.monotonic() + 25)
                secret = bytes(self._key)
                with self.upstream.request(target, body, headers, secret.decode("ascii"), deadline) as (
                    response,
                    content_type,
                ):

                    def chunks():
                        count = 0
                        while True:
                            if time.monotonic() >= deadline:
                                raise TransportError("upstream_deadline")
                            chunk = response.read1(8192)
                            if not chunk:
                                return
                            count += len(chunk)
                            self.response_bytes += len(chunk)
                            if count > MAX_RESPONSE or self.response_bytes > MAX_TOTAL_RESPONSE:
                                raise TransportError("response_limit")
                            yield chunk

                    channel.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: "
                        + content_type.encode()
                        + b"\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n"
                    )
                    sent = True
                    for chunk in redacted_stream(chunks(), secret):
                        channel.sendall(chunk)
                self.completed += 1
        except Exception:
            self.failed += 1
            if not sent:
                error_response(channel)


@contextlib.contextmanager
def serve_broker(broker, bridge_gid):
    """Supervisor starts this dedicated UID before/alongside worker bootstrap."""
    path = broker.work / "broker/model.sock"
    fixed_work(broker.work)
    info = path.parent.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_gid != bridge_gid
        or stat.S_IMODE(info.st_mode) != 0o750
        or path.exists()
    ):
        raise TransportError("unix_directory_rejected")
    stop = threading.Event()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        os.chmod(path, 0o660)
        os.chown(path, -1, bridge_gid)
        listener.listen(4)
        listener.settimeout(0.2)

        def run():
            while not stop.is_set() and time.monotonic() < broker.deadline:
                try:
                    channel, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    return
                with channel:
                    broker.handle(channel)

        thread = threading.Thread(target=run, name="prism-provider-broker", daemon=True)
        thread.start()
        try:
            yield path
        finally:
            stop.set()
            listener.close()
            thread.join(timeout=26)
            broker.close()
            if thread.is_alive():
                raise TransportError("broker_cleanup_failed")
    finally:
        listener.close()
        if path.exists() and stat.S_ISSOCK(path.lstat().st_mode):
            path.unlink()


class LoopbackBridge:
    def __init__(self, work, binding, broker_uid, bridge_gid):
        self.work, self.binding = Path(work), dict(binding)
        self.broker_uid, self.bridge_gid = broker_uid, bridge_gid
        self.listener = None
        self.stop = threading.Event()
        self.thread = None
        self.deadline = time.monotonic() + 180
        self.unknown_header_names = set()

    def forward(self, channel, port):
        sent = False
        try:
            channel.settimeout(25)
            deadline = min(self.deadline, time.monotonic() + 25)
            method, target, headers, body = parse_http(SocketReader(channel, deadline))
            self.unknown_header_names.update(
                name for name in set(headers) - SDK_HEADERS if re.fullmatch("[a-z][a-z0-9-]{0,63}", name)
            )
            if (
                headers.get("host") != f"127.0.0.1:{port}"
                or target.partition("?")[0] not in ROUTES
                or set(headers) - SDK_HEADERS
            ):
                raise TransportError("loopback_request_rejected")
            path = self.work / "broker/model.sock"
            info = path.lstat()
            if (
                not stat.S_ISSOCK(info.st_mode)
                or info.st_uid != self.broker_uid
                or info.st_gid != self.bridge_gid
                or stat.S_IMODE(info.st_mode) != 0o660
            ):
                raise TransportError("unix_socket_rejected")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
                upstream.settimeout(25)
                upstream.connect(str(path))
                peer_identity(upstream, self.binding["broker_pid"], self.broker_uid)
                headers["host"] = "prism-model-broker"
                headers["connection"] = "close"
                envelope = (method + " " + target + " HTTP/1.1\r\n").encode()
                envelope += b"".join(
                    (name + ": " + value + "\r\n").encode() for name, value in headers.items()
                )
                with socket_deadline(upstream, deadline), socket_deadline(channel, deadline):
                    upstream.sendall(envelope + b"\r\n" + body)
                    total = 0
                    while True:
                        if time.monotonic() >= deadline:
                            raise TransportError("bridge_deadline")
                        chunk = upstream.recv(8192)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_RESPONSE + MAX_HEADERS:
                            raise TransportError("bridge_response_limit")
                        channel.sendall(chunk)
                        sent = True
        except Exception:
            if not sent:
                error_response(channel)

    def __enter__(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.listener.settimeout(0.2)
        port = self.listener.getsockname()[1]

        def run():
            while not self.stop.is_set() and time.monotonic() < self.deadline:
                try:
                    channel, address = self.listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    return
                with channel:
                    if address[0] == "127.0.0.1":
                        self.forward(channel, port)

        self.thread = threading.Thread(target=run, name="prism-private-loopback", daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{port}/anthropic"

    def __exit__(self, *_):
        self.stop.set()
        self.listener.close()
        self.thread.join(timeout=26)
        if self.thread.is_alive():
            raise TransportError("bridge_cleanup_failed")


def parse_bytes(raw):
    """Offline harness helper; the live path always parses actual socket bytes."""
    return parse_http(io.BytesIO(raw))
