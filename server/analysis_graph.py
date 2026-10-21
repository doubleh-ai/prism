"""Analysis graph — auto-extract nodes & edges from chat chunks + API.

Each conversation's tool-call / text / think / ref chunks become graph
nodes connected by ``flows_to`` edges. The user can view the relationship
graph to trace the analysis's reasoning chain.
"""

import json

from fastapi import APIRouter, HTTPException, Request

from .auth import require_auth
from .database import get_db

router = APIRouter(prefix="/api")


def extract_graph(conv_id: str, messages_json: list, skill: str = ""):
    """Parse structured message chunks into graph nodes + edges. Returns
    (nodes: list[dict], edges: list[dict]). Caller is responsible for
    persisting. Already-inserted nodes for this conv are skipped
    (upsert-by-id)."""
    nodes, edges = [], []
    prev_id = None

    for mi, msg in enumerate(messages_json):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        chunks = msg.get("_chunks") or msg.get("chunks") or []
        if not isinstance(chunks, list):
            continue

        # ── user question → node ──
        if role == "user":
            qid = f"{conv_id}-q{mi}"
            label = msg.get("content", "")[:120]
            nodes.append(
                dict(
                    id=qid,
                    conv_id=conv_id,
                    node_type="question",
                    label=f"Q: {label}",
                    content=msg.get("content", "")[:2000],
                    skill=skill,
                )
            )
            if prev_id:
                edges.append(dict(source_node_id=prev_id, target_node_id=qid, edge_type="asks"))
            prev_id = qid
            continue

        if role != "assistant":
            continue

        # ── assistant chunks → nodes ──
        for ci, c in enumerate(chunks):
            if not isinstance(c, dict):
                continue
            t = c.get("t", "")
            nid = f"{conv_id}-m{mi}c{ci}"

            if t == "tool":
                tool_name = c.get("c", "tool")
                detail = c.get("d", "")[:120]
                nodes.append(
                    dict(
                        id=nid,
                        conv_id=conv_id,
                        node_type="tool_call",
                        label=f"🔧 {tool_name}",
                        content=detail,
                        skill=skill,
                    )
                )
                if prev_id:
                    edges.append(dict(source_node_id=prev_id, target_node_id=nid, edge_type="calls"))
                prev_id = nid

            elif t == "think":
                label = (c.get("c", "") or "")[:100]
                nodes.append(
                    dict(
                        id=nid,
                        conv_id=conv_id,
                        node_type="think",
                        label=f"💭 思考: {label}",
                        content=c.get("c", "")[:4000],
                        skill=skill,
                    )
                )
                if prev_id:
                    edges.append(dict(source_node_id=prev_id, target_node_id=nid, edge_type="thinks"))
                prev_id = nid

            elif t == "text":
                label = (c.get("c", "") or "")[:100]
                nodes.append(
                    dict(
                        id=nid,
                        conv_id=conv_id,
                        node_type="analysis",
                        label=f"📝 {label}",
                        content=c.get("c", "")[:4000],
                        skill=skill,
                    )
                )
                if prev_id:
                    edges.append(dict(source_node_id=prev_id, target_node_id=nid, edge_type="generates"))
                prev_id = nid

            elif t == "ref":
                title = c.get("title", "")[:80]
                url = c.get("url", "")
                nodes.append(
                    dict(
                        id=nid,
                        conv_id=conv_id,
                        node_type="reference",
                        label=f"📎 {title}",
                        content=url,
                        skill=skill,
                    )
                )
                if prev_id:
                    edges.append(dict(source_node_id=prev_id, target_node_id=nid, edge_type="references"))
                prev_id = nid

            elif t == "ask":
                q = c.get("q", "")[:120]
                nodes.append(
                    dict(
                        id=nid,
                        conv_id=conv_id,
                        node_type="question",
                        label=f"❓ {q}",
                        content=json.dumps(c.get("opts", [])),
                        skill=skill,
                    )
                )
                if prev_id:
                    edges.append(dict(source_node_id=prev_id, target_node_id=nid, edge_type="asks"))
                prev_id = nid

    return nodes, edges


def persist_graph(conv_id: str, messages_json: list, skill: str = ""):
    """Extract and persist graph nodes/edges. Idempotent — nodes for
    this conv are deleted and re-inserted so the graph stays in sync."""
    nodes, edges = extract_graph(conv_id, messages_json, skill)
    if not nodes:
        return
    with get_db() as db:
        # Explicit transaction — under autocommit each execute() would
        # otherwise commit independently, risking partial graph state.
        db.execute("BEGIN")
        try:
            db.execute("DELETE FROM analysis_edges WHERE source_node_id LIKE ?", [f"{conv_id}-%"])
            db.execute("DELETE FROM analysis_nodes WHERE conv_id = ?", [conv_id])
            for n in nodes:
                db.execute(
                    "INSERT INTO analysis_nodes (id, conv_id, node_type, label, content, skill) VALUES (?,?,?,?,?,?)",
                    [n["id"], n["conv_id"], n["node_type"], n["label"], n["content"], n["skill"]],
                )
            for e in edges:
                db.execute(
                    "INSERT OR IGNORE INTO analysis_edges (source_node_id, target_node_id, edge_type, metadata) VALUES (?,?,?,?)",
                    [e["source_node_id"], e["target_node_id"], e["edge_type"], "{}"],
                )
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise


@router.get("/convs/{conv_id}/graph")
async def api_graph(conv_id: str, request: Request):
    """Return nodes+edges for a conversation's analysis graph."""
    u = require_auth(request)
    with get_db() as db:
        # Ownership check — only the conversation owner may view its graph
        row = db.execute(
            "SELECT username FROM conversations WHERE id=? AND deleted_at IS NULL",
            [conv_id],
        ).fetchone()
        if not row:
            raise HTTPException(404, "对话不存在")
        if row["username"] != u:
            raise HTTPException(403, "无权访问该对话")
        nodes = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM analysis_nodes WHERE conv_id=? ORDER BY created_at", [conv_id]
            ).fetchall()
        ]
        edges = [
            dict(r)
            for r in db.execute(
                "SELECT e.* FROM analysis_edges e JOIN analysis_nodes n ON e.source_node_id=n.id WHERE n.conv_id=?",
                [conv_id],
            ).fetchall()
        ]
    return {"conv_id": conv_id, "nodes": nodes, "edges": edges}
