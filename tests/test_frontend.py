"""Test frontend rendering logic — run with Node.js or as pure logic tests"""

from pathlib import Path

# Simulate the frontend _renderMsg logic in Python for testing
# This mirrors app.js _renderMsg (post-refactor: no stable/live split)


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def clean_detail(d):
    if not d:
        return ""
    return d.replace("['", "").replace("']", "").replace('["', "").replace('"]', "")


def render_msg(chunks, full_text="", streaming=False, refs=None):
    """Mirror of _renderMsg in Python for testing (post-refactor)."""
    if not chunks:
        return full_text or ""

    html = ""
    pending_tools = []
    pending_thinks = []
    collected_refs = refs or []  # References collected for end-of-message display

    def flush_tools(last_is_now):
        nonlocal html
        if not pending_tools:
            return
        n = len(pending_tools)
        if last_is_now and streaming:
            # Streaming: collapse completed tools, show only the active step.
            done = pending_tools[:-1]
            html += '<div class="step-bar">'
            if done:
                html += '<details class="step-fold"><summary>已完成 ' + str(len(done)) + " 步</summary>"
            for td in done:
                detail_html = " <i>" + esc(td["detail"]) + "</i>" if td.get("detail") else ""
                html += '<span class="step-tag"><b>' + esc(td["name"]) + "</b>" + detail_html + "</span>"
            if done:
                html += "</details>"
            last = pending_tools[-1]
            ld = " <i>" + esc(last["detail"]) + "</i>" if last.get("detail") else ""
            html += (
                '<span class="step-tag now step-now"><b>' + esc(last["name"]) + "</b>" + ld + "</span></div>"
            )
        else:
            # Non-streaming or completed: fold all steps
            html += (
                '<div class="step-bar stable"><details class="step-fold"><summary>已完成 '
                + str(n)
                + " 步</summary>"
            )
            for td in pending_tools:
                detail_html = " <i>" + esc(td["detail"]) + "</i>" if td.get("detail") else ""
                html += '<span class="step-tag"><b>' + esc(td["name"]) + "</b>" + detail_html + "</span>"
            html += "</details></div>"
        pending_tools.clear()

    def flush_thinks():
        nonlocal html
        if not pending_thinks:
            return
        for think_text in pending_thinks:
            html += (
                '<details class="think-box" open><summary>Thinking</summary><div class="think-content">'
                + esc(think_text)
                + "</div></details>"
            )
        pending_thinks.clear()

    for ci, c in enumerate(chunks):
        t = c.get("t")
        if t == "tool":
            pending_tools.append({"name": c.get("c", "?"), "detail": clean_detail(c.get("d", ""))})
        elif t == "think":
            flush_tools(False)
            pending_thinks.append(c.get("c", ""))
        elif t == "ask":
            flush_tools(False)
            flush_thinks()
            html += (
                '<div class="ask-card"><div class="ask-q">💬 '
                + esc(c.get("q", ""))
                + '</div><div class="ask-opts">'
            )
            for opt in c.get("opts", []):
                html += '<button class="ask-opt">' + esc(opt) + "</button>"
            html += "</div></div>"
        elif t == "ref":
            # Reference citation — skip during streaming, shown at end
            collected_refs.append(
                {
                    "url": c.get("url", ""),
                    "title": c.get("title", c.get("url", "")),
                    "snippet": c.get("snippet", ""),
                }
            )
        else:
            is_last_text = streaming and ci == len(chunks) - 1
            # Also check: if there are trailing non-text chunks, it's not the last text
            for cj in range(ci + 1, len(chunks)):
                if chunks[cj].get("t") not in ("tool", "ref"):
                    is_last_text = False
                    break
            flush_tools(False)
            flush_thinks()
            html += c.get("c", "") + ('<span class="stream-cursor"></span>' if is_last_text else "")

    if streaming:
        flush_tools(True)
        flush_thinks()
        if html == "":
            html = '<div class="shimmer-bar" style="width:60%"></div>'
    else:
        flush_tools(False)
        flush_thinks()

    # Append reference section if we have refs and this is the final render
    if collected_refs and not streaming:
        html += (
            '<div class="ref-section"><div class="ref-title">参考来源 ('
            + str(len(collected_refs))
            + ')</div><div class="ref-list">'
        )
        for ri, ref in enumerate(collected_refs):
            html += (
                '<div class="ref-item"><span class="ref-idx">'
                + str(ri + 1)
                + '</span><div><a class="ref-link" href="'
                + esc(ref["url"])
                + '" target="_blank" rel="noopener">'
                + esc(ref["title"])
                + "</a>"
            )
            if ref.get("snippet"):
                html += '<div class="ref-snippet">' + esc(ref["snippet"]) + "</div>"
            html += "</div></div>"
        html += "</div></div>"

    return html


class TestRenderMsg:
    def test_empty_chunks_returns_text(self):
        assert render_msg([], "plain text") == "plain text"

    def test_single_tool(self):
        chunks = [{"t": "tool", "c": "Bash", "d": "['ls -la']"}]
        result = render_msg(chunks, streaming=True)
        assert "Bash" in result
        assert "ls -la" in result
        assert "step-now" in result  # streaming, so it's "now"
        assert "已完成" not in result

    def test_multiple_tools_folded(self):
        chunks = [
            {"t": "tool", "c": "Bash", "d": "['cmd1']"},
            {"t": "tool", "c": "Read", "d": "['file.md']"},
            {"t": "tool", "c": "WebSearch", "d": "['query']"},
        ]
        result = render_msg(chunks, streaming=False)
        # All 3 tools should be counted
        assert "已完成 3 步" in result
        # Details should be present
        assert "cmd1" in result
        assert "file.md" in result
        assert "query" in result

    def test_streaming_shows_now_step(self):
        chunks = [
            {"t": "tool", "c": "Bash", "d": "['cmd1']"},
            {"t": "tool", "c": "Read", "d": "['file.md']"},
        ]
        result = render_msg(chunks, streaming=True)
        assert "step-now" in result
        assert "Read" in result
        assert "已完成 1 步" in result

    def test_streaming_collapses_completed_tools(self):
        chunks = [
            {"t": "tool", "c": "Write", "d": "['report.html']"},
            {"t": "tool", "c": "Bash", "d": "['ls reports']"},
            {"t": "tool", "c": "TaskUpdate", "d": "['done']"},
        ]
        result = render_msg(chunks, streaming=True)
        assert '<details class="step-fold"><summary>已完成 2 步</summary>' in result
        assert result.count("step-now") == 1
        assert result.rfind("</details>") < result.rfind("step-now")

    def test_completed_no_now_step(self):
        chunks = [
            {"t": "tool", "c": "Bash", "d": "['cmd1']"},
        ]
        result = render_msg(chunks, streaming=False)
        assert "step-now" not in result
        assert "Bash" in result

    def test_tool_then_text(self):
        chunks = [
            {"t": "tool", "c": "Bash", "d": "['cmd1']"},
            {"t": "text", "c": "分析结果：这是一段正文。"},
        ]
        result = render_msg(chunks, streaming=False)
        assert "已完成 1 步" in result
        assert "分析结果" in result

    def test_text_then_tool_then_text(self):
        chunks = [
            {"t": "tool", "c": "Bash", "d": "['cmd1']"},
            {"t": "text", "c": "第一段正文。"},
            {"t": "tool", "c": "WebSearch", "d": "['query']"},
            {"t": "text", "c": "第二段正文。"},
        ]
        result = render_msg(chunks, streaming=False)
        assert "已完成 1 步" in result  # First group: 1 tool
        assert "第一段正文" in result
        assert "第二段正文" in result

    def test_think_blocks_wrapped(self):
        chunks = [
            {"t": "think", "c": "正在思考如何分析这个问题…"},
            {"t": "text", "c": "思考完毕，以下是分析。"},
        ]
        result = render_msg(chunks)
        assert "think-box" in result
        assert "Thinking" in result
        assert "正在思考" in result
        assert "思考完毕" in result

    def test_ask_user_question(self):
        chunks = [
            {"t": "ask", "c": "AskUserQuestion", "q": "请选择一个方向", "opts": ["A", "B", "C"]},
        ]
        result = render_msg(chunks)
        assert "ask-card" in result
        assert "请选择一个方向" in result
        assert "A" in result
        assert "B" in result
        assert "C" in result
        assert "ask-opt" in result

    def test_no_detail_handled(self):
        chunks = [
            {"t": "tool", "c": "TaskCreate", "d": ""},
        ]
        result = render_msg(chunks)
        assert "TaskCreate" in result
        # No <i> tag for empty detail
        assert "<i>" not in result

    def test_long_detail_not_truncated_by_js(self):
        """JS cleanDetail no longer hard-truncates; CSS handles overflow."""
        long_detail = "['" + "x" * 80 + "']"
        result = clean_detail(long_detail)
        # Must preserve the full content (80 x's)
        assert "x" * 80 in result
        assert len(result) >= 80


class TestCleanDetail:
    def test_python_list_format(self):
        assert clean_detail("['hello world']") == "hello world"

    def test_double_quote_format(self):
        assert clean_detail('["hello world"]') == "hello world"

    def test_empty(self):
        assert clean_detail("") == ""
        assert clean_detail(None) == ""

    def test_multiple_values(self):
        # Only first value is captured by backend
        assert clean_detail("['first', 'second']") == "first', 'second"


class TestEscaping:
    def test_html_chars(self):
        assert esc("<script>") == "&lt;script&gt;"
        assert esc("a & b") == "a &amp; b"

    def test_no_escaping_needed(self):
        assert esc("hello world") == "hello world"
        assert esc("中文测试") == "中文测试"


class TestAnalysisStreamFrontendContract:
    def test_analysis_stream_uses_backend_label_and_filters_messages(self):
        src = open("static/js/app.analysis.js", encoding="utf-8").read()
        assert "_analysisEventLabel" in src
        assert "ev.label" in src
        assert "/^messages$/i.test(title)" in src
        assert "_appendStreamItem(ev.agent, ev.node, ev.text, ev.label)" in src


class TestStreamingStability:
    """Streaming updates must not cause stable portions to flicker."""

    def test_no_stable_split_avoids_duplicate_bars(self):
        """Without stable/live split, the entire message renders in one pass.
        This test verifies that two different streaming renders produce
        consistent step-bar structure (no duplicate or nested bars)."""
        chunks1 = [
            {"t": "tool", "c": "Bash", "d": "['cmd1']"},
            {"t": "text", "c": "text."},
            {"t": "tool", "c": "WebSearch", "d": "['q1']"},
        ]
        chunks2 = chunks1 + [{"t": "tool", "c": "Read", "d": "['f1']"}]

        html1 = render_msg(chunks1, streaming=True)
        html2 = render_msg(chunks2, streaming=True)

        # Both should have exactly one step-bar each
        assert html1.count('<div class="step-bar">') == 1
        assert html2.count('<div class="step-bar">') == 1

    def test_only_live_has_now(self):
        """Only the trailing (live) step-bar should have 'now' class."""
        chunks = [
            {"t": "tool", "c": "Bash", "d": "['cmd1']"},
            {"t": "text", "c": "text."},
            {"t": "tool", "c": "WebSearch", "d": "['q1']"},
            {"t": "tool", "c": "Read", "d": "['f1']"},
        ]
        html = render_msg(chunks, streaming=True)

        # Count 'step-now' occurrences — must be exactly 1
        now_count = html.count("step-now")
        assert now_count == 1, f"Expected 1 'step-now', got {now_count}"

    def test_summary_has_no_triangle_char(self):
        """Summary text must NOT contain ▸ (CSS ::after provides ▾ instead)."""
        chunks = [
            {"t": "tool", "c": "Bash", "d": "['cmd1']"},
            {"t": "tool", "c": "Read", "d": "['file']"},
        ]
        html = render_msg(chunks, streaming=False)
        assert "▸" not in html, "Summary should not contain ▸ character"
        assert "已完成 2 步" in html

    def test_no_tools_produces_no_step_bar(self):
        """Message with only text should have no step-bar."""
        chunks = [{"t": "text", "c": "just text."}]
        html = render_msg(chunks)
        assert "step-bar" not in html
        assert "just text" in html


class TestThinkingMarkdown:
    """Thinking content must be rendered as markdown (FIX #4)."""

    def test_think_block_uses_think_content_class(self):
        """Think block should use think-content div for markdown rendering."""
        chunks = [
            {"t": "think", "c": "**bold** and *italic* thinking."},
            {"t": "text", "c": "Response."},
        ]
        html = render_msg(chunks)
        assert "think-content" in html
        assert "think-box" in html

    def test_think_preserves_markdown(self):
        """The Python test doesn't render markdown (no marked.parse),
        but the think-content wrapper must be present."""
        chunks = [{"t": "think", "c": "## Heading\n- item 1\n- item 2"}]
        html = render_msg(chunks)
        assert "think-content" in html
        assert "Heading" in html
        assert "item 1" in html

    def test_multiple_think_blocks(self):
        chunks = [
            {"t": "think", "c": "First thought."},
            {"t": "text", "c": "Response 1."},
            {"t": "think", "c": "Second thought."},
            {"t": "text", "c": "Response 2."},
        ]
        html = render_msg(chunks)
        # Two think-box elements
        assert html.count("think-box") == 2
        assert "First thought" in html
        assert "Second thought" in html


class TestSmartScroll:
    """Smart scroll behavior: don't force-scroll when user reads history."""

    def test_streaming_text_generates_cursor(self):
        """Streaming text should have stream-cursor at end."""
        chunks = [{"t": "text", "c": "streaming..."}]
        html = render_msg(chunks, streaming=True)
        assert "stream-cursor" in html

    def test_non_streaming_no_cursor(self):
        """Non-streaming text should NOT have cursor."""
        chunks = [{"t": "text", "c": "done."}]
        html = render_msg(chunks, streaming=False)
        assert "stream-cursor" not in html

    def test_user_scrolled_up_flag_in_js(self):
        """Smart scroll flag lives across app.core.js + app.chat.js (v0.8 split)."""
        import os

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        js_content = ""
        for f in ("app.core.js", "app.chat.js"):
            with open(os.path.join(project_root, "static", "js", f), encoding="utf-8") as fh:
                js_content += fh.read()
        assert "_userScrolledUp" in js_content
        assert "_scrollToBottom" in js_content
        assert "dist > 80" in js_content  # scroll threshold

    def test_stop_button_in_js(self):
        """Stop generation logic lives in app.chat.js (v0.8 split)."""
        import os

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(project_root, "static", "js", "app.chat.js"), encoding="utf-8") as f:
            js_content = f.read()
        assert "stopStream" in js_content
        assert "AbortError" in js_content  # stop generation path

    def test_error_classification_in_js(self):
        """Error classification lives in app.chat.js (v0.8 split)."""
        import os

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(project_root, "static", "js", "app.chat.js"), encoding="utf-8") as f:
            js_content = f.read()
        assert "AbortError" in js_content, "Must handle AbortError (stop generation)"
        assert "网络连接失败" in js_content, "Must show network error message"


class TestReferencesRendering:
    """Reference citations (ref chunks) must render at end of messages."""

    def test_refs_rendered_at_end(self):
        """Reference citations should appear as a ref-section at the end."""
        chunks = [
            {"t": "tool", "c": "WebSearch", "d": "['query']"},
            {"t": "text", "c": "分析结果。"},
        ]
        refs = [
            {"url": "https://example.com/article1", "title": "Article 1", "snippet": "This is a snippet."},
            {"url": "https://example.com/article2", "title": "Article 2", "snippet": ""},
        ]
        result = render_msg(chunks, refs=refs, streaming=False)
        assert "ref-section" in result
        assert "参考来源 (2)" in result
        assert "example.com/article1" in result
        assert "Article 1" in result
        assert "This is a snippet" in result
        assert "example.com/article2" in result

    def test_refs_not_rendered_during_streaming(self):
        """References should NOT render in the ref-section during streaming."""
        chunks = [
            {"t": "tool", "c": "WebSearch", "d": "['query']"},
            {"t": "text", "c": "streaming text..."},
        ]
        refs = [{"url": "https://example.com", "title": "Example"}]
        result = render_msg(chunks, refs=refs, streaming=True)
        assert "ref-section" not in result

    def test_ref_chunks_collected(self):
        """ref-type chunks should be collected and rendered."""
        chunks = [
            {"t": "tool", "c": "WebSearch", "d": "['query']"},
            {"t": "text", "c": "分析结果。"},
            {"t": "ref", "url": "https://example.com", "title": "Example", "snippet": "Snippet text."},
        ]
        result = render_msg(chunks, streaming=False)
        assert "ref-section" in result
        assert "参考来源 (1)" in result
        assert "Example" in result
        assert "Snippet text" in result


class TestNoFlickerStable:
    """Verify removal of stable/live split prevents flicker."""

    def test_no_stable_key_in_js(self):
        """The _stableKey/_stableHtml split logic must be removed (check all v0.8 modules)."""
        import os

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        js_content = ""
        for f in ("app.util.js", "app.core.js", "app.chat.js", "app.market.js"):
            with open(os.path.join(project_root, "static", "js", f), encoding="utf-8") as fh:
                js_content += fh.read()
        assert "_stableKey" not in js_content, "stable/live split should be removed to prevent flicker"
        assert "_stableHtml" not in js_content, "stable/live split should be removed to prevent flicker"

    def test_prism_byline_in_js(self):
        """The assistant byline must use 'Prism' branding (not CapitalLens). Check all v0.8 modules."""
        import os

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        js_content = ""
        for f in ("app.util.js", "app.core.js", "app.chat.js", "app.market.js"):
            with open(os.path.join(project_root, "static", "js", f), encoding="utf-8") as fh:
                js_content += fh.read()
        assert "Prism" in js_content, "JS must use Prism branding"
        assert "CapitalLens" not in js_content, "JS must not contain old CapitalLens branding"


class TestReleaseMarkdownRendering:
    """Release notes should render markdown instead of escaped source text."""

    def test_release_notes_use_safe_inline_markdown_renderer(self):
        import os

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(project_root, "static", "js", "app.release.js"), encoding="utf-8") as fh:
            js = fh.read()
        assert "_renderReleaseMarkdownInline" in js
        assert "marked.parseInline" in js
        assert "sanitizeHtml" in js
        assert "_renderReleaseMarkdownInline(item)" in js
        assert "_renderReleaseMarkdownInline(x)" in js


class TestChatObjectArtifactGuard:
    """Live chat rendering should strip Claude CLI object placeholders."""

    def test_chat_renderer_exposes_clean_stringifier(self):
        with open("static/js/app.chat-render.js", encoding="utf-8") as f:
            js = f.read()
        assert "v0.19.7" in js
        assert "clean: _clean" in js
        assert "typeof v === 'string') return _stripObjectArtifacts(v, false)" in js
        assert "return _stripObjectArtifacts(_s(t), true)" in js

    def test_streaming_path_uses_cleaned_chunks(self):
        with open("static/js/app.chat.js", encoding="utf-8") as f:
            js = f.read()
        assert "ChatRender.clean(p.error)" in js
        assert "var toolName = ChatRender.clean(p.tool || '')" in js
        assert "var td = ChatRender.clean(p.d || '')" in js
        assert "if (tc) chunks.push({t:'think',c:tc});" in js
        assert "if (pt) {" in js

    def test_streaming_path_has_stall_guard(self):
        with open("static/js/app.chat.js", encoding="utf-8") as f:
            js = f.read()
        assert "lastStreamEventAt" in js
        assert "streamTimedOut" in js
        assert "超过 7 分钟没有收到新内容" in js
        assert "reader.cancel()" in js

    def test_cached_and_rendered_artifacts_are_sanitized(self):
        with open("static/js/app.core.js", encoding="utf-8") as f:
            core = f.read()
        with open("static/js/app.chat.js", encoding="utf-8") as f:
            chat = f.read()
        assert "var DB_VERSION = 3" in core
        assert "cacheKey(id)" in core
        assert "String(App.user || '')" in core
        assert "db.deleteObjectStore('convs')" in core
        assert "_sanitizeConvPayload(req.result.data)" in core
        assert "_sanitizeConvPayload(data)" in core
        assert "_sanitizeRenderedArtifacts" in chat
        assert "document.createTreeWalker" in chat
        assert "ChatRender.clean(n.nodeValue)" in chat
        assert "this._sanitizeRenderedArtifacts(el)" in chat
        assert "this._sanitizeRenderedArtifacts(bubbles[idx])" in chat

    def test_message_scroll_area_keeps_composer_clearance(self):
        with open("static/css/app.css", encoding="utf-8") as f:
            css = f.read()
        assert ".msgs{height:100%;overflow-y:auto;padding:var(--sp-8) 0 36px" in css
        assert "scroll-padding-bottom:36px" in css

    def test_footer_icons_are_not_replaced_by_text(self):
        with open("static/js/app.core.js", encoding="utf-8") as f:
            core = f.read()
        with open("static/css/app.css", encoding="utf-8") as f:
            css = f.read()
        assert "link.setAttribute('data-count', String(trash.length))" in core
        assert "link.textContent = '回收站" not in core
        assert ".footer-icon-link[data-count]:after" in css


class TestServiceWorkerCachePolicy:
    """Deploys should not get stuck behind an old cached app shell."""

    def test_sw_uses_network_first_for_navigation(self):
        import os

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(project_root, "static", "sw.js"), encoding="utf-8") as fh:
            sw = fh.read()
        assert "prism-workbench-v93" in sw
        assert "ev.request.mode === 'navigate'" in sw
        assert "cache: 'no-store'" in sw
        assert "client.navigate(client.url)" not in sw
        assert "'/'" not in sw.split("const PRE_CACHE = [", 1)[1].split("];", 1)[0]

    def test_app_registers_sw_without_update_cache(self):
        import os

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(project_root, "static", "js", "app.core.js"), encoding="utf-8") as fh:
            js = fh.read()
        assert "updateViaCache: 'none'" in js
        assert "window.location.reload()" not in js


def test_indicator_overlay_reuses_current_chart_kline():
    """Indicator overlay should not refetch K-line when the chart already has data."""
    with open("templates/app.html", encoding="utf-8") as f:
        template = f.read()
    with open("static/js/app.indicators.js", encoding="utf-8") as f:
        js = f.read()
    with open("static/js/app.indicator-chart.js", encoding="utf-8") as f:
        chart_js = f.read()
    with open("static/js/app.market.js", encoding="utf-8") as f:
        market_js = f.read()
    with open("static/sw.js", encoding="utf-8") as f:
        sw = f.read()

    assert "app.indicator-chart.js?v=93" in template
    assert "app.indicator-chart.js" in sw
    assert "payload.kline" in js
    assert "this._lastKline.slice(-200)" in js
    assert 'period = this._currentPeriod || "1d"' in js
    assert "params: params || {}" in js
    assert "_indicatorTracks" in js
    assert "_updateIndicatorParam" in js
    assert "App.renderKlineChart = function(container, kline)" in chart_js
    assert "tooltipFormatter" in chart_js
    assert "MA60" in chart_js
    assert "振幅" in chart_js
    assert "markLine" in chart_js
    assert "_toggleKlineFullscreen" in market_js
    assert "_resetKlineZoom" in market_js
    assert "markArea" in chart_js
    assert "指标信号" in chart_js


def test_industry_workbench_static_contract():
    """Industry graph should render as a research workbench, not a plain map/list."""
    with open("templates/app.html", encoding="utf-8") as f:
        template = f.read()
    with open("static/js/app.industry.js", encoding="utf-8") as f:
        js = f.read()
    with open("static/css/app.css", encoding="utf-8") as f:
        css = f.read()
    with open("static/js/app.core.js", encoding="utf-8") as f:
        core = f.read()

    assert 'data-mode="industry"' in template
    assert "app.industry.js?v=93" in template
    assert "/api/industry/overview" in js
    assert "/api/industry/context" in js
    assert "_renderIndustryOverview" in js
    assert "_renderIndustryChainDetail" in js
    assert "_renderIndustryNodeDrawer" in js
    assert "_refreshIndustryTheme" in js
    assert "this._refreshIndustryTheme()" in core
    assert "产业链研究工作台" in js
    assert "产业雷达" in js
    assert "跨模块已同步" in js
    assert "待归链" in js
    assert "_scheduleIndustryContext" in js
    assert "_renderIndustryTickerBadges" in js
    assert "_renderIndustryDrawerContext" in js
    assert "industryGeoMap" in js
    assert "industryStageEdges" in js
    assert "t.tier !== s.tier + 1" in js
    assert 'data-tier="' in js
    assert ".industry-workbench" in css
    assert ".industry-chain-grid" in css
    assert ".industry-stage-cols" in css
    assert ".industry-drawer-metrics" in css
    assert ".industry-context-strip" in css
    assert ".industry-context-grid" in css
    assert ".industry-chain-context" in css
    assert ".industry-drawer-context" in css
    assert ".light .industry-kpi" in css
    assert ".light .industry-section-hd h3" in css
    assert ".light .ind-drawer" in css


def test_client_diagnostics_and_admin_bundle_static_contract():
    template = Path("templates/app.html").read_text(encoding="utf-8")
    diagnostics = Path("static/js/app.diagnostics.js").read_text(encoding="utf-8")
    admin = Path("static/js/app.admin.js").read_text(encoding="utf-8")
    sw = Path("static/sw.js").read_text(encoding="utf-8")

    assert "app.diagnostics.js?v=93" in template
    assert "render_object_artifact" in diagnostics
    assert "chart_blank" in diagnostics
    assert "/api/diagnostics/client" in diagnostics
    assert "复制 Codex 诊断包" in admin
    assert "/api/admin/diagnostics/bundle" in admin
    assert "app.diagnostics.js" in sw


def test_admin_health_panel_static_contract():
    """Admin console should request and render system health."""
    with open("static/js/app.admin.js", encoding="utf-8") as f:
        js = f.read()
    with open("static/css/app.css", encoding="utf-8") as f:
        css = f.read()
    assert "/api/admin/health" in js
    assert "_renderAdminHealth" in js
    assert "系统健康" in js
    assert "/api/admin/analytics/overview" in js
    assert "/api/admin/analytics/insights" in js
    assert "/api/admin/analytics/events" in js
    assert "/api/admin/analytics/users" in js
    assert "_renderAdminAnalytics" in js
    assert "_renderAdminCharts" in js
    assert "_renderAdminFunnels" in js
    assert "_renderAdminTopTargets" in js
    assert "_renderAdminPerformance" in js
    assert "_renderAdminEventTimeline" in js
    assert "_adminSetActivityFilter" in js
    assert "_adminLoadMoreEvents" in js
    assert "_adminExportEvents" in js
    assert "行为时间线" in js
    assert "功能使用矩阵" in js
    assert "核心路径转化" in js
    assert ".admin-chart-grid" in css
    assert ".admin-insight-grid" in css
    assert ".admin-tabs" in css
    assert ".admin-event-filters" in css


def test_privacy_safe_ui_activity_tracking_contract():
    with open("server/activity.py", encoding="utf-8") as f:
        activity = f.read()
    with open("server/main.py", encoding="utf-8") as f:
        main = f.read()
    with open("static/js/app.core.js", encoding="utf-8") as f:
        core = f.read()
    with open("static/js/app.market.js", encoding="utf-8") as f:
        market = f.read()

    assert '"/api/activity"' in activity
    assert "_UI_ACTIVITY_RULES" in main
    assert "ActivityTrackRequest" in main
    assert "_trackUiEvent" in core
    assert "watchlist_add" in market
    assert "watchlist_remove" in market


def test_login_and_app_share_release_cache_version():
    with open("templates/login.html", encoding="utf-8") as f:
        login = f.read()
    with open("templates/app.html", encoding="utf-8") as f:
        app = f.read()

    assert "app.css?v=93" in login
    assert "app.css?v=93" in app


def test_native_stock_entry_preserves_only_historical_analysis_rendering():
    """New execution is Native; old analysis metadata stays readable."""
    with open("templates/app.html", encoding="utf-8") as f:
        html = f.read()
    with open("static/js/app.analysis.js", encoding="utf-8") as f:
        js = f.read()
    with open("static/css/app.css", encoding="utf-8") as f:
        css = f.read()

    assert 'id="analysisMode"' not in html
    assert 'id="analysisForceRefresh"' not in html
    assert "2 Agent" not in html and "7 Agent" not in html
    assert "使用 Prism Native" in html
    assert "await this.newConv()" in js
    assert "await this.send()" in js
    assert '"/api/analysis/start"' not in js
    assert "_analysisAgentsForMode" in js
    assert "_setAnalysisMode" in js
    assert 'setAttribute("aria-pressed"' in js
    assert 'ev.cached ? "使用缓存报告"' in js
    assert "_analysisMetaHtml" in js
    assert "_analysisTextValue" in js
    assert 'replace(/^\\s*\\[object Object\\]\\s*$/gm, "")' in js
    assert 'App._renderMarkdown(ar[ak] || "")' in js
    assert ".analysis-options" in css
    assert ".analysis-mode-segment" in css
    assert ".analysis-mode-btn.active" in css
    assert ".analysis-mode-select option" not in css
    assert ".ap-meta" in css


def test_market_and_analysis_search_discard_stale_responses():
    with open("static/js/app.market.js", encoding="utf-8") as f:
        js = f.read()

    assert "_analysisSearchSeq" in js
    assert "_marketSearchSeq" in js
    assert "_analysisRunSearch(q, seq)" in js
    assert "_marketSidebarRunSearch(q, seq)" in js
    assert "seq && seq !== this._analysisSearchSeq" in js
    assert "seq && seq !== this._marketSearchSeq" in js
    assert 'document.getElementById("analysisQ")' in js
    assert "document.getElementById('marketSidebarQ')" in js


def test_watchlist_add_remove_static_contract():
    """Watchlist add/remove should be visible, touch-friendly, and robust to ticker variants."""
    with open("static/js/app.market.js", encoding="utf-8") as f:
        market = f.read()
    with open("static/js/app.vibe.js", encoding="utf-8") as f:
        vibe = f.read()
    with open("static/css/app.css", encoding="utf-8") as f:
        css = f.read()

    assert "_normalizeTicker" in market
    assert "SH|SZ|BJ" in market
    assert "_normalizeWatchEntry" in market
    assert "return true;" in market
    assert "return false;" in market
    assert "ev.target.closest('.market-hit__add')" in market
    assert "addBtn.disabled = true" in market
    assert "移除自选" in market
    assert ".watchlist-item{display:grid;grid-template-columns:auto 1fr auto auto auto" in css
    assert ".watchlist-item__del{width:22px;height:22px;display:flex" in css
    assert "var didAdd = await this._addToWatchlist(ticker, name)" in vibe


def test_full_market_screener_workbench_static_contract():
    with open("templates/app.html", encoding="utf-8") as f:
        html = f.read()
    with open("static/js/app.screener.js", encoding="utf-8") as f:
        screener = f.read()
    with open("static/js/app.indicators.js", encoding="utf-8") as f:
        indicators = f.read()
    with open("static/css/app.css", encoding="utf-8") as f:
        css = f.read()
    with open("static/sw.js", encoding="utf-8") as f:
        sw = f.read()

    assert 'data-market-tab="quote"' in html
    assert 'data-market-tab="screener"' in html
    assert 'data-market-tab="library"' in html
    assert 'id="marketScanIndicator"' in html
    assert 'id="marketScanParams"' in html
    assert 'id="marketScanResults"' in html
    assert 'id="marketScanHistory"' in html
    assert 'id="indicatorPreview"' in html
    assert "app.screener.js?v=93" in html
    assert '"/api/indicators/" + encodeURIComponent(id) + "/scan-jobs"' in screener
    assert '"/api/indicators/universe/summary?markets="' in screener
    assert "_scheduleScanPoll" in screener
    assert "preferredDirection" in screener
    assert "_planTurnover" in indicators
    assert "_indicatorParamControl" in indicators
    assert "_indicatorParamBlocks" in indicators
    assert "_indicatorLogicText" in indicators
    assert "data-param-preset" in indicators
    assert "data-param-set" in indicators
    assert "核心指标参数" in indicators
    assert "扫描方案" in indicators
    assert "_scannerLogicText" in screener
    assert "_renderMarketScanResults" in screener
    assert "_renderScannerParams" in screener
    assert "_openIndicatorEditor" in indicators
    assert 'method: editing ? "PUT" : "POST"' in indicators
    assert ".market-screener-layout" in css
    assert ".indicator-library-layout" in css
    assert ".mi-param-choices" in css
    assert ".mi-family-line" in css
    assert ".mi-config-section" in css
    assert ".market-scan-logic" in css
    assert ".market-detail-layout" in css
    assert ".market-scan-row-head{display:none}" in css
    assert "app.screener.js" in sw


def test_mobile_and_foldable_layout_static_contract():
    """Mobile layout should use a bottom nav, drawer sidebar, and safe-area padding."""
    with open("templates/app.html", encoding="utf-8") as f:
        html = f.read()
    with open("static/css/app.css", encoding="utf-8") as f:
        css = f.read()
    with open("static/js/app.core.js", encoding="utf-8") as f:
        core = f.read()
    with open("static/js/app.market.js", encoding="utf-8") as f:
        market = f.read()

    assert "viewport-fit=cover" in html
    assert "App.toggleSidebar()" in html
    assert "@media(max-width:1180px)" in css
    assert "@media(max-width:900px)" in css
    assert ".icon-rail{position:fixed" in css
    assert ".app-shell.mobile-sidebar-open .sidebar" in css
    assert "env(safe-area-inset-bottom)" in css
    assert ".ap-agent-grid,.ap-tiles,.ap-trade-grid" in css
    assert ".vibe-home,.vibe-plan,.reports-lib" in css
    assert "_isCompactViewport" in core
    assert "toggleSidebar: function(force)" in core
    assert "mobile-sidebar-open" in core
    assert "this._closeMobileSidebar();" in core
    assert "needsDrawer = mode !== 'chat' && mode !== 'industry'" in market


def test_completed_tool_steps_are_collapsed_static_contract():
    """Completed command/tool steps should not stay visibly expanded in chat."""
    with open("static/js/app.chat-render.js", encoding="utf-8") as f:
        js = f.read()
    with open("static/css/app.css", encoding="utf-8") as f:
        css = f.read()

    assert "var completed = streaming ? steps.slice(0, Math.max(0, n - 1)) : steps;" in js
    assert "已完成 '+completed.length+' 步" in js
    assert "stepTag(last, ' now step-now')" in js
    assert ".step-fold:not([open]) .step-tag{display:none}" in css


def test_kline_periods_and_a_share_color_semantics_static_contract():
    """Day/week/month must request distinct ranges and use red-up/green-down."""
    with open("static/js/app.market.js", encoding="utf-8") as f:
        market = f.read()
    with open("static/js/app.indicator-chart.js", encoding="utf-8") as f:
        chart = f.read()
    with open("static/css/app.css", encoding="utf-8") as f:
        css = f.read()

    assert "_klinePeriodLimits: {'1d':120, '1w':104, '1m':60}" in market
    assert "'1d': [[60,'3月'],[120,'半年'],[250,'1年'],[500,'全部']]" in market
    assert "'1w': [[26,'半年'],[52,'1年'],[104,'2年'],[260,'5年']]" in market
    assert "'1m': [[12,'1年'],[24,'2年'],[60,'5年'],[120,'10年']]" in market
    assert "rise:'#d6332f'" in market
    assert "fall:'#1f9d83'" in market
    assert "color: P.rise, color0: P.fall" in chart
    assert "closes[i] >= +kline[i].open ? P.rise : P.fall" in chart
    assert ".quote-chip--up .quote-chip__pct{color:#cf222e" in css
    assert ".quote-chip--down .quote-chip__pct{color:#1a7f37" in css
    assert ".watchlist-item__pct--up{color:#cf222e" in css
    assert ".watchlist-item__pct--down{color:#1a7f37" in css


def test_composite_indicator_builder_static_contract():
    html = Path("templates/app.html").read_text(encoding="utf-8")
    indicators = Path("static/js/app.indicators.js").read_text(encoding="utf-8")
    css = Path("static/css/app.css").read_text(encoding="utf-8")
    metadata = Path("server/indicators/metadata.py").read_text(encoding="utf-8")

    assert "5日线上穿10日线，10日线上穿20日线" in html
    assert "信号回溯" in html
    assert "_indicatorConditionHtml" in indicators
    assert "data-indicator-conditions" in indicators
    assert "全部满足 AND" in indicators
    assert '"ma_stack_volume"' in metadata
    assert '"sync_window"' in metadata
    assert '"baseline_window"' in metadata
    assert ".mi-condition-list" in css
    assert ".mi-condition-row" in css


def test_analysis_markdown_readability_contract():
    css = Path("static/css/app.css").read_text(encoding="utf-8")
    analysis = Path("static/js/app.analysis.js").read_text(encoding="utf-8")

    assert ".ar-body ul{list-style:none}" in css
    assert ".ar-body ul>li::before" in css
    assert ".ar-body ol>li::marker" in css
    assert "max-width:1120px" in css
    assert ".ar-fold .ar-body" in css
    assert "_normalizeAnalysisMarkdown" in analysis
    assert "Analyst\\s*:\\s*(?=#{1,6}\\s+)" in analysis


def test_chat_inline_report_reader_contract():
    chat = Path("static/js/app.chat.js").read_text(encoding="utf-8")
    renderer = Path("static/js/app.chat-render.js").read_text(encoding="utf-8")
    css = Path("static/css/app.css").read_text(encoding="utf-8")
    from server.agent.api import event_payload

    assert "_reportFilesForMessage" in chat
    assert "_compactDeliveredReportText" in chat
    assert "_inlineReportCard" in chat
    assert "_toggleInlineReport" in chat
    assert 'sandbox="allow-scripts"' in chat
    assert (
        "allow-same-origin" not in chat[chat.index("_inlineReportCard") : chat.index("_toggleInlineReport")]
    )
    assert "分析过程" in renderer
    assert ".chat-inline-report" in css
    assert ".chat-inline-report-body" in css
    payload = event_payload(
        {
            "type": "research_finalized",
            "seq": 2,
            "status": "completed",
            "ui": {"report_files": [], "report_status": "unreviewed"},
        },
        {"run_id": "native-run", "conv_id": "native-conv", "workflow": "research"},
    )
    assert payload["report_files"] == []
    assert payload["report_status"] == "unreviewed"
    assert payload["agent"]["engine"] == "prism-native"
