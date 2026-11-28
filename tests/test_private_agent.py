"""Tests for Circle Intelligence Pool, Private Personal Agent, Personal Watchman, and Batch Admin Quotas."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _login(client: TestClient, username: str, password: str = "pass123456"):
    from server.auth import hash_password
    from server.database import create_user, user_exists

    if not user_exists(username):
        create_user(username, hash_password(password))
    r = client.post(
        "/api/login",
        json={"username": username, "password": password},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert r.status_code == 200


def test_circle_intel_structuring_and_strict_privacy_isolation(monkeypatch, tmp_path):
    from server import config, database, private_agent
    from server.main import app

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test_intel.db")
    database.reset_connection()
    database.init_db()
    private_agent.init_private_agent_db()
    monkeypatch.setattr(
        private_agent,
        "fetch_fast_quotes",
        lambda codes: {
            "sh000300": {"code": "sh000300", "name": "沪深300", "price": 4050.0, "change_pct": 2.3}
        },
    )
    monkeypatch.setattr(config, "ADMIN_USERS", {"admin"})

    c_alice = TestClient(app)
    c_bob = TestClient(app)
    _login(c_alice, "alice")
    _login(c_bob, "bob")

    # 1. Alice posts an anonymous circle strategy note (matching the user's real noon example)
    sample = (
        "现在是政策预期拉动的快速反弹，不适合一把重仓追涨。"
        "如果手里已有底仓、风险承受力高，可以小仓位分批加，优先选业绩兑现的主线龙头或者 ETF；"
        "空仓的不急着冲，等后面看量能能不能稳住、细则落地情况，再找回调机会；"
        "如果本身仓位已经很重，反而要留意冲高分化风险，适当降低仓位（供参考）。"
    )
    r1 = c_alice.post(
        "/api/intel",
        json={"raw_text": sample, "visibility": "circle", "anonymous": True},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert r1.status_code == 200
    it1 = r1.json()["item"]
    assert it1["category"] == "strategy"
    assert len(it1["branches"]) >= 3
    assert it1["market_check"]["verdict"] == "volatile"

    # 2. Alice posts a strictly private note
    r2 = c_alice.post(
        "/api/intel",
        json={"raw_text": "我自己的私密底牌笔记：601138 跌到 21 块再加 2 成仓位", "visibility": "private"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert r2.status_code == 200

    # 3. Bob MUST see the anonymous circle note (with author masked!) and MUST NOT see Alice's private note
    rb = c_bob.get("/api/intel/feed?scope=active")
    assert rb.status_code == 200
    bob_items = rb.json()["items"]
    assert len(bob_items) == 1
    assert bob_items[0]["display_author"] == "匿名圈友"
    assert "私密底牌" not in bob_items[0]["raw_text"]

    # Bob endorses the circle note and cannot delete Alice's note
    re_end = c_bob.post(f"/api/intel/{it1['id']}/endorse", headers={"X-Requested-With": "XMLHttpRequest"})
    assert re_end.status_code == 200
    assert re_end.json()["item"]["endorse_count"] == 1
    r_del_forbid = c_bob.delete(f"/api/intel/{it1['id']}", headers={"X-Requested-With": "XMLHttpRequest"})
    assert r_del_forbid.status_code == 403


def test_private_position_state_and_prompt_injection_and_watchman(monkeypatch, tmp_path):
    from server import database, preferences, private_agent
    from server.main import app

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test_pa.db")
    database.reset_connection()
    database.init_db()
    preferences.init_preferences_db()
    private_agent.init_private_agent_db()
    monkeypatch.setattr(
        private_agent,
        "fetch_fast_quotes",
        lambda codes: {
            "sh000300": {"code": "sh000300", "name": "沪深300", "price": 3980.0, "change_pct": -1.2},
            "600519": {"code": "sh600519", "name": "贵州茅台", "price": 1465.0, "change_pct": -2.1},
        },
    )

    c_alice = TestClient(app)
    c_bob = TestClient(app)
    _login(c_alice, "alice")
    _login(c_bob, "bob")

    # Alice saves 85% heavy position
    rp = c_alice.put(
        "/api/private-agent/state",
        json={
            "position_pct": 85,
            "max_single_loss_pct": 6.0,
            "cash_note": "已接近满仓，优先防回撤",
            "holdings": [{"code": "600519", "name": "贵州茅台", "cost_price": 1520.0, "weight_note": "重仓"}],
        },
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert rp.status_code == 200
    assert rp.json()["position_state"]["position_pct"] == 85

    # Bob's private hub MUST remain unconfigured (zero leakage)
    rb = c_bob.get("/api/private-agent/hub")
    assert rb.status_code == 200
    assert rb.json()["position_state"]["configured"] is False

    # Snapshot Alice's run profile -> verify 3-layer memory + Devil's Advocate prompt injection
    with database.get_db() as db:
        preferences.snapshot_run(db, "alice", "run_test_1", page_context={"ticker": "600519"})
        frozen = preferences.run_profile(db, "alice", "run_test_1")
    prompt = preferences.profile_prompt(frozen)
    assert "85%" in prompt
    assert "贵州茅台(600519)" in prompt
    assert "反回音室与红蓝军风控铁律" in prompt

    # Natural language Watchman rule creation + 0-token trigger sweep
    rw = c_alice.post(
        "/api/private-agent/watchman",
        json={"raw_text": "600519 跌破 1480 提醒我"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert rw.status_code == 200
    assert rw.json()["rule"]["rule_kind"] == "price_below"
    assert rw.json()["rule"]["threshold"] == 1480.0

    rc = c_alice.post("/api/private-agent/watchman/check-now", headers={"X-Requested-With": "XMLHttpRequest"})
    assert rc.status_code == 200
    assert rc.json()["summary"]["triggered"] == 1
    assert rc.json()["rules"][0]["triggered_at"] is not None
    assert "1465" in rc.json()["rules"][0]["trigger_summary"]


def test_admin_batch_user_quota_and_preferred_model(monkeypatch, tmp_path):
    from server import auth, billing_ops, database
    from server.main import app

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test_batch_q.db")
    database.reset_connection()
    database.init_db()
    billing_ops.init_billing_ops_db()
    tok = "test_ops_gateway_secret_token_0123456789abcdef"
    monkeypatch.setattr(auth, "_read_private_gateway_token", lambda: tok)

    _login(TestClient(app), "admin")
    _login(TestClient(app), "u1")
    _login(TestClient(app), "u2")

    c_ops = TestClient(app, client=("127.0.0.1", 51234))
    ops_headers = {
        "X-Requested-With": "XMLHttpRequest",
        "X-Prism-Ops-Token": tok,
        "X-Prism-Ops-Principal": "admin",
    }

    r = c_ops.put(
        "/api/admin/users-batch/quota",
        json={
            "usernames": ["u1", "u2"],
            "unlimited_quota": False,
            "daily_calls_limit": 50,
            "monthly_budget_usd": 12.5,
            "preferred_model_mode": "set",
            "preferred_model": "",
        },
        headers=ops_headers,
    )
    assert r.status_code == 200
    assert r.json()["updated_count"] == 2

    ru = c_ops.get("/api/admin/users", headers=ops_headers)
    assert ru.status_code == 200
    by_name = {u["username"]: u for u in ru.json()["users"]}
    assert by_name["u1"]["daily_calls_limit"] == 50
    assert by_name["u2"]["monthly_budget_usd"] == 12.5


def test_agent_cross_user_privacy_and_anti_exfiltration_guards(monkeypatch, tmp_path):
    """Verify Bob's Agent can use platform tools but CANNOT read Alice's private data or exfiltrate via circle_intel_submit."""
    import asyncio

    from server import capabilities, database, private_agent, reports, watchlist
    from server.agent.tools import NativeTools
    from server.main import app
    from server.research import create_research_run
    from server.research_context import EvidenceTracker, build_research_context
    from server.work_lifecycle import Manager

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test_agent_priv.db")
    database.reset_connection()
    database.init_db()
    private_agent.init_private_agent_db()
    monkeypatch.setattr(
        private_agent,
        "fetch_fast_quotes",
        lambda codes: {
            "600519": {"code": "sh600519", "name": "贵州茅台", "price": 1500.0, "change_pct": 1.0}
        },
    )

    c_alice = TestClient(app)
    c_bob = TestClient(app)
    _login(c_alice, "alice")
    _login(c_bob, "bob")

    # Seed Alice's private assets across multiple modules:
    # 1) Watchlist, 2) Saved Report, 3) Private Intel + Anonymous Circle Intel, 4) Position State + Watchman
    watchlist.set_watchlist("alice", [{"ticker": "600519", "name": "贵州茅台"}])
    alice_report = reports.save_report_snapshot(
        "alice",
        {"source": "manual", "ticker": "600519", "title": "Alice机密研报", "report": {"summary": "底牌秘密"}},
    )
    alice_priv_intel = private_agent.submit_circle_intel(
        "alice", raw_text="Alice绝密笔记：重仓688981成本80元", visibility="private", fetch_live=False
    )
    alice_anon_intel = private_agent.submit_circle_intel(
        "alice",
        raw_text="圈内匿名分享：光模块订单排产超预期",
        visibility="circle",
        anonymous=True,
        fetch_live=False,
    )
    private_agent.create_watchman_rule("alice", "600519 跌破 1480 提醒我")

    # Check-Now called by Bob MUST NOT evaluate or leak counts of Alice's watchman rules
    bob_check = c_bob.post(
        "/api/private-agent/watchman/check-now", headers={"X-Requested-With": "XMLHttpRequest"}
    )
    assert bob_check.status_code == 200
    assert bob_check.json()["summary"] == {"checked": 0, "triggered": 0}

    # Anonymous circle intel MUST NOT leak author_credibility stats
    bob_feed = private_agent.list_visible_intel("bob", scope="active")
    assert len(bob_feed["items"]) == 1
    assert bob_feed["items"][0]["id"] == alice_anon_intel["id"]
    assert bob_feed["items"][0]["display_author"] == "匿名圈友"
    assert bob_feed["items"][0]["author_credibility"] is None

    # Create a real owned conversation + research run for Bob and execute Agent tools as Bob
    with database.get_db() as db:
        db.execute("INSERT INTO conversations(id, username) VALUES(?, ?)", ["conv_bob_1", "bob"])
    research = build_research_context(run_id="run_bob_1")
    create_research_run("bob", "conv_bob_1", "测试跨用户隔离", research)

    async def _run_bob_agent_checks():
        mgr = Manager(max_workers=2)
        agent_lease = mgr.admit("agent", persist_required=False)
        try:
            with capabilities.native_run_context(
                "bob", "conv_bob_1", "run_bob_1", parent_lease=agent_lease
            ) as ctx:
                tools = NativeTools(ctx, agent_lease, EvidenceTracker("run_bob_1"))

                # 1. Bob's watchlist_get sees ONLY Bob's empty watchlist, never Alice's 600519
                w_res = await tools.execute("watchlist_get", {}, "call_w1")
                assert w_res["ok"] is True
                assert w_res["data"]["items"] == []

                # 2. Bob's report_get with Alice's exact report ID returns not_found
                r_res = await tools.execute("report_get", {"id": alice_report["id"]}, "call_r1")
                assert r_res["ok"] is False
                assert r_res["error"]["code"] == "not_found"

                # 3. Bob's knowledge_get with Alice's report ID returns not_found
                k_res = await tools.execute(
                    "knowledge_get", {"id": f"report:{alice_report['id']}"}, "call_k1"
                )
                assert k_res["ok"] is False
                assert k_res["error"]["code"] == "not_found"

                # 4. Bob's circle_intel_search sees public anonymous intel (without Alice's identity) and NEVER Alice's private intel
                i_res = await tools.execute("circle_intel_search", {}, "call_i1")
                assert i_res["ok"] is True
                ids = [it["id"] for it in i_res["data"]["items"]]
                assert alice_anon_intel["id"] in ids
                assert alice_priv_intel["id"] not in ids
                assert "688981" not in str(i_res)
                assert "alice" not in str(i_res).lower()

                # 5. Even if prompt injection tricks Bob's Agent into calling circle_intel_submit(visibility="circle"),
                #    hard anti-exfiltration boundary clamps it to Bob's private vault so Alice NEVER sees it!
                sub_res = await tools.execute(
                    "circle_intel_submit",
                    {"raw_text": "Bob对话随手记：准备在3950附近接回半仓", "visibility": "circle"},
                    "call_sub1",
                )
                assert sub_res["ok"] is True
                assert sub_res["data"]["visibility"] == "private"

                alice_feed_after = private_agent.list_visible_intel("alice", scope="active")
                alice_visible_texts = " ".join(it["raw_text"] for it in alice_feed_after["items"])
                assert "Bob对话随手记" not in alice_visible_texts
        finally:
            agent_lease.release()
            mgr._pool.shutdown(wait=True, cancel_futures=False)

    asyncio.run(_run_bob_agent_checks())
