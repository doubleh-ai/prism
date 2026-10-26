"""Research provenance regressions; all engine events and sources are synthetic."""

import json
from datetime import UTC, datetime

import pytest

from server.research_context import (
    EvidenceTracker,
    build_research_context,
    build_research_prompt,
    parse_claude_jsonl_events,
)


def _call(call_id, name="WebSearch", **inputs):
    return {"type": "tool_use", "id": call_id, "name": name, "input": inputs}


def _result(call_id, content="[Disclosure](https://example.test/disclosure)", **extra):
    return {"type": "tool_result", "tool_use_id": call_id, "content": content, **extra}


def _event(role, blocks, **extra):
    return {"type": role, "message": {"content": blocks}, **extra}


def test_research_clock_crosses_midnight_in_shanghai_and_prompt_does_not_claim_current_data():
    context = build_research_context(run_id="round-one", now=datetime(2026, 10, 20, 18, tzinfo=UTC))
    assert context["date"] == "2026-10-21"
    assert context["as_of"] == "2026-10-21T02:00:00+08:00"
    assert context["timezone"] == "Asia/Shanghai"
    assert "不代表数据已经更新至此" in build_research_prompt(context)
    prompt = build_research_prompt(context)
    assert all(source in prompt for source in ("交易所披露", "公司公告", "IR", "财联社", "金十", "新浪财经"))
    assert "须追溯原始发布" in prompt
    assert "不因来源名称自动判定可信" in prompt


@pytest.mark.parametrize("name", ["Bash", "Read", "mcp__other__WebSearch"])
def test_arbitrary_tool_links_are_not_external_evidence(name):
    tracker = EvidenceTracker("run")
    tracker.record_call(_call("tool-1", name))
    assert tracker.record_result(_result("tool-1")) == []


def test_only_known_successful_web_calls_produce_sources_and_dates_remain_unknown():
    tracker = EvidenceTracker("run")
    assert tracker.record_result(_result("orphan")) == []
    tracker.record_call(_call("error"))
    assert tracker.record_result(_result("error", is_error=True)) == []
    tracker.record_call(_call("false-success"))
    assert (
        tracker.record_result(
            _result("false-success", '{"ok":false,"sources":[{"url":"https://example.test"}]}')
        )
        == []
    )
    tracker.record_call(_call("ok"))
    refs = tracker.record_result(_result("ok"))
    assert refs[0]["tool_call_id"] == "ok"
    assert refs[0]["run_id"] == "run"
    assert refs[0]["source_kind"] == "external_web"
    assert refs[0]["evidence_status"] == "retrieved"
    assert refs[0]["retrieved_at"] is None
    assert refs[0]["published_at"] is None
    assert tracker.record_result(_result("ok")) == []


def test_search_json_links_and_fetch_target_have_different_provenance():
    search = EvidenceTracker("search")
    search.record_call(_call("s"))
    refs = search.record_result(_result("s", 'Links: [{"title":"Notice","url":"https://example.test/a"}]'))
    assert refs[0]["url"] == "https://example.test/a"
    fetch = EvidenceTracker("fetch")
    fetch.record_call(_call("f", "WebFetch", url="https://example.test/a"))
    refs = fetch.record_result(_result("f", "Page links to [unfetched](https://other.test/b)"))
    assert [ref["url"] for ref in refs] == ["https://example.test/a"]
    assert refs[0]["retrieval_type"] == "fetched_page"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/page?ACCESS_TOKEN=synthetic",
        "https://example.test/page?Access%5fToken=synthetic",
        "https://example.test/page?API-Key=synthetic",
        "https://example.test/page#access_token=synthetic",
        "https://example.test/page#/oauth?Refresh-Token=synthetic",
        "https://example.test/page?X-Amz-Signature=synthetic",
    ],
)
def test_credential_parameters_never_become_source_links(url):
    tracker = EvidenceTracker("synthetic-run")
    tracker.record_call(_call("fetch", "WebFetch", url=url))
    assert tracker.record_result(_result("fetch", "ordinary page content")) == []


@pytest.mark.parametrize(
    "content",
    [
        "API Error: 403 forbidden",
        'API Error: 401\n{"message":"Unauthorized"}',
        "Request failed with status code 404",
        "Error fetching the requested URL",
        '{"isError":true,"message":"source unavailable"}',
    ],
)
def test_explicit_fetch_failure_notices_do_not_become_retrieved_sources(content):
    tracker = EvidenceTracker("synthetic-run")
    tracker.record_call(_call("fetch", "WebFetch", url="https://example.test/disclosure"))
    assert tracker.record_result(_result("fetch", content)) == []


@pytest.mark.parametrize(
    "content",
    [
        "Error handling in APIs: a reference guide.",
        '# Error handling\nThis article explains {"ok":false} and {"status":"error"} responses.',
        "# API Error: 403\nA documentation page about diagnosing authorization errors.",
        "A study of failed experiments and error rates.",
    ],
)
def test_error_discussion_inside_a_successful_page_is_still_a_source(content):
    tracker = EvidenceTracker("synthetic-run")
    url = "https://example.test/docs?q=error#examples"
    tracker.record_call(_call("fetch", "WebFetch", url=url))
    refs = tracker.record_result(_result("fetch", content))
    assert [ref["url"] for ref in refs] == [url]


@pytest.mark.parametrize(
    "tool,kind",
    [("mcp__prism__market_quote", "platform_data"), ("mcp__prism__report_read", "historical_research")],
)
def test_platform_and_history_are_never_relabelled_external(tool, kind):
    tracker = EvidenceTracker("run")
    tracker.record_call(_call("p", tool))
    refs = tracker.record_result(_result("p"))
    assert refs[0]["source_kind"] == kind


def test_tool_only_results_do_not_create_fake_user_questions():
    assert parse_claude_jsonl_events([_event("user", [_result("unmatched")])]) == []


async def test_permanent_delete_requires_owner_and_trash_state(client, auth_cookies):
    from server.database import get_db

    with get_db() as db:
        for cid, owner, deleted in [
            ("foreign", "someone-else", 1),
            ("active", "testuser", None),
            ("trash", "testuser", 1),
        ]:
            db.execute(
                "INSERT INTO conversations(id,username,deleted_at) VALUES(?,?,?)", [cid, owner, deleted]
            )
            db.execute("INSERT INTO messages(conv_id,role,content) VALUES(?,?,?)", [cid, "user", "keep"])
    assert (await client.delete("/api/convs/foreign/permanent", cookies=auth_cookies)).status_code == 404
    assert (await client.delete("/api/convs/active/permanent", cookies=auth_cookies)).status_code == 409
    assert (await client.delete("/api/convs/trash/permanent", cookies=auth_cookies)).status_code == 200
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM messages WHERE conv_id='foreign'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM messages WHERE conv_id='active'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM messages WHERE conv_id='trash'").fetchone()[0] == 0


def test_fake_links_and_tool_volume_never_upgrade_vibe_trust():
    from server.vibe import _quality_after_report

    quality = _quality_after_report(
        {"quality": {"tool_calls": 99, "tool_success": 99}},
        "[a](https://example.invalid/a) [b](https://example.invalid/b) [c](https://example.invalid/c)",
    )
    assert quality["source_links"] == 3
    assert quality["grade"] == "来源待核验"
    assert quality["verified_sources"] == 0


def test_legacy_vibe_heuristic_grade_is_not_displayed_as_verification():
    from server.vibe import _row_to_plan

    row = {
        "id": "legacy",
        "created_at": 1,
        "plan_json": json.dumps({"quality": {"grade": "证据较充分", "tool_calls": 10}}),
    }
    quality = _row_to_plan(row)["quality"]
    assert quality["grade"] == "来源待核验"
    assert quality["verified_sources"] == 0
    assert quality["tool_calls"] == 10


def test_vibe_attempt_counters_are_replay_safe_and_do_not_leak_between_followups():
    from server.vibe import _apply_engine_event, _begin_attempt

    plan = {"engine_attempt_id": "first", "activity": []}
    _begin_attempt(plan, "first")
    for _ in range(2):
        _apply_engine_event(plan, "tool_call", {"tool": "web_search", "tool_call_id": "call"}, "e1")
        _apply_engine_event(
            plan, "tool_result", {"tool": "web_search", "tool_call_id": "call", "preview": "result"}, "e2"
        )
    _apply_engine_event(plan, "tool_call", {"tool": "web_search", "tool_call_id": "call"}, "new-event-id")
    assert plan["quality"]["tool_calls"] == plan["quality"]["tool_success"] == 1
    _begin_attempt(plan, "first")
    assert plan["quality"]["tool_success"] == 1
    plan["engine_attempt_id"] = "second"
    _begin_attempt(plan, "second")
    _apply_engine_event(plan, "tool_call", {"tool": "web_search", "attempt_id": "first"}, "old")
    _apply_engine_event(plan, "tool_call", {"tool": "web_search", "tool_call_id": "call"}, "e1")
    _apply_engine_event(
        plan, "tool_result", {"tool": "web_search", "tool_call_id": "call", "preview": "未获取到数据"}, "e2"
    )
    assert plan["quality"]["tool_calls"] == 1
    assert plan["quality"]["tool_success"] == 0
    assert plan["quality"]["tool_degraded"] == 1
    assert plan["activity"][-1]["published_at"] is None
    assert plan["activity"][-1]["retrieved_at"] is None
    assert plan["activity"][-1]["observed_at"].endswith("+08:00")


def test_vibe_simultaneous_calls_match_by_id_not_last_tool_name():
    from server.vibe import _apply_engine_event, _begin_attempt

    plan = {"engine_attempt_id": "attempt", "activity": []}
    _begin_attempt(plan, "attempt")
    for call_id in ("first", "second"):
        _apply_engine_event(plan, "tool_call", {"tool": "web_search", "call_id": call_id}, call_id)
    _apply_engine_event(
        plan, "tool_result", {"tool": "web_search", "call_id": "first", "preview": "result"}, "result"
    )
    assert plan["activity"][0]["status"] == "complete"
    assert plan["activity"][1]["status"] == "running"


async def test_vibe_followup_gets_new_clock_without_rewriting_original_report(
    client, auth_cookies, monkeypatch
):
    from server import vibe

    old = build_research_context(run_id="old", now=datetime(2026, 7, 2, tzinfo=UTC), engine="vibe")
    new = build_research_context(run_id="new", now=datetime(2026, 10, 21, tzinfo=UTC), engine="vibe")
    plan = {
        "id": "clock",
        "prompt": "research",
        "market": "A股",
        "horizon": "swing",
        "risk_level": "balanced",
        "created_at": 1,
        "status": "complete",
        "engine_session_id": "session",
        "report_markdown": "original",
        "report_research": old,
        "research": old,
        "quality": {"tool_calls": 9},
    }
    vibe._insert_vibe_plan("testuser", plan)
    # Pure mocked legacy clock contract; the shipped execution gate stays closed.
    monkeypatch.setattr(vibe, "require_vibe_execution", lambda: None)
    monkeypatch.setattr(vibe, "build_research_context", lambda **kwargs: new)
    monkeypatch.setattr(vibe, "_start_task", lambda *args, **kwargs: None)
    response = await client.post(
        "/api/vibe/plans/clock/follow-up", cookies=auth_cookies, json={"question": "最新进展？"}
    )
    assert response.status_code == 200
    result = response.json()["plan"]
    assert result["research"] == result["followups"][0]["research"] == new
    assert result["report_research"] == old
    assert result["quality"]["tool_calls"] == 0
    assert result["report_quality"]["tool_calls"] == 9
    assert result["engine_attempt_id"] == ""
    prompt = vibe._followup_prompt(result, "最新进展？")
    assert new["as_of"] in prompt
    assert old["as_of"] not in prompt
