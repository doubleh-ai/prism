"""Stdio protocol and fixed-loopback transport; no network is used."""

import io
import json
from urllib.error import HTTPError

import pytest

from server.capabilities_mcp import Bridge, BridgeUnavailable, NoRedirect, serve, validate_base_url


def initialized_bridge():
    bridge = Bridge("http://127.0.0.1:8088", "unit-test-run-credential")
    response = bridge.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
    )
    assert response["result"]["protocolVersion"] == "2025-06-18"
    assert bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    return bridge


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:80",
        "http://localhost:8088",
        "http://192.0.2.1:80",
        "http://127.0.0.1:80/path",
        "http://user:pass@127.0.0.1:80",
        "http://127.0.0.1:80?token=x",
        "http://127.0.0.1:80#x",
        "http://127.0.0.1",
        "file:///tmp/example",
        "http://127.0.0.1:bad",
    ],
)
def test_only_fixed_numeric_loopback_base_allowed(url):
    with pytest.raises(BridgeUnavailable):
        validate_base_url(url)


def test_ipv6_loopback_is_supported():
    assert validate_base_url("http://[::1]:8088/") == "http://[::1]:8088"


def test_protocol_list_call_and_stable_idempotency_key(monkeypatch):
    bridge = initialized_bridge()
    calls = []

    def request(operation, payload=None):
        calls.append((operation, payload))
        return (
            {"tools": [{"name": "market_quote", "inputSchema": {"type": "object"}}]}
            if operation == "tools"
            else {"ok": True, "data": {"last": 10}}
        )

    monkeypatch.setattr(bridge, "request", request)
    listing = bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert listing["result"]["tools"][0]["name"] == "market_quote"
    message = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "market_quote", "arguments": {"ticker": "600519"}},
    }
    for _ in range(2):
        result = bridge.handle(message)
        assert result["result"]["structuredContent"]["data"]["last"] == 10
        assert result["result"]["isError"] is False
    assert calls[1][1]["request_id"] == calls[2][1]["request_id"]
    assert "unit-test-run-credential" not in json.dumps(result)


def test_preinit_unknown_method_and_bad_arguments():
    bridge = Bridge("http://127.0.0.1:8088", "test-token")
    assert bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["error"]["code"] == -32002
    bridge = initialized_bridge()
    assert bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "files/read"})["error"]["code"] == -32601
    assert (
        bridge.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "market_quote", "arguments": []},
            }
        )["error"]["code"]
        == -32602
    )
    assert bridge.handle([{"unexpected": "batch"}])["error"]["code"] == -32600


def test_transport_headers_disable_proxies_and_do_not_echo_http_body(monkeypatch):
    bridge = initialized_bridge()
    captured = []

    def open_request(request, timeout):
        captured.append(request)
        assert timeout == 65
        raise HTTPError(
            request.full_url, 401, "private credential in error", {}, io.BytesIO(b"private error body")
        )

    monkeypatch.setattr(bridge._opener, "open", open_request)
    result = bridge.handle(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "market_quote", "arguments": {"ticker": "600519"}},
        }
    )
    assert captured[0].full_url == "http://127.0.0.1:8088/api/internal/capabilities/call"
    assert captured[0].get_header("Authorization") == "Bearer unit-test-run-credential"
    assert captured[0].get_header("X-requested-with") == "XMLHttpRequest"
    assert result["result"]["isError"] is True
    output = json.dumps(result)
    assert "private" not in output and "unit-test-run-credential" not in output
    with pytest.raises(BridgeUnavailable):
        NoRedirect().redirect_request(None, None, 302, "ignored", {}, "https://outside.invalid/")


def test_stdio_only_json_output_and_malformed_input(monkeypatch):
    bridge = initialized_bridge()
    monkeypatch.setattr(bridge, "request", lambda operation, payload=None: {"tools": []})
    source = io.BytesIO(
        b"not json\n"
        + json.dumps({"jsonrpc": "2.0", "id": "read", "method": "tools/list"}).encode()
        + b"\n"
        + b"x" * 70000
        + b"\n"
    )
    destination = io.StringIO()
    serve(bridge, source, destination)
    responses = [json.loads(line) for line in destination.getvalue().splitlines()]
    assert len(responses) == 3
    assert responses[0]["error"]["code"] == -32700
    assert responses[1]["result"] == {"tools": []}
    assert responses[2]["error"]["message"] == "Request too large"
