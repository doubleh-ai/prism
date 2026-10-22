"""Market data HTTP endpoints — happy + 503 + stale + 404."""

from unittest.mock import AsyncMock, patch

import pytest

from server.market.models import Quote


@pytest.fixture
async def market_client(client, auth_cookies):
    client.cookies.update(auth_cookies)
    return client


@pytest.mark.asyncio
async def test_quote_endpoint_happy_path(market_client):
    fake_quote = Quote(
        ticker="600519",
        price=1680.5,
        change=12.3,
        change_pct=0.74,
        timestamp=1718700000,
        source="akshare",
    )
    with patch("server.market.router.get_chain") as gc:
        chain = gc.return_value
        chain.get_quote = AsyncMock(return_value=(fake_quote, False))
        r = await market_client.get("/api/quote/600519")
    assert r.status_code == 200
    data = r.json()
    assert data["ticker"] == "600519"
    assert data["price"] == 1680.5
    assert data["stale"] is False
    assert data["source"] == "akshare"


@pytest.mark.asyncio
async def test_quote_endpoint_stale_path(market_client):
    fake_quote = Quote(
        ticker="600519",
        price=999.0,
        timestamp=1718700000,
        source="akshare",
        stale=True,
    )
    with patch("server.market.router.get_chain") as gc:
        chain = gc.return_value
        chain.get_quote = AsyncMock(return_value=(fake_quote, True))
        r = await market_client.get("/api/quote/600519")
    assert r.status_code == 200
    data = r.json()
    assert data["stale"] is True
    assert data["price"] == 999.0


@pytest.mark.asyncio
async def test_quote_endpoint_no_provider(market_client):
    from server.market.provider import NoProviderAvailable

    with patch("server.market.router.get_chain") as gc:
        chain = gc.return_value
        chain.get_quote = AsyncMock(side_effect=NoProviderAvailable("none"))
        r = await market_client.get("/api/quote/600519")
    assert r.status_code == 503
    body = r.json()
    assert body["error"] == "no_provider_available"


@pytest.mark.asyncio
async def test_quote_endpoint_invalid_ticker_rejected(market_client):
    r = await market_client.get("/api/quote/!!!invalid!!!")
    # 404 from the route param, or 422 from the chain — both acceptable
    assert r.status_code in (400, 404, 422)


@pytest.mark.asyncio
async def test_quote_endpoint_requires_auth(client):
    """The shared HTTP client is anonymous until a session is explicitly set."""
    with patch("server.market.router.get_chain") as factory:
        response = await client.get("/api/quote/600519")
    assert response.status_code == 401
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_search_exact_code_has_direct_fallback(market_client):
    with patch("server.market.router.get_chain") as gc:
        chain = gc.return_value
        chain.search = AsyncMock(return_value=[])
        r = await market_client.get("/api/search?q=300607&limit=8")

    assert r.status_code == 200
    data = r.json()
    assert data["hits"][0]["ticker"] == "300607"
    assert data["hits"][0]["market"] == "SZ"
