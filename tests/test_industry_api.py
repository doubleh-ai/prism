"""Industry chain panel — API + seed tests."""
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


@pytest.mark.asyncio
async def test_graph_returns_tiered_nodes_and_edges(client):
    r = await client.get("/api/industry/graph?category=" + "锂电池" + "&min_confidence=0")
    assert r.status_code == 200
    data = r.json()
    assert len(data["nodes"]) > 0
    assert len(data["edges"]) > 0
    tiers = {n["tier"] for n in data["nodes"]}
    assert tiers <= {0, 1, 2}  # 上游/中游/下游


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
