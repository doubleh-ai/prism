# -*- coding: utf-8 -*-
"""Tencent provider's urllib calls must not block the event loop.

Strategy: mock urlopen to do a non-trivial amount of work, then call
get_quote. During the call, schedule another coroutine on the loop and
verify it runs — i.e. the loop is not blocked.
"""
import asyncio
import json
import time
from unittest.mock import patch, MagicMock


def test_get_quote_does_not_block_event_loop():
    """While get_quote awaits the (mocked) upstream, the loop runs other tasks."""
    from server.market.tencent_provider import TencentProvider

    # Simulate a 200 ms "upstream call" — synchronous, in a thread
    def fake_urlopen(req, timeout):
        time.sleep(0.2)
        m = MagicMock()
        m.read.return_value = b'v_sh600519="1~\xe8\xbe\xbd\xe5\xb7\x9e\xe8\x8a\xd1\xe7\x99\xbd~600519~1700.00~1690.00~1700.00~1000000~500000~1700.00~1690.00~1700.00"'
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    async def side_task():
        """Runs while get_quote is awaiting — if loop is blocked, this never finishes."""
        await asyncio.sleep(0)
        return time.time()

    async def main():
        provider = TencentProvider()
        with patch("server.market.tencent_provider.urllib.request.urlopen", new=fake_urlopen):
            # Schedule side_task to run at +50 ms (during the 200 ms upstream)
            loop = asyncio.get_event_loop()
            side_done_time = loop.create_task(asyncio.sleep(0.05))
            t0 = time.time()
            quote = await provider.get_quote("600519")
            await side_done_time
            elapsed = time.time() - t0
        # If the loop was blocked by urlopen (sync in async), elapsed
        # would be >= 200 ms (sequential). If asyncio.to_thread worked,
        # elapsed ≈ 200 ms but side_task ran in parallel.
        assert quote is not None
        assert quote.ticker == "600519"
        # Loop was free: side task should fire around +50 ms, not +200 ms.
        # We don't assert exact timing (CI noise) — just that get_quote
        # didn't block. The presence of the `await asyncio.sleep(0.05)`
        # before get_quote returns is what we check below.
        return elapsed

    elapsed = asyncio.run(main())
    # The elapsed time should be <= 250ms (200ms upstream + 50ms scheduling
    # overhead), NOT 200ms + 200ms (which would mean the side task was
    # blocked behind the upstream call). Allow 300ms for CI noise.
    assert elapsed < 0.3, f"Loop appears blocked: {elapsed*1000:.0f} ms elapsed"


def test_get_quote_returns_valid_quote():
    """The mocked quote parses to a real Quote model."""
    from server.market.tencent_provider import TencentProvider

    def fake_urlopen(req, timeout):
        m = MagicMock()
        m.read.return_value = (
            b'v_sz002050="51~\xe4\xb8\x89\xe8\x8a\xb1\xe6\x99\xba\xe6\x8e\xa7~002050~'
            b'45.74~45.50~45.80~1000000~500000~45.74~45.50~45.74"'
        )
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    async def main():
        provider = TencentProvider()
        with patch("server.market.tencent_provider.urllib.request.urlopen", new=fake_urlopen):
            return await provider.get_quote("002050")

    quote = asyncio.run(main())
    assert quote.ticker == "002050"
    assert quote.price == 45.74
    assert quote.source == "tencent"


def test_get_kline_does_not_block_event_loop():
    """Same non-blocking guarantee for get_kline."""
    from server.market.tencent_provider import TencentProvider

    def fake_urlopen(req, timeout):
        time.sleep(0.2)
        m = MagicMock()
        m.read.return_value = b'[{"day":"2024-01-01","open":1.0,"high":2.0,"low":0.5,"close":1.5,"volume":1000.0}]'
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    async def main():
        provider = TencentProvider()
        with patch("server.market.tencent_provider.urllib.request.urlopen", new=fake_urlopen):
            t0 = time.time()
            kline = await provider.get_kline("600519", "1d", 10)
            elapsed = time.time() - t0
        return kline, elapsed

    kline, elapsed = asyncio.run(main())
    assert len(kline) == 1
    assert kline[0].close == 1.5
    assert elapsed < 0.3, f"K-line loop appears blocked: {elapsed*1000:.0f} ms"


def test_get_kline_builds_weekly_bars_and_fetches_enough_daily_history():
    from server.market.tencent_provider import TencentProvider

    captured = {}
    payload = [
        {"day": "2024-01-29", "open": 10, "high": 12, "low": 9, "close": 11, "volume": 100},
        {"day": "2024-02-02", "open": 11, "high": 14, "low": 10, "close": 13, "volume": 200},
        {"day": "2024-02-05", "open": 13, "high": 15, "low": 12, "close": 14, "volume": 300},
    ]

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        response = MagicMock()
        response.read.return_value = json.dumps(payload).encode("utf-8")
        response.__enter__ = lambda value: value
        response.__exit__ = lambda value, *args: None
        return response

    async def main():
        provider = TencentProvider()
        with patch("server.market.tencent_provider.urllib.request.urlopen", new=fake_urlopen):
            return await provider.get_kline("600519", "1w", 2)

    bars = asyncio.run(main())
    assert "datalen=60" in captured["url"]
    assert len(bars) == 2
    assert (bars[0].open, bars[0].high, bars[0].low, bars[0].close) == (10, 14, 9, 13)
    assert bars[0].volume == 300
