"""Offline byte/protocol fixtures. No real key, kernel attestation or paid I/O."""

import contextlib
import hashlib
import io
import json
import socket
import ssl
import stat
import struct
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


def test_dns_pin_and_tls_sni_are_actual_connection_inputs(monkeypatch):
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
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    secured = transport.PinnedUpstream(policy).connect(transport.time.monotonic() + 2)
    assert secured is raw and raw.connected == [("8.8.8.8", 443)] and used == ["api.deepseek.com"]
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2


def test_mixed_private_dns_answer_is_rejected_before_dial(monkeypatch):
    monkeypatch.setattr(
        transport.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", (address, 443)) for address in ["8.8.8.8", "169.254.169.254"]],
    )
    monkeypatch.setattr(
        transport.socket, "socket", lambda *args: pytest.fail("private/mixed DNS must not dial")
    )
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(transport.TransportError, match="upstream_dns_rejected"):
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
