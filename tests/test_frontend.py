"""Test frontend rendering logic — run with Node.js or as pure logic tests"""
import json
import pytest


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
            # Streaming: show fold + current step prominently
            html += '<div class="step-bar"><details class="step-fold"><summary>已完成 ' + str(n) + ' 步</summary>'
            for td in pending_tools:
                detail_html = ' <i>' + esc(td["detail"]) + '</i>' if td.get("detail") else ""
                html += '<span class="step-tag"><b>' + esc(td["name"]) + '</b>' + detail_html + '</span>'
            html += '</details>'
            last = pending_tools[-1]
            ld = ' <i>' + esc(last["detail"]) + '</i>' if last.get("detail") else ""
            html += '<span class="step-tag now step-now"><b>' + esc(last["name"]) + '</b>' + ld + '</span></div>'
        else:
            # Non-streaming or completed: fold all steps
            html += '<div class="step-bar stable"><details class="step-fold"><summary>已完成 ' + str(n) + ' 步</summary>'
            for td in pending_tools:
                detail_html = ' <i>' + esc(td["detail"]) + '</i>' if td.get("detail") else ""
                html += '<span class="step-tag"><b>' + esc(td["name"]) + '</b>' + detail_html + '</span>'
            html += '</details></div>'
        pending_tools.clear()

    def flush_thinks():
        nonlocal html
        if not pending_thinks:
            return
        for think_text in pending_thinks:
            html += '<details class="think-box" open><summary>Thinking</summary><div class="think-content">' + esc(think_text) + '</div></details>'
        pending_thinks.clear()

    for ci, c in enumerate(chunks):
        t = c.get("t")
        if t == "tool":
            pending_tools.append({
                "name": c.get("c", "?"),
                "detail": clean_detail(c.get("d", ""))
            })
        elif t == "think":
            flush_tools(False)
            pending_thinks.append(c.get("c", ""))
        elif t == "ask":
            flush_tools(False)
            flush_thinks()
            html += '<div class="ask-card"><div class="ask-q">💬 ' + esc(c.get("q", "")) + '</div><div class="ask-opts">'
            for opt in c.get("opts", []):
                html += '<button class="ask-opt">' + esc(opt) + '</button>'
            html += '</div></div>'
        elif t == "ref":
            # Reference citation — skip during streaming, shown at end
            collected_refs.append({"url": c.get("url", ""), "title": c.get("title", c.get("url", "")), "snippet": c.get("snippet", "")})
        else:
            is_last_text = streaming and ci == len(chunks) - 1
            # Also check: if there are trailing non-text chunks, it's not the last text
            for cj in range(ci + 1, len(chunks)):
                if chunks[cj].get("t") not in ("tool", "ref"):
                    is_last_text = False
                    break
            flush_tools(False)
            flush_thinks()
            html += c.get("c", "") + ('<span class="stream-cursor"></span>' if is_last_text else '')

    if streaming:
        flush_tools(True)
        flush_thinks()
        if html == '':
            html = '<div class="shimmer-bar" style="width:60%"></div>'
    else:
        flush_tools(False)
        flush_thinks()

    # Append reference section if we have refs and this is the final render
    if collected_refs and not streaming:
        html += '<div class="ref-section"><div class="ref-title">参考来源 (' + str(len(collected_refs)) + ')</div><div class="ref-list">'
        for ri, ref in enumerate(collected_refs):
            html += '<div class="ref-item"><span class="ref-idx">' + str(ri + 1) + '</span><div><a class="ref-link" href="' + esc(ref["url"]) + '" target="_blank" rel="noopener">' + esc(ref["title"]) + '</a>'
            if ref.get("snippet"):
                html += '<div class="ref-snippet">' + esc(ref["snippet"]) + '</div>'
            html += '</div></div>'
        html += '</div></div>'

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
        assert "已完成" in result

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
        assert now_count == 1, "Expected 1 'step-now', got {}".format(now_count)

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
        """The smart scroll flag (_userScrolledUp) must be in app.js."""
        import os
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        js_path = os.path.join(project_root, "static", "js", "app.js")
        if os.path.exists(js_path):
            with open(js_path, encoding="utf-8") as f:
                js_content = f.read()
            assert "_userScrolledUp" in js_content
            assert "_scrollToBottom" in js_content
            assert "dist > 80" in js_content  # scroll threshold

    def test_stop_button_in_js(self):
        """Stop generation button logic must be in app.js."""
        import os
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        js_path = os.path.join(project_root, "static", "js", "app.js")
        if os.path.exists(js_path):
            with open(js_path, encoding="utf-8") as f:
                js_content = f.read()
            assert "stopStream" in js_content
            assert "stopBtn" in js_content or "AbortError" in js_content

    def test_error_classification_in_js(self):
        """Error classification (AbortError, TypeError, timeout) must be in app.js."""
        import os
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        js_path = os.path.join(project_root, "static", "js", "app.js")
        if os.path.exists(js_path):
            with open(js_path, encoding="utf-8") as f:
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
        """The _stableKey / _stableHtml split logic should be removed from app.js."""
        import os
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        js_path = os.path.join(project_root, "static", "js", "app.js")
        if os.path.exists(js_path):
            with open(js_path, encoding="utf-8") as f:
                js_content = f.read()
            assert "_stableKey" not in js_content, "stable/live split should be removed to prevent flicker"
            assert "_stableHtml" not in js_content, "stable/live split should be removed to prevent flicker"

    def test_prism_byline_in_js(self):
        """The assistant byline must use 'Prism' branding."""
        import os
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        js_path = os.path.join(project_root, "static", "js", "app.js")
        if os.path.exists(js_path):
            with open(js_path, encoding="utf-8") as f:
                js_content = f.read()
            assert "Prism" in js_content, "JS must use Prism branding"
            assert "CapitalLens" not in js_content, "JS must not contain old CapitalLens branding"
