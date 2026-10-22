"""Actual market router on a disposable source copy; isolated sessions only."""

import hashlib
import types
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def runtime_router(tmp_path):
    source = ROOT / "server/market/router.py"
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    destination = tmp_path / "server/market/router.py"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(source.read_bytes())
    module = types.ModuleType("server.market._auth_runtime_copy")
    module.__file__, module.__package__ = str(destination), "server.market"
    exec(compile(destination.read_bytes(), str(destination), "exec"), module.__dict__)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    return module


URLS = (
    "/api/quote/600519",
    "/api/quotes?tickers=600519",
    "/api/quote/600519/kline",
    "/api/fundamentals/600519",
    "/api/search?q=600519",
    "/api/data-providers",
)


def session(state):
    from server.database import create_session, delete_session, get_db

    if state == "anonymous":
        return {}
    if state == "invalid":
        return {"token": "synthetic-nonexistent-session"}
    with get_db() as db:
        db.execute(
            "INSERT INTO users(username,password_hash) VALUES(?,?)",
            ["synthetic-market-owner", "synthetic-unused-login-hash"],
        )
    token = create_session("synthetic-market-owner")
    if state == "revoked":
        delete_session(token)
    elif state == "expired":
        with get_db() as db:
            db.execute("UPDATE sessions SET expires=0 WHERE token=?", [token])
    elif state == "disabled":
        with get_db() as db:
            db.execute("UPDATE users SET disabled_at=1 WHERE username=?", ["synthetic-market-owner"])
    return {"token": token}


@pytest.mark.asyncio
@pytest.mark.parametrize("url", URLS)
@pytest.mark.parametrize("state", ["anonymous", "invalid", "revoked", "expired", "disabled"])
async def test_all_market_routes_reject_before_any_provider_io(runtime_router, monkeypatch, url, state):
    def forbidden():
        pytest.fail("unauthenticated request reached the provider factory")

    monkeypatch.setattr(runtime_router, "get_chain", forbidden)
    app = FastAPI()
    app.include_router(runtime_router.router)
    cookies = session(state)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://fixture") as client:
        client.cookies.update(cookies)
        response = await client.get(url + ("&" if "?" in url else "?") + "optional=true")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_valid_session_retains_the_existing_quote_contract(runtime_router, monkeypatch):
    calls = []

    class Chain:
        async def get_quote(self, ticker):
            calls.append(ticker)
            value = {"ticker": ticker, "source": "synthetic-provider", "price": 1.0}
            return types.SimpleNamespace(model_dump=lambda: value), False

    monkeypatch.setattr(runtime_router, "get_chain", lambda: Chain())
    app = FastAPI()
    app.include_router(runtime_router.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://fixture") as client:
        client.cookies.update(session("valid"))
        response = await client.get("/api/quote/600519")
    assert response.status_code == 200
    assert response.json() == {"ticker": "600519", "source": "synthetic-provider", "price": 1.0}
    assert calls == ["600519"]
