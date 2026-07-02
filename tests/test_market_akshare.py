"""AkshareProvider — lazy import + response normalization."""
import asyncio
import pandas as pd
import pytest
from unittest.mock import patch


def _fake_sina_df():
    """Return a DataFrame matching ``ak.stock_zh_a_daily`` column set."""
    import datetime
    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    return pd.DataFrame([
        {
            "date": yesterday.strftime("%Y-%m-%d"),
            "open": 1668.0,
            "close": 1668.2,
            "high": 1675.0,
            "low": 1660.0,
            "volume": 10000,
            "amount": 1668200000.0,
        },
        {
            "date": today.strftime("%Y-%m-%d"),
            "open": 1670.0,
            "close": 1680.5,
            "high": 1690.0,
            "low": 1665.0,
            "volume": 12345,
            "amount": 2075000000.0,
        },
    ])


@pytest.mark.asyncio
async def test_get_quote_from_sina_daily():
    """get_quote uses stock_zh_a_daily (Sina) and derives price/change
    from the two most recent daily bars."""
    with patch("akshare.stock_zh_a_daily", return_value=_fake_sina_df()):
        from server.market.akshare_provider import AkshareProvider
        p = AkshareProvider()
        q = await p.get_quote("600519")
    assert q.ticker == "600519"
    assert q.price == 1680.5
    assert abs(q.change - 12.3) < 0.01
    assert abs(q.change_pct - 0.737) < 0.1
    assert q.source == "akshare"
    assert q.open == 1670.0
    assert q.high == 1690.0
    assert q.low == 1665.0
    assert q.prev_close == 1668.2


@pytest.mark.asyncio
async def test_get_quote_unknown_ticker_raises():
    empty = pd.DataFrame()
    with patch("akshare.stock_zh_a_daily", return_value=empty):
        from server.market.akshare_provider import AkshareProvider
        p = AkshareProvider()
        from server.market.provider import ProviderNotFound
        with pytest.raises(ProviderNotFound):
            await p.get_quote("999999")


@pytest.mark.asyncio
async def test_provider_unavailable_when_akshare_missing(monkeypatch):
    """If akshare is not installed, the provider raises ProviderUnavailable."""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "akshare" or name.startswith("akshare."):
            raise ImportError("akshare not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    import importlib
    import server.market.akshare_provider as mod
    importlib.reload(mod)
    from server.market.akshare_provider import AkshareProvider
    from server.market.provider import ProviderUnavailable
    p = AkshareProvider()
    with pytest.raises(ProviderUnavailable):
        await p.get_quote("600519")


@pytest.mark.asyncio
async def test_get_quote_timeout_raises_provider_unavailable(monkeypatch):
    """Timeout → ProviderUnavailable."""
    import server.config as cfg

    def _hang():
        import time as _t
        _t.sleep(5)
        return _fake_sina_df()

    monkeypatch.setattr(cfg, "AKSHARE_TIMEOUT", 1)
    with patch("akshare.stock_zh_a_daily", side_effect=_hang):
        from server.market.akshare_provider import AkshareProvider
        from server.market.provider import ProviderUnavailable
        p = AkshareProvider()
        with pytest.raises(ProviderUnavailable):
            await p.get_quote("600519")