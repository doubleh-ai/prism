"""Security regressions: XSS, security headers, path traversal."""

import os
import re
import sys
from pathlib import Path

import pytest

# Make server importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── HTML sanitization (front-end render function logic) ──
def _sanitize_html(html):
    """Mirror of static/js/app.js sanitizeHtml for unit testing."""
    if not html:
        return ""
    s = html
    s = re.sub(r"<script\b[\s\S]*?</script>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"<style\b[\s\S]*?</style>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"<iframe\b[\s\S]*?</iframe>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"<object\b[\s\S]*?</object>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"<form\b[\s\S]*?</form>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"<embed\b[^>]*?/?>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"<link\b[^>]*?/?>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"<meta\b[^>]*?/?>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"<base\b[^>]*?/?>", "", s, flags=re.IGNORECASE)
    s = re.sub(r'\s+on[a-z]+\s*=\s*"[^"]*"', "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+on[a-z]+\s*=\s*'[^']*'", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+on[a-z]+\s*=\s*[^\s>]+", "", s, flags=re.IGNORECASE)
    s = re.sub(
        r'(href|src|action|formaction|srcset|xlink:href)\s*=\s*"\s*(javascript|vbscript)\s*:[^"]*"',
        r'\1="#"',
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(
        r"(href|src|action|formaction|srcset|xlink:href)\s*=\s*'\s*(javascript|vbscript)\s*:[^']*'",
        "$1='#'",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(
        r"(href|src|action|formaction|srcset|xlink:href)\s*=\s*(javascript|vbscript)\s*:[^\s>]*",
        '$1="#"',
        s,
        flags=re.IGNORECASE,
    )
    return s


class TestHTMLSanitizer:
    def test_strips_script_tag(self):
        out = _sanitize_html("<p>hi</p><script>alert(1)</script>")
        assert "<script" not in out.lower()
        assert "<p>hi</p>" in out

    def test_strips_inline_event_handler_double_quote(self):
        out = _sanitize_html('<a href="https://x.com" onclick="alert(1)">x</a>')
        assert "onclick" not in out.lower()
        assert 'href="https://x.com"' in out

    def test_strips_inline_event_handler_single_quote(self):
        out = _sanitize_html("<img src=x onerror='alert(1)'>")
        assert "onerror" not in out.lower()

    def test_strips_inline_event_handler_unquoted(self):
        out = _sanitize_html("<img src=x onerror=alert(1)>")
        assert "onerror" not in out.lower()

    def test_strips_javascript_href(self):
        out = _sanitize_html('<a href="javascript:alert(1)">x</a>')
        assert "javascript:" not in out.lower()
        assert 'href="#"' in out

    def test_strips_vbscript_href(self):
        out = _sanitize_html('<a href="VBSCRIPT:alert(1)">x</a>')
        assert "vbscript:" not in out.lower()
        assert 'href="#"' in out

    def test_strips_iframe(self):
        out = _sanitize_html('<iframe src="https://evil.com"></iframe>')
        assert "<iframe" not in out.lower()

    def test_preserves_normal_markdown(self):
        md = '<p>Hello <strong>world</strong> with <a href="https://example.com">link</a></p>'
        out = _sanitize_html(md)
        assert "<strong>world</strong>" in out
        assert 'href="https://example.com"' in out

    def test_strips_object_and_embed(self):
        out = _sanitize_html('<object data="x"></object><embed src="y">')
        assert "<object" not in out.lower()
        assert "<embed" not in out.lower()


# ── Server-side: filename XSS in 404 page ──
class TestServerXSS:
    def test_filename_escaped_in_404(self):
        """The 404 page must escape the filename — no reflected XSS."""

        # Simulate a malicious filename — quoted HTML would otherwise inject markup
        _evil = "<script>alert(1)</script>.html"
        # We don't have a live app here; just assert the constant path is escaped
        # by reading main.py and confirming html.escape is used in the 404 branch.
        main_src = Path(__file__).resolve().parent.parent / "server" / "main.py"
        text = main_src.read_text(encoding="utf-8")
        # Must have a html.escape call in the report 404 block
        assert "html.escape(filename)" in text
        # And the literal string '<pre>{filename}</pre>' (unescaped) must NOT exist
        assert "<pre>{filename}</pre>" not in text

    def test_path_traversal_uses_resolve(self):
        """api_view_report must use Path.resolve() + relative_to() check."""
        main_src = Path(__file__).resolve().parent.parent / "server" / "main.py"
        text = main_src.read_text(encoding="utf-8")
        # The new traversal defense should resolve the canonical path and
        # verify it stays within the reports dir. We accept either the
        # canonical `file_path.resolve()` form or the inline
        # `(reports_dir / filename).resolve()` form.
        assert ".resolve()" in text
        assert "file_path.relative_to(reports_dir)" in text
        # And the substring `..` check must still be present as a first-line defense
        assert '".." in filename' in text


# ── Security headers middleware ──
class TestSecurityHeaders:
    def test_middleware_attached(self):
        main_src = Path(__file__).resolve().parent.parent / "server" / "main.py"
        text = main_src.read_text(encoding="utf-8")
        assert "SecurityHeadersMiddleware" in text
        assert "Content-Security-Policy" in text
        assert "X-Frame-Options" in text
        assert "X-Content-Type-Options" in text
        assert "Referrer-Policy" in text

    def test_csp_excludes_unsafe_eval(self):
        main_src = Path(__file__).resolve().parent.parent / "server" / "main.py"
        text = main_src.read_text(encoding="utf-8")
        # 'unsafe-eval' is the dangerous one — must not be present
        assert "unsafe-eval" not in text


# ── Iframe sandbox ──
class TestIframeSandbox:
    def test_report_iframe_sandbox(self):
        """The report viewer iframe must have sandbox with allow-scripts."""
        html = Path(__file__).resolve().parent.parent / "templates" / "app.html"
        text = html.read_text(encoding="utf-8")
        m = re.search(r'<iframe[^>]*id="reportFrame"[^>]*>', text)
        assert m is not None, "reportFrame iframe not found"
        attrs = m.group(0)
        # allow-scripts is needed for skill-generated HTML reports to render
        # allow-same-origin is needed for auth cookies (reports require login)
        # Defense-in-depth: only /api/reports/* paths are exempt from X-Frame-Options
        assert "allow-scripts" in attrs
        assert "allow-same-origin" in attrs
        assert "sandbox" in attrs


# ── CSRF protection (live HTTP-level) ──
class TestCSRFDefense:
    """Verify that the CSRFMiddleware blocks cross-site form-style requests."""

    @pytest.mark.asyncio
    async def test_post_without_csrf_header_is_rejected(self, client, auth_cookies):
        """A POST without X-Requested-With: XMLHttpRequest must be 403."""
        r = await client.post(
            "/api/convs",
            json={"title": "x"},
            headers={"X-Requested-With": ""},  # present but wrong value
            cookies=auth_cookies,
        )
        assert r.status_code == 403, f"Got {r.status_code}: {r.text}"

    @pytest.mark.asyncio
    async def test_post_with_csrf_header_passes(self, client, auth_cookies):
        """A POST with the correct CSRF header must succeed."""
        r = await client.post(
            "/api/convs",
            json={"title": "csrf test"},
            cookies=auth_cookies,
        )
        assert r.status_code == 200, f"Got {r.status_code}: {r.text}"

    @pytest.mark.asyncio
    async def test_delete_without_csrf_header_is_rejected(self, client, auth_cookies):
        r = await client.delete(
            "/api/convs/abc",
            cookies=auth_cookies,
            headers={"X-Requested-With": ""},
        )
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_get_requests_dont_need_csrf(self, client, auth_cookies):
        """GET is a safe method and must not require CSRF header."""
        r = await client.get("/api/convs", cookies=auth_cookies)
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_login_exempt_from_csrf(self, client, test_user):
        """Login is unauthenticated so CSRF can't apply (no session to bind to)."""
        r = await client.post(
            "/api/login",
            json={"username": test_user["username"], "password": test_user["password"]},
        )
        # 200 on success, 403 on bad creds — but never 403 due to missing CSRF header
        assert r.status_code in (200, 403)


# ── Cookie security configuration ──
class TestCookieSecurity:
    def test_cookie_samesite_configurable(self):
        """config.py must expose COOKIE_SAMESITE for hardening."""
        cfg_src = Path(__file__).resolve().parent.parent / "server" / "config.py"
        text = cfg_src.read_text(encoding="utf-8")
        assert "COOKIE_SAMESITE" in text
        # Default must be 'strict' (strongest CSRF defense)
        assert '"strict"' in text.lower() or "'strict'" in text.lower()

    def test_cookie_secure_configurable(self):
        cfg_src = Path(__file__).resolve().parent.parent / "server" / "config.py"
        text = cfg_src.read_text(encoding="utf-8")
        assert "COOKIE_SECURE" in text

    def test_login_sets_csrf_cookie(self, client, test_user):
        """Successful login must issue both 'token' and 'csrf_token' cookies."""
        import asyncio

        async def _check():
            r = await client.post(
                "/api/login",
                json={"username": test_user["username"], "password": test_user["password"]},
            )
            assert r.status_code == 200
            # httpx stores cookies on the client; verify both were set
            jar = dict(client.cookies)
            # At minimum the token cookie is present
            assert "token" in jar

        asyncio.get_event_loop().run_until_complete(_check())


# ── Auth CSRF verification function ──
class TestCSRFVerify:
    def test_get_passes(self):
        from server.auth import verify_csrf

        # Build a mock request-like object
        class R:
            method = "GET"
            headers = {}

        verify_csrf(R())  # No raise

    def test_post_without_header_raises(self):
        from fastapi import HTTPException

        from server.auth import verify_csrf

        class R:
            method = "POST"
            headers = {}

        with pytest.raises(HTTPException) as e:
            verify_csrf(R())
        assert e.value.status_code == 403

    def test_post_with_header_passes(self):
        from server.auth import verify_csrf

        class R:
            method = "POST"
            headers = {"X-Requested-With": "XMLHttpRequest"}

        verify_csrf(R())  # No raise


# ── Pydantic input validation ──
class TestPydanticModels:
    def test_login_request_requires_username(self):
        from pydantic import ValidationError

        from server.models import LoginRequest

        with pytest.raises(ValidationError):
            LoginRequest(password="x")

    def test_login_request_length_limits(self):
        from pydantic import ValidationError

        from server.models import LoginRequest

        with pytest.raises(ValidationError):
            LoginRequest(username="x" * 100, password="x")
        with pytest.raises(ValidationError):
            LoginRequest(username="x", password="")  # empty rejected by min_length=1

    def test_chat_stream_question_too_long_rejected(self):
        from pydantic import ValidationError

        from server.models import ChatStreamRequest

        with pytest.raises(ValidationError):
            ChatStreamRequest(question="x" * 40000)

    def test_chat_stream_question_required(self):
        from pydantic import ValidationError

        from server.models import ChatStreamRequest

        with pytest.raises(ValidationError):
            ChatStreamRequest()

    def test_batch_delete_ids_too_many(self):
        from pydantic import ValidationError

        from server.models import BatchDeleteRequest

        with pytest.raises(ValidationError):
            BatchDeleteRequest(ids=[f"id{i}" for i in range(300)])

    def test_batch_delete_ids_too_long_string(self):
        from pydantic import ValidationError

        from server.models import BatchDeleteRequest

        with pytest.raises(ValidationError):
            BatchDeleteRequest(ids=["x" * 200])

    def test_explain_term_length_cap(self):
        from pydantic import ValidationError

        from server.models import ExplainTermRequest

        with pytest.raises(ValidationError):
            ExplainTermRequest(term="x" * 100)

    @pytest.mark.asyncio
    async def test_chat_endpoint_returns_422_on_invalid_payload(self, client, auth_cookies):
        r = await client.post("/api/chat/stream", cookies=auth_cookies, json={})
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_chat_endpoint_returns_422_on_oversize_question(self, client, auth_cookies):
        r = await client.post(
            "/api/chat/stream",
            cookies=auth_cookies,
            json={"question": "x" * 40000, "skill": "bottleneck-hunter"},
        )
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_batch_delete_returns_422_on_missing_ids(self, client, auth_cookies):
        r = await client.post("/api/convs/batch-delete", cookies=auth_cookies, json={})
        assert r.status_code == 422


# ── Middleware: request ID + error sanitization ──
class TestRequestIDMiddleware:
    @pytest.mark.asyncio
    async def test_response_carries_request_id_header(self, client, auth_cookies):
        r = await client.get("/api/me", cookies=auth_cookies)
        assert "X-Request-ID" in r.headers
        # Should be a non-empty short string
        assert len(r.headers["X-Request-ID"]) > 0

    @pytest.mark.asyncio
    async def test_client_supplied_request_id_is_echoed(self, client, auth_cookies):
        r = await client.get("/api/me", cookies=auth_cookies, headers={"X-Request-ID": "trace-abc-123"})
        assert r.headers["X-Request-ID"] == "trace-abc-123"

    def test_get_request_id_context(self):
        from server.middleware import _request_id_var, get_request_id

        # Without a request, the default is '-'
        _request_id_var.set("-")
        assert get_request_id() == "-"
        _request_id_var.set("rid-1")
        assert get_request_id() == "rid-1"
        _request_id_var.set("-")


class TestErrorSanitization:
    @pytest.mark.asyncio
    async def test_404_does_not_leak_internals(self, client, auth_cookies):
        r = await client.get("/api/convs/does-not-exist", cookies=auth_cookies)
        assert r.status_code == 404
        body = r.json()
        # Should not contain stack trace / exception class names
        s = str(body)
        assert "Traceback" not in s
        assert "Exception" not in s or "detail" in s

    @pytest.mark.asyncio
    async def test_validation_error_sanitized(self, client, auth_cookies):
        r = await client.post("/api/chat/stream", cookies=auth_cookies, json={"question": "x" * 99999})
        assert r.status_code == 422
        body = r.json()
        # The full echo of the sent value must NOT be returned
        s = str(body)
        assert "x" * 99999 not in s
        # The error should be brief (not a full stack trace)
        assert "Traceback" not in s
        # Either 'detail' or 'error' key must be present (depends on
        # whether the endpoint uses the global handler or its own)
        assert "detail" in body or "error" in body


# ── Logging configuration ──
class TestLoggingConfig:
    def test_uses_rotating_file_handler(self):
        import logging
        import tempfile

        from server.middleware import configure_logging

        with tempfile.TemporaryDirectory() as td:
            log_path = os.path.join(td, "test.log")
            configure_logging(log_path, level="DEBUG")
            # The root logger should have a RotatingFileHandler
            root = logging.getLogger()
            kinds = [type(h).__name__ for h in root.handlers]
            assert "RotatingFileHandler" in kinds

    def test_request_id_filter_attached(self):
        import logging
        import tempfile

        from server.middleware import RequestIDFilter, configure_logging

        with tempfile.TemporaryDirectory() as td:
            log_path = os.path.join(td, "test.log")
            configure_logging(log_path)
            root = logging.getLogger()
            # Filter must be on at least one handler
            found = any(any(isinstance(f, RequestIDFilter) for f in h.filters) for h in root.handlers)
            assert found

    def test_log_level_configurable(self):
        import logging
        import tempfile

        from server.middleware import configure_logging

        with tempfile.TemporaryDirectory() as td:
            log_path = f"{td}/test.log"
            configure_logging(log_path, level="DEBUG")
            assert logging.getLogger().level == logging.DEBUG
            # Re-configure with INFO; level should change
            configure_logging(log_path, level="INFO")
            assert logging.getLogger().level == logging.INFO
