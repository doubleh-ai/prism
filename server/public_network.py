"""Startup injection for credential-free public data connections.

The ordinary runtime uses direct HTTPS. An isolated runtime can install its
own transports and TLS connector before serving requests. This module neither
selects a proxy from the environment nor grants network or deployment rights.
Models use their separate provider transports, never this public-data channel.
"""

from __future__ import annotations

import ssl
from typing import Protocol

import httpx


class PublicNetwork(Protocol):
    def sync_transport(self) -> httpx.BaseTransport: ...

    def async_transport(self) -> httpx.AsyncBaseTransport: ...

    def connect_tls(self, host: str, timeout: float) -> ssl.SSLSocket: ...


_network: PublicNetwork | None = None


def install(network: PublicNetwork) -> None:
    """Install once at trusted startup; there is no HTTP configuration route."""
    global _network
    if _network is not None or not all(
        callable(getattr(network, name, None))
        for name in ("sync_transport", "async_transport", "connect_tls")
    ):
        raise ValueError("public_network_configuration_invalid")
    _network = network


def delegated() -> bool:
    return _network is not None


def connect_tls(host: str, timeout: float) -> ssl.SSLSocket:
    """A delegated failure stays a failure; it cannot fall back to direct TCP."""
    if _network is None:
        raise ValueError("public_network_not_configured")
    return _network.connect_tls(host, timeout)


def sync_client(*, timeout=8, headers=None, limits=None) -> httpx.Client:
    transport = _network.sync_transport() if _network is not None else None
    if _network is not None and not isinstance(transport, httpx.BaseTransport):
        raise ValueError("public_network_transport_invalid")
    return httpx.Client(
        **({"transport": transport} if _network is not None else {}),
        timeout=timeout,
        headers=headers,
        limits=limits if limits is not None else httpx.Limits(),
        trust_env=False,
        follow_redirects=False,
    )


def async_client(*, timeout=8, headers=None, limits=None) -> httpx.AsyncClient:
    transport = _network.async_transport() if _network is not None else None
    if _network is not None and not isinstance(transport, httpx.AsyncBaseTransport):
        raise ValueError("public_network_transport_invalid")
    return httpx.AsyncClient(
        **({"transport": transport} if _network is not None else {}),
        timeout=timeout,
        headers=headers,
        limits=limits if limits is not None else httpx.Limits(),
        trust_env=False,
        follow_redirects=False,
    )
