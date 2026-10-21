"""BaostockProvider — lazy login, K-line/quote/fundamentals from daily bars."""
import datetime
import pytest
from unittest.mock import patch, MagicMock


def _make_fake_rs(rows, fields):
    """Build a fake baostock ResultData.

    baostock iteration: ``rs.next()`` returns True while a row exists
    (advances cursor), ``rs.get_row_data()`` returns the current row as a
    list of strings in ``rs.fields`` order.
    """
    rs = MagicMock()
    rs.fields = list(fields)
    rs.error_code = "0"
    # Pre-render row values in field order (baostock returns strings).
    rendered = [[str(r[f]) for f in fields] for r in rows]
    state = {"i": 0, "cur": []}

    def nxt():
        if state["i"] < len(rendered):
            state["cur"] = rendered[state["i"]]
            state["i"] += 1
            return True
        return False

    rs.next.side_effect = nxt
    rs.get_row_data.side_effect = lambda: state["cur"]
    return rs


def _patched_bs(fake_bs):
    """Context manager that reloads baostock_provider with a fake baostock."""
    return patch.dict("sys.modules", {"baostock": fake_bs})


def _kline_rows():
    return [
        {"date": "2024-01-15", "open": "1670.0", "high": "1690.0", "low": "1665.0",
         "close": "1680.5", "volume": "12345", "amount": "2075000000"},
        {"date": "2024-01-16", "open": "1680.0", "high": "1700.0", "low": "1675.0",
         "close": "1695.0", "volume": "11000", "amount": "1860000000"},
    ]


def _quote_rows():
    return [
        {"date": "2024-01-15", "code": "sh.600519", "open": "1670.0", "high": "1690.0",
         "low": "1665.0", "close": "1680.5", "preclose": "1670.0", "volume": "12345",
         "amount": "2075000000", "pctChg": "0.63", "turn": "0.1"},
        {"date": "2024-01-16", "code": "sh.600519", "open": "1680.0", "high": "1700.0",
         "low": "1675.0", "close": "1695.0", "preclose": "1680.5", "volume": "11000",
         "amount": "1860000000", "pctChg": "0.86", "turn": "0.09"},
    ]


def _fund_rows():
    return [
        {"date": "2024-01-16", "close": "1695.0", "pctChg": "0.86",
         "turn": "0.09", "peTTM": "18.74", "pbMRQ": "5.72"},
    ]


@pytest.mark.asyncio
async def test_get_quote_builds_from_latest_bar():
    fake_bs = MagicMock()
    fake_bs.login.return_value = MagicMock(error_code="0", error_msg="success")
    quote_fields = ["date", "code", "open", "high", "low", "close", "preclose",
                    "volume", "amount", "pctChg", "turn"]
    fake_bs.query_history_k_data_plus.return_value = _make_fake_rs(_quote_rows(), quote_fields)
    with _patched_bs(fake_bs):
        import importlib
        import server.market.baostock_provider as mod
        importlib.reload(mod)
        from server.market.baostock_provider import BaostockProvider
        p = BaostockProvider()
        q = await p.get_quote("600519")
    # Latest bar close = 1695.0, preclose = 1680.5 → change = 14.5
    assert q.ticker == "600519"
    assert q.price == 1695.0
    assert q.prev_close == 1680.5
    assert q.change == pytest.approx(14.5)
    assert q.change_pct == pytest.approx(0.86)
    assert q.source == "baostock"


@pytest.mark.asyncio
async def test_get_kline_normalizes_rows():
    fake_bs = MagicMock()
    fake_bs.login.return_value = MagicMock(error_code="0", error_msg="success")
    kline_fields = ["date", "open", "high", "low", "close", "volume", "amount"]
    fake_bs.query_history_k_data_plus.return_value = _make_fake_rs(_kline_rows(), kline_fields)
    with _patched_bs(fake_bs):
        import importlib
        import server.market.baostock_provider as mod
        importlib.reload(mod)
        from server.market.baostock_provider import BaostockProvider
        p = BaostockProvider()
        bars = await p.get_kline("sh.600519", period="1d", limit=120)
    assert len(bars) == 2
    assert bars[0].close == 1680.5
    assert bars[1].close == 1695.0


@pytest.mark.asyncio
@pytest.mark.parametrize(("period", "frequency", "days_per_bar"), [
    ("1w", "w", 8),
    ("1m", "m", 35),
])
async def test_get_kline_uses_native_period_and_sufficient_lookback(period, frequency, days_per_bar):
    fake_bs = MagicMock()
    fake_bs.login.return_value = MagicMock(error_code="0", error_msg="success")
    fields = ["date", "open", "high", "low", "close", "volume", "amount"]
    fake_bs.query_history_k_data_plus.return_value = _make_fake_rs(_kline_rows(), fields)
    with _patched_bs(fake_bs):
        import importlib
        import server.market.baostock_provider as mod
        importlib.reload(mod)
        from server.market.baostock_provider import BaostockProvider
        await BaostockProvider().get_kline("600519", period=period, limit=20)

    call = fake_bs.query_history_k_data_plus.call_args
    assert call.kwargs["frequency"] == frequency
    start = datetime.datetime.strptime(call.kwargs["start_date"], "%Y-%m-%d").date()
    assert (datetime.date.today() - start).days >= 20 * days_per_bar


@pytest.mark.asyncio
async def test_get_fundamentals_extracts_pe_pb():
    fake_bs = MagicMock()
    fake_bs.login.return_value = MagicMock(error_code="0", error_msg="success")
    fund_fields = ["date", "close", "pctChg", "turn", "peTTM", "pbMRQ"]
    fake_bs.query_history_k_data_plus.return_value = _make_fake_rs(_fund_rows(), fund_fields)
    with _patched_bs(fake_bs):
        import importlib
        import server.market.baostock_provider as mod
        importlib.reload(mod)
        from server.market.baostock_provider import BaostockProvider
        p = BaostockProvider()
        fund = await p.get_fundamentals("600519")
    assert fund.pe_ttm == pytest.approx(18.74)
    assert fund.pb == pytest.approx(5.72)
    assert fund.source == "baostock"


@pytest.mark.asyncio
async def test_get_quote_no_data_raises_not_found():
    fake_bs = MagicMock()
    fake_bs.login.return_value = MagicMock(error_code="0", error_msg="success")
    empty_fields = ["date", "close"]
    fake_bs.query_history_k_data_plus.return_value = _make_fake_rs([], empty_fields)
    with _patched_bs(fake_bs):
        import importlib
        import server.market.baostock_provider as mod
        importlib.reload(mod)
        from server.market.baostock_provider import BaostockProvider
        from server.market.provider import ProviderNotFound
        p = BaostockProvider()
        with pytest.raises(ProviderNotFound):
            await p.get_quote("999999")


@pytest.mark.asyncio
async def test_search_returns_empty():
    """baostock delegates search to the next provider in the chain."""
    fake_bs = MagicMock()
    with _patched_bs(fake_bs):
        import importlib
        import server.market.baostock_provider as mod
        importlib.reload(mod)
        from server.market.baostock_provider import BaostockProvider
        p = BaostockProvider()
        hits = await p.search("茅台", limit=5)
    assert hits == []
