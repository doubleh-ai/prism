"""Sidecar failures must not persist or expose provider credentials."""

import json

from server import vibe, vibe_engine

SECRET = "sk-isolated-provider-secret-0123456789"
OPAQUE = "synthetic-opaque-credential"


def test_normal_research_is_preserved_and_credential_shapes_redacted():
    research = "## 结论\n增长 12%，依据 [公司公告](https://example.com/notice?id=7)。\n尚缺客户确认。"
    assert vibe_engine.safe_engine_payload({"content": research}) == {"content": research}
    payload = {
        "content": f"API_KEY={OPAQUE}; Authorization: Bearer {SECRET}; https://user:{OPAQUE}@host.invalid/",
        "api_key": OPAQUE,
        "extra": f"API Key: {OPAQUE}; Authorization: Basic {OPAQUE}",
        "nested": {"access_token": SECRET},
    }
    output = json.dumps(vibe_engine.safe_engine_payload(payload))
    assert OPAQUE not in output
    assert SECRET not in output
    assert "[redacted]" in output


def test_legacy_failed_plan_does_not_reexpose_saved_provider_errors():
    report = "# 已有研究\n收入增长 12%，仍需核实。"
    saved = {
        "status": "failed",
        "stage_label": OPAQUE,
        "report_markdown": report,
        "activity": [{"status": "error", "detail": OPAQUE, "summary": SECRET}],
        "followups": [{"status": "failed", "error": OPAQUE}],
    }
    raw = json.dumps(saved)
    result = vibe._row_to_plan({"id": "legacy-plan", "created_at": 1, "plan_json": raw})
    assert OPAQUE not in json.dumps(result)
    assert SECRET not in json.dumps(result)
    assert result["report_markdown"] == report
    assert json.loads(raw) == saved


async def test_health_route_never_echoes_sidecar_exception(client, auth_cookies, monkeypatch):
    async def failure():
        raise AssertionError(OPAQUE + SECRET)

    monkeypatch.setattr(vibe_engine, "engine_health", failure, raising=False)
    response = await client.get("/api/vibe/engine", cookies=auth_cookies)
    assert response.status_code == 200
    assert response.json()["status"] == "execution_unavailable"
    assert OPAQUE not in response.text
    assert SECRET not in response.text
