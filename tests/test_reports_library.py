from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_report_library_static_contract():
    """Report library should have a stable shell, API path, and cache entry."""
    template = (ROOT / "templates" / "app.html").read_text(encoding="utf-8")
    reports_js = (ROOT / "static" / "js" / "app.reports.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
    sw = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")

    assert 'data-mode="reports"' in template
    assert "reportsPanel" in template
    assert "app.reports.js?v=83" in template
    assert "app.chat-render.js?v=83" in template
    assert "/api/reports/library" in reports_js
    assert "/save-report" in reports_js
    assert "_reportText: function(value)" in reports_js
    assert "replace(/\\[object Object\\]/g, '')" in reports_js
    assert "this._safeReportMarkdown(finalDecision)" in reports_js
    assert "reports-lib-detail-actions" in reports_js
    assert ".reports-lib-detail-actions" in css
    assert "overflow-wrap:anywhere" in css
    assert "app.reports.js" in sw


def test_report_backend_accepts_object_content_for_summary():
    """Saved report metadata should not depend on report fields being strings."""
    from server.reports import _compact_summary, _signal_from

    report = {
        "structured": {
            "signal": {"type": "text", "text": "BUY"},
            "thesis": [
                {"type": "thinking", "thinking": "[object Object]"},
                {"type": "text", "text": "**核心**: 上游瓶颈仍有验证空间。"},
            ],
        },
        "final_decision": {"content": [{"type": "text", "text": "备用结论"}]},
    }

    assert _signal_from(report) == "BUY"
    assert "[object Object]" not in _compact_summary(report)
    assert "上游瓶颈" in _compact_summary(report)


def test_report_payload_sanitizes_tool_blocks_and_object_strings():
    """Saved report detail should not surface Claude tool objects as text."""
    from server.reports import _sanitize_report_payload

    report = {
        "title": "AI数据中心液冷供应链上游瓶颈",
        "final_decision": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            {"type": "text", "text": "[object Object]"},
            {"content": [{"type": "text", "text": "真实结论：保持观察。"}]},
        ],
    }

    clean = _sanitize_report_payload(report)
    assert "真实结论" in clean["final_decision"]
    assert "[object Object]" not in clean["final_decision"]
    assert "Bash" not in clean["final_decision"]


def test_chat_report_list_can_open_saved_snapshots():
    """Conversation report modal should open saved report snapshots too."""
    chat_js = (ROOT / "static" / "js" / "app.chat.js").read_text(encoding="utf-8")

    assert "openSavedReportFromChat" in chat_js
    assert "rep.report_id" in chat_js
    assert "App.openSavedReportFromChat" in chat_js


def test_report_detail_marks_interrupted_skill_runs():
    """Interrupted research belongs in a collapsed process, not final decision."""
    from server.chat import _prepare_chat_report

    text, status = _prepare_chat_report("先读取资料，然后重做批量:", "serenity-bottleneck-hunter")
    assert status == "incomplete"
    assert text.endswith(":")

    delivered, status = _prepare_chat_report(
        "先读取资料\n\n## 报告已交付\n\n**核心发现**：上游仍有验证空间。",
        "serenity-bottleneck-hunter",
    )
    assert status == "complete"
    assert delivered.startswith("## 报告已交付")
    assert "先读取资料" not in delivered


def test_report_detail_theme_and_incomplete_state_contract():
    reports_js = (ROOT / "static" / "js" / "app.reports.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")

    assert "_reportHtmlString: function(value)" in reports_js
    assert "_reportIsIncompleteChat" in reports_js
    assert "研究未完成" in reports_js
    assert "reports-lib-process" in reports_js
    assert ".reports-lib-detail-head" in css
    assert "background:var(--bg);border-bottom:1px solid var(--border)" in css
