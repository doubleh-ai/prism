"""Synthetic model traffic verifies startup injection and purpose isolation."""

import asyncio
import json

import httpx
import pytest

from server import model_network, public_network
from server.agent import provider


@pytest.fixture(autouse=True)
def isolated_networks(monkeypatch):
    monkeypatch.setattr(model_network, "_network", None)
    monkeypatch.setattr(public_network, "_network", None)


@pytest.fixture(autouse=True)
def temp_data_dir(tmp_path):
    return tmp_path


def frame(kind, **fields):
    return ("event: " + kind + "\ndata: " + json.dumps({"type": kind, **fields}) + "\n\n").encode()


def answer():
    return b"".join(
        [
            frame(
                "message_start", message={"role": "assistant", "content": [], "usage": {"input_tokens": 2}}
            ),
            frame("content_block_start", index=0, content_block={"type": "text", "text": ""}),
            frame("content_block_delta", index=0, delta={"type": "text_delta", "text": "synthetic answer"}),
            frame("content_block_stop", index=0),
            frame("message_delta", delta={"stop_reason": "end_turn"}, usage={"output_tokens": 2}),
            frame("message_stop"),
        ]
    )


def config(*, purpose="research", vendor="deepseek"):
    return {
        "base_url": {
            "deepseek": "https://api.deepseek.com/anthropic",
            "minimax": "https://api.minimax.io/anthropic/v1",
            "minimax-cn": "https://api.minimaxi.com/anthropic/v1",
            "openai": "https://api.openai.com/v1",
        }[vendor],
        "model": "synthetic-model",
        "api_key": "synthetic-provider-authentication",
        "protocol": "openai_responses" if vendor == "openai" else "anthropic_messages",
        "_model_purpose": purpose,
    }


async def consume(cfg, **changes):
    options = dict(system="synthetic system", tools=[], model_cfg=cfg, max_tokens=100, timeout_seconds=2)
    options.update(changes)
    return [event async for event in provider.stream([{"role": "user", "content": "synthetic"}], **options)]


class Adapter:
    def __init__(self, *, status=200):
        self.status, self.routes, self.requests, self.transports = status, [], [], []

    def async_transport(self, **route):
        self.routes.append(route)

        def handle(request):
            self.requests.append(request)
            return httpx.Response(
                self.status, content=answer(), headers={"content-type": "text/event-stream"}
            )

        transport = httpx.MockTransport(handle)
        self.transports.append(transport)
        return transport


@pytest.mark.parametrize("value", [None, {}, object(), "https://arbitrary.invalid"])
def test_only_startup_network_objects_can_be_installed(value):
    with pytest.raises(model_network.ModelNetworkError, match="configuration_invalid"):
        model_network.install(value)
    assert not model_network.delegated()


def test_installed_network_cannot_be_replaced():
    model_network.install(Adapter())
    with pytest.raises(model_network.ModelNetworkError, match="configuration_invalid"):
        model_network.install(Adapter())


@pytest.mark.parametrize("vendor", ["deepseek", "minimax", "minimax-cn", "openai"])
async def test_native_protocols_use_their_fixed_model_transport_without_giving_it_credentials(vendor):
    adapter = Adapter(status=401)
    model_network.install(adapter)
    cfg = config(vendor=vendor)
    with pytest.raises(provider.ProviderError, match="provider_auth_error"):
        await consume(cfg)
    assert len(adapter.requests) == len(adapter.routes) == 1
    route = adapter.routes[0]
    assert route == {"purpose": "research", "model": cfg["model"], "endpoint": str(adapter.requests[0].url)}
    assert cfg["api_key"] not in repr(route)
    assert str(adapter.requests[0].url) in model_network.ENDPOINTS


async def test_each_purpose_call_owns_a_fresh_transport_and_keeps_payload_metadata_local(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://synthetic-must-not-be-used.invalid:9")
    adapter = Adapter()
    model_network.install(adapter)
    for purpose in ("research", "work_planning", "auxiliary"):
        events = await consume(config(purpose=purpose))
        assert events[-1]["type"] == "done"
    assert len({id(t) for t in adapter.transports}) == 3
    assert [r["purpose"] for r in adapter.routes] == ["research", "work_planning", "auxiliary"]
    for req in adapter.requests:
        body = json.loads(req.content)
        assert body["model"] == "synthetic-model" and "_model_purpose" not in body
        assert req.headers["x-api-key"] == "synthetic-provider-authentication"
        assert "cookie" not in req.headers and "proxy-authorization" not in req.headers


@pytest.mark.parametrize("value", [None, object(), "https://direct.invalid"])
async def test_missing_delegated_transport_never_falls_back_to_direct_http(value, monkeypatch):
    class Invalid:
        def async_transport(self, **route):
            return value

    model_network.install(Invalid())
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kw: pytest.fail("direct connection"))
    with pytest.raises(provider.ProviderError, match="provider_configuration_error"):
        await consume(config())


@pytest.mark.parametrize("purpose", [None, "repair", "research?scope=all", ["research"]])
async def test_unknown_or_missing_purpose_is_rejected_before_transport_creation(purpose):
    adapter = Adapter()
    model_network.install(adapter)
    with pytest.raises(provider.ProviderError, match="provider_configuration_error"):
        await consume(config(purpose=purpose))
    assert adapter.routes == adapter.requests == []


async def test_delegated_failure_does_not_leak_diagnostics_or_select_a_different_provider():
    class Broken:
        def async_transport(self, **route):
            raise ValueError("synthetic-private-diagnostic-and-key")

    model_network.install(Broken())
    with pytest.raises(provider.ProviderError) as caught:
        await consume(config())
    assert str(caught.value) == "provider_configuration_error"
    assert "synthetic" not in repr(caught.value)


async def test_invalid_model_request_does_not_allocate_a_transport():
    adapter = Adapter()
    model_network.install(adapter)
    with pytest.raises(provider.ProviderError, match="provider_input_error"):
        await consume(config(), max_tokens=0)
    assert adapter.routes == []


async def test_explicit_provider_transport_cannot_override_installed_policy():
    adapter = Adapter()
    model_network.install(adapter)
    local = provider.AnthropicMessagesProvider(
        transport=httpx.MockTransport(lambda req: pytest.fail("override"))
    )
    with pytest.raises(provider.ProviderError, match="provider_configuration_error"):
        _ = [
            e
            async for e in local.stream(
                [{"role": "user", "content": "synthetic"}],
                system="synthetic",
                tools=[],
                model_cfg=config(),
                max_tokens=10,
                timeout_seconds=1,
            )
        ]
    assert adapter.routes == []


async def test_factory_cancellation_propagates_without_replacing_the_network():
    class Cancelled:
        def async_transport(self, **route):
            raise asyncio.CancelledError()

    model_network.install(Cancelled())
    with pytest.raises(asyncio.CancelledError):
        await consume(config())
    assert model_network.delegated()


def test_public_data_attachment_does_not_enable_model_network():
    class DataOnly:
        def async_transport(self):
            return httpx.MockTransport(lambda req: pytest.fail("public data must not carry model requests"))

        sync_transport = async_transport

        def connect_tls(self, host, timeout):
            raise OSError("synthetic")

    public_network.install(DataOnly())
    assert public_network.delegated() and not model_network.delegated()
