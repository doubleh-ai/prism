from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_official_vibe_workbench_static_contract():
    template = (ROOT / "templates" / "app.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
    js = (ROOT / "static" / "js" / "app.vibe.js").read_text(encoding="utf-8")
    sw = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")

    assert 'data-mode="vibe"' in template
    assert "vibePanel" in template
    assert 'id="vibeDepth"' in template
    assert "开始正式研究" in template
    assert "app.vibe.js?v=83" in template
    assert '#appShell[data-mode="vibe"]' in css
    assert "/api/vibe/plan" in js
    assert "/api/vibe/engine" in js
    assert "_vibeExecutionNotice" in js
    assert "HKUDS/Vibe-Trading" in js
    assert "_vibeActivityHtml" in js
    assert "_vibeActivityIssueGroups" in js
    assert "_vibeActivityCopy" in js
    assert "采集有效率" in js
    assert "数据缺口" in js
    assert "已完成 " in js
    assert "report_markdown" in js
    assert "_renderVibeMarkdown" in js
    assert "追问暂不可用" in js
    assert "/cancel" in js
    assert "_deleteVibePlan" in js
    assert "vibe-side-del" in js
    assert ".vibe-report-markdown table" in css
    assert ".vibe-workbench-grid" in css
    assert ".vibe-run-health-bar" in css
    assert ".vibe-activity-issues" in css
    assert ".vibe-activity-issue-group" in css
    assert "app.vibe.js" in sw
