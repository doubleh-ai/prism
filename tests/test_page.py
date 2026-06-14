"""Test page rendering — Double H branding with StaticFiles"""
import re
import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def page_html(auth_cookies):
    """Fetch the full rendered app page as authenticated user."""
    from server.main import app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/", cookies=auth_cookies)
        assert r.status_code == 200
        return r.text


class TestPageRendering:
    """Smoke tests — prevent white screen of death"""

    def test_html_has_theme_class(self, page_html):
        """<html> must have light or dark class so CSS variables are defined (prevents white screen)."""
        assert 'class="light"' in page_html or 'class="dark"' in page_html, \
            "html must have theme class so CSS variables like var(--bg) resolve"

    def test_html_has_app_shell(self, page_html):
        """Page must contain the app-shell div."""
        assert 'class="app-shell"' in page_html or 'id="appShell"' in page_html

    def test_branding_prism(self, page_html):
        """Page title and branding must use Prism."""
        assert 'Prism' in page_html
        assert 'CapitalLens' not in page_html

    def test_css_linked(self, page_html):
        """CSS is served via link tag (StaticFiles), not inline."""
        assert 'href="/static/css/app.css' in page_html

    def test_js_linked(self, page_html):
        """JS is served via script tag (StaticFiles), not inline."""
        assert 'src="/static/js/app.js' in page_html

    def test_marked_loaded(self, page_html):
        """Marked.js must be available via /static/ for rendering."""
        assert 'marked.min.js' in page_html

    def test_theme_class_would_be_set(self):
        """Theme JS must be in app.js (now external, not inline)."""
        import os
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        js_path = os.path.join(project_root, "static", "js", "app.js")
        with open(js_path, encoding="utf-8") as f:
            assert "documentElement.className" in f.read()

    def test_conv_list_present(self, page_html):
        """Sidebar conversation list must exist."""
        assert 'id="convList"' in page_html

    def test_input_area_present(self, page_html):
        """Input textarea must exist."""
        assert 'id="input"' in page_html

    def test_ac_dropdown_present(self, page_html):
        """Autocomplete dropdown must exist."""
        assert 'id="acDropdown"' in page_html

    def test_stop_button_present(self, page_html):
        """Stop generation button must exist (FIX #14)."""
        assert 'id="stopBtn"' in page_html  # DOM element
        # stopStream function is in external JS
        import os
        js_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "js", "app.js")
        with open(js_path, encoding="utf-8") as f:
            assert 'stopStream' in f.read()

    def test_trash_modal_present(self, page_html):
        """Trash modal must exist (FIX #6)."""
        assert 'id="trashModal"' in page_html
        assert 'trashLink' in page_html

    def test_bulk_bar_present(self, page_html):
        """Bulk delete bar must exist."""
        assert 'id="bulkBar"' in page_html

    def test_copy_button_logic_present(self):
        """Copy button code must exist in app.js and app.css (FIX #15)."""
        import os
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        js_path = os.path.join(project_root, "static", "js", "app.js")
        with open(js_path, encoding="utf-8") as f:
            assert '_addCopyButtons' in f.read()
        css_path = os.path.join(project_root, "static", "css", "app.css")
        with open(css_path, encoding="utf-8") as f:
            assert 'copy-btn' in f.read()

    def test_stream_error_classification(self):
        """Error classification logic must be present in app.js (FIX #2)."""
        import os
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        js_path = os.path.join(project_root, "static", "js", "app.js")
        with open(js_path, encoding="utf-8") as f:
            js = f.read()
        assert 'AbortError' in js, "Must handle AbortError (stop generation)"
        assert '网络连接失败' in js, "Must show network error message"

    def test_smart_scroll_logic(self):
        """Smart scroll must be implemented in app.js (user feature request)."""
        import os
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        js_path = os.path.join(project_root, "static", "js", "app.js")
        with open(js_path, encoding="utf-8") as f:
            js = f.read()
        assert '_userScrolledUp' in js

    def test_typewriter_effect(self):
        """Typewriter character-by-character reveal must be in app.js."""
        import os
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        js_path = os.path.join(project_root, "static", "js", "app.js")
        with open(js_path, encoding="utf-8") as f:
            js = f.read()
        assert '_startTypewriter' in js, "Typewriter start function must exist"
        assert '_stopTypewriter' in js, "Typewriter stop function must exist"
        assert '_displayLen' in js, "Display length counter must exist for typewriter"

    def test_recycle_bin_ui(self, page_html):
        """Recycle bin UI elements must exist."""
        assert 'openTrash' in page_html  # onclick handler in HTML
        # restoreConv and permDeleteConv are in external JS
        import os
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        js_path = os.path.join(project_root, "static", "js", "app.js")
        with open(js_path, encoding="utf-8") as f:
            js = f.read()
        assert 'restoreConv' in js
        assert 'permDeleteConv' in js


class TestStaticFiles:
    """Static files are served via FastAPI StaticFiles mount"""

    async def test_css_static_served(self, auth_cookies):
        """CSS file should be accessible via /static/ path."""
        from server.main import app
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/static/css/app.css")
            assert r.status_code == 200

    async def test_js_static_served(self, auth_cookies):
        """JS file should be accessible via /static/ path."""
        from server.main import app
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/static/js/app.js")
            assert r.status_code == 200


class TestLoginPage:
    """Login page specific tests — prevent white screen"""

    async def test_login_page_has_theme_class(self):
        """Login page <html> must have theme class (CSS variables depend on it)."""
        from server.main import app
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/login")
            assert r.status_code == 200
            assert 'class="light"' in r.text, "Login page must have html class='light' for CSS vars"

    async def test_login_page_has_css(self):
        """Login page must reference CSS file."""
        from server.main import app
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/login")
            assert 'app.css' in r.text, "Login page must include CSS reference"
