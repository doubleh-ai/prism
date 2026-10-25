"""Synthetic DNS/socket/TLS fixtures only; no external request is performed."""

import io
import socket
import ssl

import pytest

from server.agent import sources


def wire(body=b"public text", *, status=200, content_type="text/html; charset=utf-8", headers=()):
    values = [("Content-Type", content_type), ("Content-Length", str(len(body))), *headers]
    return (
        f"HTTP/1.1 {status} Synthetic\r\n".encode()
        + b"".join(f"{key}: {value}\r\n".encode() for key, value in values)
        + b"\r\n"
        + body
    )


@pytest.fixture
def network(monkeypatch):
    """Exercise real HTTPResponse parsing while replacing all network boundaries."""
    state = {
        "wires": [wire()],
        "dns_calls": [],
        "connects": [],
        "sni": [],
        "requests": [],
        "closed": 0,
        "peer": "8.8.8.8",
        "tls_peer": "8.8.8.8",
        "rows": None,
        "sockets": [],
        "recv_hook": None,
    }

    def dns(host, port, **kwargs):
        state["dns_calls"].append(host)
        return state["rows"] or [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", port))
        ]

    class Raw:
        def __init__(self, *args):
            self.timeouts = []
            self.closed = False
            state["sockets"].append(self)

        def settimeout(self, value):
            self.timeouts.append(value)

        def connect(self, address):
            state["connects"].append(address)

        def getpeername(self):
            return state["peer"], 443

        def close(self):
            self.closed = True

    class TLS:
        def __init__(self, raw):
            self.raw = raw
            self.bytes = io.BytesIO(state["wires"].pop(0))

        def settimeout(self, value):
            self.raw.settimeout(value)

        def getpeername(self):
            return state["tls_peer"], 443

        def sendall(self, request):
            state["requests"].append(request)

        def recv_into(self, buffer):
            if state["recv_hook"]:
                state["recv_hook"]()
            return self.bytes.readinto(buffer)

        def close(self):
            state["closed"] += 1
            self.raw.close()

    class Context:
        check_hostname = True
        verify_mode = ssl.CERT_REQUIRED

        def wrap_socket(self, raw, *, server_hostname):
            state["sni"].append(server_hostname)
            if state.get("tls_failure"):
                raise ssl.SSLCertVerificationError("synthetic cert failure, never print credential")
            return TLS(raw)

    state["context"] = Context()
    monkeypatch.setattr(sources.socket, "getaddrinfo", dns)
    monkeypatch.setattr(sources.socket, "socket", Raw)
    monkeypatch.setattr(sources.ssl, "create_default_context", lambda: state["context"])
    monkeypatch.delenv("PRISM_RESEARCH_SOURCE_HOSTS", raising=False)
    return state


def test_actual_parser_preserves_real_excerpt_dates_and_unreviewed_scope(network):
    network["wires"] = [
        wire(b"""<html><head><title>Disclosure &amp; results</title>
      <meta property="article:published_time" content="2026-10-20T09:30:00+08:00"></head>
      <nav>hidden menu</nav><script>privateScript()</script><style>hiddenStyle</style>
      <main><h1>Results</h1><p>Revenue <b>120</b> million.</p></main></html>""")
    ]
    result = sources.fetch_public_source("https://www.sse.com.cn/disclosure#section")
    assert result["status"] == "ok" and result["title"] == "Disclosure & results"
    assert "Revenue 120 million." in result["text"]
    assert all(hidden not in result["text"] for hidden in ("hidden menu", "privateScript", "hiddenStyle"))
    assert result["published_at"] == "2026-10-20T09:30:00+08:00"
    assert result["original_obtained"] is True and result["original_preserved"] is False
    assert result["content_scope"] == "server_extracted_excerpt" and result["semantic_status"] == "unreviewed"
    assert result["trust_level"] == "untrusted_external_content"
    assert network["connects"] == [("8.8.8.8", 443)] and network["sni"] == ["www.sse.com.cn"]
    request = network["requests"][0]
    assert request.startswith(b"GET /disclosure HTTP/1.1\r\nHost: www.sse.com.cn\r\n")
    assert b"#section" not in request and b"Authorization" not in request and b"Cookie" not in request
    assert network["closed"] == 1 and network["sockets"][0].closed


@pytest.mark.parametrize(
    "url",
    [
        "http://www.sse.com.cn/",
        "https://www.sse.com.cn:444/",
        "https://user:pass@www.sse.com.cn/",
        "https://www.sse.com.cn/?api_key=synthetic",
        "https://www.sse.com.cn/?%61ccess_token=synthetic",
        "https://www.sse.com.cn/#password=synthetic",
        "https://127.0.0.1/",
        "https://localhost/",
        "https://sse.com.cn.attacker.example/",
        "https://arbitrary.example/",
        "file:///etc/passwd",
        "https://www.sse.com.cn/%0d%0aHost:evil",
        "https://www.sse.com.cn\\@evil.example/",
    ],
)
def test_disallowed_urls_make_no_network_request(url, network):
    result = sources.fetch_public_source(url)
    assert result["status"] in {"invalid_url", "host_not_allowed"}
    assert result["url"] is None and not result["original_obtained"]
    assert network["dns_calls"] == network["connects"] == network["requests"] == []


@pytest.mark.parametrize(
    "ip", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "192.168.1.1", "100.64.0.1", "224.0.0.1"]
)
def test_any_private_or_special_dns_answer_rejects_the_whole_answer_set(ip, network):
    network["rows"] = [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443)),
    ]
    result = sources.fetch_public_source("https://www.sse.com.cn/")
    assert result["status"] == "unsafe_dns" and network["connects"] == []


@pytest.mark.parametrize("ip", ["::1", "fe80::1", "fc00::1", "::ffff:8.8.8.8"])
def test_private_and_ipv4_mapped_ipv6_answers_are_rejected(ip, network):
    network["rows"] = [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443, 0, 0))]
    assert sources.fetch_public_source("https://www.sse.com.cn/")["status"] == "unsafe_dns"
    assert network["connects"] == []


def test_public_ipv6_is_connected_as_literal_with_original_tls_hostname(network):
    ip = "2606:4700:4700::1111"
    network["rows"] = [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443, 0, 0))]
    network["peer"] = network["tls_peer"] = ip
    assert sources.fetch_public_source("https://www.sse.com.cn/")["status"] == "ok"
    assert network["connects"] == [(ip, 443, 0, 0)] and network["sni"] == ["www.sse.com.cn"]


@pytest.mark.parametrize("peer_field", ["peer", "tls_peer"])
def test_connected_peer_must_equal_the_chosen_address(network, peer_field):
    network[peer_field] = "1.1.1.1"
    result = sources.fetch_public_source("https://www.sse.com.cn/")
    assert result["status"] == "peer_mismatch"
    assert network["dns_calls"] == ["www.sse.com.cn"] and network["requests"] == []
    assert all(sock.closed for sock in network["sockets"])


def test_certificate_failure_closes_owned_socket_and_never_retries(network):
    network["tls_failure"] = True
    result = sources.fetch_public_source("https://www.sse.com.cn/")
    assert result["status"] == "tls_failed" and "synthetic cert" not in str(result)
    assert len(network["connects"]) == 1 and network["requests"] == []
    assert network["sockets"][0].closed and result["automatic_retry"] is False


def test_second_public_dns_address_is_not_a_silent_fallback(network):
    network["rows"] = [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443))
        for ip in ("8.8.8.8", "1.1.1.1")
    ]
    network["tls_failure"] = True
    assert sources.fetch_public_source("https://www.sse.com.cn/")["status"] == "tls_failed"
    assert network["connects"] == [("8.8.8.8", 443)]


def test_tls_validation_cannot_be_disabled_by_injected_context(network):
    network["context"].check_hostname = False
    assert sources.fetch_public_source("https://www.sse.com.cn/")["status"] == "tls_failed"
    assert network["requests"] == []


def test_two_relative_redirects_reresolve_and_preserve_final_url(network):
    network["wires"] = [
        wire(status=302, headers=[("Location", "/second")]),
        wire(status=301, headers=[("Location", "https://www.cninfo.com.cn/final")]),
        wire(b"final disclosure", content_type="text/plain"),
    ]
    result = sources.fetch_public_source("https://www.sse.com.cn/start")
    assert result["status"] == "ok" and result["url"] == "https://www.cninfo.com.cn/final"
    assert result["redirect_count"] == 2 and result["text"] == "final disclosure"
    assert network["dns_calls"] == ["www.sse.com.cn", "www.sse.com.cn", "www.cninfo.com.cn"]
    assert network["closed"] == 3


@pytest.mark.parametrize(
    "target",
    [
        "https://127.0.0.1/admin",
        "https://evil.example/",
        "http://www.sse.com.cn/",
        "https://www.sse.com.cn/?token=synthetic-private",
        "https://www.sse.com.cn:8443/",
    ],
)
def test_redirect_target_revalidated_before_any_second_dns_or_request(target, network):
    network["wires"] = [wire(status=302, headers=[("Location", target)])]
    result = sources.fetch_public_source("https://www.sse.com.cn/")
    assert result["status"] in {"invalid_url", "host_not_allowed"}
    assert len(network["dns_calls"]) == len(network["requests"]) == 1
    assert "synthetic-private" not in str(result)


def test_redirect_limit_is_bounded_without_retry(network):
    network["wires"] = [wire(status=302, headers=[("Location", "/again")]) for _ in range(3)]
    result = sources.fetch_public_source("https://www.sse.com.cn/")
    assert result["status"] == "redirect_limit" and len(network["requests"]) == 3


def test_deployment_ir_hosts_are_exact_and_news_is_secondary(network, monkeypatch):
    network["wires"] = [wire(), wire(), wire()]
    assert sources.fetch_public_source("https://ir.company.example/")["status"] == "host_not_allowed"
    result = sources.fetch_public_source("https://ir.company.example/", allowed_hosts=("ir.company.example",))
    assert result["source_class"] == "company_ir" and result["status"] == "ok"
    assert (
        sources.fetch_public_source("https://sub.ir.company.example/", allowed_hosts=("ir.company.example",))[
            "status"
        ]
        == "host_not_allowed"
    )
    monkeypatch.setenv("PRISM_RESEARCH_SOURCE_HOSTS", "ir.deployment.example")
    assert sources.fetch_public_source("https://ir.deployment.example/")["status"] == "ok"
    result = sources.fetch_public_source("https://finance.sina.com.cn/")
    assert result["source_class"] == "secondary_news"


@pytest.mark.parametrize(
    "hosts", ["*.example.com", "127.0.0.1", "localhost", "https://ir.example.com/", ["x"] * 33]
)
def test_invalid_additional_host_config_fails_before_dns(hosts, network):
    result = sources.fetch_public_source("https://www.sse.com.cn/", allowed_hosts=hosts)
    assert result["status"] == "invalid_host_configuration" and network["dns_calls"] == []


def test_pdf_and_compressed_body_are_explicitly_unsupported(network):
    network["wires"] = [wire(b"%PDF-synthetic", content_type="application/pdf")]
    result = sources.fetch_public_source("https://www.sse.com.cn/report.pdf")
    assert result["status"] == "unsupported_pdf" and not result["original_obtained"] and result["text"] == ""
    network["wires"] = [wire(headers=[("Content-Encoding", "gzip")])]
    assert sources.fetch_public_source("https://www.sse.com.cn/")["status"] == "unsupported_content_encoding"


def test_actual_body_and_excerpt_limits(network):
    network["wires"] = [wire(b"x" * (sources.MAX_BODY_BYTES + 1), content_type="text/plain")]
    result = sources.fetch_public_source("https://www.sse.com.cn/")
    assert result["status"] == "body_too_large" and result["text"] == ""
    network["wires"] = [wire(b"x" * 40000, content_type="text/plain")]
    result = sources.fetch_public_source("https://www.sse.com.cn/")
    assert len(result["text"]) == sources.MAX_TEXT_CHARS and result["text_truncated"] is True
    assert result["original_obtained"] is True and result["body_bytes"] == 40000


def test_ambiguous_length_headers_and_truncated_body_are_not_accepted(network):
    network["wires"] = [wire(headers=[("Content-Length", "99")])]
    assert sources.fetch_public_source("https://www.sse.com.cn/")["status"] == "invalid_headers"
    network["wires"] = [b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 99\r\n\r\nshort"]
    assert sources.fetch_public_source("https://www.sse.com.cn/")["status"] == "network_failed"


def test_unknown_publication_is_not_replaced_with_retrieval_date(network):
    network["wires"] = [wire(b'<time datetime="2026-10-20">event date</time>')]
    result = sources.fetch_public_source("https://www.sse.com.cn/")
    assert result["published_at"] is None and result["published_at_basis"] is None
    assert result["retrieved_at"].endswith("+08:00")


def test_bad_links_redacted_and_js_only_page_is_not_a_completed_analysis(network):
    network["wires"] = [wire(b"<p>https://example.com/?api_key=synthetic-secret</p>")]
    assert "synthetic-secret" not in sources.fetch_public_source("https://www.sse.com.cn/")["text"]
    network["wires"] = [wire(b"<script>renderEverything()</script><nav>menu</nav>")]
    result = sources.fetch_public_source("https://www.sse.com.cn/")
    assert result["status"] == "empty_content" and result["text"] == ""


def test_total_deadline_is_checked_inside_header_recv_and_after_dns(network, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(sources.time, "monotonic", lambda: clock[0])
    network["recv_hook"] = lambda: clock.__setitem__(0, clock[0] + 11)
    result = sources.fetch_public_source("https://www.sse.com.cn/")
    assert result["status"] == "timeout" and network["closed"] == 1
    network["recv_hook"] = None
    original_dns = sources.socket.getaddrinfo

    def slow_dns(*args, **kwargs):
        clock[0] += 11
        return original_dns(*args, **kwargs)

    monkeypatch.setattr(sources.socket, "getaddrinfo", slow_dns)
    count = len(network["connects"])
    assert sources.fetch_public_source("https://www.sse.com.cn/")["status"] == "timeout"
    assert len(network["connects"]) == count  # Resolver may overrun; no HTTP starts afterwards.


@pytest.mark.parametrize("timeout", [0, -1, True, 11, float("inf"), float("nan")])
def test_timeout_configuration_is_bounded(timeout, network):
    assert (
        sources.fetch_public_source("https://www.sse.com.cn/", timeout_seconds=timeout)["status"]
        == "invalid_timeout"
    )
    assert network["dns_calls"] == []
