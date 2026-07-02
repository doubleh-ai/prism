"""Static files are gzipped when client accepts it, and have immutable cache headers."""
import pytest


@pytest.mark.asyncio
async def test_static_js_gzipped_when_accepted(client):
    """/static/js/*.js with Accept-Encoding: gzip returns gzipped body."""
    import httpx
    from server.main import app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as raw_client:
        r = await raw_client.get(
            "/static/js/app.core.js",
            headers={"Accept-Encoding": "gzip"},
        )
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
    # httpx 0.27 auto-decompresses, so r.content is the decompressed text.
    # Verify it's valid JS (contains expected tokens).
    decoded = r.content.decode("utf-8")
    assert "App" in decoded or "init" in decoded


@pytest.mark.asyncio
async def test_static_js_not_gzipped_when_not_accepted(client):
    """Without Accept-Encoding: gzip (identity), returns raw bytes."""
    r = await client.get("/static/js/app.core.js", headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") is None


@pytest.mark.asyncio
async def test_static_files_have_immutable_cache_control(client):
    """/static/* files get Cache-Control: max-age=31536000, immutable."""
    r = await client.get("/static/css/app.css")
    cc = r.headers.get("cache-control", "")
    assert "max-age=31536000" in cc
    assert "immutable" in cc


@pytest.mark.asyncio
async def test_dynamic_routes_not_immortal_cached(client):
    """/api/* responses do NOT get the immutable cache header."""
    # /api/me returns 401 for unauthenticated; we don't care, only the header
    r = await client.get("/api/me")
    cc = r.headers.get("cache-control", "")
    assert "max-age=31536000" not in cc
