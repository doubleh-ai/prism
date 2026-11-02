"""Owner-scoped, evidence-linked industrial transmission paths.

Background seed links never enter this graph. A reference match is structural,
not an independent validation of a supplier relationship or bottleneck thesis.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import HTTPException

from .database import get_db
from .productivity.store import _active
from .research import knowledge, ledger
from .source_urls import source_display_payload

DIMENSIONS = {
    "supply": "产能、供货阶段与可兑现数量",
    "certification": "认证周期、客户验证与切换成本",
    "substitution": "替代产品、良率与切换条件",
    "cost": "采购成本、定价条款与毛利传导",
    "demand": "终端需求、订单能见度与库存消化",
}


def _paths(relations):
    """Bounded two/three-link hypotheses; do not turn centrality into monopoly."""
    candidates = [row for row in relations if not row["blocking_gaps"]]
    by_source = {}
    for row in candidates:
        by_source.setdefault(row["from_entity"].casefold(), []).append(row)
    results, seen = [], set()

    def walk(path, nodes):
        if len(results) >= 30 or len(path) >= 3:
            return
        for following in by_source.get(path[-1]["to_entity"].casefold(), []):
            dest = following["to_entity"].casefold()
            if dest in nodes:
                continue
            combined = path + [following]
            key = tuple(row["relation_id"] for row in combined)
            if key not in seen:
                seen.add(key)
                results.append(
                    {
                        "relation_ids": list(key),
                        "entities": [combined[0]["from_entity"], *[row["to_entity"] for row in combined]],
                        "products": [row["product"] for row in combined],
                        "mechanisms": [row["mechanism"] for row in combined],
                        "valid_periods": [row["valid_period"] for row in combined],
                        "status": "hypothesis_requires_cross_link_review",
                        "checks_required": [
                            "产品能否跨环节连接",
                            "有效期间是否重叠",
                            "数量和瓶颈是否有披露",
                            "成本能否转嫁及终端需求能否兑现",
                        ],
                    }
                )
            if len(results) >= 30:
                return
            walk(combined, nodes | {dest})

    for row in candidates:
        walk([row], {row["from_entity"].casefold(), row["to_entity"].casefold()})
        if len(results) >= 30:
            break
    return results


def evidence_graph(owner, *, query="", before=None, limit=40, compact=False):
    if type(query) is not str or len(query) > 160 or type(limit) is not int or not 1 <= limit <= 60:
        raise HTTPException(422, "产业研究检索参数无效")
    if before is not None and (type(before) is not int or before < 1):
        raise HTTPException(422, "产业研究分页参数无效")
    ledger.init_research_db()
    knowledge.init_knowledge_db()
    clause, params = "", [owner]
    if query.strip():
        escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clause += " AND rel.payload LIKE ? ESCAPE '\\'"
        params.append("%" + escaped + "%")
    if before is not None:
        clause += " AND rel.rowid<?"
        params.append(before)
    with get_db() as db:
        _active(db, owner)
        rows = db.execute(
            "SELECT rel.rowid AS sequence,rel.run_id,rel.payload FROM research_relations rel "
            "JOIN research_runs r ON r.run_id=rel.run_id WHERE r.username=? "
            "AND (r.conv_id IS NULL OR EXISTS(SELECT 1 FROM conversations c WHERE c.id=r.conv_id "
            "AND c.username=r.username AND c.deleted_at IS NULL))"
            + clause
            + " ORDER BY rel.rowid DESC LIMIT ?",
            [*params, limit + 1],
        ).fetchall()
        items = []
        now = datetime.now(UTC)
        for row in rows[:limit]:
            relation = json.loads(row["payload"])
            blocking, evidence = [], []
            support = relation.get("supporting_claims", [])
            if not support:
                blocking.append("support_missing")
            for role in ("supporting_claims", "opposing_claims"):
                for cid in relation.get(role, [])[:20]:
                    try:
                        claim_row = knowledge._row(db, owner, cid)
                    except HTTPException:
                        if role == "supporting_claims":
                            blocking.append("claim_inaccessible")
                        continue
                    if claim_row["run_id"] != row["run_id"]:
                        blocking.append("cross_run_reference_invalid")
                        continue
                    claim, review = json.loads(claim_row["payload"]), knowledge._present(claim_row, now=now)
                    if role == "supporting_claims" and (
                        review["citation_status"] != "valid"
                        or review["effective_state"] in {"expired", "retracted", "disputed", "superseded"}
                    ):
                        blocking.append("support_ineligible")
                    refs = []
                    for pid in claim.get("supports", [])[:5]:
                        passage = db.execute(
                            "SELECT p.payload,d.payload AS document FROM research_passages p "
                            "JOIN research_documents d ON d.document_id=p.document_id AND d.run_id=p.run_id "
                            "WHERE p.passage_id=? AND p.run_id=?",
                            [pid, row["run_id"]],
                        ).fetchone()
                        if passage:
                            p, d = json.loads(passage["payload"]), json.loads(passage["document"])
                            refs.append(
                                {
                                    "passage_id": pid,
                                    "url": d.get("url"),
                                    "title": d.get("title"),
                                    "published_at": d.get("published_at"),
                                    "excerpt": p.get("excerpt", "")[:1000],
                                    "content_scope": p.get("content_scope"),
                                    "document_gaps": d.get("gaps", []),
                                }
                            )
                    if role == "supporting_claims" and not refs:
                        blocking.append("original_passage_missing")
                    evidence.append(
                        {
                            "claim_id": cid,
                            "role": role,
                            "statement": review["statement"],
                            "kind": review["kind"],
                            "period": review["period"],
                            "citation_status": review["citation_status"],
                            "effective_state": review["effective_state"],
                            "semantic_status": review["semantic_status"],
                            "sources": refs,
                        }
                    )
            if relation.get("opposing_claims"):
                blocking.append("opposing_evidence_requires_review")
            item = {
                **relation,
                "sequence": row["sequence"],
                "run_id": row["run_id"],
                "blocking_gaps": sorted(set(blocking)),
                "evidence": evidence,
                "status": "references_require_review" if not blocking else "needs_evidence",
                "target": {"view": "reports", "run_id": row["run_id"]},
            }
            items.append(item)
    dimensions = []
    if compact:
        for item in items:
            item["evidence_detail_scope"] = "bounded_summary_read_original_run_for_full_evidence"
            item["mechanism"] = item.get("mechanism", "")[:600]
            item["invalidators"] = [text[:200] for text in item.get("invalidators", [])[:4]]
            item["evidence"] = item["evidence"][:2]
            for claim in item["evidence"]:
                claim["statement"] = claim["statement"][:300]
                claim["sources"] = claim["sources"][:1]
                for source in claim["sources"]:
                    source.pop("excerpt", None)
    for key, question in DIMENSIONS.items():
        relevant = [row for row in items if row.get("dimension") == key]
        dimensions.append(
            {
                "dimension": key,
                "question": question,
                "relation_ids": [row["relation_id"] for row in relevant],
                "status": "references_require_review" if relevant else "not_collected",
            }
        )
    return source_display_payload(
        {
            "relations": items,
            "transmission_paths": _paths(items)[:8] if compact else _paths(items),
            "dimensions": dimensions,
            "next_cursor": rows[limit - 1]["sequence"] if len(rows) > limit else None,
            "scope": "owned_structured_research_relations",
            "evidence_scope": "summary_with_original_links" if compact else "bounded_original_excerpts",
            "query": query,
            "bottleneck_status": "requires_capacity_substitution_and_customer_evidence",
            "notice": "来自本人研究的关系候选与原始节选。相同产业、引用数量和连线度数不能证明真实供货、垄断或投资价值；跨环节路径仍须核对产品、期间、产能、替代与终端需求。",
        }
    )
