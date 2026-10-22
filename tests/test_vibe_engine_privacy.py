"""Sidecar failures must not persist or expose provider credentials."""

import json

import httpx
import pytest

from server import vibe, vibe_engine

SECRET = "sk-isolated-provider-secret-0123456789"
OPAQUE = "synthetic-opaque-credential"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RuntimeError(f"provider body {OPAQUE}"), "研究引擎暂时不可用"),
        (ValueError(f"invalid JSON {SECRET}"), "研究引擎返回无效数据"),
        (httpx.ReadTimeout(f"timeout Authorization: Bearer {OPAQUE}"), "研究引擎请求超时"),
        (httpx.ConnectError(f"https://user:{OPAQUE}@provider.invalid"), "研究引擎连接失败"),
    ],
)
def test_exception_messages_are_classified_without_provider_text(error, expected):
    assert str(vibe_engine._raise_engine_error(error)) == expected


def test_http_error_keeps_only_safe_status():
    request = httpx.Request("GET", f"https://provider.invalid/?API_KEY={OPAQUE}")
    response = httpx.Response(403, request=request, text=f"provider rejected {SECRET}: {OPAQUE}")
    error = httpx.HTTPStatusError(f"Authorization: Bearer {OPAQUE}", request=request, response=response)
    assert str(vibe_engine._raise_engine_error(error)) == "研究引擎请求失败（HTTP 403）"


def test_diagnostic_events_drop_opaque_errors_and_preserve_attempt_binding():
    raw = {"attempt_id": "attempt-a", "error": OPAQUE, "summary": SECRET, "preview": OPAQUE}
    safe = vibe_engine.safe_engine_event("attempt.failed", raw)
    assert safe == {"attempt_id": "attempt-a", "error": "研究引擎未能完成任务"}
    progress = vibe_engine.safe_engine_event("tool_progress", {"message": OPAQUE, "stage": SECRET})
    assert progress["message"] == "研究工具正在处理请求"
    assert "stage" not in progress


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


def test_failure_and_progress_do_not_persist_provider_text_in_plan():
    plan = {"quality": {}, "activity": [], "created_at": 1, "engine_attempt_id": "attempt-a"}
    vibe._apply_engine_event(plan, "tool_call", {"tool": "web_search", "call_id": "call-a"}, "one")
    vibe._apply_engine_event(
        plan,
        "tool_result",
        {
            "tool": "web_search",
            "call_id": "call-a",
            "status": "error",
            "preview": f"failed {OPAQUE} {SECRET}",
        },
        "two",
    )
    vibe._apply_engine_event(plan, "tool_progress", {"message": OPAQUE}, "three")
    vibe._apply_engine_event(plan, "attempt.failed", {"error": OPAQUE, "summary": SECRET}, "four")
    assert OPAQUE not in json.dumps(plan)
    assert SECRET not in json.dumps(plan)
    assert plan["quality"]["tool_errors"] == 1
    assert plan["stage_label"] == "研究引擎未能完成任务"


async def test_poll_messages_redacts_tokens_without_rewriting_report(monkeypatch):
    report = "# 研究报告\n收入增长 12%，事件日期未知。"
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json=[
                {"role": "assistant", "linked_attempt_id": "a", "content": report},
                {"role": "assistant", "linked_attempt_id": "b", "content": f"API_KEY={OPAQUE}"},
            ],
        )
    )
    original = httpx.AsyncClient
    monkeypatch.setattr(
        vibe_engine.httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs)
    )
    result = await vibe_engine.engine_get_messages("session")
    assert result[0]["content"] == report
    assert OPAQUE not in json.dumps(result)


async def test_sse_failure_does_not_forward_body_or_invalid_json(monkeypatch):
    events = (
        "event: attempt.failed\ndata: "
        + json.dumps({"attempt_id": "a", "error": OPAQUE, "summary": SECRET})
        + "\n\n"
        "event: message\ndata: " + SECRET + "\n\n"
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=events))
    original = httpx.AsyncClient
    monkeypatch.setattr(
        vibe_engine.httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs)
    )
    result = [event async for event in vibe_engine.engine_events("session")]
    assert result[0][1]["attempt_id"] == "a"
    assert OPAQUE not in json.dumps(result)
    assert SECRET not in json.dumps(result)


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
        raise vibe_engine.VibeEngineError(OPAQUE + SECRET)

    monkeypatch.setattr(vibe_engine, "engine_health", failure)
    response = await client.get("/api/vibe/engine", cookies=auth_cookies)
    assert response.status_code == 200
    assert response.json()["status"] == "execution_unavailable"
    assert OPAQUE not in response.text
    assert SECRET not in response.text
