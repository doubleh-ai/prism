"""Offline fixtures plus opt-in native denial canaries; no real key or paid I/O."""

import ast
import contextlib
import hashlib
import io
import json
import os
import socket
import ssl
import stat
import struct
import sys
import types
from pathlib import Path

import pytest

from scripts import live_integration_policy as policy
from scripts import live_integration_transport as transport


@pytest.fixture(autouse=True)
def synthetic_approved_policy_source(monkeypatch):
    # Only this offline test module substitutes root-owned filesystem setup.
    monkeypatch.setattr(transport, "fixed_work", lambda work: Path(work))
    monkeypatch.setattr(transport, "load_policy", lambda path: policy)


@pytest.fixture
def clean_upstream_environment(monkeypatch):
    prohibited = {
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
    for name in tuple(transport.os.environ):
        if name.upper() in prohibited:
            monkeypatch.delenv(name, raising=False)


def binding():
    return {
        "candidate": "a" * 40,
        "cli_sha256": "b" * 64,
        "probe_sha256": "c" * 64,
        "nonce": "d" * 32,
        "worker_pid": 123,
        "broker_pid": 456,
    }


def envelope(*, target="/anthropic/v1/messages", method="POST", headers=None, body=None):
    body = transport.canonical(
        body
        if body is not None
        else {
            "model": policy.MODEL,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": "public synthetic question"}],
            "stream": True,
        }
    )
    values = {
        "Host": "prism-model-broker",
        "Content-Type": "application/json",
        "Authorization": "Bearer " + transport.synthetic_token(binding()),
        "Content-Length": str(len(body)),
        **(headers or {}),
    }
    return (
        (method + " " + target + " HTTP/1.1\r\n").encode()
        + b"".join((key + ": " + value + "\r\n").encode() for key, value in values.items())
        + b"\r\n"
        + body
    )


class Channel:
    def __init__(self, incoming=b"", *, peers=(123, 1001, 1003), chunk=8192):
        self.incoming, self.outgoing = io.BytesIO(incoming), bytearray()
        self.peers, self.chunk, self.connected, self.closed = peers, chunk, [], False
        self.timeouts = []

    def recv(self, maximum):
        return self.incoming.read(min(maximum, self.chunk))

    def sendall(self, value):
        self.outgoing.extend(value)

    def settimeout(self, value):
        self.timeouts.append(value)

    def getsockopt(self, *args):
        return struct.pack("3i", *self.peers)

    def connect(self, target):
        self.connected.append(target)

    def getpeername(self):
        return self.connected[-1]

    def shutdown(self, *_):
        self.closed = True

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


@pytest.mark.parametrize("chunk", [1, 2, 7, 8192])
def test_actual_http_bytes_survive_arbitrary_socket_fragmentation(chunk):
    channel = Channel(envelope(), chunk=chunk)
    parsed = transport.parse_http(transport.SocketReader(channel, transport.time.monotonic() + 2))
    target, operation, raw, headers = transport.provider_request(
        parsed, transport.synthetic_token(binding()), policy
    )
    assert target == "/anthropic/v1/messages" and operation == "messages"
    assert json.loads(raw)["max_tokens"] == 1024
    assert "authorization" not in headers and "Authorization" not in headers
    assert headers["Accept-Encoding"] == "identity"


@pytest.mark.parametrize(
    "raw",
    [
        b"CONNECT api.deepseek.com:443 HTTP/1.1\r\nHost: prism-model-broker\r\n\r\n",
        b"POST https://api.deepseek.com/anthropic/v1/messages HTTP/1.1\r\nHost: prism-model-broker\r\nContent-Length: 0\r\n\r\n",
        b"POST / HTTP/1.0\r\nHost: prism-model-broker\r\nContent-Length: 0\r\n\r\n",
        b"POST / HTTP/1.1\nHost: prism-model-broker\n\n",
        b"POST / HTTP/1.1\r\nHost: a\r\nhost: b\r\nContent-Length: 0\r\n\r\n",
        b"POST / HTTP/1.1\r\nHost: a\r\nContent-Length: 0\r\nContent-Length: 1\r\n\r\n",
        b"POST / HTTP/1.1\r\nHost: a\r\nTransfer-Encoding: chunked\r\nContent-Length: 0\r\n\r\n",
        b"POST / HTTP/1.1\r\nHost: a\r\n Content-Length: 0\r\n\r\n",
        b"POST / HTTP/1.1\r\nHost: a\r\nContent-Length: +1\r\n\r\nx",
        b"POST / HTTP/1.1\r\nHost: a\r\nContent-Length: 01\r\n\r\nx",
        b"POST / HTTP/1.1\r\nHost: a\r\nContent-Length: 9\r\n\r\nx",
        b"POST / HTTP/1.1\r\nHost: a\r\nExpect: 100-continue\r\nContent-Length: 0\r\n\r\n",
        b"POST / HTTP/1.1\r\nHost: a\r\nContent-Length: 999999\r\n\r\n",
        b"POST / HTTP/1.1\r\nHost: a\r\nCookie: value\x00hidden\r\nContent-Length: 0\r\n\r\n",
    ],
)
def test_smuggling_ambiguous_or_unsupported_requests_are_rejected(raw):
    with pytest.raises(transport.TransportError):
        transport.parse_bytes(raw)


@pytest.mark.parametrize(
    "headers",
    [
        {"Cookie": "private"},
        {"X-Forwarded-For": "127.0.0.1"},
        {"Authorization": "Bearer different"},
        {"x-api-key": "other"},
        {"Content-Type": "text/plain"},
        {"Host": "api.deepseek.com"},
        {"X-Stainless-Retry-Count": "1"},
    ],
)
def test_provider_headers_cannot_carry_extra_credentials_or_routes(headers):
    with pytest.raises(transport.TransportError):
        transport.provider_request(
            transport.parse_bytes(envelope(headers=headers)), transport.synthetic_token(binding()), policy
        )


@pytest.mark.parametrize(
    "target",
    [
        "/other",
        "/anthropic/v1/messages?next=other",
        "/anthropic/v1/messages/../other",
        "/anthropic/v1/messages?beta=true&x=y",
    ],
)
def test_upstream_paths_and_query_are_fixed(target):
    with pytest.raises(transport.TransportError):
        transport.provider_request(
            transport.parse_bytes(envelope(target=target)), transport.synthetic_token(binding()), policy
        )


def test_count_tokens_has_its_real_schema_without_inventing_output_tokens():
    body = {"model": policy.MODEL, "messages": [{"role": "user", "content": "synthetic"}]}
    target, operation, raw, _ = transport.provider_request(
        transport.parse_bytes(envelope(target="/anthropic/v1/messages/count_tokens", body=body)),
        transport.synthetic_token(binding()),
        policy,
    )
    assert target.endswith("count_tokens") and operation == "count_tokens"
    assert json.loads(raw) == body and "max_tokens" not in json.loads(raw)


@pytest.mark.parametrize("name", ["WebFetch", "Bash", "mcp__prism__analysis_start", "mcp__other__anything"])
def test_provider_body_cannot_reintroduce_forbidden_tools(name):
    body = {"model": policy.MODEL, "max_tokens": 10, "tools": [{"name": name}]}
    with pytest.raises(transport.TransportError, match="provider_tools_rejected"):
        transport.provider_request(
            transport.parse_bytes(envelope(body=body)), transport.synthetic_token(binding()), policy
        )


@pytest.mark.parametrize(
    "raw",
    [b'{"model":"a","model":"b"}', b'{"temperature":NaN}', b'{"temperature":Infinity}', b"\xff", b"null bad"],
)
def test_json_duplicates_and_nonfinite_values_are_rejected(raw):
    with pytest.raises(transport.TransportError, match="json_rejected"):
        transport.strict_json(raw)


def test_socket_total_deadline_expires_even_before_the_first_read():
    with pytest.raises(transport.TransportError, match="http_deadline"):
        transport.parse_http(transport.SocketReader(Channel(envelope()), 0))


def test_key_is_removed_at_every_possible_chunk_boundary():
    key = b"SYNTHETIC_TEST_KEY_123456789"
    original = b"data: before " + key + b" after " + key + b"\n\n"
    for position in range(len(original) + 1):
        value = b"".join(transport.redacted_stream([original[:position], original[position:]], key))
        assert key not in value and value == original.replace(key, b"[redacted]")
    assert b"".join(transport.redacted_stream([bytes([byte]) for byte in original], key)) == original.replace(
        key, b"[redacted]"
    )


class UpstreamFixture:
    def __init__(self, chunks=None, fail=False):
        self.chunks = chunks or [
            b'event: message_start\ndata: {"type":"message_start"}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]
        self.fail = fail
        self.requests = []

    @contextlib.contextmanager
    def request(self, target, body, headers, key, deadline):
        self.requests.append((target, json.loads(body), headers, key, deadline))
        if self.fail:
            raise OSError("synthetic upstream rejection; not a real request")
        parts = iter(self.chunks)
        yield types.SimpleNamespace(read1=lambda _: next(parts, b"")), "text/event-stream"


@pytest.fixture
def broker(monkeypatch, tmp_path):
    value = transport.Broker(tmp_path, binding())
    value._key = bytearray(b"SYNTHETIC_BROKER_TEST_KEY")
    value.authorized = True
    value.ready.set()
    value.upstream = UpstreamFixture()
    monkeypatch.setattr(transport, "peer_identity", lambda channel, pid, uid: None)
    monkeypatch.setattr(transport.pwd, "getpwnam", lambda _: types.SimpleNamespace(pw_uid=1001))
    yield value
    value.close()


def test_sse_is_forwarded_and_authorization_replaced_only_at_the_upstream(broker):
    key = bytes(broker._key)
    broker.upstream = UpstreamFixture([b"data: ", key[:7], key[7:], b" end\n\n"])
    channel = Channel(envelope(), chunk=3)
    broker.handle(channel)
    assert channel.outgoing.startswith(b"HTTP/1.1 200 OK") and b"[redacted]" in channel.outgoing
    assert key not in channel.outgoing
    assert broker.upstream.requests[0][3] == key.decode()
    assert broker.upstream.requests[0][1]["max_tokens"] == 1024
    assert broker.summary()["requests"]["messages"] == 1
    assert broker.summary()["native_fetch_verified"] is False


def test_failed_requests_consume_a_slot_without_retry(broker):
    broker.upstream = UpstreamFixture(fail=True)
    channel = Channel(envelope())
    broker.handle(channel)
    assert len(broker.upstream.requests) == 1 and broker.budget.counts["messages"] == 1
    assert channel.outgoing.startswith(b"HTTP/1.1 502") and b"synthetic upstream" not in channel.outgoing


def test_seventh_model_request_never_reaches_provider(broker):
    for _ in range(7):
        broker.handle(Channel(envelope()))
    assert len(broker.upstream.requests) == 6
    assert broker.completed == 6 and broker.failed == 1


def test_wrong_peer_and_bad_request_are_blocked_before_provider(broker, monkeypatch):
    def reject(*args):
        raise transport.TransportError("unix_peer_rejected")

    monkeypatch.setattr(transport, "peer_identity", reject)
    broker.handle(Channel(envelope()))
    assert not broker.upstream.requests
    assert broker.budget.counts["messages"] == 0


def test_total_and_per_response_limits_are_enforced(broker, monkeypatch):
    monkeypatch.setattr(transport, "MAX_RESPONSE", 8)
    broker.upstream = UpstreamFixture([b"a" * 9])
    channel = Channel(envelope())
    broker.handle(channel)
    assert broker.failed == 1 and broker.completed == 0
    assert channel.outgoing.count(b"HTTP/1.1") == 1
    monkeypatch.setattr(transport, "MAX_TOTAL_REQUEST", 1)
    broker.handle(Channel(envelope()))
    assert len(broker.upstream.requests) == 1


def test_authorize_acquires_key_only_after_kernel_and_source_permit(monkeypatch, tmp_path):
    order = []
    value = transport.Broker(tmp_path, binding())
    monkeypatch.setattr(transport, "fixed_work", lambda _: tmp_path)
    monkeypatch.setattr(transport.os, "getpid", lambda: 456)
    monkeypatch.setattr(transport.os, "getuid", lambda: 1002)
    monkeypatch.setattr(
        transport.pwd, "getpwnam", lambda _: types.SimpleNamespace(pw_uid=1002, pw_shell="/usr/sbin/nologin")
    )
    monkeypatch.setattr(transport.os, "environ", {"PATH": "/usr/bin:/bin"})
    value.upstream = types.SimpleNamespace(
        preflight=lambda _: order.append("tls") or {"public_dns_pinned": True, "upstream_tls_verified": True}
    )
    monkeypatch.setattr(transport, "verify_allow", lambda *args: order.append("permit") or {})
    monkeypatch.setattr(transport, "self_identity", lambda *args: order.append("kernel"))

    def gate(identity, facts):
        assert identity == binding() and facts["upstream_tls_verified"] is True
        order.append("root-gatekeeper")

    value.authorize(gatekeeper=gate, key_provider=lambda: order.append("key") or "SYNTHETIC_ONLY_KEY_1234")
    assert order == ["tls", "root-gatekeeper", "permit", "kernel", "key", "permit", "kernel"]
    assert value.authorized
    memory = value._key
    value.close()
    assert memory == b"\0" * len(memory) and value._key is None
    with pytest.raises(transport.TransportError, match="broker_authorization_rejected"):
        value.authorize(gatekeeper=gate, key_provider=lambda: pytest.fail("one authorization attempt only"))


def test_failed_permit_cannot_call_key_provider(monkeypatch, tmp_path):
    value = transport.Broker(tmp_path, binding())
    monkeypatch.setattr(transport, "fixed_work", lambda _: tmp_path)
    monkeypatch.setattr(transport.os, "getpid", lambda: 456)
    monkeypatch.setattr(transport.os, "getuid", lambda: 1002)
    monkeypatch.setattr(
        transport.pwd, "getpwnam", lambda _: types.SimpleNamespace(pw_uid=1002, pw_shell="/usr/sbin/nologin")
    )
    monkeypatch.setattr(transport.os, "environ", {})
    value.upstream = types.SimpleNamespace(preflight=lambda _: {})

    def blocked(*args):
        raise transport.TransportError("root_allow_rejected")

    monkeypatch.setattr(transport, "verify_allow", blocked)
    with pytest.raises(transport.TransportError, match="root_allow_rejected"):
        value.authorize(
            gatekeeper=lambda *args: None, key_provider=lambda: pytest.fail("must not acquire a key")
        )


def test_immutable_permit_requires_all_pid_uid_namespace_source_and_canary_bindings(monkeypatch, tmp_path):
    files = {
        "worker": b"public synthetic worker",
        "transport": b"public synthetic transport",
        "policy": Path(policy.__file__).read_bytes(),
    }
    for name, raw in files.items():
        (tmp_path / (name + ".py")).write_bytes(raw)
    identity = {**binding(), "probe_sha256": hashlib.sha256(files["worker"]).hexdigest()}
    kernel = {
        "worker_uids": [1001] * 4,
        "broker_uids": [1002] * 4,
        "worker_netns_inode": 101,
        "broker_netns_inode": 102,
        "host_netns_inode": 102,
        "worker_startticks": 201,
        "broker_startticks": 202,
    }
    allow = {
        "schema": 1,
        "binding": identity,
        "policy_sha256": transport.POLICY_SHA256,
        "bootstrap_proof_sha256": hashlib.sha256(b"proof").hexdigest(),
        "allowed_at": 1000,
        "sources": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
        "broker_memory_sealed": True,
        "kernel": kernel,
        "witness": {
            "binding": identity,
            "checked_at": 999,
            "checks": dict.fromkeys(policy.REQUIRED_WITNESSES, True),
        },
    }
    monkeypatch.setattr(transport, "__file__", str(tmp_path / "transport.py"))
    monkeypatch.setattr(transport, "locked", lambda _: None)
    monkeypatch.setattr(transport, "read_record", lambda _: (allow, b""))
    monkeypatch.setattr(
        transport.pwd,
        "getpwnam",
        lambda name: types.SimpleNamespace(pw_uid=1001 if name == "prism-integration" else 1002),
    )
    assert transport.verify_allow(tmp_path, identity, policy, proof_raw=b"proof", now=lambda: 1001) == kernel
    allow["broker_memory_sealed"] = False
    with pytest.raises(transport.TransportError, match="root_allow_rejected"):
        transport.verify_allow(tmp_path, identity, policy, now=lambda: 1001)
    allow["broker_memory_sealed"] = True
    allow["witness"]["checks"]["metadata_denied"] = 1
    with pytest.raises(transport.TransportError):
        transport.verify_allow(tmp_path, identity, policy, now=lambda: 1001)
    allow["witness"]["checks"]["metadata_denied"] = True
    kernel["worker_netns_inode"] = 102
    with pytest.raises(transport.TransportError, match="kernel_identity_rejected"):
        transport.verify_allow(tmp_path, identity, policy, now=lambda: 1001)
    kernel["worker_netns_inode"] = 101
    (tmp_path / "worker.py").write_bytes(b"changed source")
    with pytest.raises(transport.TransportError, match="approved_source_required"):
        transport.verify_allow(tmp_path, identity, policy, now=lambda: 1001)


def test_dns_pin_and_tls_sni_are_actual_connection_inputs(monkeypatch, clean_upstream_environment):
    # The connection inputs are inspected through fake DNS/socket/TLS boundaries.
    raw = Channel()
    used = []
    context = types.SimpleNamespace(
        verify_mode=ssl.CERT_REQUIRED,
        check_hostname=True,
        wrap_socket=lambda channel, server_hostname: used.append(server_hostname) or channel,
    )
    monkeypatch.setattr(
        transport.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )
    monkeypatch.setattr(transport.socket, "socket", lambda *args: raw)
    monkeypatch.setattr(transport.ssl, "create_default_context", lambda: context)
    upstream = transport.PinnedUpstream(policy)
    secured = upstream.connect(transport.time.monotonic() + 2)
    assert secured is raw and raw.connected == [("8.8.8.8", 443)] and used == ["api.deepseek.com"]
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert upstream.phase == "connected"
    assert upstream.resolved_peers == ("8.8.8.8",)
    assert upstream.selected_peer == "8.8.8.8"


def test_mixed_private_dns_answer_is_rejected_before_dial(monkeypatch, clean_upstream_environment):
    monkeypatch.setattr(
        transport.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", (address, 443)) for address in ["8.8.8.8", "169.254.169.254"]],
    )
    monkeypatch.setattr(
        transport.socket, "socket", lambda *args: pytest.fail("private/mixed DNS must not dial")
    )
    upstream = transport.PinnedUpstream(policy)
    with pytest.raises(transport.TransportError, match="upstream_dns_rejected") as caught:
        upstream.connect(transport.time.monotonic() + 2)
    assert caught.value.code == "upstream_dns_rejected"
    assert upstream.phase == "dns_validate"
    assert upstream.resolved_peers == () and upstream.selected_peer is None


def test_transport_codes_cover_only_fixed_source_categories():
    tree = ast.parse(Path(transport.__file__).read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "TransportError"
    ]
    assert calls and all(len(node.args) == 1 and isinstance(node.args[0], ast.Constant) for node in calls)
    codes = {node.args[0].value for node in calls}
    assert transport.TRANSPORT_ERROR_CODES == codes | {"transport_error"}
    assert len(transport.TRANSPORT_ERROR_CODES) == 43
    for code in transport.TRANSPORT_ERROR_CODES:
        error = transport.TransportError(code)
        assert error.code == code and error.args == (code,)
        with pytest.raises(AttributeError):
            error.code = "unknown"


@pytest.mark.parametrize("value", ["private-unrecognized-text", None, 4, [], {"private": "text"}])
def test_unknown_transport_error_does_not_retain_input(value):
    error = transport.TransportError(value)
    assert error.code == "transport_error"
    assert error.args == ("transport_error",)
    assert error.__dict__ == {"_code": "transport_error"}


def test_upstream_diagnostics_are_initially_empty_and_read_only():
    upstream = transport.PinnedUpstream(policy)
    assert upstream.phase == "not_started"
    assert upstream.resolved_peers == () and upstream.selected_peer is None
    assert transport.UPSTREAM_PHASES == {
        "not_started",
        "environment",
        "dns_resolve",
        "dns_validate",
        "tcp_socket",
        "tcp_connect",
        "tls_context",
        "tls_handshake",
        "peer",
        "connected",
    }
    for name, value in [("phase", "connected"), ("resolved_peers", ()), ("selected_peer", "x")]:
        with pytest.raises(AttributeError):
            setattr(upstream, name, value)


@pytest.mark.parametrize(
    "failed_phase",
    ["dns_resolve", "tcp_socket", "tcp_connect", "tls_context", "tls_handshake", "peer"],
)
def test_fake_boundaries_keep_native_error_and_phase_without_retry(
    monkeypatch, clean_upstream_environment, failed_phase
):
    """Offline failures prove attribution only, not real DNS/TCP/TLS success."""
    upstream = transport.PinnedUpstream(policy)
    failure = PermissionError(13, "synthetic local refusal")
    calls = []
    raw = Channel()

    def step(phase, value):
        calls.append(phase)
        assert upstream.phase == phase
        if phase == failed_phase:
            raise failure
        return value

    monkeypatch.setattr(
        transport.socket,
        "getaddrinfo",
        lambda *a, **kw: step("dns_resolve", [(2, 1, 6, "", ("1.1.1.1", 443))]),
    )
    monkeypatch.setattr(transport.socket, "socket", lambda *a: step("tcp_socket", raw))
    raw.connect = lambda target: step("tcp_connect", raw.connected.append(target))
    raw.getpeername = lambda: step("peer", ("1.1.1.1", 443))
    context = types.SimpleNamespace(
        verify_mode=ssl.CERT_REQUIRED,
        check_hostname=True,
        wrap_socket=lambda *a, **kw: step("tls_handshake", raw),
    )
    monkeypatch.setattr(transport.ssl, "create_default_context", lambda: step("tls_context", context))
    with pytest.raises(PermissionError) as caught:
        upstream.connect(transport.time.monotonic() + 2)
    assert caught.value is failure and caught.value.errno == 13
    assert upstream.phase == failed_phase and calls.count(failed_phase) == 1
    assert upstream.resolved_peers == (() if failed_phase == "dns_resolve" else ("1.1.1.1",))
    assert upstream.selected_peer == (None if failed_phase == "dns_resolve" else "1.1.1.1")
    if failed_phase not in {"dns_resolve", "tcp_socket"}:
        assert raw.closed


def test_dns_facts_are_original_single_lookup_and_reset_on_new_attempt(
    monkeypatch, clean_upstream_environment
):
    upstream = transport.PinnedUpstream(policy)
    answers = ["2606:4700:4700::1111", "8.8.8.8", "1.1.1.1", "8.8.8.8"]
    dns_calls = []
    peer_calls = []

    def resolve(*args, **kwargs):
        dns_calls.append((args, kwargs))
        return [(2, 1, 6, "", (peer, 443)) for peer in answers]

    def validate(host, peers):
        peer_calls.append((host, peers))
        return policy.public_peers(host, peers)

    upstream.policy = types.SimpleNamespace(public_peers=validate, PolicyError=policy.PolicyError)
    monkeypatch.setattr(transport.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(transport.socket, "socket", lambda *a: Channel())
    context = types.SimpleNamespace(
        verify_mode=ssl.CERT_REQUIRED,
        check_hostname=True,
        wrap_socket=lambda channel, **kw: channel,
    )
    monkeypatch.setattr(transport.ssl, "create_default_context", lambda: context)
    upstream.connect(transport.time.monotonic() + 2)
    assert len(dns_calls) == len(peer_calls) == 1
    assert peer_calls == [(transport.HOST, answers)]
    assert upstream.resolved_peers == ("1.1.1.1", "2606:4700:4700::1111", "8.8.8.8")
    assert upstream.selected_peer == "1.1.1.1"
    monkeypatch.setenv("HTTPS_PROXY", "public-synthetic-proxy")
    with pytest.raises(transport.TransportError) as caught:
        upstream.connect(transport.time.monotonic() + 2)
    assert caught.value.code == "upstream_environment_rejected"
    assert len(dns_calls) == len(peer_calls) == 1
    assert upstream.phase == "environment"
    assert upstream.resolved_peers == () and upstream.selected_peer is None


@pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("PRISM_TEST_KERNEL_DENIALS") != "1",
    reason="requires reviewed Mac sandbox runner; flag alone does not prove denial",
)
def test_native_denied_socket_records_actual_boundary(monkeypatch, clean_upstream_environment):
    """Only DNS is a fixture; the socket syscall must be denied by the kernel."""
    upstream = transport.PinnedUpstream(policy)
    native_socket = socket.socket
    attempted = []

    class NativeSocket(native_socket):
        def __init__(self, *args):
            attempted.append(("tcp_socket", upstream.phase))
            super().__init__(*args)

        def connect(self, address):
            attempted.append(("tcp_connect", upstream.phase))
            return super().connect(address)

    monkeypatch.setattr(transport.socket, "getaddrinfo", lambda *a, **kw: [(2, 1, 6, "", ("1.1.1.1", 443))])
    monkeypatch.setattr(transport.socket, "socket", NativeSocket)
    monkeypatch.setattr(
        transport.ssl, "create_default_context", lambda: pytest.fail("kernel must deny before TLS")
    )
    with pytest.raises(PermissionError) as caught:
        upstream.connect(transport.time.monotonic() + 2)
    assert caught.value.errno in {1, 13}
    assert attempted[-1] == (upstream.phase, upstream.phase)
    assert upstream.phase in {"tcp_socket", "tcp_connect"}
    assert len(attempted) <= 2 and len(set(attempted)) == len(attempted)
    assert upstream.resolved_peers == ("1.1.1.1",) and upstream.selected_peer == "1.1.1.1"


@pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("PRISM_TEST_KERNEL_DENIALS") != "1",
    reason="requires reviewed public-file denial fixture, not credential access",
)
def test_native_denied_public_file_records_tls_context_phase(monkeypatch, clean_upstream_environment):
    """Earlier stages are fake; actual file denial proves attribution, not TLS."""
    path = Path(os.environ["PRISM_TEST_DENIED_PUBLIC_FILE"])
    assert path.is_absolute() and path.name == "denied-public-fixture.txt"
    upstream = transport.PinnedUpstream(policy)
    raw = Channel()
    attempted = []

    def denied_context():
        attempted.append(upstream.phase)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        os.close(descriptor)
        pytest.fail("reviewed sandbox must actually deny the public fixture")

    monkeypatch.setattr(transport.socket, "getaddrinfo", lambda *a, **kw: [(2, 1, 6, "", ("1.1.1.1", 443))])
    monkeypatch.setattr(transport.socket, "socket", lambda *a: raw)
    monkeypatch.setattr(transport.ssl, "create_default_context", denied_context)
    with pytest.raises(PermissionError) as caught:
        upstream.connect(transport.time.monotonic() + 2)
    assert caught.value.errno in {1, 13}
    assert upstream.phase == "tls_context" and attempted == ["tls_context"]
    assert raw.closed


@pytest.mark.parametrize(
    "name",
    [
        "HTTPS_PROXY",
        "https_proxy",
        "http_proxy",
        "ALL_PROXY",
        "SSL_CERT_FILE",
        "ssl_cert_dir",
        "SSLKEYLOGFILE",
        "OPENSSL_CONF",
        "OPENSSL_MODULES",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
    ],
)
def test_dirty_upstream_environment_is_rejected_before_dns_or_dial(
    monkeypatch, clean_upstream_environment, name
):
    monkeypatch.setenv(name, "public-synthetic-inherited-value")
    monkeypatch.setattr(
        transport.socket,
        "getaddrinfo",
        lambda *args, **kwargs: pytest.fail("dirty environment must be rejected before DNS"),
    )
    monkeypatch.setattr(
        transport.socket, "socket", lambda *args, **kwargs: pytest.fail("dirty environment must never dial")
    )
    with pytest.raises(transport.TransportError, match="upstream_environment_rejected"):
        transport.PinnedUpstream(policy).connect(transport.time.monotonic() + 2)


@pytest.mark.parametrize(
    "status,encoding",
    [(301, "identity"), (302, "identity"), (307, "identity"), (401, "identity"), (200, "gzip")],
)
def test_upstream_redirects_and_encoded_responses_are_never_forwarded(monkeypatch, status, encoding):
    calls = []
    response = types.SimpleNamespace(
        status=status,
        getheader=lambda name, default="": "text/event-stream" if name == "Content-Type" else encoding,
    )
    connection = types.SimpleNamespace(
        sock=None,
        request=lambda *args, **kwargs: calls.append(args),
        getresponse=lambda: response,
        close=lambda: None,
    )
    monkeypatch.setattr(transport.http.client, "HTTPConnection", lambda *args, **kwargs: connection)
    upstream = transport.PinnedUpstream(policy)
    monkeypatch.setattr(upstream, "connect", lambda _: Channel())
    with (
        pytest.raises(transport.TransportError, match="upstream_response_rejected"),
        upstream.request(
            "/anthropic/v1/messages", b"{}", {}, "SYNTHETIC_TEST_KEY", transport.time.monotonic() + 2
        ),
    ):
        pytest.fail("no rejected response may escape")
    assert len(calls) == 1


def test_loopback_bridge_uses_only_locked_uds_and_checks_broker_peer(monkeypatch, tmp_path):
    identity = binding()
    upstream = Channel(
        b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\ndata: synthetic\n\n", peers=(456, 1002, 1003)
    )
    seen = []
    monkeypatch.setattr(transport.socket, "socket", lambda *args: upstream)
    monkeypatch.setattr(transport, "peer_identity", lambda channel, pid, uid: seen.append((pid, uid)))
    monkeypatch.setattr(transport, "socket_deadline", lambda *args: contextlib.nullcontext())
    original = Path.lstat

    def info(path):
        if path == tmp_path / "broker/model.sock":
            return types.SimpleNamespace(st_mode=stat.S_IFSOCK | 0o660, st_uid=1002, st_gid=1003)
        return original(path)

    monkeypatch.setattr(Path, "lstat", info)
    local = Channel(envelope(headers={"Host": "127.0.0.1:4321"}))
    bridge = transport.LoopbackBridge(tmp_path, identity, 1002, 1003)
    bridge.forward(local, 4321)
    assert seen == [(456, 1002)] and upstream.connected == [str(tmp_path / "broker/model.sock")]
    assert b"host: prism-model-broker\r\n" in upstream.outgoing
    assert b"api.deepseek.com:443" not in upstream.outgoing
    assert local.outgoing.endswith(b"data: synthetic\n\n")


def test_peer_credentials_are_kernel_bytes_not_a_header(monkeypatch):
    monkeypatch.setattr(transport.sys, "platform", "linux")
    monkeypatch.setattr(transport.socket, "SO_PEERCRED", 17, raising=False)
    transport.peer_identity(Channel(peers=(123, 1001, 1003)), 123, 1001)
    with pytest.raises(transport.TransportError, match="unix_peer_rejected"):
        transport.peer_identity(Channel(peers=(124, 1001, 1003)), 123, 1001)


def test_unknown_header_diagnostic_retains_only_names_not_values(broker, tmp_path):
    value = "SYNTHETIC_VALUE_MUST_NOT_BE_RECORDED"
    channel = Channel(envelope(headers={"X-Synthetic-New-Sdk-Header": value}))
    broker.handle(channel)
    assert broker.summary()["unknown_header_names"] == ["x-synthetic-new-sdk-header"]
    assert value not in json.dumps(broker.summary()) and value.encode() not in channel.outgoing
    bridge = transport.LoopbackBridge(tmp_path, binding(), 1002, 1003)
    local = Channel(envelope(headers={"Host": "127.0.0.1:4321", "X-Synthetic-New-Sdk-Header": value}))
    bridge.forward(local, 4321)
    assert bridge.unknown_header_names == {"x-synthetic-new-sdk-header"}
    assert value.encode() not in local.outgoing


@pytest.mark.parametrize(
    "name", ["SSLKEYLOGFILE", "SSL_CERT_FILE", "OPENSSL_CONF", "https_proxy", "LD_PRELOAD"]
)
def test_tls_overrides_and_keylogging_environment_are_rejected_before_dns(monkeypatch, name):
    monkeypatch.setenv(name, "/synthetic/forbidden")
    monkeypatch.setattr(
        transport.socket,
        "getaddrinfo",
        lambda *args, **kwargs: pytest.fail("no DNS before clean TLS environment"),
    )
    with pytest.raises(transport.TransportError, match="upstream_environment_rejected"):
        transport.PinnedUpstream(policy).connect(transport.time.monotonic() + 2)
