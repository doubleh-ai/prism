"""Native runtime: one bounded claim-registration pass in the concluding turn.

Production evidence (run b4f44d9eeb64bbdc6c4fa00c87c76a3d): the wrap-up turn
disabled every tool, so research_record_claims could never run, research_claims
stayed empty and claim_reconciliation reported verified_claim_ratio=null with
all report numbers unsupported. The concluding turn must therefore keep exactly
that one tool for a single bounded pass, then really go tool-free.
"""

from test_native_runtime_continuation import _tool_use_turn, done, execute
from test_native_runtime_continuation import engine as engine

from server.agent import runtime, tools
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
    assert "先登记关键数字，再直接写报告正文。" in calls[1]["system"]
    assert "不要调用其它工具、不要继续检索" in calls[1]["system"]
    # The next concluding call is genuinely tool-free: the bounded pass is spent.
    assert calls[2]["tools"] == []
    assert "本轮已经不能再登记 claim" in calls[2]["system"]
    assert outcome.status == "completed" and outcome.error_code is None
    # The model's tool call really reached the ledger, not just the fake runtime.
    saved = ledger.read_research_run(engine.user, engine.run["run_id"])
    assert [claim["statement"] for claim in saved["claims"]] == ["合成公司 2025 年产量 120 万吨。"]


async def test_exhausted_model_call_budget_never_offers_the_claim_tool(engine, monkeypatch):
    engine.service.limits = runtime.Limits(model_calls=2, context_characters=24000)
    _claim_pass_patch(monkeypatch, OVERSIZED_READ)
    calls = []

    async def script(messages, **kwargs):
        calls.append({"tools": kwargs["tools"], "system": kwargs["system"]})
        if len(calls) == 1:
            yield _tool_use_turn("budget-read")
            yield done("tool_use", content=[{"type": "tool_use", "id": "budget-read"}])
        else:
            yield {"type": "text_delta", "text": "合成收尾报告。"}
            yield done()

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)

    assert len(calls) == 2
    # The last permitted model call is reserved for the answer: a claim pass would
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
