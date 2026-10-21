import asyncio

from server.models import VibePlanRequest
from server.vibe import (
    _apply_engine_event,
    _engine_iteration_budget,
    _extract_candidates,
    _quality_after_report,
    _structured_text,
    _tool_result_summary,
    build_vibe_plan,
)

REPORT = """# 存储产业深度研究

> 存储至少包含存储芯片、企业数据存储与能源储能三条路线，本次根据 A 股纯度与证据质量聚焦存储芯片。

## 证据账本

- 行情与财务数据来自 [东方财富](https://example.com/eastmoney)，数据截至 2026-07-10。
- 行业供需由 [行业来源](https://example.com/industry) 交叉核验。

## 候选池

| # | 代码 | 名称 | 纯度 | 角色 | 核心证据 | 风险 |
|---|---|---|---|---|---|---|
| 1 | **688525** | **佰维存储** | 高 | 存储模组 | 财务与价格周期验证 | 原厂扩产 |
| 2 | **301308** | **江波龙** | 高 | 企业级 SSD 与模组 | 收入结构验证 | 周期反转 |

## 数据质量与缺口

一个板块接口失败，相关结论标记为未验证。"""


def test_build_vibe_plan_is_an_official_research_brief_not_rule_answer():
    plan = build_vibe_plan(
        VibePlanRequest(
            prompt="存储相关产业帮我分析一下",
            market="A股",
            horizon="swing",
            risk_level="balanced",
            research_depth="deep",
        )
    )

    assert plan["engine"]["name"] == "HKUDS/Vibe-Trading"
    assert plan["engine"]["mode"] == "official_sidecar"
    assert plan["status"] == "queued"
    assert plan["candidates"] == []
    assert plan["report_markdown"] == ""
    assert plan["iteration_budget"] == 14
    assert plan["estimated_duration_label"] == "约 3-6 分钟"
    assert "半导体设备" not in plan["title"]


def test_vibe_depths_have_bounded_official_agent_iterations():
    assert _engine_iteration_budget("focused") == 6
    assert _engine_iteration_budget("deep") == 14
    assert _engine_iteration_budget("swarm") == 28
    assert _engine_iteration_budget("unknown") == 14


def test_extract_candidates_from_official_heading_format():
    report = """## A 股候选池

#### 候选1：澜起科技（688008.SH）

**产业链角色：** DDR5 内存接口芯片

**证据：** 全球份额和财务数据已核验

#### 候选2：江波龙（301308.SZ）

**产业链角色：** 存储模组
"""
    candidates = _extract_candidates(report)
    assert [item["ticker"] for item in candidates] == ["688008", "301308"]
    assert candidates[0]["name"] == "澜起科技"
    assert "DDR5" in candidates[0]["role"]
    assert "|" not in candidates[0]["role"]

    table_report = """#### 候选1：澜起科技（688008.SH）

| 维度 | 内容 |
|---|---|
| 产业链角色 | DDR5 内存接口芯片 |
| 主题纯度 | 高 |
"""
    table_candidate = _extract_candidates(table_report)[0]
    assert table_candidate["role"] == "DDR5 内存接口芯片"
    assert "主题纯度：高" in table_candidate["why"]


def test_vibe_structured_answer_and_disclosed_gaps_are_readable():
    answer = {"type": "text", "content": [{"type": "text", "text": "# 完整报告"}]}
    assert _structured_text(answer) == "# 完整报告"

    quality = _quality_after_report({"quality": {"tool_calls": 8, "tool_success": 8}}, REPORT)
    assert quality["tool_errors"] == 0
    assert quality["reported_gaps"] >= 1
    assert any("数据源或验证缺口" in item for item in quality["warnings"])

    plan = {"quality": {}, "activity": [], "created_at": 1}
    _apply_engine_event(plan, "tool_call", {"tool": "web_search"}, "one")
    _apply_engine_event(
        plan,
        "tool_result",
        {"tool": "web_search", "status": "ok", "preview": '{"ok": false, "error": "provider unavailable"}'},
        "two",
    )
    assert plan["quality"]["tool_errors"] == 1
    assert plan["quality"]["tool_success"] == 0


def test_vibe_tool_failures_expose_classification_without_raw_provider_detail():
    connection_error = (
        '{"ok": false, "error": "ranking_request failed: '
        "HTTPConnectionPool(host='push2.eastmoney.com', port=443): Max retries exceeded"
        '"}'
    )
    assert _tool_result_summary("get_sector_info", connection_error, "error") == (
        "外部数据源暂时不可达，已跳过并记录缺口"
    )
    assert (
        _tool_result_summary(
            "web_search",
            '{"status":"ok","query":"HBM","results":[],"note":"No results found"}',
            "warning",
        )
        == "本次检索未找到可用公开证据"
    )

    plan = {"quality": {}, "activity": [], "created_at": 1}
    _apply_engine_event(plan, "tool_call", {"tool": "get_sector_info"}, "call")
    _apply_engine_event(
        plan,
        "tool_result",
        {"tool": "get_sector_info", "status": "error", "preview": connection_error},
        "result",
    )
    activity = plan["activity"][-1]
    assert activity["result"] == "外部数据源暂时不可达，已跳过并记录缺口"
    assert "HTTPConnectionPool" not in activity["result"]
    assert activity["detail"] == activity["result"]


async def test_vibe_draft_crud(client, auth_cookies):
    created = await client.post(
        "/api/vibe/plan",
        cookies=auth_cookies,
        json={
            "prompt": "人形机器人执行器和减速器上游卡点",
            "market": "A股",
            "horizon": "position",
            "risk_level": "defensive",
            "research_depth": "focused",
            "agent": False,
        },
    )
    assert created.status_code == 200
    plan = created.json()["plan"]
    assert plan["id"]
    assert plan["status"] == "draft"
    assert plan["agent_status"] == "not_started"
    assert plan["risk_label"] == "防守"

    listed = await client.get("/api/vibe/plans", cookies=auth_cookies)
    assert listed.status_code == 200
    assert any(item["id"] == plan["id"] for item in listed.json()["plans"])

    fetched = await client.get(f"/api/vibe/plans/{plan['id']}", cookies=auth_cookies)
    assert fetched.status_code == 200
    assert fetched.json()["plan"]["id"] == plan["id"]

    deleted = await client.delete(f"/api/vibe/plans/{plan['id']}", cookies=auth_cookies)
    assert deleted.status_code == 200
    missing = await client.get(f"/api/vibe/plans/{plan['id']}", cookies=auth_cookies)
    assert missing.status_code == 404


async def test_official_vibe_run_persists_tools_report_and_followup(client, auth_cookies, monkeypatch):
    state = {"attempt": 0, "terminal": set()}

    async def fake_create(title, config=None):
        assert config["research_depth"] == "deep"
        assert config["max_iterations"] == 14
        return {"session_id": "official-session"}

    async def fake_send(session_id, content):
        assert session_id == "official-session"
        assert "研究协议" in content or "同一研究会话" in content
        state["attempt"] += 1
        return {"attempt_id": f"attempt-{state['attempt']}"}

    async def fake_messages(session_id, limit=100):
        messages = []
        for attempt in sorted(state["terminal"]):
            content = (
                REPORT
                if attempt == "attempt-1"
                else "## 增量核验\n\n追问已使用 [新来源](https://example.com/new) 完成复核。"
            )
            messages.append({"role": "assistant", "linked_attempt_id": attempt, "content": content})
        return messages

    async def fake_events(session_id):
        attempt = f"attempt-{state['attempt']}"
        yield "attempt.started", {}, "e1"
        yield "tool_call", {"tool": "web_search", "arguments": {"query": "存储产业 A股"}}, "e2"
        yield (
            "tool_result",
            {"tool": "web_search", "status": "ok", "elapsed_ms": 120, "preview": "2 sources"},
            "e3",
        )
        yield "tool_call", {"tool": "get_financial_statements", "arguments": {"symbol": "688525.SH"}}, "e4"
        yield (
            "tool_result",
            {"tool": "get_financial_statements", "status": "ok", "elapsed_ms": 220, "preview": "financials"},
            "e5",
        )
        yield "thinking_done", {}, "e6"
        state["terminal"].add(attempt)
        yield "attempt.completed", {"summary": "done"}, "e7"

    async def fake_delete(session_id):
        return None

    monkeypatch.setattr("server.vibe.engine_create_session", fake_create)
    monkeypatch.setattr("server.vibe.engine_send_message", fake_send)
    monkeypatch.setattr("server.vibe.engine_get_messages", fake_messages)
    monkeypatch.setattr("server.vibe.engine_events", fake_events)
    monkeypatch.setattr("server.vibe.engine_delete_session", fake_delete)

    created = await client.post(
        "/api/vibe/plan",
        cookies=auth_cookies,
        json={
            "prompt": "存储相关产业帮我分析一下",
            "market": "A股",
            "horizon": "swing",
            "risk_level": "balanced",
            "research_depth": "deep",
            "agent": True,
        },
    )
    assert created.status_code == 200
    plan = created.json()["plan"]
    assert plan["status"] == "queued"
    assert plan["candidates"] == []

    for _ in range(20):
        await asyncio.sleep(0.02)
        fetched = await client.get(f"/api/vibe/plans/{plan['id']}", cookies=auth_cookies)
        done = fetched.json()["plan"]
        if done["status"] == "complete":
            break
    assert done["status"] == "complete"
    assert done["agent_status"] == "official_complete"
    assert done["report_markdown"].startswith("# 存储产业深度研究")
    assert done["quality"]["tool_calls"] == 2
    assert done["quality"]["source_links"] == 2
    assert any(item["status"] == "complete" for item in done["activity"])
    assert [item["ticker"] for item in done["candidates"]] == ["688525", "301308"]

    followup = await client.post(
        f"/api/vibe/plans/{plan['id']}/follow-up",
        cookies=auth_cookies,
        json={"question": "重新核验财务数字和来源"},
    )
    assert followup.status_code == 200
    assert followup.json()["plan"]["status"] == "queued"

    for _ in range(20):
        await asyncio.sleep(0.02)
        fetched = await client.get(f"/api/vibe/plans/{plan['id']}", cookies=auth_cookies)
        followed = fetched.json()["plan"]
        if followed["status"] == "complete" and followed["followups"][-1]["status"] == "complete":
            break
    assert "增量核验" in followed["followups"][-1]["answer_markdown"]


async def test_official_engine_failure_never_returns_rule_candidates(client, auth_cookies, monkeypatch):
    async def fail_create(title, config=None):
        raise RuntimeError("sidecar offline")

    monkeypatch.setattr("server.vibe.engine_create_session", fail_create)
    created = await client.post(
        "/api/vibe/plan",
        cookies=auth_cookies,
        json={"prompt": "存储", "agent": True},
    )
    plan = created.json()["plan"]
    for _ in range(20):
        await asyncio.sleep(0.02)
        fetched = await client.get(f"/api/vibe/plans/{plan['id']}", cookies=auth_cookies)
        failed = fetched.json()["plan"]
        if failed["status"] == "failed":
            break
    assert failed["status"] == "failed"
    assert failed["candidates"] == []
    assert failed["report_markdown"] == ""
    assert "未用规则结果冒充分析" in failed["agent_summary"]


async def test_vibe_requires_auth(client):
    response = await client.post("/api/vibe/plan", json={"prompt": "AI 液冷"})
    assert response.status_code == 401
