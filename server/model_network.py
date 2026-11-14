"""Startup-only model network attachment, separate from public data access.

The owner supplies a fresh transport for each model call. This module receives
only purpose, logical endpoint and model name; it never finds credentials,
chooses another provider or grants model/deployment authority.
"""

from __future__ import annotations

from typing import Protocol

import httpx

PURPOSES = frozenset({"research", "work_planning", "auxiliary"})
ENDPOINTS = frozenset(
    {
        "https://api.deepseek.com/anthropic/v1/messages",
        "https://api.minimax.io/anthropic/v1/messages",
        "https://api.minimax.cn/anthropic/v1/messages",
        "https://api.minimaxi.com/anthropic/v1/messages",
        "https://api.openai.com/v1/responses",
    }
)


class ModelNetwork(Protocol):
    def async_transport(self, *, purpose: str, endpoint: str, model: str) -> httpx.AsyncBaseTransport: ...


class ModelNetworkError(ValueError):
    """Closed failures; never expose a transport's diagnostic or credential."""


_network: ModelNetwork | None = None


def install(network: ModelNetwork) -> None:
    global _network
    if _network is not None or not callable(getattr(network, "async_transport", None)):
        raise ModelNetworkError("model_network_configuration_invalid")
    _network = network


def delegated() -> bool:
    return _network is not None


def async_transport(*, purpose: str | None, endpoint: str, model: str) -> httpx.AsyncBaseTransport | None:
    if _network is None:
        return None
    if not (
        type(purpose) is str
        and purpose in PURPOSES
        and type(endpoint) is str
        and endpoint in ENDPOINTS
        and type(model) is str
        and 0 < len(model) <= 128
        and all(32 <= ord(c) < 127 for c in model)
    ):
        raise ModelNetworkError("model_network_route_invalid")
    try:
        transport = _network.async_transport(purpose=purpose, endpoint=endpoint, model=model)
    except Exception:
        raise ModelNetworkError("model_network_unavailable") from None
    if not isinstance(transport, httpx.AsyncBaseTransport):
        raise ModelNetworkError("model_network_transport_invalid")
    return transport
