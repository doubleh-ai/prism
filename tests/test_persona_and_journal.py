"""Prism V3 tests: Per-User Persona Engine, Human-Confirmed Traits, Cross-Asset Journal & Omni-Agent Tools."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server import capabilities as cap
from server.agent import store as agent_store
from server.agent import tools as T
from server.database import get_db
from server.journal_hub import init_journal_hub_db
from server.preferences import init_preferences_db, profile_prompt
from server.research import create_research_run
from server.research_context import EvidenceTracker, build_research_context
from server.work_lifecycle import Manager


@pytest.fixture
def two_users(auth_cookies):
    init_preferences_db()
    init_journal_hub_db()
    with get_db() as db:
        db.execute("INSERT INTO users(username,password_hash) VALUES('peer-user','synthetic-only')")
    from server.auth import create_session

    return auth_cookies, {"token": create_session("peer-user")}


async def test_persona_archetypes_negative_rules_and_human_confirmed_trait_workflow(client, two_users):
    owner_cookies, peer_cookies = two_users

    initial = (await client.get("/api/preferences/persona", cookies=owner_cookies)).json()
    assert initial["profile"] == {"configured": False, "version": 0}
    assert {item["id"] for item in initial["archetypes"]} == {"value", "cycle", "trend", "defensive"}
    assert initial["traits"] == []
    assert initial["pending_proposals"] == []

    saved = await client.put(
        "/api/preferences/research",
        cookies=owner_cookies,
        json={
            "expected_version": 0,
            "style": "balanced",
            "horizon": "medium",
            "max_position_pct": 20,
            "max_drawdown_pct": 12,
            "allow_leverage": True,
            "archetype": "cycle",
            "focus_markets": ["ashare", "futures"],
            "negative_rules": ["排除 ST 与退市整理", "回避近5年 >85% 分位追高"],
            "decision_style_note": "先给三行核心结论，再列催化剂与失效位",
        },
    )
    assert saved.status_code == 200
    assert saved.json()["profile"]["archetype"] == "cycle"
    assert len(saved.json()["profile"]["negative_rules"]) == 2

    added = await client.post(
        "/api/preferences/traits",
        cookies=owner_cookies,
        json={"trait_text": "黑色系重点核对钢厂日均铁水与社会库存拐点", "category": "focus"},
    )
    assert added.status_code == 200
    trait_id = added.json()["trait"]["id"]
    assert (
        await client.delete(f"/api/preferences/traits/{trait_id}", cookies=peer_cookies)
    ).status_code == 404

    with get_db() as db:
        db.execute("INSERT INTO conversations(id,username) VALUES('persona-conv','testuser')")
    rctx = build_research_context(run_id="persona-run")
    create_research_run("testuser", "persona-conv", "合成研究", rctx)
    manager = Manager()
    parent = manager.admit("agent", persist_required=False)
    try:
        with cap.native_run_context("testuser", "persona-conv", "persona-run", parent_lease=parent) as ctx:
            native_tools = T.NativeTools(ctx, parent, EvidenceTracker("persona-run"))
            proposed = await native_tools.execute(
                "persona_propose_trait",
                {
                    "trait_text": "单笔止损失效位不超过入场价 6%",
                    "category": "discipline",
                    "reason": "用户在对话中明确要求严格止损纪律",
                },
                "prop-call-1",
            )
        assert proposed["ok"] is True
        assert proposed["data"]["status"] == "pending_user_confirmation"
        proposal_id = proposed["data"]["proposal_id"]

        mid = (await client.get("/api/preferences/persona", cookies=owner_cookies)).json()
        assert len(mid["traits"]) == 1
        assert [p["id"] for p in mid["pending_proposals"]] == [proposal_id]
        assert (
            await client.post(
                f"/api/preferences/proposals/{proposal_id}/decide",
                cookies=peer_cookies,
                json={"decision": "accept"},
            )
        ).status_code == 404

        accepted = await client.post(
            f"/api/preferences/proposals/{proposal_id}/decide",
            cookies=owner_cookies,
            json={"decision": "accept"},
        )
        assert accepted.status_code == 200 and accepted.json()["status"] == "accepted"
        after = (await client.get("/api/preferences/persona", cookies=owner_cookies)).json()
        assert after["pending_proposals"] == []
        assert len(after["traits"]) == 2

        run = agent_store.create_run(
            "testuser",
            "persona-conv",
            "帮我按我的风格诊断当前这只票",
            "mock",
            "company",
            "persona-req-1",
            page_context={
                "surface": "stock_quote",
                "ticker": "300750",
                "name": "宁德时代",
                "period": "1d",
                "summary": "个股复盘 300750 宁德时代",
                "ignored_extra_key": "must-not-persist",
            },
        )
        frozen = agent_store.get_run("testuser", run["id"])["profile"]
        assert frozen["archetype"] == "cycle"
        assert "ignored_extra_key" not in frozen["page_context"]
        prompt = profile_prompt(frozen)
        assert "产业周期与供需派" in prompt
        assert "回避近5年 >85% 分位追高" in prompt
        assert "单笔止损失效位不超过入场价 6%" in prompt
        assert "300750" in prompt and "宁德时代" in prompt
    finally:
        parent.release()
        manager._pool.shutdown(wait=True, cancel_futures=False)


async def test_cross_asset_decision_journal_crud_and_live_quote_reconcile(client, two_users, monkeypatch):
    owner_cookies, peer_cookies = two_users

    jm_res = await client.post(
        "/api/futures/JM/research",
        cookies=owner_cookies,
        json={"session": "2026-11-19", "question": "近月和远月的价差怎样变化？"},
    )
    assert jm_res.status_code == 200
    jm_saved = await client.post(
        "/api/futures/JM/judgments",
        cookies=owner_cookies,
        json={"analysis_id": jm_res.json()["analysis_id"], "choice_id": "curve"},
    )
    assert jm_saved.status_code == 200

    rb_res = await client.post(
        "/api/futures/RB/research",
        cookies=owner_cookies,
        json={"session": "2026-11-16", "question": "去库是需求回暖还是钢厂减产？"},
    )
    assert rb_res.status_code == 200
    rb_saved = await client.post(
        "/api/futures/RB/judgments",
        cookies=owner_cookies,
        json={"analysis_id": rb_res.json()["analysis_id"], "choice_id": "society_down"},
    )
    assert rb_saved.status_code == 200

    created = await client.post(
        "/api/journal/equity",
        cookies=owner_cookies,
        json={
            "ticker": "600519",
            "name": "贵州茅台",
            "direction": "bullish",
            "thesis": "回踩半年线缩量企稳，批价走稳",
            "reference_price": 1500.0,
            "target_price": 1620.0,
            "invalidation_price": 1440.0,
            "horizon_days": 15,
        },
    )
    assert created.status_code == 200
    jid = created.json()["item"]["id"]

    overview = (await client.get("/api/journal/overview", cookies=owner_cookies)).json()
    assert overview["stats"]["total"] == 3
    assert overview["stats"]["futures_total"] == 2
    assert overview["stats"]["equity_total"] == 1
    assert {item["name"] for item in overview["items"]} == {"焦煤", "螺纹钢", "贵州茅台"}
    persona_snap = await client.get("/api/preferences/persona", cookies=owner_cookies)
    assert persona_snap.status_code == 200
    assert persona_snap.json()["journal_stats"]["futures_total"] == 2

    assert (await client.get("/api/journal/overview", cookies=peer_cookies)).json()["stats"]["total"] == 0
    assert (
        await client.post(f"/api/journal/equity/{jid}/reconcile", cookies=peer_cookies)
    ).status_code == 404

    from server.market import factory as market_factory

    class MockChain:
        async def get_quote(self, ticker):
            assert ticker == "600519"
            return SimpleNamespace(price=1635.0, source="synthetic"), False

    monkeypatch.setattr(market_factory, "get_chain", lambda: MockChain())
    reconciled = await client.post(f"/api/journal/equity/{jid}/reconcile", cookies=owner_cookies)
    assert reconciled.status_code == 200
    assert reconciled.json()["item"]["status"] == "supported"
    assert "1635.00" in reconciled.json()["item"]["review"]["summary"]

    updated = (await client.get("/api/journal/overview", cookies=owner_cookies)).json()
    assert updated["stats"]["supported"] == 1
    assert updated["stats"]["win_rate_pct"] == 100.0

    deleted = await client.delete(f"/api/journal/equity/{jid}", cookies=owner_cookies)
    assert deleted.status_code == 204
    assert (await client.get("/api/journal/overview", cookies=owner_cookies)).json()["stats"][
        "equity_total"
    ] == 0


async def test_futures_intelligence_native_tool_returns_v2_causal_and_chain_bridge(test_user, monkeypatch):
    from server import futures_workspace

    with get_db() as db:
        db.execute("INSERT INTO conversations(id,username) VALUES('fut-intel-conv','testuser')")
    rctx = build_research_context(run_id="fut-intel-run")
    create_research_run("testuser", "fut-intel-conv", "合成黄金研究", rctx)
    monkeypatch.setattr(
        futures_workspace,
        "card",
        AsyncMock(
            return_value={
                "symbol": "AU",
                "name": "黄金",
                "exchange": "SHFE",
                "generated_at": "2026-11-27T12:00:00+08:00",
                "archetype_label": "贵金属宏观锚",
                "market": {"status": "collected", "data_as_of": "2026-11-27", "values": {"close": 767.62}},
                "technicals": {"status": "collected", "values": {"ema20": 772.0, "ema60": 780.0}},
                "basis": {"status": "collected", "values": {"basis": -0.55, "basis_pct": -0.07}},
                "inventory": {"status": "collected", "values": {"quantity": 15120}},
                "spread": {"status": "collected", "values": {"far_minus_near": 16.62}},
                "roll_guard": {"roll_suspect": False},
                "margins": [],
                "causal_view": {"headline": "近端现实与盘面趋势劈叉"},
                "chain_canvas": {"chain_name": "贵金属宏观与避险链", "stages": []},
                "research_brief": {
                    "fundamentals": {"rows": []},
                    "technical": {"label": "短中期分歧"},
                    "institutions": {"status": "unavailable", "items": []},
                    "pre_interpretation": {"summary": "预核验摘要"},
                },
            }
        ),
    )
    manager = Manager()
    parent = manager.admit("agent", persist_required=False)
    try:
        with cap.native_run_context(
            "testuser", "fut-intel-conv", "fut-intel-run", parent_lease=parent
        ) as ctx:
            native_tools = T.NativeTools(ctx, parent, EvidenceTracker("fut-intel-run"))
            res = await native_tools.execute("futures_intelligence", {"symbol": "au"}, "call-au")
        assert res["ok"] is True
        assert res["data"]["symbol"] == "AU"
        assert "期货升水抢跑预期" in res["data"]["causal_view"]["headline"]
        assert res["call"]["target"]["href"] == "/futures?symbol=AU"
    finally:
        parent.release()
        manager._pool.shutdown(wait=True, cancel_futures=False)
