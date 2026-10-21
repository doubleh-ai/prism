"""FastAPI routes for market data — /api/quote, /api/quote/kline,
/api/fundamentals, /api/search, /api/data-providers.

All endpoints require authentication (the existing middleware
enforces that for any non-``/api/login`` path). The endpoints are
GET-only so no CSRF header is required.
"""
import re
import logging
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from .factory import get_chain
from .models import SearchHit
from .provider import NoProviderAvailable, ProviderNotFound

log = logging.getLogger("stock-app")

router = APIRouter(prefix="/api")

# Tickers: 6 digits (A-share) or HK 5-digit / US alpha-numeric.
# Loose pattern — providers will reject truly bogus ones with NotFound.
_TICKER_RE = re.compile(r"^[A-Za-z0-9.\-]{1,16}$")
_A_SHARE_CODE_RE = re.compile(r"^(?:sh|sz|bj)?(\d{6})(?:\.(?:sh|sz|bj))?$", re.I)


def _validate_ticker(ticker: str) -> str:
    ticker = ticker.strip()
    if not _TICKER_RE.match(ticker):
        raise HTTPException(status_code=400, detail="invalid ticker")
    return ticker


def _market_for_code(code: str) -> str | None:
    if code.startswith("6"):
        return "SH"
    if code.startswith(("0", "3")):
        return "SZ"
    if code.startswith(("4", "8", "92")):
        return "BJ"
    return None


def _direct_code_hit(query: str) -> SearchHit | None:
    match = _A_SHARE_CODE_RE.match(query.strip())
    if not match:
        return None
    code = match.group(1)
    return SearchHit(ticker=code, name=code, market=_market_for_code(code))


@router.get("/quote/{ticker}")
async def api_quote(ticker: str, request: Request):
    ticker = _validate_ticker(ticker)
    try:
        quote, stale = await get_chain().get_quote(ticker)
    except NoProviderAvailable as e:
        return JSONResponse(
            status_code=503,
            content={"error": "no_provider_available", "detail": str(e)},
        )
    except ProviderNotFound:
        raise HTTPException(404, "ticker not found")
    return quote.model_dump()


@router.get("/quotes")
async def api_quotes_batch(request: Request, tickers: str = ""):
    """Batch quote — multiple tickers in one request (comma-separated).
    Returns {ticker: quote} for all successfully fetched tickers.
    Much faster than N individual requests for chat [[quote:XXX]] chips."""
    ticker_list = [t.strip() for t in tickers.split(",") if t.strip()][:20]
    if not ticker_list:
        return JSONResponse(status_code=400, content={"error": "no tickers"})
    chain = get_chain()
    results = {}
    for t in ticker_list:
        try:
            quote, _ = await chain.get_quote(t)
            results[t] = quote.model_dump()
        except Exception:
            results[t] = {"error": "unavailable"}
    return {"quotes": results}


@router.get("/quote/{ticker}/kline")
async def api_kline(
    ticker: str, request: Request,
    period: str = "1d", limit: int = 120,
):
    ticker = _validate_ticker(ticker)
    if period not in ("1d", "1w", "1m", "5m", "15m", "30m", "60m"):
        raise HTTPException(400, "invalid period")
    if limit < 1 or limit > 500:
        raise HTTPException(400, "limit must be 1..500")
    try:
        kls, stale = await get_chain().get_kline(ticker, period=period, limit=limit)
    except NoProviderAvailable as e:
        return JSONResponse(
            status_code=503,
            content={"error": "no_provider_available", "detail": str(e)},
        )
    return {"ticker": ticker, "period": period, "kline": [k.model_dump() for k in kls], "stale": stale}


@router.get("/fundamentals/{ticker}")
async def api_fundamentals(ticker: str, request: Request):
    ticker = _validate_ticker(ticker)
    try:
        fund, stale = await get_chain().get_fundamentals(ticker)
    except NoProviderAvailable as e:
        return JSONResponse(
            status_code=503,
            content={"error": "no_provider_available", "detail": str(e)},
        )
    except ProviderNotFound:
        raise HTTPException(404, "ticker not found")
    return fund.model_dump()


@router.get("/search")
async def api_search(request: Request, q: str = "", limit: int = 10):
    q = q.strip()
    if not q or len(q) > 32:
        return {"hits": []}
    if limit < 1 or limit > 50:
        limit = 10
    hits = await get_chain().search(q, limit=limit)
    direct = _direct_code_hit(q)
    if direct and not any(h.ticker == direct.ticker for h in hits):
        hits = [direct] + hits[: max(0, limit - 1)]
    return {"hits": [h.model_dump() for h in hits]}


@router.get("/data-providers")
async def api_data_providers(request: Request):
    health = await get_chain().health()
    providers = [{"name": name, "healthy": ok} for name, ok in health.items()]
    return {"providers": providers}
