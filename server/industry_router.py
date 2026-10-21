"""Industry chain graph API — query, search, geo-distribution.

Phase 6: Each astock analysis can feed new nodes/edges into the graph,
building a global industry knowledge map over time.
"""

import json
import re

from fastapi import APIRouter, HTTPException, Request

from .auth import require_auth
from .database import get_db
from .industry_context import build_industry_context

router = APIRouter(prefix="/api/industry")


def _slug(name: str) -> str:
    """Normalise a name into a stable node ID."""
    return re.sub(r"[^a-z0-9-]", "-", name.strip().lower())[:80]


def _category_stats(db) -> list[dict]:
    """Return enriched per-chain stats for navigation and overview cards."""
    rows = [
        dict(r)
        for r in db.execute(
            """
        SELECT
            category,
            COUNT(*) AS node_count,
            COUNT(DISTINCT country) AS country_count,
            COUNT(DISTINCT NULLIF(source_ticker, '')) AS ticker_count,
            ROUND(AVG(confidence), 3) AS avg_confidence,
            SUM(CASE WHEN tier = 0 THEN 1 ELSE 0 END) AS upstream_count,
            SUM(CASE WHEN tier = 1 THEN 1 ELSE 0 END) AS midstream_count,
            SUM(CASE WHEN tier = 2 THEN 1 ELSE 0 END) AS downstream_count,
            MAX(COALESCE(market_share, 0)) AS max_market_share
        FROM industry_nodes
        GROUP BY category
        ORDER BY node_count DESC, category ASC
        """
        ).fetchall()
    ]
    edge_counts = {
        r["category"]: r["edge_count"]
        for r in db.execute(
            """
            SELECT s.category AS category, COUNT(*) AS edge_count
            FROM industry_edges e
            JOIN industry_nodes s ON e.source_node_id = s.id
            GROUP BY s.category
            """
        ).fetchall()
    }
    for row in rows:
        cat = row["category"]
        row["edge_count"] = edge_counts.get(cat, 0)
        row["top_nodes"] = [
            dict(r)
            for r in db.execute(
                """
            SELECT id, name, source_ticker, tier, country, description,
                   market_share, confidence
            FROM industry_nodes
            WHERE category = ?
            ORDER BY COALESCE(market_share, 0) DESC, confidence DESC, tier ASC, name ASC
            LIMIT 4
            """,
                [cat],
            ).fetchall()
        ]
    return rows


@router.get("/graph")
async def graph(
    ticker: str = "",
    category: str = "",
    country: str = "",
    q: str = "",
    depth: int = 2,
    min_confidence: float = 0.2,
):
    """Return nodes + edges filtered by optional criteria."""
    with get_db() as db:
        wheres = []
        params = []
        if ticker:
            wheres.append(
                "(source_ticker = ? OR target_node_id IN (SELECT id FROM industry_nodes WHERE source_ticker = ?))"
            )
            params.extend([ticker, ticker])
        if category:
            wheres.append("category = ?")
            params.append(category)
        if country:
            wheres.append("country = ?")
            params.append(country.upper())

        where = " AND ".join(wheres) if wheres else "1=1"
        nodes = [
            dict(r)
            for r in db.execute(
                f"SELECT * FROM industry_nodes WHERE confidence >= ? AND ({where}) ORDER BY tier, confidence DESC",
                [min_confidence] + params,
            ).fetchall()
        ]

        node_ids = {n["id"] for n in nodes}
        edges = []
        if node_ids:
            placeholders = ",".join("?" for _ in node_ids)
            edges = [
                dict(r)
                for r in db.execute(
                    f"SELECT e.*, s.name AS source_name, t.name AS target_name "
                    f"FROM industry_edges e "
                    f"JOIN industry_nodes s ON e.source_node_id = s.id "
                    f"JOIN industry_nodes t ON e.target_node_id = t.id "
                    f"WHERE e.source_node_id IN ({placeholders}) OR e.target_node_id IN ({placeholders})",
                    list(node_ids) + list(node_ids),
                ).fetchall()
            ]

        # Expand by depth: include nodes reachable from current set
        if depth > 1 and edges:
            expanded_ids = set(node_ids)
            for e in edges:
                expanded_ids.add(e["source_node_id"])
                expanded_ids.add(e["target_node_id"])
            new_ids = expanded_ids - node_ids
            if new_ids:
                placeholders2 = ",".join("?" for _ in new_ids)
                extra = [
                    dict(r)
                    for r in db.execute(
                        f"SELECT * FROM industry_nodes WHERE id IN ({placeholders2})",
                        list(new_ids),
                    ).fetchall()
                ]
                nodes.extend(extra)

    # Search
    results = nodes
    if q:
        ql = q.lower()
        results = [
            n
            for n in nodes
            if ql in n["name"].lower()
            or ql in n.get("description", "").lower()
            or ql in n.get("category", "").lower()
            or ql in n.get("source_ticker", "").lower()
        ]

    return {
        "nodes": results,
        "edges": edges,
        "total_nodes": len(results),
        "total_edges": len(edges),
        "summary": {
            "category": category,
            "country": country,
            "ticker": ticker,
            "node_count": len(results),
            "edge_count": len(edges),
            "country_count": len({n.get("country") for n in results if n.get("country")}),
            "ticker_count": len({n.get("source_ticker") for n in results if n.get("source_ticker")}),
        },
    }


@router.get("/overview")
async def overview():
    """Aggregated industry-map overview for the research workbench."""
    with get_db() as db:
        totals = dict(
            db.execute(
                """
            SELECT
                COUNT(DISTINCT category) AS industry_count,
                COUNT(*) AS node_count,
                COUNT(DISTINCT country) AS country_count,
                COUNT(DISTINCT NULLIF(source_ticker, '')) AS ticker_count,
                ROUND(AVG(confidence), 3) AS avg_confidence
            FROM industry_nodes
            """
            ).fetchone()
        )
        edge_total = db.execute("SELECT COUNT(*) AS cnt FROM industry_edges").fetchone()["cnt"]
        totals["edge_count"] = edge_total
        categories = _category_stats(db)
        countries = [
            dict(r)
            for r in db.execute(
                """
            SELECT
                country,
                COUNT(*) AS node_count,
                COUNT(DISTINCT category) AS industry_count,
                COUNT(DISTINCT NULLIF(source_ticker, '')) AS ticker_count
            FROM industry_nodes
            WHERE country IS NOT NULL AND country != ''
            GROUP BY country
            ORDER BY node_count DESC, country ASC
            """
            ).fetchall()
        ]
        nodes = [
            dict(r)
            for r in db.execute(
                """
            WITH deg AS (
                SELECT id, SUM(cnt) AS degree FROM (
                    SELECT source_node_id AS id, COUNT(*) AS cnt
                    FROM industry_edges GROUP BY source_node_id
                    UNION ALL
                    SELECT target_node_id AS id, COUNT(*) AS cnt
                    FROM industry_edges GROUP BY target_node_id
                )
                GROUP BY id
            )
            SELECT n.*, COALESCE(deg.degree, 0) AS degree
            FROM industry_nodes n
            LEFT JOIN deg ON deg.id = n.id
            ORDER BY COALESCE(deg.degree, 0) DESC,
                     COALESCE(n.market_share, 0) DESC,
                     n.confidence DESC,
                     n.name ASC
            LIMIT 18
            """
            ).fetchall()
        ]
    return {
        "totals": totals,
        "categories": categories,
        "countries": countries,
        "key_nodes": nodes,
    }


@router.get("/context")
async def live_context(request: Request):
    """Return the current user's live signals from other Prism features."""
    username = require_auth(request)
    return build_industry_context(username)


@router.get("/geo")
async def geo():
    """Return geo-region data for world-map rendering."""
    with get_db() as db:
        regions = [
            dict(r)
            for r in db.execute("SELECT * FROM geo_regions ORDER BY continent, country_name").fetchall()
        ]
    # Attach node counts per country
    with get_db() as db:
        counts = {}
        for r in db.execute(
            "SELECT country, COUNT(*) AS cnt FROM industry_nodes WHERE country IS NOT NULL GROUP BY country"
        ).fetchall():
            counts[r["country"]] = r["cnt"]
    for reg in regions:
        reg["node_count"] = counts.get(reg["country_code"], 0)
        try:
            reg["key_industries"] = json.loads(reg["key_industries"])
        except Exception:
            reg["key_industries"] = []
    return {"regions": regions}


@router.get("/search")
async def search(q: str = "", limit: int = 20):
    """Full-text search on industry node names, tickers, categories and roles."""
    if not q:
        return {"results": []}
    q = q.strip()
    if len(q) > 40:
        q = q[:40]
    with get_db() as db:
        rows = [
            dict(r)
            for r in db.execute(
                """
            SELECT * FROM industry_nodes
            WHERE name LIKE ?
               OR description LIKE ?
               OR category LIKE ?
               OR source_ticker LIKE ?
            ORDER BY
                CASE
                    WHEN source_ticker = ? THEN 0
                    WHEN source_ticker LIKE ? THEN 1
                    WHEN name LIKE ? THEN 2
                    ELSE 3
                END,
                confidence DESC,
                name ASC
            LIMIT ?
            """,
                [f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%", q, f"{q}%", f"{q}%", limit],
            ).fetchall()
        ]
    return {"results": rows}


@router.get("/ticker/{ticker}/chain")
async def ticker_chain(ticker: str):
    """Return the complete upstream/downstream chain for a ticker."""
    with get_db() as db:
        nodes = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM industry_nodes WHERE source_ticker = ? ORDER BY tier, confidence DESC",
                [ticker],
            ).fetchall()
        ]
        node_ids = {n["id"] for n in nodes}
        edges = []
        if node_ids:
            placeholders = ",".join("?" for _ in node_ids)
            edges = [
                dict(r)
                for r in db.execute(
                    f"SELECT e.*, s.name AS source_name, t.name AS target_name FROM industry_edges e "
                    f"JOIN industry_nodes s ON e.source_node_id = s.id "
                    f"JOIN industry_nodes t ON e.target_node_id = t.id "
                    f"WHERE e.source_node_id IN ({placeholders}) OR e.target_node_id IN ({placeholders})",
                    list(node_ids) + list(node_ids),
                ).fetchall()
            ]
            expanded_ids = set(node_ids)
            for edge in edges:
                expanded_ids.add(edge["source_node_id"])
                expanded_ids.add(edge["target_node_id"])
            missing_ids = expanded_ids - node_ids
            if missing_ids:
                placeholders2 = ",".join("?" for _ in missing_ids)
                nodes.extend(
                    [
                        dict(r)
                        for r in db.execute(
                            f"SELECT * FROM industry_nodes WHERE id IN ({placeholders2}) ORDER BY tier, confidence DESC",
                            list(missing_ids),
                        ).fetchall()
                    ]
                )
    return {"ticker": ticker, "nodes": nodes, "edges": edges}


@router.get("/categories")
async def categories():
    """Distinct industry chains with node counts — populates the sidebar list."""
    with get_db() as db:
        rows = _category_stats(db)
    return {"categories": rows}


@router.get("/node/{node_id}")
async def node_detail(node_id: str):
    """A node plus its upstream suppliers and downstream customers (detail drawer)."""
    with get_db() as db:
        row = db.execute("SELECT * FROM industry_nodes WHERE id=?", [node_id]).fetchone()
        if not row:
            raise HTTPException(404, "节点不存在")
        node = dict(row)
        upstream = [
            dict(r)
            for r in db.execute(
                "SELECT n.*, e.relation FROM industry_edges e "
                "JOIN industry_nodes n ON e.source_node_id = n.id "
                "WHERE e.target_node_id = ?",
                [node_id],
            ).fetchall()
        ]
        downstream = [
            dict(r)
            for r in db.execute(
                "SELECT n.*, e.relation FROM industry_edges e "
                "JOIN industry_nodes n ON e.target_node_id = n.id "
                "WHERE e.source_node_id = ?",
                [node_id],
            ).fetchall()
        ]
    return {"node": node, "upstream": upstream, "downstream": downstream}


@router.post("/seed")
async def reseed(request: Request):
    """Reload curated seed chains (admin). Clears existing seed rows first."""
    u = require_auth(request)
    from .config import ADMIN_USERS

    if ADMIN_USERS and u not in ADMIN_USERS:
        raise HTTPException(403, "仅管理员可执行此操作")
    from .industry_seed import seed_all

    with get_db() as db:
        db.execute("DELETE FROM industry_edges WHERE evidence = 'seed'")
        db.execute("DELETE FROM industry_nodes WHERE source_date = 'seed'")
        seed_all(db)
    return {"ok": True}
