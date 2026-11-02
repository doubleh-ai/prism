"""A live, owned claim index with versioned human review and correction links.

Every persisted research claim is discoverable; no latest-200 cutoff or copied
private text index. Review never rewrites the historical report or certifies a
model claim as fact. Retrieval checks permissions, expiry and correction state.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import Field, model_validator

from ..auth import require_auth, verify_csrf
from ..database import get_db
from ..productivity.models import ClosedModel, canonical_instant, clean_text
from ..productivity.routes import _body
from ..productivity.store import _active

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])
STATES = {"candidate", "reviewed", "disputed", "retracted"}


def init_knowledge_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS knowledge_reviews (
            claim_id TEXT PRIMARY KEY, owner TEXT NOT NULL, version INTEGER NOT NULL,
            state TEXT NOT NULL, note TEXT NOT NULL, valid_until TEXT,
            superseded_by TEXT, updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS knowledge_review_owner ON knowledge_reviews(owner,updated_at);
        CREATE TABLE IF NOT EXISTS knowledge_review_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, claim_id TEXT NOT NULL,
            version INTEGER NOT NULL, state TEXT NOT NULL, note TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS knowledge_uses (
            owner TEXT NOT NULL, source_claim_id TEXT NOT NULL,
            consumer_run_id TEXT NOT NULL, review_version INTEGER NOT NULL,
            PRIMARY KEY(owner,source_claim_id,consumer_run_id)
        );
        CREATE TRIGGER IF NOT EXISTS knowledge_account_delete AFTER DELETE ON users BEGIN
            DELETE FROM knowledge_reviews WHERE owner=OLD.username;
            DELETE FROM knowledge_review_events WHERE owner=OLD.username;
            DELETE FROM knowledge_uses WHERE owner=OLD.username;
        END;
        """)


def _owner(request):
    owner = require_auth(request)
    verify_csrf(request)
    if request.headers.get("X-Prism-Account", owner) != owner:
        raise HTTPException(409, "账号已经切换，请刷新页面")
    return owner


_SELECT = (
    "SELECT c.rowid AS sequence,c.claim_id,c.run_id,c.payload,r.payload AS run_payload,"
    "v.version,v.state,v.note,v.valid_until,v.superseded_by,v.updated_at "
    "FROM research_claims c JOIN research_runs r ON r.run_id=c.run_id "
    "LEFT JOIN knowledge_reviews v ON v.claim_id=c.claim_id AND v.owner=r.username "
    "WHERE r.username=? AND (r.conv_id IS NULL OR EXISTS(SELECT 1 FROM conversations conv "
    "WHERE conv.id=r.conv_id AND conv.username=r.username AND conv.deleted_at IS NULL)) "
)


def _row(db, owner, claim_id):
    row = db.execute(_SELECT + " AND c.claim_id=?", [owner, claim_id]).fetchone()
    if row is None:
        raise HTTPException(404, "知识条目不存在或不可访问")
    return row


def _present(row, *, now=None):
    now = now or datetime.now(UTC)
    claim, run = json.loads(row["payload"]), json.loads(row["run_payload"])
    state = row["state"] or "candidate"
    if row["superseded_by"]:
        effective = "superseded"
    elif state == "reviewed" and row["valid_until"] and datetime.fromisoformat(row["valid_until"]) <= now:
        effective = "expired"
    else:
        effective = state
    return {
        "id": row["claim_id"],
        "run_id": row["run_id"],
        "sequence": row["sequence"],
        "statement": claim.get("statement", ""),
        "kind": claim.get("kind", "hypothesis"),
        "entities": claim.get("entities", []),
        "period": claim.get("period"),
        "metric": claim.get("metric"),
        "value": claim.get("value"),
        "unit": claim.get("unit"),
        "currency": claim.get("currency"),
        "accounting_basis": claim.get("accounting_basis"),
        "citation_status": claim.get("citation_status", "unknown"),
        "state": state,
        "effective_state": effective,
        "version": row["version"] or 0,
        "review_note": row["note"] or "",
        "valid_until": row["valid_until"],
        "superseded_by": row["superseded_by"],
        "reviewed_at": row["updated_at"],
        "research_as_of": run.get("as_of"),
        "limitations": claim.get("limitations", []),
        "target": {"view": "reports", "run_id": row["run_id"]},
        "semantic_status": "owner_reviewed" if effective == "reviewed" else "unreviewed",
    }


def search(owner, *, query="", before=None, limit=30, exclude_run_id=None, reusable_only=False):
    if type(query) is not str or len(query) > 160 or type(limit) is not int or not 1 <= limit <= 100:
        raise HTTPException(422, "知识检索参数无效")
    init_knowledge_db()
    params = [owner]
    clause = ""
    if query.strip():
        escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clause += " AND c.payload LIKE ? ESCAPE '\\'"
        params.append("%" + escaped + "%")
    if before is not None:
        if type(before) is not int or before < 1:
            raise HTTPException(422, "分页参数无效")
        clause += " AND c.rowid<?"
        params.append(before)
    if exclude_run_id:
        clause += " AND c.run_id<>?"
        params.append(exclude_run_id)
    if reusable_only:
        clause += (
            " AND (v.state IS NULL OR v.state NOT IN ('disputed','retracted')) AND v.superseded_by IS NULL "
        )
        clause += " AND (v.valid_until IS NULL OR v.valid_until>?)"
        params.append(datetime.now(UTC).isoformat().replace("+00:00", "Z"))
    with get_db() as db:
        _active(db, owner)
        rows = db.execute(
            _SELECT + clause + " ORDER BY c.rowid DESC LIMIT ?", [*params, limit + 1]
        ).fetchall()
    return {
        "items": [_present(row) for row in rows[:limit]],
        "next_cursor": rows[limit - 1]["sequence"] if len(rows) > limit else None,
        "notice": "历史结论需核对原始资料；用户复核不等于外部事实认证。",
    }


def detail(owner, claim_id):
    init_knowledge_db()
    with get_db() as db:
        _active(db, owner)
        row = _row(db, owner, claim_id)
        claim = json.loads(row["payload"])
        supporting = set(claim.get("supports", []))
        passages = [
            json.loads(r[0])
            for r in db.execute("SELECT payload FROM research_passages WHERE run_id=?", [row["run_id"]])
        ]
        documents = {
            r["document_id"]: r
            for r in (
                json.loads(r[0])
                for r in db.execute("SELECT payload FROM research_documents WHERE run_id=?", [row["run_id"]])
            )
        }
        sources = [
            {
                "passage_id": p["passage_id"],
                "excerpt": p.get("excerpt", ""),
                "url": documents.get(p["document_id"], {}).get("url"),
                "published_at": documents.get(p["document_id"], {}).get("published_at"),
                "content_scope": p.get("content_scope", "unknown"),
            }
            for p in passages
            if p["passage_id"] in supporting
        ]
        events = [
            dict(r)
            for r in db.execute(
                "SELECT version,state,note,created_at FROM knowledge_review_events "
                "WHERE owner=? AND claim_id=? ORDER BY version DESC LIMIT 50",
                [owner, claim_id],
            )
        ]
        affected = [
            dict(r)
            for r in db.execute(
                "SELECT u.consumer_run_id AS run_id,u.review_version FROM knowledge_uses u "
                "JOIN research_runs r ON r.run_id=u.consumer_run_id AND r.username=u.owner "
                "WHERE u.owner=? AND u.source_claim_id=? AND (r.conv_id IS NULL OR EXISTS(SELECT 1 "
                "FROM conversations c WHERE c.id=r.conv_id AND c.username=r.username AND c.deleted_at IS NULL)) "
                "ORDER BY r.created_at DESC LIMIT 100",
                [owner, claim_id],
            )
        ]
        return {
            "item": _present(row),
            "sources": sources,
            "review_history": events,
            "affected_runs": affected,
        }


def record_usage(owner, consumer_run_id, items):
    """Record exactly which claim versions the current owned research retrieved."""
    init_knowledge_db()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            _active(db, owner)
            consumer = db.execute(
                "SELECT 1 FROM research_runs r WHERE r.run_id=? AND r.username=? AND "
                "(r.conv_id IS NULL OR EXISTS(SELECT 1 FROM conversations c WHERE c.id=r.conv_id "
                "AND c.username=r.username AND c.deleted_at IS NULL))",
                [consumer_run_id, owner],
            ).fetchone()
            if consumer is None:
                raise HTTPException(404, "研究不存在")
            for item in items[:20]:
                row = _row(db, owner, item["id"])
                if row["run_id"] == consumer_run_id:
                    continue
                # Preserve the actual observed version even if review changed
                # between retrieval and this transaction; readback then warns.
                db.execute(
                    "INSERT OR IGNORE INTO knowledge_uses VALUES(?,?,?,?)",
                    [owner, item["id"], consumer_run_id, item["version"]],
                )
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise


def change_notices(db, owner, run_id):
    rows = db.execute(
        "SELECT u.source_claim_id,u.review_version,v.version,v.state,v.valid_until,v.superseded_by "
        "FROM knowledge_uses u JOIN knowledge_reviews v ON v.claim_id=u.source_claim_id AND v.owner=u.owner "
        "WHERE u.owner=? AND u.consumer_run_id=? AND (v.version>u.review_version OR "
        "v.state IN ('disputed','retracted') OR v.valid_until<=?) LIMIT 100",
        [owner, run_id, datetime.now(UTC).isoformat().replace("+00:00", "Z")],
    ).fetchall()
    return [dict(row) for row in rows]


class Review(ClosedModel):
    expected_version: int = Field(ge=0)
    state: Literal["candidate", "reviewed", "disputed", "retracted"]
    note: str = Field(min_length=1, max_length=1000)
    valid_until: str | None = Field(default=None, max_length=40)
    superseded_by: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def review_boundary(self):
        self.note = clean_text(self.note)
        if self.state == "reviewed":
            if self.valid_until is None:
                raise ValueError("review requires expiry")
            self.valid_until = canonical_instant(self.valid_until, "UTC")
            until = datetime.fromisoformat(self.valid_until)
            if not datetime.now(UTC) < until <= datetime.now(UTC) + timedelta(days=366):
                raise ValueError("review expiry must be within one year")
        elif self.valid_until is not None:
            raise ValueError("expiry only belongs to reviewed claims")
        if self.superseded_by and self.state != "retracted":
            raise ValueError("a corrected claim must be retracted")
        return self


def review(owner, claim_id, body):
    init_knowledge_db()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            _active(db, owner)
            row = _row(db, owner, claim_id)
            if (row["version"] or 0) != body.expected_version:
                raise HTTPException(409, "知识复核已更新，请重新读取")
            claim = json.loads(row["payload"])
            if body.state == "reviewed" and (
                claim.get("citation_status") != "valid" or not claim.get("supports")
            ):
                raise HTTPException(409, "原结论的引用尚不完整，请先补充一轮有来源的研究")
            if body.superseded_by:
                replacement = _row(db, owner, body.superseded_by)
                # A correction is a directed, acyclic edge. Replacing a replaced
                # claim is allowed; an existing path back to this claim is not.
                seen = {claim_id}
                for _ in range(100):
                    if replacement["claim_id"] in seen:
                        raise HTTPException(409, "纠错引用形成循环")
                    seen.add(replacement["claim_id"])
                    if not replacement["superseded_by"]:
                        break
                    replacement = _row(db, owner, replacement["superseded_by"])
                else:
                    raise HTTPException(409, "纠错引用过长，请先整理")
            version, stamp = body.expected_version + 1, int(time.time())
            db.execute(
                "INSERT INTO knowledge_reviews VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(claim_id) DO UPDATE SET "
                "version=excluded.version,state=excluded.state,note=excluded.note,valid_until=excluded.valid_until,"
                "superseded_by=excluded.superseded_by,updated_at=excluded.updated_at",
                [
                    claim_id,
                    owner,
                    version,
                    body.state,
                    body.note,
                    body.valid_until,
                    body.superseded_by,
                    stamp,
                ],
            )
            db.execute(
                "INSERT INTO knowledge_review_events(owner,claim_id,version,state,note,created_at) VALUES(?,?,?,?,?,?)",
                [owner, claim_id, version, body.state, body.note, stamp],
            )
            result = _present(_row(db, owner, claim_id))
            db.execute("COMMIT")
            return {"item": result}
        except BaseException:
            db.execute("ROLLBACK")
            raise


@router.get("/items")
async def list_items(
    request: Request,
    query: str = Query(default="", max_length=160),
    before: int | None = Query(default=None, ge=1),
    limit: int = Query(default=30, ge=1, le=100),
):
    return search(_owner(request), query=query, before=before, limit=limit)


@router.get("/items/{claim_id}")
async def read_item(claim_id: str, request: Request):
    if len(claim_id) > 100:
        raise HTTPException(404, "知识条目不存在")
    return detail(_owner(request), claim_id)


@router.put("/items/{claim_id}/review")
async def put_review(claim_id: str, request: Request):
    owner = _owner(request)
    body = await _body(request, Review)
    if _owner(request) != owner:
        raise HTTPException(401, "请重新登录")
    return review(owner, claim_id, body)
