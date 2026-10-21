"""Industry chain panel — API + seed tests."""
import json
import time

import pytest


@pytest.mark.asyncio
async def test_categories_seeded(client):
    r = await client.get("/api/industry/categories")
    assert r.status_code == 200
    cats = r.json()["categories"]
    names = [c["category"] for c in cats]
    assert "锂电池" in names and "半导体" in names
    assert len(cats) >= 8
    # node counts are positive
    assert all(c["node_count"] > 0 for c in cats)
    assert all("edge_count" in c for c in cats)
    assert all("top_nodes" in c for c in cats)
    assert all(c["upstream_count"] + c["midstream_count"] + c["downstream_count"] == c["node_count"] for c in cats)


@pytest.mark.asyncio
async def test_overview_returns_research_workbench_metrics(client):
    r = await client.get("/api/industry/overview")
    assert r.status_code == 200
    data = r.json()
    assert data["totals"]["industry_count"] >= 8
    assert data["totals"]["node_count"] > 0
    assert data["totals"]["edge_count"] > 0
    assert data["categories"]
    assert data["countries"]
    assert data["key_nodes"]
    first = data["categories"][0]
    assert {"category", "node_count", "edge_count", "country_count", "ticker_count", "top_nodes"} <= set(first)
    assert data["key_nodes"][0]["degree"] >= 0


@pytest.mark.asyncio
async def test_live_context_fuses_private_cross_module_signals(client, auth_cookies):
    from server.database import get_db

    now = int(time.time())
    vibe_plan = {
        "status": "complete",
        "updated_at": now - 5,
        "title": "锂电池产业研究",
        "candidates": [
            {"ticker": "300750", "name": "宁德时代", "role": "电池龙头", "why": "订单与产能验证"},
            {"ticker": "300999", "name": "待归链公司", "role": "研究候选", "why": "仍需产业证据"},
        ],
    }
    with get_db() as db:
        db.execute(
            """
            INSERT INTO analysis_cache
                (id,username,ticker,trade_date,mode,name,signal,report_json,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            ["a1", "testuser", "300750.SZ", "2026-07-13", "deep", "宁德时代", "BUY", "{}", now],
        )
        db.execute(
            """
            INSERT INTO saved_reports
                (id,username,source,ticker,name,title,signal,summary,report_json,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            ["r1", "testuser", "analysis", "300750", "宁德时代", "宁德时代分析", "BUY", "需求仍在验证", "{}", now - 10],
        )
        db.execute(
            """
            INSERT INTO vibe_plans
                (id,username,prompt,market,horizon,risk_level,plan_json,created_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            ["v1", "testuser", "锂电池", "A股", "swing", "balanced", json.dumps(vibe_plan, ensure_ascii=False), now - 20],
        )
        db.execute(
            """
            INSERT INTO activity_events
                (username,feature,action,target_type,target_id,target_label,created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            ["testuser", "market", "watchlist_add", "ticker", "300750", "宁德时代", now - 30],
        )
        db.execute(
            """
            INSERT INTO activity_events
                (username,feature,action,target_type,target_id,target_label,created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            ["testuser", "market", "watchlist_add", "ticker", "600000", "浦发银行", now - 40],
        )
        db.execute(
            """
            INSERT INTO activity_events
                (username,feature,action,target_type,target_id,target_label,created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            ["testuser", "market", "watchlist_remove", "ticker", "600000", "浦发银行", now - 35],
        )
        db.execute(
            """
            INSERT INTO analysis_cache
                (id,username,ticker,trade_date,mode,name,signal,report_json,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            ["other-a", "other-user", "600519", "2026-07-13", "deep", "贵州茅台", "SELL", "{}", now],
        )

    response = await client.get("/api/industry/context", cookies=auth_cookies)
    assert response.status_code == 200
    data = response.json()
    assert data["summary"]["active_tickers"] == 2
    assert data["summary"]["linked_tickers"] == 1
    assert data["summary"]["unlinked_tickers"] == 1
    assert data["summary"]["source_totals"] == {
        "analysis": 1, "vibe": 2, "report": 1, "watchlist": 1,
    }
    linked = data["tickers"]["300750"]
    assert linked["linked"] is True
    assert linked["latest_signal"] == "BUY"
    assert set(linked["sources"]) == {"analysis", "vibe", "report", "watchlist"}
    assert "锂电池" in linked["categories"]
    assert data["unlinked"][0]["ticker"] == "300999"
    assert "600000" not in data["tickers"]  # latest action removed it
    assert "600519" not in data["tickers"]  # another user's analysis stays private


@pytest.mark.asyncio
async def test_live_context_requires_login(client):
    response = await client.get("/api/industry/context")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_graph_returns_tiered_nodes_and_edges(client):
    r = await client.get("/api/industry/graph?category=" + "锂电池" + "&min_confidence=0")
    assert r.status_code == 200
    data = r.json()
    assert len(data["nodes"]) > 0
    assert len(data["edges"]) > 0
    assert data["summary"]["category"] == "锂电池"
    assert data["summary"]["node_count"] == len(data["nodes"])
    tiers = {n["tier"] for n in data["nodes"]}
    assert tiers <= {0, 1, 2}  # 上游/中游/下游


@pytest.mark.asyncio
async def test_search_matches_ticker_and_category(client):
    r = await client.get("/api/industry/search?q=300750&limit=5")
    assert r.status_code == 200
    rows = r.json()["results"]
    assert rows
    assert rows[0]["source_ticker"] == "300750"

    r = await client.get("/api/industry/search?q=半导体&limit=5")
    assert r.status_code == 200
    rows = r.json()["results"]
    assert rows
    assert any(row["category"] == "半导体" for row in rows)


@pytest.mark.asyncio
async def test_ticker_chain_expands_connected_nodes(client):
    r = await client.get("/api/industry/ticker/300750/chain")
    assert r.status_code == 200
    data = r.json()
    node_ids = {n["id"] for n in data["nodes"]}
    assert len(node_ids) > 1
    assert data["edges"]
    assert all(e["source_node_id"] in node_ids and e["target_node_id"] in node_ids for e in data["edges"])


@pytest.mark.asyncio
async def test_node_detail_has_upstream_downstream(client):
    # Grab a node id from 锂电池 graph
    g = (await client.get("/api/industry/graph?category=锂电池&min_confidence=0")).json()
    nid = g["nodes"][0]["id"]
    r = await client.get("/api/industry/node/" + nid)
    assert r.status_code == 200
    d = r.json()
    assert d["node"]["id"] == nid
    assert "upstream" in d and "downstream" in d


@pytest.mark.asyncio
async def test_geo_has_coords(client):
    r = await client.get("/api/industry/geo")
    assert r.status_code == 200
    regions = r.json()["regions"]
    with_coords = [rg for rg in regions if rg.get("lat") is not None]
    assert len(with_coords) >= 8  # geo_coords seeded


@pytest.mark.asyncio
async def test_node_detail_404(client):
    r = await client.get("/api/industry/node/does-not-exist")
    assert r.status_code == 404
