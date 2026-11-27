"""Native runtime: one bounded claim-registration pass in the concluding turn.

Production evidence (run b4f44d9eeb64bbdc6c4fa00c87c76a3d): the wrap-up turn
disabled every tool, so research_record_claims could never run, research_claims
stayed empty and claim_reconciliation reported verified_claim_ratio=null with
all report numbers unsupported. The concluding turn must therefore keep exactly
that one tool for a single bounded pass, then really go tool-free.
"""

import json

import httpx
from test_native_agent_provider import frame, start, stop
from test_native_runtime_continuation import _tool_use_turn, done, events, execute
from test_native_runtime_continuation import engine as engine

from server.agent import runtime, tools
from server.agent.provider import AnthropicMessagesProvider
from server.research import ledger

# A single read body large enough that the wrap-up threshold (3/4 of the
# context budget) is crossed, but small enough that the claim tool result still
# does not force a context release which would re-open the full tool list.
OVERSIZED_READ = "资料" * 9500

CLAIM_ARGS = {
    "claims": [
        {
            "kind": "fact",
            "statement": "合成公司 2025 年产量 120 万吨。",
            "metric": "产量",
            "value": 120,
            "unit": "万吨",
            "period": "2025",
            "verification": "unverified",
        }
    ]
}


def _claim_pass_patch(monkeypatch, text):
    """Run the claim tool for real; every other tool returns one oversized document."""
    real_execute = tools.NativeTools.execute

    async def fake_execute(self, name, args, call_id):
        if name == "research_record_claims":
            return await real_execute(self, name, args, call_id)
        return {"ok": True, "data": {"text": text}, "refs": []}

    monkeypatch.setattr(tools.NativeTools, "execute", fake_execute)


async def test_wrap_up_registers_claims_once_then_answers_without_tools(engine, monkeypatch):
    engine.service.limits = runtime.Limits(context_characters=24000)
    _claim_pass_patch(monkeypatch, OVERSIZED_READ)
    calls = []

    async def script(messages, **kwargs):
        calls.append({"tools": kwargs["tools"], "system": kwargs["system"]})
        if len(calls) == 1:
            yield _tool_use_turn("claim-pass-read")
            yield done("tool_use", content=[{"type": "tool_use", "id": "claim-pass-read"}])
        elif len(calls) == 2:
            yield {
                "type": "tool_call",
                "id": "claim-pass-1",
                "name": "research_record_claims",
                "arguments": CLAIM_ARGS,
            }
            yield done(
                "tool_use",
                content=[
                    {
                        "type": "tool_use",
                        "id": "claim-pass-1",
                        "name": "research_record_claims",
                        "input": CLAIM_ARGS,
                    }
                ],
            )
        else:
            yield {"type": "text_delta", "text": "合成收尾报告：2025 年产量 120 万吨。"}
            yield done()

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)

    assert len(calls) == 3
    # The first wrap-up call offers exactly one tool and names it in the prompt.
    assert [spec["name"] for spec in calls[1]["tools"]] == ["research_record_claims"]
    assert "上下文即将用尽" in calls[1]["system"]
    assert "本轮唯一可用工具是 research_record_claims" in calls[1]["system"]
    assert "先登记报告正文的每个数值，再直接写报告正文。" in calls[1]["system"]
    assert "不要调用其它工具、不要继续检索" in calls[1]["system"]
    # The next concluding call is genuinely tool-free: the bounded pass is spent.
    assert calls[2]["tools"] == []
    assert "本轮已经不能再登记 claim" in calls[2]["system"]
    assert outcome.status == "completed" and outcome.error_code is None
    # The model's tool call really reached the ledger, not just the fake runtime.
    saved = ledger.read_research_run(engine.user, engine.run["run_id"])
    assert [claim["statement"] for claim in saved["claims"]] == ["合成公司 2025 年产量 120 万吨。"]
    # The run response carries the coverage metric computed after claims landed:
    # one claim for the report's only number is full coverage.
    research = json.loads(outcome.message_payload)["research"]
    assert research["claim_reconciliation"]["coverage"] == 1.0
    assert research["claim_reconciliation"]["report_numbers_checked"] == 1


async def test_exhausted_model_call_budget_never_offers_the_claim_tool(engine, monkeypatch):
    engine.service.limits = runtime.Limits(model_calls=2)
    calls = []

    async def script(messages, **kwargs):
        calls.append({"tools": kwargs["tools"], "system": kwargs["system"]})
        if len(calls) == 1:
            yield done("pause_turn")
        else:
            yield {"type": "text_delta", "text": "合成收尾报告。"}
            yield done()

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)

    assert len(calls) == 2
    # Only one call is left, so it is reserved for the answer: a claim pass would
    # consume it without leaving a call to write the report.
    assert calls[1]["tools"] == []
    assert "本轮唯一可用工具" not in calls[1]["system"]
    assert "本轮已经不能再登记 claim" in calls[1]["system"]
    assert outcome.status == "completed" and outcome.error_code is None


async def test_wall_clock_margin_reserves_the_answer_call(engine, monkeypatch):
    engine.service.limits = runtime.Limits(wall_seconds=60, context_characters=24000)
    _claim_pass_patch(monkeypatch, OVERSIZED_READ)
    calls = []

    async def script(messages, **kwargs):
        calls.append({"tools": kwargs["tools"], "system": kwargs["system"]})
        if len(calls) == 1:
            yield _tool_use_turn("wall-read")
            yield done("tool_use", content=[{"type": "tool_use", "id": "wall-read"}])
        else:
            yield {"type": "text_delta", "text": "合成收尾报告。"}
            yield done()

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)

    assert len(calls) == 2
    # Less than the 90-second claim-pass margin remains, so the wrap-up degrades to
    # the existing tool-free answer instead of buying an extra model call.
    assert calls[1]["tools"] == []
    assert "本轮唯一可用工具" not in calls[1]["system"]
    assert "本轮已经不能再登记 claim" in calls[1]["system"]
    assert outcome.status == "completed" and outcome.error_code is None


async def test_two_remaining_model_calls_start_the_claim_pass_before_the_answer(engine, monkeypatch):
    # Production evidence (run ec2eff60): the budget-only wrap-up started on the
    # last permitted call, so the claim tool was never offered and the truncated
    # report needed a continuation call the run could not afford. With two calls
    # left the wrap-up must start now: this call registers claims, the next one
    # writes the tool-free report.
    engine.service.limits = runtime.Limits(model_calls=3)
    calls = []

    async def script(messages, **kwargs):
        calls.append({"tools": kwargs["tools"], "system": kwargs["system"]})
        if len(calls) == 1:
            yield done("pause_turn")
        elif len(calls) == 2:
            yield {
                "type": "tool_call",
                "id": "proactive-claims",
                "name": "research_record_claims",
                "arguments": CLAIM_ARGS,
            }
            yield done(
                "tool_use",
                content=[
                    {
                        "type": "tool_use",
                        "id": "proactive-claims",
                        "name": "research_record_claims",
                        "input": CLAIM_ARGS,
                    }
                ],
            )
        else:
            yield {"type": "text_delta", "text": "合成收尾报告：关键数字已登记。"}
            yield done()

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)

    assert len(calls) == 3
    # The first call is still a research call; the early wrap-up waits until the
    # claim pass and the report fit in the remaining two calls.
    assert "research_record_claims" in [spec["name"] for spec in calls[0]["tools"]]
    assert "本轮唯一可用工具" not in calls[0]["system"]
    # The claim pass owns the second-to-last call, the last one is tool-free.
    assert [spec["name"] for spec in calls[1]["tools"]] == ["research_record_claims"]
    assert "本轮唯一可用工具是 research_record_claims" in calls[1]["system"]
    assert calls[2]["tools"] == []
    assert "本轮已经不能再登记 claim" in calls[2]["system"]
    assert outcome.status == "completed" and outcome.error_code is None
    saved = ledger.read_research_run(engine.user, engine.run["run_id"])
    assert [claim["statement"] for claim in saved["claims"]] == ["合成公司 2025 年产量 120 万吨。"]


async def test_early_wrap_up_reserves_the_claim_review_and_report_calls(engine, monkeypatch):
    engine.service.limits = runtime.Limits(model_calls=5)
    calls = []

    async def script(messages, **kwargs):
        calls.append({"tools": kwargs["tools"], "system": kwargs["system"]})
        if len(calls) <= 2:
            yield done("pause_turn")
        elif len(calls) == 3:
            yield {
                "type": "tool_call",
                "id": "timed-claims",
                "name": "research_record_claims",
                "arguments": CLAIM_ARGS,
            }
            yield done(
                "tool_use",
                content=[
                    {
                        "type": "tool_use",
                        "id": "timed-claims",
                        "name": "research_record_claims",
                        "input": CLAIM_ARGS,
                    }
                ],
            )
        else:
            yield {"type": "text_delta", "text": "合成收尾报告。"}
            yield done()

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)

    assert len(calls) == 4
    # Three calls are left at the third call, so the early wrap-up starts now and
    # keeps one call each for the bounded claim pass, the excerpt review (skipped
    # here because the synthetic claim cites no excerpt) and the tool-free report.
    assert "research_record_claims" in [spec["name"] for spec in calls[1]["tools"]]
    assert "本轮唯一可用工具" not in calls[1]["system"]
    assert [spec["name"] for spec in calls[2]["tools"]] == ["research_record_claims"]
    assert calls[3]["tools"] == []
    started = [e for e in events(engine) if e["type"] == "model_started"]
    assert [bool(e.get("wrap_up")) for e in started] == [False, False, True, True]
    assert outcome.status == "completed" and outcome.error_code is None


async def test_wrap_up_calls_get_the_larger_output_budget(engine, monkeypatch):
    # The production wrap-up report hit the 4096-token ceiling, needed one
    # continuation call and then exceeded model_calls. Wrap-up calls therefore use
    # the dedicated larger budget; ordinary research calls keep the configured one.
    engine.service.limits = runtime.Limits(model_calls=3)
    tokens = []

    async def script(messages, **kwargs):
        tokens.append(kwargs["max_tokens"])
        if len(tokens) == 1:
            yield done("pause_turn")
        elif len(tokens) == 2:
            yield {
                "type": "tool_call",
                "id": "budget-claims",
                "name": "research_record_claims",
                "arguments": CLAIM_ARGS,
            }
            yield done(
                "tool_use",
                content=[
                    {
                        "type": "tool_use",
                        "id": "budget-claims",
                        "name": "research_record_claims",
                        "input": CLAIM_ARGS,
                    }
                ],
            )
        else:
            yield {"type": "text_delta", "text": "合成收尾报告。"}
            yield done()

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)

    assert tokens == [
        engine.service.limits.output_tokens_per_call,
        runtime._WRAP_UP_OUTPUT_TOKENS,
        runtime._WRAP_UP_OUTPUT_TOKENS,
    ]
    assert runtime._WRAP_UP_OUTPUT_TOKENS == 8192
    assert outcome.status == "completed" and outcome.error_code is None


async def test_truncated_claim_tool_json_degrades_and_the_report_still_runs(engine, monkeypatch):
    # Production run 1f70e9239401829679ecaae59c37bad8 exhausted the claim pass'
    # output budget in the middle of the tool-argument JSON. That protocol error
    # used to end the whole run with claims=0 and gaps=[structured_claims_missing,
    # engine_failed]. The truncated call must instead reach the tool layer as one
    # is_error result so the run still writes its report.
    engine.service.limits = runtime.Limits(model_calls=3)
    requests = []

    def text_turn(text, reason="end_turn"):
        return (
            start()
            + frame("content_block_start", index=0, content_block={"type": "text", "text": ""})
            + frame("content_block_delta", index=0, delta={"type": "text_delta", "text": text})
            + frame("content_block_stop", index=0)
            + stop(reason)
        )

    research_turn = text_turn("合成研究阶段。", "pause_turn")
    truncated_claim_turn = (
        start()
        + frame(
            "content_block_start",
            index=0,
            content_block={
                "type": "tool_use",
                "id": "truncated-claims",
                "name": "research_record_claims",
                "input": {},
            },
        )
        + frame(
            "content_block_delta",
            index=0,
            delta={"type": "input_json_delta", "partial_json": '{"claims":[{"statement":'},
        )
        + frame("content_block_stop", index=0)
        + stop("max_tokens")
    )
    report_turn = text_turn("合成收尾报告：2025 年产量 120 万吨。")

    def handler(request):
        requests.append(request)
        body = [research_turn, truncated_claim_turn, report_turn][len(requests) - 1]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    adapter = AnthropicMessagesProvider(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(runtime, "model_stream", adapter.stream)
    outcome = await execute(engine)

    assert len(requests) == 3
    assert outcome.status == "completed" and outcome.error_code is None
    research = json.loads(outcome.message_payload)["research"]
    assert "structured_claims_missing" in research["gaps"]
    assert "engine_failed" not in research["gaps"]
    # The report body still gets a server-extracted claim even though the model's
    # only claim call was truncated, so coverage is complete and the advisory
    # low-coverage gap is gone.
    assert research["claim_reconciliation"]["coverage"] == 1.0
    assert research["claim_reconciliation"]["report_numbers_checked"] == 1
    assert research["claim_reconciliation"]["unsupported_claims"] == 0
    assert "claim_coverage_low" not in research["gaps"]
    # The degraded call was attempted once and recorded as an error, not executed.
    assert [event["tool"] for event in events(engine) if event["type"] == "tool_started"] == [
        "research_record_claims"
    ]
    assert not [event for event in events(engine) if event["type"] == "tool_completed"]
    stored_claims = ledger.read_research_run(engine.user, engine.run["run_id"])["claims"]
    assert [claim["origin"] for claim in stored_claims] == ["server_extract"]
    assert stored_claims[0]["verification"] == "unverified"


INVALID_CLAIM_ARGS = {"claims": [{**CLAIM_ARGS["claims"][0], "entities": [{"a": 1}]}]}


def _claim_turn(call_id, arguments):
    return [
        {"type": "tool_call", "id": call_id, "name": "research_record_claims", "arguments": arguments},
        done(
            "tool_use",
            content=[
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": "research_record_claims",
                    "input": arguments,
                }
            ],
        ),
    ]


async def test_rejected_claim_arguments_get_one_retry_before_the_report(engine, monkeypatch):
    # Production run 0fc4038c: one pydantic field error rejected the whole claim
    # batch, the runtime never offered the claim tool again and the run stored
    # zero model claims. A claim round rejected for argument shape only now gets
    # exactly one more bounded claim call, then the tool-free report.
    engine.service.limits = runtime.Limits(model_calls=4)
    calls = []

    async def script(messages, **kwargs):
        calls.append({"tools": kwargs["tools"], "system": kwargs["system"]})
        if len(calls) == 1:
            yield done("pause_turn")
        elif len(calls) == 2:
            for event in _claim_turn("rejected-claims", INVALID_CLAIM_ARGS):
                yield event
        elif len(calls) == 3:
            for event in _claim_turn("retried-claims", CLAIM_ARGS):
                yield event
        else:
            yield {"type": "text_delta", "text": "合成收尾报告。"}
            yield done()

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)

    assert len(calls) == 4
    assert [spec["name"] for spec in calls[1]["tools"]] == ["research_record_claims"]
    assert "上一次 research_record_claims 调用" not in calls[1]["system"]
    # The retry round names the retry and asks for corrected argument shapes.
    assert [spec["name"] for spec in calls[2]["tools"]] == ["research_record_claims"]
    assert "上一次 research_record_claims 调用因参数结构与校验要求不符被拒绝" in calls[2]["system"]
    assert calls[3]["tools"] == []
    # The retry is visible in the event stream and the generated claim landed.
    started = [event for event in events(engine) if event["type"] == "model_started"]
    assert [event.get("claim_pass_attempt") for event in started] == [None, 1, 2, None]
    failed = [event for event in events(engine) if event["type"] == "tool_failed"]
    assert [(event["ui"]["error_kind"], event["ui"]["error_paths"]) for event in failed] == [
        ("pydantic", ["claims.0.entities.0:string_type"])
    ]
    saved = ledger.read_research_run(engine.user, engine.run["run_id"])
    assert [claim["statement"] for claim in saved["claims"]] == ["合成公司 2025 年产量 120 万吨。"]
    assert outcome.status == "completed" and outcome.error_code is None


async def test_claim_retry_is_capped_at_two_attempts(engine, monkeypatch):
    engine.service.limits = runtime.Limits(model_calls=5)
    calls = []

    async def script(messages, **kwargs):
        calls.append({"tools": kwargs["tools"], "system": kwargs["system"]})
        if len(calls) <= 2:
            yield done("pause_turn")
        elif len(calls) <= 4:
            for event in _claim_turn(f"rejected-{len(calls)}", INVALID_CLAIM_ARGS):
                yield event
        else:
            yield {"type": "text_delta", "text": "合成收尾报告。"}
            yield done()

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)

    assert len(calls) == 5
    assert [spec["name"] for spec in calls[2]["tools"]] == ["research_record_claims"]
    assert [spec["name"] for spec in calls[3]["tools"]] == ["research_record_claims"]
    assert calls[4]["tools"] == []
    started = [event for event in events(engine) if event["type"] == "model_started"]
    assert [event.get("claim_pass_attempt") for event in started] == [None, None, 1, 2, None]
    assert [event["tool"] for event in events(engine) if event["type"] == "tool_started"] == [
        "research_record_claims",
        "research_record_claims",
    ]
    saved = ledger.read_research_run(engine.user, engine.run["run_id"])
    assert saved["claims"] == []
    assert outcome.status == "completed" and outcome.error_code is None


async def test_claim_retry_is_skipped_when_only_the_report_call_fits(engine, monkeypatch):
    engine.service.limits = runtime.Limits(model_calls=3)
    calls = []

    async def script(messages, **kwargs):
        calls.append({"tools": kwargs["tools"], "system": kwargs["system"]})
        if len(calls) == 1:
            yield done("pause_turn")
        elif len(calls) == 2:
            for event in _claim_turn("rejected-claims", INVALID_CLAIM_ARGS):
                yield event
        else:
            yield {"type": "text_delta", "text": "合成收尾报告。"}
            yield done()

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)

    assert len(calls) == 3
    # The remaining call is reserved for the report: no second claim call.
    assert calls[2]["tools"] == []
    assert all("上一次 research_record_claims 调用" not in call["system"] for call in calls)
    started = [event for event in events(engine) if event["type"] == "model_started"]
    assert [event.get("claim_pass_attempt") for event in started] == [None, 1, None]
    # Skipping the retry is visible as an advisory gap, so the run's claim loss is
    # traceable to the budget conflict instead of looking like a model omission.
    research = json.loads(outcome.message_payload)["research"]
    assert "claim_retry_skipped_for_report" in research["gaps"]
    saved = ledger.read_research_run(engine.user, engine.run["run_id"])
    assert saved["claims"] == []
    assert outcome.status == "completed" and outcome.error_code is None


CLAIM_WITHOUT_BASIS = {
    "claims": [
        {
            "kind": "fact",
            "statement": "合成公司产量 120 万吨。",
            "metric": "产量",
            "value": 120,
            "unit": "万吨",
            "verification": "unverified",
        }
    ]
}
BASIS_FIXED_CLAIM = {"claims": [{**CLAIM_WITHOUT_BASIS["claims"][0], "period": "2025"}]}


async def test_claim_gap_details_get_one_correction_retry_before_the_report(engine, monkeypatch):
    # Production runs 00d8a852/a1310d01 registered claims whose citation gaps the
    # model never learned about: the tool returned a generic failure only, the run
    # moved straight to the report and the claims stayed partial/invalid. A claim
    # batch that lands but reports retryable gaps now gets one bounded correction
    # pass with content-free field hints, then the report.
    engine.service.limits = runtime.Limits(model_calls=4)
    calls = []

    async def script(messages, **kwargs):
        calls.append({"tools": kwargs["tools"], "system": kwargs["system"], "messages": messages})
        if len(calls) == 1:
            yield done("pause_turn")
        elif len(calls) == 2:
            for event in _claim_turn("gapped-claims", CLAIM_WITHOUT_BASIS):
                yield event
        elif len(calls) == 3:
            for event in _claim_turn("fixed-claims", BASIS_FIXED_CLAIM):
                yield event
        else:
            yield {"type": "text_delta", "text": "合成收尾报告：2025 年产量 120 万吨。"}
            yield done()

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)

    assert len(calls) == 4
    assert [spec["name"] for spec in calls[1]["tools"]] == ["research_record_claims"]
    # The second claim pass corrects gaps, not argument shape: it uses the gap
    # prompt and the tool result carried the structured hint back to the model.
    assert [spec["name"] for spec in calls[2]["tools"]] == ["research_record_claims"]
    assert "字段缺口" in calls[2]["system"]
    assert "上一次 research_record_claims 调用因参数结构与校验要求不符被拒绝" not in calls[2]["system"]
    tool_results = json.dumps(calls[2]["messages"], ensure_ascii=False)
    assert "claim_details" in tool_results and "numeric_basis_missing" in tool_results
    assert calls[3]["tools"] == []
    started = [event for event in events(engine) if event["type"] == "model_started"]
    assert [event.get("claim_pass_attempt") for event in started] == [None, 1, 2, None]
    saved = ledger.read_research_run(engine.user, engine.run["run_id"])["claims"]
    # The retry upgraded the same measurement instead of adding a duplicate claim.
    assert [claim["statement"] for claim in saved] == ["合成公司产量 120 万吨。"]
    assert saved[0]["unit"] == "万吨" and saved[0]["period"] == "2025"
    assert "numeric_basis_missing" not in saved[0]["gaps"]
    assert outcome.status == "completed" and outcome.error_code is None


async def test_claim_gap_retry_is_skipped_when_only_the_report_call_fits(engine, monkeypatch):
    engine.service.limits = runtime.Limits(model_calls=3)
    calls = []

    async def script(messages, **kwargs):
        calls.append({"tools": kwargs["tools"], "system": kwargs["system"]})
        if len(calls) == 1:
            yield done("pause_turn")
        elif len(calls) == 2:
            for event in _claim_turn("gapped-claims", CLAIM_WITHOUT_BASIS):
                yield event
        else:
            yield {"type": "text_delta", "text": "合成收尾报告。"}
            yield done()

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)

    assert len(calls) == 3
    # The remaining call is reserved for the report: no gap-correction call.
    assert calls[2]["tools"] == []
    assert all("字段缺口" not in call["system"] for call in calls)
    started = [event for event in events(engine) if event["type"] == "model_started"]
    assert [event.get("claim_pass_attempt") for event in started] == [None, 1, None]
    # The skipped correction is traceable to the budget conflict, not a model omission.
    research = json.loads(outcome.message_payload)["research"]
    assert "claim_retry_skipped_for_report" in research["gaps"]
    saved = ledger.read_research_run(engine.user, engine.run["run_id"])["claims"]
    assert [claim["gaps"] for claim in saved] == [["support_missing", "numeric_basis_missing"]]
    assert outcome.status == "completed" and outcome.error_code is None


async def test_model_started_events_expose_the_allocated_output_budget(engine, monkeypatch):
    # A/B runs read the allocation from the event stream: wrap-up calls get the
    # larger single-shot report budget, ordinary research calls the configured one.
    engine.service.limits = runtime.Limits(model_calls=3)
    calls = []

    async def script(messages, **kwargs):
        calls.append({"tools": kwargs["tools"], "system": kwargs["system"]})
        if len(calls) == 1:
            yield done("pause_turn")
        elif len(calls) == 2:
            for event in _claim_turn("budget-claims", CLAIM_ARGS):
                yield event
        else:
            yield {"type": "text_delta", "text": "合成收尾报告。"}
            yield done()

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)

    started = [event for event in events(engine) if event["type"] == "model_started"]
    assert [event["max_tokens"] for event in started] == [
        engine.service.limits.output_tokens_per_call,
        runtime._WRAP_UP_OUTPUT_TOKENS,
        runtime._WRAP_UP_OUTPUT_TOKENS,
    ]
    assert outcome.status == "completed" and outcome.error_code is None


def _sized_claim_args(count):
    """A claim batch whose encoded size is driven by long, distinct statements."""
    return {
        "claims": [
            {
                **CLAIM_ARGS["claims"][0],
                "statement": "证" * (3000 - len(str(index))) + str(index),
                # A distinct measurement per claim keeps the ledger from folding
                # the batch into one upgraded claim.
                "value": 120 + index,
            }
            for index in range(count)
        ]
    }


# ~135 KB: above the claim tool's own argument cap, so it must be rejected as a
# retryable size error instead of the opaque common-validation rejection run
# 8c746a5a hit.
OVERSIZED_CLAIM_ARGS = _sized_claim_args(15)
# ~45 KB: above the general 32 KiB cap but below the claim tool's 128 KiB cap.
LARGE_CLAIM_ARGS = _sized_claim_args(5)


async def test_oversized_claim_arguments_get_one_retry_as_a_retryable_error(engine, monkeypatch):
    engine.service.limits = runtime.Limits(model_calls=4)
    calls = []

    async def script(messages, **kwargs):
        calls.append({"tools": kwargs["tools"], "system": kwargs["system"]})
        if len(calls) == 1:
            yield done("pause_turn")
        elif len(calls) == 2:
            for event in _claim_turn("oversized-claims", OVERSIZED_CLAIM_ARGS):
                yield event
        elif len(calls) == 3:
            for event in _claim_turn("retried-claims", CLAIM_ARGS):
                yield event
        else:
            yield {"type": "text_delta", "text": "合成收尾报告：2025 年产量 120 万吨。"}
            yield done()

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)

    assert len(calls) == 4
    # The oversized batch is retryable shape feedback, so the claim tool is
    # offered again with the size guidance.
    assert [spec["name"] for spec in calls[2]["tools"]] == ["research_record_claims"]
    assert "参数体积超限" in calls[2]["system"]
    failed = [event for event in events(engine) if event["type"] == "tool_failed"]
    assert [(event["ui"]["error_kind"], event["ui"]["error_paths"]) for event in failed] == [
        ("arguments_too_large", ["arguments:arguments_too_large"])
    ]
    started = [event for event in events(engine) if event["type"] == "model_started"]
    assert [event.get("claim_pass_attempt") for event in started] == [None, 1, 2, None]
    research = json.loads(outcome.message_payload)["research"]
    assert "claim_retry_skipped_for_report" not in research["gaps"]
    saved = ledger.read_research_run(engine.user, engine.run["run_id"])
    assert [claim["statement"] for claim in saved["claims"]] == ["合成公司 2025 年产量 120 万吨。"]
    assert outcome.status == "completed" and outcome.error_code is None


async def test_claim_batch_above_the_general_cap_executes_under_the_claim_tool_cap(engine, monkeypatch):
    engine.service.limits = runtime.Limits(model_calls=4)
    calls = []

    async def script(messages, **kwargs):
        calls.append({"tools": kwargs["tools"], "system": kwargs["system"]})
        if len(calls) == 1:
            yield done("pause_turn")
        elif len(calls) == 2:
            for event in _claim_turn("large-claims", LARGE_CLAIM_ARGS):
                yield event
        else:
            yield {"type": "text_delta", "text": "合成收尾报告。"}
            yield done()

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)

    assert len(calls) == 3
    # The claim tool's larger allowance means this batch is executed once, with
    # no size rejection and no retry call.
    assert not [event for event in events(engine) if event["type"] == "tool_failed"]

    assert [event["tool"] for event in events(engine) if event["type"] == "tool_started"] == [
        "research_record_claims"
    ]
    started = [event for event in events(engine) if event["type"] == "model_started"]
    assert [event.get("claim_pass_attempt") for event in started] == [None, 1, None]
    saved = ledger.read_research_run(engine.user, engine.run["run_id"])
    assert len(saved["claims"]) == 5
    assert outcome.status == "completed" and outcome.error_code is None


async def test_last_call_tool_use_keeps_the_streamed_report(engine, monkeypatch):
    # Production run 4f4165a0: the model answered the forced tool-free report
    # call with text plus one more claim call. The run raised budget_exceeded and
    # lost the already-streamed report. The report text now wins: the stray call
    # is dropped and the degradation is recorded as an advisory gap.
    engine.service.limits = runtime.Limits(model_calls=3)
    calls = []

    async def script(messages, **kwargs):
        calls.append({"tools": kwargs["tools"], "system": kwargs["system"]})
        if len(calls) == 1:
            yield done("pause_turn")
        elif len(calls) == 2:
            for event in _claim_turn("rejected-claims", INVALID_CLAIM_ARGS):
                yield event
        else:
            yield {"type": "text_delta", "text": "合成收尾报告：2025 年产量 120 万吨。"}
            for event in _claim_turn("late-claims", CLAIM_ARGS):
                yield event

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)

    assert len(calls) == 3
    assert [spec["name"] for spec in calls[1]["tools"]] == ["research_record_claims"]
    assert calls[2]["tools"] == []
    # The last call's stray tool_use is not executed: only the rejected claim call
    # reached the tool layer.
    assert [event["tool"] for event in events(engine) if event["type"] == "tool_started"] == [
        "research_record_claims"
    ]
    assert [event for event in events(engine) if event["type"] == "tool_completed"] == []
    assert outcome.status == "completed" and outcome.error_code is None
    assert "2025 年产量 120 万吨" in outcome.text
    research = json.loads(outcome.message_payload)["research"]
    assert "claim_retry_skipped_for_report" in research["gaps"]
    assert "report_tool_use_salvaged" in research["gaps"]
    assert "budget_exceeded" not in json.dumps(research.get("gaps") or [])
