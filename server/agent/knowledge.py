"""Owned historical research retrieval. Stored conclusions never become verified facts."""

from __future__ import annotations

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from ..database import get_db
from ..source_urls import source_display_payload
from .public_text import public_answer_text


def _text(value) -> str:
    return public_answer_text(value)


def _payload(raw):
    if not isinstance(raw, str) or len(raw) > 400_000:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _documents(user: str, *, document_id: str | None = None):
    """Bounded first version; no external index can bypass account/deletion checks."""
    with get_db() as db:
        if not db.execute("SELECT 1 FROM users WHERE username=? AND disabled_at IS NULL", [user]).fetchone():
            raise HTTPException(404, "研究记录不存在或不可访问")
        report_filter = (
            " AND s.id=?"
            if document_id and document_id.startswith("report:")
            else (" AND 0" if document_id else "")
        )
        research_filter = (
            " AND r.run_id=?"
            if document_id and document_id.startswith("research:")
            else (" AND 0" if document_id else "")
        )
        reports = db.execute(
            "SELECT id,conv_id,title,report_json,updated_at FROM saved_reports s WHERE username=? "
            "AND (conv_id IS NULL OR EXISTS (SELECT 1 FROM conversations c WHERE c.id=s.conv_id "
            "AND c.username=s.username AND c.deleted_at IS NULL))"
            + report_filter
            + " ORDER BY updated_at DESC LIMIT 200",
            [user, document_id.split(":", 1)[1]] if report_filter == " AND s.id=?" else [user],
        ).fetchall()
        rows = db.execute(
            "SELECT r.run_id,r.payload,v.payload AS revision FROM research_runs r "
            "JOIN research_revisions v ON v.run_id=r.run_id WHERE r.username=? "
            "AND (r.conv_id IS NULL OR EXISTS (SELECT 1 FROM conversations c WHERE c.id=r.conv_id "
            "AND c.username=r.username AND c.deleted_at IS NULL))"
            + research_filter
            + " ORDER BY r.created_at DESC LIMIT 200",
            [user, document_id.split(":", 1)[1]] if research_filter == " AND r.run_id=?" else [user],
        ).fetchall()
    documents = []
    for row in rows:
        run, revision = _payload(row["payload"]), _payload(row["revision"])
        documents.append(
            {
                "id": "research:" + row["run_id"],
                "run_id": row["run_id"],
                "title": _text(run.get("question", ""))[:160],
                "text": _text(revision)[:100000],
                "research_observed_at": run.get("as_of"),
                "saved_at": revision.get("created_at"),
                "status": run.get("status", "unknown"),
                "semantic_status": "unreviewed",
                "source_kind": "historical_research",
                "target": {"view": "reports", "run_id": row["run_id"]},
            }
        )
    for row in reports:
        report = _payload(row["report_json"])
        context = report.get("research") if isinstance(report.get("research"), dict) else {}
        documents.append(
            {
                "id": "report:" + row["id"],
                "run_id": report.get("run_id"),
                "title": _text(row["title"]),
                "text": _text(report)[:100000],
                "research_observed_at": context.get("as_of"),
                "saved_at": datetime.fromtimestamp(row["updated_at"], ZoneInfo("Asia/Shanghai")).isoformat()
                if isinstance(row["updated_at"], (int, float))
                else None,
                "status": report.get("status", "historical"),
                "semantic_status": "unreviewed",
                "source_kind": "historical_research",
                "target": {"view": "reports", "id": row["id"]},
            }
        )
    return documents


def search(user: str, query: str, *, limit: int = 8, exclude_run_id: str | None = None) -> dict:
    if (
        not isinstance(query, str)
        or not 1 <= len(query.strip()) <= 160
        or type(limit) is not int
        or not 1 <= limit <= 20
    ):
        raise HTTPException(422, "知识检索参数无效")
    terms = list(dict.fromkeys(re.findall(r"[\w.\-]+", query.casefold())))[:12]
    matches = []
    seen = set()
    for doc in _documents(user):
        if exclude_run_id and doc["run_id"] == exclude_run_id:
            continue
        key = doc["run_id"] or doc["id"]
        haystack = (doc["title"] + "\n" + doc["text"]).casefold()
        score = sum(1 + (term in doc["title"].casefold()) * 2 for term in terms if term in haystack)
        if not score or key in seen:
            continue
        seen.add(key)
        at = min((haystack.find(term) for term in terms if term in haystack), default=0)
        excerpt = (doc["title"] + "\n" + doc["text"])[max(0, at - 80) : at + 800]
        matches.append((score, {k: v for k, v in doc.items() if k != "text"} | {"excerpt": excerpt}))
    matches.sort(key=lambda item: item[0], reverse=True)
    return source_display_payload(
        {
            "results": [row for _, row in matches[:limit]],
            "index_scope": "latest_200_reports_and_200_research_versions",
            "notice": "历史研究及候选判断，尚未经语义核验。观察时间不代表数据更新时间；复用前检查原始证据与时效。",
        }
    )


def get(user: str, document_id: str) -> dict:
    if not isinstance(document_id, str) or not re.fullmatch(
        r"(?:research|report):[A-Za-z0-9_-]{1,128}", document_id
    ):
        raise HTTPException(404, "研究记录不存在或不可访问")
    for doc in _documents(user, document_id=document_id):
        if doc["id"] == document_id:
            return source_display_payload(
                {"document": doc, "notice": "内部历史研究不是外部原始披露，结论仍为待核验。"}
            )
    raise HTTPException(404, "研究记录不存在或不可访问")
