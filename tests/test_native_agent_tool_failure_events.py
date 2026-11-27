"""Tool failure observability: sanitised field paths, never argument values.

Tool arguments can carry research content (claim statements, source text). A
rejected call therefore exposes only the failing field path and pydantic error
type to the event log and to the model's self-correction turn.
"""

import json

import httpx
import pytest
from pydantic import ValidationError
from test_native_agent_runtime_review import done, events, execute
from test_native_agent_runtime_review import engine as engine
from test_native_agent_tools import native as native

from server.agent import runtime, tools
from server.capabilities import ClaimsArgs


def test_pydantic_details_keep_field_paths_without_argument_values():
    invalid = {
        "claims": [
            {
                "kind": "fact",
                "statement": "合成结论-秘密值-123",
                "source_locator": "page-7",
                "confidence": 0.9,
            }
        ]
    }
    with pytest.raises(ValidationError) as captured:
        ClaimsArgs.model_validate(invalid)
    details = tools._pydantic_details(captured.value)
    assert details and len(details) <= tools._MAX_ERROR_DETAILS
    assert all(set(item) == {"loc", "type"} for item in details)
    assert {"loc": "claims.0.confidence", "type": "extra_forbidden"} in details
    assert any(item["loc"] == "claims.0.source_locator" for item in details)
    dumped = json.dumps(details, ensure_ascii=False)
    assert "秘密值" not in dumped and "page-7" not in dumped


def test_native_tool_error_keeps_only_known_kinds_and_safe_details():
    error = tools.NativeToolError(
        "invalid_arguments",
        details=[
            {"loc": "claims.0.value", "type": "string_type"},
            {"loc": 7, "type": "string_type"},
            "raw-value",
            {"loc": "claims.1", "type": "value_error"},
            {"loc": "claims.2", "type": "extra_forbidden"},
        ],
        kind="bogus",
    )
    assert error.code == "invalid_arguments" and error.kind == "other"
    assert error.details == (
        {"loc": "claims.0.value", "type": "string_type"},
        {"loc": "claims.1", "type": "value_error"},
        {"loc": "claims.2", "type": "extra_forbidden"},
    )


async def test_unparseable_tool_arguments_report_their_shape(native):
    result = await native.tools.execute("research_record_claims", None, "missing-json-1")
    assert result["ok"] is False and result["error"]["code"] == "invalid_arguments"
    assert result["error"]["kind"] == "other"
    assert result["error"]["details"] == [{"loc": "arguments", "type": "invalid_json"}]


def test_failure_kind_classifies_timeout_provider_and_other():
    assert tools._failure_kind(TimeoutError()) == "timeout"
    assert tools._failure_kind(httpx.TimeoutException("synthetic timeout")) == "timeout"
    assert tools._failure_kind(httpx.HTTPError("synthetic provider error")) == "provider"
    assert tools._failure_kind(RuntimeError("synthetic")) == "other"


async def test_failed_claim_arguments_emit_sanitised_tool_failed_event(engine, monkeypatch):
    calls = []

    async def script(messages, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            offered = {spec.get("name") for spec in kwargs["tools"]}
            assert "research_record_claims" in offered
            call_id = "claim-invalid-1"
            arguments = {"claims": [{"kind": "fact", "statement": "合成结论-秘密值-123", "confidence": 0.9}]}
            yield {
                "type": "tool_call",
                "id": call_id,
                "name": "research_record_claims",
                "arguments": arguments,
            }
            yield done(
                "tool_use",
                content=[
                    {"type": "tool_use", "id": call_id, "name": "research_record_claims", "input": arguments}
                ],
            )
            return
        yield {"type": "text_delta", "text": "合成答案：本轮登记参数被拒绝，保留研究缺口。"}
        yield done()

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)
    assert outcome.status in {"completed", "partial"}

    failures = [event for event in events(engine) if event["type"] == "tool_failed"]
    assert len(failures) == 1
    failure = failures[0]
    assert failure["tool"] == "research_record_claims"
    assert failure["ui"]["error_code"] == "invalid_arguments"
    assert failure["ui"]["error_kind"] == "pydantic"
    paths = failure["ui"]["error_paths"]
    assert any(path.startswith("claims.0.confidence:") for path in paths)
    assert "秘密值" not in json.dumps(failures, ensure_ascii=False)

    # The follow-up turn carries the same sanitised detail so the model can fix
    # the shape instead of silently dropping the registration.
    results = [
        block
        for message in calls[1]
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if block.get("type") == "tool_result"
    ]
    assert results and results[-1]["is_error"] is True
    content = results[-1]["content"]
    assert '"invalid_arguments"' in content and "extra_forbidden" in content
    assert "秘密值" not in content


async def test_successful_tools_do_not_emit_tool_failed(engine, monkeypatch):
    calls = []

    async def script(messages, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            call_id = "indicator-ok-1"
            arguments = {}
            yield {"type": "tool_call", "id": call_id, "name": "indicator_list", "arguments": arguments}
            yield done(
                "tool_use",
                content=[{"type": "tool_use", "id": call_id, "name": "indicator_list", "input": arguments}],
            )
            return
        yield {"type": "text_delta", "text": "合成答案：指标清单已读取。"}
        yield done()

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)
    assert outcome.status in {"completed", "partial"}
    assert [event for event in events(engine) if event["type"] == "tool_failed"] == []
