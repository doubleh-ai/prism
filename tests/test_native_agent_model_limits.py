"""Per-model agent limits: catalog overrides merge over untouched global defaults.

DeepSeek is tuned and must keep the global ``Limits`` byte-for-byte; a model
catalog entry may only add a ``limits`` mapping for the provider it was measured
with. Deployment route caps stay authoritative and can only tighten budgets.
"""

import pytest
from test_native_agent_runtime_review import done, execute, last_usage
from test_native_agent_runtime_review import engine as engine

from server import config
from server.agent import runtime

DEEPSEEK = {
    "name": "DeepSeek V4 Pro",
    "base_url": "https://api.deepseek.com/anthropic",
    "api_key": "synthetic",
    "model": "deepseek-v4-pro",
}
ROUTE = {
    "purpose": "research",
    "version": "route-v1",
    "configured": False,
    "model_calls_max": 6,
    "output_tokens_max": 2048,
    "wall_seconds_max": 180,
}


def test_global_limits_defaults_are_unchanged():
    assert runtime.Limits() == runtime.Limits(
        model_calls=10,
        tool_calls=20,
        server_search_calls=4,
        output_tokens_per_call=4096,
        token_continuations=4,
        wall_seconds=480,
        output_characters=60000,
        context_characters=200000,
        concurrent_runs=4,
    )
    assert runtime._WRAP_UP_OUTPUT_TOKENS == 8192
    assert runtime._CLAIM_PASS_MIN_REMAINING_SECONDS == 90


def test_model_without_catalog_limits_keeps_global_defaults():
    limits, wrap_up = runtime._effective_limits(runtime.Limits(), DEEPSEEK)
    assert limits == runtime.Limits()
    assert wrap_up == runtime._WRAP_UP_OUTPUT_TOKENS


def test_route_caps_still_only_tighten_global_defaults():
    model = {**DEEPSEEK, "_purpose_route": ROUTE}
    limits, wrap_up = runtime._effective_limits(runtime.Limits(), model)
    assert limits == runtime.Limits(model_calls=6, output_tokens_per_call=2048, wall_seconds=180)
    assert wrap_up == 2048


def test_minimax_catalog_entry_overrides_only_named_fields():
    entry = config.MODELS["minimax-cn-m3"]
    assert entry["limits"] == {
        "model_calls": 12,
        "tool_calls": 24,
        "output_tokens_per_call": 8192,
        "wall_seconds": 600,
        "wrap_up_output_tokens": 16384,
    }
    limits, wrap_up = runtime._effective_limits(runtime.Limits(), entry)
    assert limits == runtime.Limits(
        model_calls=12,
        tool_calls=24,
        output_tokens_per_call=8192,
        wall_seconds=600,
    )
    assert wrap_up == 16384
    # Fields the entry does not name keep the global default exactly.
    assert limits.server_search_calls == runtime.Limits().server_search_calls
    assert limits.token_continuations == runtime.Limits().token_continuations
    assert limits.output_characters == runtime.Limits().output_characters
    assert limits.context_characters == runtime.Limits().context_characters
    assert limits.concurrent_runs == runtime.Limits().concurrent_runs


def test_only_the_measured_minimax_entry_carries_limits():
    overridden = {key for key, entry in config.MODELS.items() if "limits" in entry}
    assert overridden == {"minimax-cn-m3"}
    for key in ("deepseek-v4", "deepseek-v4-flash"):
        assert "limits" not in config.MODELS[key]


def test_route_caps_still_apply_above_a_model_override():
    model = {**config.MODELS["minimax-cn-m3"], "_purpose_route": ROUTE}
    limits, wrap_up = runtime._effective_limits(runtime.Limits(), model)
    assert limits.model_calls == 6
    assert limits.output_tokens_per_call == 2048
    assert limits.wall_seconds == 180
    assert wrap_up == 2048
    # Non-capped override fields survive the merge.
    assert limits.tool_calls == 24


@pytest.mark.parametrize(
    "override",
    [
        {"model_calls": "12"},
        {"model_calls": 0},
        {"wall_seconds": -1},
        {"concurrent_runs": 99},
        {"bogus": 3},
        {"wrap_up_output_tokens": "16384"},
        {"wrap_up_output_tokens": 0},
        "not-a-mapping",
    ],
)
def test_malformed_overrides_are_ignored(override):
    limits, wrap_up = runtime._effective_limits(runtime.Limits(), {**DEEPSEEK, "limits": override})
    assert limits == runtime.Limits()
    assert wrap_up == runtime._WRAP_UP_OUTPUT_TOKENS


async def test_runtime_consumes_the_model_override_end_to_end(engine, monkeypatch):
    engine.model["limits"] = {
        "model_calls": 3,
        "output_tokens_per_call": 8192,
        "wrap_up_output_tokens": 16384,
    }
    budgets = []

    async def script(*args, **kwargs):
        budgets.append(kwargs["max_tokens"])
        if len(budgets) < 3:
            yield done("pause_turn")
        else:
            yield {"type": "text_delta", "text": "基于已有资料的阶段性判断。"}
            yield done()

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)
    assert outcome.status == "completed"
    # model_calls is the override's, not the global default's.
    assert len(budgets) == 3
    # Normal calls use the model's output budget; the wrap-up report call gets
    # the model's one-shot wrap-up budget instead.
    assert budgets == [8192, 16384, 16384]
    assert last_usage(engine)["model_calls"] == 3


async def test_runtime_keeps_global_budgets_for_deepseek(engine, monkeypatch):
    budgets = []

    async def script(*args, **kwargs):
        budgets.append(kwargs["max_tokens"])
        if len(budgets) < 8:
            yield done("pause_turn")
        else:
            yield {"type": "text_delta", "text": "基于已有资料的阶段性判断。"}
            yield done()

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)
    assert outcome.status == "completed"
    # Global Limits: normal calls stay at 4096 and the forced last-three-calls
    # wrap-up window (index >= model_calls - 3) gets 8192, exactly as before.
    assert budgets == [4096] * 7 + [8192]
