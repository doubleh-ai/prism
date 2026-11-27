import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_report_library_static_contract():
    """Report library should have a stable shell, API path, and cache entry."""
    template = (ROOT / "templates" / "app.html").read_text(encoding="utf-8")
    reports_js = (ROOT / "static" / "js" / "app.reports.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
    sw = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")

    assert 'data-mode="reports"' in template
    assert "reportsPanel" in template
    asset_version = re.search(r"app\.css\?v=(\d+)", template)
    assert asset_version is not None
    version = asset_version.group(1)
    assert f"app.reports.js?v={version}" in template
    assert f"app.chat-render.js?v={version}" in template
    assert f"prism-workbench-v{version}" in sw
    assert f"path + '?v={version}'" in sw
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


def test_report_detail_preserves_and_renders_interrupted_legacy_runs(test_user):
    """The shipped reader keeps historical incomplete snapshots visibly incomplete."""
    from server.reports import save_report_snapshot

    interrupted = save_report_snapshot(
        test_user["username"],
        {
            "source": "chat",
            "source_id": "legacy-interrupted",
            "report": {
                "status": "incomplete",
                "skill": "serenity-bottleneck-hunter",
                "final_decision": "先读取资料，然后重做批量:",
            },
        },
    )
    assert interrupted["report"]["status"] == "incomplete"
    assert interrupted["report"]["final_decision"].endswith(":")
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const context = {App: {}, window: {}};
vm.runInNewContext(fs.readFileSync('static/js/app.reports.js', 'utf8'), context);
const app = context.App;
const saved = JSON.parse(process.argv[1]);
const report = saved.report;
assert.equal(app._reportIsIncompleteChat(saved, report, report.final_decision, ''), true);
assert.equal(app._reportIsIncompleteChat({source:'chat'}, {skill:'historical'}, 'Interrupted API Error', ''), true);
const draft = '先读取资料\n\n## 报告已交付\n\n**核心发现**：上游仍有验证空间。';
const delivered = app._reportDeliveredText(draft);
assert.ok(delivered.startsWith('## 报告已交付'));
assert.equal(delivered.includes('先读取资料'), false);
assert.equal(app._reportIsIncompleteChat({source:'chat'}, {skill:'historical'}, draft, delivered), false);
assert.equal(app._reportIsIncompleteChat({source:'chat'}, {status:'complete'}, '完成答案', ''), false);
"""
    result = subprocess.run(
        [node, "-e", script, json.dumps(interrupted, ensure_ascii=False)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_report_detail_theme_and_incomplete_state_contract():
    reports_js = (ROOT / "static" / "js" / "app.reports.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")

    assert "_reportHtmlString: function(value)" in reports_js
    assert "_reportIsIncompleteChat" in reports_js
    assert "研究未完成" in reports_js
    assert "reports-lib-process" in reports_js
    assert ".reports-lib-detail-head" in css
    assert "background:var(--bg);border-bottom:1px solid var(--border)" in css
