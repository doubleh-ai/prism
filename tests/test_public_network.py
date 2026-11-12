"""Startup-only transport injection must never enable an implicit fallback."""

import httpx
import pytest

from server import public_network


@pytest.fixture(autouse=True)
def empty_adapter(monkeypatch):
    monkeypatch.setattr(public_network, "_network", None)


class Adapter:
    def __init__(self):
        self.requests = []

    def handle(self, request):
        self.requests.append(request)
        return httpx.Response(
            302, headers={"Location": "https://other.invalid/", "Set-Cookie": "fixture=value"}
        )

    def sync_transport(self):
        return httpx.MockTransport(self.handle)

    def async_transport(self):
        return httpx.MockTransport(self.handle)

    def connect_tls(self, host, timeout):
        raise OSError("synthetic relay unavailable")


@pytest.mark.parametrize("invalid", [None, object(), {}, "http://proxy.invalid"])
def test_invalid_injection_is_not_installed(invalid):
    with pytest.raises(ValueError, match="configuration_invalid"):
        public_network.install(invalid)
    assert public_network.delegated() is False


def test_network_cannot_be_replaced_after_startup():
    public_network.install(Adapter())
    with pytest.raises(ValueError, match="configuration_invalid"):
        public_network.install(Adapter())
    assert public_network.delegated() is True
    with pytest.raises(OSError, match="relay unavailable"):
        public_network.connect_tls("www.sse.com.cn", 2)


async def test_sync_async_clients_share_injected_policy_without_redirects_or_cookies(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://private-proxy.invalid:9999")
    adapter = Adapter()
    public_network.install(adapter)
    with public_network.sync_client() as client:
        assert client.get("https://www.sse.com.cn/first").status_code == 302
    async with public_network.async_client() as client:
        assert (await client.get("https://www.shfe.com.cn/second")).status_code == 302
    assert [request.url.host for request in adapter.requests] == ["www.sse.com.cn", "www.shfe.com.cn"]
    assert all("cookie" not in r.headers and "authorization" not in r.headers for r in adapter.requests)


@pytest.mark.parametrize("value", [None, object(), "http://proxy.invalid"])
def test_invalid_sync_transport_cannot_fall_back_to_direct(value):
    class Invalid(Adapter):
        def sync_transport(self):
            return value

    public_network.install(Invalid())
    with pytest.raises(ValueError, match="transport_invalid"):
        public_network.sync_client()


@pytest.mark.parametrize("value", [None, object(), "http://proxy.invalid"])
def test_invalid_async_transport_cannot_fall_back_to_direct(value):
    class Invalid(Adapter):
        def async_transport(self):
            return value

    public_network.install(Invalid())
    with pytest.raises(ValueError, match="transport_invalid"):
        public_network.async_client()
