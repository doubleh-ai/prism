"""Prism Agent V4 redesign tests: dual-speed mode, tool-turn folding, and expanded industry seeds."""

import json

import pytest
from test_native_agent_runtime_review import done, execute
from test_native_agent_runtime_review import engine as engine

from server.agent import runtime, store


async def test_compact_mode_uses_fast_toolset_skips_claim_pass_and_allows_market_figures(engine, monkeypatch):
    captured_tools = []
    captured_systems = []

    async def fake_prefetch(page_ctx):
        assert page_ctx["ticker"] == "601138"
        return "\n【当前页面已预注的实时平台快照】\n- 实时行情：工业富联(601138) 现价 44.74 元，总市值 8878.25 亿元"

    async def script(messages, *, system, tools, **kwargs):
        captured_tools.append([spec.get("name") for spec in tools])
        captured_systems.append(system)
        yield {
            "type": "text_delta",
            "text": "**【核心结论】**\n工业富联(601138) 现价 44.74 元，总市值 8878.25 亿元，处于 AI 服务器整机核心环节。",
        }
        yield done("end_turn")

    monkeypatch.setattr(runtime, "_prefetch_page_context_brief", fake_prefetch)
    monkeypatch.setattr(runtime, "model_stream", script)

    run = store.create_run(
        engine.user,
        engine.run["conv_id"],
        "梳理工业富联上下游",
        "minimax-cn-m3",
        "industry",
        page_context={
            "surface": "stock_quote",
            "ticker": "601138",
            "name": "工业富联",
            "period": "1w",
            "response_mode": "compact",
        },
    )
    engine.run = run
    outcome = await execute(engine)

    assert outcome.status == "completed"
    assert len(captured_tools) == 1
    assert "source_original" not in captured_tools[0]
    assert "research_record_claims" not in captured_tools[0]
    assert "source_find" in captured_tools[0]
    assert "预注的实时平台快照" in captured_systems[0]
    assert "8878.25 亿元" in outcome.text
    # Fast copilot must not be withheld by the deep PDF original_required gate.
    payload = json.loads(outcome.message_payload)
    assert payload["report_status"] != "blocked"
    assert payload["research"]["response_mode"] == "compact"


async def test_intermediate_tool_turn_chatter_folds_out_of_main_answer(engine, monkeypatch):
    turn = 0

    async def script(messages, *, system, tools, **kwargs):
        nonlocal turn
        turn += 1
        if turn == 1:
            yield {"type": "text_delta", "text": "I'll check the indicator list first."}
            yield {"type": "tool_call", "id": "call_1", "name": "indicator_list", "arguments": {}}
            yield done("tool_use")
        else:
            yield {"type": "text_delta", "text": "**【核心结论】**\n已核对指标列表，结论清晰。"}
            yield done("end_turn")

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)

    assert outcome.status == "completed"
    assert "I'll check the indicator list first." not in outcome.text
    assert "【核心结论】" in outcome.text
    payload = json.loads(outcome.message_payload)
    think_chunks = [c for c in payload["chunks"] if c.get("t") == "think"]
    text_chunks = [c for c in payload["chunks"] if c.get("t") == "text"]
    assert any("I'll check the indicator list first." in c.get("c", "") for c in think_chunks)
    assert all("I'll check the indicator list first." not in c.get("c", "") for c in text_chunks)
    events = store.list_events(engine.user, engine.run["run_id"])
    assert any(ev.get("type") == "turn_folded" and ev.get("ui", {}).get("fold_to_think") for ev in events)


@pytest.mark.asyncio
async def test_ai_compute_seed_graph_includes_foxconn_industrial_and_commodity_anchors(client):
    resp = await client.get("/api/industry/graph?ticker=601138&depth=1")
    assert resp.status_code == 200
    data = resp.json()
    tickers = {n.get("source_ticker") for n in data["nodes"]}
    assert "601138" in tickers
    assert len(data["nodes"]) >= 4
    assert len(data["edges"]) >= 3

    overview = await client.get("/api/industry/overview")
    assert overview.status_code == 200
    cats = {c["category"]: c for c in overview.json()["categories"]}
    assert "AI算力与服务器" in cats
    fut_syms = {f["symbol"] for f in cats["AI算力与服务器"]["linked_futures"]}
    assert {"CU", "SN", "AL"} <= fut_syms
